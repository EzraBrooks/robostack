import subprocess
"""robostack-bot: command parsing and the pure helpers of rebuild, rebuild-dependents,
update-vinca and dependency-report (no network)."""

import json

import pytest

import maintenance
import robostack as rs


def jobs(body, association="MEMBER", pull_request=""):
    return maintenance.parse_command(body, association, pull_request)


def test_rebuild_parsing():
    assert jobs("@robostack-bot rebuild ros-jazzy-rclcpp std-msgs jazzy --with-dependents") == [
        {"command": "rebuild", "distro": "", "args": "rclcpp std_msgs jazzy --with-dependents", "preview": False}]
    assert jobs("@robostack-bot rebuild rclcpp all")[0]["args"] == "rclcpp"
    assert jobs("@robostack-bot rebuild jazzy") == []  # no package
    assert jobs("@robostack-bot rebuild rclcpp", association="NONE") == []  # maintainers only


def test_rebuild_dependents_only_on_pull_requests():
    assert jobs("@robostack-bot rebuild-dependents", pull_request="44") == [
        {"command": "rebuild-dependents", "distro": "", "args": "44", "preview": False}]
    assert jobs("@robostack-bot rebuild-dependents") == []  # an issue, not a pull request
    assert jobs("@robostack-bot rebuild-dependents", association="CONTRIBUTOR", pull_request="44") == []


def test_update_vinca_parsing():
    assert jobs("@robostack-bot update-vinca")[0]["args"] == ""
    assert jobs("@robostack-bot update-vinca robostack-integration")[0]["args"] == "robostack-integration"
    assert jobs("@robostack-bot update-vinca $(rm -rf /)") == []


def test_dependency_report_is_per_distribution():
    assert [j["distro"] for j in jobs("@robostack-bot dependency-report all")] == rs.distros()
    assert jobs("@robostack-bot dependency-report jazzy") == [
        {"command": "dependency-report", "distro": "jazzy", "args": "", "preview": False}]


def test_rebuild_plan_follows_host_and_run_dependents():
    reqs = {
        "rclcpp": {"build": [["cmake"]], "host": [["ros2-rcl"]], "run": [[]]},
        "rcl": {"build": [[]], "host": [[]], "run": [[]]},
        "tf2": {"build": [[]], "host": [["ros2-rclcpp"]], "run": [[]]},
        "launch_py": {"build": [[]], "host": [[]], "run": [["ros2-tf2"]]},  # transitively, via run
        "unrelated": {"build": [[]], "host": [["ros2-rcl"]], "run": [[]]},
    }
    assert maintenance.rebuild_plan(reqs, "ros2", ["rclcpp", "missing"], False) == {"rclcpp": "requested"}
    assert maintenance.rebuild_plan(reqs, "ros2", ["rclcpp"], True) == {
        "rclcpp": "requested", "tf2": "depends on rclcpp", "launch_py": "depends on tf2"}


ABI_COMMENT = """<!-- robostack-abi-check -->
### ABI check (linux-64)

#### jazzy linux-64

| package | released → PR | verdict | details |
|---|---|---|---|
| rclcpp | 28.1.0 (build 7) → 28.2.0 | **soname**: ... | ... |

**To rebuild the dependents in this pull request**, add these to `distros/jazzy/pkg_additional_info.yaml` (...):

```yaml
tf2_ros:
  build_number: 9
rclcpp_action:
  build_number: 9
```

Compared with the newest builds on https://repo.prefix.dev/robostack-jazzy/linux-64.

#### humble linux-64

Nothing that needs a rebuild of other packages.

#### rolling linux-64

```yaml
tf2_ros:
  build_number: true
"bad name":
  build_number: 3
nav2_core:
  build_number: 4
```
"""


def test_abi_rebuilds_parses_the_snippets_per_distribution():
    assert maintenance.abi_rebuilds(ABI_COMMENT) == {
        "jazzy": {"tf2_ros": 9, "rclcpp_action": 9},
        "rolling": {"nav2_core": 4},  # invalid names and numbers are dropped
    }
    two_platforms = ABI_COMMENT.replace("#### humble linux-64", "#### jazzy linux-aarch64\n\n```yaml\ntf2_ros:\n  build_number: 11\n```")
    assert maintenance.abi_rebuilds(two_platforms)["jazzy"]["tf2_ros"] == 11


