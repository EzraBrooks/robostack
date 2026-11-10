"""Maintenance commands (used by robostack-bot, .github/workflows/bot.yaml).

    pixi run rs check                                 # sanity checks of the whole repository
    pixi run rs <distro> update-snapshot              # snapshot the latest rosdistro sync, summarise bumps
    pixi run rs rosdistro-syncs                       # bot jobs for distributions with a new sync
    pixi run rs <distro> find-stale                   # published packages built against outdated pins
    pixi run rs <distro> update-pinning               # latest conda-forge pinning + rebuild plan
    pixi run rs new-distro NAME --from DISTRO         # add distros/NAME, seeded from DISTRO
    pixi run rs add-package PKG... [DISTRO...] [--preview]   # select packages for building
    pixi run rs rebuild PKG... [DISTRO...] [--with-dependents]  # per-package build-number bumps
    pixi run rs rebuild-dependents PR                 # commit the ABI check's suggested rebuilds to the PR
    pixi run rs update-vinca [REF]                    # move the vinca pin to the newest commit of its branch
    pixi run rs update-pixi                           # `pixi upgrade` the tooling's dependencies
    pixi run rs <distro> dependency-report            # dependency check, kept in a dependency-report issue
    pixi run rs parse-command --body TEXT --association ROLE   # for @robostack-bot comments

Each command prints a markdown summary; `--summary FILE` also writes it to FILE and,
in GitHub Actions, sets the outputs title / changed / ok.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import yaml

import robostack as rs

BOT_COMMANDS = {
    "add-package": "add ROS packages to the package selection (opens a PR; anyone can ask)",
    "update-rosdistro-snapshot": "refresh the distribution's rosdistro_snapshot.yaml (opens a PR)",
    "find-stale-packages": "list its published packages built against outdated pins",
    "update-conda-forge-pinning": "move the conda-forge pinning to the latest version, rebuilding what changed pins affect (opens a PR)",
    "rebuild": "new builds of ROS packages, optionally with everything depending on them (opens a PR)",
    "rebuild-dependents": "on a pull request: add the rebuilds its ABI check suggests (pushes to its branch)",
    "update-vinca": "move the vinca pin to the newest commit of its branch (opens a PR)",
    "update-pixi": "upgrade the tooling's dependencies in pixi.toml / pixi.lock to their newest versions (opens a PR)",
    "dependency-report": "check the dependencies against the pins, in the distribution's dependency-report issue",
    "update-build-timings": "record the build times of the last week's build-branch runs, for batching by time (opens a PR)",
}
PER_DISTRO_COMMANDS = ("update-rosdistro-snapshot", "find-stale-packages", "update-conda-forge-pinning",
                       "dependency-report")
ALLOWED_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}


# The bot's PRs that don't change packages get build:none (only the checks run), so that
# they don't build every distribution (they change files outside distros/) while a
# build is under way; a maintainer removes the label (or adds build:<distro>... after
# removing it) to build them.
NO_BUILD_NOTE = ("Labelled `build:none` ({what}): only the checks run. Remove the label to build "
                 "every distribution, or replace it with `build:<distro>` labels to build some.")


@dataclass
class Result:
    title: str
    summary: str
    changed: bool = False
    ok: bool = True
    labels: list[str] = field(default_factory=list)  # extra labels for the bot's PR


def tail(text: str, lines: int = 80) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


# --------------------------------------------------------------------------- #
# per distribution
# --------------------------------------------------------------------------- #
def _versions(path: Path) -> dict[str, str]:
    data = yaml.safe_load(path.read_text()) if path.is_file() else {}
    return {k: str(v.get("version")) for k, v in (data or {}).items() if isinstance(v, dict)}


def snapshot_changes(old: dict[str, str], new: dict[str, str]) -> str:
    """Counts up front, the lists folded away (they run to hundreds of lines)."""
    added, removed = sorted(set(new) - set(old)), sorted(set(old) - set(new))
    bumped = sorted(k for k in set(old) & set(new) if old[k] != new[k])
    lines = [f"{len(bumped)} updated, {len(added)} added, {len(removed)} removed packages."]

    def fold(title: str, body: list[str]) -> None:
        lines.extend(["", f"<details><summary>{title}</summary>", "", *body, "", "</details>"])

    if bumped:
        fold(f"Updated ({len(bumped)})", ["| package | old | new |", "|---|---|---|"]
             + [f"| {k} | {old[k]} | {new[k]} |" for k in bumped])
    if added:
        fold(f"Added ({len(added)})", [", ".join(f"`{k}`" for k in added)])
    if removed:
        fold(f"Removed ({len(removed)})", [", ".join(f"`{k}`" for k in removed)])
    return "\n".join(lines)


REPODATA_SUBDIRS = ("linux-64", "linux-aarch64", "osx-64", "osx-arm64", "win-64")  # get_channel_records adds noarch


def released_builds(distro: str) -> dict[str, int] | None:
    """Highest build number of every package on the distribution's channel (all
    platforms), or None when the channel can't be read."""
    from vinca.utils import get_channel_records

    base = rs.repodata_url(distro)
    builds: dict[str, int] = {}
    for subdir in REPODATA_SUBDIRS:
        for record in get_channel_records(base, subdir):
            builds[record["name"]] = max(builds.get(record["name"], 0), int(record.get("build_number", 0)))
    return builds or None


def released_build_number(distro: str, builds: dict[str, int] | None = None) -> int | None:
    """Highest build number of the distribution's packages on its channel (the mutex
    aside), or None when the channel can't be read."""
    builds = released_builds(distro) if builds is None else builds
    if builds is None:
        return None
    mutex = (rs.read_vinca(distro).get("mutex_package") or {}).get("name")
    prefixes = (f"ros-{distro}-", "ros2-")
    numbers = [n for name, n in builds.items() if name.startswith(prefixes) and name != mutex]
    return max(numbers) if numbers else None


def bump_rebuild(distro: str) -> list[str]:
    """Rebuild everything: build_number = highest released build number + 1 (so every
    package gets a new build, whatever the current value), mutex minor + 1, and no
    per-package build numbers."""
    path = rs.DISTROS / distro / "vinca.yaml"
    text = path.read_text()
    current = int(re.search(r"(?m)^build_number:\s*(\d+)", text).group(1))
    released = released_build_number(distro)
    # per-package build numbers (dropped below) may not be on the channel yet, but
    # their builds can be in CI caches: stay above them as well
    info = rs.DISTROS / distro / "pkg_additional_info.yaml"
    overrides = [e["build_number"] for e in ((yaml.safe_load(info.read_text()) if info.is_file() else None) or {}).values()
                 if isinstance(e, dict) and isinstance(e.get("build_number"), int)]
    number = max([current if released is None else released, *overrides]) + 1
    text = re.sub(r"(?m)^(build_number:\s*)\d+", rf"\g<1>{number}", text, count=1)
    m = re.search(r"(?m)^mutex_package:\n(?:[ \t]+.*\n|\n)*?[ \t]+version:[ \t]*[\"']?(\d+)\.(\d+)\.(\d+)", text)
    if not m:
        raise SystemExit(f"{path}: no mutex_package version")
    old_mutex = ".".join(m.group(1, 2, 3))
    new_mutex = f"{m.group(1)}.{int(m.group(2)) + 1}.0"
    text = text[:m.start(1)] + new_mutex + text[m.end(3):]
    path.write_text(text)
    source = "channel unreadable, current" if released is None else f"highest released build {released}"
    if overrides and max(overrides) >= (current if released is None else released):
        source = f"highest per-package build number {max(overrides)}"
    lines = [f"`build_number` {current} → {number} ({source} + 1), mutex {old_mutex} → {new_mutex} (rebuilds every package)"]

    if info.is_file():
        ry = rs.ruamel_yaml()
        data = ry.load(info.read_text()) or {}
        dropped = []
        for pkg in list(data):
            entry = data[pkg]
            if isinstance(entry, dict) and "build_number" in entry:
                dropped.append(f"`{pkg}` ({entry['build_number']})")
                del entry["build_number"]
                if not entry:
                    del data[pkg]
        if dropped:
            with info.open("w") as fh:
                ry.dump(data, fh)
            from vinca.sort_yaml_keys import sort_mapping_keys

            sort_mapping_keys(info)  # `pixi run sort` order, or rs check fails
            lines.append(f"removed {len(dropped)} per-package build numbers: " + ", ".join(dropped))
    return lines


# A pinning change that rebuilds more than this share of a distribution's packages is
# done as a full rebuild (new mutex minor version) instead of a partial one.
FULL_REBUILD_SHARE = 0.5


def _pin_values(value) -> list[str]:
    return [str(v) for v in (value if isinstance(value, list) else [] if value is None else [value])]


def plan_rebuild(distro: str, old_cbc: str) -> tuple[dict[str, str], int, dict[str, tuple]]:
    """Packages of the distribution that the change from old_cbc to its current
    conda_build_config.yaml has to rebuild (with the reason), how many packages it has
    (vinca-rebuild-plan), and the changed pins ({name: (before, after)})."""
    from vinca import rebuild

    new = yaml.safe_load((rs.DISTROS / distro / "conda_build_config.yaml").read_text()) or {}
    changed = rebuild.changed_pins(yaml.safe_load(old_cbc) or {}, new)
    # only the quoting changed (e.g. '6' -> 6): the same pin
    changed = {k: v for k, v in changed.items() if _pin_values(v[0]) != _pin_values(v[1])}
    if not changed:
        return {}, 0, {}
    requirements, prefix = rebuild.requirements_from_vinca(rs.prepare(distro))
    return rebuild.plan(requirements, changed, prefix), len(requirements), changed


