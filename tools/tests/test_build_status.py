"""tools/build_status.py: the "All builds" commit status."""

import build_status as bs

WORKFLOWS = ["Build", "build lyrical linux-64", "build lyrical win-64"]


def run(name, status="completed", conclusion="success", number=1):
    return {"name": name, "status": status, "conclusion": conclusion, "run_number": number, "html_url": f"u/{name}/{number}"}


def test_success_when_every_workflow_succeeded():
    assert bs.state(WORKFLOWS, [run(n) for n in WORKFLOWS] + [run("Auto-merge", conclusion="failure")])[0] == "success"


def test_pending_while_one_runs_or_hasnt_started():
    assert bs.state(WORKFLOWS, [run("Build"), run("build lyrical linux-64", "in_progress", None)])[0] == "pending"


def test_only_the_newest_run_of_a_workflow_counts():
    found = [run(n) for n in WORKFLOWS] + [run("build lyrical win-64", conclusion="failure", number=2)]

    s, description, url = bs.state(WORKFLOWS, found)

    assert (s, url) == ("failure", "u/build lyrical win-64/2")
    assert "build lyrical win-64" in description
    assert bs.state(WORKFLOWS, [*found, run("build lyrical win-64", number=3)])[0] == "success"
