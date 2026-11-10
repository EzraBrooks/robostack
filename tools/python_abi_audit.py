"""Check vinca's Python classification (conditional_python_dependency) against the
published packages.

    pixi run rs lyrical python-audit                 # linux-64, info/paths.json only
    pixi run rs lyrical python-audit --elf           # also scan ELF files for libpython
    pixi run rs lyrical python-audit --platform osx-arm64 --json audit.json

A generated recipe (work/recipes) either has python in host, and is built for every
Python of the distribution's pinning, or not, and is built once. This compares that
with what the newest published build of each ros2-* package actually contains:

- native: CPython extensions (*.cpython-*.so, *.pyd), rosidl_generator_py libraries,
  or (--elf) ELF files that link libpython;
- pure: only Python modules (site-packages);
- none: no Python content.

A recipe built once whose published package has Python content is a miss: that
content would only work with the Python it happened to be built with. Misses make
the exit status 1; fix them with `add_host: ["python"]` in patch/dependencies.yaml.
Recipes built per Python without Python content in the package are only reported
(an unneeded rebuild per Python, not a broken package).

Run it before the switch to per-Python builds, while the channel still has the
packages that were built with python in host (their content is complete).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

import rattler
import yaml
from rattler.package_streaming import PackageArchive
from vinca.utils import get_channel_records

SITE_PACKAGES = re.compile(r"(^|/)(lib/python[0-9.]*/site-packages|Lib/site-packages)/")
EXTENSION = re.compile(r"\.cpython-[^/]*\.(so|dylib)$|\.pyd$")
CONCURRENCY = 16


async def inspect(client: rattler.Client, url: str, elf: bool) -> dict:
    """Classify by info/paths.json (read with range requests); with *elf*, the ELF
    files of a package without Python files are searched for libpython (embedding)."""
    for attempt in range(3):
        try:
            archive = await PackageArchive.from_url(client, url)
            paths = [entry.relative_path.as_posix() for entry in (await archive.paths_json()).paths]
            result = classify(paths, [])
            if elf and result["content"] == "none":
                linked = [entry.name async for entry in archive.stream("pkg")
                          if entry.is_file and (data := await entry.read())[:4] == b"\x7fELF"
                          and b"libpython3." in data]
                result = classify(paths, linked)
            return result
        except Exception as err:  # noqa: BLE001 - network or archive trouble: retried, then reported
            error = err
    return {"content": "error", "error": str(error)}


async def inspect_all(published: dict[str, dict], elf: bool) -> dict[str, dict]:
    client = rattler.Client()
    limit = asyncio.Semaphore(CONCURRENCY)

    async def one(name: str, record: dict) -> tuple[str, dict]:
        async with limit:
            return name, {"file": record["fn"], **await inspect(client, record["url"], elf)}

    return dict(await asyncio.gather(*(one(name, record) for name, record in published.items())))


def classify(paths: list[str], linked: list[str]) -> dict:
    site = [p for p in paths if SITE_PACKAGES.search(p)]
    native = [p for p in paths if EXTENSION.search(p)]
    native += [p for p in paths if "rosidl_generator_py" in p and re.search(r"\.(so|dylib|dll)$", p)]
    native += linked
    content = "native" if native else "pure" if site else "none"
    return {"content": content, "examples": (native or site)[:3]}


def flatten(requirements) -> list[str]:
    out = []
    for r in requirements or []:
        if isinstance(r, dict):
            out += flatten(r.get("then")) + flatten(r.get("else"))
        else:
            out.append(str(r))
    return out


def recipes_needing_python(recipes: Path) -> dict[str, bool]:
    result = {}
    for path in sorted(recipes.glob("ros2-*/recipe.yaml")):
        recipe = yaml.safe_load(path.read_text())
        host = flatten((recipe.get("requirements") or {}).get("host"))
        result[recipe["package"]["name"]] = "python" in host
    return result


def newest_builds(records: list[dict]) -> dict[str, dict]:
    newest: dict[str, tuple] = {}
    for rec in records:
        key = (rec["build_number"], rec.get("timestamp", 0))
        if rec["name"].startswith("ros2-") and key > newest.get(rec["name"], ((-1, 0), None))[0]:
            newest[rec["name"]] = (key, rec)
    return {name: rec for name, (_, rec) in newest.items()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--channel", required=True, help="channel URL, e.g. https://prefix.dev/robostack-lyrical")
    parser.add_argument("--platform", default="linux-64")
    parser.add_argument("--recipes", default="recipes", type=Path, help="generated recipes (default: ./recipes)")
    parser.add_argument("--elf", action="store_true",
                        help="download the packages without Python files and look for libpython in their ELF files")
    parser.add_argument("--json", type=Path, help="write the per-package results here")
    args = parser.parse_args()

    needs = recipes_needing_python(args.recipes)
    if not needs:
        print(f"No ros2-* recipes in {args.recipes}: run generate-recipes first", file=sys.stderr)
        return 1
    published = newest_builds(get_channel_records(args.channel, args.platform))
    results = asyncio.run(inspect_all({n: published[n] for n in needs if n in published}, args.elf))

    misses = sorted(n for n, r in results.items() if not needs[n] and r["content"] in ("native", "pure"))
    unneeded = sorted(n for n, r in results.items() if needs[n] and r["content"] == "none")
    errors = sorted(n for n, r in results.items() if r["content"] == "error")
    counts: dict[tuple[bool, str], int] = {}
    for n, r in results.items():
        counts[(needs[n], r["content"])] = counts.get((needs[n], r["content"]), 0) + 1

    print(f"{len(needs)} recipes, {len(results)} with a published {args.platform} package, "
          f"{len(needs) - len(results)} unpublished")
    for (per_python, content), count in sorted(counts.items()):
        print(f"  {'per Python' if per_python else 'built once':10}  package content {content:7}  {count}")
    for name in misses:
        print(f"MISS {name}: built once, but ships {results[name]['content']} Python: {results[name]['examples']}")
    if unneeded:
        print(f"{len(unneeded)} recipes are built per Python without Python content: {', '.join(unneeded)}")
    for name in errors:
        print(f"ERROR {name}: {results[name]['error']}", file=sys.stderr)
    if args.json:
        args.json.write_text(json.dumps({n: {"per_python": needs[n], **r} for n, r in results.items()},
                                        indent=1, sort_keys=True) + "\n")
    return 1 if misses or errors else 0


if __name__ == "__main__":
    sys.exit(main())
