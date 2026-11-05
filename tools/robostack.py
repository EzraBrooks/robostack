"""Run a RoboStack task for one distribution of this repository.

    pixi run rs <distro> <task> [args...]
    pixi run rs jazzy generate-recipes
    pixi run rs jazzy build-one ros2-ros-workspace
    pixi run rs changed-distros --base origin/main      # for CI (build:<distro> labels override)
    pixi run rs check | update-pinning | new-distro NAME --from DISTRO   (whole repository)
    pixi run rs <distro> update-pinning                                  (one distribution)

Every distribution lives in distros/<distro>/ and only holds what is specific to
it; everything else is in shared/. distros/<d>/vinca.yaml extends shared/vinca.yaml,
and vinca combines both and the files next to each (pkg_additional_info.yaml,
patch/ with dependencies.yaml, tests/, robostack.yaml). Tasks run in
distros/<distro>/work/ (generated, git-ignored): its vinca.yaml extends the
distribution's and adds the channel to skip published packages, and it holds
vinca_pinning.yaml, conda_build_config.yaml (rendered back into distros/<d>/ by
`render-pinning`), .scripts/ and the generated recipes/ and output/.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DISTROS = ROOT / "distros"
SHARED = ROOT / "shared"
TOOLS = ROOT / "tools"
CONDA_FORGE = "https://prefix.dev/conda-forge"

# Files whose change affects every distribution (anything outside distros/).
PER_DISTRO_PREFIX = "distros/"


def distros() -> list[str]:
    return sorted(p.name for p in DISTROS.iterdir() if (p / "vinca.yaml").is_file())


def settings(distro: str) -> dict:
    import yaml  # not needed for changed-distros, which runs without pixi in CI

    return yaml.safe_load((DISTROS / distro / "distro.yaml").read_text()) or {}


def channel_name(distro: str) -> str:
    return settings(distro).get("channel_name", f"robostack-{distro}")


def reads_from_prefix(distro: str) -> bool:
    """Whether builds read the distribution's published packages from prefix.dev (else
    anaconda.org). Uploads go to both (see upload)."""
    return settings(distro).get("read_from", "prefix") == "prefix"


def channel_url(distro: str) -> str:
    name = channel_name(distro)
    return f"https://prefix.dev/{name}" if reads_from_prefix(distro) else f"https://conda.anaconda.org/{name}"


def repodata_url(distro: str) -> str:
    """Base URL to read the channel's repodata from (repo.prefix.dev serves it directly,
    prefix.dev redirects)."""
    name = channel_name(distro)
    return f"https://repo.prefix.dev/{name}" if reads_from_prefix(distro) else f"https://conda.anaconda.org/{name}"


def run(cmd: list[str], cwd: Path) -> int:
    print("+", " ".join(cmd), f"  (in {cwd.relative_to(ROOT)})", flush=True)
    return subprocess.run(cmd, cwd=cwd).returncode


def run_tee(cmd: list[str], cwd: Path) -> tuple[int, bytes]:
    """run(), also returning the combined output."""
    print("+", " ".join(cmd), f"  (in {cwd.relative_to(ROOT)})", flush=True)
    output = bytearray()
    with subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT) as proc:
        for line in proc.stdout:
            sys.stdout.buffer.write(line)
            sys.stdout.buffer.flush()
            output += line
    return proc.returncode, bytes(output)


WORKFLOW_SIZE_MESSAGE = b"exceeding the 500 KiB limit"
MAX_BATCH_SIZE = 100


# prefix.dev's sharded repodata (what rattler reads) lists a new upload a few minutes
# after repodata.json does, so a stage that starts right after the previous one
# uploaded can find no candidates for a package that is on the channel.
# rattler words it "No candidates were found for X" or "…, for which no candidates were
# found" (wrapped over two lines): match its lowercase start, and the summary line
INDEX_LAG_MESSAGES = (b"no candidates were", b"there are no viable options")
INDEX_LAG_RETRIES = 2
INDEX_LAG_WAIT = 180
# Network failures of the runners (GitHub or prefix.dev unreachable for a moment) end
# a multi-hour build; packages already built stay in the output folder, so retrying
# continues where it stopped.
NETWORK_MESSAGES = (b"Git fetch failed", b"Failed to connect to github.com", b"error sending request for url",
                    b"stream closed because of a broken pipe", b"RPC failed; curl", b"Could not resolve host",
                    b"connection error", b"error decoding response body", b"error reading a body from connection",
                    # prefix.dev's sharded repodata answers 409 / 5xx now and then while a channel updates
                    b"HTTP status client error (409 Conflict)", b"HTTP status server error")
NETWORK_RETRIES = 4
NETWORK_WAIT = 60  # doubled on each retry: 1, 2, 4, 8 minutes


# a build job's recipes side by side, with a shared jobserver keeping them at the runner's
# cores in total; Windows has no jobserver, and was no faster with two (#154).
# Needs rattler-build >= 0.79: older versions give two variants of a recipe started in the
# same second the same build directory (prefix-dev/rattler-build#2898).
PARALLEL_BUILDS = "1" if sys.platform == "win32" else f"3:{os.cpu_count()}"


def build_ci_parallel(distro: str, recipes: list[str], args: list[str], cwd: Path) -> int:
    """build-ci for all of a build job's recipes in one rattler-build run, building
    independent ones in parallel. Its JSON log tells which recipe each line belongs to:
    printed as "HH:MM:SS [recipe] message", and each recipe's time (first to last line of
    its build) is recorded for packing the build jobs, scaled so that the times add up to
    the builds' wall time: overlapping builds share it. The compatibility recipes
    (ros-<distro>-<name>) only repackage, but wait for others while building alongside:
    they are left out (build_timings gives them ALIAS_SECONDS)."""
    span: dict[str, list[str]] = {}  # recipe -> [first, last] timestamp
    finished: set[str] = set()

    def show(line: bytes) -> None:
        try:
            entry = json.loads(line)
            spans = entry.get("spans", [])
            recipe = next((s["recipe"] for s in spans if "recipe" in s), "")
            name = recipe.rsplit("-", 2)[0]  # the output's name-version-build
            # the build itself: rattler-build renders every recipe up front
            if name and any(s.get("name") == "Running build for" for s in spans):
                span.setdefault(name, [entry["timestamp"]] * 2)[1] = entry["timestamp"]
                if str(entry["fields"].get("message", "")).startswith("Archive written to"):
                    finished.add(name)
            text = f"{entry['timestamp'][11:19]} [{name or '-'}] {entry['fields'].get('message', '')}\n"
            sys.stdout.write(text)
        except (ValueError, KeyError, AttributeError, TypeError):
            sys.stdout.buffer.write(line)
        sys.stdout.flush()

    args = [*args, "--parallel-builds", PARALLEL_BUILDS, "--log-style", "json"]
    rc = run_build(rattler_build(distro, args, skip_existing=False), cwd, show)
    if rc != 0 and (unfinished := sorted(set(span) - finished)):
        # rattler-build's final error doesn't say which of the parallel builds it is from
        print(f"Started but not finished: {', '.join(unfinished)}", file=sys.stderr, flush=True)
    if rc == 0 and os.environ.get("GITHUB_ACTIONS"):
        import build_timings  # for packing the build jobs by time
        from datetime import datetime

        times = {r: [datetime.fromisoformat(t.replace("Z", "+00:00")) for t in span[r]]
                 for r in recipes if r in span and not r.startswith(f"ros-{distro}-")}
        measured = {r: (last - first).total_seconds() for r, (first, last) in times.items()}
        if times and sum(measured.values()):
            wall = (max(last for _, last in times.values()) - min(first for first, _ in times.values())).total_seconds()
            for recipe, seconds in measured.items():
                build_timings.note(distro, recipe, seconds * wall / sum(measured.values()))
    return rc


def run_build(cmd: list[str], cwd: Path, show=None) -> int:
    """run() for builds: retry when the solver misses a package, waiting for the
    channel's index to catch up with the previous stage's uploads (at most
    INDEX_LAG_RETRIES times), or when the runner's network failed (at most
    NETWORK_RETRIES times, waiting longer each time). show(line) prints a line instead of
    writing it as is."""
    index_retries = network_retries = 0
    while True:
        print("+", " ".join(cmd), f"  (in {cwd.relative_to(ROOT)})", flush=True)
        missing = network = False
        with subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT) as proc:
            for line in proc.stdout:
                if show:
                    show(line)
                else:
                    sys.stdout.buffer.write(line)
                    sys.stdout.buffer.flush()
                missing = missing or any(m in line.lower() for m in INDEX_LAG_MESSAGES)
                network = network or any(m in line for m in NETWORK_MESSAGES)
        if proc.returncode == 0:
            return 0
        if network and network_retries < NETWORK_RETRIES:
            wait = NETWORK_WAIT * 2 ** network_retries
            network_retries += 1
            print(f"Network error; retrying in {wait} s (retry {network_retries} of {NETWORK_RETRIES})",
                  file=sys.stderr, flush=True)
            time.sleep(wait)
        elif missing and not network and index_retries < INDEX_LAG_RETRIES:
            index_retries += 1
            print(f"A dependency was not found; waiting {INDEX_LAG_WAIT} s for the channel index "
                  f"(retry {index_retries} of {INDEX_LAG_RETRIES})", file=sys.stderr, flush=True)
            time.sleep(INDEX_LAG_WAIT)
        else:
            return proc.returncode


# --------------------------------------------------------------------------- #
# generated files
# --------------------------------------------------------------------------- #
def vinca_pinning(distro: str) -> str:
    """shared/pinning/conda_forge.yaml + overrides.yaml, adjusted by distro.yaml.

    Shared override entries that the distribution overrides itself are left out
    together with the comment lines above them (comments carry the conda-build
    selectors, so this works on text, not on parsed YAML).
    """
    s = settings(distro)
    own = s.get("pinning_overrides") or {}
    out = [f"# Generated by `pixi run rs {distro} render-pinning` from shared/pinning/ and",
           f"# distros/{distro}/distro.yaml; edit those instead.", ""]
    if s.get("conda_forge_pinning_version"):
        out.append(f"conda_forge_pinning_version: {s['conda_forge_pinning_version']}")
        migrations = s.get("conda_forge_migrations") or []
        out.append("migrations:" if migrations else "migrations: []")
        out += [f"  - {m}" for m in migrations]
    else:
        out += [l for l in (SHARED / "pinning" / "conda_forge.yaml").read_text().splitlines()
                if not l.startswith("#")]
    comments: list[str] = []
    skip = False
    for line in (SHARED / "pinning" / "overrides.yaml").read_text().splitlines():
        if line.startswith("#"):
            continue
        if line.startswith("  #"):
            comments.append(line)
        elif line.startswith("  ") and not line.startswith("   "):
            skip = line[2:].split(":")[0] in own
            if not skip:
                out += comments + [line]
            comments = []
        elif line.startswith("    "):
            if not skip:
                out.append(line)
        else:
            out.append(line)
    if own:
        out.append(f"  # ros-{distro} specific (distros/{distro}/distro.yaml)")
        for key, value in own.items():
            out.append(f"  {key}:")
            out += [f"    - {v}" for v in (value if isinstance(value, list) else [value])]
    return "\n".join(out) + "\n"


def load_yaml(path: Path) -> dict:
    import yaml

    return (yaml.safe_load(path.read_text()) or {}) if path.is_file() else {}


# package lists of vinca.yaml (import_distro strips the shared entries)
LIST_KEYS = ("packages_select_by_deps", "packages_exclude", "packages_skip")


def work_dir(distro: str) -> Path:
    return DISTROS / distro / "work"


def prepare(distro: str) -> Path:
    """Set up distros/<d>/work/, where vinca and rattler-build run (recipes/ and
    output/ of earlier runs are kept). work/vinca.yaml extends the distribution's
    vinca.yaml, which extends shared/vinca.yaml (vinca combines them, and the files
    next to each: pkg_additional_info.yaml, patch/, tests/, robostack.yaml)."""
    d, w = DISTROS / distro, work_dir(distro)
    w.mkdir(exist_ok=True)
    # copies an earlier version of this script made; vinca would read them as part of
    # the configuration (work/ is the extending configuration's directory)
    for stale in ("patch", "tests"):
        shutil.rmtree(w / stale, ignore_errors=True)
    for stale in ("pkg_additional_info.yaml", "robostack.yaml", "packages_ignore.yaml", "packages-ignore.yaml"):
        (w / stale).unlink(missing_ok=True)
    # packages already on the channel aren't built again; while uploads go to another
    # channel (ROBOSTACK_UPLOAD_CHANNEL, e.g. a test channel), those count too
    skip = [channel_url(distro)] + ([extra] if (extra := upload_channel_url()) else [])
    (w / "vinca.yaml").write_text(
        f"# Generated by `pixi run rs {distro} prepare`; edit distros/{distro}/vinca.yaml or shared/vinca.yaml.\n"
        "extends: ../vinca.yaml\n"
        "skip_existing:\n" + "".join(f"  - {url}/\n" for url in skip)
    )
    # vinca-gha checks for ./.scripts/build_win.bat, which the Windows jobs call
    shutil.rmtree(w / ".scripts", ignore_errors=True)
    shutil.copytree(ROOT / ".scripts", w / ".scripts")
    (w / "vinca_pinning.yaml").write_text(vinca_pinning(distro))
    if (d / "conda_build_config.yaml").is_file():
        shutil.copy2(d / "conda_build_config.yaml", w / "conda_build_config.yaml")
    return w


def read_vinca(distro: str, platform: str | None = None) -> dict:
    """The distribution's vinca configuration as vinca reads it (shared/ + distro,
    selectors resolved for the platform, default: this machine's)."""
    from vinca.configuration import read_vinca_yaml
    from vinca.platforms import get_conda_subdir

    return read_vinca_yaml(prepare(distro) / "vinca.yaml", platform or get_conda_subdir())


