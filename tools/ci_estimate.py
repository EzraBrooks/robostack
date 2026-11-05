"""How much a pull-request build still has to do, and roughly how long it takes.

    python tools/ci_estimate.py --recipes distros/<d>/work/recipes --cache-dir <dir> --platform <p>

Recipes whose package is in the restored cache are skipped by rattler-build; the rest
are built, each taking its measured time (shared/build_timings, see build_timings.py).
The seconds per package below are rough averages from the jazzy full rebuild in October
2026 (wall clock of the build step, including environment setup): building one package,
for platforms without measured times, and skipping one that is already in the cache.
"""

import argparse
import datetime
import os
import sys
from pathlib import Path

import build_timings

SECONDS = {  # platform: (per built package, per skipped package)
    "linux-64": (19, 2),
    "linux-aarch64": (21, 3),
    "osx-arm64": (29, 3),
    "osx-64": (51, 9),
    "win-64": (69, 16),
}
JOB_LIMIT_HOURS = 6  # GitHub-hosted runners stop a job after 6 hours


def duration(seconds: float) -> str:
    """1 min, 45 min, 2 h 10 min."""
    minutes = max(1, round(seconds / 60))
    if minutes < 60:
        return f"{minutes} min"
    h, m = divmod(minutes, 60)
    return f"{h} h {m} min" if m else f"{h} h"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--recipes", required=True, type=Path)
    parser.add_argument("--cache-dir", required=True, type=Path, action="append",
                        help="A directory of cached packages (repeatable: the platform's and noarch)")
    parser.add_argument("--platform", required=True)
    args = parser.parse_args()

    recipes = {p.name for p in args.recipes.iterdir() if (p / "recipe.yaml").is_file()} if args.recipes.is_dir() else set()
    cached = {f.name.rsplit("-", 2)[0] for d in args.cache_dir for f in d.glob("*.conda")}
    to_build = sorted(recipes - cached)
    skipped = len(recipes & cached)
    build, skip = SECONDS.get(args.platform, max(SECONDS.values()))
    distro = args.recipes.resolve().parent.parent.name  # distros/<distro>/work/recipes
    measured = build_timings.timings(distro, args.platform)
    if measured:
        seconds = sum(measured["recipes"].get(r, measured["default"]) for r in to_build) + skipped * skip
    else:
        seconds = len(to_build) * build + skipped * skip
    hours = seconds / 3600
    rounds = int(hours // JOB_LIMIT_HOURS) + 1
    done = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=seconds)

    text = (
        f"{len(to_build)} packages to build, {skipped} restored from the cache: "
        f"roughly {duration(seconds)} (done around {done:%H:%M} UTC)"
    )
    if hours > JOB_LIMIT_HOURS:
        text += f" (more than the {JOB_LIMIT_HOURS} h job limit: about {rounds} runs, each continuing from the cache)"
    print(text)
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a") as fh:
            fh.write(f"**{os.environ.get('DISTRO', '')} {args.platform}**: {text}\n\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