def pin_report(changed: dict[str, tuple], packages: dict[str, str]) -> list[str]:
    """The changed pins, those packages use first (with the packages using them
    directly), the others folded away."""
    users: dict[str, list[str]] = {}
    for name, reason in packages.items():
        if reason.startswith("uses "):
            for pin in reason[len("uses "):].split(", "):
                users.setdefault(pin, []).append(name)

    def change(pin: str) -> str:
        before, after = (", ".join(_pin_values(v)) or "(none)" for v in changed[pin])
        return f"`{before}` → `{after}`"

    used = sorted(p for p in changed if p in users)
    unused = sorted(p for p in changed if p not in users)
    lines = []
    if used:
        lines += ["| changed pin | before → after | used directly by |", "|---|---|---|"]
        lines += [f"| `{p}` | {change(p)} | {', '.join(f'`{n}`' for n in sorted(users[p]))} |" for p in used]
    if unused:
        lines += ["" if lines else None, *_details(f"Changed pins no package uses ({len(unused)})",
                                [f"- `{p}`: {change(p)}" for p in unused])]
    return [line for line in lines if line is not None]


def bump_partial(distro: str, packages: dict[str, str]) -> list[str]:
    """Rebuild just these packages: a per-package build number above everything the
    channel has. The mutex has no run_constraints, so it never needs a new build here."""
    builds = released_builds(distro)
    current = int(re.search(r"(?m)^build_number:\s*(\d+)", (rs.DISTROS / distro / "vinca.yaml").read_text()).group(1))
    released = released_build_number(distro, builds)
    number = (current if released is None else max(current, released)) + 1

    info = rs.DISTROS / distro / "pkg_additional_info.yaml"
    ry = rs.ruamel_yaml()
    data = (ry.load(info.read_text()) if info.is_file() else None) or {}
    for pkg in packages:
        entry = data.get(pkg)
        if entry is None:
            data[pkg] = {"build_number": number}
        else:
            entry["build_number"] = number
    with info.open("w") as fh:
        ry.dump(data, fh)
    from vinca.sort_yaml_keys import sort_mapping_keys

    sort_mapping_keys(info)  # `pixi run sort` order (rs check fails otherwise): build-number entries last
    return [f"{len(packages)} packages get `build_number: {number}` in `distros/{distro}/pkg_additional_info.yaml`"]


