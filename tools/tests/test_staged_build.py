"""tools/staged_build.py: the staged build's schedule and stage jobs."""

import sys

import pytest

import robostack as rs
import staged_build as sb


def test_cached_recipes_need_every_variant_in_the_cache(monkeypatch, tmp_path):
    recipes, cache = tmp_path / "recipes", tmp_path / "cache"
    for name in ("ros2-a", "ros2-b", "ros2-c"):
        (recipes / name).mkdir(parents=True)
        (recipes / name / "recipe.yaml").write_text("{}\n")
    (cache / "linux-64").mkdir(parents=True)
    (cache / "noarch").mkdir()
    (cache / "linux-64" / "ros2-a-1.0.0-py312_0.conda").touch()
    (cache / "linux-64" / "ros2-a-1.0.0-py313_0.conda").touch()
    (cache / "linux-64" / "ros2-b-1.0.0-py312_0.conda").touch()
    (cache / "noarch" / "ros2-c-1.0.0-0.conda").touch()
    monkeypatch.setattr(sb.build_timings, "variant_builds", lambda path, platform: {"ros2-a": 2, "ros2-b": 2})

    assert sb.cached_recipes(recipes, "linux-64", cache) == {"ros2-a", "ros2-c"}


def test_the_schedule_keeps_the_recipes_to_build():
    jobs = {"ros2-a": {"level": 0, "recipes": ["ros2-a", "ros-lyrical-a", "ros2-b"], "needs": []},
            "ros2-c": {"level": 1, "recipes": ["ros2-c"], "needs": ["ros2-a"]}}
    # generate-recipes -n left out ros2-b (on the channel); ros2-a is in the cache
    requires = {"ros2-a": [], "ros-lyrical-a": ["ros2-a"], "ros2-c": ["ros2-a", "ros2-b"]}

    planned = sb.schedule(jobs, requires, {"ros2-a"})

    assert planned == {
        "ros2-a": {"recipes": "ros-lyrical-a", "needs": [], "requires": {"ros2-a": [], "ros-lyrical-a": ["ros2-a"]}},
        "ros2-c": {"recipes": "ros2-c", "needs": ["ros2-a"], "requires": {"ros2-c": ["ros2-a", "ros2-b"]}},
    }
    # ros2-b isn't built in this run, so it doesn't hold back ros2-c
    assert sb.blocked(planned["ros2-c"], {"ros-lyrical-a": "success"}) == {}


def test_a_recipe_in_no_job_needs_a_re_render():
    with pytest.raises(SystemExit, match="rerender"):
        sb.schedule({"ros2-a": {"level": 0, "recipes": ["ros2-a"], "needs": []}}, {"ros2-a": [], "ros2-new": []}, set())


def test_a_job_needs_its_requirements_jobs_and_theirs():
    jobs = {"a": {"needs": []}, "b": {"needs": ["a"]}, "c": {"needs": []}, "d": {"needs": ["b"]}}

    assert sb.job_needs(jobs, "d") == ["a", "b"]


def test_a_failed_recipe_blocks_only_the_recipes_that_need_it(tmp_path):
    jobs = {
        "s0": {"recipes": "ros2-zstd ros2-mola", "needs": [], "requires": {}},
        "s1": {"recipes": "ros2-a ros2-b ros2-c ros2-d", "needs": ["s0"],
               "requires": {"ros2-a": ["ros2-zstd"], "ros2-b": ["ros2-mola"], "ros2-c": ["ros2-b"], "ros2-d": ["ros2-x"]}},
        "s2": {"recipes": "ros2-x", "needs": [], "requires": {}},
    }
    (tmp_path / "status").mkdir()
    (tmp_path / "status" / "s0.json").write_text('{"ros2-zstd": "success", "ros2-mola": "failed"}')

    # s2's artifact is missing: it didn't finish
    status = sb.needed_status(jobs, ["s0", "s2"], tmp_path)

    assert status == {"ros2-zstd": "success", "ros2-mola": "failed", "ros2-x": "not built"}
    # ros2-c needs ros2-b, held back in the same job
    assert sb.blocked(jobs["s1"], status) == {"ros2-b": "ros2-mola", "ros2-c": "ros2-b", "ros2-d": "ros2-x"}