# --------------------------------------------------------------------------- #
# tasks
# --------------------------------------------------------------------------- #
def upload_channel_url() -> str | None:
    """The prefix.dev channel ROBOSTACK_UPLOAD_CHANNEL redirects uploads to, if any."""
    override = os.environ.get("ROBOSTACK_UPLOAD_CHANNEL", "").strip()
    if not override or override == "none":
        return None
    # repo.prefix.dev serves namespaced channels (owner/channel) directly
    return f"https://repo.prefix.dev/{override}"


def rattler_build(distro: str, args: list[str], skip_existing: bool = True) -> list[str]:
    # With uploads redirected (e.g. to a test channel), later stages of the staged
    # build need the packages earlier stages uploaded there, and reruns skip them.
    extra = upload_channel_url()
    cmd = ["rattler-build", "build", "-m", "./conda_build_config.yaml",
           *(["-c", extra] if extra else []),
           "-c", channel_url(distro), "-c", CONDA_FORGE, "--channel-priority", "disabled"]
    return cmd + (["--skip-existing=all"] if skip_existing else []) + args


UPLOAD_HOSTS = ("prefix", "anaconda")


def anaconda_token_secret(distro: str) -> str:
    """The repository secret with the anaconda.org API token of the distribution's
    organization (robostack-<distro>); the build workflows pass it on as ANACONDA_API_TOKEN."""
    return f"ANACONDA_API_TOKEN_{distro.upper()}"