def dependency_conflicts(distro: str) -> set[str] | None:
    """Packages that can't be installed together with the rest of the distribution's
    dependencies under its current pins (check_dependency_compat.py,
    linux-64); None when the check itself failed."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "conflicts.json"
        rc = rs.task(distro, "check-deps", ["--platform", "linux-64", "--no-migrations", "--json", str(out)])
        if rc == 0:
            return set()
        if not out.is_file():
            return None
        data = json.loads(out.read_text())
    return set(data.get("pin_conflicts") or {}) | set(data.get("conflicts") or {})


def _details(summary: str, lines: list[str]) -> list[str]:
    return ["<details>", f"<summary>{summary}</summary>", "", *lines, "", "</details>"]


SYNC_TAG = re.compile(r"^(?P<distro>[a-z]+)/(?P<date>\d{4}-\d{2}-\d{2})$")
ROSDISTRO = "https://github.com/ros/rosdistro"


def latest_sync(distro: str) -> str | None:
    """Newest sync tag of the distribution in ros/rosdistro, e.g. jazzy/2026-10-05.
    The ROS release team tags every sync from ros-testing to the main repositories."""
    # git, not the GitHub API: no rate limit without a token
    out = subprocess.run(["git", "ls-remote", "--tags", ROSDISTRO, f"refs/tags/{distro}/*"],
                         capture_output=True, text=True, check=True).stdout
    tags = [line.split("\t")[1].removeprefix("refs/tags/") for line in out.splitlines() if "\t" in line]
    tags = [t for t in tags if (m := SYNC_TAG.match(t)) and m["distro"] == distro]
    return max(tags, key=lambda t: t.split("/")[1], default=None)


def _announcement(distro: str, date: str) -> str:
    """Link to the Discourse announcement of a sync ("New Packages for Jazzy Jalisco
    2026-10-05"), or to a search for it."""
    import urllib.parse
    import urllib.request

    search = f"https://discourse.openrobotics.org/search?q={urllib.parse.quote(f'{distro} {date} in:title')}"
    try:
        request = urllib.request.Request(search.replace("/search?", "/search.json?"), headers={"User-Agent": "robostack-bot"})
        with urllib.request.urlopen(request, timeout=30) as response:
            topics = json.load(response).get("topics", [])
    except Exception:
        return search
    for topic in topics:
        if re.match(r"(?i)new packages", topic.get("title", "")) and date in topic["title"]:
            return f"https://discourse.openrobotics.org/t/{topic['slug']}/{topic['id']}"
    return search


def _set_setting(distro: str, key: str, value: str) -> None:
    """Set a top-level scalar in distros/<d>/distro.yaml, keeping the rest as is."""
    path = rs.DISTROS / distro / "distro.yaml"
    text = path.read_text()
    line = f"{key}: {value}"
    if re.search(rf"(?m)^{key}:.*$", text):
        text = re.sub(rf"(?m)^{key}:.*$", line, text, count=1)
    else:
        text = text.rstrip("\n") + "\n" + line + "\n"
    path.write_text(text)


def _pending_sync(distro: str) -> str | None:
    """The sync recorded on an open bot PR for the distribution, if any."""
    branch = f"bot/update-rosdistro-snapshot-{distro}"
    fetch = subprocess.run(["git", "fetch", "-q", "--depth=1", "origin", branch], cwd=rs.ROOT, capture_output=True)
    if fetch.returncode:
        return None
    show = subprocess.run(["git", "show", f"FETCH_HEAD:distros/{distro}/distro.yaml"], cwd=rs.ROOT,
                          capture_output=True, text=True)
    return (yaml.safe_load(show.stdout) or {}).get("rosdistro_sync") if show.returncode == 0 else None


def rosdistro_syncs() -> list[dict]:
    """Bot jobs for the distributions with a rosdistro sync newer than the one they
    (or their open snapshot PR) are on. Distributions with `rosdistro_sync: manual`
    are left out."""
    jobs = []
    for distro in rs.distros():
        recorded = rs.settings(distro).get("rosdistro_sync")
        if recorded == "manual":
            continue
        latest = latest_sync(distro)
        known = [t for t in (recorded, _pending_sync(distro)) if t]
        if latest and all(latest.split("/")[1] > t.split("/")[1] for t in known):
            print(f"{distro}: new rosdistro sync {latest} (on {recorded or 'none'})", file=sys.stderr)
            jobs.append({"command": "update-rosdistro-snapshot", "distro": distro, "args": "", "preview": False})
        else:
            print(f"{distro}: on the latest sync {latest}", file=sys.stderr)
    return jobs


def _newer(a: str, b: str) -> bool:
    from rattler import Version
    from rattler.exceptions import InvalidVersionError

    try:
        return Version(a) > Version(b)
    except InvalidVersionError:
        return False


def released_on_master(distro: str) -> set[str] | None:
    """Packages released in the distribution on rosdistro master (None if unreadable)."""
    import urllib.request

    try:
        with urllib.request.urlopen(f"https://raw.githubusercontent.com/ros/rosdistro/master/{distro}/distribution.yaml",
                                    timeout=60) as resp:
            repos = (yaml.safe_load(resp.read()) or {}).get("repositories") or {}
    except OSError:
        return None
    released = set()
    for name, repo in repos.items():
        release = (repo or {}).get("release") or {}
        if release.get("version"):
            released.update(release.get("packages") or [name])
    return released


def keep_newer(old: dict, new: dict, on_master: set[str] = frozenset()) -> tuple[dict, list[str]]:
    """The new snapshot, except that repositories the old one has ahead of it keep all
    their old entries, including packages the new one drops or adds: a sync never
    downgrades. A repository is ahead when one of its packages has a newer version, or
    when the sync drops a package that rosdistro master (`on_master`) still releases
    (the old snapshot was taken from master, or entries were spliced in from it)."""
    def repo(entry) -> str | None:
        return entry.get("repository") or entry.get("url") if isinstance(entry, dict) else None

    ahead = sorted({repo(old[k]) for k in set(old) & set(new)
                    if repo(old[k]) and _newer(str(old[k].get("version")), str(new[k].get("version")))}
                   | {repo(old[k]) for k in set(old) - set(new) if k in on_master and repo(old[k])})
    if not ahead:
        return new, []
    merged = {k: v for k, v in new.items() if repo(v) not in ahead}
    merged.update({k: v for k, v in old.items() if repo(v) in ahead})
    return dict(sorted(merged.items())), ahead


def update_snapshot(distro: str) -> Result:
    """Snapshot the distribution's latest rosdistro sync (rosdistro master with
    `rosdistro_sync: manual`), then bump for a full rebuild."""
    snapshot = rs.DISTROS / distro / "rosdistro_snapshot.yaml"
    recorded = rs.settings(distro).get("rosdistro_sync")
    ref = None if recorded == "manual" else latest_sync(distro)
    old_text = snapshot.read_text() if snapshot.is_file() else ""
    old = _versions(snapshot)
    if rs.task(distro, "create-snapshot", ["--rosdistro-ref", ref] if ref else []):
        return Result(f"{distro}: snapshot update failed", "`vinca-snapshot` failed, see the log.", ok=False)
    text = snapshot.read_text()
    data, ahead = keep_newer(yaml.safe_load(old_text) or {}, yaml.safe_load(text) or {},
                             (released_on_master(distro) or set()) if ref else set())
    if ahead:
        header = "".join(l for l in text.splitlines(True) if l.startswith("#"))
        snapshot.write_text(header + yaml.safe_dump(data, sort_keys=True, default_flow_style=False))
    new = _versions(snapshot)
    if ref:
        _set_setting(distro, "rosdistro_sync", ref)
        date = ref.split("/")[1]
        source = [
            f"rosdistro sync [`{ref}`]({ROSDISTRO}/tree/{ref})"
            + (f" ([changes since `{recorded}`]({ROSDISTRO}/compare/{recorded}...{ref}))" if recorded else "")
            + f", [announcement]({_announcement(distro, date)}).",
            "",
        ]
        title = f"{distro}: rosdistro sync {date}"
        if ahead:
            source[-1:] = ["", "Kept at their newer versions (the previous snapshot is ahead of the sync for these "
                           "repositories): " + ", ".join(f"<{r}>" for r in ahead) + ".", "", ""]
    else:
        source, title = ["rosdistro master (this distribution doesn't follow syncs).", ""], f"{distro}: update rosdistro snapshot"
    if old == new:
        return Result(f"{distro}: snapshot is up to date", "\n".join(source) + "No package versions changed.",
                      changed=bool(ref and ref != recorded))
    bump = bump_rebuild(distro)
    return Result(title, "\n".join(source) + snapshot_changes(old, new) + "\n\n" + "\n".join(f"- {l}" for l in bump),
                  changed=True)


def find_stale(distro: str) -> Result:
    url = rs.repodata_url(distro)
    rs.prepare(distro)
    proc = subprocess.run(
        [sys.executable, str(rs.TOOLS / "check_dependency_compat.py"), "--stale", "--repodata", url],
        cwd=rs.work_dir(distro), capture_output=True, text=True,
    )
    output = tail(proc.stdout + proc.stderr, 200)
    if "Traceback (most recent call last)" in output:
        return Result(f"{distro}: find-stale-packages failed", f"```\n{output}\n```", ok=False)
    stale = proc.returncode != 0
    return Result(
        f"{distro}: {'stale packages' if stale else 'no stale packages'}",
        f"`check_dependency_compat.py --stale --repodata {url}`\n\n"
        f"<details><summary>Report</summary>\n\n```\n{output}\n```\n\n</details>",
        ok=not stale,
    )


# --------------------------------------------------------------------------- #
# whole repository
# --------------------------------------------------------------------------- #
def _set_pinning(distro: str, version: str, migrations: list[str]) -> str:
    """Write the new pinning version and migrations where the distribution takes them
    from: its own distro.yaml, or shared/pinning/conda_forge.yaml. Returns that file."""
    if rs.settings(distro).get("conda_forge_pinning_version"):
        path = rs.DISTROS / distro / "distro.yaml"
        text = path.read_text()
        text = re.sub(r"(?m)^conda_forge_pinning_version:.*$", f"conda_forge_pinning_version: {version}", text, count=1)
        text = re.sub(r"(?m)^conda_forge_migrations:(?:.*\n)(?:[ \t]+.*\n)*",
                      f"conda_forge_migrations: [{', '.join(migrations)}]\n", text, count=1)
        path.write_text(text)
        return f"distros/{distro}/distro.yaml"
    shared = rs.SHARED / "pinning" / "conda_forge.yaml"
    header = [l for l in shared.read_text().splitlines() if l.startswith("#")]
    body = [f"conda_forge_pinning_version: {version}", "migrations:"] + [f"  - {m}" for m in migrations]
    shared.write_text("\n".join(header + body) + "\n")
    return "shared/pinning/conda_forge.yaml"


def uses_shared_pinning(distro: str) -> bool:
    """Whether the distribution takes its conda-forge pinning from shared/pinning/conda_forge.yaml
    (rather than conda_forge_pinning_version / conda_forge_migrations in its distro.yaml)."""
    return not rs.settings(distro).get("conda_forge_pinning_version")


def update_pinning(distro: str) -> Result:
    """Move the distribution's conda-forge pinning to the latest version, with the
    migrations that are done for its dependencies. Only the packages that use a changed
    pin (and what depends on them) are rebuilt, unless that is most of them. A
    distribution on the shared pinning moves together with every other one on it."""
    if uses_shared_pinning(distro):
        return update_shared_pinning()
    from vinca import pinning

    settings = rs.settings(distro)
    before = (settings.get("conda_forge_pinning_version"), list(settings.get("conda_forge_migrations") or []))
    print(f"Collecting dependencies of {distro}", flush=True)
    work = rs.prepare(distro)
    dependencies = pinning.dependencies_from_vinca(work, pinning.DEFAULT_PLATFORMS)
    version, migrations, reports = pinning.update_pinning(work / "vinca_pinning.yaml", dependencies=dependencies)
    if str(before[0]) == str(version) and before[1] == list(migrations):
        return Result(f"{distro}: pinning is up to date", f"{distro} is on conda-forge-pinning {version}.")
    return _move_pinning([distro], before, str(version), list(migrations), reports)


def update_shared_pinning() -> Result:
    """update_pinning for every distribution on the shared pinning at once: the
    migrations have to be done for all of their dependencies, and the shared file is
    written once (one pull request instead of one per distribution)."""
    from vinca import pinning

    distros = [d for d in rs.distros() if uses_shared_pinning(d)]
    shared = yaml.safe_load((rs.SHARED / "pinning" / "conda_forge.yaml").read_text()) or {}
    before = (shared.get("conda_forge_pinning_version"), list(shared.get("migrations") or []))
    dependencies: set[str] = set()
    works = {}
    for distro in distros:
        print(f"Collecting dependencies of {distro}", flush=True)
        works[distro] = rs.prepare(distro)
        dependencies |= pinning.dependencies_from_vinca(works[distro], pinning.DEFAULT_PLATFORMS)
    version, migrations, reports = pinning.update_pinning(works[distros[0]] / "vinca_pinning.yaml",
                                                          dependencies=dependencies)
    if str(before[0]) == str(version) and before[1] == list(migrations):
        return Result("conda-forge pinning is up to date", f"{', '.join(distros)}: on conda-forge-pinning {version}.")
    return _move_pinning(distros, before, str(version), list(migrations), reports)


def _move_pinning(distros: list[str], before: tuple, version: str, migrations: list[str], reports) -> Result:
    """Write the new pinning (once: the distributions share it, or it is one), render
    each distribution's pins and plan its rebuild."""
    before_cbc, conflicts_before = {}, {}
    for distro in distros:
        cbc = rs.DISTROS / distro / "conda_build_config.yaml"
        before_cbc[distro] = cbc.read_text() if cbc.is_file() else ""
        print(f"Checking the dependencies of {distro} with the current pins", flush=True)
        conflicts_before[distro] = dependency_conflicts(distro)
    where = _set_pinning(distros[0], version, migrations)
    who = ", ".join(distros)

    added = sorted(set(migrations) - set(before[1]))
    candidates = {name for name, _ in reports}
    finished = sorted(set(before[1]) - set(migrations) - candidates)
    removed = sorted(set(before[1]) - set(migrations) - set(finished))
    lines = [
        f"Moves **{who}** from conda-forge-pinning `{before[0]}` to `{version}` (`{where}`).",
        "",
        "Migrations: " + (", ".join(f"`{m}`" for m in migrations) or "none")
        + (f"; **new: {', '.join(f'`{m}`' for m in added)}**" if added
           else "; none new (the pins below change with conda-forge-pinning itself)")
        + (f"; finished (now part of the base pinning): {', '.join(f'`{m}`' for m in finished)}" if finished else "")
        + (f"; dropped: {', '.join(f'`{m}`' for m in removed)}" if removed else ""),
    ]
    conflicts = False
    for distro in distros:
        if rs.task(distro, "render-pinning", []):
            return Result(f"{distro}: pinning update failed", "`render-pinning` failed, see the log.", ok=False)
        lines += ["", f"#### {distro}", ""] if len(distros) > 1 else [""]
        lines_d, conflicts_d = _rebuild_for_pins(distro, before_cbc[distro], conflicts_before[distro])
        lines += lines_d
        conflicts = conflicts or conflicts_d

    groups: dict[str, list[str]] = {"selected": [], "waiting": [], "other": []}
    for name, text in reports:
        key = "selected" if text.startswith(("selected", "kept")) else "waiting" if text.startswith("waiting") else "other"
        groups[key].append(f"- `{name}`: {text}")
    lines += [
        "",
        *_details(f"Migrations used ({len(groups['selected'])})", groups["selected"] or ["none"]),
        "",
        *_details(f"Migrations waiting for our dependencies' feedstocks ({len(groups['waiting'])})",
                  groups["waiting"] or ["none"]),
        "",
        *_details(f"Migrations that don't concern {who} ({len(groups['other'])})", groups["other"] or ["none"]),
    ]
    title = (f"{distros[0]}: update conda-forge pinning to {version}" if len(distros) == 1
             else f"Update the shared conda-forge pinning to {version}")
    return Result(title, "\n".join(lines), changed=True, labels=["dependency-conflict"] if conflicts else [])


def _rebuild_for_pins(distro: str, before_cbc: str, conflicts_before) -> tuple[list[str], bool]:
    """The rebuild a distribution's newly rendered pins need (build numbers bumped), and
    the dependency check against the pins before."""
    cbc = rs.DISTROS / distro / "conda_build_config.yaml"
    if cbc.read_text() == before_cbc:
        return ["The rendered pins don't change: nothing to rebuild."], False
    lines = []
    print(f"Planning the rebuild of {distro}", flush=True)
    packages, total, changed = plan_rebuild(distro, before_cbc)
    if not changed:
        return ["Only the formatting of the rendered pins changes: nothing to rebuild."], False
    lines += [*pin_report(changed, packages), ""]
    if not packages:
        lines.append("No package uses a changed pin: nothing to rebuild.")
    elif total and len(packages) > FULL_REBUILD_SHARE * total:
        lines.append(f"**Full rebuild**: {len(packages)} of {total} packages use a changed pin or depend "
                     "on one. " + "; ".join(bump_rebuild(distro)))
    else:
        direct = sum(reason.startswith("uses ") for reason in packages.values())
        lines.append(f"**Rebuild {len(packages)} of {total} packages**: {direct} "
                     f"{'uses' if direct == 1 else 'use'} a changed pin, {len(packages) - direct} "
                     f"{'depends' if len(packages) - direct == 1 else 'depend'} on one. " + "; ".join(bump_partial(distro, packages)))
        lines += ["", *_details(f"Packages to rebuild ({len(packages)})", [
            f"- `{name}`: {reason}" for name, reason in sorted(
                packages.items(), key=lambda item: (not item[1].startswith("uses "), item[0]))])]
    conflicts_after = dependency_conflicts(distro)
    if conflicts_after is None or conflicts_before is None:
        conflicts = conflicts_after is None
        lines += ["", "Dependency check (linux-64): ❌ the check failed, see the workflow log"
                  if conflicts else "Dependency check (linux-64): no comparison (the check with the old pins failed)"]
    else:
        new_conflicts = sorted(conflicts_after - conflicts_before)
        conflicts = bool(new_conflicts)
        lines += ["", "Dependency check (linux-64): " + (
            "❌ new conflicts: " + ", ".join(f"`{n}`" for n in new_conflicts) if new_conflicts
            else "✅ no new conflicts")]
        if conflicts_before:
            lines += ["", *_details(f"Conflicts that already exist with the current pins ({len(conflicts_before)})",
                                    [", ".join(f"`{n}`" for n in sorted(conflicts_before))])]
    return lines, conflicts


def new_distro(name: str, source: str) -> Result:
    """distros/NAME from the template of an existing distribution: its package
    selection and settings, but no build numbers, patches or own pins."""
    src, dest = rs.DISTROS / source, rs.DISTROS / name
    if dest.exists():
        raise SystemExit(f"{dest} exists")
    if not (src / "vinca.yaml").is_file():
        raise SystemExit(f"no distribution {source!r} to start from (distros/{source}/vinca.yaml)")
    dest.mkdir(parents=True)
    try:
        return _populate_new_distro(name, source, src, dest)
    except BaseException:
        # leave nothing behind, so the corrected command can run again
        shutil.rmtree(dest, ignore_errors=True)
        raise


def _populate_new_distro(name: str, source: str, src: Path, dest: Path) -> Result:
    settings = rs.settings(source)
    keep = {k: settings[k] for k in ("read_from", "upload_to") if k in settings}
    (dest / "distro.yaml").write_text(
        f"# Settings of ros-{name} that differ from the other distributions\n"
        "# (everything else is shared, see the README).\n"
        + yaml.safe_dump({"channel_name": f"robostack-{name}", **keep}, sort_keys=False)
    )
    vinca = (src / "vinca.yaml").read_text()
    vinca = re.sub(r"(?m)^ros_distro:.*$", f"ros_distro: {name}", vinca)
    vinca = re.sub(r"(?m)^build_number:.*$", "build_number: 0", vinca)
    vinca = re.sub(r"(?m)^package_name_mode:.*\n", "", vinca)  # shared default: both
    (dest / "vinca.yaml").write_text(vinca)
    info = yaml.safe_load((src / "pkg_additional_info.yaml").read_text()) or {}
    info = {k: {a: b for a, b in v.items() if a != "build_number"} for k, v in info.items() if isinstance(v, dict)}
    (dest / "pkg_additional_info.yaml").write_text(yaml.safe_dump({k: v for k, v in info.items() if v}, sort_keys=True))
    (dest / "rosdistro_additional_recipes.yaml").write_text("{}\n")
    (dest / "patch").mkdir()
    (dest / "ci.yaml").write_text(_default_ci_yaml())
    steps = [rs.task(name, "create-snapshot", []), rs.task(name, "render-pinning", [])]
    patches = sorted(p.name for p in (src / "patch").glob("*.patch"))
    lines = [
        f"Created `distros/{name}/` from `distros/{source}/` "
        f"(snapshot: {'ok' if not steps[0] else 'FAILED'}, pinning: {'ok' if not steps[1] else 'FAILED'}).",
        "",
        "Next steps:",
        f"- [ ] review `distros/{name}/vinca.yaml` (mutex name/version, package selection)",
        f"- [ ] create the `robostack-{name}` channel and its Repository Access (trusted publishing, see the README)",
        f"- [ ] port patches that still apply ({len(patches)} in `distros/{source}/patch/`), "
        f"check with `pixi run rs {name} check-patches`",
    ]
    return Result(f"New distribution {name}", "\n".join(lines), changed=True, ok=not any(steps))


PACKAGE_NAME = re.compile(r"^[a-z][a-z0-9_]*$")  # valid ROS package names only
MAX_PACKAGES = 10  # per request


def _package_name(name: str) -> str:
    """ROS package name from a request: `ros-humble-foo-bar`, `ros2-foo-bar`, `foo-bar` -> foo_bar."""
    name = name.strip().strip("`'\",.").lower()
    name = re.sub(r"^(ros2-|ros-[a-z]+-)", "", name)
    return name.replace("-", "_")


PLATFORMS = ("linux-64", "linux-aarch64", "osx-64", "osx-arm64", "win-64")


def _selected(distro: str) -> set[str]:
    """Packages the distribution selects (on at least one platform)."""
    names: set[str] = set()
    for platform in PLATFORMS:
        names |= {n.replace("-", "_") for n in rs.read_vinca(distro, platform)["packages_select_by_deps"]}
    return names


def _available(distro: str) -> set[str]:
    d = rs.DISTROS / distro
    names = set(yaml.safe_load((d / "rosdistro_snapshot.yaml").read_text()) or {})
    extra = d / "rosdistro_additional_recipes.yaml"
    if extra.is_file():
        names |= set(yaml.safe_load(extra.read_text()) or {})
    return names


def _append_selection(path: Path, packages: list[str]) -> None:
    """Append to packages_select_by_deps keeping the file's comments (round trip)."""
    ry = rs.ruamel_yaml()
    data = ry.load(path.read_text()) if path.is_file() else {}
    data.setdefault("packages_select_by_deps", [])
    items = data["packages_select_by_deps"]
    for pkg in packages:
        # into the run of plain (unconditional) names, in sorted position
        index = 0
        while (index < len(items) and isinstance(items[index], str)
               and items[index].replace("-", "_").lower() < pkg.lower()):
            index += 1
        items.insert(index, pkg)
    with path.open("w") as fh:
        ry.dump(data, fh)


def add_package(packages: list[str], distros: list[str], preview: bool = False) -> Result:
    """Select ROS packages for building in shared/vinca.yaml, and list for the given
    (default: all) distributions the recipes this adds on linux-64."""
    wanted = list(dict.fromkeys(n for n in (_package_name(p) for p in packages if p.strip()) if PACKAGE_NAME.match(n)))
    wanted = wanted[:MAX_PACKAGES]
    if not wanted:
        return Result("add-package: no package given", "Name at least one ROS package.", ok=False)
    targets = distros or rs.distros()
    plan: dict[str, list[str]] = {}
    lines: list[str] = []
    for distro in targets:
        available, selected = _available(distro), _selected(distro)
        add = []
        for pkg in wanted:
            if pkg not in available:
                lines.append(f"- **{distro}**: `{pkg}` is not in its rosdistro")
            elif pkg in selected:
                lines.append(f"- **{distro}**: `{pkg}` is already selected")
            else:
                add.append(pkg)
        if add:
            plan[distro] = add
    if not plan:
        return Result(f"add-package {' '.join(wanted)}: nothing to add", "\n".join(lines))

    def recipes(distro: str) -> set[str] | None:
        """Recipes to build on linux-64 (packages already on the channel are skipped)."""
        if rs.task(distro, "generate-recipes", ["--platform", "linux-64"]):
            return None
        return {p.name for p in (rs.work_dir(distro) / "recipes").iterdir()}

    before = {distro: recipes(distro) for distro in plan}

    # the whole selection is shared; vinca skips packages a distribution doesn't have
    backup = {p: p.read_bytes() for p in [rs.SHARED / "vinca.yaml"]}
    _append_selection(rs.SHARED / "vinca.yaml", sorted({pkg for add in plan.values() for pkg in add}))
    subprocess.run(["vinca-sort-vinca-lists", "shared/vinca.yaml"], cwd=rs.ROOT, check=True)
    where = "`shared/vinca.yaml` (every distribution that has them builds them)"

    # what the request adds to the build (linux-64)
    if lines:
        lines.append("")
    builds_ok = True
    for distro, add in plan.items():
        after = recipes(distro)
        if after is None or before[distro] is None:
            builds_ok = False
            lines.append(f"- **{distro}**: adding {', '.join(f'`{p}`' for p in add)} — recipe generation **failed**, see the log")
            continue
        new = sorted(after - before[distro])
        shown = ", ".join(f"`{r}`" for r in new[:25]) + (f" and {len(new) - 25} more" if len(new) > 25 else "")
        published = [p for p in add if f"ros2-{p.replace('_', '-')}" not in after]
        lines.append(f"- **{distro}**: adding {', '.join(f'`{p}`' for p in add)} → {len(new)} new package{'s' if len(new) != 1 else ''} to build on linux-64"
                     + (f": {shown}" if new else ""))
        if published:
            lines.append(f"  - already published (built as a dependency): {', '.join(f'`{p}`' for p in published)}")
    if preview:
        for path, content in backup.items():
            path.write_bytes(content)
        request = " ".join(wanted + (distros if distros else []))
        lines += ["", f"_Preview only: a maintainer can open the PR with `@robostack-bot add-package {request}`._"]
        return Result(f"add-package {' '.join(wanted)} (preview)", "\n".join(lines), ok=builds_ok)
    lines += ["", f"Added to {where}. CI builds the new packages on all platforms."]
    return Result(f"Add {', '.join(wanted)}", "\n".join(lines), changed=True, ok=builds_ok)


REBUILD_SEED = "robostack-rebuild-request"


def rebuild_plan(requirements: dict, prefix: str, packages: list[str], with_dependents: bool) -> dict[str, str]:
    """The packages to rebuild, with the reason: the requested ones the distribution
    builds and, with_dependents, every package depending on them (host or run,
    transitively; vinca's rebuild planner)."""
    from vinca import rebuild

    wanted = [p for p in packages if p in requirements]
    if not with_dependents:
        return {p: "requested" for p in wanted}
    # the planner rebuilds the packages using a changed pin and everything that
    # depends on them: give only the requested packages a made-up pin that changed
    seeded = {name: {**reqs, "build": [*(reqs.get("build") or []), [REBUILD_SEED]]} if name in wanted else reqs
              for name, reqs in requirements.items()}
    planned = rebuild.plan(seeded, [REBUILD_SEED], prefix)
    return {name: "requested" if name in wanted else reason for name, reason in planned.items()}


def rebuild_packages(packages: list[str], distros: list[str], with_dependents: bool = False) -> Result:
    """New builds of the packages (and, with_dependents, what depends on them) in the
    given distributions (default: every one that builds them): per-package build
    numbers above everything on the channel (bump_partial)."""
    from vinca import rebuild

    wanted = list(dict.fromkeys(n for n in (_package_name(p) for p in packages if p.strip()) if PACKAGE_NAME.match(n)))
    wanted = wanted[:MAX_PACKAGES]
    if not wanted:
        return Result("rebuild: no package given", "Name at least one ROS package.", ok=False)
    title = f"Rebuild {', '.join(wanted)}" + (" and dependents" if with_dependents else "")
    lines: list[str] = []
    changed = False
    for distro in distros or rs.distros():
        requirements, prefix = rebuild.requirements_from_vinca(rs.prepare(distro))
        if missing := [p for p in wanted if p not in requirements]:
            lines.append(f"- **{distro}** doesn't build {', '.join(f'`{p}`' for p in missing)}")
        plan = rebuild_plan(requirements, prefix, wanted, with_dependents)
        if not plan:
            continue
        changed = True
        lines.append(f"- **{distro}**: " + "; ".join(bump_partial(distro, plan)))
        if dependents := sorted(n for n, reason in plan.items() if reason != "requested"):
            lines += ["", *_details(f"{distro}: dependents ({len(dependents)})",
                                    [f"- `{n}`: {plan[n]}" for n in dependents]), ""]
    if not changed:
        return Result(f"{title}: nothing to rebuild", "\n".join(lines) or "No distribution builds these packages.")
    return Result(title, "\n".join(lines), changed=True)


ABI_MARKER = "<!-- robostack-abi-check -->"
ABI_AUTHOR = "github-actions[bot]"  # testpr.yaml's abi-comment job posts with the workflow token


def abi_rebuilds(comment: str) -> dict[str, dict[str, int]]:
    """The pkg_additional_info.yaml entries the ABI check's pull-request comment
    suggests, per distribution: the ```yaml block in each `#### <distro> <platform>`
    section (tools/abi_check.py, rebuild_snippet). With several platforms of a
    distribution, the highest build number of a package."""
    found: dict[str, dict[str, int]] = {}
    for section in re.split(r"(?m)^#### ", comment)[1:]:
        heading, _, body = section.partition("\n")
        distro = (heading.split() or [""])[0]
        block = re.search(r"(?ms)^```yaml\n(.*?)^```", body)
        if not re.match(r"^[a-z]+$", distro) or not block:
            continue
        try:
            entries = yaml.safe_load(block.group(1)) or {}
        except yaml.YAMLError:
            continue
        for name, entry in (entries.items() if isinstance(entries, dict) else []):
            number = entry.get("build_number") if isinstance(entry, dict) else None
            if PACKAGE_NAME.match(str(name)) and isinstance(number, int) and not isinstance(number, bool):
                packages = found.setdefault(distro, {})
                packages[name] = max(packages.get(name, 0), number)
    return found


def merge_build_numbers(path: Path, entries: dict[str, int]) -> list[str]:
    """Set the build numbers in a pkg_additional_info.yaml, keeping the other keys of
    existing entries (and a build number that is already higher). Returns the
    packages that changed."""
    from vinca.sort_yaml_keys import sort_mapping_keys

    ry = rs.ruamel_yaml()
    data = (ry.load(path.read_text()) if path.is_file() else None) or {}
    changed = []
    for name, number in sorted(entries.items()):
        entry = data.get(name)
        if entry is None:
            data[name] = {"build_number": number}
        elif int(entry.get("build_number", -1)) < number:
            entry["build_number"] = number
        else:
            continue
        changed.append(name)
    if changed:
        with path.open("w") as fh:
            ry.dump(data, fh)
        sort_mapping_keys(path)  # `pixi run sort` order: build-number entries last
    return changed


def _gh(args: list[str]) -> str:
    proc = subprocess.run(["gh", *args], cwd=rs.ROOT, capture_output=True, text=True)
    if proc.returncode:
        raise RuntimeError(f"gh {' '.join(args[:2])} failed: {tail(proc.stderr, 5)}")
    return proc.stdout


def _repository() -> str:
    return os.environ.get("GITHUB_REPOSITORY") or _gh(["repo", "view", "--json", "nameWithOwner", "-q",
                                                       ".nameWithOwner"]).strip()


def _abi_comment(repo: str, pr: str) -> str | None:
    """The ABI check's comment on the pull request (the newest; it is updated in place)."""
    query = (f'.[] | select(.user.login == {json.dumps(ABI_AUTHOR)}) '
             f'| select(.body | startswith({json.dumps(ABI_MARKER)})) | .body | @json')
    out = _gh(["api", "--paginate", f"repos/{repo}/issues/{pr}/comments", "--jq", query])
    bodies = [json.loads(line) for line in out.splitlines() if line.strip()]
    return bodies[-1] if bodies else None


def _snippets(rebuilds: dict[str, dict[str, int]]) -> list[str]:
    lines = []
    for distro, entries in sorted(rebuilds.items()):
        lines += [f"`distros/{distro}/pkg_additional_info.yaml`:", "", "```yaml",
                  *[l for name, n in sorted(entries.items()) for l in (f"{name}:", f"  build_number: {n}")], "```", ""]
    return lines


def rebuild_dependents(pr: str) -> Result:
    """Commit the rebuilds the ABI check suggests to the pull request's own branch."""
    repo = _repository()
    title = f"rebuild-dependents #{pr}"
    pull = json.loads(_gh(["api", f"repos/{repo}/pulls/{pr}"]))
    comment = _abi_comment(repo, pr)
    if comment is None:
        return Result(f"{title}: no ABI check", "The ABI check hasn't commented on this pull request (yet).", ok=False)
    known = set(rs.distros())
    rebuilds = {d: e for d, e in abi_rebuilds(comment).items() if d in known}
    if not rebuilds:
        return Result(f"{title}: nothing to rebuild", "The ABI check suggests no rebuilds of dependents.")
    head = pull["head"]
    if pull.get("state") != "open":
        return Result(f"{title}: the pull request is closed", "Nothing changed.", ok=False)
    if (head.get("repo") or {}).get("full_name") != repo:
        # robostack-bot's token only reaches this repository
        return Result(f"{title}: can't push to a fork", "\n".join([
            "This pull request comes from a fork, which robostack-bot can't push to. "
            "Please add these entries yourself (for a package that already has an entry, set its `build_number`):",
            "", *_snippets(rebuilds)]), ok=False)

    def git(*args: str, cwd: Path = rs.ROOT) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)

    branch = head["ref"]
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "pr"
        if (fetch := git("fetch", "-q", "origin", f"refs/heads/{branch}")).returncode:
            return Result(f"{title}: can't fetch `{branch}`", f"```\n{tail(fetch.stderr, 10)}\n```", ok=False)
        git("worktree", "add", "-q", "--detach", str(work), "FETCH_HEAD")
        try:
            lines = []
            for distro, entries in sorted(rebuilds.items()):
                info = work / "distros" / distro / "pkg_additional_info.yaml"
                if not info.parent.is_dir():
                    lines.append(f"- **{distro}**: not in this branch, skipped")
                elif changed := merge_build_numbers(info, entries):
                    lines.append(f"- **{distro}**: " + ", ".join(f"`{n}` → {entries[n]}" for n in changed))
                else:
                    lines.append(f"- **{distro}**: already there")
            if not git("status", "--porcelain", cwd=work).stdout.strip():
                return Result(f"{title}: already up to date", "\n".join(lines))
            git("add", "distros", cwd=work)
            git("-c", "user.name=robostack-bot",
                "-c", "user.email=41898282+github-actions[bot]@users.noreply.github.com",
                "commit", "-q", "-m", "Rebuild the dependents the ABI check lists", cwd=work)
            if (push := git("push", "-q", "origin", f"HEAD:refs/heads/{branch}", cwd=work)).returncode:
                return Result(f"{title}: can't push to `{branch}`", "\n".join([
                    f"robostack-bot couldn't push to `{branch}`:", "", f"```\n{tail(push.stderr, 10)}\n```", "",
                    "Please add these entries yourself:", "", *_snippets(rebuilds)]), ok=False)
            sha = git("rev-parse", "HEAD", cwd=work).stdout.strip()
        finally:
            git("worktree", "remove", "--force", str(work))
    return Result(f"{title}: pushed {sha[:10]} to `{branch}`",
                  "\n".join(["Build-number bumps from the ABI check's comment:", "", *lines]), changed=True)


