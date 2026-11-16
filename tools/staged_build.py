"""Staged builds: the rendered build workflows' jobs (tools/rerender.py), for pull requests and releases.

    python tools/staged_build.py schedule --distro <d> --platform <p> --cache-dir <dir> --out <file>
    python tools/staged_build.py job --schedule <file> --distro <d> --platform <p> --job <id>
    python tools/staged_build.py artifacts --schedule <file> --distro <d> --platform <p>
    python tools/staged_build.py unpack --zips <dir> --into <dir>
    python tools/staged_build.py ready --schedule <file> --distro <d> --platform <p> --job <id> --channel <dir>
    python tools/staged_build.py status --schedule <file> --distro <d> --platform <p> --job <id> --log <file> --out <dir>

.github/workflows/build-<distro>-<platform>.yaml has a job for every batch of the
distribution's recipes, each after the jobs building its recipes' requirements.

- `schedule` (staged-plan.yaml) keeps of each job the recipes to build: those
  generate-recipes -n leaves (missing on the channel) whose every build isn't in the
  restored pull-request cache. Only the jobs with one of them run (the `todo` output);
  every stage job reads its recipes from the schedule.
- `job` is a stage job's recipes and the artifacts it needs: the packages of the jobs
  that build its requirements (the workflow's `needs`, and theirs) and that run, and the
  restored cache. tools/download_artifacts.cjs downloads them by name (`artifacts` lists a
  distribution and platform's for collecting them), `unpack` merges them into one directory.
- `ready` leaves out the job's recipes that need a recipe that wasn't built (vinca's
  `requires`, in this job or the ones it needs), `status` records how each recipe went.

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
import sys
import zipfile
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_timings
import robostack as rs



def artifact(distro: str, platform: str, job: str) -> str:
    return f"pkgs-{distro}-{platform}-{job}"


def cached_recipes(recipes: Path, platform: str, cache_dir: Path) -> set[str]:
    """The recipes whose every build (one per variant) is in the cache (<subdir>/*.conda)."""
    names = {p.parent.name for p in recipes.glob("*/recipe.yaml")}
    variants = build_timings.variant_builds(recipes, platform) if names else {}
    cached = Counter(f.name.rsplit("-", 2)[0] for f in cache_dir.glob("*/*.conda"))
    return {r for r in names if cached[r] >= variants.get(r, 1)}


def schedule(jobs: dict[str, dict], requires: dict[str, list[str]], cached: set[str]) -> dict[str, dict]:
    """The rendered jobs ({id: {"recipes", "needs"}}, rerender.parse) with the recipes to
    build: those in `requires` (generate-recipes -n's, with their requirements) that
    aren't cached. A recipe to build that no job has means the workflow wasn't re-rendered."""
    job_of = {r: j for j, job in jobs.items() for r in job["recipes"]}
    if unknown := sorted(set(requires) - job_of.keys()):
        raise SystemExit(f"{len(unknown)} recipes are in no job of the workflow, e.g. {', '.join(unknown[:5])}: "
                         "it is out of date (pixi run rs rerender)")
    return {j: {"recipes": " ".join(r for r in job["recipes"] if r in requires and r not in cached),
                "needs": job["needs"], "requires": {r: requires[r] for r in job["recipes"] if r in requires}}
            for j, job in jobs.items()}


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


def write_lines(env: str, lines: list[str]) -> None:
    if path := os.environ.get(env):
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("schedule", help="the jobs' recipes to build, and the jobs to run as the todo output")
    s.add_argument("--distro", required=True)
    s.add_argument("--platform", required=True)
    s.add_argument("--cache-dir", required=True, type=Path)
    s.add_argument("--out", required=True, type=Path)
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
    args = parser.parse_args()

    if args.command == "schedule":
        import rerender

        w = rs.work_dir(args.distro)
        jobs = rerender.parse(rerender.workflow_path(args.distro, args.platform).read_text())
        requires = rerender.requirements(args.distro, args.platform)
        cached = cached_recipes(w / "recipes", args.platform, args.cache_dir.resolve()) if requires else set()
        planned = schedule(jobs, requires, cached)
        args.out.write_text(json.dumps({f"{args.distro}/{args.platform}": planned}) + "\n")
        todo = sorted(j for j, job in planned.items() if job["recipes"])
        write_lines("GITHUB_OUTPUT", ["todo=" + json.dumps(todo, separators=(",", ":"))])
        recipes = sum(len(job["recipes"].split()) for job in planned.values())
        summary = (f"**{args.distro} {args.platform}**: {recipes} recipes to build ({len(cached)} more in the cache), "
                   f"{len(todo)} of {len(jobs)} jobs")
        write_lines("GITHUB_STEP_SUMMARY", [summary, ""])
        print(summary)
        for j in todo:
            print(f"{j}: {planned[j]['recipes']}")
        return 0

    if args.command == "unpack":
        unpack(args.zips, args.into)
        return 0

    jobs = json.loads(args.schedule.read_text())[f"{args.distro}/{args.platform}"]
    if args.command == "artifacts":
        names = [artifact(args.distro, args.platform, j) for j in ["cache", *(j for j in jobs if jobs[j]["recipes"])]]
        write_lines("GITHUB_ENV", [f"ARTIFACTS={' '.join(names)}"])
        print(f"{len(names)} artifacts")
        return 0

    job = jobs[args.job]
    needed = [j for j in job_needs(jobs, args.job) if jobs[j]["recipes"]]  # the ones that ran
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


if __name__ == "__main__":
    sys.exit(main())
