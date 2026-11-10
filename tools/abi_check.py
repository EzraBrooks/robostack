"""Compare the ABI of two builds of RoboStack packages with libabigail's abidiff.

    python tools/abi_check.py --distro jazzy --latest-pairs 40
    python tools/abi_check.py --old a.conda --new b.conda
    python tools/abi_check.py --distro jazzy --pr-builds distros/jazzy/work/output/linux-64

--pr-builds compares every package built in a pull request with the newest build of
the same package on the distribution's channel, and lists the released packages that
depend on it: the ones to rebuild too when the verdict is soname or incompatible.

For every shared library (lib/*.so*) both builds contain, abidiff compares the
two. Without debug information (the published packages are stripped) only the
exported ELF symbols are compared: removed or changed functions and variables
show up, changed struct layouts don't.

Verdicts per package (the worst of its libraries):
- compatible: no ABI change;
- additions: only added symbols (dependents keep working);
- soname: the library's SONAME changed, but no symbol was removed or changed.
  Dependents are linked to the old SONAME, so they must be rebuilt (relinked),
  even though nothing about the API changed (e.g. MoveIt's versioned SONAMEs);
- incompatible: removed or changed symbols, or a removed library (dependents
  must be rebuilt);
- no libraries: nothing to compare (Python, messages without C++ libs, data).

Linux only: abidiff reads ELF. It is run with `pixi exec -s libabigail`; ELF
entries are read with pyelftools.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import rattler
from rattler.exceptions import FetchRepoDataError
from rattler.package_streaming import download_and_extract, download_to_path, extract

# abidiff exit status bits (libabigail's abidiff_status)
ABI_ERROR = 1
ABI_USAGE_ERROR = 2
ABI_CHANGE = 4
ABI_INCOMPATIBLE_CHANGE = 8
REPODATA_CACHE = Path(tempfile.gettempdir()) / "robostack-abi-check-repodata"


def read_index(path: Path) -> dict:
    """The repodata record of a package file (its info/index.json)."""
    return json.loads(asyncio.run(rattler.RepoDataRecord.from_package_archive(path)).to_json())


def channel_records(channel: str, platform: str) -> list[dict]:
    """The repodata records of the channel for the platform (none when it has no
    packages for it). The abi-check task runs without vinca, so not vinca's
    get_channel_records."""
    try:
        [repodata] = asyncio.run(rattler.fetch_repo_data(
            channels=[rattler.Channel(channel)], platforms=[rattler.Subdir(platform)],
            cache_path=REPODATA_CACHE, callback=None,
        ))
    except FetchRepoDataError as error:
        # network failures raise the same type
        if not str(error).startswith("repodata not found"):
            raise
        return []
    return [json.loads(record.to_json()) for record in repodata.load_all_records()]


def libraries(prefix: Path) -> dict[str, Path]:
    """Shared libraries under lib/, keyed by their name without version suffixes."""
    libs = {}
    for f in (prefix / "lib").glob("**/*.so*") if (prefix / "lib").is_dir() else []:
        if f.is_file() and not f.is_symlink():
            libs[f.name.split(".so")[0] + ".so"] = f
    return libs


def abidiff(old: Path, new: Path) -> tuple[int, str]:
    cmd = ["pixi", "exec", "-s", "libabigail", "abidiff", "--no-show-locs", "--no-unreferenced-symbols",
           str(old), str(new)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


RANK = ["compatible", "additions", "soname", "error", "incompatible"]
_SUMMARY_RE = re.compile(r"(Functions|Variables) changes summary: (\d+) Removed, (\d+) Changed")


def classify(rc: int, report: str) -> str:
    """Verdict for one library from abidiff's exit status and report."""
    if rc & (ABI_ERROR | ABI_USAGE_ERROR):
        return "error"
    if not rc & (ABI_CHANGE | ABI_INCOMPATIBLE_CHANGE):
        return "compatible"
    removed_or_changed = any(int(m.group(2)) or int(m.group(3)) for m in _SUMMARY_RE.finditer(report))
    if rc & ABI_INCOMPATIBLE_CHANGE and not removed_or_changed and "SONAME changed" in report:
        return "soname"
    if rc & ABI_INCOMPATIBLE_CHANGE:
        return "incompatible"
    return "additions"