VINCA_PIN = re.compile(r'(?m)^(vinca\s*=\s*\{\s*git\s*=\s*"(?P<url>[^"]+)"\s*,\s*rev\s*=\s*")(?P<rev>[0-9a-f]{7,40})"')
# the branch that is followed per vinca repository (others: their default branch)
VINCA_BRANCHES = {"Tobias-Fischer/vinca": "robostack-integration"}
VINCA_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$")


def vinca_pin(text: str) -> tuple[str, str]:
    """(owner/repo, rev) of the vinca pin in pixi.toml."""
    m = VINCA_PIN.search(text)
    if not m:
        raise SystemExit('pixi.toml: no `vinca = { git = "https://github.com/...", rev = "..." }` pin')
    return re.sub(r"^https://github\.com/|\.git$|/$", "", m["url"]), m["rev"]


def set_vinca_pin(text: str, rev: str) -> str:
    return VINCA_PIN.sub(lambda m: f'{m[1]}{rev}"', text, count=1)


def _github_api(path: str):
    import urllib.request

    headers = {"User-Agent": "robostack-bot", "Accept": "application/vnd.github+json"}
    if token := os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(f"https://api.github.com/{path}", headers=headers)
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def vinca_changes(repo: str, old: str, new: str, compare: dict) -> list[str]:
    """The compare link and one line per vinca commit between the pins."""
    url = f"https://github.com/{repo}/compare/{old[:12]}...{new[:12]}"
    status = compare.get("status", "ahead")
    lines = [f"[{repo} `{old[:10]}...{new[:10]}`]({url})"
             + (f" ({status}: the new pin is not a descendant of the old one)" if status != "ahead" else ""), ""]
    commits = compare.get("commits") or []
    for c in commits:
        message = (c["commit"]["message"].splitlines() or [""])[0]
        lines.append(f"- [`{c['sha'][:7]}`]({c['html_url']}) {message}")
    if (total := compare.get("total_commits", len(commits))) > len(commits):
        lines.append(f"- … and {total - len(commits)} more, see the link")
    return lines


