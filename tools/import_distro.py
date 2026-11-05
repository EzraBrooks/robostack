"""(Re)import a distribution from its own repository (RoboStack/ros-<distro>).

    pixi run python tools/import_distro.py ../ros-jazzy [--ref origin/main] [--distro jazzy]

Used to move a distribution into this repository, and to pick up changes made in
its old repository until it is archived. It copies the distribution's data into
distros/<distro>/ and leaves out what shared/ already provides:

- vinca.yaml               extending shared/vinca.yaml, without its package selections,
                           with this repository's paths (patch_dir, snapshot paths;
                           skip_existing is derived); comments are kept
- pkg_additional_info.yaml, patch/dependencies.yaml
                           without entries identical to shared/ (build numbers stay);
                           ros-<distro>-<pkg> dependency names become ros2-<pkg>
- patch/                   patches renamed to ros2-<pkg>[.<platform>].patch
- rosdistro_snapshot.yaml, rosdistro_additional_recipes.yaml   as they are
- distro.yaml              channel and where builds read it from (from its pixi.toml), its
                           conda-forge pinning version, migrations and the pins that
                           differ from shared/pinning/overrides.yaml (vinca_pinning.yaml)
- ci.yaml                  reset to the defaults

Afterwards: `pixi run rs <distro> render-pinning`, `pixi run sort`, `pixi run rs check`.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import yaml

import robostack as rs

DERIVED_KEYS = ("conda_index", "patch_dir", "rosdistro_snapshot", "rosdistro_additional_recipes", "skip_existing")


def _norm(x):
    if isinstance(x, str):
        return x.replace("-", "_")
    if isinstance(x, dict):
        return {k: _norm(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_norm(v) for v in x]
    return x


def _key(x) -> str:
    return json.dumps(_norm(json.loads(json.dumps(x))), sort_keys=True)


def _entries(items) -> set[tuple[str, str | None]]:
    out = set()
    for item in items or []:
        if isinstance(item, str):
            out.add((item.replace("-", "_"), None))
        else:
            out |= {(n.replace("-", "_"), str(item["if"])) for n in item.get("then") or []}
    return out


def _keep(items, wanted: set) -> list:
    """The entries of a package list (plain names / if-blocks) that are in wanted."""
    out = []
    for item in items or []:
        if isinstance(item, str):
            if (item.replace("-", "_"), None) in wanted:
                out.append(item)
        else:
            then = [n for n in item.get("then") or [] if (n.replace("-", "_"), str(item["if"])) in wanted]
            if then:
                item["then"] = then
                for extra in [k for k in item if k not in ("if", "then")]:
                    del item[extra]
                out.append(item)
    return out


def _convert_exclusions(vinca) -> None:
    """vinca's packages_skip_by_deps / packages_remove_from_deps -> packages_exclude
    (in the remove list) and packages_skip (skip only)."""
    skip = vinca.pop("packages_skip_by_deps", None) or []
    remove = vinca.pop("packages_remove_from_deps", None) or []
    skip_only = _entries(skip) - _entries(remove)
    if remove:
        vinca["packages_exclude"] = remove
    if skip_only:
        vinca["packages_skip"] = _keep(skip, skip_only)


class Source:
    """Files of the old repository, from a working tree or a git ref."""

    def __init__(self, repo: Path, ref: str | None):
        self.repo, self.ref = repo, ref

    def text(self, path: str) -> str | None:
        if self.ref is None:
            p = self.repo / path
            return p.read_text() if p.is_file() else None
        out = subprocess.run(["git", "-C", str(self.repo), "show", f"{self.ref}:{path}"], capture_output=True, text=True)
        return out.stdout if out.returncode == 0 else None

    def copy_dir(self, path: str, dest: Path) -> None:
        if self.ref is None:
            shutil.copytree(self.repo / path, dest / path, dirs_exist_ok=True)
            return
        data = subprocess.run(["git", "-C", str(self.repo), "archive", self.ref, path], capture_output=True, check=True).stdout
        tarfile.open(fileobj=io.BytesIO(data)).extractall(dest, filter="data")


def import_distro(src: Source, distro: str | None) -> str:
    ry = rs.ruamel_yaml()
    vinca = ry.load(src.text("vinca.yaml"))
    # the old repository's names (ros-<source>-*) follow its distribution, also when
    # --distro imports it under another name
    source = vinca["ros_distro"]
    distro = distro or source
    dest = rs.DISTROS / distro
    dest.mkdir(parents=True, exist_ok=True)

    # vinca.yaml: drop what shared/ and the tooling provide
    shared = yaml.safe_load((rs.SHARED / "vinca.yaml").read_text()) or {}
    for key in DERIVED_KEYS:
        vinca.pop(key, None)
    # on top of shared/vinca.yaml, with its own patches and snapshot
    for i, (key, value) in enumerate([
        ("extends", "../../shared/vinca.yaml"),
        ("ros_distro", vinca.get("ros_distro", distro)),
        ("patch_dir", "patch"),
        ("rosdistro_snapshot", "rosdistro_snapshot.yaml"),
        ("rosdistro_additional_recipes", "rosdistro_additional_recipes.yaml"),
    ]):
        vinca.pop(key, None)
        vinca.insert(i, key, value)
    mode = vinca.get("package_name_mode", "legacy")
    if mode == shared.get("package_name_mode"):
        vinca.pop("package_name_mode", None)
    elif "package_name_mode" not in vinca:
        vinca.insert(5, "package_name_mode", mode)
    _convert_exclusions(vinca)
    for key in rs.LIST_KEYS:
        common = {_key(i) for i in shared.get(key) or []}
        items = vinca.get(key)
        if items is None:
            continue
        for i in reversed(range(len(items))):
            if _key(items[i]) in common:
                del items[i]
    with (dest / "vinca.yaml").open("w") as fh:
        ry.dump(vinca, fh)

    # as they are (patch/dependencies.yaml is filtered below)
    shutil.rmtree(dest / "patch", ignore_errors=True)
    src.copy_dir("patch", dest)
    for patch in (dest / "patch").glob(f"ros-{source}-*.patch"):
        patch.rename(patch.with_name("ros2-" + patch.name.removeprefix(f"ros-{source}-")))
    # a file the old repository no longer has goes too (re-imports mirror it)
    for name in ("rosdistro_snapshot.yaml", "rosdistro_additional_recipes.yaml"):
        if (text := src.text(name)) is not None:
            (dest / name).write_text(text)
        else:
            (dest / name).unlink(missing_ok=True)

    # per-package files: drop entries identical to shared/ (keeping build numbers)
    for name, shared_file, target in (
        ("pkg_additional_info.yaml", "pkg_additional_info.yaml", dest / "pkg_additional_info.yaml"),
        ("patch/dependencies.yaml", "patch/dependencies.yaml", dest / "patch" / "dependencies.yaml"),
    ):
        text = src.text(name)
        if text is None:
            target.unlink(missing_ok=True)
            continue
        text = re.sub(rf"\bros-{source}-(?=[a-z0-9])", "ros2-", text)
        data = ry.load(text) or {}
        common = yaml.safe_load((rs.SHARED / shared_file).read_text()) or {}
        for key in list(data):
            if key not in common:
                continue
            entry = json.loads(json.dumps(data[key]))
            rest = {k: v for k, v in entry.items() if k != "build_number"} if isinstance(entry, dict) else entry
            if rest == common[key]:
                if isinstance(entry, dict) and "build_number" in entry:
                    for k in [k for k in data[key] if k != "build_number"]:
                        del data[key][k]
                else:
                    del data[key]
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w") as fh:
            ry.dump(data, fh)

    # distro.yaml
    pixi = src.text("pixi.toml") or ""
    settings: dict = {}
    if m := re.search(r'upload\s*=\s*"rattler-build upload prefix -c (\S+)', pixi):
        settings.update(channel_name=m.group(1), read_from="prefix")
    elif m := re.search(r'upload\s*=\s*"rattler-build upload anaconda -o (\S+)', pixi):
        settings.update(channel_name=m.group(1), read_from="anaconda")
    pinning = yaml.safe_load(src.text("vinca_pinning.yaml") or "") or {}
    if pinning.get("conda_forge_pinning_version"):
        settings["conda_forge_pinning_version"] = str(pinning["conda_forge_pinning_version"])
        settings["conda_forge_migrations"] = list(pinning.get("migrations") or [])
        shared_overrides = yaml.safe_load((rs.SHARED / "pinning" / "overrides.yaml").read_text())["pinning_overrides"]
        own = {k: v for k, v in (pinning.get("pinning_overrides") or {}).items()
               if k not in shared_overrides or shared_overrides[k] != v}
        if own:
            settings["pinning_overrides"] = own
    (dest / "distro.yaml").write_text(
        f"# Settings of ros-{distro} that differ from the other distributions\n"
        "# (everything else is shared, see the README).\n"
        + yaml.safe_dump(settings, sort_keys=False, default_flow_style=None, width=100)
    )
    # ci.yaml holds the distribution's current rebuild controls: keep them on re-imports
    if not (dest / "ci.yaml").exists():
        (dest / "ci.yaml").write_text((rs.ROOT / "tools" / "ci_default.yaml").read_text())
    return distro


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("repo", type=Path, help="checkout of RoboStack/ros-<distro>")
    parser.add_argument("--ref", help="git ref to import (default: the working tree)")
    parser.add_argument("--distro", help="default: ros_distro of its vinca.yaml")
    args = parser.parse_args()
    distro = import_distro(Source(args.repo.resolve(), args.ref), args.distro)
    print(f"imported distros/{distro}; now run: pixi run rs {distro} render-pinning && pixi run sort && pixi run rs check")
    return 0


if __name__ == "__main__":
    sys.exit(main())