def compare(old_pkg: Path, new_pkg: Path, work: Path) -> dict:
    a, b = work / "old", work / "new"
    shutil.rmtree(work, ignore_errors=True)
    extract(old_pkg, a)
    extract(new_pkg, b)
    old_libs, new_libs = libraries(a), libraries(b)
    result = {"libraries": {}, "removed_libraries": sorted(set(old_libs) - set(new_libs)),
              "headers": header_changes(a, b)}
    # the SONAMEs dependents link to, of the libraries that break them
    result["broken_sonames"] = [soname(old_libs[name]) for name in result["removed_libraries"]]
    if not old_libs and not new_libs:
        result["verdict"] = "no libraries"
        return result
    verdict = "incompatible" if result["removed_libraries"] else "compatible"
    for name in sorted(set(old_libs) & set(new_libs)):
        rc, text = abidiff(old_libs[name], new_libs[name])
        lib_verdict = classify(rc, text)
        result["libraries"][name] = {"verdict": lib_verdict, "report": "\n".join(text.splitlines()[:40])}
        if lib_verdict in ("soname", "incompatible"):
            result["broken_sonames"].append(soname(old_libs[name]))
        if RANK.index(lib_verdict) > RANK.index(verdict):
            verdict = lib_verdict
    result["verdict"] = verdict
    return result


def _dynamic(path: Path, tag: str) -> list[str]:
    """DT_SONAME / DT_NEEDED entries of an ELF file ([] when it has none)."""
    from elftools.common.exceptions import ELFError
    from elftools.elf.dynamic import DynamicSection
    from elftools.elf.elffile import ELFFile

    try:
        with open(path, "rb") as fh:
            elf = ELFFile(fh)
            return [
                getattr(t, "soname" if tag == "DT_SONAME" else "needed")
                for section in elf.iter_sections() if isinstance(section, DynamicSection)
                for t in section.iter_tags() if t.entry.d_tag == tag
            ]
    except (ELFError, OSError):
        return []


def soname(path: Path) -> str:
    return next(iter(_dynamic(path, "DT_SONAME")), path.name)


def needed(prefix: Path) -> set[str]:
    """The libraries the ELF files under prefix link to (DT_NEEDED)."""
    found = set()
    for f in prefix.rglob("*"):
        if f.is_file() and not f.is_symlink():
            with open(f, "rb") as fh:
                if fh.read(4) != b"\x7fELF":
                    continue
            found.update(_dynamic(f, "DT_NEEDED"))
    return found


_COMMENTS = re.compile(r"//[^\n]*|/\*.*?\*/", re.S)


def header_changes(old: Path, new: Path) -> list[str] | None:
    """Installed headers (include/) added, removed or changed beyond comments and
    whitespace (None when neither build has headers). Unchanged headers rule out
    layout and inline changes, which the symbol comparison can't see."""
    def headers(prefix):
        root = prefix / "include"
        out = {}
        for f in root.rglob("*") if root.is_dir() else []:
            if f.is_file():
                text = _COMMENTS.sub("", f.read_text(errors="replace"))
                out[str(f.relative_to(root))] = " ".join(text.split())
        return out

    a, b = headers(old), headers(new)
    if not a and not b:
        return None
    return sorted(
        [f"+{h}" for h in set(b) - set(a)] + [f"-{h}" for h in set(a) - set(b)]
        + [h for h in set(a) & set(b) if a[h] != b[h]],
        key=lambda h: h.lstrip("+-"),
    )


def _is_compatibility_package(rec: dict) -> bool:
    """vinca's ros-<distro>-<pkg> packages (package_name_mode: both) are empty and only
    depend on ros2-<pkg> of the same version (and, since RoboStack/vinca#171, on the
    distribution's mutex)."""
    deps = rec.get("depends", [])
    return (bool(deps) and deps[0].startswith("ros2-") and "==" in deps[0]
            and all(d.startswith("ros2-distro-mutex") for d in deps[1:]))


def short_name(name: str, distro: str) -> str | None:
    """The ROS package of a conda name: ros-<distro>-<pkg> and ros2-<pkg> are the same."""
    for prefix in (f"ros-{distro}-", "ros2-"):
        if name.startswith(prefix):
            return name[len(prefix):]
    return None


