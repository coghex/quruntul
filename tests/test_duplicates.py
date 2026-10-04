"""Duplicate Hspec identities across all lanes, using isolated fixture repositories only."""
import json
from pathlib import Path
from unittest.mock import patch

from quruntul import hspec, report
from quruntul.lab import Lab, SHAKEDOWN_PROBLEMS
from quruntul.state import STATUSES
from tests.test_lab import git
from tests.test_shakedown import ShakedownFixture

TWINS = {"A/g/twin": 2, "A/h/p": 2}
SPEC = dict(examples=["A/g/twin", "A/g/other", "A/g/twin", "A/h/p", "A/h/p", "B/g/twin"])
SEED = '''
    def seed(self, ctx, suite, trial):
        (ctx.artifacts / "seed.json").write_text(json.dumps(trial["added"]))
        return {p: {"status": "stable", "reason": "fixture legacy", "evidence": "fixture"}
                for p in trial["added"] if p == "B/g/twin"}
'''


class DuplicateTests(ShakedownFixture):
    def configure(self, spec=SPEC, kind="ci"):
        adapter = self.work / ".quruntul/adapter.py"
        text = adapter.read_text()
        if 'def seed(' not in text:
            adapter.write_text(text.replace('    def outcomes(', SEED + '\n    def outcomes('))
        self.spec(spec)
        (self.work / "suites.json").write_text(json.dumps([dict(id="unit", kind=kind)]))
        git(self.work, "add", "-A")
        git(self.work, "commit", "--allow-empty", "-qm", "configure duplicate fixture")
        git(self.work, "push", "-q", "origin", "master")

    def run_record(self, lane="flake"):
        state = self.state()
        try:
            [run] = state.runs(lane=lane, limit=1)
            return run, state.trials(run["id"]), state.results(run["id"])
        finally:
            state.db.close()

    def snapshot(self, paths):
        state = self.state()
        try:
            return [(state.test("unit::" + p), state.events("unit::" + p)) for p in paths]
        finally:
            state.db.close()

    def assert_report(self, run, failures=()):
        directory = self.work / ".git/quruntul/runs" / run["id"]
        data = json.loads((directory / "result.json").read_text())
        self.assertEqual(data["summary"]["duplicates"], TWINS)
        text = (directory / "report.md").read_text()
        for path, count in TWINS.items():
            self.assertIn(f"`{path}` (count {count})", text)
        self.assertIn("enumerate.log", text)
        self.assertIn("stay unmeasured until uniquely named", text)
        for path in failures:
            self.assertIn("Reported failure under duplicated paths", text)
            self.assertIn(path, text)
            self.assertIn("trial-0001", text)
        return text

    def test_occurrences_count_whole_paths_and_keep_failed_then_passed_twins(self):
        text = "A\n  g\n    twin [✘]\n    other [✔]\n    twin [✔]\n  h\n    p [‐]\n    p [✔]\nB\n  g\n    twin [✔]\nFinished in 0.1 seconds\n"
        self.assertEqual(hspec.enumeration(text), (["A/g/other", "B/g/twin"], TWINS))
        self.assertEqual(hspec.enumerate_examples(text).count("A/g/twin"), 2)
        self.assertIn(("A/g/twin", "failed"), hspec.check_occurrences(text, set(TWINS)))

    def test_flake_excludes_twins_but_measures_and_seeds_unique_paths(self):
        self.configure()
        result = self.cli("flake", "--target", "unit")
        run, trials, results = self.run_record()
        self.assertEqual(run["document"]["duplicates"], TWINS)
        self.assertEqual(run["document"]["selected"], ["unit::A/g/other"])
        self.assertEqual(self.statuses(), {"unit::A/g/other": "stable", "unit::B/g/twin": "stable"})
        self.assertEqual({r["test_id"] for r in results}, {"unit::A/g/other"})
        directory = Path(result["result"]).parent
        self.assertEqual(json.loads((directory / "seed.json").read_text()), ["A/g/other", "B/g/twin"])
        for trial in trials:
            argv = trial["document"]["command"]
            self.assertIn("/A/g/other/", argv)
            self.assertNotIn("/A/g/twin/", argv)
        text = self.assert_report(run)
        parsed = report.parse(text)
        self.assertEqual(parsed["front"]["interpretation_status"], "observations")
        self.assertEqual([o["kind"] for o in parsed["observations"]], ["harness"])
        self.assertEqual(result["summary"]["measured"]["unit::A/g/other"]["became"], "stable")

    def test_all_existing_status_rows_and_evidence_are_preserved_in_all_lanes(self):
        paths = ["A/" + status for status in STATUSES]
        self.configure(dict(examples=paths + ["U/one"]), kind="probe")
        self.cli("test", "--target", "unit")
        state = self.state()
        try:
            for status, path in zip(STATUSES, paths):
                state.set_status("unit::" + path, status, "fixture", {"old": "evidence"})
                state.db.execute("UPDATE tests SET last_seen='old', trials=17, failures=3, last_run='old-run', "
                                 "last_outcome='failed', last_failure='old-failure', measured_revision='old-rev', "
                                 "pr='7', note='metadata' WHERE id=?", ("unit::" + path,))
        finally:
            state.db.close()
        before = self.snapshot(paths)
        self.configure(dict(examples=[p for p in paths for _ in range(2)] + ["U/one"]), kind="probe")
        for lane in ("flake", "test", "shakedown"):
            with self.subTest(lane=lane), patch.object(Lab, "merged_fixing", return_value={"unit::A/fixing"}):
                self.cli(lane, "--target", "unit")
                self.assertEqual(self.snapshot(paths), before)
                run, _, results = self.run_record(lane)
                self.assertEqual({r["test_id"] for r in results}, {"unit::U/one"})
                if lane == "flake":
                    self.assertTrue(set(run["document"]["selected"]).isdisjoint({"unit::" + p for p in paths}))
        self.configure(dict(examples=paths + ["U/one"]), kind="probe")
        self.cli("flake", "--target", "unit::A/new")
        self.assertEqual(self.statuses()["unit::A/new"], "stable")
        self.configure(dict(examples=["U/one"]), kind="probe")
        self.cli("flake", "--target", "unit")
        self.assertEqual(set(self.statuses()["unit::" + p] for p in paths), {"retired"})

    def test_explicit_target_candidate_and_merged_fix_cannot_select_duplicated_rows(self):
        self.configure(dict(examples=["A/g/twin", "U/one"]))
        self.cli("flake", "--target", "unit")
        state = self.state()
        try:
            state.set_status("unit::A/g/twin", "fixing", "fixture", {"pr": "7"})
        finally:
            state.db.close()
        before = self.snapshot(["A/g/twin"])
        git(self.work, "checkout", "-qb", "candidate")
        self.spec(dict(examples=["A/g/twin", "A/g/twin", "U/one", "U/new"]))
        git(self.work, "commit", "-qam", "candidate twins")
        with patch.object(Lab, "merged_fixing", return_value={"unit::A/g/twin"}):
            for target in ("unit::A/g/twin", "unit"):
                self.cli("flake", "--target", target, "--ref", "candidate")
                run, _, results = self.run_record()
                self.assertNotIn("unit::A/g/twin", run["document"]["selected"])
                self.assertNotIn("unit::A/g/twin", [r["test_id"] for r in results])
                self.assertEqual(self.snapshot(["A/g/twin"]), before)
                self.assertFalse(run["upstream"])

    def test_duplicate_only_enumeration_is_nothing_new_but_probes_and_shakedowns_execute(self):
        spec = dict(examples=[p for p in TWINS for _ in range(2)])
        self.configure(spec, kind="probe")
        self.assertEqual(self.cli("flake", "--target", "unit")["outcome"], "no-candidate")
        run, trials, results = self.run_record()
        self.assertEqual(run["state"], "nothing-new")
        self.assertEqual((trials, results, self.statuses()), ([], [], {}))
        self.assert_report(run)
        for lane in ("test", "shakedown"):
            self.cli(lane, "--target", "unit")
            run, trials, results = self.run_record(lane)
            self.assertEqual(len(trials), 1)
            self.assertEqual(results, [])
            self.assertEqual(self.statuses(), {})
            if lane == "test":
                self.assert_report(run)
            else:
                self.assertEqual(run["summary"]["suites"]["unit"]["result"], "duplicated")

    def test_failure_diagnostics_keep_each_channel_without_attributing_twins(self):
        # Fixture also_run models extra formatter output beyond exact selectors.
        for channel in ("checks-missing-report", "checks-malformed-report", "checks-unreadable-report", "failure-report"):
            for lane in ("flake", "test", "shakedown"):
                with self.subTest(channel=channel, lane=lane):
                    spec = dict(SPEC, occurrences={"A/g/twin": ["failed", "passed"], "A/h/p": ["pending", "passed"]},
                                also_run=list(TWINS))
                    if channel == "checks-missing-report":
                        spec["no_report"] = True
                    elif channel == "checks-malformed-report":
                        spec["garble"] = True
                    elif channel == "checks-unreadable-report":
                        spec["report_dir"] = True
                    else:
                        spec["omit"] = ["A/g/twin"]
                    self.configure(spec, kind="probe")
                    result = self.cli(lane, "--target", "unit")
                    run, trials, results = self.run_record(lane)
                    self.assertEqual({r["test_id"] for r in results}, {"unit::A/g/other", "unit::B/g/twin"}
                                     if lane != "flake" or channel != "checks-missing-report" else {"unit::A/g/other"})
                    failures = [f for t in trials for f in t["document"].get("duplicate_failures", [])]
                    self.assertTrue(failures)
                    self.assertEqual({f["path"] for f in failures}, {"A/g/twin"})
                    self.assertTrue(all(f.get("failure_report") for f in failures) if channel == "failure-report"
                                    else all(f.get("log") for f in failures))
                    if lane in ("flake", "test"):
                        self.assert_report(run, ["A/g/twin"])
                    else:
                        entry = result["suites"]["unit"]
                        self.assertEqual(entry["result"], "duplicated")
                        problem = entry["problems"][0]
                        self.assertEqual(problem["duplicates"], TWINS)
                        self.assertEqual(problem["tests"], [])
                        self.assertEqual({f["path"] for f in problem["failures"]}, {"A/g/twin"})
                        self.assertNotIn("A/h/p", str(entry.get("not_passed")))
                        text = Path(result["report"]).read_text()
                        self.assertIn("Reported failure under duplicated paths", text)
                        self.assertIn("trial-0001", text)
                    self.assertIsNone(self.state_test("unit::A/g/twin"))

    def state_test(self, test_id):
        state = self.state()
        try:
            return state.test(test_id)
        finally:
            state.db.close()

    def test_shakedown_duplicate_problem_precedes_unique_failure_and_has_no_disappearance_drift(self):
        self.configure(dict(examples=["A/g/twin", "U/bad"]))
        self.cli("flake", "--target", "unit")
        before = self.snapshot(["A/g/twin"])
        self.configure(dict(examples=["A/g/twin", "A/g/twin", "U/bad"], fail={"U/bad": [1]}))
        result = self.shake("--target", "unit")
        entry = result["suites"]["unit"]
        self.assertEqual([p["kind"] for p in entry["problems"]], ["duplicated", "failed"])
        self.assertEqual(entry["problems"][1]["tests"], ["unit::U/bad"])
        self.assertEqual(entry["ledger"]["unlisted"], [])
        self.assertEqual(self.snapshot(["A/g/twin"]), before)
        self.assertEqual(SHAKEDOWN_PROBLEMS, ("build-failed", "enumeration-failed", "duplicated", "incomplete", "failed", "unreported"))
