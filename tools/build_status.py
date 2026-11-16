"""The "All builds" commit status (.github/workflows/build-status.yaml): every build
workflow of a commit succeeded. Runs without pixi (python3 and gh on the runner).

    python3 tools/build_status.py --sha <commit> --workflows '["Build", "build humble linux-64", ...]'
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

CONTEXT = "All builds"


def runs(repo: str, sha: str) -> list[dict]:
    out = subprocess.run(["gh", "api", "--paginate", f"repos/{repo}/actions/runs?head_sha={sha}&per_page=100",
                          "-q", ".workflow_runs[] | {name, status, conclusion, run_number, html_url}"],
                         capture_output=True, text=True, check=True).stdout
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def state(workflows: list[str], found: list[dict]) -> tuple[str, str, str | None]:
    """(state, description, a run to link): failure as soon as one failed, success when
    all succeeded, else pending. Only the newest run of each workflow counts."""
    newest: dict[str, dict] = {}
    for run in found:
        if run["name"] in workflows and run["run_number"] >= newest.get(run["name"], {}).get("run_number", -1):
            newest[run["name"]] = run
    failed = [r for r in newest.values() if r["status"] == "completed"
              and r["conclusion"] not in ("success", "skipped", "neutral")]
    if failed:
        return "failure", f"{len(failed)} failed: {', '.join(sorted(r['name'] for r in failed))}"[:140], failed[0]["html_url"]
    done = [n for n in workflows if newest.get(n, {}).get("status") == "completed"]
    if len(done) == len(workflows):
        return "success", f"{len(done)} workflows succeeded", None
    return "pending", f"{len(done)} of {len(workflows)} workflows done", None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sha", required=True)
    parser.add_argument("--workflows", required=True, help="JSON list of the workflow names")
    args = parser.parse_args()
    repo = os.environ["GITHUB_REPOSITORY"]
    s, description, url = state(json.loads(args.workflows), runs(repo, args.sha))
    print(f"{s}: {description}")
    cmd = ["gh", "api", f"repos/{repo}/statuses/{args.sha}", "-f", f"state={s}", "-f", f"context={CONTEXT}",
           "-f", f"description={description}"]
    if url:
        cmd += ["-f", f"target_url={url}"]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL)
    return 0


if __name__ == "__main__":
    sys.exit(main())