def _belongs_to(rec: dict, distro: str) -> bool:
    """A record of this distribution: ros-<distro>-* names, or ros2-* builds pinned to
    its mutex (a channel such as the test channel can hold several distributions)."""
    if rec["name"].startswith(f"ros-{distro}-"):
        return True
    return any(dep.startswith("ros2-distro-mutex") and f" {distro}_" in dep for dep in rec.get("depends", []))


def released(channels: str | list[str], platform: str, distro: str) -> dict[str, dict[str, dict]]:
    """The ROS packages of the distribution on the channels: package -> version -> newest
    real build across them (with its "file" and "channel"); vinca's empty compatibility
    packages are left out."""
    by_name: dict[str, dict[str, dict]] = {}
    for channel in [channels] if isinstance(channels, str) else channels:
        for rec in channel_records(channel, platform):
            short = short_name(rec["name"], distro)
            if short is None or _is_compatibility_package(rec) or not _belongs_to(rec, distro):
                continue
            versions = by_name.setdefault(short, {})
            current = versions.get(rec["version"])
            if current is None or (rec.get("build_number", 0), rec.get("timestamp", 0)) > (
                current.get("build_number", 0), current.get("timestamp", 0)
            ):
                versions[rec["version"]] = {**rec, "file": rec["fn"], "channel": channel}
    return by_name


def newest(versions: dict[str, dict]) -> dict:
    return versions[max(versions, key=rattler.Version)]


def latest_pairs(channel: str, platform: str, count: int, distro: str) -> list[tuple[str, dict, dict]]:
    """Packages whose two newest versions on the channel differ, newest real build of
    each; ros-<distro>-<pkg> and ros2-<pkg> count as the same package."""
    pairs = []
    for short, versions in sorted(released(channel, platform, distro).items()):
        if len(versions) < 2:
            continue
        ordered = sorted(versions, key=rattler.Version)
        pairs.append((short, versions[ordered[-2]], versions[ordered[-1]]))
    # most recently uploaded new versions first
    pairs.sort(key=lambda p: p[2].get("timestamp", 0), reverse=True)
    return pairs[:count]


def dependents(by_name: dict[str, dict[str, dict]], package: str, distro: str) -> list[str]:
    """Released packages (newest version of each) that depend on the package directly."""
    found = []
    for short, versions in by_name.items():
        if short == package:
            continue
        if any(short_name(dep.split()[0], distro) == package for dep in newest(versions).get("depends", [])):
            found.append(short)
    return sorted(found)


RUNTIMES = {"libgcc", "libgcc-ng", "libstdcxx", "libstdcxx-ng", "libcxx", "vc", "vc14_runtime", "vcomp14", "ucrt"}


def pin_changes(old: dict, new: dict, distro: str) -> list[str]:
    """Non-ROS dependencies that differ between the two builds (changed pins, new and
    dropped dependencies), e.g. a changed libboost: then the ABI comparison mixes the
    package's change with the pin's."""
    def pins(rec):
        out = {}
        for dep in rec.get("depends", []):
            name, *spec = dep.split(" ", 1)
            # the distro mutex counts as ROS; compiler runtimes and virtual packages are noise
            if short_name(name, distro) is None and not name.startswith("__") and name not in RUNTIMES:
                out[name] = spec[0] if spec else ""
        return out

    a, b = pins(old), pins(new)

    def show(spec):
        return spec.replace(".0a0", "") or "any"

    def names(found):
        # a -devel package travels with its library: list only the library
        return [n for n in sorted(found) if not (n.endswith("-devel") and n[:-len("-devel")] in found)]

    changed = names({n for n in set(a) & set(b) if a[n] != b[n]})
    added = names(set(b) - set(a))
    dropped = names(set(a) - set(b))
    out = []
    if changed:
        out.append("pins changed: " + ", ".join(f"{n} {show(a[n])} → {show(b[n])}" for n in changed))
    if added:
        out.append("new dependencies: " + ", ".join(f"{n} {show(b[n])}" for n in added))
    if dropped:
        out.append("dropped dependencies: " + ", ".join(dropped))
    return out


