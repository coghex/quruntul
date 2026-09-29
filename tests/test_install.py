"""The installer links skills, preserves what it replaces, and is idempotent."""
from __future__ import annotations

import io
import json
import os
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import install  # noqa: E402


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name) / "codex"
        self.bin = Path(self.temp.name) / "bin"
        (self.home / "skills" / "flake").mkdir(parents=True)
        (self.home / "skills" / "flake" / "SKILL.md").write_text("old flake")
        (self.home / "skills" / "autotest").mkdir()
        (self.home / "skills" / "guide").mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def run_install(self, *extra):
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            install.main(["--codex-home", str(self.home), "--bin-dir", str(self.bin), *extra])
        return json.loads(out.getvalue())

    def test_preview_changes_nothing(self):
        result = self.run_install()
        self.assertFalse(result["applied"])
        self.assertEqual((self.home / "skills" / "flake" / "SKILL.md").read_text(), "old flake")

    def test_apply_links_preserves_and_retires(self):
        self.run_install("--apply")
        skills = self.home / "skills"
        for name in install.SKILLS:
            self.assertTrue((skills / name).is_symlink(), name)
            self.assertTrue((skills / name / "SKILL.md").exists(), name)
        backups = list(self.home.glob("skills-superseded-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / "flake" / "SKILL.md").read_text(), "old flake")
        self.assertTrue((backups[0] / "autotest").is_dir())
        self.assertFalse((skills / "autotest").exists())
        self.assertTrue((skills / "guide").is_dir())  # unrelated skills untouched
        self.assertTrue((self.bin / "quruntul").is_symlink())
        again = self.run_install("--apply")
        self.assertTrue(all(a["action"] == "keep" for a in again["actions"]))

    def test_refuses_to_replace_another_command(self):
        self.bin.mkdir()
        (self.bin / "quruntul").write_text("#!/bin/sh\n")
        with self.assertRaises(SystemExit):
            self.run_install("--apply")
        self.assertEqual((self.home / "skills" / "flake" / "SKILL.md").read_text(), "old flake")


if __name__ == "__main__":
    unittest.main()
