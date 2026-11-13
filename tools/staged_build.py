"""Staged builds: vinca-gha's stages as matrix jobs, for pull requests and releases.

    python tools/staged_build.py plan --distro <d> --platform <p> --os <runner> --cache-dir <dir> --out <file>
    python tools/staged_build.py schedule --out <file> <plan file>...
    python tools/staged_build.py job --schedule <file> --distro <d> --platform <p> --job <id>
    python tools/staged_build.py artifacts --schedule <file> --distro <d> --platform <p>
    python tools/staged_build.py unpack --zips <dir> --into <dir>
    python tools/staged_build.py ready --schedule <file> --distro <d> --platform <p> --job <id> --channel <dir>
    python tools/staged_build.py status --schedule <file> --distro <d> --platform <p> --job <id> --log <file> --out <dir>
    python tools/staged_build.py workflow [--check]

.github/workflows/staged-build.yaml builds the given distributions and platforms in
stages, one job per batch of recipes. GitHub can't add jobs to a running workflow, so
it has a fixed number of stage slots (SLOTS) for all of them together: stage_<k> runs
stage k of every distribution and platform, each job as a matrix entry, after
stage_<k-1>.

- `plan` (one job per distribution and platform) lists the stages' jobs with
  `vinca-gha --json`, leaving out the recipes whose every build is in the restored
  pull-request cache.
- `schedule` puts the plans into the slots (at most MAX_MATRIX jobs each, GitHub's
  matrix limit) and writes the schedule every stage job reads its recipes from.
- `job` is a stage job's recipes and the artifacts it needs: the packages of the jobs
  that build its requirements (vinca's `needs`, and theirs), and the restored cache.
  tools/download_artifacts.cjs downloads them by name (`artifacts` lists a distribution
  and platform's for collecting them), `unpack` merges them into one directory.
- `ready` leaves out the job's recipes that need a recipe that wasn't built (vinca's
  `requires`, in this job or the ones it needs), `status` records how each recipe went.
- `workflow` writes staged-build.yaml (`rs check` checks that it is up to date).

Packages pass between jobs as artifacts, pkgs-<distro>-<platform>-<job>, each with
status/<job> (success, or failure when any of its recipes wasn't built) and
status/<job>.json ({recipe: success, failed, skipped or blocked}): a recipe that failed
holds back only the recipes that need it, and the other distributions and platforms go on.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import zipfile
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_timings
import robostack as rs

SLOTS = 64  # lyrical's full linux-64 rebuild has 53 stages at --batch_size 25
MAX_MATRIX = 256  # GitHub's limit of jobs per matrix
MAX_NAME = 60  # job names are a stage's packages, which can be many
WORKFLOW = rs.ROOT / ".github" / "workflows" / "staged-build.yaml"
PLACEHOLDER = {"distro": "-", "platform": "-", "os": "ubuntu-latest", "job": "-", "name": "-"}


def artifact(distro: str, platform: str, job: str) -> str:
    return f"pkgs-{distro}-{platform}-{job}"


def cached_recipes(recipes: Path, platform: str, cache_dir: Path) -> set[str]:
    """The recipes whose every build (one per variant) is in the cache (<subdir>/*.conda)."""
    names = {p.parent.name for p in recipes.glob("*/recipe.yaml")}
    variants = build_timings.variant_builds(recipes, platform) if names else {}
    cached = Counter(f.name.rsplit("-", 2)[0] for f in cache_dir.glob("*/*.conda"))
    return {r for r in names if cached[r] >= variants.get(r, 1)}


def fold(stages: list[list[dict]], slots: int = SLOTS) -> list[list[dict]]:
    """The stages as at most `slots` stages: the ones past the last slot become one job
    there (build-ci orders a job's recipes by their dependencies)."""
    if len(stages) <= slots:
        return stages
    rest = [job for stage in stages[slots - 1:] for job in stage]
    ids = {job["id"] for job in rest}
    tail = {
        "id": f"stage_{slots - 1}_rest",
        "name": f"{len(rest)} jobs of stages {slots - 1}+",
        "recipes": " ".join(job["recipes"] for job in rest),
        "needs": sorted({n for job in rest for n in job["needs"]} - ids),
        "requires": {r: deps for job in rest for r, deps in job.get("requires", {}).items()},
    }
    return [*stages[:slots - 1], [tail]]


def plan(distro: str, platform: str, cache_dir: Path, batch_size: int) -> list[list[dict]]:
    w = rs.work_dir(distro)
    recipes = w / "recipes"
    cached = cached_recipes(recipes, platform, cache_dir) if recipes.is_dir() else set()
    # vinca-gha stages what is in ./recipes; the cached recipes count as built, like the
    # ones on the channel that generate-recipes -n leaves out
    aside = w / "recipes-cached"
    shutil.rmtree(aside, ignore_errors=True)
    aside.mkdir()
    for r in cached:
        (recipes / r).rename(aside / r)
    try:
        out = w / "stages.json"
        cmd = ["vinca-gha", "--platform", platform, "-d", "./recipes", "--batch_size", str(batch_size),
               "--json", out.name]
        if build_timings.vinca_timings(distro, platform, w / "build_timings.json"):
            cmd += ["--timings", "build_timings.json"]
        if rs.run(cmd, w):
            raise SystemExit("vinca-gha failed")
        stages = json.loads(out.read_text())["stages"]
    finally:
        for r in cached:
            (aside / r).rename(recipes / r)
        aside.rmdir()
    print(f"{len(cached)} recipes fully in the cache; {sum(map(len, stages))} jobs in {len(stages)} stages")
    return fold(stages)


def merge_smallest(stages: list[list[dict]], k: int) -> None:
    """Merge the two jobs of stage k with the fewest recipes into one."""
    a, b = sorted(stages[k], key=lambda job: len(job["recipes"].split()))[:2]
    a["recipes"] = f"{a['recipes']} {b['recipes']}"
    a["name"] = f"{a['name']} {b['name']}"
    a["needs"] = sorted(set(a["needs"]) | set(b["needs"]))
    a["requires"] = {**a.get("requires", {}), **b.get("requires", {})}
    stages[k].remove(b)
    for stage in stages[k + 1:]:
        for job in stage:
            if b["id"] in job["needs"]:
                job["needs"] = sorted((set(job["needs"]) - {b["id"]}) | {a["id"]})


def schedule(plans: list[dict], slots: int = SLOTS, max_matrix: int = MAX_MATRIX) -> tuple[list[list[dict]], dict]:
    """The slots' matrix entries, and each distribution and platform's jobs by id
    ({"<distro>/<platform>": {id: {"recipes", "needs", "requires"}}}). A slot with more
    than max_matrix jobs has the smallest jobs of its largest plans merged."""
    entries = []
    for k in range(slots):
        while sum(len(p["stages"][k]) for p in plans if k < len(p["stages"])) > max_matrix:
            largest = max((p for p in plans if k < len(p["stages"])), key=lambda p: len(p["stages"][k]))
            merge_smallest(largest["stages"], k)
        entries.append([
            {"distro": p["distro"], "platform": p["platform"], "os": p["os"], "job": job["id"],
             "name": short(f"{p['distro']} {p['platform']} {job['name']}")}
            for p in plans if k < len(p["stages"]) for job in p["stages"][k]
        ])
    while entries and not entries[-1]:
        entries.pop()
    jobs = {f"{p['distro']}/{p['platform']}": {job["id"]: {"recipes": job["recipes"], "needs": job["needs"],
                                                           "requires": job.get("requires", {})}
                                                for stage in p["stages"] for job in stage}
            for p in plans}
    return entries, jobs


def short(name: str) -> str:
    return name if len(name) <= MAX_NAME else name[:MAX_NAME - 1] + "…"


def job_needs(jobs: dict[str, dict], job: str) -> list[str]:
    """The jobs whose packages the job needs: its needs, and theirs."""
    seen: set[str] = set()
    todo = list(jobs[job]["needs"])
    while todo:
        if (j := todo.pop()) not in seen:
            seen.add(j)
            todo += jobs[j]["needs"]
    return sorted(seen)


def unpack(zips: Path, into: Path) -> None:
    """Extract the downloaded artifacts (tools/download_artifacts.cjs) into one directory."""
    into.mkdir(parents=True, exist_ok=True)
    for z in sorted(zips.glob("*.zip")):
        with zipfile.ZipFile(z) as f:
            f.extractall(into)
        z.unlink()


def needed_status(jobs: dict[str, dict], needed: list[str], channel: Path) -> dict[str, str]:
    """How each recipe of the needed jobs went, from their status/<job>.json; a job
    without one (its artifact missing: it didn't get to the end) built none."""
    status = {}
    for j in needed:
        f = channel / "status" / f"{j}.json"
        recipes = json.loads(f.read_text()) if f.is_file() else {}
        for r in jobs[j]["recipes"].split():
            status[r] = recipes.get(r, "not built")
    return status


def blocked(job: dict, status: dict[str, str]) -> dict[str, str]:
    """The job's recipes that can't be built, each with the requirement it lacks: one
    that the needed jobs didn't build, or one of the job's own recipes held back."""
    out: dict[str, str] = {}
    changed = True
    while changed:
        changed = False
        for r in job["recipes"].split():
            if r in out:
                continue
            lacking = [q for q in job["requires"].get(r, []) if q in out or status.get(q, "success") != "success"]
            if lacking:
                out[r] = lacking[0]
                changed = True
    return out


def outcomes(job: dict, held: dict[str, str], log: str | None) -> dict[str, str]:
    """How each recipe of the job went: blocked (held back), else from the last attempt in
    rattler-build's log (rs build-ci with --continue-on-failure=skip-dependents): failed
    (its build or test), skipped (it needs one that failed in this job), or success. When
    rattler-build itself failed (or there's no log), every recipe that isn't blocked failed.
    rattler-build skips only the outputs that need a failed one to build, so one that only
    runs with it (e.g. ros-<distro>-<name>, of ros2-<name>) counts as skipped here."""
    recipes = job["recipes"].split()
    attempt = re.split(r"^\+ rattler-build ", log, flags=re.M)[-1] if log else ""
    aborted = not attempt or re.search(r"^rattler-build exited with ", attempt, re.M)
    name = lambda identifier: identifier.rsplit("-", 2)[0]  # noqa: E731 (name-version-build)
    failed = {name(i) for i in re.findall(r"(?:Build|Test) failed for (\S+): ", attempt)}
    skipped = {name(i) for i in re.findall(r"Skipping (\S+) because its dependency \S+ failed to build", attempt)}
    out = {}
    for r in recipes:
        if r in held:
            out[r] = "blocked"
        elif r in failed or aborted:
            out[r] = "failed"
        elif r in skipped:
            out[r] = "skipped"
        else:
            out[r] = "success"
    changed = True
    while changed:
        changed = False
        for r in recipes:
            if out[r] == "success" and any(out.get(q, "success") != "success" for q in job["requires"].get(r, [])):
                out[r] = "skipped"
                changed = True
    return out


def workflow() -> str:
    """staged-build.yaml: HEADER, the stage slots, then COLLECT."""
    secrets = json.dumps({d: rs.anaconda_token_secret(d) for d in rs.distros()}, separators=(",", ":"))
    slots = []
    for k in range(SLOTS):
        needs = "schedule" if k == 0 else f"[schedule, stage_{k - 1}]"
        slots.append(SLOT.replace("@K@", str(k)).replace("@NEEDS@", needs).replace("@SECRETS@", secrets))
    outputs = "".join(f"      slot_{k}: ${{{{ steps.schedule.outputs.slot_{k} }}}}\n" for k in range(SLOTS))
    every = ", ".join(["schedule", *(f"stage_{k}" for k in range(SLOTS))])
    return HEADER.replace("@OUTPUTS@", outputs) + "".join(slots) + COLLECT.replace("@NEEDS@", every)


def write_lines(env: str, lines: list[str]) -> None:
    if path := os.environ.get(env):
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("plan", help="the stages of one distribution and platform")
    p.add_argument("--distro", required=True)
    p.add_argument("--platform", required=True)
    p.add_argument("--os", required=True, help="the platform's runner")
    p.add_argument("--cache-dir", required=True, type=Path)
    p.add_argument("--batch-size", type=int, default=25)
    p.add_argument("--out", required=True, type=Path)
    s = sub.add_parser("schedule", help="the plans in the stage slots, as GitHub outputs")
    s.add_argument("--out", required=True, type=Path)
    s.add_argument("plans", nargs="*", type=Path)
    j = sub.add_parser("job", help="a stage job's RECIPES, NEEDED and NEEDED_ARTIFACTS, as GitHub environment")
    a = sub.add_parser("artifacts", help="a distribution and platform's artifacts, ARTIFACTS, as GitHub environment")
    r = sub.add_parser("ready", help="the job's RECIPES whose requirements were built, as GitHub environment")
    t = sub.add_parser("status", help="how each of the job's recipes went, status/<job>.json in --out")
    for p in (j, a, r, t):
        p.add_argument("--schedule", required=True, type=Path)
        p.add_argument("--distro", required=True)
        p.add_argument("--platform", required=True)
    for p in (j, r, t):
        p.add_argument("--job", required=True)
    r.add_argument("--channel", required=True, type=Path, help="the needed jobs' artifacts, unpacked")
    t.add_argument("--log", required=True, type=Path, help="the build's output (rs build-ci)")
    t.add_argument("--out", required=True, type=Path, help="the job's artifact")
    u = sub.add_parser("unpack", help="extract downloaded artifacts into one directory")
    u.add_argument("--zips", required=True, type=Path)
    u.add_argument("--into", required=True, type=Path)
    w = sub.add_parser("workflow", help=f"write {WORKFLOW.relative_to(rs.ROOT)}")
    w.add_argument("--check", action="store_true", help="only check that it is up to date")
    args = parser.parse_args()

    if args.command == "workflow":
        text = workflow()
        if args.check:
            if not WORKFLOW.is_file() or WORKFLOW.read_text() != text:
                print(f"{WORKFLOW.relative_to(rs.ROOT)} is out of date (python tools/staged_build.py workflow)")
                return 1
            return 0
        WORKFLOW.write_text(text)
        return 0

    if args.command == "plan":
        stages = plan(args.distro, args.platform, args.cache_dir.resolve(), args.batch_size)
        doc = {"distro": args.distro, "platform": args.platform, "os": args.os, "stages": stages}
        args.out.write_text(json.dumps(doc) + "\n")
        jobs = sum(map(len, stages))
        write_lines("GITHUB_STEP_SUMMARY", [f"**{args.distro} {args.platform}**: {jobs} build jobs in {len(stages)} stages", ""])
        for k, stage in enumerate(stages):
            print(f"stage {k}: " + "; ".join(job["recipes"] for job in stage))
        return 0

    if args.command == "schedule":
        plans = [json.loads(f.read_text()) for f in sorted(args.plans)]
        entries, jobs = schedule(plans)
        args.out.write_text(json.dumps(jobs) + "\n")
        # the slots past the last stage are skipped, but GitHub reads their matrix anyway
        lines = [f"count={len(entries)}",
                 "combos=" + json.dumps([{"distro": p["distro"], "platform": p["platform"]} for p in plans])]
        lines += [f"slot_{k}=" + json.dumps(entries[k] if k < len(entries) else [PLACEHOLDER], separators=(",", ":"))
                  for k in range(SLOTS)]
        write_lines("GITHUB_OUTPUT", lines)
        print(f"{sum(map(len, entries))} jobs in {len(entries)} stages: " + ", ".join(str(len(e)) for e in entries))
        return 0

    if args.command == "unpack":
        unpack(args.zips, args.into)
        return 0

    jobs = json.loads(args.schedule.read_text())[f"{args.distro}/{args.platform}"]
    if args.command == "artifacts":
        names = [artifact(args.distro, args.platform, j) for j in ["cache", *jobs]]
        write_lines("GITHUB_ENV", [f"ARTIFACTS={' '.join(names)}"])
        print(f"{len(names)} artifacts")
        return 0

    job = jobs[args.job]
    needed = job_needs(jobs, args.job)
    held_file = Path(f"blocked-{args.job}.json")
    if args.command == "job":
        lines = [f"RECIPES={job['recipes']}", f"NEEDED={' '.join(needed)}",
                 "NEEDED_ARTIFACTS=" + " ".join(artifact(args.distro, args.platform, j) for j in ["cache", *needed])]
        write_lines("GITHUB_ENV", lines)
        print("\n".join(lines))
        return 0

    if args.command == "ready":
        status = needed_status(jobs, needed, args.channel)
        held = blocked(job, status)
        held_file.write_text(json.dumps(held) + "\n")
        for r, q in held.items():
            print(f"{r}: not built, as it needs {q} ({status.get(q, 'held back in this job')})")
        ready = [r for r in job["recipes"].split() if r not in held]
        write_lines("GITHUB_ENV", [f"RECIPES={' '.join(ready)}"])
        print(f"{len(ready)} of {len(job['recipes'].split())} recipes to build: {' '.join(ready)}")
        return 0

    held = json.loads(held_file.read_text()) if held_file.is_file() else {}
    log = args.log.read_text(encoding="utf-8", errors="replace") if args.log.is_file() else None
    result = outcomes(job, held, log)
    (args.out / "status").mkdir(parents=True, exist_ok=True)
    (args.out / "status" / f"{args.job}.json").write_text(json.dumps(result, indent=1) + "\n")
    ok = all(s == "success" for s in result.values())
    (args.out / "status" / args.job).write_text(("success" if ok else "failure") + "\n")
    lines = [f"| {r} | {s}{f' (needs {held[r]})' if r in held else ''} |" for r, s in result.items() if s != "success"]
    for line in lines:
        print(line.strip("| ").replace(" | ", ": "))
    if lines:
        write_lines("GITHUB_STEP_SUMMARY", ["| recipe | not built |", "|---|---|", *lines, ""])
    # the packages of the recipes that built, to upload (releases)
    built = {r for r, s in result.items() if s == "success"}
    Path("uploads.txt").write_text("".join(f"{p.as_posix()}\n" for p in sorted(args.out.glob("*/*.conda"))
                                           if p.name.rsplit("-", 2)[0] in built))
    print(f"{len(built)} of {len(result)} recipes built")
    return 0 if ok else 1


SETUP = """\
      - uses: actions/checkout@v6
        with:
          persist-credentials: false
      # git auto-maintenance can leave a lock file in the git clones pixi caches
      # (e.g. vinca's), which breaks every later run restoring that cache
      - name: Disable git auto-maintenance
        run: git config --global maintenance.auto false && git config --global gc.auto 0
      - uses: prefix-dev/setup-pixi@v0.11.0
        with:
          locked: true  # the lock file must match pixi.toml
          pixi-version: v0.81.0
          cache-write: false  # saved from main only (pixi-cache.yaml)
"""

HEADER = """\
# Generated by tools/staged_build.py (python tools/staged_build.py workflow); `pixi run rs check`
# checks that it is up to date.
#
# The given distributions and platforms, built in vinca-gha's stages, one job per batch
# of recipes (stage-job.yaml): pull requests (testpr.yaml) keep the packages as the
# pull request's build cache, releases (release.yaml) upload them. See
# tools/staged_build.py for how the stages become jobs.
name: Staged build

on:
  workflow_call:
    inputs:
      distros:
        description: JSON list of distributions
        required: true
        type: string
      targets:
        description: 'JSON list of platforms and their runners, [{"os": ..., "platform": ...}]'
        required: true
        type: string
      upload:
        description: Upload the packages to the distributions' channels (else keep them as the pull request's cache)
        required: true
        type: boolean

env:
  PYTHONIOENCODING: utf-8
  # while uploads go to a test channel, packages already there aren't built again
  ROBOSTACK_UPLOAD_CHANNEL: ${{ vars.ROBOSTACK_UPLOAD_CHANNEL }}
  # the pull request's packages, <subdir>/*.conda; restored and saved on Linux for every platform
  CACHE_DIR: pr-cache

jobs:
  plan:
    strategy:
      fail-fast: false
      matrix:
        distro: ${{ fromJSON(inputs.distros) }}
        target: ${{ fromJSON(inputs.targets) }}
    name: plan ${{ matrix.distro }} ${{ matrix.target.platform }}
    runs-on: ubuntu-latest
    env:
      DISTRO: ${{ matrix.distro }}
      PLATFORM: ${{ matrix.target.platform }}
    steps:
""" + SETUP + """\
      - name: Generate recipes
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: |
          set -e
          for attempt in 1 2 3; do
            if pixi run rs "$DISTRO" generate-recipes --platform "$PLATFORM" -n; then break; fi
            if [[ "$attempt" == 3 ]]; then echo "Recipe generation failed after 3 attempts" >&2; exit 1; fi
            echo "Recipe generation attempt $attempt failed; retrying..." >&2
            sleep 15
          done
      # a tarball: artifacts drop the build scripts' executable bit
      - name: Pack the recipes
        run: mkdir -p "distros/$DISTRO/work/recipes" && tar -cf recipes.tar -C "distros/$DISTRO/work" recipes
      - name: Upload the recipes
        uses: actions/upload-artifact@v6
        with:
          name: recipes-${{ matrix.distro }}-${{ matrix.target.platform }}
          path: recipes.tar
          overwrite: true
          retention-days: 7
      - name: Restore build cache
        if: ${{ !inputs.upload }}
        uses: actions/cache/restore@v5
        with:
          path: ${{ env.CACHE_DIR }}
          key: staged-${{ matrix.distro }}-${{ matrix.target.platform }}-pr-${{ github.event.pull_request.number }}
          restore-keys: |
            staged-${{ matrix.distro }}-${{ matrix.target.platform }}-pr-${{ github.event.pull_request.number }}-
      # Temporary rebuild controls live in distros/<distro>/ci.yaml.
      - name: Apply ci.yaml cache controls
        run: |
          for subdir in "$PLATFORM" noarch; do
            mkdir -p "$CACHE_DIR/$subdir"
            pixi run python tools/ci_config.py --cache-dir "$CACHE_DIR/$subdir" --config "distros/$DISTRO/ci.yaml" --vinca "distros/$DISTRO/vinca.yaml"
          done
          shopt -s nullglob
          packages=("$CACHE_DIR"/*/*.conda)
          echo "${#packages[@]} packages restored from the cache"
          for package in "${packages[@]}"; do basename "$package"; done
          mkdir -p "$CACHE_DIR/status" && echo success > "$CACHE_DIR/status/cache"
      - name: Upload the cached packages
        uses: actions/upload-artifact@v6
        with:
          name: pkgs-${{ matrix.distro }}-${{ matrix.target.platform }}-cache
          path: ${{ env.CACHE_DIR }}
          compression-level: 0  # .conda files are compressed
          overwrite: true
          retention-days: 3
      - name: Estimate the build time
        run: pixi run python tools/ci_estimate.py --recipes "distros/$DISTRO/work/recipes" --cache-dir "$CACHE_DIR/$PLATFORM" --platform "$PLATFORM"
      - name: Plan the stages
        env:
          OS: ${{ matrix.target.os }}
        run: pixi run python tools/staged_build.py plan --distro "$DISTRO" --platform "$PLATFORM" --os "$OS" --cache-dir "$CACHE_DIR" --out "plan-$DISTRO-$PLATFORM.json"
      - uses: actions/upload-artifact@v6
        with:
          name: plan-${{ matrix.distro }}-${{ matrix.target.platform }}
          path: plan-${{ matrix.distro }}-${{ matrix.target.platform }}.json
          overwrite: true
          retention-days: 7

  # Every plan in the stage slots, and the schedule the stage jobs read their recipes from;
  # a failed plan leaves out only its distribution and platform.
  schedule:
    needs: plan
    if: ${{ !cancelled() }}
    runs-on: ubuntu-latest
    outputs:
      count: ${{ steps.schedule.outputs.count }}
      combos: ${{ steps.schedule.outputs.combos }}
@OUTPUTS@    steps:
""" + SETUP + """\
      - uses: actions/download-artifact@v7
        with:
          pattern: plan-*
          merge-multiple: true
          path: plans
      - name: Schedule the stages
        id: schedule
        run: pixi run python tools/staged_build.py schedule --out schedule.json plans/*.json
      - uses: actions/upload-artifact@v6
        with:
          name: schedule
          path: schedule.json
          overwrite: true
          retention-days: 7

"""

# A slot runs after the one before even when that one failed: the jobs check that
# their own requirements built (stage-job.yaml), so the other distributions and
# platforms go on.
SLOT = """\
  stage_@K@:
    needs: @NEEDS@
    if: ${{ !cancelled() && needs.schedule.result == 'success' && fromJSON(needs.schedule.outputs.count) > @K@ }}
    strategy:
      fail-fast: false
      matrix:
        include: ${{ fromJSON(needs.schedule.outputs.slot_@K@) }}
    name: @K@ ${{ matrix.name }}
    uses: ./.github/workflows/stage-job.yaml
    secrets: inherit
    with:
      distro: ${{ matrix.distro }}
      platform: ${{ matrix.platform }}
      os: ${{ matrix.os }}
      job: ${{ matrix.job }}
      upload: ${{ inputs.upload }}
      token_secret: ${{ fromJSON('@SECRETS@')[matrix.distro] }}

"""

COLLECT = """\
  # Pull requests: the packages of every stage as the pull request's build cache (a new
  # push builds only what changed), then the ABI check of what the pull request built.
  collect:
    needs: [@NEEDS@]
    if: ${{ always() && !inputs.upload && needs.schedule.result == 'success' }}
    strategy:
      fail-fast: false
      matrix:
        include: ${{ fromJSON(needs.schedule.outputs.combos) }}
    name: collect ${{ matrix.distro }} ${{ matrix.platform }}
    runs-on: ubuntu-latest
    env:
      DISTRO: ${{ matrix.distro }}
      PLATFORM: ${{ matrix.platform }}
    steps:
""" + SETUP + """\
      - uses: actions/download-artifact@v7
        with:
          name: schedule
      - name: The stage jobs' artifacts
        run: pixi run python tools/staged_build.py artifacts --schedule schedule.json --distro "$DISTRO" --platform "$PLATFORM"
      # by name: download-artifact's pattern only sees the run's first 1,000 artifacts
      - name: Download the stage jobs' packages
        uses: actions/github-script@v8
        with:
          script: |
            await require('./tools/download_artifacts.cjs')({ names: process.env.ARTIFACTS.split(' '), dir: 'artifacts', core })
      - name: See the packages
        run: |
          pixi run python tools/staged_build.py unpack --zips artifacts --into "$CACHE_DIR"
          rm -rf "$CACHE_DIR/status"
          shopt -s nullglob
          packages=("$CACHE_DIR"/*/*.conda)
          echo "${#packages[@]} packages"
          for package in "${packages[@]}"; do basename "$package"; done
      - name: Save build cache
        id: save-cache
        uses: actions/cache/save@v5
        with:
          path: ${{ env.CACHE_DIR }}
          key: staged-${{ matrix.distro }}-${{ matrix.platform }}-pr-${{ github.event.pull_request.number }}-${{ github.run_id }}-${{ github.run_attempt }}

      # Only the newest cache of a pull request/distribution/platform is restored; the ones
      # of earlier runs and attempts only push others out of the repository's 10 GB, so
      # full rebuilds lost their progress. Forks have a read-only token, so skip deletion.
      - name: Drop this pull request's superseded build caches
        if: ${{ steps.save-cache.outcome == 'success' && github.event.pull_request.number && github.event.pull_request.head.repo.full_name == github.repository }}
        continue-on-error: true
        env:
          GH_TOKEN: ${{ github.token }}
          PREFIX: staged-${{ matrix.distro }}-${{ matrix.platform }}-pr-${{ github.event.pull_request.number }}-
          KEEP: staged-${{ matrix.distro }}-${{ matrix.platform }}-pr-${{ github.event.pull_request.number }}-${{ github.run_id }}-${{ github.run_attempt }}
        run: |
          # cache/save reports success even on upload failure: verify the replacement exists.
          if ! ids=$(pixi run gh cache list --repo "$GITHUB_REPOSITORY" --ref "$GITHUB_REF" --key "$PREFIX" --limit 100 --json id,key \\
            --jq "if any(.[]; .key == \\"$KEEP\\") then .[] | select(.key != \\"$KEEP\\") | .id else error(\\"New cache not found\\") end"); then
            echo "::warning::Could not verify the new build cache; keeping all previous caches."
            exit 0
          fi
          while read -r id; do
            [ -n "$id" ] || continue
            pixi run gh cache delete "$id" --repo "$GITHUB_REPOSITORY" && echo "deleted cache $id"
          done <<< "$ids"

      # Compare what the pull request built with the released builds, so single packages
      # can be bumped without a full rebuild; it reports and never fails the build.
      - name: ABI check against the released builds
        id: abi-check
        if: matrix.platform == 'linux-64'
        continue-on-error: true
        run: |
          pixi run abi-check --distro "$DISTRO" --platform "$PLATFORM" --pr-builds "$CACHE_DIR/$PLATFORM" --summary "abi-check-$DISTRO.md"
          cat "abi-check-$DISTRO.md" >> "$GITHUB_STEP_SUMMARY"
      - name: Upload the ABI check
        if: ${{ steps.abi-check.outcome == 'success' }}
        uses: actions/upload-artifact@v6
        with:
          name: abi-check-${{ matrix.distro }}
          path: abi-check-${{ matrix.distro }}.md
          retention-days: 7

      # the build branches' workflows, while they still build the releases
      - uses: actions/download-artifact@v7
        with:
          name: recipes-${{ matrix.distro }}-${{ matrix.platform }}
      - name: Prepare the work directory
        run: pixi run rs "$DISTRO" prepare && tar -xf recipes.tar -C "distros/$DISTRO/work"
      - name: Generate the staged build workflows (catches post-merge problems)
        run: |
          if [ -z "$(ls "distros/$DISTRO/work/recipes" 2>/dev/null)" ]; then echo "Nothing to build"; exit 0; fi
          for attempt in 1 2 3; do
            if pixi run rs "$DISTRO" gha --platform "$PLATFORM" --batch-size 25; then break; fi
            if [[ "$attempt" == 3 ]]; then echo "Workflow generation failed after 3 attempts" >&2; exit 1; fi
            echo "Workflow generation attempt $attempt failed; retrying..." >&2
            sleep 15
          done
"""


if __name__ == "__main__":
    sys.exit(main())
