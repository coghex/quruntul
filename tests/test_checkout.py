"""A lab checkout is never half-made at its path, and one an interrupted `git worktree add` left is
recovered when it holds only committed content and preserved otherwise.

Interruptions are real: a smudge filter holds `git worktree add` (or the lab calling it) mid-checkout
until the test kills its process group. The fixture repository is a temporary one; nothing needs a
compiler, a display or a network.
"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from unittest.mock import patch

from quruntul import worktree
from quruntul.common import LabError, git as git_output
from quruntul.lab import Lab, STAGING
from tests.test_lab import HERE, LabFixture, git

BINARY = b"\x00\xff\x10 binary \r\n  \t\n\n"
SMUDGE = '''
import os, sys, time
marker = sys.argv[1]
data = sys.stdin.buffer.read()
if os.path.exists(marker + ".armed"):
    open(marker, "w").close()
    time.sleep(120)
sys.stdout.buffer.write(data)
'''
NEVER_COMPLETED = "lab checkout was never completed (interrupted git worktree add)"


def snapshot(path) -> dict:
    """Every entry below path, by relative name, with its exact bytes, symlink target or type."""
    found = {}
    for directory, directories, files in os.walk(path):
        for name in directories + files:
            full = os.path.join(directory, name)
            relative = os.path.relpath(full, path)
            if os.path.islink(full):
                found[relative] = ("link", os.readlink(full))
            elif os.path.isdir(full):
                found[relative] = ("dir", None)
            else:
                found[relative] = ("file", Path(full).read_bytes())
    return found


class CheckoutFixture(LabFixture):
    """A repository whose `slow.dat` is smudged by a filter that can hold the checkout writing it."""

    def setUp(self):
        super().setUp()
        base = Path(self.temp.name)
        self.marker = base / "smudge.marker"
        script = base / "smudge.py"
        script.write_text(SMUDGE)
        git(self.work, "config", "filter.slow.smudge",
            f"{shlex.quote(sys.executable)} {shlex.quote(str(script))} {shlex.quote(str(self.marker))}")
        (self.work / ".gitattributes").write_text("slow.dat filter=slow\n")
        (self.work / "bin.dat").write_bytes(BINARY)
        (self.work / "slow.dat").write_text("slow\n")
        (self.work / "deep" / "inner").mkdir(parents=True)
        (self.work / "deep" / "inner" / "file.txt").write_text("inner\n")
        (self.work / "deep" / "other.txt").write_text("other\n")
        os.symlink("spec.json", self.work / "link")
        git(self.work, "add", "-A")
        self.push("rich fixture")
        self.revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.work, capture_output=True,
                                       text=True, check=True).stdout.strip()
        self.messages = []
        self.lab = Lab(self.work, log=self.messages.append)
        self.base = self.lab.common.parent.parent / f".{self.lab.common.parent.name}-quruntul"
        self.target = self.base / self.revision
        self.staging = self.base / (STAGING + self.revision)

    def tearDown(self):
        self.lab.state.db.close()  # an unclosed connection warns wherever it is collected, into another test's stderr
        super().tearDown()

    def hold(self, argv, cwd=None) -> subprocess.Popen:
        """Start argv in its own process group and return once it is held mid-checkout."""
        self.marker.unlink(missing_ok=True)
        Path(str(self.marker) + ".armed").touch()
        process = subprocess.Popen(argv, cwd=cwd, start_new_session=True, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 60
        while not self.marker.exists():
            if process.poll() is not None or time.monotonic() > deadline:
                self.kill(process)
                self.fail("the checkout was not held at the smudge filter")
            time.sleep(0.02)
        Path(str(self.marker) + ".armed").unlink()
        return process

    def kill(self, process) -> None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()

    def kill_add(self, path) -> None:
        """Really interrupt `git worktree add` of the rich revision at path."""
        if Path(path) == self.staging:
            self.lab._claim(self.staging, self.revision)  # as the lab does before it runs the add
        self.kill(self.hold(["git", "-C", str(self.work), "worktree", "add", "--detach", str(path), self.revision]))

    def lab_process(self) -> subprocess.Popen:
        code = "import sys; from quruntul.lab import Lab; Lab(sys.argv[1], log=lambda m: None).checkout(sys.argv[2])"
        return self.hold([sys.executable, "-B", "-c", code, str(self.work), self.revision], cwd=HERE.parent)

    def registered(self) -> list[str]:
        out = subprocess.run(["git", "-C", str(self.work), "worktree", "list", "--porcelain"],
                             capture_output=True, text=True, check=True).stdout
        return [os.path.realpath(line[len("worktree "):]) for line in out.splitlines() if line.startswith("worktree ")]

    def finished_but_unpublished(self):
        with patch.object(Lab, "_publish", side_effect=RuntimeError("stopped before publication")):
            with self.assertRaises(RuntimeError):
                self.lab.checkout(self.revision)
        self.assertFalse(os.path.lexists(self.target))
        self.assertTrue(self.registration(self.staging).complete)
        (self.staging / "kept.txt").write_text("untracked work in the finished staging checkout\n")

    def orphan_entry(self, name) -> Path:
        """An entry git made for an add that was killed before it wrote a gitdir: only its lock."""
        entry = self.lab.common / "worktrees" / name
        entry.mkdir(parents=True)
        (entry / "locked").write_text("initializing\n")
        return entry

    def registration(self, path):
        return worktree.inspect(self.lab.common, path)

    def refused(self, *needles):
        with self.assertRaises(LabError) as raised:
            self.lab.checkout(self.revision)
        message = str(raised.exception)
        for needle in needles:
            self.assertIn(needle, message)
        return message


class KilledAddTests(CheckoutFixture):
    def test_a_killed_lab_checkout_leaves_nothing_at_the_final_path_and_the_next_call_succeeds(self):
        self.kill(self.lab_process())
        self.assertFalse(os.path.lexists(self.target))
        self.assertTrue(os.path.lexists(self.staging))
        left = self.registration(self.staging)
        self.assertEqual(left.locked, "initializing")
        self.assertFalse(left.complete)
        self.assertIn(os.path.realpath(self.staging), self.registered())

        checkout = self.lab.checkout(self.revision)

        self.assertEqual(checkout, self.target)
        self.assertEqual((checkout / "slow.dat").read_text(), "slow\n")
        self.assertEqual(os.readlink(checkout / "link"), "spec.json")
        self.assertFalse(os.path.lexists(self.staging))
        self.assertEqual(sorted(self.registered()), sorted([os.path.realpath(self.work), os.path.realpath(self.target)]))
        self.assertTrue(self.registration(self.target).complete)
        self.assertFalse(self.registration(self.target).stale)

    def test_the_final_path_is_only_ever_made_by_moving_a_finished_staging_checkout(self):
        moves = []
        real = os.rename

        def rename(source, destination):
            moves.append((Path(source), Path(destination)))
            self.assertTrue(self.registration(Path(source)).complete)
            return real(source, destination)

        with patch("os.rename", side_effect=rename):
            self.lab.checkout(self.revision)
        self.assertEqual(moves, [(self.staging, self.target)])

    def test_a_git_left_running_by_a_killed_lab_keeps_others_out_of_its_staging_checkout(self):
        process = self.lab_process()
        os.kill(process.pid, signal.SIGKILL)  # the lab alone; its git add keeps running
        process.wait()
        before = snapshot(self.staging)
        outcome = {}

        def another_call():
            try:
                outcome["path"] = Lab(self.work, log=lambda _: None).checkout(self.revision)
            except Exception as error:  # reported to the test thread
                outcome["error"] = error

        caller = threading.Thread(target=another_call)
        caller.start()
        try:
            caller.join(2)
            self.assertTrue(caller.is_alive(), f"the second call did not wait for the add: {outcome}")
            self.assertEqual(snapshot(self.staging), before)  # nothing inspected for removal, nothing removed
            self.assertTrue(self.registration(self.staging).admin.exists())
        finally:
            os.killpg(process.pid, signal.SIGKILL)
        caller.join(60)
        self.assertFalse(caller.is_alive())
        self.assertEqual(outcome, {"path": self.target})  # after git ended, the ordinary rules recovered it

    def test_a_half_made_staging_checkout_with_a_registration_and_no_directory_is_cleared(self):
        self.kill_add(self.staging)
        shutil.rmtree(self.staging)
        self.assertIn(os.path.realpath(self.staging), self.registered())
        self.assertEqual(self.lab.checkout(self.revision), self.target)
        self.assertEqual(sorted(self.registered()), sorted([os.path.realpath(self.work), os.path.realpath(self.target)]))

    def test_a_half_made_staging_checkout_whose_registration_is_gone_is_preserved(self):
        self.kill_add(self.staging)
        shutil.rmtree(self.registration(self.staging).admin)
        before = snapshot(self.staging)
        self.refused("unexpected checkout at", str(self.staging), "preserved for inspection")
        self.assertEqual(snapshot(self.staging), before)

    def test_a_staging_checkout_holding_anything_else_is_preserved_not_cleared(self):
        self.kill_add(self.staging)
        (self.staging / "notes.txt").write_text("someone's work\n")
        admin = self.registration(self.staging).admin
        before, before_admin = snapshot(self.staging), snapshot(admin)
        self.refused(NEVER_COMPLETED, str(self.staging), "notes.txt", "preserved for inspection")
        self.assertEqual((snapshot(self.staging), snapshot(admin)), (before, before_admin))
        self.assertFalse(os.path.lexists(self.target))


class IncompleteCheckoutTests(CheckoutFixture):
    """Checkouts whose creation never completed."""

    def test_an_incomplete_staging_checkout_of_committed_content_and_truncations_is_recovered(self):
        self.kill_add(self.staging)  # claimed by the lab first, as the lab does
        registration = self.registration(self.staging)
        self.assertEqual((registration.locked, registration.indexed, registration.complete),
                         ("initializing", False, False))  # both markers
        self.assertTrue(git_output(self.staging, "status", "--porcelain", "--untracked-files=no"))  # git blames "edits"
        (self.staging / "probe.py").write_bytes((self.work / "probe.py").read_bytes()[:20])

        checkout = self.lab.checkout(self.revision)

        self.assertEqual(checkout, self.target)
        self.assertEqual((checkout / "probe.py").read_bytes(), (self.work / "probe.py").read_bytes())
        self.assertEqual(git_output(checkout, "status", "--porcelain", "--untracked-files=no"), "")
        self.assertEqual(sorted(self.registered()), sorted([os.path.realpath(self.work), os.path.realpath(self.target)]))
        self.assertFalse(os.path.lexists(self.staging))
        self.assertTrue(any("never-completed" in message for message in self.messages))

    def test_a_flake_through_the_cli_completes_over_a_killed_labs_incomplete_checkout_without_a_manual_step(self):
        self.kill(self.lab_process())
        self.assertFalse(self.registration(self.staging).complete)
        flaked = self.cli("flake", "--target", "unit")
        self.assertEqual(flaked["outcome"], "complete")
        self.assertTrue(self.registration(self.target).complete)

    def test_an_incomplete_final_path_has_no_claim_and_is_preserved_however_pristine(self):
        """An earlier engine's half-made checkout: nothing the lab recorded before its add can vouch for it."""
        self.kill_add(self.target)
        admin = self.registration(self.target).admin
        before, before_admin = snapshot(self.target), snapshot(admin)
        self.assertEqual(worktree.pristine_problems(self.lab.root, self.target, self.revision), [])
        message = self.refused(NEVER_COMPLETED, str(self.target), str(admin), "preserved for inspection")
        self.assertNotIn("has edits", message)
        self.assertEqual((snapshot(self.target), snapshot(admin)), (before, before_admin))
        self.assertFalse(self.lab._claim_file(self.revision).exists())  # the refusal manufactured no ownership
        flaked = self.cli("flake", "--target", "unit", code=2)
        self.assertIn(NEVER_COMPLETED, flaked["error"])
        self.assertEqual(snapshot(self.target), before)

    def test_an_incomplete_checkout_that_is_not_pristine_is_refused_and_left_untouched(self):
        def replace(path, make):
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)
            elif os.path.lexists(path):
                os.unlink(path)
            make(path)

        cases = {
            "an edited file": ("bin.dat", lambda path: Path(path).write_bytes(b"edited")),
            "a longer file": ("bin.dat", lambda path: Path(path).write_bytes(BINARY + b"more")),
            "an extra file": ("extra.txt", lambda path: Path(path).write_text("extra\n")),
            "a directory where the commit has a file": ("probe.py", lambda path: os.mkdir(path)),
            "a file where the commit has a directory": ("deep", lambda path: Path(path).write_text("x")),
            "a symlink with another target": ("link", lambda path: os.symlink("elsewhere", path)),
        }
        for name, (offender, make) in cases.items():
            with self.subTest(name):
                self.kill_add(self.staging)
                replace(self.staging / offender, make)
                admin = self.registration(self.staging).admin
                before, before_admin = snapshot(self.staging), snapshot(admin)
                message = self.refused(NEVER_COMPLETED, str(self.staging), offender, "preserved for inspection")
                self.assertNotIn("has edits", message)
                self.assertEqual((snapshot(self.staging), snapshot(admin)), (before, before_admin))
                self.assertIn(os.path.realpath(self.staging), self.registered())
                shutil.rmtree(self.staging)
                shutil.rmtree(admin)

    def test_identity_is_refused_before_anything_is_removed(self):
        other = subprocess.run(["git", "rev-parse", "HEAD~1"], cwd=self.work, capture_output=True,
                               text=True, check=True).stdout.strip()
        for name, head, message in (("another revision", other + "\n", "unexpected checkout at"),
                                    ("an attached head", "ref: refs/heads/master\n", "no longer detached")):
            with self.subTest(name):
                self.kill_add(self.target)
                admin = self.registration(self.target).admin
                (admin / "HEAD").write_text(head)
                before, before_admin = snapshot(self.target), snapshot(admin)
                self.refused(message, str(self.target))
                self.assertEqual((snapshot(self.target), snapshot(admin)), (before, before_admin))
                shutil.rmtree(self.target)
                shutil.rmtree(admin)

    def test_a_foreign_repository_at_the_path_is_refused_and_untouched(self):
        self.target.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(self.target)], check=True)
        (self.target / "mine.txt").write_text("not the lab's\n")
        before = snapshot(self.target)
        self.refused("unexpected checkout at", str(self.target))
        self.assertEqual(snapshot(self.target), before)

    def test_a_bare_directory_with_no_trace_of_a_worktree_add_is_preserved(self):
        """Committed-looking contents do not show a directory is abandoned lab state; git's own record does."""
        for path in (self.staging, self.target):
            with self.subTest(path.name):
                path.mkdir(parents=True)
                (path / "probe.py").write_bytes((self.work / "probe.py").read_bytes()[:20])
                before = snapshot(path)
                self.refused("unexpected checkout at", str(path), "preserved for inspection")
                self.assertEqual(snapshot(path), before)
                shutil.rmtree(path)

    def test_an_add_killed_before_git_wrote_its_links_is_preserved_even_under_the_labs_claim(self):
        """Git made the directory, and perhaps an entry holding only its lock; nothing yet links the two."""
        for name, make in (("no entry", lambda: None),
                           ("an entry with only the lock", lambda: self.orphan_entry("-staging-" + self.revision)),
                           ("an empty gitdir link", lambda: (self.orphan_entry("-staging-" + self.revision) / "gitdir")
                            .write_text(""))):
            with self.subTest(name):
                shutil.rmtree(self.lab.common / "worktrees" / ("-staging-" + self.revision), ignore_errors=True)
                self.staging.mkdir(parents=True)
                make()
                self.lab._claim(self.staging, self.revision)
                entries = snapshot(self.lab.common / "worktrees")
                before = snapshot(self.staging)
                self.refused("unexpected checkout at", str(self.staging), "preserved for inspection")
                self.assertEqual((snapshot(self.staging), snapshot(self.lab.common / "worktrees")), (before, entries))
                shutil.rmtree(self.staging)

    def test_a_staging_directory_whose_git_marker_was_begun_but_not_written_is_preserved(self):
        self.staging.mkdir(parents=True)
        (self.staging / ".git").write_text("")
        self.lab._claim(self.staging, self.revision)
        before = snapshot(self.staging)
        self.refused("unexpected checkout at", str(self.staging))
        self.assertEqual(snapshot(self.staging), before)

    def test_an_empty_git_marker_does_not_hide_the_registration_that_names_the_path(self):
        clone = Path(self.temp.name) / "clone"
        subprocess.run(["git", "clone", "-q", str(self.work), str(clone)], check=True)
        other = git_output(self.work, "rev-parse", "HEAD~1")

        def at_another_revision(admin):
            (admin / "HEAD").write_text(other + "\n")

        def of_another_repository(admin):
            (admin / "commondir").write_text(str(clone / ".git") + "\n")

        for name, damage, message in (("another revision", at_another_revision, "unexpected checkout at"),
                                      ("another repository", of_another_repository, "unexpected checkout at")):
            with self.subTest(name):
                self.kill_add(self.staging)
                self.lab._claim(self.staging, self.revision)
                admin = self.registration(self.staging).admin
                damage(admin)
                (self.staging / ".git").write_text("")
                before, before_admin = snapshot(self.staging), snapshot(admin)
                self.refused(message, str(self.staging))
                self.assertEqual((snapshot(self.staging), snapshot(admin)), (before, before_admin))
                shutil.rmtree(self.staging)
                shutil.rmtree(admin)

        with self.subTest("incomplete"):
            self.kill_add(self.staging)
            self.lab._claim(self.staging, self.revision)
            (self.staging / ".git").write_text("")
            self.assertEqual(self.lab.checkout(self.revision), self.target)
            self.assertEqual(sorted(self.registered()), sorted([os.path.realpath(self.work),
                                                                os.path.realpath(self.target)]))
            git(self.work, "worktree", "remove", "--force", str(self.target))

        with self.subTest("completed"):
            self.finished_but_unpublished()
            (self.staging / ".git").write_text("")
            admin = self.registration(self.staging).admin
            self.assertTrue(self.registration(self.staging).complete)
            before, before_admin = snapshot(self.staging), snapshot(admin)
            self.refused("not provably this path's", str(self.staging), str(admin))
            self.assertEqual((snapshot(self.staging), snapshot(admin)), (before, before_admin))

    def test_an_add_killed_while_writing_commondir_is_recovered_but_a_foreign_one_is_not(self):
        self.kill_add(self.staging)
        self.lab._claim(self.staging, self.revision)
        admin = self.registration(self.staging).admin
        (admin / "commondir").write_text("")
        self.assertFalse(self.registration(self.staging).foreign)
        self.assertEqual(self.lab.checkout(self.revision), self.target)
        self.assertEqual(sorted(self.registered()), sorted([os.path.realpath(self.work), os.path.realpath(self.target)]))

    def test_only_the_labs_claim_of_this_path_and_revision_lets_a_staging_checkout_be_removed(self):
        self.kill_add(self.staging)
        claim = dict(path=os.path.realpath(self.staging), revision=self.revision,
                     common=os.path.realpath(self.lab.common))
        admin = self.registration(self.staging).admin
        before, before_admin = snapshot(self.staging), snapshot(admin)
        self.lab._release(self.revision)
        attempts = [("no claim", None)] + [
            (field, dict(claim, **{field: value})) for field, value in (
                ("path", str(self.base / "other")), ("revision", "0" * 40),
                ("common", str(Path(self.temp.name) / "other.git")))]
        for name, written in attempts:
            with self.subTest(name):
                if written is not None:
                    self.lab._claim_file(self.revision).parent.mkdir(parents=True, exist_ok=True)
                    self.lab._claim_file(self.revision).write_text(json.dumps(written))
                self.refused(NEVER_COMPLETED, str(self.staging), str(admin), "preserved for inspection")
                self.assertEqual((snapshot(self.staging), snapshot(admin)), (before, before_admin))
        self.lab._claim(self.staging, self.revision)
        self.assertEqual(self.lab.checkout(self.revision), self.target)

    def test_a_registration_that_links_back_to_another_path_is_never_removed_or_rewritten(self):
        """A copy of an interrupted worktree, .git marker and all, put at the expected path."""
        for path in (self.staging, self.target):
            with self.subTest(path.name):
                original = Path(self.temp.name) / "original" / self.revision
                self.kill_add(original)
                (original / "notes.txt").write_text("the original's own notes\n")
                admin = self.registration(original).admin
                self.lab._claim(self.staging, self.revision)
                shutil.copytree(original, path, symlinks=True)
                self.assertEqual(self.registration(path).admin, admin)
                before = [snapshot(p) for p in (original, path, admin)]
                self.refused(NEVER_COMPLETED, str(path), str(admin), "preserved for inspection")
                self.assertEqual([snapshot(p) for p in (original, path, admin)], before)
                self.assertIn(os.path.realpath(original), self.registered())
                git_output(original, "status", "--porcelain")  # the original still works as a worktree
                shutil.rmtree(path)
                shutil.rmtree(original)
                shutil.rmtree(admin)

    def test_a_same_named_worktree_elsewhere_does_not_tie_a_bare_directory_to_the_repository(self):
        elsewhere = Path(self.temp.name) / "elsewhere" / self.revision
        self.kill_add(elsewhere)
        entry = self.registration(elsewhere).admin
        orphan = self.orphan_entry(self.revision + "1")  # named as git numbers a taken name, with no gitdir yet
        self.target.mkdir(parents=True)
        (self.target / "probe.py").write_bytes((self.work / "probe.py").read_bytes()[:20])
        before, before_entry, before_elsewhere = snapshot(self.target), snapshot(entry), snapshot(elsewhere)
        self.refused("unexpected checkout at", str(self.target))
        self.assertEqual((snapshot(self.target), snapshot(entry), snapshot(elsewhere)),
                         (before, before_entry, before_elsewhere))
        self.assertTrue(orphan.is_dir())

    def test_an_entry_whose_common_directory_is_another_repository_is_preserved(self):
        clone = Path(self.temp.name) / "clone"
        subprocess.run(["git", "clone", "-q", str(self.work), str(clone)], check=True)
        for path in (self.staging, self.target):
            with self.subTest(path.name):
                self.kill_add(path)
                admin = self.registration(path).admin
                (admin / "commondir").write_text(str(clone / ".git") + "\n")
                self.assertTrue(self.registration(path).foreign)
                before, before_admin, before_clone = snapshot(path), snapshot(admin), snapshot(clone / ".git")
                self.refused("unexpected checkout at", str(path))
                self.assertEqual((snapshot(path), snapshot(admin), snapshot(clone / ".git")),
                                 (before, before_admin, before_clone))
                shutil.rmtree(path)
                shutil.rmtree(admin)

    def test_either_marker_detects_an_incomplete_checkout_but_only_both_allow_recovery(self):
        def lock_only(admin):  # an index is there, the initializing lock is not yet gone
            (admin / "index").write_bytes(b"x")

        def index_only(admin):  # no index, the lock is gone
            (admin / "locked").unlink()

        for name, damage in (("lock only", lock_only), ("index only", index_only)):
            with self.subTest(name):
                self.kill_add(self.staging)
                admin = self.registration(self.staging).admin
                damage(admin)
                self.assertFalse(self.registration(self.staging).complete)
                before, before_admin = snapshot(self.staging), snapshot(admin)
                self.refused(NEVER_COMPLETED, str(self.staging), str(admin), "only one marker")
                self.assertEqual((snapshot(self.staging), snapshot(admin)), (before, before_admin))
                shutil.rmtree(self.staging)
                shutil.rmtree(admin)

    def test_a_complete_looking_final_checkout_that_lost_its_index_is_preserved(self):
        checkout = self.lab.checkout(self.revision)
        admin = self.registration(checkout).admin
        (admin / "index").unlink()
        before, before_admin = snapshot(checkout), snapshot(admin)
        self.refused(NEVER_COMPLETED, str(self.target))
        self.assertEqual((snapshot(checkout), snapshot(admin)), (before, before_admin))