def test_merge_build_numbers_keeps_other_keys(tmp_path):
    info = tmp_path / "pkg_additional_info.yaml"
    info.write_text("tf2_ros:\n  additional_cmake_args: \"-DFOO=ON\"\nnav2_core:\n  build_number: 12\n")
    changed = maintenance.merge_build_numbers(info, {"tf2_ros": 9, "nav2_core": 9, "rclcpp_action": 9})
    assert changed == ["rclcpp_action", "tf2_ros"]  # nav2_core already has a higher number
    assert maintenance.yaml.safe_load(info.read_text()) == {
        "tf2_ros": {"additional_cmake_args": "-DFOO=ON", "build_number": 9},
        "nav2_core": {"build_number": 12}, "rclcpp_action": {"build_number": 9}}
    assert list(maintenance.yaml.safe_load(info.read_text())) == ["nav2_core", "rclcpp_action", "tf2_ros"]
    assert maintenance.merge_build_numbers(info, {"tf2_ros": 9}) == []


def test_rebuild_dependents_from_a_fork_replies_with_the_entries(monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "RoboStack/robostack")
    pull = {"state": "open", "head": {"ref": "main", "repo": {"full_name": "someone/robostack"}}}

    def gh(args):
        if any(a.endswith("/comments") for a in args):
            return json.dumps(ABI_COMMENT) + "\n"
        return json.dumps(pull)

    monkeypatch.setattr(maintenance, "_gh", gh)
    result = maintenance.rebuild_dependents("44")
    assert not result.ok and "fork" in result.title
    assert "tf2_ros:\n  build_number: 9" in result.summary


PIXI = '''[pypi-dependencies]
# One vinca for all distributions.
vinca = { git = "https://github.com/Tobias-Fischer/vinca.git", rev = "cf6833a12f36509a90ea33d25766709bcc1eb2cf" }
# vinca = { path = "../vinca", editable = true }
'''


def test_vinca_pin():
    assert maintenance.vinca_pin(PIXI) == ("Tobias-Fischer/vinca", "cf6833a12f36509a90ea33d25766709bcc1eb2cf")
    updated = maintenance.set_vinca_pin(PIXI, "0123456789abcdef0123456789abcdef01234567")
    assert maintenance.vinca_pin(updated)[1] == "0123456789abcdef0123456789abcdef01234567"
    assert updated.replace("0123456789abcdef0123456789abcdef01234567", "cf6833a12f36509a90ea33d25766709bcc1eb2cf") == PIXI


def test_the_repository_pins_vinca():
    assert maintenance.vinca_pin((rs.ROOT / "pixi.toml").read_text())[0].endswith("/vinca")


def test_vinca_changes():
    compare = {"status": "ahead", "total_commits": 3, "commits": [
        {"sha": "a" * 40, "html_url": "https://github.com/o/vinca/commit/aaa", "commit": {"message": "Fix x\n\nmore"}},
        {"sha": "b" * 40, "html_url": "https://github.com/o/vinca/commit/bbb", "commit": {"message": "Add y"}}]}
    lines = maintenance.vinca_changes("o/vinca", "1" * 40, "2" * 40, compare)
    assert lines[0] == "[o/vinca `1111111111...2222222222`](https://github.com/o/vinca/compare/111111111111...222222222222)"
    assert lines[2:] == ["- [`aaaaaaa`](https://github.com/o/vinca/commit/aaa) Fix x",
                         "- [`bbbbbbb`](https://github.com/o/vinca/commit/bbb) Add y",
                         "- … and 1 more, see the link"]


CHECK_OUTPUT = """(58 solver runs)
conda-forge migration status (conda-forge-pinning 2026.09.01.16.28.00):
  visp  (feedstock: visp; pinned libs: ffmpeg)
    https://github.com/conda-forge/visp-feedstock

Legend: 'done' but still conflicting = ...
"""


def test_report_issue_body():
    data = {
        "pin_conflicts": {},
        "conflicts": {"visp": {"specs": ["visp >=3.7.0, <3.8.0a0"], "recipes": ["visp"], "pins": ["ffmpeg"],
                               "partners": [], "explanation": "visp 3.7.0 would require\n  ffmpeg >=7"}},
        "notes": {"libcamera": {"category": "runtime-glibc"}},
    }
    migrations = maintenance.migration_status(CHECK_OUTPUT)
    assert migrations.startswith("conda-forge migration status") and "Legend" not in migrations
    body = maintenance.report_issue_body("jazzy", data, migrations, "https://run")
    assert body.startswith(maintenance.REPORT_MARKER)
    assert "**1 conflict**" in body
    assert "| `visp` (visp >=3.7.0, <3.8.0a0) | 1: visp | `ffmpeg` |" in body
    assert "`libcamera`" in body and "visp-feedstock" in body and body.endswith("[Workflow run](https://run)")
    clean = maintenance.report_issue_body("jazzy", {}, "")
    assert "No conflicts" in clean and "|---|" not in clean


