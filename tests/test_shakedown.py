"""The shakedown lane end to end, through the CLI, against fixture repositories.

The fixture adapter declares its suites from `suites.json`, so each test picks the
suites it needs. Hspec suites run tests/fixtures/fake_hspec.py against their own
spec file; command and exit suites run small scripts. Every test checks that the
ledger's tests and suites are exactly as they were.
"""
from __future__ import annotations

import io
import json
import os
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import sqlite3
import sys
import unittest

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from quruntul import cli, hspec, report  # noqa: E402
from quruntul.state import State  # noqa: E402
from tests.test_lab import LabFixture, git  # noqa: E402

SHAKE_ADAPTER = '''
import json, platform, sys
from pathlib import Path

class Shake:
    name = "shake"
    flake_trials = 2

    def suites(self, ctx):
        found = []
        for item in json.loads((ctx.checkout / "suites.json").read_text()):
            item = dict(dict(kind="ci", framework="hspec", trial_seconds=30), **item)
            data = item.pop("data", {})
            if item.pop("elsewhere", False):
                item["platforms"] = ["Linux" if platform.system() == "Darwin" else "Darwin"]
            spec = ctx.checkout / (data.get("spec", "spec") + ".json")
            source = json.dumps([item, data], sort_keys=True) + (spec.read_text() if spec.exists() else "")
            found.append(ctx.Suite(description="a fixture suite", identity=ctx.digest(source), batch_seconds=300,
                                   data=data, **item))
        return found

    def prepare(self, ctx, suite):
        data = suite.data
        code = 3 if data.get("build") == "fail" else 0
        build = ctx.run([sys.executable, "-c", f"import sys; print('compiling'); sys.exit({code})"], "build", 60)
        if build["outcome"] != "passed":
            raise RuntimeError(f"the build exited {build['returncode']}")
        env = {"FAKE_HSPEC_SPEC": str(ctx.checkout / (data.get("spec", "spec") + ".json")), "FIXTURE_PREPARED": suite.id,
               "FIXTURE_CHECKS": json.dumps(data.get("checks", {})), "FIXTURE_EXIT": str(data.get("exit", 0))}
        if suite.desktop:
            env["FIXTURE_CONSENT"] = "granted"  # what a real adapter's consent supplies
        if suite.framework == "hspec":
            exe = str(ctx.checkout / "fake_hspec.py")
            if data.get("wrap"):
                return ctx.Prepared(argv=[exe, *data.get("options", [])], cwd=str(ctx.checkout), environment=env,
                                    wrapper=[sys.executable, str(ctx.checkout / "wrap.py"), exe],
                                    launches_executable=False)
            return ctx.Prepared(argv=[exe, *data.get("options", [])], cwd=str(ctx.checkout), environment=env)
        return ctx.Prepared(argv=[sys.executable, str(ctx.checkout / "script.py")], cwd=str(ctx.checkout),
                            environment=env)

    def trial_env(self, ctx, suite, trial):
        return {"FIXTURE_HOOK_TRIAL": str(trial["number"]), "FAKE_HSPEC_RECORD": trial["prefix"] + ".record.json",
                "FIXTURE_EVENTS": trial["prefix"] + ".events.jsonl"}

    def outcomes(self, ctx, suite, trial):
        path = Path(trial["prefix"] + ".events.jsonl")
        if not path.exists():
            return None
        return {e["check"]: e["outcome"] for e in map(json.loads, path.read_text().splitlines())}

def adapter():
    return Shake()
'''

# The wrapper starts the executable itself (launches_executable=False).
WRAP = "import os, sys\nos.environ['FIXTURE_WRAPPED'] = '1'\nos.execv(sys.argv[1], sys.argv[1:])\n"
# A command suite writes its checks for the adapter's outcomes hook; an exit suite only exits.
# Checks are a mapping, or a list of [check, outcome] pairs when a key must not be a string.
SCRIPT = ('import json, os, sys\n'
          'checks = json.loads(os.environ["FIXTURE_CHECKS"])\n'
          'pairs = checks if isinstance(checks, list) else list(checks.items())\n'
          'if not any(check == "__none__" for check, _ in pairs):\n'
          '    with open(os.environ["FIXTURE_EVENTS"], "w") as f:\n'
          '        for check, outcome in pairs:\n'
          '            f.write(json.dumps({"check": check, "outcome": outcome}) + "\\n")\n'
          'sys.exit(int(os.environ["FIXTURE_EXIT"]))\n')
