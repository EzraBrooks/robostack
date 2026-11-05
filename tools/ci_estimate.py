"""How much a pull-request build still has to do, and roughly how long it takes.

    python tools/ci_estimate.py --recipes distros/<d>/work/recipes --output-dir <dir> --platform <p>

A recipe makes one build per variant: per Python version for the packages with Python
content, when the distribution pins several (build_timings.variant_builds). Builds whose
package is in the restored cache are skipped by rattler-build; the rest are built, each
taking its recipe's measured time per build (shared/build_timings, see build_timings.py).
The seconds per package below are rough averages from the jazzy full rebuild in October
2026 (wall clock of the build step, including environment setup): building one package,
for platforms without measured times, and skipping one that is already in the cache.
"""

import argparse
import datetime
import os
import sys
from collections import Counter
from pathlib import Path

import build_timings
from ci_config import cache_dirs

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
    parser.add_argument("--output-dir", required=True, type=Path, help="rattler-build's output directory")
    parser.add_argument("--platform", required=True)
    args = parser.parse_args()

    recipes = {p.name for p in args.recipes.iterdir() if (p / "recipe.yaml").is_file()} if args.recipes.is_dir() else set()
    variants = build_timings.variant_builds(args.recipes, args.platform) if recipes else {}
    builds = {r: variants.get(r, 1) for r in recipes}
    cached = Counter(f.name.rsplit("-", 2)[0] for d in cache_dirs(args.output_dir, args.platform)
                     for f in d.glob("*.conda"))
    to_build = {r: n - min(n, cached[r]) for r, n in builds.items() if n > cached[r]}
    skipped = sum(min(n, cached[r]) for r, n in builds.items())
    build, skip = SECONDS.get(args.platform, max(SECONDS.values()))
    distro = args.recipes.resolve().parent.parent.name  # distros/<distro>/work/recipes
    measured = build_timings.timings(distro, args.platform)
    if measured:
        seconds = sum(measured["recipes"].get(r, measured["default"]) * n for r, n in to_build.items()) + skipped * skip
    else:
        seconds = sum(to_build.values()) * build + skipped * skip
    hours = seconds / 3600
    rounds = int(hours // JOB_LIMIT_HOURS) + 1
    done = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=seconds)

    several = sum(1 for n in to_build.values() if n > 1)
    packages = f" ({len(to_build)} recipes, {several} of them in several variants, e.g. one per Python version)" if several else ""
    text = (
        f"{sum(to_build.values())} packages to build{packages}, {skipped} restored from the cache: "
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
