"""Run the tests bundled in the published packages again, against today's dependencies.

Packages carry their recipe tests (rattler-build bundles them in info/tests), so they
can be tested again after they were uploaded. Installing each one with the newest
dependencies from conda-forge catches breakages that come from conda-forge rather
than from RoboStack, e.g. an ABI change in a dependency that a RoboStack package was
built against (RoboStack/ros-humble#274). The nightly package-tests workflow runs this
for every distribution and platform and keeps one issue for all of them
("Package tests", label package-tests, a section per distribution), closed when every
test passes.

    python tools/package_tests.py select --distro jazzy --platform linux-64
    python tools/package_tests.py run --distro jazzy --platform linux-64 --results results/jazzy-linux-64.json
    python tools/package_tests.py report results/ [--publish]   # --publish: update the issue (GitHub Actions)

The tested packages are the ones with a test file (shared/tests/<package>.yaml or
distros/<distro>/tests/<package>.yaml): vinca adds no tests of its own. Each is tested in
its newest build on the distribution's channel, every variant of it (e.g. each Python).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import rattler
from rattler.package_streaming import download_to_path
from vinca.utils import get_channel_records

sys.path.insert(0, str(Path(__file__).resolve().parent))
import robostack as rs

REPORT_LABEL = "package-tests"
REPORT_MARKER = "<!-- robostack-package-tests -->"
REPORT_TITLE = "Package tests"
# the issues of before, one per distribution: closed in favour of the one issue
OLD_TITLE = re.compile(r"^\S+: package tests$")
MAX_ISSUE_BODY = 60000  # GitHub's limit is 65536 characters
LOG_TAIL = 60  # lines of a failed test's output kept in the results
# a package's tests that run longer are stopped and reported as failed: a test that hangs
# (e.g. a node that ignores SIGTERM, holding the test's output open) otherwise runs into the
# job's time limit, and the platform's results of every package are lost
TEST_TIMEOUT = 20 * 60
TIMED_OUT = "package-tests: stopped, the tests ran longer than {minutes} min"
# a test that rattler-build ran: a script test prints "Testing commands:", the others
# "✔ <kind> test passed!" (not its closing "all tests passed!", which it also prints
# for a package without tests)
RAN_TEST = re.compile(r"^Testing commands:|\b(?<!all )tests? passed!")


def tested_packages(distro: str) -> list[str]:
    """Package names with a test file, shared or the distribution's own."""
    names = set()
    for tests in (rs.ROOT / "shared" / "tests", rs.DISTROS / distro / "tests"):
        names.update(p.stem for p in tests.glob("*.yaml"))
    return sorted(names)


def download(url: str, path: Path) -> None:
    asyncio.run(download_to_path(rattler.Client(), url, path))


def free_space(path: str) -> str:
    return f"{shutil.disk_usage(path).free / 2**30:.1f} GiB free"


def newest_builds(channel_records: list[dict], names: list[str]) -> list[dict]:
    """For each name, every variant of its newest build: the records with the version
    and build number of the newest upload (one per build string, e.g. per Python)."""
    records: dict[str, list[dict]] = {}
    for record in channel_records:
        if record.get("name") in names:
            records.setdefault(record["name"], []).append(record)
    selected = []
    for name in sorted(records):
        newest = max(records[name], key=lambda r: (r.get("build_number", 0), r.get("timestamp", 0)))
        variants: dict[str, dict] = {}
        for record in records[name]:
            if (record["version"], record.get("build_number")) != (newest["version"], newest.get("build_number")):
                continue
            # the same build in both formats: prefer .conda
            if record["build"] not in variants or record["fn"].endswith(".conda"):
                variants[record["build"]] = record
        selected += sorted(variants.values(), key=lambda r: r["build"])
    return selected