def upload_hosts(distro: str) -> list[str]:
    """Where the distribution's packages are uploaded (upload_to in distro.yaml; both by
    default), to the channel of the same name on each."""
    return list(settings(distro).get("upload_to", UPLOAD_HOSTS))


def upload(distro: str, files: list[str], cwd: Path) -> int:
    """Upload to the distribution's channel on prefix.dev and/or anaconda.org (upload_to;
    same channel name on each).

    prefix.dev uploads use trusted publishing (Repository Access, OIDC; no stored key)
    and publish an attestation. anaconda.org uploads use ANACONDA_API_TOKEN, which the
    build workflow sets from the secret ANACONDA_API_TOKEN_<DISTRO> (see generate_gha). Both skip packages that are already
    there, so a re-run only uploads what's missing.

    ROBOSTACK_UPLOAD_CHANNEL (set by the staged build workflows from the repository
    variable of the same name) redirects uploads to another prefix.dev channel only,
    e.g. a test channel (the builds then also read from it, see rattler_build); "none"
    skips uploading.
    """
    override = os.environ.get("ROBOSTACK_UPLOAD_CHANNEL", "").strip()
    if override == "none":
        print(f"ROBOSTACK_UPLOAD_CHANNEL=none: not uploading {' '.join(files)}")
        return 0
    channel = override or channel_name(distro)
    hosts = ["prefix"] if override else upload_hosts(distro)
    rc = 0
    if "prefix" in hosts:
        rc = retry(["rattler-build", "upload", "prefix", "-c", channel, "--skip-existing", "--generate-attestation",
                    *files], cwd, "prefix.dev")
    if "anaconda" not in hosts:
        return rc
    token = os.environ.get("ANACONDA_API_TOKEN", "").strip()
    if not token:
        print(f"no ANACONDA_API_TOKEN (repository secret {anaconda_token_secret(distro)}): not uploading to "
              f"anaconda.org/{channel}", file=sys.stderr)
        return 1
    missing = [f for f in files if not on_anaconda(channel, Path(cwd, f))]
    if missing:
        rc |= retry(["rattler-build", "upload", "anaconda", "-o", channel, "--api-key", token, *missing], cwd,
                    "anaconda.org")
    return rc


