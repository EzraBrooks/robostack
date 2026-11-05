"""Apply the PR-build cache controls from the distribution-owned ci.yaml.

testpr.yaml restores the build cache of the pull request and then runs this
script, so temporary rebuild controls live in ci.yaml instead of in the
(template-owned) workflow file:

    full_rebuild: true          # ignore the cache and rebuild everything
    evict_cache:                # drop these packages from the cache
      - rosidl_generator_py     # ROS name (dashes or underscores)
      - roboplan*               # or a glob

The controls apply once per configuration: a marker in the cache records them, so a
retry of the same rebuild keeps the packages the earlier attempt built (a full
rebuild that takes longer than one runner's time limit can finish across retries).
Changing full_rebuild or evict_cache applies them again.

Each evict_cache entry matches both package name prefixes (``ros2-`` and
``ros-<distro>-``, for builds made before ``package_name_mode: new``) and the plain
name (packages that vinca built under a conda-forge name).
"""

import argparse
import json
import shutil
import sys
from pathlib import Path

import yaml

MARKER = ".ci-controls"


def cache_dirs(output_dir: Path, platform: str) -> list[Path]:
    """The cached package directories in rattler-build's output directory: the
    platform's and noarch (its other contents, e.g. bld/ and src_cache/, are not
    cached)."""
    return [output_dir / platform, output_dir / "noarch"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output-dir", required=True, type=Path, help="rattler-build's output directory")
    parser.add_argument("--platform", required=True)
    parser.add_argument("--config", default="ci.yaml", type=Path)
    parser.add_argument("--vinca", default="vinca.yaml", type=Path)
    args = parser.parse_args()

    config = {}
    if args.config.exists():
        config = yaml.safe_load(args.config.read_text()) or {}
    distro = yaml.safe_load(args.vinca.read_text())["ros_distro"]
    for cache in cache_dirs(args.output_dir, args.platform):
        apply(cache, config, distro)
    return 0


def apply(cache: Path, config: dict, distro: str) -> None:
    controls = json.dumps({"full_rebuild": bool(config.get("full_rebuild", False)),
                           "evict_cache": [str(e) for e in config.get("evict_cache") or []]}, sort_keys=True)
    marker = cache / MARKER
    if not cache.is_dir():
        # nothing restored: this attempt starts clean, its packages count for the retries
        print(f"No cache directory {cache}; nothing to do.")
        cache.mkdir(parents=True)
        marker.write_text(controls)
        return

    if marker.is_file() and marker.read_text() == controls:
        print("ci.yaml: controls already applied to this cache (a retry); keeping its packages.")
        return

    if config.get("full_rebuild", False):
        print("ci.yaml: full_rebuild is set, ignoring the restored cache.")
        for child in cache.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
        marker.write_text(controls)
        return

    for entry in config.get("evict_cache") or []:
        name = str(entry).replace("_", "-")
        # A plain name only matches that package (name-version-build.conda);
        # a glob is used as given.
        suffix = name if "*" in name else f"{name}-[0-9]*"
        for prefix in ("ros2-", f"ros-{distro}-", ""):
            for path in sorted(cache.glob(prefix + suffix)):
                print(f"ci.yaml: evicting {path.name}")
                path.unlink()
    marker.write_text(controls)


if __name__ == "__main__":
    sys.exit(main())