def select(distro: str, platform: str) -> list[dict]:
    """The package files to test on this platform: {name, version, build, subdir, url}."""
    names = tested_packages(distro)
    selected = []
    for record in newest_builds(get_channel_records(rs.repodata_url(distro), platform), names):
        selected.append({"name": record["name"], "version": record["version"], "build": record["build"],
                         "subdir": record["subdir"], "url": record["url"]})
    return selected


def test_command(distro: str, package_file: Path) -> list[str]:
    """rattler-build test with the build channels: the distribution's channel for the
    other RoboStack packages, conda-forge for the rest (newest versions, not a lock)."""
    return ["rattler-build", "test", "--package-file", str(package_file), "-c", rs.channel_url(distro),
            "-c", rs.CONDA_FORGE, "--channel-priority", "disabled", "--log-style", "plain"]


def kill_tree(proc: subprocess.Popen) -> None:
    """Stop a process and everything it started (its process group, started with
    start_new_session): a test's leftover processes would keep its output pipe open."""
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, check=False)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_package(distro: str, package_file: Path, timeout: float = TEST_TIMEOUT) -> tuple[bool, str, int]:
    """Run the package's bundled tests: (passed, output, number of tests that ran); stopped
    and failed after *timeout* seconds."""
    proc = subprocess.Popen(test_command(distro, package_file), stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                            start_new_session=True)
    try:
        log, _ = proc.communicate(timeout=timeout)
        ok = proc.returncode == 0
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        log, _ = proc.communicate()
        log += "\n" + TIMED_OUT.format(minutes=round(timeout / 60))
        ok = False
    return ok, log, sum(bool(RAN_TEST.search(line.strip())) for line in log.splitlines())


def tail(text: str, lines: int) -> str:
    return "\n".join(text.splitlines()[-lines:])


def run(distro: str, platform: str, results: Path, only: list[str] | None = None) -> int:
    """Test the selected packages (only these names, if given) one by one; write the
    results (JSON) and return 1 when one failed."""
    packages = [p for p in select(distro, platform) if not only or p["name"] in only]
    # rattler-build test unpacks each test's packages and environment in a temporary
    # directory (TMPDIR): a large one (e.g. ros2-desktop-full) needs several GB, more
    # than a RAM-backed /tmp may hold (the workflow points TMPDIR to the runner's disk)
    print(f"{distro} {platform}: {len(packages)} package files to test; {tempfile.gettempdir()}: "
          f"{free_space(tempfile.gettempdir())}", flush=True)
    outcome = []
    with tempfile.TemporaryDirectory() as tmp:
        for package in packages:
            label = f"{package['name']} {package['version']} {package['build']}"
            print(f"::group::{label}" if os.environ.get("GITHUB_ACTIONS") else f"== {label}", flush=True)
            package_file = Path(tmp) / package["url"].rsplit("/", 1)[1]
            started = time.monotonic()
            try:
                download(package["url"], package_file)
                ok, log, count = test_package(distro, package_file)
                # launch tests can time out on DDS discovery: a failure is tried once more,
                # and a pass then is reported as flaky (not a test that hung: it would again)
                flaky = False
                if not ok and not log.endswith(TIMED_OUT.format(minutes=round(TEST_TIMEOUT / 60))):
                    print(log, "\n❌ failed, trying again", flush=True)
                    ok, log, count = test_package(distro, package_file)
                    flaky = ok
            except (OSError, RuntimeError) as error:  # rattler's download errors are RuntimeErrors
                ok, log, count, flaky = False, f"download failed: {error}", 0, False
            finally:
                package_file.unlink(missing_ok=True)
            print(log, flush=True)
            if os.environ.get("GITHUB_ACTIONS"):
                print("::endgroup::", flush=True)
            # a build from before its test file was added carries no tests: not a pass
            status = "failed" if not ok else "flaky" if flaky else "passed" if count else "no-tests"
            print(f"{ {'passed': '✅', 'flaky': '⚠️', 'failed': '❌', 'no-tests': '➖'}[status]} {label}: {status} "
                  f"({count} tests, {time.monotonic() - started:.0f} s, {free_space(tmp)})", flush=True)
            outcome.append({**package, "ok": ok, "status": status, "tests": count,
                            **({} if ok else {"log": tail(log, LOG_TAIL)})})
    results.parent.mkdir(parents=True, exist_ok=True)
    results.write_text(json.dumps({"distro": distro, "platform": platform, "packages": outcome}, indent=1))
    failed = [p for p in outcome if not p["ok"]]
    untested = [p for p in outcome if p["status"] == "no-tests"]
    flaky = [p for p in outcome if p["status"] == "flaky"]
    print(f"{distro} {platform}: {len(outcome) - len(failed) - len(untested)} passed ({len(flaky)} on the second "
          f"try), {len(failed)} failed, {len(untested)} without bundled tests", flush=True)
    return 1 if failed else 0


