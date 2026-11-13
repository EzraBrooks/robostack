"""tools/staged_build.py: the staged build's plans, schedule and stage slots."""

import sys

import yaml

import robostack as rs
import staged_build as sb


def job(id: str, recipes: str, needs=()) -> dict:
    return {"id": id, "name": recipes, "recipes": recipes, "needs": list(needs)}


def plan(distro: str, platform: str, stages: list[list[dict]]) -> dict:
    return {"distro": distro, "platform": platform, "os": "ubuntu-latest", "stages": stages}


def test_fold_puts_the_stages_past_the_last_slot_into_one_job():
    stages = [[job("s0", "ros2-a")], [job("s1", "ros2-b", ["s0"])],
              [job("s2a", "ros2-c", ["s1"]), job("s2b", "ros2-d", ["s0"])], [job("s3", "ros2-e", ["s2a"])]]

    folded = sb.fold(stages, slots=3)

    assert len(folded) == 3
    (tail,) = folded[2]
    assert tail["recipes"] == "ros2-c ros2-d ros2-e"
    assert tail["needs"] == ["s0", "s1"]


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


def test_schedule_shares_the_slots_between_distributions_and_platforms():
    plans = [plan("jazzy", "linux-64", [[job("stage_0_job_0", "ros2-a")], [job("stage_1_job_0", "ros2-b")]]),
             plan("lyrical", "win-64", [[job("stage_0_job_0", "ros2-a"), job("stage_0_job_1", "ros2-c")]])]

    entries, jobs = sb.schedule(plans)

    assert [[(e["distro"], e["platform"], e["job"]) for e in slot] for slot in entries] == [
        [("jazzy", "linux-64", "stage_0_job_0"), ("lyrical", "win-64", "stage_0_job_0"),
         ("lyrical", "win-64", "stage_0_job_1")],
        [("jazzy", "linux-64", "stage_1_job_0")],
    ]
    assert jobs["lyrical/win-64"]["stage_0_job_1"] == {"recipes": "ros2-c", "needs": [], "requires": {}}


def test_schedule_merges_jobs_past_the_matrix_limit():
    stages = [[job("a", "ros2-a"), job("b", "ros2-b ros2-bb"), job("c", "ros2-c")], [job("d", "ros2-d", ["c"])]]
    stages[0][2]["requires"] = {"ros2-c": []}
    stages[1][0]["requires"] = {"ros2-d": ["ros2-c"]}

    entries, jobs = sb.schedule([plan("jazzy", "linux-64", stages)], max_matrix=2)

    assert [e["job"] for e in entries[0]] == ["a", "b"]
    assert jobs["jazzy/linux-64"]["a"]["recipes"] == "ros2-a ros2-c"
    assert jobs["jazzy/linux-64"]["a"]["requires"] == {"ros2-c": []}
    assert jobs["jazzy/linux-64"]["d"]["needs"] == ["a"]


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


def test_every_slot_runs_after_the_one_before():
    jobs = yaml.safe_load(sb.workflow())["jobs"]

    assert jobs["stage_0"]["needs"] == "schedule"
    for k in range(1, sb.SLOTS):
        assert jobs[f"stage_{k}"]["needs"] == ["schedule", f"stage_{k - 1}"]
        assert f"> {k} }}" in jobs[f"stage_{k}"]["if"]
    assert jobs["collect"]["needs"] == ["schedule", *(f"stage_{k}" for k in range(sb.SLOTS))]
    assert set(jobs["schedule"]["outputs"]) == {"count", "combos", *(f"slot_{k}" for k in range(sb.SLOTS))}


def test_the_workflow_is_up_to_date():
    assert sb.WORKFLOW.read_text() == sb.workflow(), "python tools/staged_build.py workflow"


def test_rattler_build_reads_the_local_channel(monkeypatch):
    monkeypatch.delenv("ROBOSTACK_UPLOAD_CHANNEL", raising=False)
    monkeypatch.setenv("ROBOSTACK_LOCAL_CHANNEL", "pr-channel")

    cmd = rs.rattler_build("jazzy", [])

    assert cmd[cmd.index("-c") + 1] == (rs.ROOT / "pr-channel").as_uri()


def test_rattler_build_without_the_local_channel(monkeypatch):
    monkeypatch.delenv("ROBOSTACK_LOCAL_CHANNEL", raising=False)
    monkeypatch.delenv("ROBOSTACK_UPLOAD_CHANNEL", raising=False)

    assert not any(c.startswith("file:") for c in rs.rattler_build("jazzy", []))
