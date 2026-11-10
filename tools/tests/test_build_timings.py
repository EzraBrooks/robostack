import json
import subprocess
from pathlib import Path

import build_timings


def test_note_records_build_times(tmp_path, monkeypatch):
    monkeypatch.setattr(build_timings.rs, "work_dir", lambda distro: tmp_path)
    build_timings.note("jazzy", "ros2-a", 12.34)
    build_timings.note("jazzy", "ros-jazzy-a", 1.0)
    build_timings.note("jazzy", "ros2-a", 2.0, upload=True)
    entries = [json.loads(line) for line in (tmp_path / build_timings.JOURNAL).read_text().splitlines()]
    assert [(e["recipe"], e["seconds"], e.get("upload", False)) for e in entries] == [
        ("ros2-a", 12.3, False), ("ros-jazzy-a", 1.0, False), ("ros2-a", 2.0, True)]
    assert all(e["platform"] for e in entries)


def test_collect_merges_the_runs_journals(tmp_path, monkeypatch):
    monkeypatch.setattr(build_timings, "TIMINGS", tmp_path / "timings")
    runs = [{"databaseId": 2, "headBranch": "buildbranch_jazzy_linux_64", "createdAt": "2026-10-22T02:00:00Z"},
            {"databaseId": 1, "headBranch": "buildbranch_jazzy_linux_64", "createdAt": "2026-10-22T01:00:00Z"},
            {"databaseId": 3, "headBranch": "buildbranch_lyrical_linux_64", "createdAt": "2026-10-22T03:00:00Z"}]
    # (recipe, seconds, upload): the uploads come after the job's builds
    journals = {1: [("ros2-a", 10.0, False), ("ros2-b", 5.0, False), ("ros-jazzy-a", 0.2, False), ("ros2-b", 1.0, True)],
                2: [("ros2-a", 12.0, False), ("ros2-a", 2.0, True), ("ros-jazzy-a", 1.0, True)],
                3: [("ros2-a", 20.0, False)]}

    def run(cmd, **kwargs):
        if cmd[1:3] == ["run", "list"]:
            return subprocess.CompletedProcess(cmd, 0, json.dumps(runs), "")
        run_id = int(cmd[3])
        job = Path(cmd[cmd.index("--dir") + 1]) / "stage_0_job_0-1"
        job.mkdir(parents=True)
        (job / build_timings.JOURNAL).write_text("".join(
            json.dumps({"platform": "linux-64", "recipe": r, "seconds": s, **({"upload": True} if u else {})}) + "\n"
            for r, s, u in journals[run_id]))
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(build_timings.subprocess, "run", run)
    assert build_timings.collect() == 4  # the compatibility recipe is not kept
    # the newer jazzy run wins; each distribution keeps its own time, build plus uploads;
    # compatibility recipes aren't kept, their uploads count for ros2-<name>; one recipe per line
    text = (tmp_path / "timings" / "linux-64.json").read_text()
    assert json.loads(text) == {"ros2-a": {"jazzy": 15.0, "lyrical": 20.0}, "ros2-b": {"jazzy": 6.0}}
    assert text.splitlines()[1] == '"ros2-a": {"jazzy": 15.0, "lyrical": 20.0},'
    # vinca-gha gets the average over the distributions
    out = tmp_path / "vinca.json"
    assert build_timings.vinca_timings("humble", "linux-64", out)
    assert json.loads(out.read_text()) == {
        "recipes": {"ros2-a": 17.5, "ros2-b": 6.0, "ros-humble-a": build_timings.ALIAS_SECONDS,
                    "ros-humble-b": build_timings.ALIAS_SECONDS},
        "default": 11.75}
    assert not build_timings.vinca_timings("humble", "win-64", out)


def test_update_build_timings_command(tmp_path, monkeypatch):
    import maintenance

    assert maintenance.parse_command("@robostack-bot update-build-timings", "MEMBER") == [
        {"command": "update-build-timings", "distro": "", "args": "", "preview": False}]
    assert maintenance.parse_command("@robostack-bot update-build-timings", "NONE") == []
    monkeypatch.setattr(build_timings, "TIMINGS", tmp_path)

    def collect():
        (tmp_path / "linux-64.json").write_text('{"ros2-a": {"jazzy": 1.0}}\n')
        return 1

    monkeypatch.setattr(build_timings, "collect", collect)
    result = maintenance.update_build_timings()
    assert result.changed and "linux-64.json" in result.summary
    assert result.labels == ["build:none"]  # only the checks: no package changes
    assert not maintenance.update_build_timings().changed  # nothing new: no PR