def load_results(directory: Path) -> dict[str, list[dict]]:
    """distro -> results of each platform (from run's JSON files, searched recursively:
    GitHub Actions downloads every artifact into its own directory)."""
    by_distro: dict[str, list[dict]] = {}
    for path in sorted(directory.rglob("*.json")):
        data = json.loads(path.read_text())
        by_distro.setdefault(data["distro"], []).append(data)
    return by_distro


def distro_section(distro: str, results: list[dict], missing: list[str]) -> str:
    """The distribution's section of the package-tests issue."""
    failed = [(r["platform"], p) for r in sorted(results, key=lambda r: r["platform"])
              for p in r["packages"] if not p["ok"]]
    tested = sum(p.get("status") != "no-tests" for r in results for p in r["packages"])
    untested = sorted({(p["name"], p["build"]) for r in results for p in r["packages"] if p.get("status") == "no-tests"})
    flaky = [(r["platform"], p) for r in sorted(results, key=lambda r: r["platform"])
             for p in r["packages"] if p.get("status") == "flaky"]
    summary = f"{len(failed)} of {tested} failed" if failed else f"all {tested} passed"
    icon = "❌" if failed or missing else "✅"
    lines = [f"## {icon} {distro}: {summary}", ""]
    if missing:
        lines += [f"⚠️ No results for {', '.join(missing)} (the job failed before testing, see the workflow run).", ""]
    if failed:
        lines += ["| platform | package | build |", "|---|---|---|"]
        lines += [f"| {platform} | `{p['name']}` {p['version']} | `{p['build']}` |" for platform, p in failed]
        lines.append("")
        for platform, p in failed:
            lines += [f"<details><summary>{platform}: {p['name']} {p['build']}</summary>", "", "```text",
                      p.get("log", ""), "```", "", "</details>", ""]
    if flaky:
        lines += [("Passed only on the second try: "
                   + ", ".join(f"`{p['name']}` ({platform})" for platform, p in flaky)), ""]
    if untested:
        lines += [("Without bundled tests (built before their test file was added; tested again once rebuilt): "
                   + ", ".join(f"`{name}` (`{build}`)" for name, build in untested)), ""]
    return "\n".join(lines)


def issue_body(sections: dict[str, str], run_url: str = "") -> str:
    """The package-tests issue: an introduction, then a section per distribution. Each
    section gets an equal share of the size limit, so one long log can't push the
    other distributions out of the issue."""
    head = "\n".join([
        REPORT_MARKER,
        "Nightly test of the published packages: the tests bundled in each package with a test file "
        "(`shared/tests/`, `distros/<distro>/tests/`), run with `rattler-build test` against the newest "
        "dependencies from conda-forge, to catch breakages that come from conda-forge (e.g. ABI changes). "
        "robostack-bot updates this issue every night and closes it when every test of every distribution passes.",
        "",
    ])
    foot = f"[Workflow run]({run_url})" if run_url else ""
    share = (MAX_ISSUE_BODY - len(head) - len(foot)) // max(len(sections), 1) - 100
    parts = [head]
    for distro in sorted(sections):
        section = sections[distro]
        if len(section) > share:
            section = section[:share] + "\n\n… (truncated, see the workflow run)\n"
        parts.append(section)
    if foot:
        parts.append(foot)
    return "\n".join(parts)


