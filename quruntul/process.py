"""Guardian process: deadlines and parent death cannot strand a test process group.

The coordinator may hand this process a lock descriptor; the guardian retains
it until its child group has been terminated and reaped. EOF on the
parent's pipe means the coordinator died; the group is then cancelled. No PID
from a previous run is ever signalled. A returned result is fsynced before exit.

The guardian reaps only its direct child and signals only that child's group.
An exited member can remain unreaped. Linux kill(2) still succeeds for a
zombie-only group, so the group counts as present until those zombies are
reaped. Darwin killpg raises EPERM instead. That EPERM means no live member
only when every listed member is a zombie, or none remain; a live or unreadable
member is reported as a cleanup failure. Exited members alone are not a leak.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import time

sys.dont_write_bytecode = True
# The guardian runs under -I, which leaves the package off sys.path; add only
# the directory holding this trusted package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from quruntul.common import atomic_json, file_hash, utc  # noqa: E402


def members(pgid):
    """Stat strings for one process group, or None when the listing is unusable."""
    try:
        found = subprocess.run(
            ["ps", "-ax", "-o", "pid=", "-o", "stat=", "-o", "pgid="],
            capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if found.returncode != 0:
        return None
    stats = []
    for line in found.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) != 3:
            return None
        try:
            row_pgid = int(parts[2])
        except ValueError:
            return None
        if row_pgid == pgid:
            stats.append(parts[1])
    return stats


def exited_only(pgid):
    """True when every remaining member is a zombie or the group lists none."""
    stats = members(pgid)
    if stats is None:
        return None
    return all(stat.startswith("Z") for stat in stats)


def group_exists(pgid):
    """Whether kill(2) can address this group.

    Linux succeeds for a zombie-only group. Darwin raises EPERM; that is absence
    of a live member only when every listed member is a zombie or none remain.
    """
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        if exited_only(pgid) is True:
            return False
        raise
    return True


def has_live_member(pgid):
    """Whether a member is still running. Exited members are not a leak."""
    if not group_exists(pgid):
        return False
    exited = exited_only(pgid)
    if exited is None:
        raise PermissionError(f"cannot read process group {pgid}")
    return not exited


def send(pgid, sig):
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        pass
    except PermissionError:
        # Nothing running can be signalled. A live or unreadable member must surface.
        if exited_only(pgid) is not True:
            raise


def reap_group(process, grace):
    send(process.pid, signal.SIGTERM)
    end = time.monotonic() + grace
    while time.monotonic() < end:
        process.poll()  # Reap the leader, too; its zombie is not a live child.
        if not group_exists(process.pid):
            return
        time.sleep(0.02)
    send(process.pid, signal.SIGKILL)
    process.wait(timeout=5)


def supervise(spec):
    cancelled = False

    def stop(_sig, _frame):
        nonlocal cancelled
        cancelled = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    started = utc()
    before = time.monotonic()
    result = dict(schema_version=1, token=spec["token"], started=started, command=spec["command"],
                  cwd=spec["cwd"], log=spec["log"], outcome="setup-error", returncode=None)
    log = Path(spec["log"])
    try:
        with log.open("wb") as output:
            child = subprocess.Popen(spec["command"], cwd=spec["cwd"], env=spec["environment"],
                                     stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                                     start_new_session=True)
            try:
                reason = None
                while child.poll() is None:
                    readable, _, _ = select.select([sys.stdin], [], [], 0.05)
                    if cancelled or (readable and os.read(sys.stdin.fileno(), 1) == b""):
                        reason = "interrupted"
                        break
                    if log.stat().st_size > spec.get("log_limit", 64 * 1024 * 1024):
                        reason = "harness-error"
                        result["error"] = f"log exceeded {spec.get('log_limit', 64 * 1024 * 1024)} bytes"
                        break
                    if time.monotonic() - before >= spec["timeout"]:
                        reason = "timeout"
                        break
                if reason:
                    reap_group(child, spec["grace"])
                    result["outcome"] = reason
                else:
                    # A successful leader leaving a live descendant is a broken
                    # probe, not a passing test. Exited members are not a leak.
                    # A zombie-only group still exists on Linux, so clean it up
                    # without reporting a leak.
                    leaked = has_live_member(child.pid)
                    if leaked or group_exists(child.pid):
                        reap_group(child, spec["grace"])
                    result["outcome"] = ("harness-error" if leaked else
                                         "passed" if child.returncode == 0 else
                                         "crashed" if child.returncode < 0 else "failed")
                result["returncode"] = child.wait(timeout=5)
            finally:
                if child.poll() is None or group_exists(child.pid):
                    reap_group(child, spec["grace"])
            output.flush()
            os.fsync(output.fileno())
    except Exception as error:
        result["outcome"] = "harness-error"
        result["error"] = f"{type(error).__name__}: {error}"
    result.update(finished=utc(), duration_seconds=time.monotonic() - before,
                  log_sha256=file_hash(log) if log.exists() else None)
    atomic_json(Path(spec["result"]), result)
    return 0


def run(command, cwd, environment, artifact, timeout, heartbeat, lock_fd=None, grace=1.0, log_limit=64 * 1024 * 1024):
    """Run a bounded child, retaining a recoverable result before DB ingestion."""
    token = artifact.name
    spec = dict(token=token, command=command, cwd=str(cwd), environment=environment,
                timeout=timeout, grace=grace, log_limit=log_limit, log=str(artifact.with_suffix(".log")),
                result=str(artifact.with_suffix(".result.json")))
    # Environment stays out of retained manifests; it may contain credentials.
    guardian = subprocess.Popen([sys.executable, "-I", str(Path(__file__).resolve()), "--guardian"],
                                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, pass_fds=(lock_fd,) if lock_fd is not None else ())
    try:
        guardian.stdin.write((json.dumps(spec) + "\n").encode())
        guardian.stdin.flush()
        last = 0.0
        while guardian.poll() is None:
            now = time.monotonic()
            if now - last >= 5:
                heartbeat()
                last = now
            time.sleep(0.05)
        if guardian.returncode:
            raise RuntimeError(f"guardian exited {guardian.returncode}: {guardian.stderr.read().decode(errors='replace')}")
        result = json.loads(Path(spec["result"]).read_text())
        if result["token"] != token:
            raise RuntimeError("guardian returned another attempt's result")
        return result
    finally:
        guardian.stdin.close()
        try:
            guardian.wait(timeout=grace + 10)
        except subprocess.TimeoutExpired:
            # This is our guardian. Ask it to stop; never signal a stale PID.
            guardian.terminate()
            guardian.wait(timeout=grace + 10)
        guardian.stderr.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--guardian", action="store_true", required=True)
    parser.parse_args()
    # -I isolates imports, so explicitly add only this trusted script directory.
    raise SystemExit(supervise(json.loads(sys.stdin.buffer.readline())))
