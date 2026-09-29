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
                      area="core", identity=digest(spec), trial_seconds=30, batch_seconds=300, priority=20),
                Suite(id="probe-a", kind="probe", framework="command", description="a slow observation",
                      area="lua", identity=digest(probe), checks=["alpha", "beta"],
                      trial_seconds=30, batch_seconds=300)]

    def prepare(self, ctx, suite):
        build = ctx.run([sys.executable, "-c", "print('built')"], "build", 60)
        if build["outcome"] != "passed":
            raise RuntimeError("build failed")
        if suite.framework == "hspec":
            exe = str(ctx.checkout / "fake_hspec.py")
            return ctx.Prepared(argv=[exe], cwd=str(ctx.checkout), environment={},
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


class LabTests(unittest.TestCase):
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
        self.assertEqual(self.statuses()["probe-a::beta"], "flaky")
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