def test_publish_report(monkeypatch):
    calls = []
    existing = []

    def gh(args):
        calls.append(args)
        if "--jq" in args:
            return "".join(json.dumps(i) + "\n" for i in existing)
        return json.dumps({"html_url": "https://new"})

    monkeypatch.setattr(maintenance, "_gh", gh)
    # nothing to report and no issue: nothing happens
    assert maintenance.publish_report("o/r", "jazzy", "body", conflicts=False) is None
    assert len(calls) == 1
    # conflicts: a new issue
    assert maintenance.publish_report("o/r", "jazzy", "body", conflicts=True) == "https://new"
    assert "title=jazzy: dependency report" in calls[-1] and "labels[]=dependency-report" in calls[-1]
    # an existing issue is updated in place (the open one), and closed without conflicts
    existing[:] = [{"number": 3, "state": "closed", "html_url": "https://3"},
                   {"number": 7, "state": "open", "html_url": "https://7"}]
    assert maintenance.publish_report("o/r", "jazzy", "body", conflicts=False) == "https://7"
    assert calls[-1][:4] == ["api", "-X", "PATCH", "repos/o/r/issues/7"] and "state=closed" in calls[-1]


def test_sync_keeps_repositories_ahead_of_it():
    def entry(repo, version):
        return {"repository": f"https://github.com/x/{repo}", "version": version}

    old = {"easynav": entry("easynav", "0.5.0"), "easynav_tiago": entry("easynav", "0.5.0"),
           "rclcpp": entry("rclcpp", "30.1.0"), "dropped": entry("other", "1.0.0")}
    new = {"easynav": entry("easynav", "0.4.2"), "easynav_gps": entry("easynav", "0.4.2"),
           "rclcpp": entry("rclcpp", "30.2.0"), "added": entry("added", "0.1.0")}
    merged, ahead = maintenance.keep_newer(old, new)
    # the whole easynav repository stays at 0.5.0 (gps is gone there, tiago kept); the rest follows the sync
    assert ahead == ["https://github.com/x/easynav"]
    assert merged == {"added": new["added"], "easynav": old["easynav"], "easynav_tiago": old["easynav_tiago"],
                      "rclcpp": new["rclcpp"]}
    assert maintenance.keep_newer(new, new) == (new, [])
    # dropped by the sync but still released on master: released after the sync, kept
    merged, ahead = maintenance.keep_newer(old, new, {"dropped"})
    assert "https://github.com/x/other" in ahead and merged["dropped"] == old["dropped"]


def test_build_distros_labels_override_the_changed_ones():
    assert rs.build_distros(["humble", "jazzy"], []) == ["humble", "jazzy"]
    assert rs.build_distros(["humble", "jazzy"], ["automerge"]) == ["humble", "jazzy"]
    assert rs.build_distros(rs.distros(), ["build:humble", "automerge"]) == ["humble"]
    assert rs.build_distros([], ["build:rolling", "build:humble"]) == ["humble", "rolling"]
    # only the checks
    assert rs.build_distros(rs.distros(), ["build:none"]) == []
    assert rs.build_distros(rs.distros(), ["build:none", "build:humble"]) == []
    with pytest.raises(SystemExit):
        rs.build_distros([], ["build:kinetic"])
    # platform labels don't select distributions
    assert rs.build_distros(["humble", "jazzy"], ["build:win", "build:linux-64"]) == ["humble", "jazzy"]
    assert rs.build_distros(rs.distros(), ["build:jazzy", "build:osx"]) == ["jazzy"]


def test_build_targets_from_platform_labels():
    platforms = lambda labels: [t["platform"] for t in rs.build_targets(labels)]
    assert platforms([]) == ["linux-64", "linux-aarch64", "osx-64", "osx-arm64", "win-64"]
    assert platforms(["build:jazzy", "automerge"]) == platforms([])
    assert platforms(["build:win"]) == ["win-64"]
    assert platforms(["build:linux", "build:win"]) == ["linux-64", "linux-aarch64", "win-64"]
    assert platforms(["build:osx-arm64"]) == ["osx-arm64"]
    assert rs.build_targets(["build:win"]) == [{"os": "windows-2022", "platform": "win-64"}]


def test_run_build_retries_network_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(rs, "NETWORK_WAIT", 0)
    marker = tmp_path / "failed-once"
    script = tmp_path / "build.sh"
    script.write_text(f'if [ ! -e "{marker}" ]; then touch "{marker}"; echo "Git fetch failed: ..."; exit 1; fi\n')
    assert rs.run_build(["bash", str(script)], rs.ROOT) == 0
    # a real build failure is not retried
    script.write_text('echo "error: expected ;"; exit 1\n')
    assert rs.run_build(["bash", str(script)], rs.ROOT) == 1


