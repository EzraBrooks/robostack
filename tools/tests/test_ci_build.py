"""The PR build must stop its grandchildren before publishing a reusable cache."""

import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX CI supervisor")


def command(output_dir, timeout, script):
    return [sys.executable, str(ROOT / "tools/ci_build.py"), "--output-dir", str(output_dir), "--platform", "linux-64",
            "--timeout", str(timeout), "--", sys.executable, str(script)]


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM, None])
def test_interruption_stops_descendants_and_preserves_only_finished_archives(tmp_path, signum):
    cache = tmp_path / "linux-64"
    cache.mkdir()
    (cache / "restored.conda").write_text("cached")
    script = tmp_path / "build.py"
    # A descendant that ignores graceful termination and keeps modifying the output.
    script.write_text('''import os, signal, subprocess, sys, time
from pathlib import Path
cache = Path(sys.argv[0]).parent / "linux-64"
(cache / "completed.conda").write_text("complete")
print(f"Archive written to '{cache / 'completed.conda'}'", flush=True)
for i in range(3):
    (cache / f"partial-{i}.conda").write_text("incomplete")
child = subprocess.Popen([sys.executable, "-c", """
import signal, sys, time
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
signal.signal(signal.SIGINT, signal.SIG_IGN)
while True:
    Path(sys.argv[1]).write_text(str(time.monotonic_ns()))
    time.sleep(.01)
""", str(cache / "heartbeat")])
(cache / "ready").write_text(str(os.getpid()))
child.wait()
''')
    output = tmp_path / "github-output"
    env = {**os.environ, "GITHUB_OUTPUT": str(output)}
    proc = subprocess.Popen(command(tmp_path, 2 if signum is None else 30, script), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    group = None
    try:
        deadline = time.monotonic() + 10
        while not ((cache / "heartbeat").exists() and (cache / "ready").exists()):
            assert proc.poll() is None, proc.communicate()[0].decode()
            assert time.monotonic() < deadline, "fake build did not start"
            time.sleep(.01)
        group = int((cache / "ready").read_text())
        if signum is not None:
            proc.send_signal(signum)
        stdout, _ = proc.communicate(timeout=10)
        assert proc.returncode == (124 if signum is None else 128 + signum), stdout.decode()
        assert {p.name for p in cache.glob("*.conda")} == {"restored.conda", "completed.conda"}
        assert output.read_text() == "cache-ready=true\n"
        heartbeat = (cache / "heartbeat").read_bytes()
        time.sleep(.1)
        assert (cache / "heartbeat").read_bytes() == heartbeat
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        if group is not None:
            try:
                os.killpg(group, signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.parametrize("lookup_status,ids,deleted", [(0, "11\n22", "11\n22\n"),
                                                       (1, "11", ""), (0, "", "")])
def test_cache_pruning_requires_verified_replacement(tmp_path, lookup_status, ids, deleted):
    workflow = yaml.safe_load((ROOT / ".github/workflows/testpr.yaml").read_text())
    prune = next(s for s in workflow["jobs"]["build"]["steps"]
                 if s.get("name") == "Drop this pull request's superseded build caches")
    pixi = tmp_path / "pixi"
    pixi.write_text('''#!/bin/bash
if [ "$4" = list ]; then
  printf '%s\\n' "$IDS"
  exit "$LOOKUP_STATUS"
fi
[ "$4" = delete ] || exit 99
printf '%s\\n' "$5" >> "$DELETED"
''')
    pixi.chmod(0o755)
    record = tmp_path / "deleted"
    result = subprocess.run(["bash", "-c", prune["run"]], capture_output=True, text=True, timeout=10,
                            env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}",
                                 "IDS": ids, "LOOKUP_STATUS": str(lookup_status), "DELETED": str(record),
                                 "GITHUB_REPOSITORY": "RoboStack/robostack", "GITHUB_REF": "refs/pull/144/merge",
                                 "PREFIX": "cache-", "KEEP": "cache-new"})
    assert result.returncode == 0, result.stderr
    assert (record.read_text() if record.exists() else "") == deleted