UPLOAD_ATTEMPTS = 5
UPLOAD_WAIT = 30  # doubled on each retry: 30 s, 1, 2, 4 minutes (anaconda.org's 503s can last minutes)


def retry(cmd: list[str], cwd: Path, what: str) -> int:
    # the channels (and Sigstore, for attestations) fail now and then; uploads are
    # idempotent, so retry
    for attempt in range(1, UPLOAD_ATTEMPTS + 1):
        rc = run(cmd, cwd)
        if rc == 0 or attempt == UPLOAD_ATTEMPTS:
            return rc
        wait = UPLOAD_WAIT * 2 ** (attempt - 1)
        print(f"{what} upload attempt {attempt} failed, retrying in {wait} s", file=sys.stderr)
        time.sleep(wait)
    return rc


def on_anaconda(channel: str, package: Path) -> bool:
    """Whether anaconda.org already has this package file (rattler-build's anaconda upload
    has no --skip-existing). The builds write packages to output/<subdir>/."""
    import urllib.error
    import urllib.request

    stem = package.name.removesuffix(".conda").removesuffix(".tar.bz2")
    name, version, _build = stem.rsplit("-", 2)
    url = f"https://api.anaconda.org/dist/{channel}/{name}/{version}/{package.parent.name}/{package.name}"
    # the runner's network fails now and then (e.g. a DNS lookup): retry, waiting longer each time
    for attempt in range(1, 5):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "robostack"}), timeout=60):
                print(f"{package.name} is already on anaconda.org/{channel}")
                return True
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return False
            if attempt == 4 or e.code < 500:
                raise
        except OSError as e:  # URLError, timeouts, DNS failures
            if attempt == 4:
                raise
            print(f"anaconda.org unreachable ({e}); retrying in {30 * attempt} s", file=sys.stderr, flush=True)
        time.sleep(30 * attempt)
    return False


