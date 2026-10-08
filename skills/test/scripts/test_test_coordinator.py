#!/usr/bin/env python3
"""Focused tests for the $test coordinator's value and proposal paths."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).with_name("test_coordinator.py")
SPEC = importlib.util.spec_from_file_location("test_coordinator", SCRIPT)
assert SPEC and SPEC.loader
COORDINATOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(COORDINATOR)


def completed(index: int, outcome: str, area: str = "gameplay/core") -> dict[str, str]:
    return {
        "run_id": f"run-{index}",
        "test_id": f"probe:test-{index}",
        "area": area,
        "selection_rationale": f"coverage for {area}",
        "status": "completed",
        "interpretation_outcome": outcome,
        "revision": f"revision-{index}",
        "claimed_at": f"2026-08-12T00:{index:02d}:00Z",
        "completed_at": f"2026-08-12T00:{index:02d}:30Z",
    }


def report_record() -> dict[str, str]:
    return {
        "run_id": "run-report",
        "snapshot_id": "snapshot-report",
        "base_ref": "origin/master",
        "base_refreshed_at": "2026-08-26T00:00:00Z",
        "revision": "abc123",
        "revision_subject": "Test report validation",
        "revision_committed_at": "2026-08-26T00:00:00Z",
        "ci_coverage": "ad-hoc",
        "ci_exclusion_evidence": "not registered in CI",
    }


def report_text(record: dict[str, str], outcome: str, observation: bool) -> str:
    source = "\n".join(
        [
            "---",
            f'schema: "{COORDINATOR.REPORT_SCHEMA}"',
            f'run_id: {json.dumps(record["run_id"])}',
            f'snapshot_id: {json.dumps(record["snapshot_id"])}',
            f'base_ref: {json.dumps(record["base_ref"])}',
            f'base_refreshed_at: {json.dumps(record["base_refreshed_at"])}',
            f'revision: {json.dumps(record["revision"])}',
            f'revision_subject: {json.dumps(record["revision_subject"])}',
            f'revision_committed_at: {json.dumps(record["revision_committed_at"])}',
            f'ci_coverage: {json.dumps(record["ci_coverage"])}',
            f'ci_exclusion_evidence: {json.dumps(record["ci_exclusion_evidence"])}',
            f'interpretation_status: "{outcome}"',
            "---",
            "",
            "## Result summary",
            "",
            "summary",
            "",
            "## Source version",
            "",
            "source",
            "",
            "## Selection rationale",
            "",
            "selection",
            "",
            "## Execution",
            "",
            "execution",
            "",
            "## Observations",
            "",
        ]
    )
    if observation:
        source += "\n".join(
            [
                "### OBS-001 — Setup prevented meaningful execution",
                "",
                "- **Category:** harness-gap",
                "- **Severity:** blocker",
                "- **Confidence:** high",
                "- **Disposition:** harness-limitation",
                "- **Expected:** primary behavior executes",
                "- **Observed:** setup stopped first",
                "- **Evidence:** retained log",
                "- **Interpretation:** harness defect",
                "- **Recommended follow-up:** repair the harness",
                "",
            ]
        )
    source += "\n".join(
        [
            "## Coverage and limitations",
            "",
            "coverage",
            "",
            "## Artifacts",
            "",
            "artifacts",
            "",
        ]
    )
    return source


class ValueStatusTests(unittest.TestCase):
    def summarize(self, records: list[dict[str, str]], area: str | None = None) -> dict:
        return COORDINATOR.summarize_value(
            records,
            area=area,
            window=8,
            min_completed=6,
            max_observation_rate=0.25,
        )

    def test_clean_streak_is_diminishing_returns(self) -> None:
        records = [completed(index, "clean") for index in range(8)]
        summary = self.summarize(records)
        self.assertEqual(summary["signal"], "diminishing-returns")
        self.assertEqual(summary["recent_observation_rate"], 0.0)
        self.assertTrue(summary["heuristic_only"])

    def test_observation_yield_remains_productive(self) -> None:
        outcomes = ["observations", "observations", "observations", *(["clean"] * 5)]
        records = [completed(index, outcome) for index, outcome in enumerate(outcomes)]
        summary = self.summarize(records)
        self.assertEqual(summary["signal"], "productive")
        self.assertEqual(summary["recent_observation_rate"], 0.375)

    def test_area_scope_and_minimum_history_are_conservative(self) -> None:
        records = [completed(index, "clean", "combat/animation") for index in range(5)]
        records.extend(completed(index + 5, "clean", "persistence/load") for index in range(5))
        summary = self.summarize(records, area="combat")
        self.assertEqual(summary["matching_completed_runs"], 5)
        self.assertEqual(summary["signal"], "insufficient-history")

    def test_blocked_streak_identifies_apparatus_friction(self) -> None:
        records = [completed(index, "blocked") for index in range(8)]
        summary = self.summarize(records)
        self.assertEqual(summary["signal"], "apparatus-friction")


class ReportValidationTests(unittest.TestCase):
    def validate(self, outcome: str, observation: bool) -> list[str]:
        record = report_record()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "result.md"
            path.write_text(report_text(record, outcome, observation), encoding="utf-8")
            return COORDINATOR.validate_report(path, record, outcome)

    def test_blocked_report_requires_an_observation(self) -> None:
        errors = self.validate("blocked", observation=False)
        self.assertIn(
            "outcome 'blocked' requires at least one OBS-nnn section describing the blocker",
            errors,
        )

    def test_blocked_report_with_structured_observation_is_valid(self) -> None:
        self.assertEqual(self.validate("blocked", observation=True), [])

    def test_clean_report_without_observations_remains_valid(self) -> None:
        self.assertEqual(self.validate("clean", observation=False), [])


class ProposalCoordinationTests(unittest.TestCase):
    def run_cli(
        self, repo: Path, *args: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        self.assertTrue(args)
        return subprocess.run(
            ["python3", str(SCRIPT), args[0], "--repo", str(repo), *args[1:]],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=check,
        )

    def git(self, repo: Path, *args: str) -> None:
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)

    def test_proposal_is_atomic_and_blocks_duplicate_test_claims(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary) / "repo"
            repo.mkdir()
            self.git(repo, "init", "-b", "master")
            self.git(repo, "config", "user.name", "Codex Test")
            self.git(repo, "config", "user.email", "codex-test@example.invalid")
            (repo / "README.md").write_text("fixture\n", encoding="utf-8")
            self.git(repo, "add", "README.md")
            self.git(repo, "commit", "-m", "fixture")

            first_snapshot = json.loads(
                self.run_cli(repo, "refresh", "--base-ref", "HEAD").stdout
            )
            created = self.run_cli(
                repo,
                "proposal-create",
                "--snapshot-id",
                first_snapshot["snapshot_id"],
                "--test-id",
                "probe:new-player-recovery",
                "--area",
                "onboarding/recovery",
                "--title",
                "New-player recovery path",
                "--gap",
                "No existing test covers recovery from a failed first action.",
                "--rationale",
                "The onboarding path is player-critical.",
                "--scenario",
                "Fail the first action, recover, and finish onboarding.",
                "--oracle",
                "Progress resumes exactly once without lost state.",
                "--apparatus",
                "Add one focused offscreen probe.",
                "--recommended-tier",
                "manual-only",
                "--cost",
                "One engine boot; modest UI timing risk.",
            )
            proposal = json.loads(created.stdout)
            self.assertEqual(proposal["status"], "proposed")

            second_snapshot = json.loads(
                self.run_cli(repo, "refresh", "--base-ref", "HEAD").stdout
            )
            duplicate = self.run_cli(
                repo,
                "proposal-create",
                "--snapshot-id",
                second_snapshot["snapshot_id"],
                "--test-id",
                "probe:new-player-recovery",
                "--area",
                "onboarding/recovery",
                "--title",
                "Duplicate",
                "--gap",
                "Duplicate gap.",
                "--rationale",
                "Duplicate rationale.",
                "--scenario",
                "Duplicate scenario.",
                "--oracle",
                "Duplicate oracle.",
                "--apparatus",
                "Duplicate apparatus.",
                "--recommended-tier",
                "manual-only",
                "--cost",
                "Duplicate cost.",
                check=False,
            )
            self.assertEqual(duplicate.returncode, 3)

            claim = self.run_cli(
                repo,
                "claim",
                "--snapshot-id",
                second_snapshot["snapshot_id"],
                "--test-id",
                "probe:new-player-recovery",
                "--area",
                "onboarding/recovery",
                "--ci-status",
                "manual-only",
                "--ci-evidence",
                "fixture classification",
                "--",
                "true",
                check=False,
            )
            self.assertEqual(claim.returncode, 3)

            accepted = self.run_cli(
                repo,
                "proposal-close",
                "--proposal-id",
                proposal["proposal_id"],
                "--status",
                "accepted",
            )
            self.assertEqual(json.loads(accepted.stdout)["status"], "accepted")
            implemented = self.run_cli(
                repo,
                "proposal-close",
                "--proposal-id",
                proposal["proposal_id"],
                "--status",
                "implemented",
            )
            self.assertEqual(json.loads(implemented.stdout)["status"], "implemented")


if __name__ == "__main__":
    unittest.main()
