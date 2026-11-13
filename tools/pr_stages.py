"""The staged pull-request build: the build-branch stages (vinca-gha) as matrix jobs.

    python tools/pr_stages.py plan --distro <d> --platform <p> --cache-dir <dir>
    python tools/pr_stages.py workflow [--check]

GitHub can't add jobs to a running workflow, so .github/workflows/pr-stages.yaml has a
fixed number of stage slots (SLOTS), stage_0 ... stage_<SLOTS-1>, each after the one
before. `plan` (its plan job) lists each stage's jobs from `vinca-gha --json`, leaving
out the recipes whose every build is in the restored pull-request cache; a slot takes
its stage's jobs as its matrix and is skipped when there is no such stage. `workflow`
writes pr-stages.yaml (`rs check` checks that it is up to date).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_timings
import robostack as rs

SLOTS = 64  # lyrical's full linux-64 rebuild has 47 stages at --batch_size 25
MAX_NAME = 60  # job names are a stage's packages, which can be many
WORKFLOW = rs.ROOT / ".github" / "workflows" / "pr-stages.yaml"


def cached_recipes(recipes: Path, platform: str, cache_dir: Path) -> set[str]:
    """The recipes whose every build (one per variant) is in the cache (<subdir>/*.conda)."""
    names = {p.parent.name for p in recipes.glob("*/recipe.yaml")}
    variants = build_timings.variant_builds(recipes, platform) if names else {}
    cached = Counter(f.name.rsplit("-", 2)[0] for f in cache_dir.glob("*/*.conda"))
    return {r for r in names if cached[r] >= variants.get(r, 1)}


def fit(stages: list[list[dict]], slots: int = SLOTS) -> list[list[dict]]:
    """The stages as at most `slots` stages: the ones past the last slot become one job
    there (build-ci orders a job's recipes by their dependencies). Each job gets an id
    for its artifact, stage and job number."""
    if len(stages) > slots:
        rest = [job for stage in stages[slots - 1:] for job in stage]
        tail = {"name": f"{len(rest)} jobs of stages {slots - 1}+", "recipes": " ".join(j["recipes"] for j in rest)}
        stages = [*stages[:slots - 1], [tail]]
    fitted = []
    for k, stage in enumerate(stages):
        jobs = []
        for i, job in enumerate(stage):
            name = job["name"] if len(job["name"]) <= MAX_NAME else job["name"][:MAX_NAME - 1] + "…"
            jobs.append({"id": f"s{k}-{i}", "name": name, "recipes": job["recipes"]})
        fitted.append(jobs)
    return fitted


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
        out = w / "pr_stages.json"
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
    return fit(stages)


def workflow() -> str:
    """pr-stages.yaml: HEADER, the stage slots, then COLLECT."""
    slots = []
    for k in range(SLOTS):
        needs = "plan" if k == 0 else f"[plan, stage_{k - 1}]"
        slots.append(SLOT.replace("@K@", str(k)).replace("@NEEDS@", needs))
    every = ", ".join(["plan", *(f"stage_{k}" for k in range(SLOTS))])
    return HEADER + "".join(slots) + COLLECT.replace("@NEEDS@", every)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("plan", help="the stages of one distribution and platform, as GitHub outputs")
    p.add_argument("--distro", required=True)
    p.add_argument("--platform", required=True)
    p.add_argument("--cache-dir", required=True, type=Path)
    p.add_argument("--batch-size", type=int, default=25)
    w = sub.add_parser("workflow", help=f"write {WORKFLOW.relative_to(rs.ROOT)}")
    w.add_argument("--check", action="store_true", help="only check that it is up to date")
    args = parser.parse_args()

    if args.command == "workflow":
        text = workflow()
        if args.check:
            if not WORKFLOW.is_file() or WORKFLOW.read_text() != text:
                print(f"{WORKFLOW.relative_to(rs.ROOT)} is out of date (python tools/pr_stages.py workflow)")
                return 1
            return 0
        WORKFLOW.write_text(text)
        return 0

    stages = plan(args.distro, args.platform, args.cache_dir.resolve(), args.batch_size)
    # the slots past the last stage are skipped, but GitHub reads their matrix anyway
    padded = stages + [[{"id": "none", "name": "none", "recipes": ""}]] * (SLOTS - len(stages))
    lines = [f"count={len(stages)}", f"stages={json.dumps(padded, separators=(',', ':'))}"]
    if output := os.environ.get("GITHUB_OUTPUT"):
        with open(output, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a", encoding="utf-8") as fh:
            jobs = sum(map(len, stages))
            fh.write(f"**{args.distro} {args.platform}**: {jobs} build jobs in {len(stages)} stages\n\n")
    for k, stage in enumerate(stages):
        print(f"stage {k}: " + "; ".join(job["recipes"] for job in stage))
    return 0


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
# Generated by tools/pr_stages.py (python tools/pr_stages.py workflow); `pixi run rs check`
# checks that it is up to date.
#
# One distribution's pull-request build on one platform, in the build branches' stages:
# the plan job generates the recipes and lists each stage's jobs (vinca-gha --json),
# each stage_<k> slot builds its stage's jobs side by side (pr-stage-job.yaml), and
# collect keeps the packages as the pull request's build cache. Packages pass from
# stage to stage as artifacts (pkgs-<distro>-<platform>-*), which the later jobs read as
# a local channel: no secrets, so pull requests from forks build the same way.
name: PR stages

on:
  workflow_call:
    inputs:
      distro:
        required: true
        type: string
      platform:
        required: true
        type: string
      os:
        description: The platform's runner
        required: true
        type: string

env:
  PYTHONIOENCODING: utf-8
  DISTRO: ${{ inputs.distro }}
  PLATFORM: ${{ inputs.platform }}
  # while uploads go to a test channel, packages already there aren't built again
  ROBOSTACK_UPLOAD_CHANNEL: ${{ vars.ROBOSTACK_UPLOAD_CHANNEL }}
  # the pull request's packages, <subdir>/*.conda; restored and saved on Linux for every platform
  CACHE_DIR: pr-cache

jobs:
  plan:
    runs-on: ubuntu-latest
    outputs:
      count: ${{ steps.plan.outputs.count }}
      stages: ${{ steps.plan.outputs.stages }}
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
      - name: Upload the recipes
        uses: actions/upload-artifact@v6
        with:
          name: recipes-${{ inputs.distro }}-${{ inputs.platform }}
          path: distros/${{ inputs.distro }}/work/recipes
          if-no-files-found: ignore
          overwrite: true
          retention-days: 7
      - name: Restore build cache
        uses: actions/cache/restore@v5
        with:
          path: ${{ env.CACHE_DIR }}
          key: pr-stages-${{ inputs.distro }}-${{ inputs.platform }}-pr-${{ github.event.pull_request.number }}
          restore-keys: |
            pr-stages-${{ inputs.distro }}-${{ inputs.platform }}-pr-${{ github.event.pull_request.number }}-
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
          # the stage jobs' download pattern always matches this artifact
          echo "cache" > "$CACHE_DIR/manifest-cache.txt"
      - name: Upload the cached packages
        uses: actions/upload-artifact@v6
        with:
          name: pkgs-${{ inputs.distro }}-${{ inputs.platform }}-cache
          path: ${{ env.CACHE_DIR }}
          compression-level: 0  # .conda files are compressed
          overwrite: true
          retention-days: 3
      - name: Estimate the build time
        run: pixi run python tools/ci_estimate.py --recipes "distros/$DISTRO/work/recipes" --cache-dir "$CACHE_DIR/$PLATFORM" --platform "$PLATFORM"
      - name: Plan the stages
        id: plan
        run: pixi run python tools/pr_stages.py plan --distro "$DISTRO" --platform "$PLATFORM" --cache-dir "$CACHE_DIR"

"""

# a skipped slot doesn't skip the ones after it (!failure() instead of the default
# success()), but a failed one does: its packages are missing for the later stages
SLOT = """\
  stage_@K@:
    needs: @NEEDS@
    if: ${{ !cancelled() && !failure() && fromJSON(needs.plan.outputs.count) > @K@ }}
    strategy:
      fail-fast: false
      matrix:
        include: ${{ fromJSON(needs.plan.outputs.stages)[@K@] }}
    name: @K@ ${{ matrix.name }}
    uses: ./.github/workflows/pr-stage-job.yaml
    with:
      distro: ${{ inputs.distro }}
      platform: ${{ inputs.platform }}
      os: ${{ inputs.os }}
      recipes: ${{ matrix.recipes }}
      artifact: pkgs-${{ inputs.distro }}-${{ inputs.platform }}-${{ matrix.id }}

"""

COLLECT = """\
  # The packages of every stage as the pull request's build cache (a new push builds
  # only what changed), then the ABI check of what the pull request built.
  collect:
    needs: [@NEEDS@]
    if: ${{ always() && needs.plan.result == 'success' }}
    runs-on: ubuntu-latest
    steps:
""" + SETUP + """\
      - uses: actions/download-artifact@v7
        with:
          pattern: pkgs-${{ inputs.distro }}-${{ inputs.platform }}-*
          merge-multiple: true
          path: ${{ env.CACHE_DIR }}
      - name: See the packages
        run: |
          rm -f "$CACHE_DIR"/manifest-*.txt
          shopt -s nullglob
          packages=("$CACHE_DIR"/*/*.conda)
          echo "${#packages[@]} packages"
          for package in "${packages[@]}"; do basename "$package"; done
      - name: Save build cache
        id: save-cache
        uses: actions/cache/save@v5
        with:
          path: ${{ env.CACHE_DIR }}
          key: pr-stages-${{ inputs.distro }}-${{ inputs.platform }}-pr-${{ github.event.pull_request.number }}-${{ github.run_id }}-${{ github.run_attempt }}

      # Only the newest cache of a pull request/distribution/platform is restored; the ones
      # of earlier runs and attempts only push others out of the repository's 10 GB, so
      # full rebuilds lost their progress. Forks have a read-only token, so skip deletion.
      - name: Drop this pull request's superseded build caches
        if: ${{ steps.save-cache.outcome == 'success' && github.event.pull_request.number && github.event.pull_request.head.repo.full_name == github.repository }}
        continue-on-error: true
        env:
          GH_TOKEN: ${{ github.token }}
          PREFIX: pr-stages-${{ inputs.distro }}-${{ inputs.platform }}-pr-${{ github.event.pull_request.number }}-
          KEEP: pr-stages-${{ inputs.distro }}-${{ inputs.platform }}-pr-${{ github.event.pull_request.number }}-${{ github.run_id }}-${{ github.run_attempt }}
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
        if: inputs.platform == 'linux-64'
        continue-on-error: true
        run: |
          pixi run abi-check --distro "$DISTRO" --platform "$PLATFORM" --pr-builds "$CACHE_DIR/$PLATFORM" --summary "abi-check-$DISTRO.md"
          cat "abi-check-$DISTRO.md" >> "$GITHUB_STEP_SUMMARY"
      - name: Upload the ABI check
        if: ${{ steps.abi-check.outcome == 'success' }}
        uses: actions/upload-artifact@v6
        with:
          name: abi-check-${{ inputs.distro }}
          path: abi-check-${{ inputs.distro }}.md
          retention-days: 7

      # without recipes to build there is no recipes artifact, and nothing to stage
      - name: Prepare the work directory
        if: needs.plan.outputs.count != '0'
        run: pixi run rs "$DISTRO" prepare
      - if: needs.plan.outputs.count != '0'
        uses: actions/download-artifact@v7
        with:
          name: recipes-${{ inputs.distro }}-${{ inputs.platform }}
          path: distros/${{ inputs.distro }}/work/recipes
      - name: Generate the staged build workflows (catches post-merge problems)
        if: needs.plan.outputs.count != '0'
        run: |
          for attempt in 1 2 3; do
            if pixi run rs "$DISTRO" gha --platform "$PLATFORM" --batch-size 25; then break; fi
            if [[ "$attempt" == 3 ]]; then echo "Workflow generation failed after 3 attempts" >&2; exit 1; fi
            echo "Workflow generation attempt $attempt failed; retrying..." >&2
            sleep 15
          done
"""


if __name__ == "__main__":
    sys.exit(main())