def test_gha_keeps_the_journal_and_uses_the_times(tmp_path, monkeypatch):
    import robostack as rs

    work, root = tmp_path / "work", tmp_path / "root"
    work.mkdir()
    (root / ".github" / "workflows").mkdir(parents=True)
    calls = []

    def fake_run_tee(cmd, cwd):
        calls.append(cmd)
        (work / "linux.yml").write_text("name: build\njobs:\n  stage_0_job_0:\n    steps: []\n")
        return 0, b""

    monkeypatch.setattr(rs, "run_tee", fake_run_tee)
    monkeypatch.setattr(rs, "work_dir", lambda distro: work)
    monkeypatch.setattr(rs, "ROOT", root)
    monkeypatch.setattr(build_timings, "TIMINGS", tmp_path / "timings")
    assert rs.generate_gha("jazzy", ["--platform", "linux-64"]) == 0
    assert "--keep-file" in calls[-1] and "--timings" not in calls[-1]  # no times yet
    (tmp_path / "timings").mkdir()
    (tmp_path / "timings" / "linux-64.json").write_text('{"ros2-a": {"jazzy": 3.0}}\n')
    assert rs.generate_gha("jazzy", ["--platform", "linux-64"]) == 0
    assert calls[-1][calls[-1].index("--timings") + 1] == "build_timings.json"
    assert json.loads((work / "build_timings.json").read_text()) == {
        "recipes": {"ros2-a": 3.0, "ros-jazzy-a": build_timings.ALIAS_SECONDS}, "default": 3.0}


def test_ci_estimate_uses_the_measured_times(tmp_path, monkeypatch, capsys):
    import ci_estimate

    timings = tmp_path / "timings"
    timings.mkdir()
    (timings / "linux-64.json").write_text(build_timings.dumps(
        {"ros2-a": {"jazzy": 100.0, "lyrical": 200.0}, "ros2-b": {"jazzy": 10.0}}))
    monkeypatch.setattr(build_timings, "TIMINGS", timings)
    recipes = tmp_path / "distros" / "jazzy" / "work" / "recipes"
    for r in ("ros2-a", "ros-jazzy-a", "ros2-b", "ros2-new"):
        (recipes / r).mkdir(parents=True)
        (recipes / r / "recipe.yaml").write_text("")
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "ros2-b-1.0-h0_1.conda").write_text("")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", "")
    monkeypatch.setattr("sys.argv", ["ci_estimate.py", "--recipes", str(recipes), "--cache-dir", str(cache),
                                     "--platform", "linux-64"])
    assert ci_estimate.main() == 0
    # ros2-a 150 s (average), its alias 0.3 s, ros2-new the median 80 s, ros2-b cached 2 s
    assert capsys.readouterr().out.startswith("3 packages to build, 1 restored from the cache: roughly 4 min")