def channel_for(distro: str) -> list[str]:
    """Where the distribution's builds are published: the channel uploads go to while
    ROBOSTACK_UPLOAD_CHANNEL is set (e.g. a test channel), then the release channel
    (repo.prefix.dev serves repodata directly)."""
    sys.path.insert(0, str(Path(__file__).parent))
    import robostack as rs

    upload = os.environ.get("ROBOSTACK_UPLOAD_CHANNEL", "").strip()
    extra = [f"https://repo.prefix.dev/{upload}"] if upload and upload != "none" else []
    return extra + [rs.repodata_url(distro)]


def download(rec: dict, dest: Path) -> Path:
    target = dest / rec["file"]
    if not target.exists():
        asyncio.run(download_to_path(rattler.Client(), rec["url"], target))
    return target


# Above this many dependents, list them all instead of downloading each to check it.
MAX_DEPENDENTS_TO_INSPECT = 40


def linking_dependents(by_name, candidates, sonames, tmp: Path) -> dict:
    """Which of the dependents link to one of the SONAMEs (and so need a rebuild);
    metapackages, Python packages and configurations don't."""
    if len(candidates) > MAX_DEPENDENTS_TO_INSPECT:
        return {"rebuild": candidates, "checked": False}
    rebuild = []
    for name in candidates:
        target = tmp / "dependent"
        shutil.rmtree(target, ignore_errors=True)
        asyncio.run(download_and_extract(rattler.Client(), newest(by_name[name])["url"], target))
        if needed(target) & set(sonames):
            rebuild.append(name)
    return {"rebuild": rebuild, "checked": True, "candidates": len(candidates)}


def next_build_number(by_name: dict[str, dict[str, dict]], distro: str) -> int:
    """The build number a rebuild gets, as the pinning bot gives it: above the
    distribution's build_number and every published build of its packages."""
    sys.path.insert(0, str(Path(__file__).parent))
    import robostack as rs

    vinca = rs.DISTROS / distro / "vinca.yaml"
    m = re.search(r"(?m)^build_number:\s*(\d+)", vinca.read_text()) if vinca.is_file() else None
    published = [rec.get("build_number", 0) for versions in by_name.values() for rec in versions.values()]
    return max([int(m.group(1)) if m else 0, *published]) + 1


def check_pr_builds(pr_dir: Path, channel: str, platform: str, distro: str, tmp: Path) -> tuple[dict, int]:
    """Compare every package in pr_dir with the newest released build of it; also the
    build number rebuilds of dependents get."""
    by_name = released(channel, platform, distro)
    downloads = tmp / "downloads"
    downloads.mkdir(exist_ok=True)
    results = {}
    built = set()  # everything the pull request rebuilt: those dependents are taken care of
    for pkg in sorted([*pr_dir.glob("*.conda"), *pr_dir.glob("*.tar.bz2")]):
        index = read_index(pkg)
        short = short_name(index["name"], distro)
        # check_patches_clean_apply.py's patch-check packages can share the output directory
        if short is None or _is_compatibility_package(index) or "-check-patches-" in short:
            continue
        built.add(short)
        if short not in by_name:
            results[short] = {"verdict": "new package", "new": index["version"], "libraries": {}, "removed_libraries": []}
            continue
        old = newest(by_name[short])
        if old["file"] == pkg.name:
            continue
        print(f"{short}: {old['version']} (build {old.get('build_number', 0)}) -> {index['version']} (PR)", flush=True)
        old_pkg = download(old, downloads)
        res = compare(old_pkg, pkg, tmp / "work")
        old_pkg.unlink(missing_ok=True)
        res.update(old=f"{old['version']} (build {old.get('build_number', 0)})", new=index["version"],
                   pins=pin_changes(old, index, distro))
        if res["broken_sonames"]:
            res["dependents"] = linking_dependents(
                by_name, dependents(by_name, short, distro), res["broken_sonames"], tmp
            )
        results[short] = res
        print(f"  -> {res['verdict']}", flush=True)
    drop_rebuilt(results, built)
    return results, next_build_number(by_name, distro)


def drop_rebuilt(results: dict, built: set[str]) -> None:
    """Dependents the pull request rebuilt anyway don't need a build number bump."""
    for res in results.values():
        deps = res.get("dependents")
        if deps:
            deps["rebuilt"] = sorted(d for d in deps["rebuild"] if d in built)
            deps["rebuild"] = [d for d in deps["rebuild"] if d not in built]