def test_build_retries_when_the_channel_index_lags(monkeypatch, tmp_path):
    import subprocess

    outputs = [
        b"Error:   x Failed to resolve dependencies\n"
        b"  `-> Cannot solve the request because of: ros2-rcpputils * cannot be\n"
        b"      installed because there are no viable options:\n"
        b"      |  `-- ros2-distro-mutex 0.21.* lyrical_*, for which no candidates were\n"
        b"      found.\n",
        b"built\n",
    ]
    calls = []

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            calls.append(cmd)
            self.stdout = iter(outputs[len(calls) - 1].splitlines(keepends=True))
            self.returncode = 1 if len(calls) == 1 else 0

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    monkeypatch.setattr(rs.time, "sleep", lambda seconds: None)
    assert rs.run_build(["rattler-build", "build"], rs.ROOT) == 0
    assert len(calls) == 2  # retried once the index has the package


def test_on_anaconda_retries_network_errors(monkeypatch):
    import urllib.error
    import urllib.request
    from pathlib import Path

    calls = []

    def urlopen(request, timeout):
        calls.append(request.full_url)
        if len(calls) < 3:
            raise urllib.error.URLError("[Errno 8] nodename nor servname provided, or not known")
        raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(rs.time, "sleep", lambda seconds: None)
    assert rs.on_anaconda("robostack-lyrical", Path("osx-64/ros2-a-1.0.0-h_30.conda")) is False
    assert len(calls) == 3


def test_uploads_retry_five_times_waiting_longer(monkeypatch):
    calls, waits = [], []
    monkeypatch.setattr(rs, "run", lambda cmd, cwd: calls.append(cmd) or (0 if len(calls) == 5 else 1))
    monkeypatch.setattr(rs.time, "sleep", waits.append)
    assert rs.retry(["upload"], rs.ROOT, "anaconda.org") == 0
    assert waits == [30, 60, 120, 240]


def test_anaconda_retry_skips_an_upload_that_arrived_despite_failing(monkeypatch):
    # the first upload times out (HTTP 524) although anaconda.org stored the file:
    # the retry must not upload it again ("already exists") but find nothing left
    calls, uploaded = [], set()

    def run(cmd, cwd):
        calls.append(cmd)
        uploaded.update(f for f in cmd if f.endswith(".conda"))
        return 1

    monkeypatch.setattr(rs, "run", run)
    monkeypatch.setattr(rs, "on_anaconda", lambda channel, path: str(path.relative_to(rs.ROOT)) in uploaded)
    monkeypatch.setattr(rs, "upload_hosts", lambda distro: ["anaconda"])
    monkeypatch.setattr(rs.time, "sleep", lambda seconds: None)
    monkeypatch.setenv("ANACONDA_API_TOKEN", "test")
    monkeypatch.delenv("ROBOSTACK_UPLOAD_CHANNEL", raising=False)
    assert rs.upload("lyrical", ["osx-64/ros2-a-1.0.0-h_30.conda"], rs.ROOT) == 0
    assert len(calls) == 1


def test_update_pixi_parsing():
    assert jobs("@robostack-bot update-pixi") == [{"command": "update-pixi", "distro": "", "args": "", "preview": False}]
    assert maintenance.parse_command("@robostack-bot update-pixi", "NONE") == []


def test_update_pixi_holds_back_python_and_caps():
    manifest = {"dependencies": {"python": ">=3.14,<3.15", "cmake": "<4.0", "gh": ">=2.1,<3"},
                "target": {"win-64": {"dependencies": {"m2-patch": "*", "foo": {"version": " <2"}}}}}
    assert maintenance.held_back(manifest) == ["cmake", "foo", "python"]
    import tomllib
    assert maintenance.held_back(tomllib.loads((rs.ROOT / "pixi.toml").read_text())) == ["cmake", "python", "setuptools"]


def test_update_pixi_without_changes_opens_no_pr(monkeypatch):
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="{}", stderr="")

    monkeypatch.setattr(maintenance.subprocess, "run", run)
    result = maintenance.update_pixi()
    assert result.ok and not result.changed
    assert calls[0] == ["pixi", "upgrade", "--json", "--exclude", "cmake", "--exclude", "python", "--exclude", "setuptools"]


def test_update_pixi_takes_an_earlier_upgrade(monkeypatch, tmp_path):
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(maintenance.subprocess, "run", run)
    (tmp_path / "upgrade.json").write_text("{}")
    assert not maintenance.update_pixi(tmp_path / "upgrade.json").changed
    assert all(cmd[:2] != ["pixi", "upgrade"] for cmd in calls)  # the bot upgraded before `pixi run`
