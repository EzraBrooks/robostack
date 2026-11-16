"""Re-rendering: the build workflows, generated from the recipes' dependency graph and
committed, as conda-forge's `conda smithy rerender` does.

    pixi run rs rerender [<distro>...] [--platform <p>...]   # generate the recipes, write the workflows
    python tools/rerender.py render --distro <d> --platform <p>   # from distros/<d>/work/recipes (CI)
    python tools/rerender.py check                             # `rs check`: the files match the templates

.github/workflows/build-<distro>-<platform>.yaml builds every recipe of the distribution
for the platform, one job per batch of recipes, each after the jobs that build its
recipes' requirements: a workflow of its own (GitHub doesn't start a run with every
distribution's jobs), for pull requests and releases (workflow_dispatch). Its plan
(staged-plan.yaml) checks that the pull request builds the distribution and platform,
lists the recipes missing from the channel and the pull request's cache, and only the
jobs with one of them run; build-status.yaml sums the workflows up in one commit status.
A change to the recipes that changes the batches needs a re-render: testpr.yaml
re-renders, and commits it to the pull request.

The batches are content-defined, so that a re-render changes only the jobs a change
touches: a recipe's level is the longest chain of requirements below it; each level's
recipes, sorted by name, start a new batch at a recipe whose name hashes to 0 modulo
TARGET (or after CAP recipes); a batch is named after its first recipe and lists only
the jobs it needs directly that no other of those needs (the rest it waits for anyway).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import robostack as rs

TARGET = 12  # a batch holds TARGET packages on average (each with its compatibility package)
CAP = 50  # recipes per batch at most
WORKFLOWS = rs.ROOT / ".github" / "workflows"
STATUS_WORKFLOW = WORKFLOWS / "build-status.yaml"
MAX_NAME = 60


def workflow_path(distro: str, platform: str) -> Path:
    return WORKFLOWS / f"build-{distro}-{platform}.yaml"


def requirements(distro: str, platform: str) -> dict[str, list[str]]:
    """Each recipe in distros/<d>/work/recipes, with the recipes there it requires
    (vinca-gha --json: host and run requirements)."""
    w = rs.work_dir(distro)
    if not any((w / "recipes").glob("*/recipe.yaml")):
        return {}
    out = w / "stages.json"
    if rs.run(["vinca-gha", "--platform", platform, "-d", "./recipes", "--json", out.name], w):
        raise SystemExit("vinca-gha failed")
    stages = json.loads(out.read_text())["stages"]
    return {r: deps for stage in stages for job in stage for r, deps in job["requires"].items()}


def job_id(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", name if re.match(r"[A-Za-z_]", name) else f"_{name}")


def hashed(name: str) -> int:
    return int.from_bytes(hashlib.sha256(name.encode()).digest()[:8], "big")


def batch(requires: dict[str, list[str]], distro: str) -> dict[str, dict]:
    """The recipes as jobs, {id: {"level", "recipes", "needs"}}. A compatibility package
    ros-<distro>-<name> goes with ros2-<name>, which is all it needs."""
    prefix = f"ros-{distro}-"
    leader = {r: f"ros2-{r[len(prefix):]}" if r.startswith(prefix) and f"ros2-{r[len(prefix):]}" in requires else r
              for r in requires}
    units: dict[str, list[str]] = {}
    for r in sorted(requires, key=lambda r: (r != leader[r], r)):
        units.setdefault(leader[r], []).append(r)
    below = {u: sorted({leader[q] for r in members for q in requires[r] if q in leader} - {u})
             for u, members in units.items()}
    level: dict[str, int] = {}
    for u in units:
        todo = [u]
        while todo:  # depth first, without recursion (long chains)
            v = todo[-1]
            if v in level:
                todo.pop()
                continue
            missing = [q for q in below[v] if q not in level]
            if missing:
                todo += missing
                continue
            level[v] = 1 + max((level[q] for q in below[v]), default=-1)
            todo.pop()

    batches: list[tuple[int, list[str]]] = []
    for k in sorted(set(level.values())):
        current: list[str] = []
        for u in sorted(u for u in units if level[u] == k):
            if current and (hashed(u) % TARGET == 0 or sum(len(units[v]) for v in current) + len(units[u]) > CAP):
                batches.append((k, current))
                current = []
            current.append(u)
        batches.append((k, current))

    job_of = {r: job_id(us[0]) for _, us in batches for u in us for r in units[u]}
    direct = {job_id(us[0]): {job_of[q] for u in us for r in units[u] for q in requires[r] if q in job_of}
              - {job_id(us[0])} for _, us in batches}
    below_jobs: dict[str, set[str]] = {}

    def ancestors(j: str) -> set[str]:
        if j not in below_jobs:
            below_jobs[j] = set().union(*({n} | ancestors(n) for n in direct[j])) if direct[j] else set()
        return below_jobs[j]

    return {
        job_id(us[0]): {
            "level": k,
            "recipes": [r for u in us for r in units[u]],
            "needs": sorted(n for n in direct[job_id(us[0])]
                            if not any(n in ancestors(m) for m in direct[job_id(us[0])] if m != n)),
        }
        for k, us in batches
    }


def short_name(level: int, recipes: list[str], distro: str) -> str:
    names = []
    for r in recipes:
        n = r.removeprefix("ros2-").removeprefix(f"ros-{distro}-")
        if n not in names:
            names.append(n)
    name = f"{level} {' '.join(names)}"
    return name if len(name) <= MAX_NAME else name[:MAX_NAME - 1] + "…"


def os_of(platform: str) -> str:
    return next(t["os"] for t in rs.BUILD_TARGETS if t["platform"] == platform)


HEADER = """\
# Generated by `pixi run rs rerender` (tools/rerender.py) from @DISTRO@'s @PLATFORM@ recipes;
# don't edit. Every recipe, one job per batch, after the jobs building its requirements;
# a job runs only when it has a recipe to build (staged-plan.yaml's todo). Pull requests
# keep the packages as their build cache, releases (workflow_dispatch) upload them.
name: build @DISTRO@ @PLATFORM@

