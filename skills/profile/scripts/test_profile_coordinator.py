#!/usr/bin/env python3
"""Focused end-to-end tests for the local performance lab coordinator."""

from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).with_name("profile_coordinator.py")


class CoordinatorCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="profile-coordinator-test-")
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-b", "master")
        self.git("config", "user.email", "profile-test@example.invalid")
        self.git("config", "user.name", "Profile Test")
        (self.repo / "tracked.txt").write_text("first\n", encoding="utf-8")
        self.git("add", "tracked.txt")
        self.git("commit", "-m", "initial")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def git(self, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(cwd or self.repo), *args],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )

    def cli(self, *args: str, expect: int = 0) -> subprocess.CompletedProcess[str]:
        self.assertTrue(args)
        result = subprocess.run(
            [sys.executable, str(SCRIPT), args[0], "--repo", str(self.repo), *args[1:]],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(
            result.returncode,
            expect,
            msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )
        return result

    def refresh(self) -> dict:
        return json.loads(self.cli("refresh", "--base-ref", "master").stdout)

    def claim(self, snapshot_id: str, profile_id: str = "example:cpu") -> dict:
        return json.loads(
            self.cli(
                "claim",
                "--snapshot-id",
                snapshot_id,
                "--profile-id",
                profile_id,
                "--area",
                "example",
                "--workload-id",
                "example-workload",
                "--metric-family",
                "throughput",
                "--instrument",
                "prod-rts-stats",
                "--question",
                "Where is time spent?",
                "--rationale",
                "No current evidence exists.",
                "--comparison-key",
                "example/workload/throughput/prod-rts-v1",
            ).stdout
        )

    @staticmethod
    def complete_profile_report(path: Path, outcome: str = "complete") -> None:
        text = path.read_text(encoding="utf-8")
        text = text.replace(
            'measurement_status: "<REPLACE: complete | inconclusive | blocked>"',
            f'measurement_status: "{outcome}"',
        )
        text = re.sub(r"<REPLACE:[^>]*>", "test evidence", text)
        path.write_text(text, encoding="utf-8")

    @staticmethod
    def complete_assessment(path: Path, outcome: str = "recommendations") -> None:
        text = path.read_text(encoding="utf-8")
        text = text.replace(
            'assessment_outcome: "<REPLACE: recommendations | follow-up-measurement | no-action>"',
            f'assessment_outcome: "{outcome}"',
        )
        text = re.sub(r"<REPLACE:[^>]*>", "test evidence", text)
        path.write_text(text, encoding="utf-8")

    def completed_run(self, target_ref: str | None = None, profile_id: str = "example:cpu") -> str:
        refresh_args = ["refresh", "--base-ref", "master"]
        if target_ref:
            refresh_args.extend(["--target-ref", target_ref])
        snapshot = json.loads(self.cli(*refresh_args).stdout)
        run = self.claim(snapshot["snapshot_id"], profile_id)
        run_id = run["run_id"]
        self.cli("prepare", "--run-id", run_id)
        self.cli(
            "exec",
            "--run-id",
            run_id,
            "--phase",
            "measure",
            "--final",
            "--",
            sys.executable,
            "-c",
            "print('ok')",
        )
        report_path = Path(self.cli("report-init", "--run-id", run_id).stdout.strip())
        self.complete_profile_report(report_path)
        self.cli("report", "--run-id", run_id, "--outcome", "complete")
        self.cli("restore", "--run-id", run_id)
        return run_id

    def completed_assessment(
        self, run_id: str, outcome: str = "recommendations", base_ref: str | None = None
    ) -> dict:
        claim_args = ["analysis-claim", "--run", run_id]
        if base_ref:
            claim_args.extend(["--base-ref", base_ref])
        assessment = json.loads(self.cli(*claim_args).stdout)
        assessment_id = assessment["assessment_id"]
        self.cli("analysis-worktree", "--assessment-id", assessment_id)
        path = Path(self.cli("assessment-init", "--assessment-id", assessment_id).stdout.strip())
        self.complete_assessment(path, outcome=outcome)
        completed = json.loads(
            self.cli(
                "analysis-complete",
                "--assessment-id",
                assessment_id,
                "--outcome",
                outcome,
            ).stdout
        )
        self.cli("analysis-cleanup", "--assessment-id", assessment_id)
        return completed

    def test_full_profile_and_performance_lifecycle(self) -> None:
        initialized = json.loads(self.cli("init").stdout)
        snapshot = self.refresh()
        run = self.claim(snapshot["snapshot_id"])
        run_id = run["run_id"]

        prepared = json.loads(self.cli("prepare", "--run-id", run_id).stdout)
        self.assertEqual(prepared["status"], "prepared")
        worktree = Path(prepared["worktree_path"])
        self.assertEqual(
            self.git("rev-parse", "HEAD", cwd=worktree).stdout.strip(), snapshot["revision"]
        )

        self.cli(
            "exec",
            "--run-id",
            run_id,
            "--phase",
            "measure",
            "--final",
            "--",
            sys.executable,
            "-c",
            "print('sample=1.25')",
        )
        report_path = Path(self.cli("report-init", "--run-id", run_id).stdout.strip())
        self.assertNotIn("{{", report_path.read_text(encoding="utf-8"))
        rejected = self.cli("report", "--run-id", run_id, "--outcome", "complete", expect=5)
        self.assertIn("<REPLACE", rejected.stderr)
        self.complete_profile_report(report_path)
        attached = json.loads(
            self.cli("report", "--run-id", run_id, "--outcome", "complete").stdout
        )
        self.assertEqual(attached["analysis_status"], "unprocessed")
        self.cli("restore", "--run-id", run_id)
        self.assertEqual(
            self.git("symbolic-ref", "--short", "HEAD", cwd=worktree).stdout.strip(),
            "profile-lab",
        )

        assessment = json.loads(self.cli("analysis-claim", "--run", run_id).stdout)
        assessment_id = assessment["assessment_id"]
        self.cli("analysis-worktree", "--assessment-id", assessment_id)
        assessment_path = Path(
            self.cli("assessment-init", "--assessment-id", assessment_id).stdout.strip()
        )
        self.assertNotIn("{{", assessment_path.read_text(encoding="utf-8"))
        self.assertIn("## Decision now", assessment_path.read_text(encoding="utf-8"))
        self.complete_assessment(assessment_path)
        completed = json.loads(
            self.cli(
                "analysis-complete",
                "--assessment-id",
                assessment_id,
                "--outcome",
                "recommendations",
            ).stdout
        )
        self.assertEqual(completed["status"], "completed")
        self.cli("analysis-cleanup", "--assessment-id", assessment_id)

        shown = json.loads(self.cli("show", "--run-id", run_id).stdout)
        self.assertEqual(shown["analysis_status"], "analyzed")
        coordinator = Path(initialized["coordinator"]).read_text(encoding="utf-8")
        self.assertIn(run_id, coordinator)
        self.assertIn(assessment_id, coordinator)
        self.assertIn("[report]", coordinator)

    def test_second_claim_refuses_the_active_single_lab_owner(self) -> None:
        snapshot = self.refresh()
        first = self.claim(snapshot["snapshot_id"], "example:first")
        conflict = self.cli(
            "claim",
            "--snapshot-id",
            snapshot["snapshot_id"],
            "--profile-id",
            "example:second",
            "--area",
            "example",
            "--workload-id",
            "other",
            "--metric-family",
            "residency",
            "--instrument",
            "heap-census",
            "--question",
            "What is retained?",
            "--rationale",
            "Independent question.",
            "--comparison-key",
            "example/other/residency/heap-v1",
            expect=3,
        )
        self.assertIn(first["run_id"], conflict.stderr)

    def test_refresh_fast_forwards_but_refuses_a_unique_lab_commit(self) -> None:
        first = self.refresh()
        lab = Path(first["lab_worktree_path"])
        (self.repo / "tracked.txt").write_text("second\n", encoding="utf-8")
        self.git("add", "tracked.txt")
        self.git("commit", "-m", "advance master")
        second = self.refresh()
        self.assertNotEqual(first["base_revision"], second["base_revision"])
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=lab).stdout.strip(), second["base_revision"])

        (lab / "lab-only.txt").write_text("must never exist\n", encoding="utf-8")
        self.git("add", "lab-only.txt", cwd=lab)
        self.git("commit", "-m", "bad local lab commit", cwd=lab)
        refused = self.cli("refresh", "--base-ref", "master", expect=2)
        self.assertIn("non-fast-forward", refused.stderr)

    def test_analysis_release_returns_sources_to_the_queue(self) -> None:
        snapshot = self.refresh()
        run = self.claim(snapshot["snapshot_id"])
        run_id = run["run_id"]
        self.cli("prepare", "--run-id", run_id)
        self.cli(
            "exec",
            "--run-id",
            run_id,
            "--phase",
            "measure",
            "--final",
            "--",
            sys.executable,
            "-c",
            "print('ok')",
        )
        report_path = Path(self.cli("report-init", "--run-id", run_id).stdout.strip())
        self.complete_profile_report(report_path)
        self.cli("report", "--run-id", run_id, "--outcome", "complete")
        self.cli("restore", "--run-id", run_id)

        assessment = json.loads(self.cli("analysis-claim", "--run", run_id).stdout)
        self.cli(
            "analysis-release",
            "--assessment-id",
            assessment["assessment_id"],
            "--reason",
            "cancelled test",
        )
        shown = json.loads(self.cli("show", "--run-id", run_id).stdout)
        self.assertEqual(shown["analysis_status"], "unprocessed")

    def test_restore_preserves_an_untracked_profiler_artifact(self) -> None:
        snapshot = self.refresh()
        run = self.claim(snapshot["snapshot_id"])
        run_id = run["run_id"]
        prepared = json.loads(self.cli("prepare", "--run-id", run_id).stdout)
        lab = Path(prepared["worktree_path"])
        accidental = lab / "capture.eventlog"
        accidental.write_text("evidence\n", encoding="utf-8")
        self.cli("block", "--run-id", run_id, "--reason", "profiler setup failed")
        report_path = Path(self.cli("report-init", "--run-id", run_id).stdout.strip())
        self.complete_profile_report(report_path, outcome="blocked")
        self.cli("report", "--run-id", run_id, "--outcome", "blocked")
        refused = self.cli("restore", "--run-id", run_id, expect=2)
        self.assertIn("worktree is dirty", refused.stderr)
        self.assertTrue(accidental.exists())

    def test_delivery_claim_candidate_validation_and_pr_lifecycle(self) -> None:
        source_run = self.completed_run(profile_id="example:source")
        source_assessment = self.completed_assessment(source_run)
        delivery_path = self.root / "delivery-worktree"
        branch = "codex/perf-example"
        claimed = json.loads(
            self.cli(
                "delivery-claim",
                "--assessment-id",
                source_assessment["assessment_id"],
                "--finding-id",
                "PFN-001",
                "--branch",
                branch,
                "--worktree-path",
                str(delivery_path),
                "--base-ref",
                "master",
            ).stdout
        )
        delivery_id = claimed["delivery_id"]
        conflict = self.cli(
            "delivery-claim",
            "--assessment-id",
            source_assessment["assessment_id"],
            "--finding-id",
            "PFN-001",
            "--branch",
            branch,
            "--worktree-path",
            str(delivery_path),
            "--base-ref",
            "master",
            expect=3,
        )
        self.assertIn(delivery_id, conflict.stderr)

        ready = json.loads(
            self.cli("delivery-worktree", "--delivery-id", delivery_id).stdout
        )
        self.assertEqual(ready["status"], "worktree-ready")
        (delivery_path / "tracked.txt").write_text("optimized\n", encoding="utf-8")
        self.git("add", "tracked.txt", cwd=delivery_path)
        self.git("commit", "-m", "Optimize example", cwd=delivery_path)
        candidate = self.git("rev-parse", "HEAD", cwd=delivery_path).stdout.strip()
        candidate_ready = json.loads(
            self.cli(
                "delivery-update",
                "--delivery-id",
                delivery_id,
                "--status",
                "candidate-ready",
                "--candidate-revision",
                candidate,
            ).stdout
        )
        self.assertEqual(candidate_ready["candidate_revision"], candidate)

        self.cli(
            "delivery-update",
            "--delivery-id",
            delivery_id,
            "--status",
            "validating",
        )
        (delivery_path / "tracked.txt").write_text("optimized again\n", encoding="utf-8")
        self.git("add", "tracked.txt", cwd=delivery_path)
        self.git("commit", "-m", "Refresh candidate", cwd=delivery_path)
        candidate = self.git("rev-parse", "HEAD", cwd=delivery_path).stdout.strip()
        refreshed = json.loads(
            self.cli(
                "delivery-update",
                "--delivery-id",
                delivery_id,
                "--status",
                "candidate-ready",
                "--candidate-revision",
                candidate,
            ).stdout
        )
        self.assertEqual(refreshed["validation_run_ids"], [])
        self.cli(
            "delivery-update",
            "--delivery-id",
            delivery_id,
            "--status",
            "validating",
        )
        validation_run = self.completed_run(
            target_ref=candidate, profile_id="example:validation"
        )
        validating = json.loads(
            self.cli(
                "delivery-update",
                "--delivery-id",
                delivery_id,
                "--status",
                "validating",
                "--validation-run",
                validation_run,
            ).stdout
        )
        self.assertEqual(validating["validation_run_ids"], [validation_run])
        validation_assessment = self.completed_assessment(
            validation_run, outcome="no-action", base_ref=candidate
        )
        validated = json.loads(
            self.cli(
                "delivery-update",
                "--delivery-id",
                delivery_id,
                "--status",
                "validated",
                "--validation-assessment-id",
                validation_assessment["assessment_id"],
                "--validation-decision",
                "accepted",
            ).stdout
        )
        self.assertEqual(validated["validation_decision"], "accepted")
        opened = json.loads(
            self.cli(
                "delivery-update",
                "--delivery-id",
                delivery_id,
                "--status",
                "pr-open",
                "--pr-url",
                "https://example.invalid/pull/1",
            ).stdout
        )
        self.assertEqual(opened["status"], "pr-open")
        shown = json.loads(
            self.cli("delivery-show", "--delivery-id", delivery_id).stdout
        )
        self.assertEqual(shown["pr_url"], "https://example.invalid/pull/1")
        coordinator = (self.repo / ".git" / "codex-profile" / "coordinator.md").read_text(
            encoding="utf-8"
        )
        self.assertIn(delivery_id, coordinator)
        self.assertIn("[PR]", coordinator)

        terminal = self.cli(
            "delivery-claim",
            "--assessment-id",
            source_assessment["assessment_id"],
            "--finding-id",
            "PFN-001",
            "--branch",
            "codex/perf-duplicate",
            "--worktree-path",
            str(self.root / "duplicate"),
            "--base-ref",
            "master",
            expect=2,
        )
        self.assertIn("already terminal", terminal.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
