"""A leader that parks an exited member of its own process group (from hetoimasia tools/flake)."""

# The leader parks one exited member of its own process group, held by a child
# that has left that group but stayed in the session. waitid observes that
# zombie without reaping it, pipes announce the park, and the release fifo lets
# the fixture reap its helper. The leader then sleeps only so the deadline fires.
_HOLDS_EXITED_MEMBER = r"""
import os
import signal
import sys
import time
from pathlib import Path

status_path = sys.argv[1]
release_path = sys.argv[2]
leader = os.getpid()
held_r, held_w = os.pipe()
ready_r, ready_w = os.pipe()
holder = os.fork()
if holder == 0:
    os.setpgid(0, 0)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    os.close(held_r)
    zombie = os.fork()
    if zombie == 0:
        os.close(held_w)
        os.close(ready_r)
        try:
            os.setpgid(0, leader)
        except OSError as error:
            os.write(ready_w, str(error.errno).encode())
            os.close(ready_w)
            os._exit(1)
        os.write(ready_w, b"1")
        os.close(ready_w)
        os._exit(0)
    try:
        os.setpgid(zombie, leader)
    except ProcessLookupError:
        pass
    os.close(ready_w)
    mark = os.read(ready_r, 16)
    os.close(ready_r)
    if mark != b"1":
        os.write(held_w, b"err\n")
        os.close(held_w)
        os._exit(1)
    # Observe the zombie without reaping it, then announce on the pipe.
    os.waitid(os.P_PID, zombie, os.WEXITED | os.WNOWAIT)
    release = os.open(release_path, os.O_RDWR)
    payload = f"{os.getpid()} {os.getpgid(0)} {os.getsid(0)} {zombie}\n".encode()
    os.write(held_w, payload)
    os.close(held_w)
    os.read(release, 1)
    os.close(release)
    os.waitpid(zombie, 0)
    os._exit(0)
try:
    os.setpgid(holder, holder)
except ProcessLookupError:
    pass
Path(status_path + ".holder").write_text(str(holder))
os.close(held_w)
os.close(ready_r)
os.close(ready_w)
line = os.read(held_r, 128)
os.close(held_r)
if not line or line.startswith(b"err"):
    sys.exit(2)
holder_pid, holder_pgid, holder_sid, zombie = (int(part) for part in line.split())
lines = [
    f"leader={leader}",
    f"leader_pgid={os.getpgid(0)}",
    f"leader_sid={os.getsid(0)}",
    f"holder={holder_pid}",
    f"holder_pgid={holder_pgid}",
    f"holder_sid={holder_sid}",
    f"zombie={zombie}",
]
path = Path(status_path)
temporary = path.with_suffix(path.suffix + ".tmp")
temporary.write_text("\n".join(lines) + "\n")
os.replace(temporary, path)
time.sleep(60)
"""