def task(distro: str, name: str, args: list[str]) -> int:
    d, w = DISTROS / distro, work_dir(distro)
    py = [sys.executable]
    if name == "prepare":
        prepare(distro)
        return 0
    if name == "render-pinning":
        prepare(distro)
        rc = run(["vinca-pinning-render"], w)
        if not rc:
            shutil.copy2(w / "conda_build_config.yaml", d / "conda_build_config.yaml")
        return rc
    if name == "create-snapshot":
        return run(["vinca-snapshot", "-d", distro, "-o", "rosdistro_snapshot.yaml", *args], d)
    if name == "sort":
        files = [f for f in ("pkg_additional_info.yaml", "rosdistro_additional_recipes.yaml") if (d / f).is_file()]
        rc = run(["vinca-sort-vinca-lists", *args, "vinca.yaml"], d)
        return rc or run(["vinca-sort-yaml-keys", *args, *files], d)
    if name == "upload":
        started = time.monotonic()
        rc = upload(distro, args, w if w.is_dir() else d)
        if rc == 0 and len(args) == 1 and os.environ.get("GITHUB_ACTIONS"):
            import build_timings  # uploads take long too (attestations, two channels)

            recipe = Path(args[0]).name.removesuffix(".conda").removesuffix(".tar.bz2").rsplit("-", 2)[0]
            build_timings.note(distro, recipe, time.monotonic() - started, upload=True)
        return rc
    if name in ("update-snapshot", "find-stale", "update-pinning", "dependency-report"):
        import maintenance

        return maintenance.main(name, args, distro)

    # everything else runs in the assembled work directory
    if name == "generate-recipes":
        prepare(distro)
        shutil.rmtree(w / "recipes", ignore_errors=True)
        shutil.rmtree(w / "recipes_only_patch", ignore_errors=True)
        (w / "recipes").mkdir()
        return run(["vinca", "-m", *args], w)
    if not (w / "vinca.yaml").is_file():
        prepare(distro)
    if name == "check-orphaned-patches":
        return run(py + [str(TOOLS / "check_orphaned_platform_patches.py"), "--patch-dir", str(d / "patch"), *args], w)
    if name == "check-patches":
        # shared/patch too, which the distribution's patches for the same package replace
        dirs = ["--patch-dir", str(SHARED / "patch"), "--patch-dir", str(d / "patch")]
        return run(py + [str(TOOLS / "check_patches_clean_apply.py"), *dirs, *args], w)
    if name == "check-deps":
        return run(py + [str(TOOLS / "check_dependency_compat.py"), *args], w)
    if name == "gap-report":
        return run(py + [str(TOOLS / "build_gap_report.py"), *args], w)
    if name in ("build", "build-one"):
        if not (w / "recipes").is_dir():
            print(f"No {(w / 'recipes').relative_to(ROOT)}: run `pixi run rs {distro} generate-recipes` first",
                  file=sys.stderr)
            return 1
        if not any((w / "recipes").glob("*/recipe.yaml")):
            # generated, but every package is already built (on the channel)
            print(f"Nothing to build: {(w / 'recipes').relative_to(ROOT)} is empty")
            return 0
    if name == "build":
        return run_build(rattler_build(distro, ["--recipe-dir", "./recipes", *args]), w)
    if name == "build-ci":  # used by .scripts/build_unix.sh / build_win.bat
        recipes = [Path(args[i + 1].rstrip("\\/")).name for i, a in enumerate(args[:-1]) if a == "--recipe"]
        if len(recipes) > 1:
            return build_ci_parallel(distro, recipes, args, w)
        started = time.monotonic()
        rc = run_build(rattler_build(distro, args, skip_existing=False), w)
        if rc == 0 and recipes and os.environ.get("GITHUB_ACTIONS"):
            import build_timings  # for packing the build jobs by time

            build_timings.note(distro, recipes[0], time.monotonic() - started)
        return rc
    if name == "build-one":
        pkg = args[0] if args else "ros2-ros-workspace"
        for patch in (d / "patch").glob(f"{pkg}.*patch"):
            shutil.copy(patch, w / "recipes" / pkg / "patch" / patch.name)
        return run(rattler_build(distro, ["--recipe", f"./recipes/{pkg}/recipe.yaml", *args[1:]], False), w)
    if name == "gha":  # staged build workflows for one platform, see .github/workflows/main.yaml
        return generate_gha(distro, args)
    raise SystemExit(f"unknown task {name!r}")


