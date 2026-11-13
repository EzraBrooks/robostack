"""tools/staged_build.py: the staged build's plans, schedule and stage slots."""

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
    assert jobs["lyrical/win-64"]["stage_0_job_1"] == {"recipes": "ros2-c", "needs": []}


def test_schedule_merges_jobs_past_the_matrix_limit():
    stages = [[job("a", "ros2-a"), job("b", "ros2-b ros2-bb"), job("c", "ros2-c")], [job("d", "ros2-d", ["c"])]]

    entries, jobs = sb.schedule([plan("jazzy", "linux-64", stages)], max_matrix=2)

    assert [e["job"] for e in entries[0]] == ["a", "b"]
    assert jobs["jazzy/linux-64"]["a"]["recipes"] == "ros2-a ros2-c"
    assert jobs["jazzy/linux-64"]["d"]["needs"] == ["a"]


def test_a_job_needs_its_requirements_jobs_and_theirs():
    jobs = {"a": {"needs": []}, "b": {"needs": ["a"]}, "c": {"needs": []}, "d": {"needs": ["b"]}}

    assert sb.job_needs(jobs, "d") == ["a", "b"]
    assert sb.pattern("jazzy", "linux-64", ["a", "b"]) == "pkgs-jazzy-linux-64-{cache,a,b}"
    assert sb.pattern("jazzy", "linux-64", []) == "pkgs-jazzy-linux-64-cache"


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