class CompleteCheckoutTests(CheckoutFixture):
    def test_a_complete_checkout_with_edits_is_refused_with_the_edits_message_and_untouched(self):
        checkout = self.lab.checkout(self.revision)
        (checkout / "spec.json").write_text("{}")
        (checkout / "built.o").write_text("untracked\n")
        before, before_admin = snapshot(checkout), snapshot(self.registration(checkout).admin)
        message = self.refused("lab checkout has edits; preserve and inspect", str(self.target), "it is never reset")
        self.assertNotIn("never completed", message)
        self.assertEqual((snapshot(checkout), snapshot(self.registration(checkout).admin)), (before, before_admin))

    def test_a_complete_checkout_is_reused_and_an_owner_lock_does_not_make_it_incomplete(self):
        checkout = self.lab.checkout(self.revision)
        git(self.work, "worktree", "lock", "--reason", "mine", str(checkout))
        self.assertTrue(self.registration(checkout).complete)
        before = snapshot(checkout)
        self.assertEqual(self.lab.checkout(self.revision), checkout)
        self.assertEqual(snapshot(checkout), before)
        self.assertEqual(self.registration(checkout).locked, "mine")

    def test_a_checkout_made_under_the_old_layout_keeps_working(self):
        git(self.work, "worktree", "add", "--detach", str(self.target), self.revision)
        self.assertEqual(self.lab.checkout(self.revision), self.target)
        self.assertEqual(self.registered().count(os.path.realpath(self.target)), 1)