def update_vinca(ref: str | None = None) -> Result:
    """Move the vinca pin in pixi.toml to the newest commit of its branch (or of ref),
    then lock, re-render the pinning and run the repository checks."""
    pixi = rs.ROOT / "pixi.toml"
    text = pixi.read_text()
    repo, old = vinca_pin(text)
    if ref and not VINCA_REF.match(ref):
        return Result(f"update-vinca: invalid ref {ref!r}", "Give a branch, tag or commit of vinca.", ok=False)
    branch = ref or VINCA_BRANCHES.get(repo) or _github_api(f"repos/{repo}")["default_branch"]
    new = _github_api(f"repos/{repo}/commits/{branch}")["sha"]
    if new.startswith(old):
        return Result("vinca is up to date", f"vinca is pinned to `{old[:10]}`, the newest commit of {repo} `{branch}`.")
    compare = _github_api(f"repos/{repo}/compare/{old}...{new}")
    pixi.write_text(set_vinca_pin(text, new))
    lines = [f"Moves the vinca pin in `pixi.toml` to the newest commit of {repo} `{branch}`:", "",
             *vinca_changes(repo, old, new, compare), ""]
    print("pixi lock", flush=True)
    lock = subprocess.run(["pixi", "lock"], cwd=rs.ROOT, capture_output=True, text=True)
    if lock.returncode:
        return Result("update-vinca: pixi lock failed", f"```\n{tail(lock.stdout + lock.stderr, 40)}\n```", ok=False)
    # the new vinca may render the pinning differently
    before = {p: p.read_text() for p in rs.DISTROS.glob("*/conda_build_config.yaml")}
    render = subprocess.run(["pixi", "run", "render-pinning"], cwd=rs.ROOT, capture_output=True, text=True)
    rendered = sorted(str(p.relative_to(rs.ROOT)) for p, t in before.items() if p.read_text() != t)
    if render.returncode:
        lines.append(f"❌ `pixi run render-pinning` failed:\n\n```\n{tail(render.stdout + render.stderr, 30)}\n```")
    elif rendered:
        lines.append("The new vinca renders the pinning differently: " + ", ".join(f"`{p}`" for p in rendered))
    print("pixi run rs check", flush=True)
    checked = subprocess.run(["pixi", "run", "rs", "check"], cwd=rs.ROOT, capture_output=True, text=True)
    lines += ["", "`pixi run rs check`: " + ("✅ ok" if checked.returncode == 0 else
                                            f"❌ problems:\n\n```\n{tail(checked.stdout + checked.stderr, 40)}\n```")]
    lines += ["", NO_BUILD_NOTE.format(what="a new vinca regenerates the recipes of every distribution")]
    return Result(f"Update vinca to {new[:10]}", "\n".join(lines), changed=True, labels=[rs.NO_BUILD_LABEL])


