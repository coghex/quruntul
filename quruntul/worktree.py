"""Reading a lab checkout's Git registration and contents without trusting `git status`.

An interrupted `git worktree add` leaves a worktree locked "initializing", with no
index and a partly written tree, on which `git status` reports every tracked file
deleted. Recognising that state and proving nothing in it is anyone's work is done
here from the registration files and the committed tree, byte for byte.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import stat
import subprocess

from quruntul.common import LabError

INITIALIZING = "initializing"
PROBLEM_LIMIT = 5


@dataclass(frozen=True)
class Registration:
    """What this repository records about a worktree path."""
    admin: Path | None  # the entry under <common>/worktrees; None when this repository has none for the path
    foreign: bool = False  # the path's own .git or the entry's common directory is not this repository's
    dangling: bool = False  # the path's .git names an entry of this repository that no longer exists
    stale: bool = False  # the entry records another location than the path
    recorded: str | None = None  # the real path of the .git the entry records
    locked: str | None = None  # the entry's lock reason, None when unlocked
    indexed: bool = False  # the entry holds the worktree's index
    head: str | None = None  # the entry's HEAD: a commit for a detached worktree, "ref: ..." when attached

    @property
    def owned(self) -> bool:
        """This repository's own record, or the trace of one, ties the path to a worktree add; a bare directory has none."""
        return self.admin is not None or self.dangling

    @property
    def complete(self) -> bool:
        """Creation finished: git removes the initializing lock last and writes the index before that."""
        return self.admin is not None and self.indexed and self.locked != INITIALIZING


def inspect(common: Path, path: Path) -> Registration:
    path = Path(path)
    worktrees = os.path.realpath(Path(common) / "worktrees")
    marker = path / ".git"
    dangling = False
    named = None
    if marker.is_symlink() or marker.is_dir():
        return Registration(None, foreign=True)
    if marker.is_file():
        try:
            text = marker.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return Registration(None, foreign=True)
        if not text.startswith("gitdir:"):
            return Registration(None, foreign=True)
        named = Path(path, text[len("gitdir:"):].strip())
        if os.path.realpath(named.parent) != worktrees:
            return Registration(None, foreign=True)
        admin = named if named.is_dir() else None
        dangling = admin is None
    else:
        admin = _admin_recording(worktrees, path)
    if admin is None:
        return Registration(None, dangling=dangling)
    # Git names the entry's repository in commondir (absent until the add has written it); a different one is foreign.
    shared = _read(admin / "commondir")
    if shared is not None and os.path.realpath(admin / shared) != os.path.realpath(common):
        return Registration(None, foreign=True)
    recorded = _recorded(admin)
    return Registration(admin, stale=recorded != os.path.realpath(marker), recorded=recorded,
                        locked=_read(admin / "locked"), indexed=(admin / "index").exists(), head=_read(admin / "HEAD"))


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


def _recorded(admin: Path) -> str | None:
    recorded = _read(admin / "gitdir")
    return os.path.realpath(recorded) if recorded else None


def _admin_recording(worktrees: str, path: Path) -> Path | None:
    """The entry for a path git never wrote a .git file into.

    Its gitdir names path/.git, or, when the add stopped before writing that, it has no gitdir
    yet and bears the name git gives the path (its basename, numbered if that was taken).
    """
    if not os.path.isdir(worktrees):
        return None
    wanted = os.path.realpath(path / ".git")
    named = re.compile(re.escape(path.name) + r"\d*")
    for admin in sorted(Path(worktrees).iterdir()):
        recorded = _recorded(admin)
        if recorded == wanted or (recorded is None and not (admin / "gitdir").exists() and named.fullmatch(admin.name)):
            return admin
    return None