on:
  pull_request:
  workflow_dispatch:
    inputs:
      upload:
        description: Upload the packages to the distribution's channels
        type: boolean
        default: true

# a new push cancels the pull request's run in progress; releases wait for each other
concurrency:
  group: ${{ github.workflow }}-${{ github.event.pull_request.number || github.ref }}
  cancel-in-progress: ${{ github.event_name == 'pull_request' }}

permissions:
  actions: write  # drop the pull request's superseded build caches
  contents: read
  id-token: write  # prefix.dev trusted publishing (releases)
  pull-requests: write  # the ABI check comment

env:
  PYTHONIOENCODING: utf-8
  DISTRO: @DISTRO@
  PLATFORM: @PLATFORM@
  # while uploads go to a test channel, packages already there aren't built again
  ROBOSTACK_UPLOAD_CHANNEL: ${{ vars.ROBOSTACK_UPLOAD_CHANNEL }}
  # rattler_build() in tools/robostack.py adds it as a channel (relative to the repository)
  ROBOSTACK_LOCAL_CHANNEL: pr-channel
  # where rattler-build writes the packages
  OUTPUT_DIR: @OUTPUT_DIR@

jobs:
  plan:
    uses: ./.github/workflows/staged-plan.yaml
    with:
      distro: @DISTRO@
      platform: @PLATFORM@
      upload: ${{ inputs.upload == true }}
"""

JOB = """\
  @ID@:
    name: @NAME@
    needs:
@NEEDS@    if: ${{ !cancelled() && needs.plan.result == 'success' && contains(fromJSON(needs.plan.outputs.todo), '@ID@') }}
    runs-on: @OS@
    timeout-minutes: 350
    steps:
      - uses: actions/checkout@v6
        with:
          persist-credentials: false
      - uses: ./.github/actions/stage-job
        with:
          job: @ID@
          upload: ${{ inputs.upload == true }}
          anaconda_token: ${{ inputs.upload == true && secrets.@SECRET@ || '' }}
          recipes: |
@RECIPES@
"""

COLLECT = """\
  collect:
    needs:
@NEEDS@    if: ${{ always() && inputs.upload != true && needs.plan.result == 'success' && needs.plan.outputs.todo != '[]' }}
    uses: ./.github/workflows/staged-collect.yaml
    with:
      distro: @DISTRO@
      platform: @PLATFORM@
"""


def lines(items: list[str], indent: int) -> str:
    return "".join(f"{' ' * indent}{item}\n" for item in items)


def render(distro: str, platform: str, jobs: dict[str, dict]) -> str:
    """The workflow, its jobs sorted by id: a re-render moves no job."""
    sub = {"@DISTRO@": distro, "@PLATFORM@": platform,
           # rattler-build writes to distros/<d>/work/output, or to CONDA_BLD_PATH (win-64)
           "@OUTPUT_DIR@": "D:/bld" if platform == "win-64" else f"distros/{distro}/work/output"}

    def fill(text: str, values: dict[str, str]) -> str:
        for key, value in {**sub, **values}.items():
            text = text.replace(key, value)
        return text

    out = [fill(HEADER, {})]
    for j in sorted(jobs):
        job = jobs[j]
        out.append(fill(JOB, {
            "@ID@": j, "@NAME@": json.dumps(short_name(job["level"], job["recipes"], distro), ensure_ascii=False),
            "@NEEDS@": lines([f"- {n}" for n in ["plan", *job["needs"]]], 6), "@OS@": os_of(platform),
            "@SECRET@": rs.anaconda_token_secret(distro), "@RECIPES@": lines(job["recipes"], 12).rstrip("\n"),
        }))
    needed = {n for job in jobs.values() for n in job["needs"]}
    out.append(fill(COLLECT, {"@NEEDS@": lines([f"- {n}" for n in ["plan", *sorted(set(jobs) - needed)]], 6)}))
    return "\n".join(out)


def parse(text: str) -> dict[str, dict]:
    """The jobs of a rendered workflow, as batch() returns them."""
    import yaml

    doc = yaml.safe_load(text)
    return {
        j: {"level": int(spec["name"].split()[0]), "recipes": spec["steps"][1]["with"]["recipes"].split(),
            "needs": [n for n in spec["needs"] if n != "plan"]}
        for j, spec in doc["jobs"].items() if j not in ("plan", "collect")
    }


STATUS = """\
# Generated by `pixi run rs rerender` (tools/rerender.py); don't edit. One commit status,
# "All builds", for branch protection: every build workflow (build-<distro>-<platform>.yaml)
# and testpr.yaml of a commit succeeded. Each is a workflow of its own (GitHub doesn't
# start a run with all their jobs), so this follows their runs (tools/build_status.py).
name: Build status

