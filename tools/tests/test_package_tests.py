import json
import sys
import time
from pathlib import Path

import package_tests


def record(fn, name, version, build, number, timestamp=0):
    return {"fn": fn, "name": name, "version": version, "build": build, "build_number": number, "timestamp": timestamp}


def test_tested_packages_are_the_test_files(tmp_path, monkeypatch):
    (tmp_path / "shared" / "tests").mkdir(parents=True)
    (tmp_path / "distros" / "jazzy" / "tests").mkdir(parents=True)
    (tmp_path / "shared" / "tests" / "ros2-rclpy.yaml").write_text("tests: []\n")
    (tmp_path / "shared" / "tests" / "ros2-std-msgs").mkdir()  # the files a test uses
    (tmp_path / "distros" / "jazzy" / "tests" / "ros2-rcutils.yaml").write_text("tests: []\n")
    monkeypatch.setattr(package_tests.rs, "ROOT", tmp_path)
    monkeypatch.setattr(package_tests.rs, "DISTROS", tmp_path / "distros")
    assert package_tests.tested_packages("jazzy") == ["ros2-rclpy", "ros2-rcutils"]
    assert package_tests.tested_packages("humble") == ["ros2-rclpy"]


def test_newest_builds_keeps_every_variant_of_the_newest_build():
    records = [
        record("a-1-py312_25.conda", "ros2-a", "1", "py312_25", 25, 1),
        record("a-1-py314_27.conda", "ros2-a", "1", "py314_27", 27, 2),
        # the same build in the old format too: the .conda one is tested
        record("a-1-py314_27.tar.bz2", "ros2-a", "1", "py314_27", 27, 2),
        record("a-1-py313_27.conda", "ros2-a", "1", "py313_27", 27, 3),
        record("b-2-h1_3.conda", "ros2-b", "2", "h1_3", 3),
        record("c-1-h_1.conda", "ros2-untested", "1", "h_1", 1),
    ]
    selected = package_tests.newest_builds(records, ["ros2-a", "ros2-b"])
    assert [(r["name"], r["build"], r["fn"]) for r in selected] == [
        ("ros2-a", "py313_27", "a-1-py313_27.conda"),
        ("ros2-a", "py314_27", "a-1-py314_27.conda"),
        ("ros2-b", "h1_3", "b-2-h1_3.conda"),
    ]
    assert package_tests.newest_builds([], ["ros2-a"]) == []


def test_ran_test_counts_the_tests_rattler_build_ran():
    log = ["Testing commands:", "✔ python imports test passed!", "✔ package contents test passed!",
           "✔ all tests passed!", "✔ Successfully updated the test environment"]
    assert sum(bool(package_tests.RAN_TEST.search(line)) for line in log) == 3


def test_issue_body_has_a_section_per_distribution():
    results = [
        {"distro": "jazzy", "platform": "win-64", "packages": [
            {"name": "ros2-a", "version": "1", "build": "h_27", "ok": False, "status": "failed", "log": "boom"},
            {"name": "ros2-b", "version": "2", "build": "h_25", "ok": True, "status": "no-tests"},
            {"name": "ros2-c", "version": "3", "build": "h_27", "ok": True, "status": "flaky"},
        ]},
        {"distro": "jazzy", "platform": "linux-64", "packages": [
            {"name": "ros2-a", "version": "1", "build": "h_27", "ok": True, "status": "passed"},
        ]},
    ]
    jazzy = package_tests.distro_section("jazzy", results, ["osx-64"])
    assert jazzy.startswith("## ❌ jazzy: 1 of 3 failed") and "second try: `ros2-c` (win-64)" in jazzy
    assert "| win-64 | `ros2-a` 1 | `h_27` |" in jazzy and "boom" in jazzy
    assert "`ros2-b` (`h_25`)" in jazzy and "No results for osx-64" in jazzy
    humble = package_tests.distro_section("humble", results[1:], [])
    assert humble.startswith("## ✅ humble: all 1 passed") and "|---|" not in humble
    body = package_tests.issue_body({"jazzy": jazzy, "humble": humble}, "https://run")
    assert body.startswith(package_tests.REPORT_MARKER) and body.endswith("[Workflow run](https://run)")
    assert body.index("## ✅ humble") < body.index("## ❌ jazzy")