LOG = """\
+ rattler-build build --recipe ./recipes/ros2-a  (in distros/lyrical/work)
10:00:00 [ros2-a] Build failed for ros2-a-1.0.0-np2py312h0_31: network
rattler-build exited with 1
Network error; retrying in 60 s (retry 1 of 4)
+ rattler-build build --recipe ./recipes/ros2-a  (in distros/lyrical/work)
10:01:00 [ros2-a] Archive written to 'output/linux-64/ros2-a-1.0.0-np2py312h0_31.conda'
10:02:00 [ros2-mola] Build failed for ros2-mola-3.3.0-np2py312hdd89193_31: Script failed to execute
10:02:00 [-] Skipping ros-lyrical-mola-3.3.0-h9490d1a_31 because its dependency ros2-mola failed to build
10:03:00 [ros2-t] Test failed for ros2-t-1.0.0-h0_31: import failed
"""


def test_each_recipe_s_outcome_from_the_last_attempt():
    job = {"recipes": "ros2-a ros2-mola ros-lyrical-mola ros2-t ros-lyrical-t ros2-held", "requires": {"ros-lyrical-t": ["ros2-t"]}}

    # ros-lyrical-t only runs with ros2-t: rattler-build built it
    assert sb.outcomes(job, {"ros2-held": "ros2-x"}, LOG) == {
        "ros2-a": "success", "ros2-mola": "failed", "ros-lyrical-mola": "skipped", "ros2-t": "failed",
        "ros-lyrical-t": "skipped", "ros2-held": "blocked",
    }


def test_every_recipe_failed_when_rattler_build_itself_did():
    log = "+ rattler-build build\n10:00:00 [ros2-a] Archive written to 'x'\nrattler-build exited with 1\n"
    job = {"recipes": "ros2-a ros2-b", "requires": {}}

    assert sb.outcomes(job, {}, log) == {"ros2-a": "failed", "ros2-b": "failed"}
    assert sb.outcomes(job, {"ros2-b": "ros2-x"}, None) == {"ros2-a": "failed", "ros2-b": "blocked"}


def test_unpack_merges_the_artifacts(tmp_path):
    import zipfile

    zips = tmp_path / "zips"
    zips.mkdir()
    for name, files in {"pkgs-a": ["linux-64/ros2-a-1-0.conda", "status/a"], "pkgs-b": ["noarch/ros2-b-1-0.conda", "status/b"]}.items():
        with zipfile.ZipFile(zips / f"{name}.zip", "w") as z:
            for f in files:
                z.writestr(f, "x")

    sb.unpack(zips, tmp_path / "channel")

    assert sorted(p.relative_to(tmp_path / "channel").as_posix() for p in (tmp_path / "channel").rglob("*") if p.is_file()) == [
        "linux-64/ros2-a-1-0.conda", "noarch/ros2-b-1-0.conda", "status/a", "status/b"]
    assert not list(zips.iterdir())


def test_a_build_that_failed_under_continue_on_failure_fails(monkeypatch, tmp_path, capsys):
    script = "import sys; print('{\"fields\": {\"message\": \"Build failed for ros2-a-1-0: x\"}}')"
    monkeypatch.setattr(rs, "ROOT", tmp_path)

    assert rs.run_build([sys.executable, "-c", script], tmp_path) == 1
    assert rs.run_build([sys.executable, "-c", "print('ok')"], tmp_path) == 0
    assert rs.run_build([sys.executable, "-c", "raise SystemExit(3)"], tmp_path) == 3
    assert "exited with 3" in capsys.readouterr().err


def test_rattler_build_reads_the_local_channel(monkeypatch):
    monkeypatch.delenv("ROBOSTACK_UPLOAD_CHANNEL", raising=False)
    monkeypatch.setenv("ROBOSTACK_LOCAL_CHANNEL", "pr-channel")

    cmd = rs.rattler_build("jazzy", [])

    assert cmd[cmd.index("-c") + 1] == (rs.ROOT / "pr-channel").as_uri()


def test_rattler_build_without_the_local_channel(monkeypatch):
    monkeypatch.delenv("ROBOSTACK_LOCAL_CHANNEL", raising=False)
    monkeypatch.delenv("ROBOSTACK_UPLOAD_CHANNEL", raising=False)

    assert not any(c.startswith("file:") for c in rs.rattler_build("jazzy", []))