def test_build_ci_parallel_times_each_build(tmp_path, monkeypatch, capsys):
    import robostack

    def line(t, recipe, message, span="Running build for"):
        spans = [{"recipe": recipe, "name": span}] if recipe else []
        return (json.dumps({"timestamp": f"2026-10-24T07:00:{t:02d}.0Z", "fields": {"message": message},
                            "spans": spans}) + "\n").encode()

    log = [line(0, "", "rattler-build 0.77.0"), line(1, "ros2-a-1.0-h0_1", "Fetching source"),
           line(5, "ros2-b-1.0-h0_1", "Fetching source"), line(9, "ros2-a-1.0-h0_1", "Artifact"),
           line(20, "ros2-b-1.0-h0_1", "Artifact"), line(21, "ros-jazzy-a-1.0-h0_1", "Fetching source"),
           line(40, "ros-jazzy-a-1.0-h0_1", "Artifact"), b"not json\n",
           line(30, "ros2-a-1.0-h0_1", "summary", span="Build summary for")]
    shown = []

    def run_build(cmd, cwd, show=None):
        assert "--parallel-builds" in cmd and "json" in cmd
        for entry in log:
            show(entry)
        return 0

    noted = []
    monkeypatch.setattr(robostack, "run_build", run_build)
    monkeypatch.setattr(robostack, "rattler_build", lambda distro, args, skip_existing=True: ["rattler-build", *args])
    monkeypatch.setattr(build_timings, "note", lambda distro, recipe, seconds: noted.append((recipe, seconds)))
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert robostack.build_ci_parallel("jazzy", ["ros2-a", "ros2-b", "ros-jazzy-a"],
                                       ["--recipe", "a", "--recipe", "b", "--recipe", "c"], tmp_path) == 0
    # from each recipe's first to its last line in the build (8 s, 15 s), not the summary at
    # the end, scaled to add up to the builds' wall time (19 s): overlapping builds share it;
    # not the compatibility recipe
    assert [(r, round(s, 2)) for r, s in noted] == [("ros2-a", 6.61), ("ros2-b", 12.39)]
    out = capsys.readouterr().out
    assert "07:00:01 [ros2-a] Fetching source" in out and "not json" in out


def test_build_ci_parallel_names_the_unfinished_builds(tmp_path, monkeypatch, capsys):
    import robostack

    def line(t, recipe, message):
        return (json.dumps({"timestamp": f"2026-10-24T07:00:{t:02d}.0Z", "fields": {"message": message},
                            "spans": [{"recipe": recipe, "name": "Running build for"}]}) + "\n").encode()

    def run_build(cmd, cwd, show=None):
        for entry in (line(1, "ros2-a-1.0-h0_1", "Fetching source"), line(2, "ros2-b-1.0-h0_1", "Applying patch"),
                      line(3, "ros2-a-1.0-h0_1", "Archive written to '/bld/ros2-a-1.0-h0_1.conda'"),
                      b"Error:   x Patch application error\n"):
            show(entry)
        return 1

    monkeypatch.setattr(robostack, "run_build", run_build)
    monkeypatch.setattr(robostack, "rattler_build", lambda distro, args, skip_existing=True: ["rattler-build", *args])
    assert robostack.build_ci_parallel("rolling", ["ros2-a", "ros2-b"], ["--recipe", "a", "--recipe", "b"], tmp_path) == 1
    assert "Started but not finished: ros2-b" in capsys.readouterr().err


VARIANT_CONFIG = """\
python:
  - 3.12.* *_cpython   # [not riscv64]
  - 3.13.* *_cp313     # [not riscv64]
  - 3.14.* *_cp314
is_python_min:
  - true               # [not riscv64]
  - false              # [not riscv64]
  - false              # [not riscv64]
  - true               # [riscv64]
hdf5:
  - '2'
  - 1.14.6
zip_keys:
  -   - python
      - is_python_min
"""


def variant_recipes(tmp_path, distro="lyrical"):
    work = tmp_path / "distros" / distro / "work"
    (work / "recipes").mkdir(parents=True)
    (work / "conda_build_config.yaml").write_text(VARIANT_CONFIG)
    recipes = {
        # C++ only: Python pinned to python_min in build, Python only for cross-compiling
        "ros2-cpp": "requirements:\n  build:\n    - python ${{ python_min }}.*\n"
                    "    - if: build_platform != target_platform\n      then:\n        - python\n"
                    "  host:\n    - eigen\n",
        "ros2-py": "requirements:\n  host:\n    - python\n    - numpy\n  run:\n    - python\n",
        "ros2-py-hdf5": "requirements:\n  host:\n    - python\n    - hdf5\n",
        "ros2-win-py": "requirements:\n  host:\n    - if: win\n      then:\n        - python\n",
    }
    for name, text in recipes.items():
        (work / "recipes" / name).mkdir()
        (work / "recipes" / name / "recipe.yaml").write_text(text)
    return work / "recipes"


def test_variant_builds_counts_the_python_versions(tmp_path):
    recipes = variant_recipes(tmp_path)
    assert build_timings.variant_builds(recipes, "linux-64") == {
        "ros2-cpp": 1, "ros2-py": 3, "ros2-py-hdf5": 6, "ros2-win-py": 1}
    assert build_timings.variant_builds(recipes, "win-64")["ros2-win-py"] == 3
    # conda-forge has only 3.14 for riscv64: python and is_python_min are zipped, one variant
    assert build_timings.variant_builds(recipes, "linux-riscv64")["ros2-py"] == 1


