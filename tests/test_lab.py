"""End to end through the CLI against a real Git repository and a fake Hspec suite.

The fixture repository has an upstream remote, a committed .quruntul/adapter.py,
one CI Hspec suite (tests/fixtures/fake_hspec.py reading spec.json) and one
command probe. No real compiler runs; the Git, SQLite, worktree and process
behaviour is real.
"""
from __future__ import annotations

import io
import json
import os
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from quruntul import cli  # noqa: E402

ADAPTER = '''
import hashlib, sys
from pathlib import Path

class Fixture:
    name = "fixture"
    flake_trials = 3

    def suites(self, ctx):
        spec = (ctx.checkout / "spec.json").read_text()
        probe = (ctx.checkout / "probe.py").read_text()
        Suite, digest = ctx.Suite, ctx.digest
        return [Suite(id="unit", kind="ci", framework="hspec", description="fake hspec suite",
                      area="core", identity=digest(spec), trial_seconds=30, batch_seconds=300, priority=20,
                      rts_capabilities=[1, 4]),
                Suite(id="probe-a", kind="probe", framework="command", description="a slow observation",
                      area="lua", identity=digest(probe), checks=["alpha", "beta"],
                      trial_seconds=30, batch_seconds=300)]

    def prepare(self, ctx, suite):
        build = ctx.run([sys.executable, "-c", "print('built')"], "build", 60)
        if build["outcome"] != "passed":
            raise RuntimeError("build failed")
        if suite.framework == "hspec":
            exe = str(ctx.checkout / "fake_hspec.py")
            return ctx.Prepared(argv=[exe], cwd=str(ctx.checkout),
                                environment={"FAKE_HSPEC_RECORD": str(ctx.artifacts / "fake-hspec-record.json")},
                                provenance=dict(executable_sha256=hashlib.sha256(Path(exe).read_bytes()).hexdigest()))
        return ctx.Prepared(argv=[sys.executable, str(ctx.checkout / "probe.py")], cwd=str(ctx.checkout), environment={})

def adapter():
    return Fixture()
'''

PROBE = '''
import json, os
checks = {"alpha": "passed", "beta": "failed"}
json.dump({"schema": "quruntul-probe/v1", "checks": checks}, open(os.environ["QURUNTUL_PROBE_RESULT"], "w"))
raise SystemExit(1)
'''


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