def close_old_issues(repo: str, keep: str | None) -> None:
    """Close the open per-distribution issues ("<distro>: package tests") that the one
    issue for all distributions replaces."""
    import maintenance

    query = '.[] | {number, title, html_url} | @json'
    out = maintenance._gh(["api", "--paginate", f"repos/{repo}/issues?labels={REPORT_LABEL}&state=open&per_page=100",
                           "--jq", query])
    for issue in (json.loads(line) for line in out.splitlines() if line.strip()):
        if OLD_TITLE.match(issue["title"]) and issue["html_url"] != keep:
            maintenance._gh(["api", "-X", "PATCH", f"repos/{repo}/issues/{issue['number']}",
                             "-f", "state=closed", "-f", "state_reason=not_planned"])
            print(f"  closed {issue['html_url']} ({issue['title']}; replaced by {keep or 'one issue'})", flush=True)


def report(directory: Path, platforms: list[str], distros: list[str] | None = None, publish: bool = False) -> int:
    """Summarise the results (of the given distributions, also those without any
    results) and, with publish, update the package-tests issue: open while a test fails
    or results are missing, closed otherwise."""
    run_url = ""
    if run_id := os.environ.get("GITHUB_RUN_ID"):
        run_url = f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{os.environ.get('GITHUB_REPOSITORY')}/actions/runs/{run_id}"
    failing = False
    sections = {}
    by_distro = load_results(directory)
    for distro in sorted(set(distros or []) | set(by_distro)):
        results = by_distro.get(distro, [])
        missing = sorted(set(platforms) - {r["platform"] for r in results})
        failed = sum(not p["ok"] for r in results for p in r["packages"])
        failing |= bool(failed or missing)
        sections[distro] = distro_section(distro, results, missing)
        print(f"{distro}: {failed} failed" + (f", no results for {', '.join(missing)}" if missing else ""), flush=True)
    body = issue_body(sections, run_url)
    if publish:
        import maintenance

        repo = maintenance._repository()
        url = maintenance.publish_issue(repo, REPORT_TITLE, REPORT_LABEL, body, failing)
        print(f"  {url or 'all passed, no issue to close'}", flush=True)
        close_old_issues(repo, url)
    else:
        print(body)
    return 1 if failing else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("select", help="list the package files that would be tested (JSON)")
    p.add_argument("--distro", required=True)
    p.add_argument("--platform", required=True)
    p = sub.add_parser("run", help="test them, writing the results")
    p.add_argument("--distro", required=True)
    p.add_argument("--platform", required=True)
    p.add_argument("--results", type=Path, required=True)
    p.add_argument("--package", action="append", help="only this package (repeatable; for local runs)")
    p = sub.add_parser("report", help="summarise results (and update the issue)")
    p.add_argument("directory", type=Path)
    p.add_argument("--publish", action="store_true",
                   help="create, update or close the package-tests issue (GitHub Actions, scheduled runs on main)")
    p.add_argument("--platforms", default="linux-64 linux-aarch64 osx-64 osx-arm64 win-64",
                   help="platforms that should have results (default: all five)")
    p.add_argument("--distros", default="", help="distributions that should have results (default: those found)")
    ns = parser.parse_args()
    if ns.command == "select":
        print(json.dumps(select(ns.distro, ns.platform), indent=1))
        return 0
    if ns.command == "run":
        return run(ns.distro, ns.platform, ns.results, ns.package)
    return report(ns.directory, ns.platforms.split(), ns.distros.split(), ns.publish)


if __name__ == "__main__":
    sys.exit(main())
