"""A flake batch under a hard per-trial deadline (--trial-deadline), end to end through the CLI.

The fixture repository is tests/test_lab.py's: a fake Hspec suite `unit` (trial_seconds 30,
batch_seconds 300, rts_capabilities [1, 4]) and a command probe `probe-a`. Deadlines here are
a few seconds at most, and the fixture hangs, dies or exits nonzero at chosen trials.
"""
from __future__ import annotations

import hashlib
import itertools
import json
from pathlib import Path

from quruntul import lab as lab_module
from quruntul.state import State
from tests.test_lab import EVENTS, HOOKED_ADAPTER, LEGACY, LabFixture, git

ROW = "unit::A/one"


class DeadlineTests(LabFixture):
    def setUp(self):
        super().setUp()
        # One example, enumerated and passing, so each batch below starts from a stable test.
        self.spec(dict(examples=["A/one"]))
        self.push("one stable example")
        self.cli("flake", "--target", "unit", "--trials", "1")
        self.assertEqual(self.statuses()[ROW], "stable")

    def faulty(self, **faults):
        self.spec(dict(examples=["A/one"], **faults))
        self.push("fault " + ", ".join(faults))

    def batch(self, trials, *flags, target=ROW, code=0):
        return self.cli("flake", "--target", target, "--trials", str(trials), *flags, code=code)

    def measured(self, result, row=ROW):
        return result["summary"]["measured"][row]

    def trial_documents(self, result):
        return [t["document"] for t in self.cli("show", result["run_id"])["trials"]]

    def row(self, row=ROW):
        return next(r for r in self.cli("tests") if r["id"] == row)

    def edit_adapter(self, old, new):
        path = self.work / ".quruntul" / "adapter.py"
        path.write_text(path.read_text().replace(old, new))
        self.push("adapter: " + new)

    # -- the strict rule -------------------------------------------------------

    def test_a_hang_past_the_deadline_is_a_failure_that_says_it_timed_out(self):
        self.faulty(hang_on=[2])
        result = self.batch(3, "--trial-deadline", "1")
        measured = self.measured(result)
        self.assertEqual((measured["failures"], measured["complete"], measured["became"]), (1, 3, "flaky"))
        self.assertEqual(measured["failure_causes"]["timed-out"], 1)
        self.assertEqual(sum(measured["failure_causes"].values()), measured["failures"])
        self.assertEqual(result["summary"]["counts"], {"passed": 2, "timeout": 1})
        self.assertEqual(self.statuses()[ROW], "flaky")
        self.assertEqual(self.row()["failures"], 1)
        documents = self.trial_documents(result)
        self.assertEqual([d["deadline_seconds"] for d in documents], [1, 1, 1])
        self.assertEqual(documents[1]["outcome"], "timeout")
        self.assertEqual(documents[1]["failure_causes"], {ROW: "timed-out"})
        self.assertNotIn("failure_causes", documents[0])
        # The trial was cut off by the batch's deadline, not the suite's 30 seconds.
        self.assertLess(documents[1]["duration_seconds"], 10)
        text = Path(result["report"]).read_text()
        self.assertIn("1 timed out", text)
        self.assertIn("Trial deadline: 1s per trial", text)
        state = State(self.work / ".git" / "quruntul")
        try:
            event = next(e for e in state.events(ROW) if e["kind"] == "status" and e["document"]["status"] == "flaky")
        finally:
            state.db.close()
        self.assertIn("1 timed out", event["document"]["reason"])

    def test_a_trial_killed_by_a_signal_is_a_failure_that_says_it_crashed(self):
        self.faulty(crash_on=[2])
        result = self.batch(3, "--trial-deadline", "5")
        measured = self.measured(result)
        self.assertEqual((measured["failures"], measured["became"]), (1, "flaky"))
        self.assertEqual(measured["failure_causes"]["crashed"], 1)
        self.assertEqual(result["summary"]["counts"], {"passed": 2, "crashed": 1})
        self.assertEqual(self.trial_documents(result)[1]["failure_causes"], {ROW: "crashed"})
        self.assertIn("1 crashed (killed by a signal)", Path(result["report"]).read_text())

    def test_a_nonzero_exit_without_the_tests_report_is_a_failure(self):
        self.faulty(exit_on=[2])
        result = self.batch(3, "--trial-deadline", "5")
        measured = self.measured(result)
        self.assertEqual((measured["failures"], measured["became"]), (1, "flaky"))
        self.assertEqual(measured["failure_causes"]["exited-nonzero"], 1)
        self.assertEqual(result["summary"]["counts"], {"passed": 2, "failed": 1})
        self.assertEqual(self.trial_documents(result)[1]["failure_causes"], {ROW: "exited-nonzero"})
        self.assertIn("1 exited nonzero without the test's report", Path(result["report"]).read_text())

    def test_a_trial_that_ends_badly_after_the_test_passed_is_still_a_failure(self):
        for how, cause in (("hang", "passed-then-timed-out"), ("crash", "passed-then-crashed"),
                           ("exit", "passed-then-exited-nonzero")):
            with self.subTest(how=how):
                self.faulty(stop=dict(after="A/one", how=how, trials=[2]))
                before = self.row()["failures"]
                result = self.batch(3, "--trial-deadline", "1")
                measured = self.measured(result)
                self.assertEqual(measured["failures"], 1)
                self.assertEqual(measured["failure_causes"][cause], 1)
                self.assertEqual(sum(measured["failure_causes"].values()), 1)
                self.assertEqual(self.row()["failures"], before + 1)
                self.assertEqual(self.trial_documents(result)[1]["failure_causes"], {ROW: cause})
                self.assertIn("1 " + cause.replace("-", " "), Path(result["report"]).read_text())

    def test_each_failed_trial_counts_under_one_cause_in_a_mixed_batch(self):
        self.spec(dict(examples=["A/one"], fail={"A/one": [1]}, hang_on=[2], exit_on=[4],
                       stop=dict(after="A/one", how="crash", trials=[3])))
        self.push("one trial of each kind")
        result = self.batch(5, "--trial-deadline", "1")
        measured = self.measured(result)
        self.assertEqual((measured["failures"], measured["complete"]), (4, 5))
        causes = measured["failure_causes"]
        self.assertEqual({c: n for c, n in causes.items() if n}, {
            "timed-out": 1, "passed-then-crashed": 1, "exited-nonzero": 1})
        # The fourth failure is Hspec's own, which fails the trial with a nonzero exit: it stays ordinary.
        self.assertEqual(measured["failures"] - sum(causes.values()), 1)
        self.assertIn("1 failed otherwise", Path(result["report"]).read_text())
        self.assertEqual(self.trial_documents(result)[0].get("failure_causes"), None)

    def test_a_failure_reported_before_the_process_died_counts_once_under_the_unqualified_cause(self):
        self.spec(dict(examples=["A/one"], fail={"A/one": [1]}, stop=dict(after="A/one", how="crash", trials=[1])))
        self.push("fails, then is killed")
        result = self.batch(2, "--trial-deadline", "5")
        measured = self.measured(result)
        self.assertEqual(measured["failures"], 1)
        self.assertEqual({c: n for c, n in measured["failure_causes"].items() if n}, {"crashed": 1})

    def test_a_batch_in_which_every_trial_times_out_makes_the_test_failing(self):
        self.faulty(hang_on=[1, 2])
        result = self.batch(2, "--trial-deadline", "1")
        measured = self.measured(result)
        self.assertEqual((measured["failures"], measured["consistent"], measured["became"]), (2, True, "failing"))
        self.assertEqual(self.statuses()[ROW], "failing")
        self.assertEqual(result["summary"]["newly_failing"], [ROW])
        text = Path(result["report"]).read_text()
        self.assertIn("failed every trial", text)
        self.assertIn("2 timed out", text)

    def test_a_batch_in_which_every_trial_is_killed_by_a_signal_makes_the_test_failing(self):
        self.faulty(crash_on=[1, 2])
        result = self.batch(2, "--trial-deadline", "5")
        self.assertEqual(self.measured(result)["became"], "failing")
        self.assertEqual(self.statuses()[ROW], "failing")

    def test_a_mix_of_timeouts_and_ordinary_failures_in_every_trial_is_failing(self):
        self.spec(dict(examples=["A/one"], fail={"A/one": [2]}, hang_on=[1]))
        self.push("a timeout, then an ordinary failure")
        result = self.batch(2, "--trial-deadline", "1")
        measured = self.measured(result)
        self.assertEqual((measured["failures"], measured["became"]), (2, "failing"))
        self.assertEqual(measured["failure_causes"]["timed-out"], 1)

    def test_a_candidate_batch_under_a_deadline_changes_no_status(self):
        git(self.work, "checkout", "-q", "-b", "candidate")
        self.spec(dict(examples=["A/one"], hang_on=[1, 2]))
        git(self.work, "commit", "-qam", "candidate hangs")
        git(self.work, "push", "-q", "origin", "candidate")
        result = self.batch(2, "--ref", "candidate", "--trial-deadline", "1")
        self.assertTrue(result["candidate"])
        self.assertEqual(self.measured(result)["failure_causes"]["timed-out"], 2)
        self.assertEqual(self.statuses()[ROW], "stable")

    # -- without the option ----------------------------------------------------

    def test_without_the_option_the_same_faults_are_not_failures(self):
        self.edit_adapter("trial_seconds=30, batch_seconds=300, priority=20", "trial_seconds=1, batch_seconds=300, priority=20")
        self.faulty(hang_on=[1], crash_on=[2], exit_on=[3])
        result = self.batch(3)
        measured = self.measured(result)
        self.assertEqual((measured["failures"], measured["became"]), (0, None))
        self.assertNotIn("failure_causes", measured)
        self.assertEqual(result["summary"]["counts"], {"timeout": 1, "crashed": 1, "failed": 1})
        outcomes = {r["number"]: r["outcome"] for r in self.cli("show", result["run_id"])["results"]}
        self.assertEqual(outcomes, {1: "incomplete", 2: "incomplete", 3: "missing"})
        self.assertEqual(self.statuses()[ROW], "stable")
        document = self.cli("show", result["run_id"])
        self.assertNotIn("deadline_seconds", document["document"])
        for trial in self.trial_documents(result):
            self.assertNotIn("deadline_seconds", trial)
            self.assertNotIn("failure_causes", trial)
        text = Path(result["report"]).read_text()
        self.assertNotIn("Trial deadline", text)
        self.assertNotIn("by cause", text)

    def test_a_passed_test_followed_by_a_nonzero_exit_is_still_a_pass_without_the_option(self):
        self.faulty(stop=dict(after="A/one", how="exit", trials=[2]))
        result = self.batch(3)
        self.assertEqual(self.measured(result)["failures"], 0)
        self.assertEqual(self.statuses()[ROW], "stable")

    # -- the deadline's range and the batch budget ----------------------------

    def test_the_option_overrides_the_suites_trial_seconds_by_raising_it(self):
        self.edit_adapter("trial_seconds=30, batch_seconds=300, priority=20", "trial_seconds=1, batch_seconds=300, priority=20")
        self.faulty(sleep_on={"1": 2})
        slow = self.batch(1)
        self.assertEqual(slow["summary"]["counts"], {"timeout": 1})
        raised = self.batch(1, "--trial-deadline", "10")
        self.assertEqual(raised["summary"]["counts"], {"passed": 1})
        self.assertEqual(self.measured(raised)["failures"], 0)
        self.assertEqual(self.cli("show", raised["run_id"])["document"]["deadline_seconds"], 10)
        self.assertEqual(self.cli("show", raised["run_id"])["document"]["suite"]["trial_seconds"], 1)
        self.assertEqual(self.trial_documents(raised)[0]["deadline_seconds"], 10)

    def test_the_batch_budget_check_uses_the_batch_deadline(self):
        def run(deadline, ticks):
            clock = itertools.chain(ticks, itertools.repeat(10_000))
            real = lab_module.time
            lab_module.time = type("Clock", (), dict(time=staticmethod(real.time), sleep=staticmethod(real.sleep),
                                                     monotonic=staticmethod(lambda: next(clock))))
            try:
                flags = ["--trial-deadline", str(deadline)] if deadline else []
                return self.batch(4, *flags, code=1)
            finally:
                lab_module.time = real

        # batch_seconds is 300 and trial_seconds 30. 280 s into the batch, 20 s are left:
        # room for a 10 s deadline but not for the suite's 30 s.
        low = run(10, [0, 0, 280])
        self.assertEqual((low["outcome"], low["summary"]["counts"]), ("budget-exhausted", {"passed": 2}))
        without = run(None, [0, 0, 280])
        self.assertEqual(without["summary"]["counts"], {"passed": 1})
        # 150 s into the batch, 150 s are left: room for the suite's 30 s but not for a 200 s deadline.
        high = run(200, [0, 0, 150])
        self.assertEqual((high["outcome"], high["summary"]["counts"]), ("budget-exhausted", {"passed": 1}))
        without = run(None, [0, 0, 150, 150])
        self.assertEqual(without["summary"]["counts"], {"passed": 3})

    # -- refusals --------------------------------------------------------------

    def test_refusals_happen_before_any_build_or_trial(self):
        self.cli("flake", "--target", "probe-a", "--trials", "1")
        before = len(self.cli("runs"))
        cases = [
            (("--target", ROW, "--trial-deadline", "0"), "positive whole number"),
            (("--target", ROW, "--trial-deadline", "-3"), "positive whole number"),
            (("--target", ROW, "--trial-deadline", "301"), "exceeds its batch_seconds (300)"),
            (("--trial-deadline", "5"), "explicit single-test --target"),
            (("--target", "unit", "--trial-deadline", "5"), "requires a single-test target"),
            (("--target", "probe-a::alpha", "--trial-deadline", "5"), "requires an Hspec suite"),
            (("--target", "probe-a", "--trial-deadline", "5"), "requires a single-test target"),
        ]
        for flags, message in cases:
            with self.subTest(flags=flags):
                refused = self.cli("flake", *flags, code=2)
                self.assertIn(message, refused["error"])
        self.assertEqual(len(self.cli("runs")), before)
        self.assertEqual(self.batch(1, "--trial-deadline", "250")["outcome"], "complete")

    def test_the_option_is_accepted_only_by_flake(self):
        for lane in ("shakedown", "test"):
            with self.subTest(lane=lane), self.assertRaises(SystemExit) as raised:
                from quruntul import cli as cli_module
                import contextlib
                import io
                with contextlib.redirect_stderr(io.StringIO()):
                    cli_module.parser().parse_args([lane, "--trial-deadline", "5"])
            self.assertEqual(raised.exception.code, 2)

    # -- candidates and baselines ---------------------------------------------

    def candidate(self, name="candidate", **spec):
        git(self.work, "checkout", "-q", "-B", name, "master")
        self.spec(dict(examples=["A/one"], **spec))
        git(self.work, "commit", "--allow-empty", "-qam", name)
        git(self.work, "push", "-q", "-f", "origin", name)

    def test_a_candidate_report_says_whether_its_deadline_matches_the_baselines(self):
        baseline = self.batch(1, "--trial-deadline", "5")
        self.candidate()
        same = self.batch(1, "--ref", "candidate", "--trial-deadline", "5")
        self.assertIn(f"batch `{baseline['run_id']}` ran under a 5s per-trial deadline; this candidate ran under "
                      "the same 5s deadline, so they are comparable", Path(same["report"]).read_text())
        # A newer candidate batch never becomes the baseline.
        different = self.batch(1, "--ref", "candidate", "--trial-deadline", "7")
        text = Path(different["report"]).read_text()
        self.assertIn(f"batch `{baseline['run_id']}` ran under a 5s per-trial deadline; this candidate ran under "
                      "7s, so they are not comparable", text)
        self.assertNotIn(same["run_id"], text)
        self.assertEqual(self.statuses()[ROW], "stable")

    def test_a_baseline_without_the_option_is_read_as_the_suites_trial_seconds_it_ran_with(self):
        baseline = self.batch(1)
        self.edit_adapter("trial_seconds=30, batch_seconds=300, priority=20", "trial_seconds=40, batch_seconds=300, priority=20")
        self.candidate()
        text = Path(self.batch(1, "--ref", "candidate", "--trial-deadline", "30")["report"]).read_text()
        self.assertIn(f"batch `{baseline['run_id']}` ran under a 30s per-trial deadline; this candidate ran under "
                      "the same 30s deadline, so they are comparable", text)
        text = Path(self.batch(1, "--ref", "candidate", "--trial-deadline", "40")["report"]).read_text()
        self.assertIn("ran under a 30s per-trial deadline; this candidate ran under 40s, so they are not comparable", text)

    def test_the_baseline_is_the_batch_that_measured_this_row(self):
        self.spec(dict(examples=["A/one", "A/two"]))
        self.push("a second example")
        self.cli("flake", "--target", "unit", "--trials", "1")
        first = self.batch(1, "--trial-deadline", "5")
        # A later upstream batch of another row, under another deadline, is not this row's baseline.
        self.batch(1, "--trial-deadline", "9", target="unit::A/two")
        git(self.work, "checkout", "-q", "-B", "candidate", "master")
        self.spec(dict(examples=["A/one", "A/two", "A/three"]))
        git(self.work, "commit", "-qam", "candidate adds a test")
        git(self.work, "push", "-q", "-f", "origin", "candidate")
        text = Path(self.batch(1, "--ref", "candidate", "--trial-deadline", "5")["report"]).read_text()
        self.assertIn(f"batch `{first['run_id']}` ran under a 5s per-trial deadline", text)
        self.assertIn("so they are comparable", text)
        # A row no upstream batch ever measured has no baseline.
        fresh = self.batch(1, "--ref", "candidate", "--trial-deadline", "5", target="unit::A/three")
        self.assertIn("no earlier batch at the upstream head measured this test", Path(fresh["report"]).read_text())

    def test_a_setting_row_has_its_own_baseline(self):
        default = self.batch(1, "--trial-deadline", "5")
        setting_id = f"unit::rts-1::{hashlib.sha256(b'A/one').hexdigest()}"
        setting = self.batch(1, "--rts-capabilities", "1", "--trial-deadline", "9")
        self.candidate()
        text = Path(self.batch(1, "--ref", "candidate", "--rts-capabilities", "1", "--trial-deadline", "9")["report"]).read_text()
        self.assertIn(f"Baseline of `{setting_id}`: batch `{setting['run_id']}` ran under a 9s", text)
        self.assertNotIn(default["run_id"], text)

    # -- identity and the capability count ------------------------------------

    def test_the_deadline_is_not_part_of_the_rows_identity(self):
        rows = {r["id"] for r in self.cli("tests")}
        first = self.batch(2, "--trial-deadline", "5")
        second = self.batch(2, "--trial-deadline", "9")
        third = self.batch(2)
        self.assertEqual({r["id"] for r in self.cli("tests")}, rows)
        for result in (first, second, third):
            self.assertEqual(set(result["summary"]["measured"]), {ROW})
        self.assertEqual(self.row()["trials"], 1 + 6)

    def test_the_option_works_with_a_capability_count_and_a_setting_id(self):
        setting_id = f"unit::rts-4::{hashlib.sha256(b'A/one').hexdigest()}"
        self.faulty(hang_on=[2])
        first = self.batch(3, "--rts-capabilities", "4", "--trial-deadline", "1")
        self.assertEqual(set(first["summary"]["measured"]), {setting_id})
        self.assertEqual(self.measured(first, setting_id)["failure_causes"]["timed-out"], 1)
        document = self.cli("show", first["run_id"])["document"]
        self.assertEqual((document["rts_capabilities"], document["deadline_seconds"]), (4, 1))
        argv = self.trial_documents(first)[0]["command"]
        self.assertEqual(argv[argv.index("+RTS") + 1:argv.index("-RTS")], ["-N4"])
        # The default row is untouched, and the setting's row is the one measured again by id.
        self.assertEqual((self.row()["status"], self.row()["trials"]), ("stable", 1))
        again = self.batch(3, "--trial-deadline", "2", target=setting_id)
        self.assertEqual(set(again["summary"]["measured"]), {setting_id})
        self.assertEqual(self.row(setting_id)["trials"], 6)
        refused = self.batch(1, "--rts-capabilities", "1", "--trial-deadline", "2", target=setting_id, code=2)
        self.assertIn("cannot override it", refused["error"])


class ExitSuiteDeadlineTests(LabFixture):
    def setUp(self):
        super().setUp()
        (self.work / ".quruntul" / "adapter.py").write_text(HOOKED_ADAPTER)
        (self.work / "legacy.py").write_text(LEGACY)
        (self.work / "events.py").write_text(EVENTS)
        git(self.work, "add", "-A")
        git(self.work, "commit", "-qm", "hooked adapter")
        git(self.work, "push", "-q", "origin", "master")

    def test_the_option_is_refused_for_exit_and_command_suites_before_a_run(self):
        self.cli("flake", "--target", "legacy", "--trials", "1")
        self.cli("flake", "--target", "events", "--trials", "1")
        before = len(self.cli("runs"))
        for target in ("legacy::run", "events::alpha"):
            with self.subTest(target=target):
                refused = self.cli("flake", "--target", target, "--trial-deadline", "5", code=2)
                self.assertIn("requires an Hspec suite", refused["error"])
        self.assertEqual(len(self.cli("runs")), before)