UNIT = dict(examples=["A/one", "A/two", "B/three"])


class ShakedownFixture(LabFixture):
    def setUp(self):
        super().setUp()
        (self.work / ".quruntul" / "adapter.py").write_text(SHAKE_ADAPTER)
        (self.work / "wrap.py").write_text(WRAP)
        (self.work / "script.py").write_text(SCRIPT)
        self.spec(UNIT)
        self.declare([dict(id="unit")])

    def declare(self, suites, specs=None):
        """Commit and push these suites, each hspec suite reading its own spec file."""
        for name, spec in (specs or {}).items():
            (self.work / f"{name}.json").write_text(json.dumps(spec))
        (self.work / "suites.json").write_text(json.dumps(suites))
        git(self.work, "add", "-A")
        git(self.work, "commit", "-qm", "declare suites")
        git(self.work, "push", "-q", "origin", "master")

    def ledger(self):
        """Every row and column of the tests and suites tables."""
        path = self.work / ".git" / "quruntul" / "ledger.sqlite3"
        if not path.exists():
            return [], []
        db = sqlite3.connect(path)
        try:
            return ([tuple(r) for r in db.execute("SELECT * FROM tests ORDER BY id")],
                    [tuple(r) for r in db.execute("SELECT * FROM suites ORDER BY id")])
        finally:
            db.close()

    def state(self):
        return State(self.work / ".git" / "quruntul")

    def shake(self, *argv, code=0):
        """A shakedown under an explicit owner, so only the lab itself can release its claims."""
        before = self.ledger()
        result = self.cli("shakedown", "--owner", "shaker", *argv, code=code)
        self.assertEqual(self.ledger(), before, "a shakedown never writes tests or suites")
        self.assertEqual([c for c in self.cli("claims") if c["owner"] == "shaker"], [])
        self.assertNotIn("running", [t["state"] for t in self.cli("show", result["run_id"])["trials"]])
        return result

    def observations(self, result):
        return {o["document"]["title"].split(":")[0].removeprefix("shakedown of "): o
                for o in self.cli("observations", "--run", result["run_id"])}

    def problems(self, entry):
        return [(p["kind"], p["tests"]) for p in entry["problems"]]