on:
  workflow_run:
    workflows:
@WORKFLOWS@    types: [requested, completed]

permissions:
  actions: read
  statuses: write

jobs:
  status:
    runs-on: ubuntu-latest
    concurrency:
      group: build-status-${{ github.event.workflow_run.head_sha }}
    steps:
      - uses: actions/checkout@v6
        with:
          persist-credentials: false
      - env:
          GH_TOKEN: ${{ github.token }}
          SHA: ${{ github.event.workflow_run.head_sha }}
          WORKFLOWS: '@JSON@'
        run: python3 tools/build_status.py --sha "$SHA" --workflows "$WORKFLOWS"
"""


def workflow_names() -> list[str]:
    """The workflows the status follows: testpr.yaml's and every build workflow's."""
    return ["Build", *(f"build {d} {t['platform']}" for d in rs.distros() for t in rs.BUILD_TARGETS)]


def render_status() -> str:
    names = workflow_names()
    return (STATUS.replace("@WORKFLOWS@", lines([f"- {n}" for n in names], 6))
            .replace("@JSON@", json.dumps(names)))


def render_from_recipes(distro: str, platform: str) -> Path:
    path = workflow_path(distro, platform)
    jobs = batch(requirements(distro, platform), distro)
    path.write_text(render(distro, platform, jobs))
    print(f"{path.relative_to(rs.ROOT)}: {len(jobs)} jobs")
    return path


def rerender(distro: str, platforms: list[str]) -> int:
    for platform in platforms:
        for attempt in range(3):
            if not rs.run(["pixi", "run", "rs", distro, "generate-recipes", "--all", "--platform", platform], rs.ROOT):
                break
            print(f"{distro} {platform}: recipe generation attempt {attempt + 1} failed")
        else:
            return 1
        render_from_recipes(distro, platform)
    return 0


def check() -> list[str]:
    """Problems: the status workflow or a rendered workflow doesn't match the templates."""
    problems = []
    if not STATUS_WORKFLOW.is_file() or STATUS_WORKFLOW.read_text() != render_status():
        problems.append(f"{STATUS_WORKFLOW.relative_to(rs.ROOT)} is out of date (pixi run rs rerender)")
    for d in rs.distros():
        for t in rs.BUILD_TARGETS:
            path = workflow_path(d, t["platform"])
            if not path.is_file():
                problems.append(f"{path.relative_to(rs.ROOT)} is missing (pixi run rs rerender {d})")
            elif render(d, t["platform"], parse(path.read_text())) != path.read_text():
                problems.append(f"{path.relative_to(rs.ROOT)} is out of date (pixi run rs rerender {d})")
    return problems


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["render"]:
        r = argparse.ArgumentParser(prog="rerender.py render", description="one workflow, from distros/<d>/work/recipes")
        r.add_argument("--distro", required=True)
        r.add_argument("--platform", required=True)
        ns = r.parse_args(argv[1:])
        render_from_recipes(ns.distro, ns.platform)
        return 0
    if argv[:1] == ["check"]:
        problems = check()
        print("\n".join(problems) or "up to date")
        return 1 if problems else 0
    parser = argparse.ArgumentParser(prog="rs rerender", description=__doc__.splitlines()[0])
    parser.add_argument("distros", nargs="*", help="default: all")
    parser.add_argument("--platform", action="append", dest="platforms", help="default: all")
    args = parser.parse_args(argv)
    if unknown := sorted(set(args.distros) - set(rs.distros())):
        parser.error(f"unknown distributions: {', '.join(unknown)}")
    STATUS_WORKFLOW.write_text(render_status())
    distros = args.distros or rs.distros()
    platforms = args.platforms or [t["platform"] for t in rs.BUILD_TARGETS]
    # distributions side by side (each generates into its own work directory)
    with ThreadPoolExecutor(len(distros)) as pool:
        return max(pool.map(lambda d: rerender(d, platforms), distros), default=0)


if __name__ == "__main__":
    sys.exit(main())