class _Blobs:
    """Committed blobs by id, byte exact, through one `git cat-file --batch`."""

    def __init__(self, root: Path):
        self.process = subprocess.Popen(["git", "cat-file", "--batch"], cwd=root, stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def read(self, sha: bytes) -> bytes:
        self.process.stdin.write(sha + b"\n")
        self.process.stdin.flush()
        header = self.process.stdout.readline().split()
        if len(header) != 3 or header[1] != b"blob":
            raise LabError(f"cannot read committed blob {sha.decode(errors='replace')}")
        data = self.process.stdout.read(int(header[2]))
        self.process.stdout.read(1)
        return data

    def close(self) -> None:
        self.process.stdin.close()
        self.process.stdout.close()
        self.process.wait()


def pristine_problems(root: Path, path: Path, revision: str) -> list[str]:
    """Why the tree at path is not a prefix of the revision's committed tree; empty when it is.

    Pristine means every entry below path (its .git aside) is a committed path of the same
    type whose content is byte-identical to the commit's, or a byte-prefix of it (a file the
    interrupted checkout had begun writing), or, for a symlink, has exactly the committed
    target. Committed paths not yet written are fine. Nothing is followed or modified.
    """
    path = Path(path)
    if os.path.islink(path) or (os.path.lexists(path) and not path.is_dir()):
        return [f"{path}: not a directory"]
    if not os.path.lexists(path):
        return []
    listing = subprocess.run(["git", "ls-tree", "-r", "-t", "-l", "-z", "--full-tree", revision], cwd=root,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if listing.returncode:
        raise LabError(f"cannot list the tree of {revision}: {listing.stderr.decode(errors='replace').strip()}")
    committed = {}
    for record in listing.stdout.split(b"\0"):
        if record:
            meta, name = record.split(b"\t", 1)
            mode, kind, sha, size = meta.split()
            committed[name] = (mode, kind, sha, 0 if size == b"-" else int(size))
    problems: list[str] = []
    blobs = _Blobs(root)
    try:
        _compare(os.fsencode(path), b"", committed, blobs, problems)
    finally:
        blobs.close()
    return problems


def _compare(directory: bytes, prefix: bytes, committed: dict, blobs: _Blobs, problems: list[str]) -> None:
    with os.scandir(directory) as entries:
        children = sorted(entries, key=lambda entry: entry.name)
    for child in children:
        if len(problems) >= PROBLEM_LIMIT:
            return
        name = prefix + child.name
        if not prefix and child.name == b".git":
            continue
        try:
            problem = _entry_problem(child.path, name, committed, blobs)
            if problem is None and stat.S_ISDIR(os.lstat(child.path).st_mode) and committed[name][1] == b"tree":
                _compare(child.path, name + b"/", committed, blobs, problems)
        except OSError as error:
            problem = f"unreadable ({error.strerror or error})"
        if problem:
            problems.append(f"{os.fsdecode(name)}: {problem}")


def _entry_problem(disk: bytes, name: bytes, committed: dict, blobs: _Blobs) -> str | None:
    wanted = committed.get(name)
    if wanted is None:
        return "not in the committed tree"
    mode, kind, sha, size = wanted
    info = os.lstat(disk)
    if stat.S_ISLNK(info.st_mode):
        if mode != b"120000":
            return "a symlink where the commit has something else"
        return None if os.readlink(disk) == blobs.read(sha) else "symlink target differs from the commit"
    if stat.S_ISDIR(info.st_mode):
        if kind == b"tree":
            return None
        if kind == b"commit":
            with os.scandir(disk) as inside:
                return "a submodule directory holding files" if any(True for _ in inside) else None
        return "a directory where the commit has a file"
    if not stat.S_ISREG(info.st_mode):
        return "neither a file, a directory nor a symlink"
    if kind != b"blob" or mode == b"120000":
        return "a file where the commit has something else"
    if info.st_size > size:
        return "longer than the committed file"
    if info.st_size:
        with open(disk, "rb") as stream:
            written = stream.read()
        if not blobs.read(sha).startswith(written):
            return "content differs from the commit"
    return None