def generate_gha(distro: str, args: list[str]) -> int:
    """vinca-gha for one distribution; the workflow gets a distro-specific name and
    trigger branch, the file name .github/workflows/build.yaml, and tells the build
    scripts which distro to build."""
    parser = argparse.ArgumentParser(prog="rs <distro> gha")
    parser.add_argument("--platform", required=True)
    parser.add_argument("--batch-size", default="25")
    ns = parser.parse_args(args)
    d = work_dir(distro)
    branch = f"buildbranch_{distro}_{ns.platform.replace('-', '_')}"
    before = set(d.glob("*.yml"))
    # GitHub refuses workflow files over 500 KiB, which a full rebuild's many batches can
    # reach (jazzy's win-64 at 25): then retry with larger, hence fewer, batches.
    batch_size = int(ns.batch_size)
    # each job keeps the build times it records (build-ci); vinca-gha packs each stage's
    # recipes into jobs by those times (every recipe the same without them)
    import build_timings

    extra = ["--keep-file", f"distros/{distro}/work/{build_timings.JOURNAL}"]
    if build_timings.vinca_timings(distro, ns.platform, d / "build_timings.json"):
        extra += ["--timings", "build_timings.json"]
    # vinca-gha keeps the workflow under GitHub's size limit itself (a job budget); only
    # when even the fewest jobs the batch size allows are too many, retry with larger batches
    while True:
        rc, output = run_tee(["vinca-gha", "--platform", ns.platform, "--trigger-branch", branch, "-d",
                              "./recipes", "--batch_size", str(batch_size), *extra], d)
        if not rc or WORKFLOW_SIZE_MESSAGE not in output:
            break
        if batch_size >= MAX_BATCH_SIZE:
            break
        batch_size = min(batch_size * 3 // 2, MAX_BATCH_SIZE)
        print(f"The workflow is too large; retrying with --batch_size {batch_size}", flush=True)
    if rc:
        return rc
    # Edit the text: a YAML round trip would turn the `on:` key into `true:` (YAML 1.1).
    for wf in sorted(set(d.glob("*.yml")) - before):
        lines = wf.read_text().splitlines()
        # all build branches share .github/workflows/build.yaml, which GitHub lists as one
        # workflow: run-name tells the runs apart
        # one run per distribution and platform at a time: a newer run (the latest main's
        # recipes and workflow) cancels the older one, and skips what that one uploaded
        # already. Re-run failed jobs only in the newest run: re-running an older one
        # would cancel the newer run.
        lines = [f"name: build\nrun-name: {distro} {ns.platform}\n"
                 f"concurrency:\n  group: build-{distro}-{ns.platform}\n  cancel-in-progress: true"
                 if l.startswith("name: ") else l for l in lines]
        # vinca-gha passes secrets.ANACONDA_API_TOKEN to the build jobs, for the anaconda.org
        # upload (prefix.dev uses trusted publishing, id-token: write): each distribution
        # has its own token (its organization's), the secret ANACONDA_API_TOKEN_<DISTRO>
        secret = anaconda_token_secret(distro)
        lines = [l.replace("secrets.ANACONDA_API_TOKEN }}", f"secrets.{secret} }}}}") for l in lines]
        # jobs that declare permissions get none they don't list: checking out the
        # repository needs contents: read (only works without it while it is public)
        lines = [f"{l}\n{l[:len(l) - len(l.lstrip())]}contents: read" if l.strip() == "id-token: write" else l
                 for l in lines]
        if sum("permissions:" in l for l in lines) != sum("contents: read" in l for l in lines):
            raise SystemExit(f"{wf}: a job with permissions but without contents: read")
        if ns.platform == "win-64" and not any("build_win.bat" in l for l in lines):
            raise SystemExit(f"{wf}: the Windows workflow doesn't use .scripts/build_win.bat")
        if any(l.startswith("env:") for l in lines):
            raise SystemExit(f"{wf}: unexpected top-level env in the vinca-gha output")
        lines += [
            "env:",
            f"  ROBOSTACK_DISTRO: {distro}",
            "  # optional repository variable: upload to this prefix.dev channel instead",
            "  # (e.g. a test channel), or 'none' to only build",
            "  ROBOSTACK_UPLOAD_CHANNEL: ${{ vars.ROBOSTACK_UPLOAD_CHANNEL }}",
        ]
        # one file name on every build branch (each branch carries only its own
        # workflow), so a single prefix.dev Repository Access source matches them all
        dest = ROOT / ".github" / "workflows" / "build.yaml"
        dest.write_text("\n".join(lines) + "\n")
        wf.unlink()
        print(f"wrote {dest.relative_to(ROOT)} (trigger branch {branch})")
    return 0


NO_BUILD_LABEL = "build:none"  # only the checks (tooling, CI or docs changes)
# the pull-request build's platforms and their runners; build:linux / build:osx / build:win
# (or build:<platform>) labels limit a PR build to those
BUILD_TARGETS = [
    {"os": "ubuntu-latest", "platform": "linux-64"},
    {"os": "ubuntu-24.04-arm", "platform": "linux-aarch64"},
    {"os": "macos-15-intel", "platform": "osx-64"},
    {"os": "macos-15", "platform": "osx-arm64"},
    {"os": "windows-2022", "platform": "win-64"},
]
PLATFORM_LABELS = {"linux": ("linux-64", "linux-aarch64"), "osx": ("osx-64", "osx-arm64"), "win": ("win-64",)}


def build_distros(changed: list[str], labels: list[str]) -> list[str]:
    """The distributions a pull request builds: none with the build:none label, those of
    its build:<distro> labels when it has any (e.g. a rebuild of one distribution that also
    touches shared files), else the changed ones."""
    if NO_BUILD_LABEL in labels:
        return []
    platforms = set(PLATFORM_LABELS) | {t["platform"] for t in BUILD_TARGETS}
    selected = sorted({label.split(":", 1)[1] for label in labels if label.startswith("build:")} - platforms)
    unknown = sorted(set(selected) - set(distros()))
    if unknown:
        raise SystemExit(f"build:<distro> labels name unknown distributions: {', '.join(unknown)}")
    return selected or changed


def build_targets(labels: list[str]) -> list[dict]:
    """The platforms a pull request builds: those of its build:linux / build:osx / build:win
    (or build:<platform>) labels when it has any, else all of them."""
    wanted = set()
    for label in labels:
        name = label.split(":", 1)[1] if label.startswith("build:") else ""
        wanted |= set(PLATFORM_LABELS.get(name, ())) | ({name} & {t["platform"] for t in BUILD_TARGETS})
    return [t for t in BUILD_TARGETS if not wanted or t["platform"] in wanted]


def changed_distros(base: str) -> list[str]:
    # no common history (main's history was rewritten and force-pushed): nothing to
    # diff against, so every distribution counts as changed
    if subprocess.run(["git", "merge-base", base, "HEAD"], cwd=ROOT, capture_output=True).returncode:
        return distros()
    out = subprocess.run(["git", "diff", "--name-only", f"{base}...HEAD"], cwd=ROOT,
                         capture_output=True, text=True, check=True).stdout.split()
    out = [f for f in out if not f.endswith(".md") and f != "LICENSE"]  # docs don't need builds
    if any(not f.startswith(PER_DISTRO_PREFIX) for f in out):
        return distros()  # shared files changed: every distribution is affected
    return sorted({f.split("/")[1] for f in out if f.count("/") >= 2} & set(distros()))


REPO_COMMANDS = ("check", "update-pinning", "new-distro", "parse-command", "add-package", "rosdistro-syncs",
                 "rebuild", "rebuild-dependents", "update-vinca", "update-build-timings")


def main() -> int:
    if len(sys.argv) >= 2 and sys.argv[1] in REPO_COMMANDS:
        import maintenance

        return maintenance.main(sys.argv[1], sys.argv[2:])
    if len(sys.argv) >= 2 and sys.argv[1] == "changed-distros":
        parser = argparse.ArgumentParser(prog="rs changed-distros")
        parser.add_argument("--base", required=True)
        parser.add_argument("--all", action="store_true", help="ignore the diff, list every distribution")
        parser.add_argument("--labels", default="[]", help="JSON list of the pull request's labels (build:<distro>)")
        ns = parser.parse_args(sys.argv[2:])
        changed = distros() if ns.all else changed_distros(ns.base)
        print(json.dumps(build_distros(changed, json.loads(ns.labels))))
        return 0
    if len(sys.argv) >= 2 and sys.argv[1] == "build-targets":
        parser = argparse.ArgumentParser(prog="rs build-targets")
        parser.add_argument("--labels", default="[]", help="JSON list of the pull request's labels")
        ns = parser.parse_args(sys.argv[2:])
        print(json.dumps(build_targets(json.loads(ns.labels))))
        return 0
    if len(sys.argv) < 3:
        print(__doc__)
        print("distributions:", ", ".join(distros()))
        return 2
    distro, name, *args = sys.argv[1:]
    if distro not in distros():
        raise SystemExit(f"unknown distribution {distro!r}; choose from {', '.join(distros())}")
    return task(distro, name, args)


if __name__ == "__main__":
    sys.exit(main())