VERDICT_NOTE = {
    "compatible": "no ABI change",
    "additions": "only added symbols",
    "soname": "SONAME changed: rebuild the dependents",
    "incompatible": "removed or changed symbols: rebuild the dependents",
    "error": "abidiff failed",
    "no libraries": "nothing to compare",
    "new package": "not released yet",
}


def _row(name: str, res: dict) -> str:
    details = []
    changed = [f"`{lib}` ({info['verdict']})" for lib, info in res["libraries"].items() if info["verdict"] != "compatible"]
    if changed:
        details.append("libraries: " + ", ".join(changed))
    if res["removed_libraries"]:
        details.append("removed: " + ", ".join(f"`{lib}`" for lib in res["removed_libraries"]))
    if res.get("dependents") is not None:
        deps = res["dependents"]
        listed = ", ".join(deps["rebuild"]) if deps["rebuild"] else "none"
        if deps["checked"]:
            details.append(f"**rebuild {len(deps['rebuild'])} dependents** (of {deps['candidates']} released "
                           f"dependents, these link to it): {listed}")
        else:
            details.append(f"**{len(deps['rebuild'])} released dependents** (too many to check which link to it; "
                           f"rebuild those with compiled code): {listed}")
        if deps.get("rebuilt"):
            details.append(f"{len(deps['rebuilt'])} more already rebuilt in this pull request: {', '.join(deps['rebuilt'])}")
    headers = res.get("headers")
    if headers:
        shown = ", ".join(f"`{h}`" for h in headers[:8]) + (f" and {len(headers) - 8} more" if len(headers) > 8 else "")
        details.append(f"{len(headers)} headers changed (check for layout/inline changes): {shown}")
    elif headers is not None:
        details.append("headers unchanged")
    details.extend(res.get("pins") or [])
    verdict = f"**{res['verdict']}**: {VERDICT_NOTE.get(res['verdict'], '')}"
    return f"| {name} | {res.get('old', '')} → {res.get('new', '')} | {verdict} | {'<br>'.join(details)} |"


def _needs_attention(res: dict) -> bool:
    """Breaks dependents, failed, or changed headers of a package with libraries."""
    return res["verdict"] in ("soname", "incompatible", "error") or (
        res["verdict"] in ("compatible", "additions") and bool(res.get("headers"))
    )


TABLE_HEADER = ["| package | released → PR | verdict | details |", "|---|---|---|---|"]


def rebuild_snippet(results: dict, distro: str, build_number: int | None) -> list[str]:
    """pkg_additional_info.yaml entries for the dependents to rebuild in the same PR."""
    names = sorted({dep.replace("-", "_") for res in results.values()
                    for dep in (res.get("dependents") or {}).get("rebuild", [])})
    if not names or build_number is None:
        return []
    unchecked = any(not (res.get("dependents") or {}).get("checked", True) for res in results.values())
    return [
        f"**To rebuild the dependents in this pull request**, add these to `distros/{distro}/pkg_additional_info.yaml` "
        "(for a package that already has an entry, set its `build_number`):"
        + (" some lists couldn't be checked for linking, drop the packages without compiled code." if unchecked else ""),
        "",
        "```yaml",
        *[line for name in names for line in (f"{name}:", f"  build_number: {build_number}")],
        "```",
        "",
    ]


