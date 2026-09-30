"""Pure pieces: Hspec parsing, report validation, selection, ledger rules."""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from quruntul import hspec, report, select  # noqa: E402
from quruntul.adapter import Suite  # noqa: E402
from quruntul.common import LabError  # noqa: E402
from quruntul.state import State  # noqa: E402

CHECKS = """
Lua
  faults
    reports a syntax error [✔]
    keeps a cancellation [✘]
  close
    treats a second close as a no-op [✔]
    waits for a pending thing [‐]
Protocol
  a top-level item [✔]

Failures:
  test/Lua.hs:10:3:
  1) Lua, faults, keeps a cancellation [✔]

Finished in 0.1 seconds
"""


def suite(identity="i1", **changes):
    fields = dict(id="unit", kind="ci", framework="hspec", description="a suite", identity=identity)
    fields.update(changes)
    return Suite(**fields).validate()


class HspecTests(unittest.TestCase):
    def test_checks_output_maps_every_item_by_path(self):
        parsed = hspec.parse_checks(CHECKS)
        self.assertEqual(parsed, {
            "Lua/faults/reports a syntax error": "passed",
            "Lua/faults/keeps a cancellation": "failed",
            "Lua/close/treats a second close as a no-op": "passed",
            "Lua/close/waits for a pending thing": "pending",
            "Protocol/a top-level item": "passed",
        })

    def test_carriage_return_keeps_only_the_final_state(self):
        self.assertEqual(hspec.parse_checks("G\n  item [ ]\r  item [✔]\n"), {"G/item": "passed"})

    def test_output_that_fits_no_known_path_is_ignored(self):
        text = ("G\n  sub\n    one [✔]\nchild: all spec items have been filtered\n    two [✔]\n"
                "  other\n    stray child line\n    three [‐]\n")
        known = {"G/sub/one", "G/sub/two", "G/other/three"}
        self.assertEqual(hspec.parse_checks(text, known),
                         {"G/sub/one": "passed", "G/sub/two": "passed", "G/other/three": "pending"})
        # Without the known paths the stray line reads as a group and misnames what follows.
        self.assertNotIn("G/sub/two", hspec.parse_checks(text))

    def test_failure_report_decodes_haskell_strings(self):
        text = ('FailureReport {failureReportSeed = 1, failureReportPaths = '
                '[(["A","B \\"q\\""],"ex/1 \\1234\\&5"),([],"top"),(["G"],"tab\\there")]}')
        self.assertEqual(hspec.parse_failure_report(text), ['A/B "q"/ex/1 Ӓ5', "top", "G/tab\there"])
        self.assertEqual(hspec.parse_failure_report("FailureReport {failureReportPaths = []}"), [])
        with self.assertRaises(ValueError):
            hspec.parse_failure_report("nothing")

    def test_exact_selection_supersedes_a_profile_match_but_keeps_its_skips(self):
        profile = ["--match", "/P/", "--skip", "/P/slow/", "--match=/Q/", "--times"]
        selected = hspec.trial_argv("exe", ["P/a"], 1, "/r", None, profile)
        self.assertEqual([a for a in selected if a.startswith("/") and a != "/r"], ["/P/a/", "/P/slow/"])
        self.assertNotIn("--match=/Q/", selected)
        self.assertIn("--times", selected)
        whole = hspec.trial_argv("exe", None, 1, "/r", None, profile)
        self.assertEqual(whole[-len(profile):], profile)

    def test_selection_is_exact_by_path(self):
        argv = hspec.trial_argv("exe", ["A/b"], 7, "/tmp/r", ["-N2"], ["--extra"])
        self.assertIn("--seed=7", argv)
        self.assertEqual(argv[argv.index("--match") + 1], "/A/b/")
        self.assertEqual(argv[-3:], ["+RTS", "-N2", "-RTS"])


class ReportTests(unittest.TestCase):
    RUN = dict(id="r1", lane="test", suite="s", revision="a" * 40, upstream=1)

    def observation(self, **changes):
        base = dict(title="thing broke", area="lua", kind="product", tests="s::x", evidence="log line 3",
                    expected="ok", observed="not ok", confidence="high", follow_up="look")
        base.update(changes)
        return base

    def test_skeleton_must_be_completed(self):
        with self.assertRaisesRegex(LabError, "REPLACE"):
            report.parse(report.skeleton(self.RUN, {}, "- facts"))

    def test_statuses_and_observations_agree(self):
        clean = report.skeleton(self.RUN, {}, "- f", "clean", "fine", "none", [])
        self.assertEqual(report.parse(clean)["observations"], [])
        with self.assertRaisesRegex(LabError, "needs at least one"):
            report.parse(report.skeleton(self.RUN, {}, "- f", "observations", "x", "y", []))
        with self.assertRaisesRegex(LabError, "no observations"):
            report.parse(report.skeleton(self.RUN, {}, "- f", "clean", "x", "y", [self.observation()]))
        parsed = report.parse(report.skeleton(self.RUN, {}, "- f", "observations", "x", "y",
                                              [self.observation(), self.observation(title="second")]))
        self.assertEqual([o["title"] for o in parsed["observations"]], ["thing broke", "second"])
        self.assertEqual(parsed["observations"][0]["suggested_follow_up"], "look")

    def test_bad_observation_fields_refused(self):
        with self.assertRaisesRegex(LabError, "kind"):
            report.parse(report.skeleton(self.RUN, {}, "- f", "observations", "x", "y", [self.observation(kind="bug")]))


