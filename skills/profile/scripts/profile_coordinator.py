#!/usr/bin/env python3
"""Atomic local coordination for the paired $profile/$performance skills.

One persistent worktree carries a local-only ``profile-lab`` branch which is
allowed to move only by fast-forward from the repository's upstream base. A
profile run temporarily detaches that worktree at one exact committed revision
and restores the branch afterwards. Registry state, reports, raw artifacts,
logs, and performance assessments live below the common Git directory, so they
never dirty a checkout or acquire a remote lifecycle.
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
from typing import Any, Callable, Iterator


SCHEMA = "codex-profile-coordinator/v1"
REPORT_SCHEMA = "codex-profile-result/v1"
ASSESSMENT_SCHEMA = "codex-performance-assessment/v1"
LAB_BRANCH = "profile-lab"
ACTIVE_RUN_STATUSES = {"claimed", "prepared", "running", "awaiting-report"}
ACTIVE_ASSESSMENT_STATUSES = {"claimed", "worktree-ready", "awaiting-assessment"}
ACTIVE_DELIVERY_STATUSES = {
    "claimed",
    "worktree-ready",
    "candidate-ready",
    "validating",
    "validated",
}
DELIVERY_STATUSES = ACTIVE_DELIVERY_STATUSES | {"pr-open", "stopped"}
DELIVERY_TRANSITIONS = {
    "claimed": {"worktree-ready", "stopped"},
    "worktree-ready": {"candidate-ready", "stopped"},
    "candidate-ready": {"validating", "stopped"},
    "validating": {"candidate-ready", "validating", "validated", "stopped"},
    "validated": {"candidate-ready", "pr-open", "stopped"},
    "pr-open": set(),
    "stopped": set(),
}
DEFAULT_STALE_SECONDS = 24 * 60 * 60
RUN_OUTCOMES = ("complete", "inconclusive", "blocked")
ASSESSMENT_OUTCOMES = ("recommendations", "follow-up-measurement", "no-action")


class CoordinatorError(RuntimeError):
    """A controlled refusal or invalid coordinator transition."""


class OwnershipConflict(CoordinatorError):
    """Another active invocation owns the selected local resource."""


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def parse_iso(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def slug(value: str, limit: int = 48) -> str:
    clean = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return (clean or "profile")[:limit]


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


def canonical_checkout(repo: Path) -> Path:
    common = common_git_dir(repo)
    return common.parent.resolve() if common.name == ".git" else repo.resolve()


def coordinator_paths(repo: Path) -> dict[str, Path]:
    common = common_git_dir(repo)
    root = common / "codex-profile"
    canonical = canonical_checkout(repo)
    fingerprint = hashlib.sha256(str(common).encode()).hexdigest()[:8]
    sibling = canonical.parent
    return {
        "root": root,
        "registry": root / "registry.json",
        "coordinator": root / "coordinator.md",
        "registry_lock": root / "registry.lock",
        "lab_lock": root / "lab.lock",
        "analysis_lock": root / "analysis-worktrees.lock",
        "delivery_lock": root / "delivery-worktrees.lock",
        "logs": root / "logs",
        "artifacts": root / "artifacts",
        "reports": root / "reports",
        "assessments": root / "assessments",
        "lab_worktree": sibling
        / f".codex-profile-worktree-{canonical.name}-{fingerprint}",
        "analysis_worktrees": sibling
        / f".codex-performance-worktrees-{canonical.name}-{fingerprint}",
    }


def ensure_dirs(paths: dict[str, Path]) -> None:
    for key in (
        "root",
        "logs",
        "artifacts",
        "reports",
        "assessments",
        "analysis_worktrees",
    ):
        paths[key].mkdir(parents=True, exist_ok=True)


def empty_registry() -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "updated_at": now_iso(),
        "snapshots": [],
        "runs": [],
        "assessments": [],
        "deliveries": [],
    }


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f"{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def md_cell(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def render_coordinator(registry: dict[str, Any], paths: dict[str, Path]) -> str:
    lines = [
        "# Local performance lab coordinator",
        "",
        "This file is generated by `profile_coordinator.py`. Do not edit it by hand.",
        "The entire lab is local-only: no entry here is committed, pushed, or published.",
        "",
        f"- Updated: `{registry.get('updated_at', '')}`",
        f"- Persistent worktree: `{paths['lab_worktree']}`",
        f"- Local branch: `{LAB_BRANCH}`",
        f"- Registry: `{paths['registry']}`",
        "",
        "## Active profile runs",
        "",
        "| Run | State | Area | Question | Revision | Claimed |",
        "|---|---|---|---|---|---|",
    ]
    active = [
        item for item in registry["runs"] if item.get("status") in ACTIVE_RUN_STATUSES
    ]
    if active:
        for item in sorted(active, key=lambda row: row.get("claimed_at", "")):
            lines.append(
                "| `{}` | {} | {} | {} | `{}` | {} |".format(
                    md_cell(item.get("run_id")),
                    md_cell(item.get("status")),
                    md_cell(item.get("area")),
                    md_cell(item.get("performance_question")),
                    md_cell(str(item.get("revision", ""))[:12]),
                    md_cell(item.get("claimed_at")),
                )
            )
    else:
        lines.append("| — | — | — | — | — | — |")

    lines.extend(
        [
            "",
            "## Completed profile reports",
            "",
            "| Run | Date | Profile | Metric / instrument | Revision | Result | Analysis | Report |",
            "|---|---|---|---|---|---|---|---|",
        ]
    )
    completed = [item for item in registry["runs"] if item.get("status") == "completed"]
    if completed:
        for item in sorted(completed, key=lambda row: row.get("reported_at", ""), reverse=True):
            report = item.get("report_path", "")
            report_link = f"[report](<{report}>)" if report else "—"
            lines.append(
                "| `{}` | {} | {} | {} / {} | `{}` | {} | {} | {} |".format(
                    md_cell(item.get("run_id")),
                    md_cell(item.get("reported_at")),
                    md_cell(item.get("profile_id")),
                    md_cell(item.get("metric_family")),
                    md_cell(item.get("instrument")),
                    md_cell(str(item.get("revision", ""))[:12]),
                    md_cell(item.get("measurement_outcome")),
                    md_cell(item.get("analysis_status", "unprocessed")),
                    report_link,
                )
            )
    else:
        lines.append("| — | — | — | — | — | — | — | — |")

    lines.extend(
        [
            "",
            "## Performance assessments",
            "",
            "| Assessment | State | Current revision | Source reports | Outcome | Document |",
            "|---|---|---|---:|---|---|",
        ]
    )
    if registry["assessments"]:
        for item in sorted(
            registry["assessments"], key=lambda row: row.get("claimed_at", ""), reverse=True
        ):
            document = item.get("assessment_path", "")
            document_link = f"[assessment](<{document}>)" if document else "—"
            lines.append(
                "| `{}` | {} | `{}` | {} | {} | {} |".format(
                    md_cell(item.get("assessment_id")),
                    md_cell(item.get("status")),
                    md_cell(str(item.get("assessment_revision", ""))[:12]),
                    len(item.get("run_ids", [])),
                    md_cell(item.get("outcome", "pending")),
                    document_link,
                )
            )
    else:
        lines.append("| — | — | — | 0 | — | — |")
    lines.extend(
        [
            "",
            "## Performance deliveries",
            "",
            "| Delivery | Finding | State | Branch | Candidate | Validation runs | PR |",
            "|---|---|---|---|---|---:|---|",
        ]
    )
    if registry["deliveries"]:
        for item in sorted(
            registry["deliveries"], key=lambda row: row.get("claimed_at", ""), reverse=True
        ):
            pr_url = item.get("pr_url", "")
            pr_link = f"[PR](<{pr_url}>)" if pr_url else "—"
            lines.append(
                "| `{}` | `{}` / {} | {} | `{}` | `{}` | {} | {} |".format(
                    md_cell(item.get("delivery_id")),
                    md_cell(item.get("assessment_id")),
                    md_cell(item.get("finding_id")),
                    md_cell(item.get("status")),
                    md_cell(item.get("branch")),
                    md_cell(str(item.get("candidate_revision", ""))[:12]),
                    len(item.get("validation_run_ids", [])),
                    pr_link,
                )
            )
    else:
        lines.append("| — | — | — | — | — | 0 | — |")
    lines.append("")
    return "\n".join(lines)


@contextlib.contextmanager
def locked_registry(repo: Path) -> Iterator[tuple[dict[str, Any], dict[str, Path]]]:
    paths = coordinator_paths(repo)
    ensure_dirs(paths)
    with paths["registry_lock"].open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if paths["registry"].exists():
            try:
                registry = json.loads(paths["registry"].read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise CoordinatorError(f"cannot read registry {paths['registry']}: {exc}") from exc
            if registry.get("schema") != SCHEMA:
                raise CoordinatorError(f"unsupported registry schema in {paths['registry']}")
            registry.setdefault("deliveries", [])
            for key in ("snapshots", "runs", "assessments", "deliveries"):
                if not isinstance(registry.get(key), list):
                    raise CoordinatorError(f"registry field {key!r} is not a list")
        else:
            registry = empty_registry()
        yield registry, paths
        registry["updated_at"] = now_iso()
        atomic_write(paths["registry"], json.dumps(registry, indent=2, sort_keys=True) + "\n")
        atomic_write(paths["coordinator"], render_coordinator(registry, paths))
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def file_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        yield
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def read_registry(repo: Path) -> tuple[dict[str, Any], dict[str, Path]]:
    with locked_registry(repo) as (registry, paths):
        return json.loads(json.dumps(registry)), paths


def find_record(records: list[dict[str, Any]], key: str, value: str, kind: str) -> dict[str, Any]:
    for record in records:
        if record.get(key) == value:
            return record
    raise CoordinatorError(f"unknown {kind} {value!r}")


def find_run(registry: dict[str, Any], run_id: str) -> dict[str, Any]:
    return find_record(registry["runs"], "run_id", run_id, "profile run")


def find_snapshot(registry: dict[str, Any], snapshot_id: str) -> dict[str, Any]:
    return find_record(registry["snapshots"], "snapshot_id", snapshot_id, "snapshot")


def find_assessment(registry: dict[str, Any], assessment_id: str) -> dict[str, Any]:
    return find_record(
        registry["assessments"], "assessment_id", assessment_id, "performance assessment"
    )


def find_delivery(registry: dict[str, Any], delivery_id: str) -> dict[str, Any]:
    return find_record(
        registry["deliveries"], "delivery_id", delivery_id, "performance delivery"
    )


def record_update(repo: Path, run_id: str, update: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    with locked_registry(repo) as (registry, _paths):
        record = find_run(registry, run_id)
        update(record)
        record["heartbeat_at"] = now_iso()
        return json.loads(json.dumps(record))


def assessment_update(
    repo: Path, assessment_id: str, update: Callable[[dict[str, Any]], None]
) -> dict[str, Any]:
    with locked_registry(repo) as (registry, _paths):
        record = find_assessment(registry, assessment_id)
        update(record)
        record["heartbeat_at"] = now_iso()
        return json.loads(json.dumps(record))


def delivery_update(
    repo: Path, delivery_id: str, update: Callable[[dict[str, Any]], None]
) -> dict[str, Any]:
    with locked_registry(repo) as (registry, _paths):
        record = find_delivery(registry, delivery_id)
        update(record)
        record["heartbeat_at"] = now_iso()
        return json.loads(json.dumps(record))


def command_after_separator(values: list[str]) -> list[str]:
    if values and values[0] == "--":
        values = values[1:]
    if not values:
        raise CoordinatorError("execution requires a command after --")
    return values


def default_base_ref(repo: Path) -> str:
    upstream = run_git(
        repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}", check=False
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


def remote_for_ref(repo: Path, ref: str) -> str | None:
    normalized = ref.removeprefix("refs/remotes/")
    for remote in run_git(repo, "remote").stdout.splitlines():
        if normalized == remote or normalized.startswith(f"{remote}/"):
            return remote
    return None


def resolve_commit(repo: Path, ref: str, label: str) -> str:
    result = run_git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}", check=False)
    if result.returncode != 0:
        raise CoordinatorError(
            f"cannot resolve {label} {ref!r}: {result.stderr.strip() or result.stdout.strip()}"
        )
    return result.stdout.strip()


def worktree_branch(worktree: Path) -> str | None:
    result = run_git(worktree, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def require_clean_worktree(worktree: Path, purpose: str) -> None:
    dirty = run_git(worktree, "status", "--porcelain", "--untracked-files=all").stdout.strip()
    if dirty:
        raise CoordinatorError(
            f"{purpose} worktree is dirty; preserve and inspect it before continuing: "
            f"{worktree}\n{dirty}"
        )


def validate_existing_worktree(repo: Path, target: Path) -> None:
    if not target.is_dir():
        raise CoordinatorError(f"profiling worktree path is not a directory: {target}")
    try:
        target_root = repo_root(target)
        target_common = common_git_dir(target_root)
    except subprocess.CalledProcessError as exc:
        raise CoordinatorError(f"profiling path is not a valid Git worktree: {target}") from exc
    if target_root != target.resolve() or target_common != common_git_dir(repo):
        raise CoordinatorError(f"profiling worktree belongs to another repository: {target}")


def ensure_lab_worktree(repo: Path, paths: dict[str, Path], base_revision: str) -> None:
    target = paths["lab_worktree"]
    branch_ref = f"refs/heads/{LAB_BRANCH}"
    branch_exists = run_git(repo, "show-ref", "--verify", "--quiet", branch_ref, check=False).returncode == 0

    if target.exists():
        validate_existing_worktree(repo, target)
        require_clean_worktree(target, "profiling")
        if worktree_branch(target) != LAB_BRANCH:
            switched = run_git(target, "switch", LAB_BRANCH, check=False)
            if switched.returncode != 0:
                raise CoordinatorError(switched.stderr.strip() or switched.stdout.strip())
    elif branch_exists:
        branch_revision = resolve_commit(repo, LAB_BRANCH, "profiling branch")
        ancestry = run_git(repo, "merge-base", "--is-ancestor", branch_revision, base_revision, check=False)
        if ancestry.returncode != 0:
            raise CoordinatorError(
                f"local {LAB_BRANCH} has commits not fast-forwardable to the base; "
                "the lab branch must never carry unique commits"
            )
        created = run_git(repo, "worktree", "add", str(target), LAB_BRANCH, check=False)
        if created.returncode != 0:
            raise CoordinatorError(created.stderr.strip() or created.stdout.strip())
    else:
        created = run_git(
            repo, "worktree", "add", "-b", LAB_BRANCH, str(target), base_revision, check=False
        )
        if created.returncode != 0:
            raise CoordinatorError(created.stderr.strip() or created.stdout.strip())

    require_clean_worktree(target, "profiling")
    current = resolve_commit(target, "HEAD", "profiling worktree HEAD")
    ancestry = run_git(repo, "merge-base", "--is-ancestor", current, base_revision, check=False)
    if ancestry.returncode != 0:
        raise CoordinatorError(
            f"refusing a non-fast-forward profiling branch move: {current} -> {base_revision}"
        )
    if current != base_revision:
        moved = run_git(target, "merge", "--ff-only", base_revision, check=False)
        if moved.returncode != 0:
            raise CoordinatorError(moved.stderr.strip() or moved.stdout.strip())


def reap_stale_runs(registry: dict[str, Any], stale_seconds: int) -> None:
    now = dt.datetime.now(dt.timezone.utc)
    for record in registry["runs"]:
        if record.get("status") not in ACTIVE_RUN_STATUSES:
            continue
        stamp = parse_iso(record.get("heartbeat_at") or record.get("claimed_at"))
        if stamp and (now - stamp).total_seconds() > stale_seconds:
            prior = record["status"]
            record.update(
                {
                    "status": "abandoned",
                    "completed_at": now_iso(),
                    "abandon_reason": f"stale {prior} claim exceeded {stale_seconds}s",
                }
            )


def cmd_init(args: argparse.Namespace) -> int:
    repo = repo_root(args.repo)
    with locked_registry(repo) as (_registry, paths):
        payload = {key: str(value) for key, value in paths.items()}
        payload["lab_branch"] = LAB_BRANCH
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def cmd_refresh(args: argparse.Namespace) -> int:
    repo = repo_root(args.repo)
    paths = coordinator_paths(repo)
    with file_lock(paths["lab_lock"]):
        with locked_registry(repo) as (registry, _locked_paths):
            reap_stale_runs(registry, args.stale_seconds)
            active = [
                record
                for record in registry["runs"]
                if record.get("status") in ACTIVE_RUN_STATUSES
            ]
            if active:
                owner = active[0]
                raise OwnershipConflict(
                    f"profiling lab is owned by {owner.get('run_id')} "
                    f"({owner.get('status')})"
                )

        base_ref = args.base_ref or default_base_ref(repo)
        remote = remote_for_ref(repo, base_ref)
        if remote:
            fetched = run_git(repo, "fetch", "--prune", remote, check=False)
            if fetched.returncode != 0:
                raise CoordinatorError(
                    f"failed to fetch {remote}: {fetched.stderr.strip() or fetched.stdout.strip()}"
                )
        base_revision = resolve_commit(repo, base_ref, "profiling base ref")
        ensure_lab_worktree(repo, paths, base_revision)

        target_ref = args.target_ref or base_ref
        revision = resolve_commit(repo, target_ref, "profiling target ref")
        source_dirty = bool(
            run_git(repo, "status", "--porcelain", "--untracked-files=all").stdout.strip()
        )
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        snapshot = {
            "snapshot_id": f"{stamp}-{slug(target_ref)}-{secrets.token_hex(3)}",
            "base_ref": base_ref,
            "base_revision": base_revision,
            "target_ref": target_ref,
            "revision": revision,
            "revision_subject": run_git(repo, "show", "-s", "--format=%s", revision).stdout.strip(),
            "revision_committed_at": run_git(
                repo, "show", "-s", "--format=%cI", revision
            ).stdout.strip(),
            "refreshed_at": now_iso(),
            "fetch_remote": remote,
            "source_checkout": str(repo),
            "source_checkout_dirty": source_dirty,
            "lab_worktree_path": str(paths["lab_worktree"]),
        }
        with locked_registry(repo) as (registry, _locked_paths):
            registry["snapshots"].append(snapshot)
    print(json.dumps(snapshot, indent=2, sort_keys=True))
    return 0


def area_matches(record: dict[str, Any], hint: str) -> bool:
    needle = hint.casefold()
    fields = (
        record.get("area", ""),
        record.get("profile_id", ""),
        record.get("workload_id", ""),
        record.get("performance_question", ""),
    )
    return any(needle in str(value).casefold() for value in fields)


def cmd_list(args: argparse.Namespace) -> int:
    repo = repo_root(args.repo)
    registry, paths = read_registry(repo)
    records = list(registry["runs"])
    if args.active:
        records = [record for record in records if record.get("status") in ACTIVE_RUN_STATUSES]
    if args.unprocessed:
        records = [
            record
            for record in records
            if record.get("status") == "completed"
            and record.get("analysis_status", "unprocessed") == "unprocessed"
        ]
    if args.area:
        records = [record for record in records if area_matches(record, args.area)]
    records.sort(key=lambda record: record.get("claimed_at", ""), reverse=True)
    if args.limit:
        records = records[: args.limit]
    if args.json:
        print(
            json.dumps(
                {"paths": {key: str(value) for key, value in paths.items()}, "runs": records},
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    if not records:
        print("No matching coordinated profile runs.")
        return 0
    print(f"{'RUN ID':62} {'STATUS':18} {'AREA':18} PROFILE")
    for record in records:
        print(
            f"{record.get('run_id', ''):62} "
            f"{record.get('status', ''):18} "
            f"{record.get('area', '')[:18]:18} "
            f"{record.get('profile_id', '')}"
        )
    print(f"\nCoordinator: {paths['coordinator']}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    repo = repo_root(args.repo)
    registry, _paths = read_registry(repo)
    record = find_run(registry, args.run_id)
    print(json.dumps(record, indent=2, sort_keys=True))
    return 0


def cmd_claim(args: argparse.Namespace) -> int:
    repo = repo_root(args.repo)
    with locked_registry(repo) as (registry, paths):
        reap_stale_runs(registry, args.stale_seconds)
        active = [
            record for record in registry["runs"] if record.get("status") in ACTIVE_RUN_STATUSES
        ]
        if active:
            owner = active[0]
            raise OwnershipConflict(
                f"profiling lab is owned by {owner.get('run_id')} ({owner.get('status')})"
            )
        snapshot = find_snapshot(registry, args.snapshot_id)
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        run_id = f"{stamp}-{slug(args.profile_id)}-{secrets.token_hex(3)}"
        artifact_dir = paths["artifacts"] / run_id
        log_dir = paths["logs"] / run_id
        artifact_dir.mkdir(parents=True, exist_ok=False)
        log_dir.mkdir(parents=True, exist_ok=False)
        record = {
            "run_id": run_id,
            "profile_id": args.profile_id,
            "area": args.area,
            "workload_id": args.workload_id,
            "metric_family": args.metric_family,
            "instrument": args.instrument,
            "performance_question": args.question,
            "selection_rationale": args.rationale,
            "comparison_key": args.comparison_key,
            "status": "claimed",
            "execution_status": "not-run",
            "measurement_outcome": "pending",
            "analysis_status": "not-ready",
            "claimed_at": now_iso(),
            "heartbeat_at": now_iso(),
            "repo_root": str(repo),
            "snapshot_id": snapshot["snapshot_id"],
            "base_ref": snapshot["base_ref"],
            "base_revision": snapshot["base_revision"],
            "target_ref": snapshot["target_ref"],
            "revision": snapshot["revision"],
            "revision_subject": snapshot["revision_subject"],
            "revision_committed_at": snapshot["revision_committed_at"],
            "base_refreshed_at": snapshot["refreshed_at"],
            "source_checkout_dirty": snapshot["source_checkout_dirty"],
            "worktree_path": str(paths["lab_worktree"]),
            "artifact_dir": str(artifact_dir),
            "log_dir": str(log_dir),
            "report_path": str(paths["reports"] / f"{run_id}.profile-result.md"),
            "commands": [],
        }
        registry["runs"].append(record)
    print(json.dumps(record, indent=2, sort_keys=True))
    return 0


def cmd_prepare(args: argparse.Namespace) -> int:
    repo = repo_root(args.repo)
    paths = coordinator_paths(repo)
    with file_lock(paths["lab_lock"]):
        registry, _paths = read_registry(repo)
        original = find_run(registry, args.run_id)
        if original.get("status") != "claimed":
            raise CoordinatorError(
                f"preparation requires status 'claimed', got {original.get('status')!r}"
            )
        target = Path(original["worktree_path"])
        validate_existing_worktree(repo, target)
        require_clean_worktree(target, "profiling")
        if worktree_branch(target) != LAB_BRANCH:
            raise CoordinatorError(
                f"profiling worktree is not attached to {LAB_BRANCH}; run refresh after "
                "resolving any prior interrupted run"
            )
        checked = run_git(target, "switch", "--detach", original["revision"], check=False)
        if checked.returncode != 0:
            record_update(
                repo,
                args.run_id,
                lambda record: record.update(
                    {
                        "status": "awaiting-report",
                        "execution_status": "blocked",
                        "preparation_error": checked.stderr.strip() or checked.stdout.strip(),
                    }
                ),
            )
            return 4
        record = record_update(
            repo,
            args.run_id,
            lambda item: item.update({"status": "prepared", "prepared_at": now_iso()}),
        )
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


def update_command_record(
    repo: Path, run_id: str, sequence: int, update: dict[str, Any]
) -> dict[str, Any]:
    def apply(record: dict[str, Any]) -> None:
        for command in record.get("commands", []):
            if command.get("sequence") == sequence:
                command.update(update)
                return
        raise CoordinatorError(f"run {run_id} has no command sequence {sequence}")

    return record_update(repo, run_id, apply)


def cmd_exec(args: argparse.Namespace) -> int:
    repo = repo_root(args.repo)
    paths = coordinator_paths(repo)
    command = command_after_separator(args.command)
    with file_lock(paths["lab_lock"]):
        registry, _paths = read_registry(repo)
        original = find_run(registry, args.run_id)
        if original.get("status") != "prepared":
            raise CoordinatorError(
                f"execution requires status 'prepared', got {original.get('status')!r}"
            )
        worktree = Path(original["worktree_path"])
        validate_existing_worktree(repo, worktree)
        require_clean_worktree(worktree, "profiling")
        current = resolve_commit(worktree, "HEAD", "profiling worktree HEAD")
        if current != original["revision"] or worktree_branch(worktree) is not None:
            raise CoordinatorError("profiling worktree is not detached at the claimed revision")

        sequence = len(original.get("commands", [])) + 1
        log_path = Path(original["log_dir"]) / f"{sequence:02d}-{slug(args.phase)}.log"
        command_record = {
            "sequence": sequence,
            "phase": args.phase,
            "argv": command,
            "command": shlex.join(command),
            "log_path": str(log_path),
            "started_at": now_iso(),
            "status": "running",
            "final": bool(args.final),
        }

        def begin(record: dict[str, Any]) -> None:
            record["status"] = "running"
            record["execution_status"] = "running"
            record["commands"].append(command_record)

        record_update(repo, args.run_id, begin)

        timed_out = False
        cancelled = False
        exit_code = 127
        proc: subprocess.Popen[str] | None = None
        started = time.monotonic()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with log_path.open("a", encoding="utf-8") as log:
                banner = f"$ {shlex.join(command)}\n"
                print(banner, end="")
                log.write(banner)
                log.flush()
                env = os.environ.copy()
                env.update(
                    {
                        "CODEX_PROFILE_RUN_ID": args.run_id,
                        "CODEX_PROFILE_REVISION": original["revision"],
                        "CODEX_PROFILE_ARTIFACT_DIR": original["artifact_dir"],
                    }
                )
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

                reader = threading.Thread(
                    target=copy_output, name="codex-profile-output", daemon=True
                )
                reader.start()
                next_heartbeat = time.monotonic() + 20
                while proc.poll() is None:
                    time.sleep(0.5)
                    current_time = time.monotonic()
                    if current_time >= next_heartbeat:
                        record_update(repo, args.run_id, lambda _record: None)
                        next_heartbeat = current_time + 20
                    if args.timeout and current_time - started > args.timeout:
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
        elapsed = round(time.monotonic() - started, 3)
        update_command_record(
            repo,
            args.run_id,
            sequence,
            {
                "status": execution,
                "exit_code": exit_code,
                "finished_at": now_iso(),
                "elapsed_seconds": elapsed,
            },
        )

        def finish(record: dict[str, Any]) -> None:
            record["execution_status"] = execution
            record["last_command_finished_at"] = now_iso()
            record["status"] = (
                "awaiting-report" if args.final or execution != "passed" else "prepared"
            )

        record = record_update(repo, args.run_id, finish)
    print(
        json.dumps(
            {
                "run_id": args.run_id,
                "phase": args.phase,
                "execution_status": execution,
                "exit_code": exit_code,
                "elapsed_seconds": elapsed,
                "log_path": str(log_path),
                "next_status": record["status"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if execution == "passed" else 1


def cmd_block(args: argparse.Namespace) -> int:
    repo = repo_root(args.repo)

    def block(record: dict[str, Any]) -> None:
        if record.get("status") not in {"claimed", "prepared"}:
            raise CoordinatorError(
                f"blocking requires a claimed or prepared run, got {record.get('status')!r}"
            )
        record.update(
            {
                "status": "awaiting-report",
                "execution_status": "blocked",
                "block_reason": args.reason,
            }
        )

    record = record_update(repo, args.run_id, block)
    print(json.dumps(record, indent=2, sort_keys=True))
    return 0


def cmd_heartbeat(args: argparse.Namespace) -> int:
    repo = repo_root(args.repo)
    record = record_update(repo, args.run_id, lambda _record: None)
    print(json.dumps({"run_id": args.run_id, "heartbeat_at": record["heartbeat_at"]}))
    return 0


def profile_template_path() -> Path:
    return Path(__file__).resolve().parent.parent / "assets" / "profile-result-template.md"


def performance_template_path() -> Path:
    return (
        Path(__file__).resolve().parent.parent.parent
        / "performance"
        / "assets"
        / "performance-assessment-template.md"
    )


def fill_profile_template(template: str, record: dict[str, Any]) -> str:
    commands = "\n".join(
        f"- `{item['phase']}`: `{item['command']}` → `{item.get('status', 'pending')}` "
        f"([log](<{item['log_path']}>))"
        for item in record.get("commands", [])
    ) or "- No command completed; explain the blocker."
    replacements = {
        "{{schema_json}}": json.dumps(REPORT_SCHEMA),
        "{{run_id_json}}": json.dumps(record["run_id"]),
        "{{profile_id_json}}": json.dumps(record["profile_id"]),
        "{{area_json}}": json.dumps(record["area"]),
        "{{workload_id_json}}": json.dumps(record["workload_id"]),
        "{{metric_family_json}}": json.dumps(record["metric_family"]),
        "{{instrument_json}}": json.dumps(record["instrument"]),
        "{{snapshot_id_json}}": json.dumps(record["snapshot_id"]),
        "{{base_ref_json}}": json.dumps(record["base_ref"]),
        "{{base_revision_json}}": json.dumps(record["base_revision"]),
        "{{target_ref_json}}": json.dumps(record["target_ref"]),
        "{{revision_json}}": json.dumps(record["revision"]),
        "{{revision_subject_json}}": json.dumps(record["revision_subject"]),
        "{{revision_committed_at_json}}": json.dumps(record["revision_committed_at"]),
        "{{base_refreshed_at_json}}": json.dumps(record["base_refreshed_at"]),
        "{{claimed_at_json}}": json.dumps(record["claimed_at"]),
        "{{comparison_key_json}}": json.dumps(record["comparison_key"]),
        "{{execution_status_json}}": json.dumps(record.get("execution_status")),
        "{{artifact_dir_json}}": json.dumps(record["artifact_dir"]),
        "{{title}}": record["profile_id"],
        "{{performance_question}}": record["performance_question"],
        "{{selection_rationale}}": record["selection_rationale"],
        "{{commands}}": commands,
        "{{source_dirty}}": "yes" if record.get("source_checkout_dirty") else "no",
        "{{block_reason}}": record.get("block_reason", "None recorded."),
    }
    for marker, value in replacements.items():
        template = template.replace(marker, value)
    return template


def cmd_report_init(args: argparse.Namespace) -> int:
    repo = repo_root(args.repo)
    registry, _paths = read_registry(repo)
    record = find_run(registry, args.run_id)
    if record.get("status") not in {"awaiting-report", "completed"}:
        raise CoordinatorError(
            f"report initialization requires an ended run, got {record.get('status')!r}"
        )
    destination = Path(record["report_path"])
    if not destination.exists():
        template = profile_template_path().read_text(encoding="utf-8")
        atomic_write(destination, fill_profile_template(template, record))
    print(destination)
    return 0


def validate_profile_report(path: Path, record: dict[str, Any], outcome: str) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"cannot read report: {exc}"]
    required = [
        f'schema: "{REPORT_SCHEMA}"',
        f'run_id: {json.dumps(record["run_id"])}',
        f'profile_id: {json.dumps(record["profile_id"])}',
        f'snapshot_id: {json.dumps(record["snapshot_id"])}',
        f'base_ref: {json.dumps(record["base_ref"])}',
        f'base_revision: {json.dumps(record["base_revision"])}',
        f'target_ref: {json.dumps(record["target_ref"])}',
        f'revision: {json.dumps(record["revision"])}',
        f'comparison_key: {json.dumps(record["comparison_key"])}',
        f'artifact_dir: {json.dumps(record["artifact_dir"])}',
        f'measurement_status: "{outcome}"',
        "machine_fingerprint:",
        "toolchain_fingerprint:",
        "build_method_fingerprint:",
        "artifact_fingerprint:",
        "workload_fingerprint:",
        "## Performance question",
        "## Source version",
        "## Environment and comparability",
        "## Method",
        "## Measurements",
        "## Measurement interpretation",
        "## Coverage and limitations",
        "## Artifacts",
    ]
    errors = [f"missing {marker!r}" for marker in required if marker not in text]
    if "<REPLACE:" in text:
        errors.append("report still contains <REPLACE: ...> markers")
    if "{{" in text or "}}" in text:
        errors.append("report still contains an unresolved template marker")
    for field in (
        "machine_fingerprint",
        "toolchain_fingerprint",
        "build_method_fingerprint",
        "artifact_fingerprint",
        "workload_fingerprint",
    ):
        if re.search(rf"^{field}:\s*(?:\"\"|null)?\s*$", text, re.MULTILINE):
            errors.append(f"frontmatter {field} must not be empty")
    return errors


def cmd_report(args: argparse.Namespace) -> int:
    repo = repo_root(args.repo)
    registry, _paths = read_registry(repo)
    original = find_run(registry, args.run_id)
    if original.get("status") == "completed":
        if original.get("measurement_outcome") != args.outcome:
            raise CoordinatorError("a completed profile report outcome cannot be changed")
        print(json.dumps(original, indent=2, sort_keys=True))
        return 0
    if original.get("status") != "awaiting-report":
        raise CoordinatorError(
            f"report attachment requires status 'awaiting-report', got {original.get('status')!r}"
        )
    report_path = Path(original["report_path"]).resolve()
    reports_root = coordinator_paths(repo)["reports"].resolve()
    if os.path.commonpath([str(report_path), str(reports_root)]) != str(reports_root):
        raise CoordinatorError("report path escaped the coordinator reports directory")
    errors = validate_profile_report(report_path, original, args.outcome)
    if errors:
        print("Profile report validation failed:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 5

    def attach(record: dict[str, Any]) -> None:
        record.update(
            {
                "status": "completed",
                "measurement_outcome": args.outcome,
                "analysis_status": "unprocessed",
                "reported_at": now_iso(),
                "completed_at": now_iso(),
            }
        )

    record = record_update(repo, args.run_id, attach)
    print(json.dumps(record, indent=2, sort_keys=True))
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    repo = repo_root(args.repo)
    paths = coordinator_paths(repo)
    with file_lock(paths["lab_lock"]):
        registry, _paths = read_registry(repo)
        original = find_run(registry, args.run_id)
        if original.get("status") not in {"completed", "abandoned"}:
            raise CoordinatorError(
                f"restoration requires a completed or abandoned run, got {original.get('status')!r}"
            )
        target = Path(original["worktree_path"])
        validate_existing_worktree(repo, target)
        require_clean_worktree(target, "profiling")
        if worktree_branch(target) != LAB_BRANCH:
            switched = run_git(target, "switch", LAB_BRANCH, check=False)
            if switched.returncode != 0:
                print("Profiling worktree preserved at the measured revision:", file=sys.stderr)
                print(switched.stderr or switched.stdout, file=sys.stderr)
                return 4
        record = record_update(
            repo,
            args.run_id,
            lambda item: item.update({"lab_restored_at": now_iso()}),
        )
    print(json.dumps({"run_id": record["run_id"], "worktree": str(target)}, indent=2))
    return 0


def fetch_assessment_revision(repo: Path, base_ref: str | None) -> tuple[str, str, str | None]:
    resolved_ref = base_ref or default_base_ref(repo)
    remote = remote_for_ref(repo, resolved_ref)
    if remote:
        fetched = run_git(repo, "fetch", "--prune", remote, check=False)
        if fetched.returncode != 0:
            raise CoordinatorError(
                f"failed to fetch {remote}: {fetched.stderr.strip() or fetched.stdout.strip()}"
            )
    return resolved_ref, resolve_commit(repo, resolved_ref, "assessment base ref"), remote


def cmd_analysis_list(args: argparse.Namespace) -> int:
    repo = repo_root(args.repo)
    registry, paths = read_registry(repo)
    if args.assessments:
        records = list(registry["assessments"])
        if args.active:
            records = [
                item for item in records if item.get("status") in ACTIVE_ASSESSMENT_STATUSES
            ]
        print(json.dumps({"assessments": records, "coordinator": str(paths["coordinator"])}, indent=2))
        return 0
    records = [
        item
        for item in registry["runs"]
        if item.get("status") == "completed"
        and item.get("analysis_status", "unprocessed") == "unprocessed"
    ]
    if args.area:
        records = [item for item in records if area_matches(item, args.area)]
    print(json.dumps({"runs": records, "coordinator": str(paths["coordinator"])}, indent=2))
    return 0


def cmd_analysis_claim(args: argparse.Namespace) -> int:
    repo = repo_root(args.repo)
    base_ref, revision, remote = fetch_assessment_revision(repo, args.base_ref)
    with locked_registry(repo) as (registry, paths):
        selected: list[dict[str, Any]] = []
        if args.run:
            for run_id in args.run:
                record = find_run(registry, run_id)
                if record.get("status") != "completed":
                    raise CoordinatorError(f"profile run {run_id} has no completed report")
                if record.get("analysis_status") == "claimed":
                    raise OwnershipConflict(
                        f"profile run {run_id} is claimed by {record.get('analysis_id')}"
                    )
                if record.get("analysis_status") == "analyzed":
                    raise CoordinatorError(f"profile run {run_id} was already analyzed")
                selected.append(record)
        else:
            selected = [
                record
                for record in registry["runs"]
                if record.get("status") == "completed"
                and record.get("analysis_status", "unprocessed") == "unprocessed"
                and (not args.area or area_matches(record, args.area))
            ]
        unique = {record["run_id"]: record for record in selected}
        selected = list(unique.values())
        if not selected:
            raise CoordinatorError("no matching unprocessed profile reports are available")
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        assessment_id = f"{stamp}-performance-{secrets.token_hex(3)}"
        record = {
            "assessment_id": assessment_id,
            "status": "claimed",
            "outcome": "pending",
            "claimed_at": now_iso(),
            "heartbeat_at": now_iso(),
            "base_ref": base_ref,
            "assessment_revision": revision,
            "assessment_revision_subject": run_git(
                repo, "show", "-s", "--format=%s", revision
            ).stdout.strip(),
            "assessment_revision_committed_at": run_git(
                repo, "show", "-s", "--format=%cI", revision
            ).stdout.strip(),
            "fetch_remote": remote,
            "run_ids": [item["run_id"] for item in selected],
            "source_reports": [item["report_path"] for item in selected],
            "worktree_path": str(paths["analysis_worktrees"] / assessment_id),
            "assessment_path": str(
                paths["assessments"] / f"{assessment_id}.performance-assessment.md"
            ),
        }
        registry["assessments"].append(record)
        for item in selected:
            item["analysis_status"] = "claimed"
            item["analysis_id"] = assessment_id
    print(json.dumps(record, indent=2, sort_keys=True))
    return 0


def cmd_analysis_worktree(args: argparse.Namespace) -> int:
    repo = repo_root(args.repo)
    paths = coordinator_paths(repo)
    with file_lock(paths["analysis_lock"]):
        registry, _paths = read_registry(repo)
        original = find_assessment(registry, args.assessment_id)
        if original.get("status") != "claimed":
            raise CoordinatorError(
                f"analysis worktree creation requires status 'claimed', got {original.get('status')!r}"
            )
        target = Path(original["worktree_path"])
        if target.exists():
            raise CoordinatorError(f"analysis worktree path already exists: {target}")
        created = run_git(
            repo,
            "worktree",
            "add",
            "--detach",
            str(target),
            original["assessment_revision"],
            check=False,
        )
        if created.returncode != 0:
            raise CoordinatorError(created.stderr.strip() or created.stdout.strip())
        record = assessment_update(
            repo,
            args.assessment_id,
            lambda item: item.update({"status": "worktree-ready", "worktree_created_at": now_iso()}),
        )
    print(json.dumps(record, indent=2, sort_keys=True))
    return 0


def fill_assessment_template(
    template: str, assessment: dict[str, Any], runs: list[dict[str, Any]]
) -> str:
    dispositions = "\n".join(
        f"- `{record['run_id']}` — <REPLACE: state how this source is used or why it is non-actionable>"
        for record in runs
    )
    report_links = "\n".join(
        f"- `{record['run_id']}`: [{record['profile_id']}](<{record['report_path']}>)"
        for record in runs
    )
    replacements = {
        "{{schema_json}}": json.dumps(ASSESSMENT_SCHEMA),
        "{{assessment_id_json}}": json.dumps(assessment["assessment_id"]),
        "{{base_ref_json}}": json.dumps(assessment["base_ref"]),
        "{{revision_json}}": json.dumps(assessment["assessment_revision"]),
        "{{revision_subject_json}}": json.dumps(assessment["assessment_revision_subject"]),
        "{{revision_committed_at_json}}": json.dumps(
            assessment["assessment_revision_committed_at"]
        ),
        "{{claimed_at_json}}": json.dumps(assessment["claimed_at"]),
        "{{source_run_count}}": str(len(runs)),
        "{{source_run_ids_json}}": json.dumps([record["run_id"] for record in runs]),
        "{{source_dispositions}}": dispositions,
        "{{source_reports}}": report_links,
    }
    for marker, value in replacements.items():
        template = template.replace(marker, value)
    return template


def cmd_assessment_init(args: argparse.Namespace) -> int:
    repo = repo_root(args.repo)
    registry, _paths = read_registry(repo)
    original = find_assessment(registry, args.assessment_id)
    if original.get("status") not in {"worktree-ready", "awaiting-assessment", "completed"}:
        raise CoordinatorError(
            f"assessment initialization requires a ready worktree, got {original.get('status')!r}"
        )
    destination = Path(original["assessment_path"])
    if not destination.exists():
        runs = [find_run(registry, run_id) for run_id in original["run_ids"]]
        template_path = performance_template_path()
        if not template_path.exists():
            raise CoordinatorError(f"paired $performance template is unavailable: {template_path}")
        template = template_path.read_text(encoding="utf-8")
        atomic_write(destination, fill_assessment_template(template, original, runs))
    if original.get("status") == "worktree-ready":
        assessment_update(
            repo,
            args.assessment_id,
            lambda item: item.update({"status": "awaiting-assessment"}),
        )
    print(destination)
    return 0


def validate_assessment(
    path: Path, assessment: dict[str, Any], outcome: str
) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"cannot read assessment: {exc}"]
    required = [
        f'schema: "{ASSESSMENT_SCHEMA}"',
        f'assessment_id: {json.dumps(assessment["assessment_id"])}',
        f'base_ref: {json.dumps(assessment["base_ref"])}',
        f'assessment_revision: {json.dumps(assessment["assessment_revision"])}',
        f'assessment_outcome: "{outcome}"',
        "## Source profile reports",
        "## Source disposition",
        "## Evidence quality and comparability",
        "## Current-code verification",
        "## Decision now",
        "## Findings and recommendations",
        "## Follow-up profiling",
        "## Boundaries",
    ]
    errors = [f"missing {marker!r}" for marker in required if marker not in text]
    if "<REPLACE:" in text:
        errors.append("assessment still contains <REPLACE: ...> markers")
    if "{{" in text or "}}" in text:
        errors.append("assessment still contains an unresolved template marker")
    for run_id in assessment["run_ids"]:
        marker = f"- `{run_id}` — "
        if text.count(marker) != 1:
            errors.append(f"source disposition must contain {run_id!r} exactly once")
    findings = re.findall(r"^### (PFN-\d{3}) — .+$", text, re.MULTILINE)
    expected = [f"PFN-{index:03d}" for index in range(1, len(findings) + 1)]
    if findings != expected:
        errors.append("finding IDs must be unique and sequential from PFN-001")
    if outcome == "recommendations" and not findings:
        errors.append("outcome 'recommendations' requires at least one PFN-nnn finding")
    return errors


def cmd_analysis_complete(args: argparse.Namespace) -> int:
    repo = repo_root(args.repo)
    with locked_registry(repo) as (registry, _paths):
        original = find_assessment(registry, args.assessment_id)
        if original.get("status") == "completed":
            if original.get("outcome") != args.outcome:
                raise CoordinatorError("a completed performance assessment outcome cannot change")
            print(json.dumps(original, indent=2, sort_keys=True))
            return 0
        if original.get("status") != "awaiting-assessment":
            raise CoordinatorError(
                "assessment completion requires status 'awaiting-assessment', "
                f"got {original.get('status')!r}"
            )
        path = Path(original["assessment_path"])
        errors = validate_assessment(path, original, args.outcome)
        if errors:
            print("Performance assessment validation failed:", file=sys.stderr)
            for error in errors:
                print(f"  - {error}", file=sys.stderr)
            return 5
        completed_at = now_iso()
        original.update(
            {"status": "completed", "outcome": args.outcome, "completed_at": completed_at}
        )
        for run_id in original["run_ids"]:
            run = find_run(registry, run_id)
            if run.get("analysis_id") != args.assessment_id:
                raise CoordinatorError(f"profile run {run_id} is no longer owned by this assessment")
            run.update(
                {
                    "analysis_status": "analyzed",
                    "analyzed_at": completed_at,
                    "analysis_path": original["assessment_path"],
                }
            )
        record = json.loads(json.dumps(original))
    print(json.dumps(record, indent=2, sort_keys=True))
    return 0


def cmd_analysis_release(args: argparse.Namespace) -> int:
    repo = repo_root(args.repo)
    with locked_registry(repo) as (registry, _paths):
        original = find_assessment(registry, args.assessment_id)
        if original.get("status") not in ACTIVE_ASSESSMENT_STATUSES:
            raise CoordinatorError(
                f"release requires an active assessment, got {original.get('status')!r}"
            )
        for run_id in original["run_ids"]:
            run = find_run(registry, run_id)
            if run.get("analysis_id") == args.assessment_id:
                run["analysis_status"] = "unprocessed"
                run.pop("analysis_id", None)
        original.update(
            {"status": "released", "release_reason": args.reason, "released_at": now_iso()}
        )
        record = json.loads(json.dumps(original))
    print(json.dumps(record, indent=2, sort_keys=True))
    return 0


def cmd_analysis_cleanup(args: argparse.Namespace) -> int:
    repo = repo_root(args.repo)
    paths = coordinator_paths(repo)
    with file_lock(paths["analysis_lock"]):
        registry, _paths = read_registry(repo)
        original = find_assessment(registry, args.assessment_id)
        if original.get("status") not in {"completed", "released"}:
            raise CoordinatorError(
                f"cleanup requires a completed or released assessment, got {original.get('status')!r}"
            )
        target = Path(original["worktree_path"])
        if target.exists():
            removed = run_git(repo, "worktree", "remove", str(target), check=False)
            if removed.returncode != 0:
                print("Analysis worktree preserved because normal removal refused it:", file=sys.stderr)
                print(removed.stderr or removed.stdout, file=sys.stderr)
                return 4
        assessment_update(
            repo,
            args.assessment_id,
            lambda item: item.update({"worktree_removed_at": now_iso()}),
        )
    print(f"Removed analysis worktree {target}")
    return 0


def cmd_delivery_list(args: argparse.Namespace) -> int:
    repo = repo_root(args.repo)
    registry, paths = read_registry(repo)
    records = list(registry["deliveries"])
    if args.active:
        records = [item for item in records if item.get("status") in ACTIVE_DELIVERY_STATUSES]
    records.sort(key=lambda item: item.get("claimed_at", ""), reverse=True)
    print(json.dumps({"deliveries": records, "coordinator": str(paths["coordinator"])}, indent=2))
    return 0


def cmd_delivery_show(args: argparse.Namespace) -> int:
    repo = repo_root(args.repo)
    registry, _paths = read_registry(repo)
    print(json.dumps(find_delivery(registry, args.delivery_id), indent=2, sort_keys=True))
    return 0


def cmd_delivery_claim(args: argparse.Namespace) -> int:
    repo = repo_root(args.repo)
    if not re.fullmatch(r"PFN-\d{3}", args.finding_id):
        raise CoordinatorError("delivery finding id must have the form PFN-nnn")
    branch = args.branch.strip()
    if not branch or branch in {LAB_BRANCH, "docs-wip", "master", "main"}:
        raise CoordinatorError(f"refusing reserved delivery branch {branch!r}")
    paths = coordinator_paths(repo)
    target = Path(args.worktree_path).expanduser().resolve()
    if target in {canonical_checkout(repo), paths["lab_worktree"].resolve()}:
        raise CoordinatorError("delivery worktree must be isolated from the primary and profile lab")
    base_ref, base_revision, remote = fetch_assessment_revision(repo, args.base_ref)
    with locked_registry(repo) as (registry, _paths):
        assessment = find_assessment(registry, args.assessment_id)
        if assessment.get("status") != "completed" or assessment.get("outcome") != "recommendations":
            raise CoordinatorError(
                "delivery requires a completed assessment with outcome 'recommendations'"
            )
        assessment_path = Path(assessment["assessment_path"])
        try:
            assessment_text = assessment_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise CoordinatorError(f"cannot read delivery assessment: {exc}") from exc
        if not re.search(rf"^### {re.escape(args.finding_id)} — .+$", assessment_text, re.MULTILINE):
            raise CoordinatorError(
                f"assessment {args.assessment_id} has no finding {args.finding_id}"
            )
        duplicates = [
            item
            for item in registry["deliveries"]
            if item.get("assessment_id") == args.assessment_id
            and item.get("finding_id") == args.finding_id
        ]
        if duplicates:
            existing = duplicates[-1]
            if existing.get("status") in ACTIVE_DELIVERY_STATUSES:
                raise OwnershipConflict(
                    f"delivery is owned by {existing.get('delivery_id')} "
                    f"({existing.get('status')})"
                )
            raise CoordinatorError(
                f"delivery {existing.get('delivery_id')} is already terminal "
                f"({existing.get('status')})"
            )
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        delivery_id = (
            f"{stamp}-delivery-{slug(args.assessment_id, 24)}-"
            f"{args.finding_id.lower()}-{secrets.token_hex(3)}"
        )
        record = {
            "delivery_id": delivery_id,
            "status": "claimed",
            "claimed_at": now_iso(),
            "heartbeat_at": now_iso(),
            "assessment_id": args.assessment_id,
            "finding_id": args.finding_id,
            "base_ref": base_ref,
            "base_revision": base_revision,
            "fetch_remote": remote,
            "branch": branch,
            "worktree_path": str(target),
            "candidate_revision": None,
            "validation_run_ids": [],
            "validation_assessment_id": None,
            "validation_decision": "pending",
            "pr_url": None,
        }
        registry["deliveries"].append(record)
    print(json.dumps(record, indent=2, sort_keys=True))
    return 0


def cmd_delivery_worktree(args: argparse.Namespace) -> int:
    repo = repo_root(args.repo)
    paths = coordinator_paths(repo)
    with file_lock(paths["delivery_lock"]):
        registry, _paths = read_registry(repo)
        original = find_delivery(registry, args.delivery_id)
        if original.get("status") != "claimed":
            raise CoordinatorError(
                f"delivery worktree requires status 'claimed', got {original.get('status')!r}"
            )
        target = Path(original["worktree_path"])
        if args.adopt:
            validate_existing_worktree(repo, target)
            if worktree_branch(target) != original["branch"]:
                raise CoordinatorError("adopted worktree is not on the claimed delivery branch")
            head = resolve_commit(target, "HEAD", "adopted delivery worktree")
            if head != original["base_revision"]:
                raise CoordinatorError(
                    "adopted worktree HEAD must equal the claimed base revision before delivery"
                )
            dirty = bool(run_git(target, "status", "--porcelain").stdout.strip())
            adopted = True
        else:
            if target.exists():
                raise CoordinatorError(f"delivery worktree path already exists: {target}")
            created = run_git(
                repo,
                "worktree",
                "add",
                "-b",
                original["branch"],
                str(target),
                original["base_revision"],
                check=False,
            )
            if created.returncode != 0:
                raise CoordinatorError(created.stderr.strip() or created.stdout.strip())
            head = original["base_revision"]
            dirty = False
            adopted = False

        def mark_ready(item: dict[str, Any]) -> None:
            if item.get("status") != "claimed":
                raise CoordinatorError("delivery state changed while preparing its worktree")
            item.update(
                {
                    "status": "worktree-ready",
                    "worktree_ready_at": now_iso(),
                    "worktree_adopted": adopted,
                    "worktree_initial_head": head,
                    "worktree_initially_dirty": dirty,
                }
            )

        record = delivery_update(repo, args.delivery_id, mark_ready)
    print(json.dumps(record, indent=2, sort_keys=True))
    return 0


def cmd_delivery_transition(args: argparse.Namespace) -> int:
    repo = repo_root(args.repo)
    registry, _paths = read_registry(repo)
    original = find_delivery(registry, args.delivery_id)
    current = original.get("status")
    requested = args.status
    if requested not in DELIVERY_STATUSES:
        raise CoordinatorError(f"unknown delivery status {requested!r}")
    if requested not in DELIVERY_TRANSITIONS.get(current, set()):
        raise CoordinatorError(f"invalid delivery transition {current!r} -> {requested!r}")

    updates: dict[str, Any] = {"status": requested, f"{requested.replace('-', '_')}_at": now_iso()}
    if requested == "candidate-ready":
        if not args.candidate_revision:
            raise CoordinatorError("candidate-ready requires --candidate-revision")
        candidate = resolve_commit(repo, args.candidate_revision, "delivery candidate")
        target = Path(original["worktree_path"])
        validate_existing_worktree(repo, target)
        require_clean_worktree(target, "delivery candidate")
        if worktree_branch(target) != original["branch"]:
            raise CoordinatorError("delivery candidate worktree left its claimed branch")
        if resolve_commit(target, "HEAD", "delivery candidate worktree") != candidate:
            raise CoordinatorError("candidate revision is not the delivery worktree HEAD")
        ancestor = run_git(
            repo, "merge-base", "--is-ancestor", original["base_revision"], candidate, check=False
        )
        if ancestor.returncode != 0:
            raise CoordinatorError("delivery candidate does not descend from its claimed base")
        updates.update(
            {
                "candidate_revision": candidate,
                "validation_run_ids": [],
                "validation_assessment_id": None,
                "validation_decision": "pending",
            }
        )
    elif requested == "validating":
        if not original.get("candidate_revision"):
            raise CoordinatorError("validation requires a recorded candidate revision")
        validation_runs = list(original.get("validation_run_ids", []))
        for run_id in args.validation_run or []:
            run = find_run(registry, run_id)
            if run.get("status") != "completed":
                raise CoordinatorError(f"validation run {run_id} is not completed")
            if run.get("revision") != original["candidate_revision"]:
                raise CoordinatorError(f"validation run {run_id} measured another revision")
            if run_id not in validation_runs:
                validation_runs.append(run_id)
        updates["validation_run_ids"] = validation_runs
    elif requested == "validated":
        if args.validation_decision != "accepted":
            raise CoordinatorError("validated requires --validation-decision accepted")
        if not args.validation_assessment_id:
            raise CoordinatorError("validated requires --validation-assessment-id")
        if not original.get("validation_run_ids"):
            raise CoordinatorError("validated delivery has no recorded validation runs")
        assessment = find_assessment(registry, args.validation_assessment_id)
        if assessment.get("status") != "completed":
            raise CoordinatorError("validation assessment is not completed")
        missing = set(original["validation_run_ids"]) - set(assessment.get("run_ids", []))
        if missing:
            raise CoordinatorError(
                "validation assessment does not own recorded runs: " + ", ".join(sorted(missing))
            )
        updates.update(
            {
                "validation_assessment_id": args.validation_assessment_id,
                "validation_decision": "accepted",
            }
        )
    elif requested == "pr-open":
        if not args.pr_url or not re.match(r"^https://", args.pr_url):
            raise CoordinatorError("pr-open requires an https --pr-url")
        updates["pr_url"] = args.pr_url
    elif requested == "stopped":
        if not args.reason:
            raise CoordinatorError("stopped requires --reason")
        updates["stop_reason"] = args.reason

    def transition(item: dict[str, Any]) -> None:
        if item.get("status") != current:
            raise CoordinatorError("delivery state changed during transition")
        item.update(updates)

    record = delivery_update(repo, args.delivery_id, transition)
    print(json.dumps(record, indent=2, sort_keys=True))
    return 0


def add_repo_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", default=".", help="any path inside the target Git repository")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Atomic coordinator for the paired Codex $profile/$performance skills"
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)

    init = sub.add_parser("init", help="initialize and show local lab paths")
    add_repo_arg(init)
    init.set_defaults(func=cmd_init)

    refresh = sub.add_parser("refresh", help="fast-forward the persistent lab and snapshot a ref")
    add_repo_arg(refresh)
    refresh.add_argument("--base-ref")
    refresh.add_argument("--target-ref")
    refresh.add_argument("--stale-seconds", type=int, default=DEFAULT_STALE_SECONDS)
    refresh.set_defaults(func=cmd_refresh)

    listing = sub.add_parser("list", help="list coordinated profile runs")
    add_repo_arg(listing)
    listing.add_argument("--active", action="store_true")
    listing.add_argument("--unprocessed", action="store_true")
    listing.add_argument("--area")
    listing.add_argument("--limit", type=int)
    listing.add_argument("--json", action="store_true")
    listing.set_defaults(func=cmd_list)

    show = sub.add_parser("show", help="show one profile run")
    add_repo_arg(show)
    show.add_argument("--run-id", required=True)
    show.set_defaults(func=cmd_show)

    claim = sub.add_parser("claim", help="claim one targeted performance question")
    add_repo_arg(claim)
    claim.add_argument("--snapshot-id", required=True)
    claim.add_argument("--profile-id", required=True)
    claim.add_argument("--area", required=True)
    claim.add_argument("--workload-id", required=True)
    claim.add_argument("--metric-family", required=True)
    claim.add_argument("--instrument", required=True)
    claim.add_argument("--question", required=True)
    claim.add_argument("--rationale", required=True)
    claim.add_argument("--comparison-key", required=True)
    claim.add_argument("--stale-seconds", type=int, default=DEFAULT_STALE_SECONDS)
    claim.set_defaults(func=cmd_claim)

    prepare = sub.add_parser("prepare", help="detach the persistent lab at the claimed revision")
    add_repo_arg(prepare)
    prepare.add_argument("--run-id", required=True)
    prepare.set_defaults(func=cmd_prepare)

    execute = sub.add_parser("exec", help="run and log one build, warm-up, or measurement command")
    add_repo_arg(execute)
    execute.add_argument("--run-id", required=True)
    execute.add_argument("--phase", required=True)
    execute.add_argument("--timeout", type=float)
    execute.add_argument("--final", action="store_true")
    execute.add_argument("command", nargs=argparse.REMAINDER)
    execute.set_defaults(func=cmd_exec)

    block = sub.add_parser("block", help="end setup with a structured blocked result")
    add_repo_arg(block)
    block.add_argument("--run-id", required=True)
    block.add_argument("--reason", required=True)
    block.set_defaults(func=cmd_block)

    heartbeat = sub.add_parser("heartbeat", help="refresh an active profile claim")
    add_repo_arg(heartbeat)
    heartbeat.add_argument("--run-id", required=True)
    heartbeat.set_defaults(func=cmd_heartbeat)

    report_init = sub.add_parser("report-init", help="create the profile report skeleton")
    add_repo_arg(report_init)
    report_init.add_argument("--run-id", required=True)
    report_init.set_defaults(func=cmd_report_init)

    report = sub.add_parser("report", help="validate and attach a profile report")
    add_repo_arg(report)
    report.add_argument("--run-id", required=True)
    report.add_argument("--outcome", required=True, choices=RUN_OUTCOMES)
    report.set_defaults(func=cmd_report)

    restore = sub.add_parser("restore", help="reattach the persistent worktree to profile-lab")
    add_repo_arg(restore)
    restore.add_argument("--run-id", required=True)
    restore.set_defaults(func=cmd_restore)

    analysis_list = sub.add_parser("analysis-list", help="list analysis input or assessments")
    add_repo_arg(analysis_list)
    analysis_list.add_argument("--area")
    analysis_list.add_argument("--assessments", action="store_true")
    analysis_list.add_argument("--active", action="store_true")
    analysis_list.set_defaults(func=cmd_analysis_list)

    analysis_claim = sub.add_parser("analysis-claim", help="claim unprocessed profile reports")
    add_repo_arg(analysis_claim)
    analysis_claim.add_argument("--run", action="append")
    analysis_claim.add_argument("--area")
    analysis_claim.add_argument("--base-ref")
    analysis_claim.set_defaults(func=cmd_analysis_claim)

    analysis_worktree = sub.add_parser(
        "analysis-worktree", help="create an immutable current-code assessment worktree"
    )
    add_repo_arg(analysis_worktree)
    analysis_worktree.add_argument("--assessment-id", required=True)
    analysis_worktree.set_defaults(func=cmd_analysis_worktree)

    assessment_init = sub.add_parser("assessment-init", help="create an assessment skeleton")
    add_repo_arg(assessment_init)
    assessment_init.add_argument("--assessment-id", required=True)
    assessment_init.set_defaults(func=cmd_assessment_init)

    analysis_complete = sub.add_parser(
        "analysis-complete", help="validate an assessment and consume its source reports"
    )
    add_repo_arg(analysis_complete)
    analysis_complete.add_argument("--assessment-id", required=True)
    analysis_complete.add_argument("--outcome", required=True, choices=ASSESSMENT_OUTCOMES)
    analysis_complete.set_defaults(func=cmd_analysis_complete)

    analysis_release = sub.add_parser("analysis-release", help="release claimed source reports")
    add_repo_arg(analysis_release)
    analysis_release.add_argument("--assessment-id", required=True)
    analysis_release.add_argument("--reason", required=True)
    analysis_release.set_defaults(func=cmd_analysis_release)

    analysis_cleanup = sub.add_parser("analysis-cleanup", help="remove a terminal analysis worktree")
    add_repo_arg(analysis_cleanup)
    analysis_cleanup.add_argument("--assessment-id", required=True)
    analysis_cleanup.set_defaults(func=cmd_analysis_cleanup)

    delivery_list = sub.add_parser("delivery-list", help="list performance deliveries")
    add_repo_arg(delivery_list)
    delivery_list.add_argument("--active", action="store_true")
    delivery_list.set_defaults(func=cmd_delivery_list)

    delivery_show = sub.add_parser("delivery-show", help="show one performance delivery")
    add_repo_arg(delivery_show)
    delivery_show.add_argument("--delivery-id", required=True)
    delivery_show.set_defaults(func=cmd_delivery_show)

    delivery_claim = sub.add_parser(
        "delivery-claim", help="claim one accepted performance finding for delivery"
    )
    add_repo_arg(delivery_claim)
    delivery_claim.add_argument("--assessment-id", required=True)
    delivery_claim.add_argument("--finding-id", required=True)
    delivery_claim.add_argument("--branch", required=True)
    delivery_claim.add_argument("--worktree-path", required=True)
    delivery_claim.add_argument("--base-ref")
    delivery_claim.set_defaults(func=cmd_delivery_claim)

    delivery_worktree = sub.add_parser(
        "delivery-worktree", help="create or adopt the claimed implementation worktree"
    )
    add_repo_arg(delivery_worktree)
    delivery_worktree.add_argument("--delivery-id", required=True)
    delivery_worktree.add_argument("--adopt", action="store_true")
    delivery_worktree.set_defaults(func=cmd_delivery_worktree)

    delivery_transition = sub.add_parser(
        "delivery-update", help="advance a claimed performance delivery"
    )
    add_repo_arg(delivery_transition)
    delivery_transition.add_argument("--delivery-id", required=True)
    delivery_transition.add_argument("--status", required=True, choices=sorted(DELIVERY_STATUSES))
    delivery_transition.add_argument("--candidate-revision")
    delivery_transition.add_argument("--validation-run", action="append")
    delivery_transition.add_argument("--validation-assessment-id")
    delivery_transition.add_argument("--validation-decision", choices=("accepted",))
    delivery_transition.add_argument("--pr-url")
    delivery_transition.add_argument("--reason")
    delivery_transition.set_defaults(func=cmd_delivery_transition)
    return parser


def main() -> int:
    try:
        args = build_parser().parse_args()
        return args.func(args)
    except OwnershipConflict as exc:
        print(f"profile coordinator: {exc}", file=sys.stderr)
        return 3
    except (CoordinatorError, subprocess.CalledProcessError) as exc:
        print(f"profile coordinator: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
