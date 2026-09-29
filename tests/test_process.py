"""The guardian's process-lifetime guarantees, carried over from hetoimasia's tools/flake."""
from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from quruntul import process  # noqa: E402
from tests.fixtures.members import _HOLDS_EXITED_MEMBER  # noqa: E402


class GuardianTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="quruntul-guardian-")
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def run_child(self, code, timeout=2, command=None):
        argv = [sys.executable, "-c", code] if command is None else command
        return process.run(argv, self.root, dict(os.environ), self.root / "attempt", timeout, lambda: None, grace=0.1)

    def assert_stopped(self, pid):
        for _ in range(100):
            found = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], text=True, capture_output=True)
            if not found.stdout.strip() or found.stdout.strip().startswith("Z"):
                return
            time.sleep(0.02)
        self.fail(f"owned child {pid} survived")

    def test_success_failure_and_crash_are_distinct(self):
        self.assertEqual(self.run_child("print('evidence')")["outcome"], "passed")
        self.assertEqual(self.run_child("raise SystemExit(1)")["outcome"], "failed")
        self.assertEqual(self.run_child("import os, signal; os.kill(os.getpid(), signal.SIGKILL)")["outcome"], "crashed")

    def test_log_is_retained_with_its_hash(self):
        result = self.run_child("print('hello')")
        self.assertEqual(Path(result["log"]).read_text(), "hello\n")
        self.assertTrue(result["log_sha256"])

    def test_log_ceiling_is_a_harness_error(self):
        result = process.run([sys.executable, "-c", "import sys; sys.stdout.write('x' * 4096); sys.stdout.flush(); "
                              "import time; time.sleep(5)"], self.root, dict(os.environ), self.root / "big", 10,
                             lambda: None, grace=0.1, log_limit=1024)
        self.assertEqual(result["outcome"], "harness-error")

    def test_timeout_reaps_a_stubborn_descendant(self):
        pid = self.root / "child.pid"
        child = (f"import os,signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
                 f"open({str(pid)!r},'w').write(str(os.getpid())); time.sleep(60)")
        code = f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',{child!r}]); time.sleep(60)"
        result = self.run_child(code, timeout=0.5)
        self.assertEqual(result["outcome"], "timeout")
        self.assertTrue(pid.exists())
        self.assert_stopped(int(pid.read_text()))

    def test_timeout_is_kept_when_an_exited_member_stays_unreaped(self):
        status = self.root / "member.status"
        release = self.root / "member.release"
        os.mkfifo(release)
        script = self.root / "hold_exited_member.py"
        script.write_text(_HOLDS_EXITED_MEMBER)
        holder = None
        try:
            # The deadline must outlast the fixture's own setup (two forks and a
            # waitid) on a slow hosted runner; the leader then sleeps into it.
            result = self.run_child("", timeout=8, command=[sys.executable, str(script), str(status), str(release)])
            early = Path(str(status) + ".holder")
            if early.exists():
                holder = int(early.read_text())
            self.assertTrue(status.exists(), "the exited member was not parked before the deadline")
            info = {k: int(v) for k, v in (line.split("=", 1) for line in status.read_text().splitlines())}
            holder = info["holder"]
            self.assertEqual(result["outcome"], "timeout", result.get("error"))
            self.assertNotIn("error", result)
            self.assertFalse(process.has_live_member(info["leader_pgid"]))
            if sys.platform == "darwin":
                self.assertFalse(process.group_exists(info["leader_pgid"]))
            elif sys.platform.startswith("linux"):
                self.assertTrue(process.group_exists(info["leader_pgid"]))
        finally:
            if holder is not None:
                self._release(release, holder)

    def _release(self, release, holder):
        try:
            fd = os.open(release, os.O_WRONLY | os.O_NONBLOCK)
            os.write(fd, b"x")
            os.close(fd)
        except OSError:
            pass
        for _ in range(100):
            try:
                os.kill(holder, 0)
            except ProcessLookupError:
                return
            time.sleep(0.02)
        try:
            os.kill(holder, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def test_a_leaked_child_is_not_a_pass(self):
        code = "import subprocess,sys; subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])"
        self.assertEqual(self.run_child(code)["outcome"], "harness-error")

    def test_parent_death_stops_the_child(self):
        pid = self.root / "child.pid"
        code = f"import os,time; open({str(pid)!r},'w').write(str(os.getpid())); time.sleep(60)"
        wrapper = (f"import os,sys; sys.path.insert(0,{str(HERE.parent)!r}); from pathlib import Path; "
                   f"from quruntul import process; "
                   f"process.run([sys.executable,'-c',{code!r}],Path({str(self.root)!r}),dict(os.environ),"
                   f"Path({str(self.root / 'death')!r}),60,lambda:None,grace=0.1)")
        parent = subprocess.Popen([sys.executable, "-c", wrapper], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            until = time.monotonic() + 10
            while not pid.exists() and parent.poll() is None and time.monotonic() < until:
                time.sleep(0.02)
            self.assertTrue(pid.exists(), parent.stderr.read() if parent.poll() is not None else "child never started")
            parent.kill()
            parent.wait(timeout=5)
            self.assert_stopped(int(pid.read_text()))
        finally:
            if parent.poll() is None:
                parent.kill()
            parent.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main()