def test_issue_body_shares_the_size_limit_between_distributions(monkeypatch):
    monkeypatch.setattr(package_tests, "MAX_ISSUE_BODY", 3000)
    body = package_tests.issue_body({"humble": "## humble\n" + "x" * 5000, "jazzy": "## jazzy\nshort"})
    assert len(body) <= 3000 and "## jazzy\nshort" in body and "truncated" in body


def test_report_includes_distributions_without_results(tmp_path, capsys):
    (tmp_path / "package-tests-jazzy-linux-64").mkdir()
    (tmp_path / "package-tests-jazzy-linux-64" / "jazzy-linux-64.json").write_text(json.dumps(
        {"distro": "jazzy", "platform": "linux-64",
         "packages": [{"name": "ros2-a", "version": "1", "build": "h_1", "ok": True, "status": "passed"}]}))
    assert package_tests.report(tmp_path, ["linux-64"], ["jazzy"]) == 0
    # humble's jobs failed before writing results: reported, and the run fails
    assert package_tests.report(tmp_path, ["linux-64"], ["humble", "jazzy"]) == 1
    out = capsys.readouterr().out
    assert "humble: 0 failed, no results for linux-64" in out and "## ❌ humble" in out and "## ✅ jazzy" in out


def test_report_publishes_one_issue_and_closes_the_old_ones(tmp_path, monkeypatch):
    import maintenance

    published, calls = [], []
    monkeypatch.setattr(maintenance, "_repository", lambda: "o/r")
    monkeypatch.setattr(maintenance, "publish_issue",
                        lambda repo, title, label, body, open_: published.append((title, label, open_)) or "https://i/9")
    old = [{"number": 149, "title": "jazzy: package tests", "html_url": "https://i/149"},
           {"number": 9, "title": "Package tests", "html_url": "https://i/9"}]

    def gh(args):
        calls.append(args)
        return "\n".join(json.dumps(i) for i in old) if args[1] == "--paginate" else ""
    monkeypatch.setattr(maintenance, "_gh", gh)
    assert package_tests.report(tmp_path, ["linux-64"], ["jazzy"], publish=True) == 1
    assert published == [("Package tests", "package-tests", True)]
    closed = [a for a in calls if "PATCH" in a]
    assert closed == [["api", "-X", "PATCH", "repos/o/r/issues/149", "-f", "state=closed", "-f", "state_reason=not_planned"]]


def test_a_test_that_hangs_is_stopped_and_failed(monkeypatch):
    # the test leaves a process behind that ignores SIGTERM and keeps the output open,
    # like a node the test didn't stop: the whole process group is killed at the timeout
    hang = ("import signal, subprocess, sys, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "subprocess.Popen([sys.executable, '-c', 'import signal, time; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)']); "
            "print('Testing commands:', flush=True); time.sleep(60)")
    monkeypatch.setattr(package_tests, "test_command", lambda *_: [sys.executable, "-c", hang])
    started = time.monotonic()

    ok, log, count = package_tests.test_package("humble", Path("p.conda"), timeout=2)

    assert time.monotonic() - started < 30
    assert not ok and count == 1
    assert log.endswith(package_tests.TIMED_OUT.format(minutes=0))


def test_a_test_that_finishes_passes(monkeypatch):
    monkeypatch.setattr(package_tests, "test_command",
                        lambda *_: [sys.executable, "-c", "print('Testing commands:'); print('✔ all tests passed!')"])
    assert package_tests.test_package("humble", Path("p.conda"), timeout=30) == (
        True, "Testing commands:\n✔ all tests passed!\n", 1)