class ShakedownTests(ShakedownFixture):
    def test_a_clean_shakedown_launches_every_suite_as_flake_does_and_records_nothing(self):
        self.declare([
            dict(id="unit", batch_tests=1, data=dict(spec="unit", wrap=True, options=["--skip", "/Z/"])),
            dict(id="events", framework="command", checks=["alpha", "beta"],
                 data=dict(checks=dict(alpha="passed", beta="passed"))),
            dict(id="legacy", kind="probe", framework="exit"),
            dict(id="window", kind="probe", desktop=True, data=dict(spec="window")),
            dict(id="linux-or-darwin", elsewhere=True),
        ], dict(unit=UNIT, window=dict(examples=["W/opens"])))
        self.assertEqual(self.ledger(), ([], []))  # an unseeded ledger
        result = self.shake()
        self.assertEqual((result["outcome"], result["interpretation"]), ("complete", "clean"))
        suites = result["suites"]
        self.assertEqual({k: v["result"] for k, v in suites.items()}, dict(
            unit="clean", events="clean", legacy="clean", window="clean", **{"linux-or-darwin": "skipped"}))
        self.assertTrue(suites["linux-or-darwin"]["reason"].startswith("platform-inapplicable"))
        self.assertEqual(self.ledger(), ([], []))
        self.assertEqual(self.cli("observations", "--run", result["run_id"]), [])
        # batch_tests never slices a shakedown: every listed test ran, and none is in the ledger.
        self.assertEqual(suites["unit"]["listed"], 3)
        self.assertEqual(suites["unit"]["ledger"]["unrecorded"], ["unit::A/one", "unit::A/two", "unit::B/three"])

        run = self.cli("show", result["run_id"])
        self.assertEqual(run["lane"], "shakedown")
        # One trial row per suite, each naming its suite, each with its own evidence directory.
        self.assertEqual([(t["number"], t["document"]["suite"]) for t in run["trials"]],
                         [(1, "unit"), (2, "events"), (3, "legacy"), (4, "window")])
        self.assertEqual({r["test_id"]: r["outcome"] for r in run["results"]}, {
            "unit::A/one": "passed", "unit::A/two": "passed", "unit::B/three": "passed",
            "events::alpha": "passed", "events::beta": "passed", "legacy::run": "passed", "window::W/opens": "passed"})
        unit = run["trials"][0]["document"]
        checkout = Path(suites["unit"]["prepared"]["cwd"])
        exe = str(checkout / "fake_hspec.py")
        evidence = Path(suites["unit"]["artifacts"])
        # The flake lane's launch of the whole suite, exactly: the wrapper, which starts the executable
        # itself, the suite's own options, and no per-test selector.
        self.assertEqual(unit["command"], [sys.executable, str(checkout / "wrap.py"), exe] + hspec.trial_argv(
            exe, None, unit["seed"], str(evidence / "trial-0001.failures"), [], ["--skip", "/Z/"])[1:])
        self.assertNotIn("--match", unit["command"])
        self.assertEqual(Path(unit["log"]), evidence / "trial-0001.log")
        record = json.loads((evidence / "trial-0001.record.json").read_text())
        self.assertEqual(record["env"], dict(FIXTURE_WRAPPED="1", FIXTURE_PREPARED="unit", FIXTURE_HOOK_TRIAL="1",
                                             FIXTURE_CHECKS="{}", FIXTURE_EXIT="0",
                                             FIXTURE_EVENTS=str(evidence / "trial-0001.events.jsonl")))
        window = json.loads((Path(suites["window"]["artifacts"]) / "trial-0001.record.json").read_text())
        self.assertEqual((window["env"]["FIXTURE_CONSENT"], window["env"]["FIXTURE_HOOK_TRIAL"]), ("granted", "1"))

        text = Path(result["report"]).read_text()
        self.assertEqual(report.parse(text)["front"]["interpretation_status"], "clean")
        self.assertIn("- `linux-or-darwin`: skipped (platform-inapplicable", text)
        # Advisory: flake selection never reads it; every suite is still unmeasured.
        flake = self.cli("flake", "--owner", "f1")
        self.assertEqual(self.cli("show", flake["run_id"])["document"]["reason"], "never-enumerated")

    def test_build_and_enumeration_failures_do_not_stop_later_suites(self):
        self.declare([dict(id="broken", data=dict(build="fail")),
                      dict(id="unlisted", data=dict(spec="unlisted")),
                      dict(id="unit")], dict(unlisted=dict(examples=["A/one"], dry_fail="no such module")))
        self.cli("flake", "--target", "unit")  # a seeded ledger stays exactly as it is
        result = self.shake()
        suites = result["suites"]
        broken, unlisted = suites["broken"], suites["unlisted"]
        self.assertEqual((broken["result"], unlisted["result"], suites["unit"]["result"]),
                         ("build-failed", "enumeration-failed", "clean"))
        [build] = broken["problems"]
        self.assertIn("the build exited 3", build["reason"])
        self.assertEqual([Path(p).name for p in build["logs"]], ["build.log"])
        self.assertEqual(build["tests"], [])
        [listing] = unlisted["problems"]
        self.assertIn("enumeration failed", listing["reason"])
        self.assertEqual(Path(listing["log"]).name, "enumerate.log")
        self.assertIn("no such module", Path(listing["log"]).read_text())
        self.assertEqual(result["interpretation"], "observations")
        observed = self.observations(result)
        self.assertEqual(sorted(observed), ["broken", "unlisted"])
        for suite_id, kind in (("broken", "build-failed"), ("unlisted", "enumeration-failed")):
            document = observed[suite_id]["document"]
            self.assertEqual((document["kind"], document["tests"]), ("harness", "none"))
            self.assertIn(kind, document["observed"])
            self.assertIn("$assess-tests", document["suggested_follow_up"])
            self.assertIn("harness and environment first", document["suggested_follow_up"])
        self.assertIn(f"runs/{result['run_id']}/broken/build.log", observed["broken"]["document"]["evidence"])
        # Its observations are claimed and assessed like any other.
        claimed = self.cli("assess", "claim", "--owner", "a1", "--run", result["run_id"])
        self.assertEqual(len(claimed["observations"]), 2)

    def test_crashed_timed_out_and_harness_error_trials_are_incomplete_and_keep_their_failures(self):
        stop = dict(examples=["A/one", "A/two", "B/three"], fail={"A/two": [1]})
        self.declare([dict(id="crash", data=dict(spec="crash")),
                      dict(id="hang", trial_seconds=2, data=dict(spec="hang")),
                      dict(id="leak", data=dict(spec="leak")),
                      dict(id="garbled", data=dict(spec="garbled")),
                      dict(id="silent", framework="command", checks=["alpha", "beta"],
                           data=dict(checks={"__none__": True}))],
                     dict(crash=dict(stop, stop=dict(after="A/two", how="crash")),
                          hang=dict(stop, stop=dict(after="A/two", how="hang")),
                          leak=dict(stop, leak=True), garbled=dict(stop, garble=True)))
        result = self.shake()
        suites = result["suites"]
        for suite_id, outcome in (("crash", "crashed"), ("hang", "timeout")):
            entry = suites[suite_id]
            self.assertEqual(entry["result"], "incomplete")
            self.assertEqual(self.problems(entry), [("incomplete", [f"{suite_id}::B/three"]),
                                                    ("failed", [f"{suite_id}::A/two"])])
            self.assertEqual(entry["problems"][0]["outcome"], outcome)
            self.assertTrue(entry["problems"][0]["log"].endswith(f"/{suite_id}/trial-0001.log"))
        self.assertIn("exit status -9", suites["crash"]["problems"][0]["detail"])
        # The trial left a live process behind: a harness error, though every test reported.
        leak = suites["leak"]
        self.assertEqual(self.problems(leak), [("incomplete", []), ("failed", ["leak::A/two"])])
        self.assertEqual(leak["problems"][0]["outcome"], "harness-error")
        # An unreadable failure report is a harness error; the failure the log shows is kept.
        garbled = suites["garbled"]
        self.assertEqual(self.problems(garbled), [("incomplete", []), ("failed", ["garbled::A/two"])])
        self.assertEqual((garbled["problems"][0]["outcome"], garbled["problems"][0]["detail"]),
                         ("harness-error", "unreadable failure report (ValueError)"))
        # The adapter could read nothing from a command suite: a harness error, and no test has a result.
        silent = suites["silent"]
        self.assertEqual(self.problems(silent), [("incomplete", ["silent::alpha", "silent::beta"])])
        self.assertEqual(silent["problems"][0]["outcome"], "harness-error")
        self.assertIn("could not read", silent["problems"][0]["detail"])
        observed = self.observations(result)
        self.assertEqual(sorted(observed), ["crash", "garbled", "hang", "leak", "silent"])
        self.assertIn("(also failed)", observed["crash"]["document"]["title"])
        self.assertEqual(observed["crash"]["document"]["tests"], "crash::B/three, crash::A/two")

    def test_failed_and_unreported_tests_are_each_recorded_in_full(self):
        self.declare([
            dict(id="failing", data=dict(spec="failing")),
            dict(id="quiet", data=dict(spec="quiet")),
            dict(id="both", data=dict(spec="both")),
            dict(id="gated", data=dict(spec="gated")),
            dict(id="checks", framework="command", checks=["alpha", "beta", "gamma", "delta"],
                 data=dict(checks=dict(alpha="unproven", beta="missing", gamma="failed"), exit=1)),
            dict(id="exits", framework="exit", data=dict(exit=4)),
        ], dict(failing=dict(UNIT, fail={"A/two": [1], "B/three": [1]}),
                quiet=dict(UNIT, omit=["B/three"]),
                both=dict(UNIT, fail={"A/one": [1]}, omit=["B/three"]),
                gated=dict(UNIT, pending=["B/three"])))
        result = self.shake()
        suites = result["suites"]
        self.assertEqual(self.problems(suites["failing"]), [("failed", ["failing::A/two", "failing::B/three"])])
        self.assertTrue(suites["failing"]["problems"][0]["failure_report"].endswith("trial-0001.failures"))
        self.assertEqual(self.problems(suites["quiet"]), [("unreported", ["quiet::B/three"])])
        self.assertEqual(suites["quiet"]["result"], "unreported")
        self.assertEqual(self.problems(suites["both"]), [("failed", ["both::A/one"]), ("unreported", ["both::B/three"])])
        self.assertEqual(suites["both"]["result"], "failed")
        # A pending example reported a result: the suite is clean, and the report says it is not a pass.
        self.assertEqual((suites["gated"]["result"], suites["gated"]["not_passed"]),
                         ("clean", {"pending": ["gated::B/three"]}))
        # A check the probe says is missing, and one it never mentions, are unreported; unproven is kept.
        self.assertEqual(self.problems(suites["checks"]), [("failed", ["checks::gamma"]),
                                                           ("unreported", ["checks::beta", "checks::delta"])])
        self.assertEqual(suites["checks"]["not_passed"], {"unproven": ["checks::alpha"]})
        self.assertEqual(self.problems(suites["exits"]), [("failed", ["exits::run"])])
        observed = self.observations(result)
        self.assertEqual(sorted(observed), ["both", "checks", "exits", "failing", "quiet"])
        both = observed["both"]["document"]
        self.assertEqual(both["title"], "shakedown of both: failed (also unreported)")
        self.assertEqual(both["tests"], "both::A/one, both::B/three")
        text = Path(result["report"]).read_text()
        self.assertIn("  - unreported: 1 listed tests reported no result in the completed trial; "
                      "tests (1): `both::B/three`", text)
        self.assertIn("reported pending, not passed (1): `gated::B/three`", text)
        self.assertIn("1 pending in `gated`", text)

    def test_an_interrupted_shakedown_keeps_what_it_recorded_and_lists_the_rest_as_not_run(self):
        self.declare([dict(id="unit"), dict(id="stopped", data=dict(spec="stopped")), dict(id="later")],
                     dict(stopped=dict(UNIT, fail={"A/one": [1]}, stop=dict(after="A/two", how="interrupt"))))
        os.environ["FIXTURE_INTERRUPT_PID"] = str(os.getpid())
        try:
            result = self.shake(code=1)
        finally:
            del os.environ["FIXTURE_INTERRUPT_PID"]
        self.assertEqual(result["outcome"], "interrupted")
        suites = result["suites"]
        self.assertEqual({k: v["result"] for k, v in suites.items()},
                         dict(unit="clean", stopped="incomplete", later="not-run"))
        stopped = suites["stopped"]
        self.assertEqual(self.problems(stopped), [("incomplete", ["stopped::B/three"]), ("failed", ["stopped::A/one"])])
        self.assertEqual(stopped["problems"][0]["outcome"], "interrupted")
        self.assertIn("interrupted", stopped["problems"][0]["detail"])
        run = self.cli("show", result["run_id"])
        self.assertEqual(run["state"], "interrupted")
        self.assertEqual([t["state"] for t in run["trials"]], ["passed", "interrupted"])
        self.assertEqual(sorted(self.observations(result)), ["stopped"])

    def trial(self, result, suite_id):
        run = self.cli("show", result["run_id"])
        [trial] = [t for t in run["trials"] if t["document"]["suite"] == suite_id]
        outcomes = {r["test_id"]: r["outcome"] for r in run["results"] if r["number"] == trial["number"]}
        return trial, outcomes

    def test_a_failure_report_that_cannot_be_read_keeps_the_failures_the_log_shows(self):
        # The failure report's path is a directory, so reading it raises an OSError.
        self.declare([dict(id="unreadable", data=dict(spec="unreadable")), dict(id="unit")],
                     dict(unreadable=dict(UNIT, fail={"A/two": [1]}, report_dir=True)))
        result = self.shake()
        self.assertEqual((result["outcome"], result["suites"]["unit"]["result"]), ("complete", "clean"))
        entry = result["suites"]["unreadable"]
        self.assertEqual(self.problems(entry), [("incomplete", []), ("failed", ["unreadable::A/two"])])
        self.assertEqual(entry["problems"][0]["outcome"], "harness-error")
        self.assertEqual(entry["problems"][0]["detail"], "unreadable failure report (IsADirectoryError)")
        trial, outcomes = self.trial(result, "unreadable")
        # The log's results are recorded, and the guardian's record of the process is kept beside the error.
        self.assertEqual(outcomes, {"unreadable::A/one": "passed", "unreadable::A/two": "failed",
                                    "unreadable::B/three": "passed"})
        document = trial["document"]
        self.assertEqual((trial["state"], document["guardian_outcome"], document["returncode"]),
                         ("harness-error", "failed", 1))
        self.assertTrue(document["log_sha256"])
        self.assertIn("IsADirectoryError", document["failure_report_error"])
        observed = self.observations(result)
        self.assertEqual(sorted(observed), ["unreadable"])
        self.assertEqual(observed["unreadable"]["document"]["title"],
                         "shakedown of unreadable: incomplete (also failed)")

    def test_an_interrupted_trial_keeps_its_logged_failures_when_its_failure_report_cannot_be_read(self):
        self.declare([dict(id="stopped", data=dict(spec="stopped")), dict(id="later")],
                     dict(stopped=dict(UNIT, fail={"A/one": [1]}, report_dir=True,
                                       stop=dict(after="A/two", how="interrupt"))))
        os.environ["FIXTURE_INTERRUPT_PID"] = str(os.getpid())
        try:
            result = self.shake(code=1)
        finally:
            del os.environ["FIXTURE_INTERRUPT_PID"]
        self.assertEqual({k: v["result"] for k, v in result["suites"].items()},
                         dict(stopped="incomplete", later="not-run"))
        stopped = result["suites"]["stopped"]
        self.assertEqual(self.problems(stopped), [("incomplete", ["stopped::B/three"]), ("failed", ["stopped::A/one"])])
        self.assertEqual(stopped["problems"][0]["outcome"], "interrupted")
        trial, outcomes = self.trial(result, "stopped")
        self.assertEqual(outcomes, {"stopped::A/one": "failed", "stopped::A/two": "passed",
                                    "stopped::B/three": "incomplete"})
        self.assertEqual((trial["state"], trial["document"]["guardian_outcome"]), ("interrupted", "interrupted"))
        self.assertIn("IsADirectoryError", trial["document"]["failure_report_error"])

    def test_malformed_hook_keys_are_this_suites_harness_error_and_later_suites_still_run(self):
        # The outcomes hook returns {"alpha": "failed", 1: "passed", "unknown": "passed"}.
        self.declare([dict(id="mixed", framework="command", checks=["alpha", "beta"],
                           data=dict(checks=[["alpha", "failed"], [1, "passed"], ["unknown", "passed"]])),
                      dict(id="unit")])
        result = self.shake()
        self.assertEqual((result["outcome"], result["interpretation"]), ("complete", "observations"))
        self.assertEqual({k: v["result"] for k, v in result["suites"].items()}, dict(mixed="incomplete", unit="clean"))
        mixed = result["suites"]["mixed"]
        # The readable failure is kept; the check the malformed report cannot vouch for has no result.
        self.assertEqual(self.problems(mixed), [("incomplete", ["mixed::beta"]), ("failed", ["mixed::alpha"])])
        self.assertEqual(mixed["problems"][0]["outcome"], "harness-error")
        self.assertIn("unknown checks ['unknown', 1]", mixed["problems"][0]["detail"])
        trial, outcomes = self.trial(result, "mixed")
        self.assertEqual(outcomes, {"mixed::alpha": "failed", "mixed::beta": "incomplete"})
        self.assertEqual((trial["state"], trial["document"]["guardian_outcome"]), ("harness-error", "passed"))
        self.assertEqual(sorted(self.observations(result)), ["mixed"])

    def test_deferred_suites_are_skipped_and_cannot_be_targeted(self):
        self.declare([dict(id="unit"), dict(id="later", data=dict(spec="spec"))])
        self.cli("defer", "later", "--reason", "waits on the renderer", "--resume-when", "#9 merges")
        result = self.shake()
        self.assertEqual(result["suites"]["later"], dict(result="skipped", reason="deferred: waits on the renderer"))
        self.assertEqual(result["suites"]["unit"]["result"], "clean")
        refused = self.cli("shakedown", "--target", "later", code=2)
        self.assertIn("later is deferred: waits on the renderer", refused["error"])

    def test_a_suite_or_desktop_claimed_elsewhere_is_busy(self):
        self.declare([dict(id="unit"), dict(id="window", desktop=True, data=dict(spec="spec")),
                      dict(id="other", data=dict(spec="spec"))])
        state = self.state()
        self.assertIsNone(state.claim("suite:unit", "flake-1", "flake"))
        self.assertIsNone(state.claim("desktop", "test-2", "test"))
        result = self.shake()
        suites = result["suites"]
        self.assertEqual(suites["unit"], dict(result="busy", reason="claimed by flake-1 (flake)"))
        self.assertEqual(suites["window"], dict(result="busy", reason="desktop in use by test-2 (test)"))
        self.assertEqual(suites["other"]["result"], "clean")
        self.assertEqual(result["interpretation"], "inconclusive")
        self.assertEqual(self.cli("observations", "--run", result["run_id"]), [])
        # The other lanes' claims are theirs; the shakedown released only its own.
        self.assertEqual(sorted(c["resource"] for c in self.cli("claims")), ["desktop", "suite:unit"])
        state.db.close()

    def test_new_and_vanished_tests_are_reported_and_not_recorded(self):
        self.cli("flake", "--target", "unit")
        self.spec(dict(examples=["A/one", "A/two", "B/four"]))
        self.push("B/three becomes B/four")
        result = self.shake("--target", "unit")
        self.assertEqual(result["suites"]["unit"]["ledger"], dict(unrecorded=["unit::B/four"],
                                                                  unlisted=["unit::B/three"]))
        self.assertEqual(result["suites"]["unit"]["result"], "clean")
        statuses = self.statuses()
        self.assertNotIn("unit::B/four", statuses)
        self.assertNotEqual(statuses["unit::B/three"], "retired")
        text = Path(result["report"]).read_text()
        self.assertIn("not in the ledger, and not recorded (1): `unit::B/four`", text)
        self.assertIn("in the ledger but no longer listed, and not retired (1): `unit::B/three`", text)

    def test_the_upstream_head_is_shaken_down_whatever_the_checkout_holds(self):
        upstream = subprocess_output(self.work, "rev-parse", "origin/master")
        git(self.work, "checkout", "-q", "-b", "local")
        self.spec(dict(UNIT, fail={"A/one": [1]}))
        git(self.work, "commit", "-qam", "a local failure")
        result = self.shake("--target", "unit")
        self.assertEqual((result["revision"], result["suites"]["unit"]["result"]), (upstream, "clean"))
        self.assertTrue(self.cli("show", result["run_id"])["upstream"])

    def test_the_command_takes_only_a_suite_at_the_upstream_head(self):
        self.declare([dict(id="unit"), dict(id="elsewhere", elsewhere=True, data=dict(spec="spec"))])
        self.cli("flake", "--target", "unit")
        refused = self.cli("shakedown", "--target", "unit::A/one", code=2)
        self.assertIn("is not a suite", refused["error"])
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as stopped:
            cli.main(["--repo", str(self.work), "shakedown", "--ref", "HEAD"])
        self.assertEqual(stopped.exception.code, 2)
        self.assertIn("unrecognized arguments: --ref", err.getvalue())
        # A targeted suite that does not apply here is skipped, not refused and not run.
        result = self.shake("--target", "elsewhere")
        self.assertEqual(result["suites"]["elsewhere"]["result"], "skipped")
        self.assertEqual((result["interpretation"], self.cli("show", result["run_id"])["trials"]), ("inconclusive", []))