class SelectionTests(unittest.TestCase):
    def test_flake_order_prefers_fix_verification_then_new_tests(self):
        suites = [suite(id="a"), suite(id="b"), suite(id="c"), suite(id="d", identity="changed"), suite(id="e")]
        ledger = {"b": dict(enumerated="t", identity="i1"), "d": dict(enumerated="t", identity="old"),
                  "e": dict(enumerated="t", identity="i1"), "a": dict(enumerated="t", identity="i1")}
        tests = {"a": [dict(id="a::x", status="fixing")], "b": [dict(id="b::y", status="new")],
                 "e": [dict(id="e::z", status="stable")]}
        order, skipped = select.flake_order(suites, ledger, tests, {}, {}, "Darwin", "me", {"a::x"})
        self.assertEqual([(s.id, why) for s, why in order],
                         [("a", "verify-fix"), ("b", "new-tests"), ("c", "never-enumerated"), ("d", "changed")])
        self.assertIn("e", skipped)

    def test_stable_tests_are_never_reselected_however_old(self):
        order, skipped = select.flake_order([suite()], {"unit": dict(enumerated="2020-01-01", identity="i1")},
                                            {"unit": [dict(id="unit::x", status="stable")]}, {}, {}, "Linux", "me")
        self.assertEqual(order, [])
        self.assertIn("no new tests", skipped["unit"])

    def test_blockers_skip_before_ranking(self):
        suites = [suite(id="deferred"), suite(id="mac", platforms=["Darwin"]), suite(id="held"),
                  suite(id="window", desktop=True)]
        claims = {"suite:held": dict(owner="other", lane="flake"), "desktop": dict(owner="other", lane="flake")}
        order, skipped = select.flake_order(suites, {}, {}, {"deferred": dict(reason="waiting")}, claims, "Linux", "me")
        self.assertEqual(order, [])
        self.assertTrue(skipped["deferred"].startswith("deferred"))
        self.assertEqual(skipped["mac"], "platform-inapplicable")
        self.assertIn("claimed by other", skipped["held"])
        self.assertIn("desktop in use", skipped["window"])

    def test_test_lane_runs_probes_only_and_refreshes_stale_ones(self):
        now = datetime(2026, 9, 29, tzinfo=timezone.utc)
        suites = [suite(id="ci"), suite(id="p-new", kind="probe"), suite(id="p-fresh", kind="probe"),
                  suite(id="p-stale", kind="probe"), suite(id="p-changed", kind="probe", identity="i2")]
        fresh = (now - timedelta(days=1)).isoformat()
        ledger = {"p-fresh": dict(last_test_run=fresh, last_test_identity="i1"),
                  "p-stale": dict(last_test_run=(now - timedelta(days=30)).isoformat(), last_test_identity="i1"),
                  "p-changed": dict(last_test_run=fresh, last_test_identity="i1")}
        order, skipped = select.test_order(suites, ledger, {}, {}, "Linux", "me", 7, now)
        self.assertEqual([(s.id, why) for s, why in order],
                         [("p-new", "never-tested"), ("p-changed", "changed"), ("p-stale", "stale")])
        self.assertNotIn("ci", skipped)
        self.assertIn("p-fresh", skipped)
        order, _ = select.test_order(suites, ledger, {}, {}, "Linux", "me", 7, now, hint="stale")
        self.assertEqual([s.id for s, _ in order], ["p-stale"])

    def test_deflake_prefers_the_worst_unclaimed_flaky_test(self):
        tests = [dict(id="a", suite="s", status="flaky", failures=1, trials=10, last_failure="2026-01-01"),
                 dict(id="b", suite="s", status="flaky", failures=5, trials=10, last_failure="2026-01-01"),
                 dict(id="c", suite="s", status="flaky", failures=5, trials=10, last_failure="2026-02-01"),
                 dict(id="d", suite="s", status="flaky", failures=9, trials=10, last_failure="2026-02-01"),
                 dict(id="e", suite="s", status="stable", failures=0, trials=10, last_failure=None)]
        ranked = select.deflake_order(tests, {"test:d": dict(owner="other")}, {}, "me")
        self.assertEqual([t["id"] for t in ranked], ["c", "b", "a"])


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = State(Path(self.temp.name))

    def tearDown(self):
        self.state.db.close()
        self.temp.cleanup()

    def test_enumeration_adds_new_and_retires_only_at_upstream(self):
        s = suite().record()
        self.state.enumerated(s, "i1", "r1", ["a", "b"], True)
        self.assertEqual({t["id"]: t["status"] for t in self.state.tests()}, {"unit::a": "new", "unit::b": "new"})
        self.state.set_status("unit::a", "stable", "clean batch")
        self.state.enumerated(s, "i2", "r2", ["a"], False)
        self.assertEqual(self.state.test("unit::b")["status"], "new")
        change = self.state.enumerated(s, "i3", "r3", ["a", "c"], True)
        self.assertEqual(change["retired"], ["unit::b"])
        self.assertEqual(self.state.test("unit::a")["status"], "stable")
        self.assertEqual(self.state.test("unit::c")["status"], "new")
        change = self.state.enumerated(s, "i4", "r4", ["a", "b", "c"], True)
        self.assertEqual(change["revived"], ["unit::b"])
        self.assertEqual(self.state.test("unit::b")["status"], "new")

    def test_status_changes_are_recorded_events(self):
        self.state.enumerated(suite().record(), "i", "r", ["a"], True)
        self.state.set_status("unit::a", "flaky", "failed in CI", dict(evidence="run 5"))
        event = self.state.events("unit::a")[0]
        self.assertEqual((event["kind"], event["document"]["previous"], event["document"]["status"]),
                         ("status", "new", "flaky"))
        with self.assertRaises(LabError):
            self.state.set_status("unit::a", "stable", " ")

    def test_claims_exclude_live_holders_and_take_over_stale_ones(self):
        self.assertIsNone(self.state.claim("suite:x", "a", "flake"))
        self.assertEqual(self.state.claim("suite:x", "b", "flake")["owner"], "a")
        self.assertIsNone(self.state.claim("suite:x", "a", "flake"))  # re-entrant for its owner
        self.state.db.execute("UPDATE claims SET heartbeat=? WHERE resource='suite:x'", (time.time() - 10_000,))
        self.assertIsNone(self.state.claim("suite:x", "b", "flake"))
        self.assertEqual(self.state.held("suite:x")["owner"], "b")
        self.assertEqual(self.state.events("suite:x")[0]["kind"], "claim-takeover")

    def test_trials_are_immutable_once_finished(self):
        run = self.state.begin("flake", "unit", "r", "origin/master", True, {})
        self.state.start_trial(run, 1, {})
        self.state.finish_trial(run, 1, dict(outcome="passed"), {"unit::a": "passed"})
        self.state.finish_trial(run, 1, dict(outcome="passed"), {"unit::a": "passed"})  # idempotent
        with self.assertRaisesRegex(LabError, "immutable"):
            self.state.finish_trial(run, 1, dict(outcome="failed"), {})

    def test_assessment_needs_the_proposed_hash(self):
        run = self.state.begin("test", "p", "r", "origin/master", True, {})
        [obs] = self.state.add_observations(run, [dict(title="t", area="x")])
        assessment = self.state.claim_observations("me", [obs])
        with self.assertRaises(LabError):
            self.state.claim_observations("you", [obs])
        sha = self.state.propose_assessment(assessment, "## FND-001\n")
        with self.assertRaises(LabError):
            self.state.approve_assessment(assessment, "0" * 64)
        self.state.approve_assessment(assessment, sha)
        self.assertEqual(self.state.observations()[0]["status"], "assessed")
        self.state.record_issue(assessment, "FND-001", "https://example/1")
        self.assertEqual(self.state.assessment(assessment)["document"]["issues"], {"FND-001": "https://example/1"})

    def test_proposals_deduplicate_by_target(self):
        proposal = dict(target_id="probe:x", lane="test", question="q", gap="g", scenario="s", oracle="o",
                        cost="c", tier="probe", revision="r")
        first, created = self.state.propose(proposal)
        again, created_again = self.state.propose(dict(proposal, question="other"))
        self.assertEqual((first, created, created_again), (again, True, False))
        self.state.close_proposal(first, "accepted", "owner approved")
        self.assertEqual(self.state.proposals()[0]["status"], "accepted")

    def test_future_schema_refused(self):
        self.state.db.execute("PRAGMA user_version=99")
        with self.assertRaisesRegex(LabError, "unsupported"):
            State(Path(self.temp.name))


if __name__ == "__main__":
    unittest.main()