def held_back(manifest: dict) -> list[str]:
    """The dependencies update-pixi leaves alone: Python (a new minor version comes
    before builds of everything the tooling needs, so it is moved by hand), and every
    requirement that is only an upper bound, like `cmake = "<4.0"` (a deliberate cap)."""
    tables = [manifest.get("dependencies", {})]
    tables += [target.get("dependencies", {}) for target in manifest.get("target", {}).values()]
    held = {"python"}
    for table in tables:
        for name, spec in table.items():
            version = spec if isinstance(spec, str) else spec.get("version", "")
            if version.strip().startswith("<"):
                held.add(name)
    return sorted(held)


def pixi_upgrade_args() -> list[str]:
    """`pixi upgrade` of everything except held_back()."""
    import tomllib

    held = held_back(tomllib.loads((rs.ROOT / "pixi.toml").read_text()))
    return ["pixi", "upgrade", "--json", *(a for name in held for a in ("--exclude", name))]


def update_pixi(changes: Path | None = None) -> Result:
    """`pixi upgrade`: the newest versions of the tooling's dependencies in pixi.toml and
    pixi.lock (except held_back()), with pixi-diff-to-markdown's table of the changes,
    then the repository checks. vinca's pin is update-vinca's.

    changes: the output of an earlier `pixi upgrade --json` (`rs update-pixi --excludes`
    gives its arguments). The bot upgrades before `pixi run`, so that the environment
    isn't reinstalled under the running command; without it, this runs `pixi upgrade`."""
    import tomllib

    held = held_back(tomllib.loads((rs.ROOT / "pixi.toml").read_text()))
    if changes is not None:
        upgrade = subprocess.CompletedProcess([], 0, stdout=changes.read_text(), stderr="")
    else:
        print("pixi upgrade", flush=True)
        upgrade = subprocess.run(pixi_upgrade_args(), cwd=rs.ROOT, capture_output=True, text=True)
    if upgrade.returncode:
        return Result("update-pixi: pixi upgrade failed", f"```\n{tail(upgrade.stdout + upgrade.stderr, 40)}\n```", ok=False)
    kept = ", ".join(f"`{name}`" for name in held)
    if subprocess.run(["git", "diff", "--quiet", "--", "pixi.toml", "pixi.lock"], cwd=rs.ROOT).returncode == 0:
        return Result("pixi dependencies are up to date", f"`pixi upgrade` found nothing newer (held back: {kept}).")
    table = subprocess.run(["pixi", "exec", "pixi-diff-to-markdown"], input=upgrade.stdout,
                           cwd=rs.ROOT, capture_output=True, text=True)
    changes = table.stdout.strip() if table.returncode == 0 and table.stdout.strip() else (
        f"```json\n{tail(upgrade.stdout, 80)}\n```")
    lines = [f"`pixi upgrade`: the newest versions of the tooling's dependencies in `pixi.toml` and `pixi.lock`. "
             f"Held back: {kept} (Python is moved by hand, the others are capped on purpose); "
             "the vinca pin is `update-vinca`'s.", "", changes, ""]
    print("pixi run rs check", flush=True)
    checked = subprocess.run(["pixi", "run", "rs", "check"], cwd=rs.ROOT, capture_output=True, text=True)
    lines.append("`pixi run rs check`: " + ("✅ ok" if checked.returncode == 0 else
                                            f"❌ problems:\n\n```\n{tail(checked.stdout + checked.stderr, 40)}\n```"))
    lines += ["", NO_BUILD_NOTE.format(what="the tooling's dependencies; a new rattler-build is worth a build")]
    return Result("Upgrade pixi dependencies", "\n".join(lines), changed=True, labels=[rs.NO_BUILD_LABEL])


REPORT_LABEL = "dependency-report"
REPORT_MARKER = "<!-- robostack-dependency-report -->"
MAX_ISSUE_BODY = 60000  # GitHub's limit is 65536 characters


def migration_status(output: str) -> str:
    """The conda-forge migration section of check_dependency_compat.py's output."""
    m = re.search(r"(?ms)^conda-forge migration status.*?(?=^Legend:|\Z)", output)
    return m.group(0).strip() if m else ""


