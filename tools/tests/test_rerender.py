"""tools/rerender.py: the rendered build workflows."""

import yaml

import rerender

# a chain of 40 message packages, each with its compatibility package, and a few more
CHAIN = {f"ros2-m{i:02}": [f"ros2-m{i - 1:02}"] if i else [] for i in range(40)}
REQUIRES = {
    **CHAIN,
    **{f"ros-lyrical-m{i:02}": [f"ros2-m{i:02}"] for i in range(40)},
    **{f"ros2-leaf{i:03}": ["ros2-m05"] for i in range(200)},
}


def test_a_compatibility_package_goes_with_its_package():
    jobs = rerender.batch(REQUIRES, "lyrical")

    job_of = {r: j for j, job in jobs.items() for r in job["recipes"]}
    assert all(job_of[f"ros-lyrical-m{i:02}"] == job_of[f"ros2-m{i:02}"] for i in range(40))
    assert jobs["ros2-m00"]["recipes"] == ["ros2-m00", "ros-lyrical-m00"]


def test_a_job_lists_only_its_nearest_needs():
    requires = {"ros2-a": [], "ros2-b": ["ros2-a"], "ros2-c": ["ros2-a", "ros2-b"]}

    jobs = rerender.batch(requires, "lyrical")

    assert {j: (job["level"], job["needs"]) for j, job in jobs.items()} == {
        "ros2-a": (0, []), "ros2-b": (1, ["ros2-a"]), "ros2-c": (2, ["ros2-b"])}


def test_batches_are_capped():
    jobs = rerender.batch(REQUIRES, "lyrical")

    assert all(len(job["recipes"]) <= rerender.CAP for job in jobs.values())
    leaves = [j for j, job in jobs.items() if job["level"] == 6]
    assert len(leaves) >= 200 // rerender.CAP


def test_a_new_recipe_changes_only_its_own_job():
    before = rerender.batch(REQUIRES, "lyrical")
    after = rerender.batch({**REQUIRES, "ros2-leaf100x": ["ros2-m05"]}, "lyrical")

    changed = {j for j in before.keys() | after.keys() if before.get(j) != after.get(j)}
    assert len(changed) <= 2  # its job, or that one split at it


def test_a_re_render_changes_only_its_distribution(monkeypatch):
    import subprocess

    import robostack as rs

    files = ".github/workflows/build-lyrical-win-64.yaml\ndistros/jazzy/vinca.yaml\n"
    monkeypatch.setattr(rs.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, files, ""))

    assert rs.changed_distros("base") == ["jazzy", "lyrical"]


def test_the_rendered_workflow_reads_back():
    jobs = rerender.batch(REQUIRES, "lyrical")

    text = rerender.render("lyrical", "linux-64", jobs)

    assert rerender.parse(text) == jobs
    doc = yaml.safe_load(text)
    assert list(doc["jobs"]) == ["plan", *sorted(jobs), "collect"]
    assert doc["jobs"]["ros2-m01"]["needs"] == ["plan", "ros2-m00"]
    # collect waits for the jobs no other one needs (and so for all)
    sinks = set(jobs) - {n for job in jobs.values() for n in job["needs"]}
    assert "ros2-m39" in sinks
    assert doc["jobs"]["collect"]["needs"] == ["plan", *sorted(sinks)]
