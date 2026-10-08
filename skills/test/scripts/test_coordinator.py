#!/usr/bin/env python3
"""Atomic coordination for the $test skill.

State, logs, and reports live under the repository's common Git directory so
every linked worktree sees one registry. One detached base worktree is
fast-forwarded under a lock; each test gets a sibling detached worktree at its
own immutable base snapshot, so the primary checkout remains untouched.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Callable


SCHEMA = "codex-test-coordinator/v1"
REPORT_SCHEMA = "codex-test-result/v1"
ACTIVE_STATUSES = {"claimed", "creating-worktree", "worktree-ready", "running", "awaiting-report"}
ACTIVE_PROPOSAL_STATUSES = {"proposed", "accepted"}
DEFAULT_STALE_SECONDS = 6 * 60 * 60
DEFAULT_VALUE_WINDOW = 8
DEFAULT_VALUE_MIN_COMPLETED = 6
DEFAULT_VALUE_MAX_OBSERVATION_RATE = 0.25


class CoordinatorError(RuntimeError):
    pass


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def run_git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=check,
    )


def repo_root(repo: str | Path) -> Path:
    supplied = Path(repo).expanduser().resolve()
    result = run_git(supplied, "rev-parse", "--show-toplevel")
    return Path(result.stdout.strip()).resolve()


def common_git_dir(repo: Path) -> Path:
    result = run_git(repo, "rev-parse", "--git-common-dir")
    value = Path(result.stdout.strip())
    if not value.is_absolute():
        value = repo / value
    return value.resolve()


def coordinator_paths(repo: Path) -> dict[str, Path]:
    common = common_git_dir(repo)
    state_root = common / "codex-test"
    fingerprint = hashlib.sha256(str(common).encode()).hexdigest()[:8]
    canonical_checkout = common.parent if common.name == ".git" else repo
    worktrees = canonical_checkout.parent / (
        f".codex-test-worktrees-{canonical_checkout.name}-{fingerprint}"
    )
    return {
        "root": state_root,
        "registry": state_root / "registry.json",
        "lock": state_root / "registry.lock",
        "base_lock": state_root / "base.lock",
        "logs": state_root / "logs",
        "reports": state_root / "reports",
        "worktrees": worktrees,
        "base_worktree": worktrees / "_base",
    }


def ensure_dirs(paths: dict[str, Path]) -> None:
    for key in ("root", "logs", "reports", "worktrees"):
        paths[key].mkdir(parents=True, exist_ok=True)


def empty_registry() -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "updated_at": now_iso(),
        "snapshots": [],
        "runs": [],
        "proposals": [],
    }


@contextlib.contextmanager
def locked_registry(repo: Path):
    paths = coordinator_paths(repo)
    ensure_dirs(paths)
    with paths["lock"].open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if paths["registry"].exists():
            try:
                registry = json.loads(paths["registry"].read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise CoordinatorError(f"cannot read registry {paths['registry']}: {exc}") from exc
            if registry.get("schema") != SCHEMA or not isinstance(registry.get("runs"), list):
                raise CoordinatorError(f"unsupported registry schema in {paths['registry']}")
            registry.setdefault("snapshots", [])
            if not isinstance(registry["snapshots"], list):
                raise CoordinatorError(f"unsupported snapshot registry in {paths['registry']}")
            registry.setdefault("proposals", [])
            if not isinstance(registry["proposals"], list):
                raise CoordinatorError(f"unsupported proposal registry in {paths['registry']}")
        else:
            registry = empty_registry()
        yield registry, paths
        registry["updated_at"] = now_iso()
        fd, temporary = tempfile.mkstemp(prefix="registry.", suffix=".json", dir=paths["root"])
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                json.dump(registry, output, indent=2, sort_keys=True)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, paths["registry"])
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def locked_base(repo: Path):
    paths = coordinator_paths(repo)
    ensure_dirs(paths)
    with paths["base_lock"].open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        yield paths
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def read_registry(repo: Path) -> tuple[dict[str, Any], dict[str, Path]]:
    with locked_registry(repo) as (registry, paths):
        return json.loads(json.dumps(registry)), paths


def find_run(registry: dict[str, Any], run_id: str) -> dict[str, Any]:
    for record in registry["runs"]:
        if record.get("run_id") == run_id:
            return record
    raise CoordinatorError(f"unknown run id {run_id!r}")


def find_snapshot(registry: dict[str, Any], snapshot_id: str) -> dict[str, Any]:
    for snapshot in registry["snapshots"]:
        if snapshot.get("snapshot_id") == snapshot_id:
            return snapshot
    raise CoordinatorError(f"unknown base snapshot {snapshot_id!r}; run refresh first")


def find_proposal(registry: dict[str, Any], proposal_id: str) -> dict[str, Any]:
    for proposal in registry["proposals"]:
        if proposal.get("proposal_id") == proposal_id:
            return proposal
    raise CoordinatorError(f"unknown proposal id {proposal_id!r}")


def slug(text: str) -> str:
    clean = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (clean or "test")[:40]


def reap_stale(registry: dict[str, Any], stale_seconds: int) -> None:
    now = dt.datetime.now(dt.timezone.utc)
    for record in registry["runs"]:
        if record.get("status") not in ACTIVE_STATUSES:
            continue
        stamp = parse_iso(record.get("heartbeat_at") or record.get("claimed_at"))
        if stamp and (now - stamp).total_seconds() > stale_seconds:
            prior = record["status"]
            record.update({
                "status": "abandoned",
                "completed_at": now_iso(),
                "abandon_reason": f"stale {prior} claim exceeded {stale_seconds}s",
            })


def record_update(repo: Path, run_id: str, update: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    with locked_registry(repo) as (registry, _paths):
        record = find_run(registry, run_id)
        update(record)
        record["heartbeat_at"] = now_iso()
        return json.loads(json.dumps(record))


def command_after_separator(values: list[str]) -> list[str]:
    if values and values[0] == "--":
        values = values[1:]
    if not values:
        raise CoordinatorError("claim requires a command after --")
    return values


def default_base_ref(repo: Path) -> str:
    upstream = run_git(
        repo,
        "rev-parse",
        "--abbrev-ref",
        "--symbolic-full-name",
        "@{upstream}",
        check=False,
    )
    if upstream.returncode == 0 and upstream.stdout.strip():
        return upstream.stdout.strip()
    remote_head = run_git(
        repo,
        "symbolic-ref",
        "--quiet",
        "--short",
        "refs/remotes/origin/HEAD",
        check=False,
    )
    if remote_head.returncode == 0 and remote_head.stdout.strip():
        return remote_head.stdout.strip()
    for candidate in ("origin/master", "origin/main"):
        exists = run_git(repo, "rev-parse", "--verify", f"{candidate}^{{commit}}", check=False)
        if exists.returncode == 0:
            return candidate
    return "HEAD"


def remote_for_ref(repo: Path, base_ref: str) -> str | None:
    remotes = run_git(repo, "remote").stdout.splitlines()
    normalized = base_ref.removeprefix("refs/remotes/")
    for remote in remotes:
        if normalized == remote or normalized.startswith(f"{remote}/"):
            return remote
    return None


def advance_base_worktree(repo: Path, paths: dict[str, Path], revision: str) -> str | None:
    target = paths["base_worktree"]
    previous_revision: str | None = None
    if target.exists():
        if not target.is_dir():
            raise CoordinatorError(f"test base path is not a directory: {target}")
        try:
            target_root = repo_root(target)
            target_common = common_git_dir(target_root)
        except subprocess.CalledProcessError as exc:
            raise CoordinatorError(f"test base path is not a valid worktree: {target}") from exc
        if target_root != target.resolve() or target_common != common_git_dir(repo):
            raise CoordinatorError(f"test base path belongs to another repository: {target}")
        dirty = run_git(target, "status", "--porcelain", "--untracked-files=all").stdout.strip()
        if dirty:
            raise CoordinatorError(
                f"test base worktree is dirty; preserve it and investigate before refresh: {target}"
            )
        previous_revision = run_git(target, "rev-parse", "HEAD").stdout.strip()
        if previous_revision != revision:
            ancestry = run_git(
                repo,
                "merge-base",
                "--is-ancestor",
                previous_revision,
                revision,
                check=False,
            )
            if ancestry.returncode != 0:
                raise CoordinatorError(
                    "refusing to move the test base non-fast-forward: "
                    f"{previous_revision} -> {revision}"
                )
            moved = run_git(target, "checkout", "--detach", "--quiet", revision, check=False)
            if moved.returncode != 0:
                raise CoordinatorError(moved.stderr.strip() or moved.stdout.strip())
    else:
        created = run_git(
            repo,
            "worktree",
            "add",
            "--detach",
            str(target),
            revision,
            check=False,
        )
        if created.returncode != 0:
            raise CoordinatorError(created.stderr.strip() or created.stdout.strip())
    return previous_revision


def cmd_refresh(args: argparse.Namespace) -> int:
    repo = repo_root(args.repo)
    with locked_base(repo) as paths:
        base_ref = args.base_ref or default_base_ref(repo)
        remote = remote_for_ref(repo, base_ref)
        if remote:
            fetched = run_git(repo, "fetch", "--prune", remote, check=False)
            if fetched.returncode != 0:
                raise CoordinatorError(
                    f"failed to fetch {remote}: {fetched.stderr.strip() or fetched.stdout.strip()}"
                )
        resolved = run_git(repo, "rev-parse", "--verify", f"{base_ref}^{{commit}}", check=False)
        if resolved.returncode != 0:
            raise CoordinatorError(
                f"cannot resolve test base ref {base_ref!r}: "
                f"{resolved.stderr.strip() or resolved.stdout.strip()}"
            )
        revision = resolved.stdout.strip()
        previous_revision = advance_base_worktree(repo, paths, revision)
        refreshed_at = now_iso()
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        snapshot = {
            "snapshot_id": f"{stamp}-{slug(base_ref)}-{secrets.token_hex(3)}",
            "base_ref": base_ref,
            "revision": revision,
            "revision_subject": run_git(repo, "show", "-s", "--format=%s", revision).stdout.strip(),
            "revision_committed_at": run_git(
                repo, "show", "-s", "--format=%cI", revision
            ).stdout.strip(),
            "refreshed_at": refreshed_at,
            "fetch_remote": remote,
            "previous_revision": previous_revision,
            "base_worktree_path": str(paths["base_worktree"]),
        }
        with locked_registry(repo) as (registry, _registry_paths):
            registry["snapshots"].append(snapshot)
    print(json.dumps(snapshot, indent=2, sort_keys=True))
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    repo = repo_root(args.repo)
    with locked_registry(repo) as (_registry, paths):
        payload = {key: str(value) for key, value in paths.items()}
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    repo = repo_root(args.repo)
    registry, paths = read_registry(repo)
    records = registry["runs"]
    if args.active:
        records = [record for record in records if record.get("status") in ACTIVE_STATUSES]
    records = sorted(records, key=lambda record: record.get("claimed_at", ""), reverse=True)
    if args.limit:
        records = records[: args.limit]
    if args.json:
        print(json.dumps({"paths": {k: str(v) for k, v in paths.items()}, "runs": records}, indent=2))
        return 0
    if not records:
        print("No matching coordinated test runs.")
        return 0
    print(f"{'RUN ID':55} {'STATUS':18} {'AREA':18} TEST")
    for record in records:
        print(
            f"{record.get('run_id', ''):55} "
            f"{record.get('status', ''):18} "
            f"{record.get('area', '')[:18]:18} "
            f"{record.get('test_id', '')}"
        )
    print(f"\nRegistry: {paths['registry']}")
    return 0


def normalized_words(value: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", value.casefold()))


def run_matches_area(record: dict[str, Any], area: str | None) -> bool:
    if not area:
        return True
    requested = normalized_words(area)
    if not requested:
        return True
    searchable = " ".join(
        str(record.get(key, ""))
        for key in ("area", "test_id", "selection_rationale")
    )
    return requested <= normalized_words(searchable)


def summarize_value(
    records: list[dict[str, Any]],
    *,
    area: str | None,
    window: int,
    min_completed: int,
    max_observation_rate: float,
) -> dict[str, Any]:
    completed = [
        record
        for record in records
        if record.get("status") == "completed" and run_matches_area(record, area)
    ]
    completed.sort(
        key=lambda record: record.get("completed_at") or record.get("claimed_at", ""),
        reverse=True,
    )
    recent = completed[:window]
    outcomes: dict[str, int] = {}
    for record in recent:
        outcome = str(record.get("interpretation_outcome", "unknown"))
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
    interpreted_count = outcomes.get("clean", 0) + outcomes.get("observations", 0)
    observation_count = outcomes.get("observations", 0)
    unresolved_count = outcomes.get("blocked", 0) + outcomes.get("inconclusive", 0)
    observation_rate = (
        observation_count / interpreted_count if interpreted_count else None
    )
    if len(recent) >= min_completed and unresolved_count >= max(2, len(recent) // 2):
        signal = "apparatus-friction"
        recommendation = "audit-apparatus-gap"
    elif interpreted_count < min_completed:
        signal = "insufficient-history"
        recommendation = "continue-high-value-selection"
    elif observation_rate is not None and observation_rate <= max_observation_rate:
        signal = "diminishing-returns"
        recommendation = "audit-remaining-candidates-before-another-run"
    else:
        signal = "productive"
        recommendation = "continue-high-value-selection"
    return {
        "scope": area or "all",
        "signal": signal,
        "recommendation": recommendation,
        "heuristic_only": True,
        "window": window,
        "minimum_interpreted_runs": min_completed,
        "maximum_observation_rate": max_observation_rate,
        "matching_completed_runs": len(completed),
        "recent_runs_considered": len(recent),
        "recent_interpreted_runs": interpreted_count,
        "recent_outcomes": outcomes,
        "recent_observation_rate": (
            round(observation_rate, 4) if observation_rate is not None else None
        ),
        "recent_unique_test_ids": len({record.get("test_id") for record in recent}),
        "recent_unique_areas": len({record.get("area") for record in recent}),
        "recent_runs": [
            {
                "run_id": record.get("run_id"),
                "test_id": record.get("test_id"),
                "area": record.get("area"),
                "outcome": record.get("interpretation_outcome"),
                "revision": record.get("revision"),
                "completed_at": record.get("completed_at"),
            }
            for record in recent
        ],
    }


def cmd_value_status(args: argparse.Namespace) -> int:
    repo = repo_root(args.repo)
    if args.window < 1 or args.min_completed < 1:
        raise CoordinatorError("--window and --min-completed must be positive")
    if not 0 <= args.max_observation_rate <= 1:
        raise CoordinatorError("--max-observation-rate must be between 0 and 1")
    registry, _paths = read_registry(repo)
    summary = summarize_value(
        registry["runs"],
        area=args.area,
        window=args.window,
        min_completed=args.min_completed,
        max_observation_rate=args.max_observation_rate,
    )
    summary["open_proposals"] = [
        {
            "proposal_id": proposal.get("proposal_id"),
            "test_id": proposal.get("test_id"),
            "area": proposal.get("area"),
            "title": proposal.get("title"),
            "status": proposal.get("status"),
        }
        for proposal in registry["proposals"]
        if proposal.get("status") in ACTIVE_PROPOSAL_STATUSES
        and run_matches_area(proposal, args.area)
    ]
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


def cmd_proposal_list(args: argparse.Namespace) -> int:
    repo = repo_root(args.repo)
    registry, _paths = read_registry(repo)
    proposals = sorted(
        registry["proposals"], key=lambda item: item.get("proposed_at", ""), reverse=True
    )
    if args.active:
        proposals = [
            proposal
            for proposal in proposals
            if proposal.get("status") in ACTIVE_PROPOSAL_STATUSES
        ]
    if args.json:
        print(json.dumps({"proposals": proposals}, indent=2, sort_keys=True))
        return 0
    if not proposals:
        print("No matching coordinated test proposals.")
        return 0
    print(f"{'PROPOSAL ID':58} {'STATUS':12} {'AREA':18} TEST")
    for proposal in proposals:
        print(
            f"{proposal.get('proposal_id', ''):58} "
            f"{proposal.get('status', ''):12} "
            f"{proposal.get('area', '')[:18]:18} "
            f"{proposal.get('test_id', '')}"
        )
    return 0


def cmd_proposal_create(args: argparse.Namespace) -> int:
    repo = repo_root(args.repo)
    with locked_registry(repo) as (registry, _paths):
        snapshot = find_snapshot(registry, args.snapshot_id)
        snapshot_conflict = next(
            (
                item
                for item in [*registry["runs"], *registry["proposals"]]
                if item.get("snapshot_id") == args.snapshot_id
            ),
            None,
        )
        test_conflict = next(
            (
                item
                for item in [*registry["runs"], *registry["proposals"]]
                if item.get("test_id") == args.test_id
            ),
            None,
        )
        if snapshot_conflict or test_conflict:
            print(
                json.dumps(
                    {"error": "already-coordinated", "record": snapshot_conflict or test_conflict},
                    indent=2,
                ),
                file=sys.stderr,
            )
            return 3
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        proposal = {
            "proposal_id": f"{stamp}-proposal-{slug(args.test_id)}-{secrets.token_hex(3)}",
            "test_id": args.test_id,
            "area": args.area,
            "title": args.title,
            "coverage_gap": args.gap,
            "priority_rationale": args.rationale,
            "scenario": args.scenario,
            "oracle": args.oracle,
            "apparatus": args.apparatus,
            "recommended_tier": args.recommended_tier,
            "cost_and_risks": args.cost,
            "status": "proposed",
            "proposed_at": now_iso(),
            "snapshot_id": snapshot["snapshot_id"],
            "base_ref": snapshot["base_ref"],
            "base_refreshed_at": snapshot["refreshed_at"],
            "revision": snapshot["revision"],
            "revision_subject": snapshot["revision_subject"],
            "revision_committed_at": snapshot["revision_committed_at"],
        }
        registry["proposals"].append(proposal)
        print(json.dumps(proposal, indent=2, sort_keys=True))
    return 0


def cmd_proposal_close(args: argparse.Namespace) -> int:
    repo = repo_root(args.repo)
    with locked_registry(repo) as (registry, _paths):
        proposal = find_proposal(registry, args.proposal_id)
        if proposal.get("status") not in ACTIVE_PROPOSAL_STATUSES:
            raise CoordinatorError(
                f"proposal is already closed with status {proposal.get('status')!r}"
            )
        proposal.update(
            {
                "status": args.status,
                "decision_note": args.note or "",
                "decided_at": now_iso(),
            }
        )
        print(json.dumps(proposal, indent=2, sort_keys=True))
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    repo = repo_root(args.repo)
    registry, _paths = read_registry(repo)
    print(json.dumps(find_run(registry, args.run_id), indent=2, sort_keys=True))
    return 0


def cmd_claim(args: argparse.Namespace) -> int:
    repo = repo_root(args.repo)
    command = command_after_separator(args.command)
    if not args.ci_evidence.strip():
        raise CoordinatorError("--ci-evidence must not be empty")
    with locked_registry(repo) as (registry, paths):
        snapshot = find_snapshot(registry, args.snapshot_id)
        reap_stale(registry, args.stale_seconds)
        run_conflict = next(
            (
                record
                for record in registry["runs"]
                if record.get("snapshot_id") == args.snapshot_id
                or (
                    record.get("status") in ACTIVE_STATUSES
                    and (record.get("test_id") == args.test_id or record.get("command") == command)
                )
            ),
            None,
        )
        proposal_conflict = next(
            (
                proposal
                for proposal in registry["proposals"]
                if proposal.get("snapshot_id") == args.snapshot_id
                or (
                    proposal.get("status") in ACTIVE_PROPOSAL_STATUSES
                    and proposal.get("test_id") == args.test_id
                )
            ),
            None,
        )
        conflict = run_conflict or proposal_conflict
        if conflict:
            print(json.dumps({"error": "already-claimed", "run": conflict}, indent=2), file=sys.stderr)
            return 3
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        run_id = f"{stamp}-{slug(args.test_id)}-{secrets.token_hex(3)}"
        record = {
            "run_id": run_id,
            "test_id": args.test_id,
            "area": args.area or "auto",
            "ci_coverage": args.ci_status,
            "ci_exclusion_evidence": args.ci_evidence,
            "selection_rationale": args.rationale or "",
            "status": "claimed",
            "execution_status": "not-run",
            "interpretation_outcome": "pending",
            "claimed_at": now_iso(),
            "heartbeat_at": now_iso(),
            "repo_root": str(repo),
            "snapshot_id": snapshot["snapshot_id"],
            "base_ref": snapshot["base_ref"],
            "base_refreshed_at": snapshot["refreshed_at"],
            "base_worktree_path": snapshot["base_worktree_path"],
            "revision": snapshot["revision"],
            "revision_subject": snapshot["revision_subject"],
            "revision_committed_at": snapshot["revision_committed_at"],
            "command": command,
            "log_path": str(paths["logs"] / f"{run_id}.log"),
            "report_path": str(paths["reports"] / f"{run_id}.test-result.md"),
        }
        registry["runs"].append(record)
        print(json.dumps(record, indent=2, sort_keys=True))
    return 0


def cmd_worktree(args: argparse.Namespace) -> int:
    repo = repo_root(args.repo)
    with locked_base(repo) as paths:
        registry, _registry_paths = read_registry(repo)
        original = find_run(registry, args.run_id)
        if original.get("status") != "claimed":
            raise CoordinatorError(
                f"worktree creation requires status 'claimed', got {original.get('status')!r}"
            )
        target = paths["worktrees"] / args.run_id
        if target.exists():
            raise CoordinatorError(f"worktree path already exists: {target}")
        record_update(
            repo,
            args.run_id,
            lambda record: record.update({"status": "creating-worktree"}),
        )
        result = run_git(
            repo,
            "worktree",
            "add",
            "--detach",
            str(target),
            original["revision"],
            check=False,
        )
        if result.returncode != 0:
            record_update(
                repo,
                args.run_id,
                lambda record: record.update(
                    {
                        "status": "blocked",
                        "execution_status": "not-run",
                        "completed_at": now_iso(),
                        "worktree_error": result.stderr.strip() or result.stdout.strip(),
                    }
                ),
            )
            print(result.stderr or result.stdout, file=sys.stderr)
            return 4
        record = record_update(
            repo,
            args.run_id,
            lambda item: item.update({"status": "worktree-ready", "worktree_path": str(target)}),
        )
    if result.stdout.strip():
        print(result.stdout.strip())
    print(json.dumps(record, indent=2, sort_keys=True))
    return 0


def terminate_process(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=10)
    except ProcessLookupError:
        pass


def cmd_run(args: argparse.Namespace) -> int:
    repo = repo_root(args.repo)
    registry, _paths = read_registry(repo)
    original = find_run(registry, args.run_id)
    if original.get("status") != "worktree-ready":
        raise CoordinatorError(
            f"execution requires status 'worktree-ready', got {original.get('status')!r}"
        )
    worktree = Path(original.get("worktree_path", ""))
    if not worktree.is_dir():
        raise CoordinatorError("create the run worktree before executing it")
    command = original.get("command")
    if not isinstance(command, list) or not command:
        raise CoordinatorError("claimed command is missing")
    log_path = Path(original["log_path"])
    log_path.parent.mkdir(parents=True, exist_ok=True)
    record_update(
        repo,
        args.run_id,
        lambda record: record.update(
            {"status": "running", "started_at": now_iso(), "runner_pid": os.getpid()}
        ),
    )

    timed_out = False
    cancelled = False
    exit_code = 127
    proc: subprocess.Popen[str] | None = None
    started = time.monotonic()
    try:
        with log_path.open("a", encoding="utf-8") as log:
            banner = f"$ {shlex.join(command)}\n"
            print(banner, end="")
            log.write(banner)
            log.flush()
            env = os.environ.copy()
            env["CODEX_TEST_RUN_ID"] = args.run_id
            env["CODEX_TEST_REVISION"] = original["revision"]
            env["CODEX_TEST_BASE_REF"] = original["base_ref"]
            proc = subprocess.Popen(
                command,
                cwd=worktree,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
                start_new_session=True,
            )

            def copy_output() -> None:
                assert proc is not None and proc.stdout is not None
                for line in proc.stdout:
                    sys.stdout.write(line)
                    sys.stdout.flush()
                    log.write(line)
                    log.flush()

            reader = threading.Thread(target=copy_output, name="codex-test-output", daemon=True)
            reader.start()
            next_heartbeat = time.monotonic() + 20
            while proc.poll() is None:
                time.sleep(0.5)
                now = time.monotonic()
                if now >= next_heartbeat:
                    record_update(repo, args.run_id, lambda _record: None)
                    next_heartbeat = now + 20
                if args.timeout and now - started > args.timeout:
                    timed_out = True
                    terminate_process(proc)
                    break
            exit_code = proc.wait()
            reader.join(timeout=5)
    except KeyboardInterrupt:
        cancelled = True
        if proc is not None:
            terminate_process(proc)
            exit_code = proc.returncode if proc.returncode is not None else 130
    except OSError as exc:
        with log_path.open("a", encoding="utf-8") as log:
            log.write(f"runner error: {exc}\n")
        exit_code = 127

    if timed_out:
        execution = "timed-out"
    elif cancelled:
        execution = "cancelled"
    elif exit_code == 0:
        execution = "passed"
    else:
        execution = "failed"
    record = record_update(
        repo,
        args.run_id,
        lambda item: item.update(
            {
                "status": "awaiting-report",
                "execution_status": execution,
                "test_exit_code": exit_code,
                "test_finished_at": now_iso(),
                "elapsed_seconds": round(time.monotonic() - started, 3),
            }
        ),
    )
    print(json.dumps({"run_id": args.run_id, "execution_status": execution, "exit_code": exit_code, "log_path": str(log_path)}, indent=2))
    return 0 if execution == "passed" else 1


def fill_template(template: str, record: dict[str, Any]) -> str:
    replacements = {
        "{{schema_json}}": json.dumps(REPORT_SCHEMA),
        "{{run_id_json}}": json.dumps(record["run_id"]),
        "{{test_id_json}}": json.dumps(record["test_id"]),
        "{{area_json}}": json.dumps(record.get("area", "auto")),
        "{{ci_status_json}}": json.dumps(record["ci_coverage"]),
        "{{ci_evidence_json}}": json.dumps(record["ci_exclusion_evidence"]),
        "{{snapshot_id_json}}": json.dumps(record["snapshot_id"]),
        "{{base_ref_json}}": json.dumps(record["base_ref"]),
        "{{base_refreshed_at_json}}": json.dumps(record["base_refreshed_at"]),
        "{{execution_status_json}}": json.dumps(record.get("execution_status", "not-run")),
        "{{claimed_at_json}}": json.dumps(record.get("claimed_at")),
        "{{test_finished_at_json}}": json.dumps(record.get("test_finished_at")),
        "{{revision_json}}": json.dumps(record.get("revision")),
        "{{revision_subject_json}}": json.dumps(record.get("revision_subject")),
        "{{revision_committed_at_json}}": json.dumps(record.get("revision_committed_at")),
        "{{command_json}}": json.dumps(shlex.join(record.get("command", []))),
        "{{log_path_json}}": json.dumps(record.get("log_path")),
        "{{title}}": record["test_id"],
        "{{ci_evidence}}": record["ci_exclusion_evidence"],
        "{{selection_rationale}}": record.get("selection_rationale") or "<REPLACE: explain why this test was selected>",
    }
    for marker, value in replacements.items():
        template = template.replace(marker, value)
    return template


def cmd_report_init(args: argparse.Namespace) -> int:
    repo = repo_root(args.repo)
    registry, _paths = read_registry(repo)
    record = find_run(registry, args.run_id)
    if record.get("status") not in {"awaiting-report", "blocked", "completed"}:
        raise CoordinatorError(
            "report initialization requires an executed or blocked run; "
            f"got {record.get('status')!r}"
        )
    destination = Path(record["report_path"])
    if destination.exists():
        print(destination)
        return 0
    template_path = Path(__file__).resolve().parent.parent / "assets" / "test-result-template.md"
    template = template_path.read_text(encoding="utf-8")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(fill_template(template, record), encoding="utf-8")
    print(destination)
    return 0


def validate_report(path: Path, record: dict[str, Any], outcome: str) -> list[str]:
    errors: list[str] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"cannot read report: {exc}"]
    required = [
        f'schema: "{REPORT_SCHEMA}"',
        f'run_id: {json.dumps(record["run_id"])}',
        f'snapshot_id: {json.dumps(record["snapshot_id"])}',
        f'base_ref: {json.dumps(record["base_ref"])}',
        f'base_refreshed_at: {json.dumps(record["base_refreshed_at"])}',
        f'revision: {json.dumps(record["revision"])}',
        f'revision_subject: {json.dumps(record["revision_subject"])}',
        f'revision_committed_at: {json.dumps(record["revision_committed_at"])}',
        f'ci_coverage: {json.dumps(record["ci_coverage"])}',
        f'ci_exclusion_evidence: {json.dumps(record["ci_exclusion_evidence"])}',
        "## Result summary",
        "## Source version",
        "## Selection rationale",
        "## Execution",
        "## Observations",
        "## Coverage and limitations",
        "## Artifacts",
    ]
    for marker in required:
        if marker not in text:
            errors.append(f"missing {marker!r}")
    if f'interpretation_status: "{outcome}"' not in text:
        errors.append(f"frontmatter interpretation_status must be {outcome!r}")
    if "<REPLACE:" in text:
        errors.append("report still contains <REPLACE: ...> markers")
    observation_matches = list(re.finditer(r"^### (OBS-\d{3}) — .+$", text, re.MULTILINE))
    observation_ids = [match.group(1) for match in observation_matches]
    expected_ids = [f"OBS-{index:03d}" for index in range(1, len(observation_ids) + 1)]
    if observation_ids != expected_ids:
        errors.append("observation IDs must be unique and sequential from OBS-001")
    if outcome == "observations" and not observation_matches:
        errors.append("outcome 'observations' requires at least one OBS-nnn section")
    if outcome == "blocked" and not observation_matches:
        errors.append(
            "outcome 'blocked' requires at least one OBS-nnn section describing the blocker"
        )
    if outcome == "clean" and observation_matches:
        errors.append("outcome 'clean' must not contain OBS-nnn sections")
    required_fields = [
        "- **Category:**",
        "- **Severity:**",
        "- **Confidence:**",
        "- **Disposition:**",
        "- **Expected:**",
        "- **Observed:**",
        "- **Evidence:**",
        "- **Interpretation:**",
        "- **Recommended follow-up:**",
    ]
    for index, match in enumerate(observation_matches):
        end = observation_matches[index + 1].start() if index + 1 < len(observation_matches) else len(text)
        section = text[match.start() : end]
        for field in required_fields:
            if field not in section:
                errors.append(f"{match.group(1)} missing {field}")
    return errors


def cmd_report(args: argparse.Namespace) -> int:
    repo = repo_root(args.repo)
    registry, _paths = read_registry(repo)
    original = find_run(registry, args.run_id)
    if original.get("status") == "completed":
        if original.get("interpretation_outcome") != args.outcome:
            raise CoordinatorError(
                "completed report outcome cannot be changed from "
                f"{original.get('interpretation_outcome')!r} to {args.outcome!r}"
            )
        print(json.dumps(original, indent=2, sort_keys=True))
        return 0
    if original.get("status") not in {"awaiting-report", "blocked"}:
        raise CoordinatorError(
            "report attachment requires an executed or blocked run; "
            f"got {original.get('status')!r}"
        )
    report_path = Path(original["report_path"]).resolve()
    reports_root = coordinator_paths(repo)["reports"].resolve()
    if os.path.commonpath([str(report_path), str(reports_root)]) != str(reports_root):
        raise CoordinatorError("report path escaped the coordinator reports directory")
    errors = validate_report(report_path, original, args.outcome)
    if errors:
        print("Report validation failed:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 5
    record = record_update(
        repo,
        args.run_id,
        lambda item: item.update(
            {
                "status": "completed",
                "interpretation_outcome": args.outcome,
                "reported_at": now_iso(),
                "completed_at": now_iso(),
            }
        ),
    )
    print(json.dumps(record, indent=2, sort_keys=True))
    return 0


def cmd_heartbeat(args: argparse.Namespace) -> int:
    repo = repo_root(args.repo)
    record = record_update(repo, args.run_id, lambda _record: None)
    print(json.dumps({"run_id": args.run_id, "heartbeat_at": record["heartbeat_at"]}))
    return 0


def cmd_cleanup(args: argparse.Namespace) -> int:
    repo = repo_root(args.repo)
    with locked_base(repo):
        registry, _paths = read_registry(repo)
        original = find_run(registry, args.run_id)
        if original.get("status") not in {"completed", "blocked", "abandoned"}:
            raise CoordinatorError(
                "cleanup requires a completed, blocked, or abandoned run; "
                f"got {original.get('status')!r}"
            )
        worktree_text = original.get("worktree_path")
        if not worktree_text:
            print("No worktree was recorded.")
            return 0
        worktree = Path(worktree_text)
        if not worktree.exists():
            record_update(
                repo,
                args.run_id,
                lambda record: record.update({"worktree_removed_at": now_iso()}),
            )
            print("Worktree was already absent.")
            return 0
        result = run_git(repo, "worktree", "remove", str(worktree), check=False)
        if result.returncode != 0:
            print("Worktree preserved because normal removal refused it:", file=sys.stderr)
            print(result.stderr or result.stdout, file=sys.stderr)
            return 4
        record_update(
            repo,
            args.run_id,
            lambda record: record.update({"worktree_removed_at": now_iso()}),
        )
    print(f"Removed clean test worktree {worktree}")
    return 0


def add_repo_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", default=".", help="any path inside the target Git repository")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Atomic coordinator for the Codex $test skill")
    sub = parser.add_subparsers(dest="subcommand", required=True)

    init = sub.add_parser("init", help="initialize/show shared coordinator paths")
    add_repo_arg(init)
    init.set_defaults(func=cmd_init)

    refresh = sub.add_parser(
        "refresh",
        help="fetch and fast-forward the detached local test base, then issue one snapshot",
    )
    add_repo_arg(refresh)
    refresh.add_argument(
        "--base-ref",
        help="upstream ref to fetch and snapshot; defaults to the current upstream/default branch",
    )
    refresh.set_defaults(func=cmd_refresh)

    listing = sub.add_parser("list", help="list coordinated runs")
    add_repo_arg(listing)
    listing.add_argument("--active", action="store_true")
    listing.add_argument("--json", action="store_true")
    listing.add_argument("--limit", type=int)
    listing.set_defaults(func=cmd_list)

    value_status = sub.add_parser(
        "value-status",
        help="summarize recent finding yield as a heuristic for diminishing returns",
    )
    add_repo_arg(value_status)
    value_status.add_argument("--area", help="limit history to an area-like text match")
    value_status.add_argument("--window", type=int, default=DEFAULT_VALUE_WINDOW)
    value_status.add_argument(
        "--min-completed", type=int, default=DEFAULT_VALUE_MIN_COMPLETED
    )
    value_status.add_argument(
        "--max-observation-rate",
        type=float,
        default=DEFAULT_VALUE_MAX_OBSERVATION_RATE,
    )
    value_status.set_defaults(func=cmd_value_status)

    proposal_list = sub.add_parser("proposal-list", help="list coordinated test proposals")
    add_repo_arg(proposal_list)
    proposal_list.add_argument("--active", action="store_true")
    proposal_list.add_argument("--json", action="store_true")
    proposal_list.set_defaults(func=cmd_proposal_list)

    proposal_create = sub.add_parser(
        "proposal-create", help="atomically record one missing-test proposal"
    )
    add_repo_arg(proposal_create)
    proposal_create.add_argument("--snapshot-id", required=True)
    proposal_create.add_argument("--test-id", required=True)
    proposal_create.add_argument("--area", required=True)
    proposal_create.add_argument("--title", required=True)
    proposal_create.add_argument("--gap", required=True)
    proposal_create.add_argument("--rationale", required=True)
    proposal_create.add_argument("--scenario", required=True)
    proposal_create.add_argument("--oracle", required=True)
    proposal_create.add_argument("--apparatus", required=True)
    proposal_create.add_argument(
        "--recommended-tier",
        required=True,
        choices=("ci", "manual-only", "exploratory"),
    )
    proposal_create.add_argument("--cost", required=True)
    proposal_create.set_defaults(func=cmd_proposal_create)

    proposal_close = sub.add_parser(
        "proposal-close", help="record the user's decision on an open proposal"
    )
    add_repo_arg(proposal_close)
    proposal_close.add_argument("--proposal-id", required=True)
    proposal_close.add_argument(
        "--status", required=True, choices=("accepted", "rejected", "implemented", "superseded")
    )
    proposal_close.add_argument("--note")
    proposal_close.set_defaults(func=cmd_proposal_close)

    show = sub.add_parser("show", help="show one run record")
    add_repo_arg(show)
    show.add_argument("--run-id", required=True)
    show.set_defaults(func=cmd_show)

    claim = sub.add_parser("claim", help="atomically claim an exact test command")
    add_repo_arg(claim)
    claim.add_argument("--snapshot-id", required=True)
    claim.add_argument("--test-id", required=True)
    claim.add_argument("--area")
    claim.add_argument(
        "--ci-status",
        required=True,
        choices=("manual-only", "outside-ci", "ad-hoc"),
        help="why this test is eligible despite the CI exclusion rule",
    )
    claim.add_argument(
        "--ci-evidence",
        required=True,
        help="workflow or classifier evidence proving the test is not CI-covered",
    )
    claim.add_argument("--rationale")
    claim.add_argument("--stale-seconds", type=int, default=DEFAULT_STALE_SECONDS)
    claim.add_argument("command", nargs=argparse.REMAINDER)
    claim.set_defaults(func=cmd_claim)

    worktree = sub.add_parser("worktree", help="create the claimed detached worktree")
    add_repo_arg(worktree)
    worktree.add_argument("--run-id", required=True)
    worktree.set_defaults(func=cmd_worktree)

    execute = sub.add_parser("run", help="run the claimed command with logging and heartbeats")
    add_repo_arg(execute)
    execute.add_argument("--run-id", required=True)
    execute.add_argument("--timeout", type=float, help="wall-clock timeout in seconds")
    execute.set_defaults(func=cmd_run)

    heartbeat = sub.add_parser("heartbeat", help="refresh a run claim while investigating")
    add_repo_arg(heartbeat)
    heartbeat.add_argument("--run-id", required=True)
    heartbeat.set_defaults(func=cmd_heartbeat)

    report_init = sub.add_parser("report-init", help="create the standard report skeleton")
    add_repo_arg(report_init)
    report_init.add_argument("--run-id", required=True)
    report_init.set_defaults(func=cmd_report_init)

    report = sub.add_parser("report", help="validate and attach the completed report")
    add_repo_arg(report)
    report.add_argument("--run-id", required=True)
    report.add_argument("--outcome", required=True, choices=("clean", "observations", "inconclusive", "blocked"))
    report.set_defaults(func=cmd_report)

    cleanup = sub.add_parser("cleanup", help="remove the worktree only when normal Git removal is safe")
    add_repo_arg(cleanup)
    cleanup.add_argument("--run-id", required=True)
    cleanup.set_defaults(func=cmd_cleanup)
    return parser


def main() -> int:
    try:
        args = build_parser().parse_args()
        return args.func(args)
    except (CoordinatorError, subprocess.CalledProcessError) as exc:
        print(f"test coordinator: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