def report_issue_body(distro: str, data: dict, migrations: str = "", run_url: str = "") -> str:
    """The dependency-report issue of a distribution, from check_dependency_compat.py's
    --json output (pin_conflicts, conflicts, notes) and its migration status."""
    conflicts, pins, notes = data.get("conflicts") or {}, data.get("pin_conflicts") or {}, data.get("notes") or {}
    count = len(conflicts) + len(pins)
    lines = [
        REPORT_MARKER,
        f"Weekly dependency check of **{distro}** (linux-64, `pixi run rs {distro} check-deps`): can every non-ROS "
        f"dependency of its packages be installed together with the pins of `distros/{distro}/conda_build_config.yaml`? "
        "robostack-bot updates this issue every week and closes it when nothing conflicts.",
        "",
        f"**{count} conflict{'s' if count != 1 else ''}**" if count else "✅ **No conflicts.**",
        "",
    ]
    if pins:
        lines += ["Mutex constraints that contradict the rendered pins:", ""]
        lines += [f"- `{info.get('mutex')}` vs `{info.get('variant')}`" for info in pins.values()] + [""]
    if conflicts:
        lines += ["| dependency | needed by | clashes with |", "|---|---|---|"]
        for name, info in sorted(conflicts.items()):
            recipes = info.get("recipes") or []
            needed = f"{len(recipes)}: " + ", ".join(recipes[:5]) + (" …" if len(recipes) > 5 else "")
            specs = ", ".join(s for s in info.get("specs") or [] if s != name)
            lines.append(f"| `{name}`{f' ({specs})' if specs else ''} | {needed} | "
                         f"{', '.join(f'`{p}`' for p in info.get('pins') or []) or 'not attributed'} |")
        lines.append("")
    if notes:
        lines += ["Notes (run requirements built for a newer glibc than the build floor: they limit the systems "
                  "they install on, but don't conflict with the pins): " + ", ".join(f"`{n}`" for n in sorted(notes)), ""]
    if migrations:
        lines += [*_details("conda-forge migration status", ["```text", migrations, "```"]), ""]
    if conflicts:
        explanations = []
        for name, info in sorted(conflicts.items()):
            text = "\n".join((info.get("explanation") or "").splitlines()[:30])
            explanations += [f"**{name}**", "", "```text", text, "```", ""]
        lines += [*_details("Solver explanations", explanations), ""]
    if run_url:
        lines.append(f"[Workflow run]({run_url})")
    body = "\n".join(lines)
    if len(body) > MAX_ISSUE_BODY:
        body = body[:MAX_ISSUE_BODY] + "\n\n… (truncated, see the workflow run)"
    return body


def publish_issue(repo: str, title: str, label: str, body: str, open_: bool) -> str | None:
    """Create or update the issue with this title and label: open while open_ (e.g.
    there are conflicts), closed otherwise. Returns its URL (None: nothing to report
    and no issue to close)."""
    query = f'.[] | select(.title == {json.dumps(title)}) | {{number, state, html_url}} | @json'
    out = _gh(["api", "--paginate", f"repos/{repo}/issues?labels={label}&state=all&per_page=100", "--jq", query])
    # the open one, else the newest
    issues = sorted((json.loads(l) for l in out.splitlines() if l.strip()),
                    key=lambda i: (i["state"] == "open", i["number"]))
    if not issues and not open_:
        return None
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as fh:
        fh.write(body)
    try:
        if issues:
            issue = issues[-1]
            _gh(["api", "-X", "PATCH", f"repos/{repo}/issues/{issue['number']}", "-F", f"body=@{fh.name}",
                 "-f", f"state={'open' if open_ else 'closed'}"])
            return issue["html_url"]
        created = _gh(["api", f"repos/{repo}/issues", "-f", f"title={title}", "-F", f"body=@{fh.name}",
                       "-f", f"labels[]={label}"])
        return json.loads(created)["html_url"]
    finally:
        os.unlink(fh.name)


def publish_report(repo: str, distro: str, body: str, conflicts: bool) -> str | None:
    """Create or update the distribution's dependency-report issue: open while there
    are conflicts, closed otherwise. Returns its URL."""
    return publish_issue(repo, f"{distro}: dependency report", REPORT_LABEL, body, conflicts)


def dependency_report(distro: str) -> Result:
    """Check the distribution's dependencies against its pins (linux-64) and keep its
    dependency-report issue up to date (in GitHub Actions; locally only the report)."""
    work = rs.prepare(distro)
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "conflicts.json"
        proc = subprocess.run([sys.executable, str(rs.TOOLS / "check_dependency_compat.py"), "--platform", "linux-64",
                               "--json", str(out)], cwd=work, capture_output=True, text=True)
        data = json.loads(out.read_text()) if out.is_file() else {}
    print(proc.stdout + proc.stderr, flush=True)
    if proc.returncode not in (0, 1) or (proc.returncode == 1 and not data):
        return Result(f"{distro}: dependency check failed", f"```\n{tail(proc.stdout + proc.stderr, 40)}\n```", ok=False)
    count = len(data.get("conflicts") or {}) + len(data.get("pin_conflicts") or {})
    run_url = ""
    if run_id := os.environ.get("GITHUB_RUN_ID"):
        run_url = f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{os.environ.get('GITHUB_REPOSITORY')}/actions/runs/{run_id}"
    body = report_issue_body(distro, data, migration_status(proc.stdout), run_url)
    title = f"{distro}: {count} dependency conflict{'s' if count != 1 else ''}" if count else f"{distro}: no dependency conflicts"
    if not os.environ.get("GITHUB_ACTIONS"):
        return Result(title, body)
    url = publish_report(_repository(), distro, body, bool(count))
    return Result(title, f"Report: {url}" if url else "No conflicts, and no report issue to close.")


def update_build_timings() -> Result:
    """Merge the build times of the last week's build-branch runs into shared/build_timings/."""
    import build_timings

    before = {p: p.read_text() for p in build_timings.TIMINGS.glob("*.json")}
    count = build_timings.collect()
    changed = sorted(p.name for p in build_timings.TIMINGS.glob("*.json") if before.get(p) != p.read_text())
    if not changed:
        return Result("Build times are up to date", "No new build times in the last week's build-branch runs.")
    return Result("Update the build times", f"{count} build times from the last week's build-branch runs, in "
                  + ", ".join(f"`shared/build_timings/{name}`" for name in changed)
                  + ". `rs <distro> gha` packs each stage's recipes into jobs by them.\n\n"
                  + NO_BUILD_NOTE.format(what="the times only change how the build jobs are packed"),
                  changed=True, labels=[rs.NO_BUILD_LABEL])


def _default_ci_yaml() -> str:
    return (rs.TOOLS / "ci_default.yaml").read_text()


def parse_command(body: str, association: str, pull_request: str = "") -> list[dict]:
    """What a comment or issue asks for:

    - `@robostack-bot <command> [<distro>... | all]`, `@robostack-bot add-package <pkg>... [<distro>...]`,
      `@robostack-bot rebuild <pkg>... [<distro>...] [--with-dependents]`, `@robostack-bot update-vinca [<ref>]`,
      `@robostack-bot rebuild-dependents` (in a comment on the pull request, its number in pull_request)
    - the "robostack-bot command" and "Package request" issue forms

    Returns the jobs to run, each {"command", "distro", "args", "preview"} (one per
    distribution for the per-distribution commands). Anyone can request packages
    (add-package opens the PR for them too, maintainers review and merge it); the
    other commands are for owners, members and collaborators.
    """
    body = body or ""
    maintainer = association.upper() in ALLOWED_ASSOCIATIONS
    command, rest = "", ""
    if m := re.search(r"^\s*@robostack-bot,?\s+(?:please\s+)?([a-z-]+)([^\n]*)", body, re.I | re.M):
        command, rest = m.group(1).lower(), m.group(2)
    else:
        form = _form_fields(body)
        if form.get("package names"):
            command, rest = "add-package", form["package names"]
        elif form.get("command"):
            command, rest = form["command"].lower(), form.get("packages", "")
        rest += " " + form.get("distributions", "")
    if command not in BOT_COMMANDS:
        return []
    known = rs.distros()
    words = [w for w in re.split(r"[\s,]+", rest.strip()) if w and w.lower() not in ("_no", "response_", "none")]
    distros = [w.lower() for w in words if w.lower() in known]
    if any(w.lower() == "all" for w in words):
        distros = []  # every distribution
    if command == "add-package":
        packages = [w for w in words if w.lower() not in known and w.lower() != "all"]
        packages = [p for p in packages if PACKAGE_NAME.match(_package_name(p))][:MAX_PACKAGES]
        if not packages:
            return []
        return [{"command": command, "distro": "", "args": " ".join(packages + distros), "preview": False}]
    if not maintainer:
        return []
    if command == "rebuild":
        packages = [_package_name(w) for w in words if w.lower() not in known and w.lower() != "all"
                    and not w.startswith("--")]
        packages = list(dict.fromkeys(p for p in packages if PACKAGE_NAME.match(p)))[:MAX_PACKAGES]
        flags = ["--with-dependents"] if any(w.lower() == "--with-dependents" for w in words) else []
        if not packages:
            return []
        return [{"command": command, "distro": "", "args": " ".join(packages + distros + flags), "preview": False}]
    if command == "rebuild-dependents":
        if not pull_request.isdigit():
            return []
        return [{"command": command, "distro": "", "args": pull_request, "preview": False}]
    if command == "update-vinca":
        refs = [w for w in words if w.lower() not in known and w.lower() != "all"]
        if refs and not VINCA_REF.match(refs[0]):
            return []
        return [{"command": command, "distro": "", "args": refs[0] if refs else "", "preview": False}]
    if command in PER_DISTRO_COMMANDS:
        return [{"command": command, "distro": d, "args": "", "preview": False} for d in distros or known]
    return [{"command": command, "distro": "", "args": "", "preview": False}]


def _form_fields(body: str) -> dict[str, str]:
    """Fields of an issue form: `### Label` headings, each followed by its value."""
    fields: dict[str, str] = {}
    for m in re.finditer(r"(?m)^###\s*(.+?)\s*\n([\s\S]*?)(?=^###|\Z)", body):
        label, value = m.group(1).strip().lower(), m.group(2).strip()
        label = {"package name": "package names", "distribution": "distributions"}.get(label, label)
        checked = re.findall(r"- \[[xX]\]\s*(\S+)", value)
        fields[label] = " ".join(checked) if re.search(r"- \[[ xX]\]", value) else value
    return fields


