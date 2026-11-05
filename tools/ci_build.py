"""Bound a POSIX PR build and stop all its writers before saving the package cache."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import signal
import subprocess
import sys

from ci_config import cache_dirs


class Interrupted(Exception):
    def __init__(self, signum):
        self.code = 124 if signum == signal.SIGALRM else 128 + signum


def run(cmd: list[str], package_dirs: list[Path], timeout: float) -> int:
    package_dirs = [d.resolve() for d in package_dirs]

    def archives():
        return [p for d in package_dirs for p in d.glob("*.conda")]

    original = {p: (p.stat().st_mtime_ns, p.stat().st_size) for p in archives()}
    completed = set()

    def output(line):
        if match := re.search(rb"Archive written to ['\"](.+\.conda)['\"]", line):
            completed.add(Path(os.fsdecode(match[1])).resolve())
        sys.stdout.buffer.write(line)
        sys.stdout.buffer.flush()

    def interrupt(signum, frame):
        raise Interrupted(signum)

    # Isolate rattler-build, its build scripts and compilers from the runner's signals.
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    previous = {s: signal.signal(s, interrupt) for s in (signal.SIGINT, signal.SIGTERM, signal.SIGALRM)}
    signal.setitimer(signal.ITIMER_REAL, timeout)
    try:
        for line in proc.stdout:
            output(line)
        return proc.wait()
    except Interrupted as exc:
        print(f"Stopping build (exit {exc.code}); preserving completed packages.", flush=True)
        return exc.code
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        for s in previous:
            signal.signal(s, signal.SIG_IGN)
        try:
            # Freeze first: a script must not package an empty build after its compiler dies.
            try:
                os.killpg(proc.pid, signal.SIGSTOP)
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
            for line in proc.stdout:
                output(line)
            proc.stdout.close()
            # Parallel builds can leave several partial archives, not just the newest one.
            for p in archives():
                if p not in completed and original.get(p) != (p.stat().st_mtime_ns, p.stat().st_size):
                    print(f"Removing unfinished archive: {p}", flush=True)
                    p.unlink()
            # A runner-level hard kill must not let a later step save a still-live directory.
            if destination := os.environ.get("GITHUB_OUTPUT"):
                with open(destination, "a") as f:
                    f.write("cache-ready=true\n")
        finally:
            for s, handler in previous.items():
                signal.signal(s, handler)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True, help="rattler-build's output directory")
    parser.add_argument("--platform", required=True)
    parser.add_argument("--timeout", type=float, required=True, help="Build budget in seconds")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.timeout <= 0 or not args.command or args.command[0] != "--" or len(args.command) < 2:
        parser.error("provide a positive timeout and a command after --")
    sys.exit(run(args.command[1:], cache_dirs(args.output_dir, args.platform), args.timeout))