def test_ci_estimate_counts_each_variant(tmp_path, monkeypatch, capsys):
    import ci_estimate

    recipes = variant_recipes(tmp_path)
    timings = tmp_path / "timings"
    timings.mkdir()
    (timings / "linux-64.json").write_text(build_timings.dumps(
        {"ros2-cpp": {"lyrical": 60.0}, "ros2-py": {"lyrical": 100.0}, "ros2-py-hdf5": {"lyrical": 10.0},
         "ros2-win-py": {"lyrical": 30.0}}))
    monkeypatch.setattr(build_timings, "TIMINGS", timings)
    cache = tmp_path / "cache"
    cache.mkdir()
    for f in ("ros2-py-1.0-py312h0_1.conda", "ros2-cpp-1.0-h0_1.conda"):
        (cache / f).write_text("")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", "")
    monkeypatch.setattr("sys.argv", ["ci_estimate.py", "--recipes", str(recipes), "--cache-dir", str(cache),
                                     "--platform", "linux-64"])
    assert ci_estimate.main() == 0
    # ros2-py: 2 of 3 builds left, 200 s; ros2-py-hdf5 6 x 10 s; ros2-win-py 30 s; 2 cached x 2 s
    out = capsys.readouterr().out
    assert out.startswith("9 packages to build (3 recipes, 2 of them in several variants, e.g. one per "
                          "Python version), 2 restored from the cache: roughly 5 min")


def test_vinca_timings_count_each_variant(tmp_path, monkeypatch):
    recipes = variant_recipes(tmp_path)
    timings = tmp_path / "timings"
    timings.mkdir()
    (timings / "linux-64.json").write_text(build_timings.dumps(
        {"ros2-cpp": {"lyrical": 60.0}, "ros2-py": {"lyrical": 100.0}}))
    monkeypatch.setattr(build_timings, "TIMINGS", timings)
    out = recipes.parent / "build_timings.json"
    assert build_timings.vinca_timings("lyrical", "linux-64", out)
    data = json.loads(out.read_text())
    # measured: per build times the builds; unmeasured with several builds: the default times them
    assert data["recipes"]["ros2-cpp"] == 60.0 and data["recipes"]["ros2-py"] == 300.0
    assert data["recipes"]["ros2-py-hdf5"] == 6 * data["default"]
    assert "ros2-win-py" not in data["recipes"]


def test_build_ci_parallel_times_each_variant(tmp_path, monkeypatch):
    import robostack

    def line(t, build, message):
        return (json.dumps({"timestamp": f"2026-10-24T07:00:{t:02d}.0Z", "fields": {"message": message},
                            "spans": [{"recipe": build, "name": "Running build for"}]}) + "\n").encode()

    # ros2-a's two Python variants side by side (10 s each), then ros2-b alone (10 s)
    log = [line(0, "ros2-a-1.0-py312h0_1", "Fetching source"), line(0, "ros2-a-1.0-py313h1_1", "Fetching source"),
           line(10, "ros2-a-1.0-py312h0_1", "Artifact"), line(10, "ros2-a-1.0-py313h1_1", "Artifact"),
           line(10, "ros2-b-1.0-h0_1", "Fetching source"), line(20, "ros2-b-1.0-h0_1", "Artifact")]

    def run_build(cmd, cwd, show=None):
        for entry in log:
            show(entry)
        return 0

    noted = []
    monkeypatch.setattr(robostack, "run_build", run_build)
    monkeypatch.setattr(robostack, "rattler_build", lambda distro, args, skip_existing=True: ["rattler-build", *args])
    monkeypatch.setattr(build_timings, "note", lambda distro, recipe, seconds: noted.append((recipe, seconds)))
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert robostack.build_ci_parallel("lyrical", ["ros2-a", "ros2-b"], [], tmp_path) == 0
    # 30 s of builds in 20 s: each build's share, ros2-a's the average of its two builds
    assert [(r, round(s, 2)) for r, s in noted] == [("ros2-a", 6.67), ("ros2-b", 6.67)]
