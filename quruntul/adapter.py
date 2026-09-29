"""The adapter interface a consuming repository implements in .quruntul/adapter.py.

The engine imports the adapter from the pinned checkout it measures, so the
description of a repository's tests is versioned with those tests. An adapter
describes suites and knows how to build one; the engine owns selection,
repetition, process lifetime, per-test results and the ledger.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import importlib.util
import platform as _platform
from pathlib import Path
import re
import sys
from typing import Callable

from quruntul.common import LabError

ADAPTER_PATH = Path(".quruntul") / "adapter.py"
SUITE_ID = re.compile(r"[a-z][a-z0-9.:-]{0,95}")


@dataclass
class Suite:
    """One independently buildable and runnable set of tests."""

    id: str
    kind: str  # "ci": CI runs it; "probe": local-only, never CI
    framework: str  # "hspec", "command" (declared checks) or "exit" (one test: the exit status)
    description: str
    area: str = ""
    platforms: list[str] = field(default_factory=lambda: ["Darwin", "Linux"])
    desktop: bool = False  # opens windows on the machine's own display
    trial_seconds: int = 900
    batch_seconds: int = 7200
    identity: str = ""  # digest of every input that can change the suite's behaviour
    rts: list[str] = field(default_factory=list)
    checks: list[str] = field(default_factory=list)  # command probes: declared stable check ids
    priority: int = 10
    # At most this many unmeasured tests per flake batch (0: no limit). A large
    # suite is then measured in slices, in suite order, one batch at a time.
    batch_tests: int = 0
    data: dict = field(default_factory=dict)  # adapter-private details, echoed back to prepare()

    def validate(self) -> "Suite":
        if not SUITE_ID.fullmatch(self.id):
            raise LabError(f"suite id must be a stable lowercase identifier: {self.id!r}")
        if self.kind not in ("ci", "probe"):
            raise LabError(f"{self.id}: kind must be ci or probe")
        if self.framework not in ("hspec", "command", "exit"):
            raise LabError(f"{self.id}: framework must be hspec, command or exit")
        if not self.description.strip():
            raise LabError(f"{self.id}: a suite needs a description")
        if not self.platforms or not all(p in ("Darwin", "Linux") for p in self.platforms):
            raise LabError(f"{self.id}: platforms must name Darwin and/or Linux")
        if not 1 <= self.trial_seconds <= self.batch_seconds <= 86400:
            raise LabError(f"{self.id}: need 1 <= trial_seconds <= batch_seconds <= 86400")
        if self.framework == "command" and (not self.checks or len(set(self.checks)) != len(self.checks)):
            raise LabError(f"{self.id}: a command probe declares distinct stable check ids")
        if self.framework == "exit":
            self.checks = ["run"]
        if not self.identity:
            raise LabError(f"{self.id}: a suite needs a source identity")
        if self.batch_tests < 0:
            raise LabError(f"{self.id}: batch_tests must be zero (no limit) or positive")
        return self

    def record(self) -> dict:
        return asdict(self)


@dataclass
class Prepared:
    """What prepare() hands back: how to start one trial of a built suite."""

    argv: list[str]  # the executable (hspec) or full command (command probes)
    cwd: str
    environment: dict[str, str]
    provenance: dict = field(default_factory=dict)
    wrapper: list[str] = field(default_factory=list)  # prefixed to every trial, e.g. an isolated display
    # False when the wrapper starts the executable itself (it then receives only
    # the test options); argv[0] is still recorded as the executable.
    launches_executable: bool = True


class Context:
    """What an adapter may use. `run` goes through the guardian, so a build's
    process group is owned, bounded and recorded like any trial.

    The context also carries `Suite`, `Prepared` and `digest`, so an adapter
    needs no import from quruntul and its repository can test it with a stub
    context of its own."""

    Suite = Suite
    Prepared = Prepared

    def __init__(self, checkout: Path, revision: str, artifacts: Path | None,
                 runner: Callable[..., dict] | None, log: Callable[[str], None] = print):
        from quruntul.common import digest
        self.digest = digest
        self.checkout = Path(checkout)
        self.revision = revision
        self.artifacts = artifacts
        self._runner = runner
        self.platform = _platform.system()
        self.log = log

    def run(self, argv: list[str], name: str, timeout: float, cwd: Path | None = None,
            environment: dict | None = None) -> dict:
        if self._runner is None:
            raise LabError("this context cannot run processes (selection only)")
        return self._runner(argv, name, timeout, cwd or self.checkout, environment)


def load(checkout: Path):
    """Import the adapter committed at a checkout."""
    path = Path(checkout) / ADAPTER_PATH
    if not path.is_file():
        raise LabError(f"no quruntul adapter at {path}; this repository is not set up for quruntul")
    name = f"quruntul_adapter_{abs(hash(str(path.resolve())))}"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as error:  # an adapter bug is a blocker, reported as such
        raise LabError(f"adapter at {path} failed to import: {type(error).__name__}: {error}") from error
    if not hasattr(module, "adapter"):
        raise LabError(f"{path} defines no adapter()")
    adapter = module.adapter()
    for required in ("name", "suites", "prepare"):
        if not hasattr(adapter, required):
            raise LabError(f"adapter lacks {required}")
    return adapter


def suites(adapter, ctx: Context) -> list[Suite]:
    try:
        declared = adapter.suites(ctx)
    except LabError:
        raise
    except Exception as error:
        raise LabError(f"adapter could not list suites: {type(error).__name__}: {error}") from error
    found = [s.validate() for s in declared]
    ids = [s.id for s in found]
    if len(ids) != len(set(ids)):
        raise LabError("adapter declared a suite id twice")
    return found


def option(adapter, name: str, default):
    value = getattr(adapter, name, default)
    return value() if callable(value) else value