class LabFixture(unittest.TestCase):
    """A fixture repository with an upstream remote; no tests of its own."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="quruntul-lab-")
        base = Path(self.temp.name)
        self.work = base / "work"
        self.work.mkdir()
        git(self.work, "init", "-q", "-b", "master")
        git(self.work, "config", "user.email", "t@example.com")
        git(self.work, "config", "user.name", "t")
        (self.work / ".quruntul").mkdir()
        (self.work / ".quruntul" / "adapter.py").write_text(ADAPTER)
        shutil.copy(HERE / "fixtures" / "fake_hspec.py", self.work / "fake_hspec.py")
        os.chmod(self.work / "fake_hspec.py", 0o755)
        (self.work / "probe.py").write_text(PROBE)
        self.spec(dict(examples=["A/one", "A/two", "B/three"], fail={"A/two": [2]}))
        git(self.work, "add", "-A")
        git(self.work, "commit", "-qm", "fixture")
        subprocess.run(["git", "clone", "-q", "--bare", str(self.work), str(base / "origin.git")], check=True)
        git(self.work, "remote", "add", "origin", str(base / "origin.git"))
        git(self.work, "fetch", "-q", "origin")
        git(self.work, "remote", "set-head", "origin", "master")

    def tearDown(self):
        self.temp.cleanup()

    def spec(self, value):
        (self.work / "spec.json").write_text(json.dumps(value))

    def push(self, message):
        git(self.work, "commit", "-qam", message)
        git(self.work, "push", "-q", "origin", "master")

    def cli(self, *argv, code=0):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            result = cli.main(["--repo", str(self.work), *argv])
        self.assertEqual(result, code, err.getvalue() + out.getvalue())
        return json.loads(out.getvalue()) if out.getvalue().strip() else json.loads(err.getvalue())

    def statuses(self):
        return {t["id"]: t["status"] for t in self.cli("tests")}


class LabTests(LabFixture):
    def test_flake_keeps_each_rts_setting_in_its_own_row_and_records_argv(self):
        import hashlib

        self.spec(dict(examples=["A/one"], fail_rts={"A/one": {"1": [1]}}))
        self.push("setting-sensitive fixture")
        default = self.cli("flake", "--target", "unit", "--trials", "3")
        default_id = "unit::A/one"
        default_argv = self.cli("show", default["run_id"])["trials"][0]["document"]["command"]
        self.assertNotIn("+RTS", default_argv)
        original = self.cli("tests")[0]
        before = (original["status"], original["trials"], original["failures"])

        old_ghcrts = os.environ.get("GHCRTS")
        os.environ["GHCRTS"] = "-N8"
        try:
            one = self.cli("flake", "--target", default_id, "--rts-capabilities", "1", "--trials", "3")
            four = self.cli("flake", "--target", default_id, "--rts-capabilities", "4", "--trials", "3")
        finally:
            if old_ghcrts is None:
                os.environ.pop("GHCRTS", None)
            else:
                os.environ["GHCRTS"] = old_ghcrts

        one_id = f"unit::rts-1::{hashlib.sha256(b'A/one').hexdigest()}"
        four_id = f"unit::rts-4::{hashlib.sha256(b'A/one').hexdigest()}"
        rows = {row["id"]: row for row in self.cli("tests")}
        self.assertEqual((rows[default_id]["status"], rows[default_id]["trials"], rows[default_id]["failures"]), before)
        self.assertEqual((rows[one_id]["status"], rows[one_id]["failures"]), ("flaky", 1))
        self.assertEqual((rows[four_id]["status"], rows[four_id]["failures"]), ("stable", 0))
        self.assertEqual(rows[one_id]["rts_capabilities"], 1)
        self.assertEqual(rows[four_id]["rts_capabilities"], 4)
        for result, count in ((one, "1"), (four, "4")):
            trial = self.cli("show", result["run_id"])["trials"][-1]["document"]
            argv = trial["command"]
            self.assertEqual(argv[argv.index("+RTS") + 1:argv.index("-RTS")], [f"-N{count}"])
            self.assertEqual(result["summary"]["measured"].keys(), {one_id if count == "1" else four_id})
            recorded = json.loads((Path(result["result"]).parent / "fake-hspec-record.json").read_text())
            self.assertIsNone(recorded["ghcrts"])
            self.assertEqual(recorded["claim"]["rts_capabilities"], int(count))
        self.assertEqual(one["summary"]["measured"][one_id]["became"], "flaky")
        self.assertEqual(four["summary"]["measured"][four_id]["became"], "stable")
        self.assertEqual(self.cli("show", one["run_id"])["document"]["rts_capabilities"], 1)
        self.assertIn("-N1", Path(one["report"]).read_text())
        refused = self.cli("flake", "--target", one_id, "--rts-capabilities", "4", code=2)
        self.assertIn("cannot override it", refused["error"])

        rerun = self.cli("flake", "--target", one_id, "--trials", "1")
        self.assertEqual(rerun["summary"]["measured"].keys(), {one_id})
        self.cli("note", one_id, "--text", "setting-specific note")
        self.cli("mark", four_id, "--status", "flaky", "--reason", "setting-specific mark")
        resolved = self.cli("resolve-arg", "deflake", one_id)
        self.assertEqual(resolved["target"]["id"], one_id)
        selected = self.cli("deflake", "select", "--owner", "rts-owner-1")
        self.assertEqual(selected["test"]["id"], one_id)
        self.cli("deflake", "pr", one_id, "--owner", "rts-owner-1", "--pr", "https://example.test/1")
        other = self.cli("deflake", "select", "--owner", "rts-owner-2")
        self.assertEqual(other["test"]["id"], four_id)
        self.cli("deflake", "release", four_id, "--owner", "rts-owner-2", "--reason", "check setting claim")

    def test_suite_rts_option_is_overridden_by_the_chosen_capability(self):
        adapter = (self.work / ".quruntul" / "adapter.py").read_text()
        adapter = adapter.replace("rts_capabilities=[1, 4])", "rts=[\"-N2\"], rts_capabilities=[1, 4])")
        (self.work / ".quruntul" / "adapter.py").write_text(adapter)
        git(self.work, "commit", "-qam", "declare a suite RTS default")
        git(self.work, "push", "-q", "origin", "master")
        result = self.cli("flake", "--target", "unit", "--rts-capabilities", "4", "--trials", "1")
        trial = self.cli("show", result["run_id"])["trials"][0]["document"]
        argv = trial["command"]
        options = argv[argv.index("+RTS") + 1:argv.index("-RTS")]
        self.assertEqual(options, ["-N2", "-N4"])
        record = json.loads((Path(result["result"]).parent / "fake-hspec-record.json").read_text())
        self.assertEqual(record["argv"][record["argv"].index("+RTS") + 1:record["argv"].index("-RTS")], options)

    def test_suite_without_a_capability_declaration_accepts_any_positive_count(self):
        adapter = (self.work / ".quruntul" / "adapter.py").read_text()
        (self.work / ".quruntul" / "adapter.py").write_text(adapter.replace("rts_capabilities=[1, 4]", "rts_capabilities=[]"))
        self.push("allow arbitrary RTS counts")
        result = self.cli("flake", "--target", "unit", "--rts-capabilities", "7", "--trials", "1")
        trial = self.cli("show", result["run_id"])["trials"][0]["document"]
        argv = trial["command"]
        self.assertEqual(argv[argv.index("+RTS") + 1:argv.index("-RTS")], ["-N7"])

    def test_candidate_setting_batch_records_evidence_without_changing_status(self):
        import hashlib

        self.spec(dict(examples=["A/one"], fail_rts={"A/one": {"1": [1]}}))
        self.push("setting baseline")
        self.cli("flake", "--target", "unit", "--trials", "1")
        row_id = f"unit::rts-1::{hashlib.sha256(b'A/one').hexdigest()}"
        baseline = self.cli("flake", "--target", "unit::A/one", "--rts-capabilities", "1", "--trials", "3")
        self.assertEqual(baseline["summary"]["measured"][row_id]["became"], "flaky")
        self.assertEqual(self.statuses()[row_id], "flaky")
        git(self.work, "checkout", "-q", "-b", "candidate")
        self.spec(dict(examples=["A/one"]))
        git(self.work, "commit", "-qam", "candidate passes at -N1")
        git(self.work, "push", "-q", "origin", "candidate")
        candidate = self.cli("flake", "--target", row_id, "--ref", "candidate", "--trials", "3")
        self.assertTrue(candidate["candidate"])
        self.assertEqual(self.statuses()[row_id], "flaky")

    def test_rts_option_refusals_and_declared_settings_do_not_change_default_selection(self):
        self.spec(dict(examples=["A/one", "A/two"]))
        self.push("two examples for setting refusals")
        self.cli("flake", "--target", "unit", "--trials", "1")
        self.cli("flake", "--target", "probe-a", "--trials", "1")
        before = len(self.cli("runs"))
        refused = self.cli("flake", "--rts-capabilities", "1", code=2)
        self.assertIn("explicit --target", refused["error"])
        refused = self.cli("flake", "--target", "unit", "--rts-capabilities", "2", code=2)
        self.assertIn("not declared", refused["error"])
        refused = self.cli("flake", "--target", "unit", "--rts-capabilities", "0", code=2)
        self.assertIn("positive integer", refused["error"])
        refused = self.cli("flake", "--target", "probe-a", "--rts-capabilities", "1", code=2)
        self.assertIn("Hspec suite", refused["error"])
        parser_error = io.StringIO()
        with redirect_stderr(parser_error), self.assertRaises(SystemExit) as raised:
            cli.parser().parse_args(["shakedown", "--rts-capabilities", "1"])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("unrecognized arguments", parser_error.getvalue())
        self.assertEqual(len(self.cli("runs")), before)

        self.cli("flake", "--target", "unit", "--rts-capabilities", "1", "--trials", "1")
        self.cli("flake", "--target", "unit", "--rts-capabilities", "4", "--trials", "1")
        self.spec(dict(examples=["A/one", "A/two", "A/three"]))
        self.push("one new default test")
        automatic = self.cli("flake")
        self.assertEqual(automatic["summary"]["selected"], 1, automatic["summary"]["measured"])
        self.assertEqual(list(automatic["summary"]["measured"]), ["unit::A/three"])

    def test_flake_measures_each_test_once_then_only_new_ones(self):
        first = self.cli("flake")
        self.assertEqual((first["outcome"], first["suite"]), ("complete", "unit"))
        self.assertEqual(self.statuses(), {"unit::A/one": "stable", "unit::A/two": "flaky", "unit::B/three": "stable"})
        self.assertEqual(first["summary"]["newly_flaky"], ["unit::A/two"])
        report_text = Path(first["report"]).read_text()
        self.assertIn("OBS-001 — unit::A/two failed 1 of 3 trials", report_text)
        [observation] = self.cli("observations", "--status", "open")
        self.assertEqual(observation["id"], first["run_id"] + "/OBS-001")

        # The probe suite has never been enumerated, so it is next; the CI suite is not re-measured.
        second = self.cli("flake", code=0)
        self.assertEqual(second["suite"], "probe-a")
        # beta fails every trial: a consistent failure, not flakiness.
        self.assertEqual(self.statuses()["probe-a::beta"], "failing")
        third = self.cli("flake")
        self.assertEqual(third["outcome"], "no-candidate")

        # A new example upstream: only it is measured.
        self.spec(dict(examples=["A/one", "A/two", "B/three", "B/four"], fail={"A/two": [2]}))
        self.push("add B/four")
        fourth = self.cli("flake")
        self.assertEqual(fourth["summary"]["selected"], 1)
        self.assertEqual(list(fourth["summary"]["measured"]), ["unit::B/four"])
        argv = self.cli("show", fourth["run_id"])["trials"][0]["document"]["command"]
        self.assertIn("/B/four/", argv)
        self.assertNotIn("/A/one/", argv)
        self.assertEqual(self.statuses()["unit::B/four"], "stable")

    def test_a_large_suite_is_measured_in_slices(self):
        adapter = (self.work / ".quruntul" / "adapter.py").read_text()
        (self.work / ".quruntul" / "adapter.py").write_text(
            adapter.replace("rts_capabilities=[1, 4])", "batch_tests=2, rts_capabilities=[1, 4])"))
        git(self.work, "commit", "-qam", "slice the unit suite")
        git(self.work, "push", "-q", "origin", "master")
        first = self.cli("flake")
        self.assertEqual(sorted(first["summary"]["measured"]), ["unit::A/one", "unit::A/two"])
        self.assertEqual(self.statuses()["unit::B/three"], "new")
        second = self.cli("flake")
        self.assertEqual((second["suite"], list(second["summary"]["measured"])), ("unit", ["unit::B/three"]))

    def test_an_always_pending_example_does_not_requeue_its_suite(self):
        self.spec(dict(examples=["A/one", "A/gated"], pending=["A/gated"]))
        self.push("a gated example")
        self.cli("flake", "--target", "unit")
        self.assertEqual(self.statuses()["unit::A/gated"], "pending")
        self.assertEqual(self.statuses()["unit::A/one"], "stable")
        self.assertNotEqual(self.cli("flake").get("suite"), "unit")
        self.spec(dict(examples=["A/one", "A/gated"]))  # its inputs change: queued again
        self.push("ungate")
        self.cli("flake", "--target", "unit")
        self.assertEqual(self.statuses()["unit::A/gated"], "stable")

    def test_child_output_does_not_hide_examples_and_an_unreported_one_is_observed(self):
        self.spec(dict(examples=["A/one", "A/two", "B/three", "B/four"], omit=["B/four"],
                       stray="child: all spec items have been filtered"))
        self.push("noisy children")
        result = self.cli("flake", "--target", "unit")
        statuses = self.statuses()
        self.assertEqual([statuses["unit::" + p] for p in ("A/one", "A/two", "B/three")], ["stable"] * 3)
        self.assertEqual(statuses["unit::B/four"], "new")
        self.assertEqual(result["summary"]["interpretation"], "observations")
        self.assertIn("1 selected tests of unit went unreported by a completed trial",
                      Path(result["report"]).read_text())

    def test_a_test_failing_every_trial_is_failing_and_requeued_when_its_inputs_change(self):
        self.spec(dict(examples=["A/one", "A/broken", "A/also-broken"],
                       fail={"A/broken": [1, 2, 3], "A/also-broken": [1, 2, 3]}))
        self.push("a consistently failing pair")
        result = self.cli("flake", "--target", "unit")
        statuses = self.statuses()
        self.assertEqual((statuses["unit::A/broken"], statuses["unit::A/also-broken"], statuses["unit::A/one"]),
                         ("failing", "failing", "stable"))
        self.assertEqual(sorted(result["summary"]["newly_failing"]), ["unit::A/also-broken", "unit::A/broken"])
        self.assertEqual(result["summary"]["newly_flaky"], [])
        # One observation for the batch, sent to assessment rather than $deflake.
        [observation] = self.cli("observations", "--status", "open")
        self.assertEqual(observation["document"]["kind"], "uncertain")
        self.assertIn("2 tests of unit failed every trial", observation["document"]["title"])
        self.assertIn("$assess-tests", observation["document"]["suggested_follow_up"])
        self.assertEqual(self.cli("deflake", "select", "--owner", "d1")["outcome"], "no-candidate")
        # Measured again explicitly, a test already failing that fails every trial
        # again keeps its status and still goes to assessment, never to $deflake.
        again = self.cli("flake", "--target", "unit::A/broken")
        self.assertEqual(self.statuses()["unit::A/broken"], "failing")
        [repeat] = self.cli("observations", "--status", "open", "--run", again["run_id"])
        self.assertEqual(repeat["document"]["kind"], "uncertain")
        self.assertIn("1 tests of unit failed every trial", repeat["document"]["title"])
        self.assertIn("failing 1", repeat["document"]["observed"])
        self.assertNotIn("Suggested follow-up: $deflake", Path(again["report"]).read_text())
        # Measured again with the suite's inputs unchanged, a failing test is judged
        # afresh: failing only some trials makes it flaky ($deflake's), passing
        # every trial makes it stable.
        (self.work / "override.json").write_text(json.dumps(dict(fail={"A/broken": [2]})))
        git(self.work, "add", "override.json")
        self.push("A/broken now fails only trial 2; A/also-broken passes")
        self.assertEqual(self.statuses()["unit::A/broken"], "failing")
        self.cli("flake", "--target", "unit::A/broken")
        self.assertEqual(self.statuses()["unit::A/broken"], "flaky")
        self.cli("flake", "--target", "unit::A/also-broken")
        self.assertEqual(self.statuses()["unit::A/also-broken"], "stable")
        (self.work / "override.json").unlink()
        git(self.work, "rm", "-q", "--cached", "override.json")
        git(self.work, "commit", "-qm", "drop the override")
        git(self.work, "push", "-q", "origin", "master")
        self.cli("mark", "unit::A/broken", "--status", "new", "--reason", "reset", "--evidence", "test")
        self.cli("mark", "unit::A/also-broken", "--status", "new", "--reason", "reset", "--evidence", "test")
        self.cli("flake", "--target", "unit")
        self.assertEqual(self.statuses()["unit::A/broken"], "failing")
        # The harness or code is repaired: the suite's inputs change, so both are queued again.
        self.spec(dict(examples=["A/one", "A/broken", "A/also-broken"]))
        self.push("repair")
        repaired = self.cli("flake", "--target", "unit")
        self.assertEqual(sorted(repaired["summary"]["measured"]), ["unit::A/also-broken", "unit::A/broken"])
        self.assertEqual((self.statuses()["unit::A/broken"], self.statuses()["unit::A/also-broken"]),
                         ("stable", "stable"))

    def test_a_batch_cut_short_is_neither_stable_nor_a_consistent_failure(self):
        from quruntul import lab as lab_module
        self.spec(dict(examples=["A/one", "A/broken"], fail={"A/broken": [1, 2, 3]}))
        self.push("fails every trial it gets")
        clock = iter([0, 0, 0, 10_000])  # the deadline, then room for exactly two of the three trials
        real = lab_module.time
        fake = type("Clock", (), dict(time=staticmethod(real.time), sleep=staticmethod(real.sleep),
                                      monotonic=staticmethod(lambda: next(clock))))
        lab_module.time = fake
        try:
            result = self.cli("flake", "--target", "unit", code=1)
        finally:
            lab_module.time = real
        self.assertEqual(result["outcome"], "budget-exhausted")
        self.assertEqual(result["summary"]["counts"], {"failed": 2})
        statuses = self.statuses()
        # Two passes of three requested prove nothing; two failures are a failure, not a consistent one.
        self.assertEqual((statuses["unit::A/one"], statuses["unit::A/broken"]), ("new", "flaky"))
        self.assertEqual(result["summary"]["newly_failing"], [])
        self.assertNotIn("failed every trial", Path(result["report"]).read_text())

    def test_a_crash_is_never_counted_as_a_pass(self):
        self.spec(dict(examples=["A/one"], crash_on=[1, 2, 3]))
        self.push("crashy")
        result = self.cli("flake", "--target", "unit", code=0)
        self.assertEqual(result["summary"]["counts"], {"crashed": 3})
        self.assertEqual(self.statuses()["unit::A/one"], "new")

    def test_owner_marks_and_the_deflake_handoff(self):
        self.cli("flake", "--target", "unit")
        self.cli("mark", "unit::A/one", "--status", "flaky", "--reason", "failed in CI", "--evidence", "run 42")
        claimed = self.cli("deflake", "select", "--owner", "d1")
        # A/two failed 1 of 3 trials; A/one was marked by the owner with no lab failures, so its
        # observed rate is 0 of 3 and it ranks second.
        self.assertEqual(claimed["test"]["id"], "unit::A/two")
        other = self.cli("deflake", "select", "--owner", "d2")
        self.assertEqual(other["test"]["id"], "unit::A/one")
        self.assertEqual(self.cli("deflake", "select", "--owner", "d3")["outcome"], "no-candidate")

        # Candidate evidence never changes status, even for the deflaker's own test.
        git(self.work, "checkout", "-q", "-b", "fix")
        self.spec(dict(examples=["A/one", "A/two", "B/three"]))
        git(self.work, "commit", "-qam", "fix A/two")
        candidate = self.cli("flake", "--target", "unit::A/two", "--ref", "fix", "--trials", "4", "--owner", "d1")
        self.assertTrue(candidate["candidate"])
        self.assertEqual(candidate["summary"]["measured"]["unit::A/two"]["failures"], 0)
        self.assertEqual(self.statuses()["unit::A/two"], "flaky")

        with self.assertRaises(AssertionError):
            self.cli("deflake", "pr", "unit::A/two", "--owner", "d2", "--pr", "7")
        fixing = self.cli("deflake", "pr", "unit::A/two", "--owner", "d1", "--pr", "7")
        self.assertEqual(fixing["outcome"], "fixing")
        self.assertEqual(self.statuses()["unit::A/two"], "fixing")
        released = self.cli("deflake", "release", "unit::A/one", "--owner", "d2", "--reason", "cannot reproduce")
        self.assertEqual(released["outcome"], "released")
        self.assertEqual(self.statuses()["unit::A/one"], "flaky")

    def test_probe_run_report_and_assessment(self):
        run = self.cli("test")
        self.assertEqual(run["suite"], "probe-a")
        self.assertEqual(run["summary"]["tests"], {"failed": 1, "passed": 1})
        report_path = Path(run["report"])
        text = report_path.read_text()
        with self.assertRaises(AssertionError):
            self.cli("report", "attach", run["run_id"])  # placeholders remain
        text = text.replace("<REPLACE: clean | observations | inconclusive | blocked>", "observations")
        head, _, _ = text.partition("## Interpretation")
        text = head + (
            "## Interpretation\n\nbeta fails deterministically.\n\n## Limitations\n\nOne execution.\n\n"
            "## Observations\n\n### OBS-001 — beta check fails\n\n- Area: lua\n- Kind: product\n"
            "- Tests: probe-a::beta\n- Evidence: trial-0001.log\n- Expected: passed\n- Observed: failed\n"
            "- Confidence: high\n- Suggested follow-up: inspect beta\n")
        report_path.write_text(text)
        attached = self.cli("report", "attach", run["run_id"])
        self.assertEqual(len(attached["observations"]), 1)
        self.assertEqual(self.cli("test")["outcome"], "no-candidate")  # fresh and unchanged

        claim = self.cli("assess", "claim", "--owner", "a1", "--area", "lua")
        proposal = Path(self.temp.name) / "assessment.md"
        proposal.write_text("## FND-001\n")
        proposed = self.cli("assess", "propose", claim["assessment"], "--file", str(proposal))
        self.cli("assess", "approve", claim["assessment"], "--sha", proposed["sha256"])
        self.cli("assess", "issue", claim["assessment"], "--finding", "FND-001", "--url", "https://x/1")
        self.assertEqual(self.cli("observations", "--status", "assessed")[0]["id"], attached["observations"][0])

    def test_ci_suites_are_never_test_lane_targets(self):
        self.cli("flake", "--target", "unit")
        refused = self.cli("test", "--target", "unit", code=2)
        self.assertIn("CI suite", refused["error"])

    def test_resolve_arg_grammar(self):
        self.cli("flake", "--target", "unit")
        self.assertEqual(self.cli("resolve-arg", "flake", "10")["iterations"], 10)
        exact = self.cli("resolve-arg", "deflake", "3", "unit::A/two")
        self.assertEqual((exact["iterations"], exact["target"]["kind"], exact["target"]["id"]), (3, "test", "unit::A/two"))
        fuzzy = self.cli("resolve-arg", "flake", "three")
        self.assertEqual(fuzzy["target"]["id"], "unit::B/three")
        hint = self.cli("resolve-arg", "test", "rendering")
        self.assertEqual((hint["target"], hint["hint"]), (None, "rendering"))
        self.assertEqual(self.cli("resolve-arg", "flake", "unit")["target"]["kind"], "suite")

    def test_dirty_checkout_blocks_rather_than_resetting(self):
        first = self.cli("flake", "--target", "unit")
        checkout = Path(self.cli("show", first["run_id"])["document"]["prepared"]["cwd"])
        (checkout / "spec.json").write_text("{}")
        blocked = self.cli("flake", "--target", "unit", code=2)
        self.assertIn("never reset", blocked["error"])
        self.assertEqual((checkout / "spec.json").read_text(), "{}")


if __name__ == "__main__":
    unittest.main()


HOOKED_ADAPTER = '''
import json, sys
from pathlib import Path

class Hooked:
    name = "hooked"
    flake_trials = 2

    def suites(self, ctx):
        return [ctx.Suite(id="legacy", kind="probe", framework="exit", description="an exit-status probe",
                          identity=ctx.digest((ctx.checkout / "legacy.py").read_text()), trial_seconds=30,
                          batch_seconds=300, priority=30),
                ctx.Suite(id="events", kind="ci", framework="command", description="a probe with its own protocol",
                          identity=ctx.digest((ctx.checkout / "events.py").read_text()), checks=["alpha", "beta"],
                          trial_seconds=30, batch_seconds=300, priority=20)]

    def prepare(self, ctx, suite):
        script = "legacy.py" if suite.id == "legacy" else "events.py"
        return ctx.Prepared(argv=[sys.executable, str(ctx.checkout / script)], cwd=str(ctx.checkout), environment={})

    def trial_env(self, ctx, suite, trial):
        return {"FIXTURE_EVENTS": trial["prefix"] + ".events.jsonl"} if suite.id == "events" else {}

    def outcomes(self, ctx, suite, trial):
        path = Path(trial["prefix"] + ".events.jsonl")
        if not path.exists():
            return None
        return {e["check"]: e["outcome"] for e in map(json.loads, path.read_text().splitlines())}

    def seed(self, ctx, suite, trial):
        if suite.id == "events":
            return {"beta": dict(status="flaky", reason="census: 3 of 10 failed", evidence={"runs": 10})}
        return {}

def adapter():
    return Hooked()
'''

LEGACY = "import os, sys\nsys.exit(1 if os.environ.get('QURUNTUL_TRIAL') == '2' else 0)\n"
EVENTS = ('import json, os\n'
          'with open(os.environ["FIXTURE_EVENTS"], "w") as f:\n'
          '    f.write(json.dumps({"check": "alpha", "outcome": "passed"}) + "\\n")\n')


class HookTests(LabTests):
    def setUp(self):
        super().setUp()
        (self.work / ".quruntul" / "adapter.py").write_text(HOOKED_ADAPTER)
        (self.work / "legacy.py").write_text(LEGACY)
        (self.work / "events.py").write_text(EVENTS)
        git(self.work, "add", "-A")
        git(self.work, "commit", "-qm", "hooked adapter")
        git(self.work, "push", "-q", "origin", "master")

    def test_exit_status_suites_seeding_and_adapter_read_checks(self):
        legacy = self.cli("flake")
        self.assertEqual(legacy["suite"], "legacy")
        self.assertEqual(self.statuses()["legacy::run"], "flaky")  # trial 2 exits 1
        events = self.cli("flake")
        self.assertEqual(events["suite"], "events")
        statuses = self.statuses()
        # beta was seeded flaky and so never measured; alpha passed every trial.
        self.assertEqual((statuses["events::alpha"], statuses["events::beta"]), ("stable", "flaky"))
        self.assertEqual(list(events["summary"]["measured"]), ["events::alpha"])
        seeded = [e for e in self.cli("tests") if e["id"] == "events::beta"][0]
        self.assertEqual(seeded["trials"], 0)

    def test_capability_setting_is_refused_for_exit_suite_before_a_run(self):
        before = len(self.cli("runs"))
        refused = self.cli("flake", "--target", "legacy", "--rts-capabilities", "1", code=2)
        self.assertIn("Hspec suite", refused["error"])
        self.assertEqual(len(self.cli("runs")), before)

    # The inherited end-to-end examples assume the plain fixture adapter.
    test_flake_measures_each_test_once_then_only_new_ones = None
    test_a_crash_is_never_counted_as_a_pass = None
    test_owner_marks_and_the_deflake_handoff = None
    test_probe_run_report_and_assessment = None
    test_ci_suites_are_never_test_lane_targets = None
    test_resolve_arg_grammar = None
    test_dirty_checkout_blocks_rather_than_resetting = None
    test_a_large_suite_is_measured_in_slices = None
    test_an_always_pending_example_does_not_requeue_its_suite = None
    test_child_output_does_not_hide_examples_and_an_unreported_one_is_observed = None
    test_a_test_failing_every_trial_is_failing_and_requeued_when_its_inputs_change = None
    test_a_batch_cut_short_is_neither_stable_nor_a_consistent_failure = None
    test_flake_keeps_each_rts_setting_in_its_own_row_and_records_argv = None
    test_suite_rts_option_is_overridden_by_the_chosen_capability = None
    test_suite_without_a_capability_declaration_accepts_any_positive_count = None
    test_candidate_setting_batch_records_evidence_without_changing_status = None
    test_rts_option_refusals_and_declared_settings_do_not_change_default_selection = None