class PublishBoundaryTests(CheckoutFixture):
    def test_a_finished_staging_checkout_is_resumed_not_deleted_or_reset(self):
        self.finished_but_unpublished()
        self.assertEqual(self.lab.checkout(self.revision), self.target)
        self.assertEqual((self.target / "kept.txt").read_text(), "untracked work in the finished staging checkout\n")
        self.assertFalse(os.path.lexists(self.staging))
        self.assertEqual(sorted(self.registered()), sorted([os.path.realpath(self.work), os.path.realpath(self.target)]))
        self.assertFalse(self.registration(self.target).stale)

    def test_a_publication_stopped_after_the_move_is_made_consistent(self):
        self.finished_but_unpublished()
        os.rename(self.staging, self.target)
        self.assertTrue(self.registration(self.target).stale)
        self.assertEqual(self.lab.checkout(self.revision), self.target)
        self.assertFalse(self.registration(self.target).stale)
        self.assertEqual(sorted(self.registered()), sorted([os.path.realpath(self.work), os.path.realpath(self.target)]))
        self.assertEqual((self.target / "kept.txt").read_text(), "untracked work in the finished staging checkout\n")

    def test_a_publication_stopped_after_the_move_is_not_repaired_without_the_labs_claim(self):
        self.finished_but_unpublished()
        admin = self.registration(self.staging).admin
        os.rename(self.staging, self.target)
        self.lab._release(self.revision)
        before, before_admin = snapshot(self.target), snapshot(admin)
        self.refused("not provably this path's", str(self.target), str(admin))
        self.assertEqual((snapshot(self.target), snapshot(admin)), (before, before_admin))

    def test_a_completed_staging_checkout_copied_to_the_final_path_leaves_the_original_registration_alone(self):
        self.finished_but_unpublished()
        admin = self.registration(self.staging).admin
        shutil.copytree(self.staging, self.target, symlinks=True)
        before = [snapshot(p) for p in (self.staging, self.target, admin)]
        self.refused("lab staging checkout exists beside", str(self.staging))
        self.assertEqual([snapshot(p) for p in (self.staging, self.target, admin)], before)
        self.assertIn(os.path.realpath(self.staging), self.registered())
        self.lab._release(self.revision)
        self.refused(str(self.staging))
        self.assertEqual([snapshot(p) for p in (self.staging, self.target, admin)], before)

    def test_a_publication_whose_reverse_link_was_truncated_is_preserved_and_refused(self):
        self.finished_but_unpublished()
        admin = self.registration(self.staging).admin
        (admin / "gitdir").write_text("")
        os.rename(self.staging, self.target)
        before, before_admin = snapshot(self.target), snapshot(admin)
        self.refused("not provably this path's", str(self.target), str(admin), "records no location")
        self.assertEqual((snapshot(self.target), snapshot(admin)), (before, before_admin))

    def test_a_finished_staging_checkout_with_a_truncated_reverse_link_is_preserved_and_refused(self):
        self.finished_but_unpublished()
        admin = self.registration(self.staging).admin
        (admin / "gitdir").write_text("")
        before, before_admin = snapshot(self.staging), snapshot(admin)
        self.refused("not provably this path's", str(self.staging), str(admin))
        self.assertEqual((snapshot(self.staging), snapshot(admin)), (before, before_admin))
        self.assertFalse(os.path.lexists(self.target))

    def test_a_moved_worktree_that_is_not_our_publication_is_never_repaired(self):
        other = git_output(self.work, "rev-parse", "HEAD~1")
        for name, add, message in (("another revision", ["--detach", "{place}", other], "unexpected checkout at"),
                                   ("an attached head", ["-b", "attached", "{place}", self.revision],
                                    "no longer detached")):
            with self.subTest(name):
                place = self.base / "elsewhere"
                git(self.work, "worktree", "add", *[a.format(place=place) for a in add])
                admin = self.registration(place).admin
                os.rename(place, self.target)
                self.assertTrue(self.registration(self.target).stale)
                before, before_admin = snapshot(self.target), snapshot(admin)
                self.refused(message)
                self.assertEqual((snapshot(self.target), snapshot(admin)), (before, before_admin))
                shutil.rmtree(self.target)
                shutil.rmtree(admin)

    def test_a_publication_of_the_wrong_revision_is_refused_before_it_is_repaired(self):
        self.finished_but_unpublished()
        admin = self.registration(self.staging).admin
        (admin / "HEAD").write_text(git_output(self.work, "rev-parse", "HEAD~1") + "\n")
        os.rename(self.staging, self.target)
        before_admin = snapshot(admin)
        self.refused("unexpected checkout at", str(self.target))
        self.assertEqual(snapshot(admin), before_admin)

    def test_a_finished_staging_checkout_beside_a_final_one_is_refused_untouched(self):
        self.finished_but_unpublished()
        git(self.work, "worktree", "add", "--detach", str(self.target), self.revision)
        before = snapshot(self.staging)
        self.refused("lab staging checkout exists beside", str(self.staging))
        self.assertEqual(snapshot(self.staging), before)


