#!/usr/bin/env python3
"""Run the review gate's stale-approval step against real Git histories.

The step body is read from .github/workflows/review-gate.yml and executed
exactly as written, in a temporary repository, with a stub `gh` that records
label changes. Each case checks whether `reviewed:approve` survives a push.
"""

import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "review-gate.yml"
STEP_NAME = "- name: Remove approval unless this push touches no PR-owned file"

# Answers `gh pr edit --remove-label` and `gh pr view --json labels`, which
# the step uses, and `gh pr diff --name-only` the way GitHub does, with
# literal (unescaped) file names, in case a version of the step relies on it.
# Any other call fails the test.
FAKE_GH = """\
#!/usr/bin/env bash
set -euo pipefail
case "$1 $2" in
  "pr edit") : > "$GATE_LABELS" ;;
  "pr view") cat "$GATE_LABELS" ;;
  "pr diff") git -C "$GATE_REPO" -c core.quotePath=false diff --no-renames --name-only "origin/master...$GATE_AFTER" ;;
  *) echo "unexpected gh call: $*" >&2; exit 2 ;;
esac
"""


def step_body() -> str:
    """Return the shell body of the stale-approval step's `run: |` block."""
    lines = WORKFLOW.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == STEP_NAME)
    run = next(i for i in range(start, len(lines)) if lines[i].strip() == "run: |")
    indent = len(lines[run]) - len(lines[run].lstrip()) + 2
    body = []
    for line in lines[run + 1 :]:
        if line.strip() and len(line) - len(line.lstrip()) < indent:
            break
        body.append(line[indent:])
    return "\n".join(body) + "\n"


class Repo:
    def __init__(self, path: Path):
        self.path = path
        self.git("init", "-q", "-b", "master")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "test")

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.path), *args],
            check=True, capture_output=True, text=True,
        ).stdout.strip()

    def commit(self, files: dict, message: str) -> str:
        for name, content in files.items():
            target = self.path / name
            if content is None:
                target.unlink()
            else:
                target.write_text(content, encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")


class StaleApprovalTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        (self.tmp / "repo").mkdir()
        self.repo = Repo(self.tmp / "repo")
        self.base = self.repo.commit(
            {"impl.txt": "base impl\n", "tests.txt": "base tests\n",
             "other.txt": "base other\n"},
            "base",
        )
        bindir = self.tmp / "bin"
        bindir.mkdir()
        gh = bindir / "gh"
        gh.write_text(FAKE_GH, encoding="utf-8")
        gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
        self.bindir = bindir
        self.labels = self.tmp / "labels"
        self.script = self.tmp / "step.sh"
        self.script.write_text(step_body(), encoding="utf-8")

    def branch_from_base(self) -> None:
        self.repo.git("checkout", "-q", "-b", "pr", self.base)

    def run_gate(self, before: str, after: str) -> bool:
        """Run the step for a push from `before` to `after`; True if approval survives."""
        self.repo.git("update-ref", "refs/remotes/origin/master", "master")
        self.labels.write_text("reviewed:approve\n", encoding="utf-8")
        env = dict(
            os.environ,
            PATH=f"{self.bindir}{os.pathsep}{os.environ['PATH']}",
            GH_TOKEN="unused", BEFORE=before, AFTER=after, PR_NUMBER="1",
            REPO="owner/name", BASE_REF="master",
            GATE_LABELS=str(self.labels), GATE_REPO=str(self.repo.path),
            GATE_AFTER=after,
        )
        result = subprocess.run(
            ["bash", "-e", str(self.script)], cwd=self.repo.path, env=env,
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return "reviewed:approve" in self.labels.read_text(encoding="utf-8")

    def test_editing_a_reviewed_file_removes_approval(self):
        self.branch_from_base()
        before = self.repo.commit({"impl.txt": "pr impl\n"}, "pr")
        after = self.repo.commit({"impl.txt": "pr impl, edited\n"}, "edit")
        self.assertFalse(self.run_gate(before, after))

    def test_merging_an_unrelated_base_change_keeps_approval(self):
        self.branch_from_base()
        before = self.repo.commit({"impl.txt": "pr impl\n"}, "pr")
        self.repo.git("checkout", "-q", "master")
        self.repo.commit({"other.txt": "master moved\n"}, "master moves")
        self.repo.git("checkout", "-q", "pr")
        self.repo.git("merge", "-q", "--no-edit", "master")
        after = self.repo.git("rev-parse", "HEAD")
        self.assertTrue(self.run_gate(before, after))

    def test_reverting_one_reviewed_file_removes_approval(self):
        # The reverted file leaves the PR's diff, so only the pre-push file
        # set shows the push touched reviewed content.
        self.branch_from_base()
        before = self.repo.commit(
            {"impl.txt": "pr impl\n", "tests.txt": "pr tests\n"}, "pr")
        after = self.repo.commit({"impl.txt": "base impl\n"}, "revert impl")
        self.assertFalse(self.run_gate(before, after))

    def test_deleting_a_file_the_pr_added_removes_approval(self):
        self.branch_from_base()
        before = self.repo.commit(
            {"new.txt": "added\n", "tests.txt": "pr tests\n"}, "pr")
        after = self.repo.commit({"new.txt": None}, "drop new file")
        self.assertFalse(self.run_gate(before, after))

    def test_adding_a_non_ascii_file_removes_approval(self):
        # Git escapes non-ASCII paths in its name lists; the step must compare
        # names from one source so an escaped name still matches itself.
        self.branch_from_base()
        before = self.repo.commit({"impl.txt": "pr impl\n"}, "pr")
        after = self.repo.commit({"caf\u00e9.txt": "new\n"}, "add caf\u00e9")
        self.assertFalse(self.run_gate(before, after))

    def test_editing_a_reviewed_non_ascii_file_removes_approval(self):
        self.branch_from_base()
        before = self.repo.commit({"caf\u00e9.txt": "pr\n"}, "pr")
        after = self.repo.commit({"caf\u00e9.txt": "pr, edited\n"}, "edit")
        self.assertFalse(self.run_gate(before, after))

    def test_missing_before_removes_approval(self):
        self.branch_from_base()
        after = self.repo.commit({"impl.txt": "pr impl\n"}, "pr")
        self.assertFalse(self.run_gate("0" * 40, after))


if __name__ == "__main__":
    sys.exit(unittest.main(verbosity=2))