class ProbeReportShakedownTests(LabFixture):
    """The fixture's command probe reports through $QURUNTUL_PROBE_RESULT rather than a hook."""

    def test_a_probe_report_is_read_and_a_malformed_one_is_a_harness_error_keeping_its_failures(self):
        before = self.ledger_rows()
        result = self.cli("shakedown", "--target", "probe-a")
        entry = result["suites"]["probe-a"]
        self.assertEqual([(p["kind"], p["tests"]) for p in entry["problems"]], [("failed", ["probe-a::beta"])])
        (self.work / "probe.py").write_text(
            'import json, os\n'
            'json.dump({"schema": "quruntul-probe/v1", "checks": {"alpha": "passed", "beta": "failed", '
            '"gamma": "passed"}}, open(os.environ["QURUNTUL_PROBE_RESULT"], "w"))\n'
            'raise SystemExit(1)\n')
        self.push("an undeclared check")
        result = self.cli("shakedown", "--target", "probe-a")
        entry = result["suites"]["probe-a"]
        self.assertEqual([(p["kind"], p["tests"]) for p in entry["problems"]],
                         [("incomplete", ["probe-a::alpha"]), ("failed", ["probe-a::beta"])])
        self.assertEqual(entry["problems"][0]["outcome"], "harness-error")
        self.assertIn("unknown checks ['gamma']", entry["problems"][0]["detail"])
        self.assertEqual(self.ledger_rows(), before)

    def ledger_rows(self):
        return ShakedownFixture.ledger(self)


def subprocess_output(cwd, *args):
    import subprocess
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


if __name__ == "__main__":
    unittest.main()