class ListingTests(CheckoutFixture):
    def test_checkouts_lists_only_complete_revision_checkouts(self):
        parent = git_output(self.work, "rev-parse", "HEAD~1")
        complete = self.lab.checkout(parent)
        self.kill_add(self.target)  # half-made at a final path
        self.kill_add(self.staging)  # and at a temporary one
        (self.base / "stray-directory").mkdir()
        listed = self.lab.checkouts()
        self.assertEqual([item["path"] for item in listed], [str(complete)])
        self.assertEqual([item["revision"] for item in listed], [parent])

    def test_checkouts_does_not_list_a_staging_leftover_through_the_cli(self):
        self.kill_add(self.staging)
        self.assertEqual(self.cli("checkouts"), [])
        self.assertEqual(self.cli("checkouts", "--prune", "--apply"), [])
        self.assertTrue(os.path.lexists(self.staging))  # prune stays non-forcing and does not reach it


class PristineTests(CheckoutFixture):
    """The byte-exact rule, on a complete checkout of the rich revision that each case then damages."""

    def setUp(self):
        super().setUp()
        self.tree = self.lab.checkout(self.revision)

    def problems(self):
        return worktree.pristine_problems(self.lab.root, self.tree, self.revision)

    def test_a_complete_checkout_and_a_partly_written_one_are_pristine(self):
        self.assertEqual(self.problems(), [])
        (self.tree / "spec.json").unlink()
        (self.tree / "deep" / "inner" / "file.txt").unlink()
        (self.tree / "deep" / "other.txt").write_bytes(b"")
        shutil.rmtree(self.tree / "deep" / "inner")
        (self.tree / "deep" / "inner").mkdir()
        self.assertEqual(self.problems(), [])

    def test_every_byte_prefix_of_binary_content_is_pristine_and_nothing_else_is(self):
        for length in range(len(BINARY) + 1):
            (self.tree / "bin.dat").write_bytes(BINARY[:length])
            self.assertEqual(self.problems(), [], length)
        (self.tree / "bin.dat").write_bytes(BINARY + b" ")
        self.assertEqual(len(self.problems()), 1)
        (self.tree / "bin.dat").write_bytes(BINARY.rstrip())
        self.assertEqual(self.problems(), [])
        (self.tree / "bin.dat").write_bytes(BINARY[:-1] + b"\r")
        self.assertIn("bin.dat: content differs", self.problems()[0])

    def test_symlinks_are_compared_by_target_without_being_followed(self):
        (self.tree / "link").unlink()
        os.symlink("spec.json", self.tree / "link")
        self.assertEqual(self.problems(), [])
        (self.tree / "link").unlink()
        os.symlink("probe.py", self.tree / "link")
        self.assertEqual(self.problems(), ["link: symlink target differs from the commit"])
        (self.tree / "link").unlink()
        os.symlink("../outside-dir", self.tree / "link")
        (self.tree / "link").unlink()
        os.symlink("spec.json", self.tree / "link")
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        (outside / "secret").write_text("x")
        shutil.rmtree(self.tree / "deep" / "inner")
        (self.tree / "deep" / "inner").symlink_to(outside)
        self.assertEqual(self.problems(), ["deep/inner: a symlink where the commit has something else"])

    def test_a_path_of_the_wrong_type_is_not_pristine(self):
        (self.tree / "probe.py").unlink()
        (self.tree / "probe.py").mkdir()
        self.assertEqual(self.problems(), ["probe.py: a directory where the commit has a file"])
        (self.tree / "probe.py").rmdir()
        (self.tree / "probe.py").symlink_to("spec.json")
        self.assertEqual(self.problems(), ["probe.py: a symlink where the commit has something else"])
        (self.tree / "probe.py").unlink()
        (self.tree / "link").unlink()
        (self.tree / "link").write_text("spec.json")
        self.assertEqual(self.problems(), ["link: a file where the commit has something else"])

    def test_the_gitdir_marker_is_ignored_and_a_stranger_is_named(self):
        self.assertTrue((self.tree / ".git").is_file())
        (self.tree / "deep" / "stranger.bin").write_bytes(b"\x00")
        self.assertEqual(self.problems(), ["deep/stranger.bin: not in the committed tree"])
