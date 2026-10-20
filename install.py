#!/usr/bin/env python3
"""Install quruntul's skills into Codex and Claude Code, and its command onto PATH.

Each skill directory is symlinked into both clients' skills directories, so
pulling this repository updates every skill at once. A skill directory it
replaces, and every retired skill, is moved (never deleted) into a dated
`skills-superseded-YYYY-MM-DD/` beside it. Preview is the default; pass --apply.
"""
from __future__ import annotations

import argparse
from datetime import date
import json
import os
from pathlib import Path
import shutil
import sys

REPO = Path(__file__).resolve().parent
SKILLS = ("flake", "deflake", "test", "playtest", "assess-tests", "profile", "performance")
RETIRED = ("autotest",)  # replaced by `$test N`


def plan(codex_home: Path, bin_dir: Path, claude_home: Path | None = None) -> list[dict]:
    actions = []
    seen = set()
    for home in (codex_home, claude_home):
        # One skills directory can serve both clients (a symlinked
        # ~/.claude/skills); plan it once, or the second pass would link over
        # the first pass's links.
        if home is None or (home / "skills").resolve() in seen:
            continue
        seen.add((home / "skills").resolve())
        actions += _skill_actions(home)
    command = bin_dir / "quruntul"
    source = REPO / "bin" / "quruntul"
    if command.is_symlink() and command.resolve() == source.resolve():
        actions.append(dict(action="keep", path=str(command)))
    elif command.exists() or command.is_symlink():
        actions.append(dict(action="refuse", path=str(command),
                            reason="a different quruntul is already installed there; remove it yourself first"))
    else:
        actions.append(dict(action="link", path=str(command), to=str(source)))
    return actions


def _skill_actions(home: Path) -> list[dict]:
    skills = home / "skills"
    backup = home / f"skills-superseded-{date.today().isoformat()}"
    actions = []
    for name in SKILLS:
        source = REPO / "skills" / name
        target = skills / name
        if target.is_symlink() and target.resolve() == source.resolve():
            actions.append(dict(action="keep", path=str(target)))
            continue
        if target.exists() or target.is_symlink():
            actions.append(dict(action="supersede", path=str(target), to=str(_free(backup / name))))
        actions.append(dict(action="link", path=str(target), to=str(source)))
    for name in RETIRED:
        target = skills / name
        if target.exists() or target.is_symlink():
            actions.append(dict(action="retire", path=str(target), to=str(_free(backup / name))))
    return actions


def _free(path: Path) -> Path:
    candidate, number = path, 1
    while candidate.exists() or candidate.is_symlink():
        number += 1
        candidate = path.with_name(f"{path.name}.{number}")
    return candidate


def apply(actions: list[dict]) -> None:
    if any(a["action"] == "refuse" for a in actions):
        raise SystemExit("refusing: " + "; ".join(a["reason"] for a in actions if a["action"] == "refuse"))
    for a in actions:
        path = Path(a["path"])
        if a["action"] in ("supersede", "retire"):
            Path(a["to"]).parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), a["to"])
        elif a["action"] == "link":
            path.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(a["to"], path)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="make the changes (default: preview)")
    parser.add_argument("--codex-home", default=os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    parser.add_argument("--claude-home", default=os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude")))
    parser.add_argument("--no-claude", action="store_true", help="link the skills into Codex only")
    parser.add_argument("--bin-dir", default=str(Path.home() / ".local" / "bin"))
    args = parser.parse_args(argv)
    claude_home = None if args.no_claude else Path(args.claude_home)
    actions = plan(Path(args.codex_home), Path(args.bin_dir), claude_home)
    if args.apply:
        apply(actions)
    print(json.dumps(dict(applied=args.apply, actions=actions), indent=2))
    on_path = any(Path(p).resolve() == Path(args.bin_dir).resolve() for p in os.environ.get("PATH", "").split(os.pathsep) if p)
    if not on_path:
        print(f"note: {args.bin_dir} is not on PATH; add it so skills can run `quruntul`", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
