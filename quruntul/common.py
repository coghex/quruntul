"""Small shared values used by every lane."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
from datetime import datetime, timezone


class LabError(Exception):
    """A refusal the caller should see as it is: bad input, bad state, or a blocker."""


def utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def command(args: list[str], root: Path, timeout: float = 120) -> str:
    try:
        result = subprocess.run(args, cwd=root, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        raise LabError(f"metadata command exceeded {timeout:.0f} seconds: {args!r}") from error
    if result.returncode:
        raise LabError(f"{args!r}: {result.stderr.strip()}")
    return result.stdout.strip()


def git(root: Path, *args: str, timeout: float = 120) -> str:
    return command(["git", *args], root, timeout)


def atomic_json(path: Path, value) -> None:
    atomic_text(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def atomic_text(path: Path, value: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)
