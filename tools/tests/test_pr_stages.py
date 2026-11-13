"""tools/pr_stages.py: the staged pull-request build's plan and stage slots."""

import yaml

import pr_stages
import robostack as rs


def job(recipes: str) -> dict:
    return {"name": recipes, "recipes": recipes}


def test_fit_numbers_the_jobs_for_their_artifacts():
    assert pr_stages.fit([[job("ros2-a")], [job("ros2-b"), job("ros2-c")]]) == [
        [{"id": "s0-0", "name": "ros2-a", "recipes": "ros2-a"}],
        [{"id": "s1-0", "name": "ros2-b", "recipes": "ros2-b"}, {"id": "s1-1", "name": "ros2-c", "recipes": "ros2-c"}],
    ]


def test_fit_puts_the_stages_past_the_last_slot_into_one_job():
    stages = [[job("ros2-a")], [job("ros2-b")], [job("ros2-c"), job("ros2-d")], [job("ros2-e")]]

    fitted = pr_stages.fit(stages, slots=3)

    assert len(fitted) == 3
    assert [j["recipes"] for j in fitted[2]] == ["ros2-c ros2-d ros2-e"]


def test_fit_shortens_long_job_names():
    (fitted,) = pr_stages.fit([[{"name": "x" * 100, "recipes": "ros2-x"}]])
    assert len(fitted[0]["name"]) == pr_stages.MAX_NAME


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
    monkeypatch.setattr(pr_stages.build_timings, "variant_builds",
                        lambda path, platform: {"ros2-a": 2, "ros2-b": 2})

    assert pr_stages.cached_recipes(recipes, "linux-64", cache) == {"ros2-a", "ros2-c"}


def test_every_slot_waits_for_the_one_before():
    jobs = yaml.safe_load(pr_stages.workflow())["jobs"]

    assert jobs["stage_0"]["needs"] == "plan"
    for k in range(1, pr_stages.SLOTS):
        assert jobs[f"stage_{k}"]["needs"] == ["plan", f"stage_{k - 1}"]
        assert f"> {k} }}" in jobs[f"stage_{k}"]["if"]
    assert jobs["collect"]["needs"] == ["plan", *(f"stage_{k}" for k in range(pr_stages.SLOTS))]


def test_the_workflow_is_up_to_date():
    assert pr_stages.WORKFLOW.read_text() == pr_stages.workflow(), "python tools/pr_stages.py workflow"


def test_rattler_build_reads_the_local_channel(monkeypatch):
    monkeypatch.delenv("ROBOSTACK_UPLOAD_CHANNEL", raising=False)
    monkeypatch.setenv("ROBOSTACK_LOCAL_CHANNEL", "pr-channel")

    cmd = rs.rattler_build("jazzy", [])

    assert cmd[cmd.index("-c") + 1] == (rs.ROOT / "pr-channel").as_uri()


def test_rattler_build_without_the_local_channel(monkeypatch):
    monkeypatch.delenv("ROBOSTACK_LOCAL_CHANNEL", raising=False)
    monkeypatch.delenv("ROBOSTACK_UPLOAD_CHANNEL", raising=False)

    assert not any(c.startswith("file:") for c in rs.rattler_build("jazzy", []))