def pr_summary(results: dict, distro: str, platform: str, channel: str, build_number: int | None = None) -> list[str]:
    """Markdown for the pull-request comment: one collapsed section per distribution,
    with the counts in its summary line, open only when dependents need a rebuild.
    Inside, what needs attention as a table, the rest collapsed again. Packages without
    a release are left out; a distribution with nothing to compare gets no section."""
    def worst_first(item):
        verdict = item[1]["verdict"]
        return (-RANK.index(verdict) if verdict in RANK else 1, item[0])

    ordered = sorted(results.items(), key=worst_first)
    attention = [(n, r) for n, r in ordered if r["verdict"] != "new package" and _needs_attention(r)]
    rest = [(n, r) for n, r in ordered if r["verdict"] != "new package" and not _needs_attention(r)]

    if not attention and not rest:
        return []
    breaking = [n for n, r in attention if r["verdict"] in ("soname", "incompatible", "error")]
    counts = [f"**{len(breaking)} need their dependents rebuilt**" if breaking else "",
              f"{len(attention) - len(breaking)} to review (changed headers)" if len(attention) > len(breaking) else "",
              f"{len(rest)} can be bumped on their own" if rest else ""]
    # always collapsed, the summary line shows the counts (breaking ones in bold)
    lines = [f"<details><summary><b>{distro} {platform}</b>: "
             f"{', '.join(c for c in counts if c)}</summary>", ""]
    if attention:
        lines += TABLE_HEADER + [_row(n, r) for n, r in attention] + [""]
        lines += rebuild_snippet(dict(attention), distro, build_number)
    if rest:
        verdicts: dict[str, int] = {}
        for _, res in rest:
            verdicts[res["verdict"]] = verdicts.get(res["verdict"], 0) + 1
        summary = ", ".join(f"{v}: {c}" for v, c in sorted(verdicts.items()))
        lines += [f"<details><summary>{len(rest)} packages can be bumped on their own ({summary})</summary>", "",
                  *TABLE_HEADER, *[_row(n, r) for n, r in rest], "", "</details>", ""]
    where = " and ".join(f"{c}/{platform}" for c in ([channel] if isinstance(channel, str) else channel))
    return lines + [f"Compared with the newest builds on {where}.", "", "</details>", ""]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--channel", help="channel URL (default: the distribution's release channel)")
    parser.add_argument("--platform", default="linux-64")
    parser.add_argument("--distro", default="jazzy", help="for the ros-<distro>-* package names")
    parser.add_argument("--latest-pairs", type=int, default=30)
    parser.add_argument("--old", type=Path)
    parser.add_argument("--new", type=Path)
    parser.add_argument("--pr-builds", type=Path, help="directory with the packages a pull request built")
    parser.add_argument("--json", type=Path, help="write the results here")
    parser.add_argument("--summary", type=Path, help="append a Markdown summary here")
    args = parser.parse_args()
    if sys.platform != "linux":
        raise SystemExit("abidiff reads ELF: run this on Linux")
    channel = [args.channel] if args.channel else channel_for(args.distro)

    results = {}
    next_build = None
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        if args.pr_builds:
            if args.pr_builds.is_dir():
                results, next_build = check_pr_builds(args.pr_builds, channel, args.platform, args.distro, tmp)
        elif args.old and args.new:
            results[args.new.name] = compare(args.old, args.new, tmp / "work")
        else:
            downloads = tmp / "downloads"
            downloads.mkdir()
            for name, old, new in latest_pairs(channel, args.platform, args.latest_pairs, args.distro):
                print(f"{name}: {old['version']} -> {new['version']}", flush=True)
                old_pkg = download(old, downloads)
                new_pkg = download(new, downloads)
                res = compare(old_pkg, new_pkg, tmp / "work")
                res.update(old=old["version"], new=new["version"])
                results[name] = res
                print(f"  -> {res['verdict']}", flush=True)
                old_pkg.unlink(missing_ok=True)
                new_pkg.unlink(missing_ok=True)

    counts: dict[str, int] = {}
    for res in results.values():
        counts[res["verdict"]] = counts.get(res["verdict"], 0) + 1
    print("\nSummary: " + ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())))
    if args.json:
        args.json.write_text(json.dumps(results, indent=2))
    if args.summary:
        if args.pr_builds:
            lines = pr_summary(results, args.distro, args.platform, channel, next_build)
        else:
            lines = ["### ABI check", "", "| package | old → new | verdict | libraries |", "|---|---|---|---|"]
            for name, res in results.items():
                libs = ", ".join(f"{lib} ({info['verdict']})" for lib, info in res["libraries"].items())
                if res["removed_libraries"]:
                    libs += " removed: " + ", ".join(res["removed_libraries"])
                lines.append(f"| {name} | {res.get('old', '')} → {res.get('new', '')} | {res['verdict']} | {libs} |")
            lines += ["", "Summary: " + ", ".join(f"{k}: {v}" for k, v in sorted(counts.items()))]
        with open(args.summary, "a") as fh:
            fh.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