def mutex_problems(vinca: dict) -> list[str]:
    """The mutex only keeps one distribution per environment: library versions come
    from the packages' own metadata (see shared/vinca.yaml), not from run_constraints."""
    if (vinca.get("mutex_package") or {}).get("run_constraints"):
        return ["mutex_package.run_constraints must stay empty; fix the dependencies of the affected "
                "packages instead (see shared/vinca.yaml)"]
    return []


PATCH_NAME = re.compile(r"^ros2-(?P<package>[a-z0-9-]+)(\.(osx|linux|win|unix|emscripten))?\.patch$")


def unmatched_patches(patches: list[str], packages: set[str]) -> list[str]:
    """Patches named after none of `packages`: vinca only applies ros2-<package>[.<platform>].patch,
    so a misnamed patch (or one for a package that left the distribution) silently does nothing."""
    names = {p.replace("_", "-") for p in packages}
    return [p for p in patches if (m := PATCH_NAME.match(p)) and m["package"] not in names]


def check() -> Result:
    """Sanity checks: every distribution assembles and its generated files are consistent."""
    problems: list[str] = []
    all_packages: set[str] = set()
    known = {"channel_name", "read_from", "upload_to", "conda_forge_pinning_version", "conda_forge_migrations", "pinning_overrides",
             "rosdistro_sync"}
    for distro in rs.distros():
        d = rs.DISTROS / distro
        settings = rs.settings(distro)
        if unknown := set(settings) - known:
            problems.append(f"{distro}: unknown keys in distro.yaml: {sorted(unknown)}")
        if settings.get("read_from", "prefix") not in ("prefix", "anaconda"):
            problems.append(f"{distro}: read_from must be prefix or anaconda")
        upload_to = settings.get("upload_to", list(rs.UPLOAD_HOSTS))
        if not upload_to or not set(upload_to) <= set(rs.UPLOAD_HOSTS):
            problems.append(f"{distro}: upload_to must list prefix and/or anaconda")
        sync = settings.get("rosdistro_sync")
        if sync is not None and sync != "manual" and not ((m := SYNC_TAG.match(str(sync))) and m["distro"] == distro):
            problems.append(f"{distro}: rosdistro_sync must be 'manual' or a sync tag like {distro}/2026-10-05")
        # an empty migration list is valid
        if ("conda_forge_pinning_version" in settings) != ("conda_forge_migrations" in settings):
            problems.append(f"{distro}: set conda_forge_pinning_version and conda_forge_migrations together")
        try:
            vinca = rs.read_vinca(distro, "linux-64")
        except Exception as error:  # noqa: BLE001 - reported as a problem
            problems.append(f"{distro}: vinca can't read its configuration: {error}")
            continue
        if vinca.get("ros_distro") != distro:
            problems.append(f"{distro}: vinca.yaml has ros_distro {vinca.get('ros_distro')!r}")
        # build_number 0 is valid (new-distro starts there)
        if not isinstance(vinca.get("build_number"), int) or vinca["build_number"] < 0:
            problems.append(f"{distro}: vinca.yaml needs a build_number >= 0")
        for key in ("mutex_package", "packages_select_by_deps"):
            if not vinca.get(key):
                problems.append(f"{distro}: vinca.yaml has no {key}")
        problems += [f"{distro}: {p}" for p in mutex_problems(vinca)]
        patches = sorted(p.name for p in (d / "patch").glob("*.patch"))
        for patch in patches:
            if not PATCH_NAME.match(patch):
                problems.append(f"{distro}: patch {patch} should be named ros2-<package>[.<platform>].patch")
        packages = _available(distro)
        all_packages |= packages
        for patch in unmatched_patches(patches, packages):
            problems.append(f"{distro}: patch {patch} matches no package of the distribution, so it is never applied")
        rendered = d / "conda_build_config.yaml"
        before = rendered.read_text() if rendered.is_file() else ""
        if rs.task(distro, "render-pinning", []) or rendered.read_text() != before:
            problems.append(f"{distro}: conda_build_config.yaml is out of date (pixi run rs {distro} render-pinning)")
            rendered.write_text(before)
    # a shared patch applies to every distribution that has the package
    shared = sorted(p.name for p in (rs.SHARED / "patch").glob("*.patch"))
    for patch in unmatched_patches(shared, all_packages):
        problems.append(f"shared: patch {patch} matches no package of any distribution, so it is never applied")
    # file names: .yaml everywhere (GitHub requires FUNDING.yml by that name)
    for yml in sorted(p for p in (rs.ROOT / ".github").rglob("*.yml") if p.name != "FUNDING.yml"):
        problems.append(f"{yml.relative_to(rs.ROOT)}: use the .yaml extension")
    for wf in sorted((rs.ROOT / ".github" / "workflows").glob("*.yaml")):
        text = wf.read_text()
        if "\non:" not in "\n" + text or "\ntrue:" in "\n" + text:
            problems.append(f"{wf.name}: no top-level `on:` trigger")
    sort = subprocess.run(["pixi", "run", "sort", "--check"], cwd=rs.ROOT, capture_output=True, text=True)
    if sort.returncode:
        problems.append("YAML files are not sorted (pixi run sort):\n" + tail(sort.stdout + sort.stderr, 10))
    ok = not problems
    summary = "All distributions assemble; generated files are up to date." if ok else "\n".join(f"- {p}" for p in problems)
    return Result("check: ok" if ok else "check: problems found", summary, ok=ok)


# --------------------------------------------------------------------------- #
def report(result: Result, summary_file: str | None) -> int:
    print(f"# {result.title}\n\n{result.summary}")
    if summary_file:
        Path(summary_file).write_text(result.summary + "\n\n🤖 robostack-bot\n")
    if out := os.environ.get("GITHUB_OUTPUT"):
        with open(out, "a") as fh:
            fh.write(f"title={result.title}\nchanged={str(result.changed).lower()}\nok={str(result.ok).lower()}\n")
            fh.write(f"labels={','.join(result.labels)}\n")
    return 0 if result.ok else 1


def main(command: str, argv: list[str], distro: str | None = None) -> int:
    parser = argparse.ArgumentParser(prog=f"rs {command}")
    parser.add_argument("--summary")
    if command == "new-distro":
        parser.add_argument("name")
        parser.add_argument("--from", dest="source", required=True)
    if command == "parse-command":
        # the text comes from the environment: pixi's task shell would re-parse quotes in it
        parser.add_argument("--body", default=os.environ.get("ROBOSTACK_BOT_BODY", ""))
        parser.add_argument("--association", default=os.environ.get("ROBOSTACK_BOT_ASSOCIATION", ""))
        parser.add_argument("--pull-request", default=os.environ.get("ROBOSTACK_BOT_PULL_REQUEST", ""),
                            help="number of the pull request the comment is on")
    if command == "add-package":
        parser.add_argument("names", nargs="+", help="ROS packages, optionally followed by distributions")
        parser.add_argument("--preview", action="store_true", help="don't change files, only report")
    if command == "rebuild":
        parser.add_argument("names", nargs="+", help="ROS packages, optionally followed by distributions")
        parser.add_argument("--with-dependents", action="store_true", help="also every package depending on them")
    if command == "rebuild-dependents":
        parser.add_argument("pull_request")
    if command == "update-pixi":
        parser.add_argument("--changes", type=Path, help="output of an earlier `pixi upgrade --json`")
        parser.add_argument("--excludes", action="store_true", help="print the --exclude arguments for pixi upgrade")
    if command == "update-vinca":
        parser.add_argument("ref", nargs="?", help="vinca branch, tag or commit (default: the pinned branch)")
    args = parser.parse_args(argv)
    if command == "parse-command":
        print(json.dumps(parse_command(args.body, args.association, args.pull_request)))
        return 0
    if command == "rebuild":
        known = set(rs.distros())
        distros = [n for n in args.names if n in known]
        packages = [n for n in args.names if n not in known]
        return report(rebuild_packages(packages, distros, args.with_dependents), args.summary)
    if command == "rebuild-dependents":
        return report(rebuild_dependents(args.pull_request), args.summary)
    if command == "update-vinca":
        return report(update_vinca(args.ref), args.summary)
    if command == "dependency-report":
        return report(dependency_report(distro), args.summary)
    if command == "update-build-timings":
        return report(update_build_timings(), args.summary)
    if command == "update-pixi":
        if args.excludes:
            print(" ".join(pixi_upgrade_args()[3:]))
            return 0
        return report(update_pixi(args.changes), args.summary)
    if command == "add-package":
        known = set(rs.distros())
        distros = [n for n in args.names if n in known]
        packages = [n for n in args.names if n not in known]
        return report(add_package(packages, distros, preview=args.preview), args.summary)
    if command == "check":
        return report(check(), args.summary)
    if command == "rosdistro-syncs":
        print(json.dumps(rosdistro_syncs()))
        return 0
    if command == "update-pinning":
        if distro:
            return report(update_pinning(distro), args.summary)
        own = [d for d in rs.distros() if not uses_shared_pinning(d)]
        results = ([update_shared_pinning()] if len(own) < len(rs.distros()) else []) + [update_pinning(d) for d in own]
        return report(Result("Update conda-forge pinning", "\n\n---\n\n".join(r.summary for r in results),
                             changed=any(r.changed for r in results), ok=all(r.ok for r in results),
                             labels=sorted({label for r in results for label in r.labels})), args.summary)
    if command == "new-distro":
        return report(new_distro(args.name, args.source), args.summary)
    if command == "update-snapshot":
        return report(update_snapshot(distro), args.summary)
    if command == "find-stale":
        report(find_stale(distro), args.summary)
        return 0  # findings, not a failure
    raise SystemExit(f"unknown command {command}")
