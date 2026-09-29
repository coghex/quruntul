#!/usr/bin/env python3
"""Atomic planning coordination for accepted $test proposals.

The $test coordinator remains the source of proposal decisions. This helper
claims accepted proposals for $assess-tests, prevents duplicate design docs,
and records the exact completed document without modifying the source proposal
registry.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile
from typing import Any, Callable


SCHEMA = "codex-test-proposal-design-coordinator/v1"
TEST_SCHEMA = "codex-test-coordinator/v1"
ACTIVE_STATUSES = {"claimed"}
COMPLETED_STATUSES = {"completed", "already-implemented", "superseded"}
TERMINAL_STATUSES = COMPLETED_STATUSES | {"cancelled", "abandoned", "blocked"}
RESOLUTION_STATUSES = {"already-implemented", "superseded"}
DEFAULT_STALE_SECONDS = 7 * 24 * 60 * 60
REQUIRED_HEADINGS = (
    "## Purpose and coverage gap",
    "## Binding decisions",
    "## Current repository evidence",
    "## Scenario and boundaries",
    "## Oracle",
    "## Apparatus and integration",
    "## Reliability and cost",
    "## Implementation plan",
    "## Validation and handoff",
    "## Open questions",
)
REQUIRED_PROPOSAL_FIELDS = (
    "proposal_id",
    "test_id",
    "area",
    "title",
    "base_ref",
    "revision",
    "decided_at",
    "recommended_tier",
    "decision_note",
    "coverage_gap",
    "priority_rationale",
    "scenario",
    "oracle",
    "apparatus",
    "cost_and_risks",
)


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


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


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


def paths_for(repo: Path) -> dict[str, Path]:
    test_root = common_git_dir(repo) / "codex-test"
    root = test_root / "assessments" / "proposal-designs"
    return {
        "test_registry": test_root / "registry.json",
        "root": root,
        "registry": root / "registry.json",
        "lock": root / "registry.lock",
    }


def empty_registry() -> dict[str, Any]:
    return {"schema": SCHEMA, "updated_at": now_iso(), "designs": []}


@contextlib.contextmanager
def locked_registry(repo: Path):
    paths = paths_for(repo)
    paths["root"].mkdir(parents=True, exist_ok=True)
    with paths["lock"].open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if paths["registry"].exists():
            try:
                registry = json.loads(paths["registry"].read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise CoordinatorError(f"cannot read proposal-design registry: {exc}") from exc
            if registry.get("schema") != SCHEMA or not isinstance(registry.get("designs"), list):
                raise CoordinatorError(f"unsupported registry schema in {paths['registry']}")
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


def read_registry(repo: Path) -> tuple[dict[str, Any], dict[str, Path]]:
    with locked_registry(repo) as (registry, paths):
        return json.loads(json.dumps(registry)), paths


def find_design(registry: dict[str, Any], design_id: str) -> dict[str, Any]:
    for record in registry["designs"]:
        if record.get("design_id") == design_id:
            return record
    raise CoordinatorError(f"unknown design id {design_id!r}")


def update_record(repo: Path, design_id: str, update: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    with locked_registry(repo) as (registry, _paths):
        record = find_design(registry, design_id)
        update(record)
        record["heartbeat_at"] = now_iso()
        return json.loads(json.dumps(record))


def proposal_sha(proposal: dict[str, Any]) -> str:
    return sha256_text(json.dumps(proposal, sort_keys=True, separators=(",", ":")))


def load_accepted_proposals(repo: Path) -> list[dict[str, Any]]:
    paths = paths_for(repo)
    if not paths["test_registry"].is_file():
        raise CoordinatorError(f"no local coordinated-test registry at {paths['test_registry']}")
    try:
        registry = json.loads(paths["test_registry"].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CoordinatorError(f"cannot read local coordinated-test registry: {exc}") from exc
    if registry.get("schema") != TEST_SCHEMA or not isinstance(registry.get("proposals"), list):
        raise CoordinatorError("unsupported local coordinated-test proposal registry schema")
    accepted = []
    for proposal in registry["proposals"]:
        if proposal.get("status") != "accepted":
            continue
        missing = [field for field in REQUIRED_PROPOSAL_FIELDS if not str(proposal.get(field, "")).strip()]
        if missing:
            raise CoordinatorError(
                f"accepted proposal {proposal.get('proposal_id')!r} is missing required fields: "
                + ", ".join(missing)
            )
        proposal = json.loads(json.dumps(proposal))
        proposal["source_proposal_sha256"] = proposal_sha(proposal)
        accepted.append(proposal)
    return accepted


def design_states(registry: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    active: dict[str, dict[str, Any]] = {}
    completed: dict[str, dict[str, Any]] = {}
    for record in registry["designs"]:
        proposal_id = record.get("proposal_id")
        if not proposal_id:
            continue
        if record.get("status") in ACTIVE_STATUSES:
            active[proposal_id] = record
        elif record.get("status") in COMPLETED_STATUSES:
            completed[proposal_id] = record
    return active, completed


def reap_stale(registry: dict[str, Any], stale_seconds: int) -> None:
    now = dt.datetime.now(dt.timezone.utc)
    for record in registry["designs"]:
        if record.get("status") not in ACTIVE_STATUSES:
            continue
        stamp = parse_iso(record.get("heartbeat_at") or record.get("claimed_at"))
        if stamp and (now - stamp).total_seconds() > stale_seconds:
            record.update({
                "status": "abandoned",
                "completed_at": now_iso(),
                "abandon_reason": f"stale claim exceeded {stale_seconds}s",
            })


def default_base_ref(repo: Path) -> str:
    upstream = run_git(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}", check=False)
    if upstream.returncode == 0 and upstream.stdout.strip():
        return upstream.stdout.strip()
    remote_head = run_git(repo, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD", check=False)
    if remote_head.returncode == 0 and remote_head.stdout.strip():
        return remote_head.stdout.strip()
    for candidate in ("origin/master", "origin/main", "HEAD"):
        if run_git(repo, "rev-parse", "--verify", f"{candidate}^{{commit}}", check=False).returncode == 0:
            return candidate
    raise CoordinatorError("cannot resolve a proposal-design base ref")


def snapshot(repo: Path, base_ref: str) -> dict[str, str]:
    resolved = run_git(repo, "rev-parse", "--verify", f"{base_ref}^{{commit}}", check=False)
    if resolved.returncode != 0:
        raise CoordinatorError(f"cannot resolve proposal-design base ref {base_ref!r}")
    revision = resolved.stdout.strip()
    return {
        "base_ref": base_ref,
        "revision": revision,
        "revision_subject": run_git(repo, "show", "-s", "--format=%s", revision).stdout.strip(),
        "revision_committed_at": run_git(repo, "show", "-s", "--format=%cI", revision).stdout.strip(),
        "base_resolved_at": now_iso(),
    }


def slug(text: str) -> str:
    return (re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "test-proposal")[:48]


def selected_proposals(args: argparse.Namespace, proposals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not args.proposal and not args.area:
        return proposals
    areas = [value.casefold() for value in args.area]
    selected = []
    for proposal in proposals:
        haystack = " ".join(
            str(proposal.get(key, "")) for key in ("area", "test_id", "title")
        ).casefold()
        if proposal.get("proposal_id") in args.proposal or any(area in haystack for area in areas):
            selected.append(proposal)
    return selected


def resolve_docs_worktree(repo: Path) -> Path:
    output = run_git(repo, "worktree", "list", "--porcelain").stdout
    current: Path | None = None
    for line in output.splitlines():
        if line.startswith("worktree "):
            current = Path(line[len("worktree "):]).resolve()
        elif line == "branch refs/heads/docs-wip" and current is not None:
            return current
    return repo


def design_root(repo: Path) -> Path:
    worktree = resolve_docs_worktree(repo)
    docs = worktree / "docs"
    return (docs if docs.is_dir() else worktree) / "test_designs"


def validate_document(repo: Path, record: dict[str, Any], path: Path) -> tuple[list[str], str]:
    errors: list[str] = []
    resolved = path.expanduser().resolve()
    root = design_root(repo).resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        errors.append(f"document must live under {root}")
    if resolved.suffix != ".md":
        errors.append("document must be Markdown")
    try:
        text = resolved.read_text(encoding="utf-8")
    except OSError as exc:
        return [*errors, f"cannot read document: {exc}"], ""
    if f"Source proposal: `{record['proposal_id']}`" not in text:
        errors.append("document does not name its exact source proposal")
    if f"Source test ID: `{record['test_id']}`" not in text:
        errors.append("document does not name its exact source test ID")
    source = record["source_proposal"]
    source_line = f"Source ref and commit: `{source['base_ref']}` at `{source['revision']}`"
    if source_line not in text:
        errors.append("document does not preserve the proposal source ref and commit")
    designed_line = f"Designed against: `{record['base_ref']}` at `{record['revision']}`"
    if designed_line not in text:
        errors.append("document does not preserve the design ref and commit")
    if f"Accepted at: `{source['decided_at']}`" not in text:
        errors.append("document does not preserve the proposal decision time")
    if not re.search(r"^Test design state: `(exploring|ready for implementation)`$", text, re.MULTILINE):
        errors.append("document has no valid Test design state")
    for heading in REQUIRED_HEADINGS:
        if heading not in text:
            errors.append(f"document is missing {heading!r}")
    if "<REPLACE:" in text:
        errors.append("document still contains <REPLACE: ...> markers")
    if "Implementation authorization: `not granted`" not in text:
        errors.append("document must preserve the implementation authorization boundary")
    decision_note = str(source.get("decision_note", "")).strip()
    binding = re.search(
        r"^## Binding decisions\n(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL
    )
    if decision_note and (binding is None or decision_note not in binding.group(1)):
        errors.append(
            "document must preserve the accepted proposal decision note verbatim "
            "inside Binding decisions"
        )
    tier = str(source.get("recommended_tier", "")).strip()
    if tier and f"Recommended tier: `{tier}`" not in text:
        errors.append("document must preserve the proposal's recommended tier")
    return errors, sha256_text(text)


def cmd_init(args: argparse.Namespace) -> int:
    repo = repo_root(args.repo)
    with locked_registry(repo) as (_registry, paths):
        result = {key: str(value) for key, value in paths.items()}
        result["docs_worktree"] = str(resolve_docs_worktree(repo))
        result["design_root"] = str(design_root(repo))
        print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    repo = repo_root(args.repo)
    proposals = load_accepted_proposals(repo)
    with locked_registry(repo) as (registry, paths):
        reap_stale(registry, args.stale_seconds)
        records = json.loads(json.dumps(registry["designs"]))
        active, completed = design_states(registry)
    if args.active:
        records = [record for record in records if record.get("status") in ACTIVE_STATUSES]
    records.sort(key=lambda record: record.get("claimed_at", ""), reverse=True)
    if args.limit:
        records = records[:args.limit]
    available = [
        proposal for proposal in proposals
        if proposal["proposal_id"] not in active and proposal["proposal_id"] not in completed
    ]
    if args.json:
        print(json.dumps({
            "available": available,
            "designs": records,
            "registry": str(paths["registry"]),
            "design_root": str(design_root(repo)),
        }, indent=2, sort_keys=True))
        return 0
    print(f"Available accepted proposals: {len(available)}")
    for proposal in available:
        print(f"  {proposal['proposal_id']}  [{proposal.get('area', '')}]  {proposal.get('test_id', '')}")
    if records:
        print("\nProposal designs:")
        for record in records:
            print(f"  {record['design_id']}  {record['status']}  {record['proposal_id']}")
    else:
        print("No proposal-design records.")
    print(f"\nRegistry: {paths['registry']}")
    return 0


def cmd_claim(args: argparse.Namespace) -> int:
    repo = repo_root(args.repo)
    selected = selected_proposals(args, load_accepted_proposals(repo))
    explicit = bool(args.proposal or args.area)
    with locked_registry(repo) as (registry, _paths):
        reap_stale(registry, args.stale_seconds)
        active, completed = design_states(registry)
        conflicts = [active[item["proposal_id"]] for item in selected if item["proposal_id"] in active]
        if conflicts and explicit:
            owners = {record["design_id"]: record for record in conflicts}
            print(json.dumps({"error": "already-claimed", "owners": list(owners.values())}, indent=2), file=sys.stderr)
            return 3
        selected = [
            item for item in selected
            if item["proposal_id"] not in active and item["proposal_id"] not in completed
        ]
        if not selected:
            raise CoordinatorError("no matching accepted, undesigned test proposals are available")
        snap = snapshot(repo, args.base_ref or default_base_ref(repo))
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        records = []
        for proposal in selected:
            design_id = f"{stamp}-design-{slug(proposal.get('test_id', 'test'))}-{secrets.token_hex(3)}"
            record = {
                "design_id": design_id,
                "status": "claimed",
                "claimed_at": now_iso(),
                "heartbeat_at": now_iso(),
                "repo_root": str(repo),
                "proposal_id": proposal["proposal_id"],
                "test_id": proposal.get("test_id", ""),
                "area": proposal.get("area", ""),
                "source_proposal_sha256": proposal["source_proposal_sha256"],
                "source_proposal": proposal,
                **snap,
            }
            registry["designs"].append(record)
            records.append(record)
        print(json.dumps({"designs": records}, indent=2, sort_keys=True))
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    repo = repo_root(args.repo)
    registry, _paths = read_registry(repo)
    print(json.dumps(find_design(registry, args.design_id), indent=2, sort_keys=True))
    return 0


def cmd_heartbeat(args: argparse.Namespace) -> int:
    repo = repo_root(args.repo)
    registry, _paths = read_registry(repo)
    record = find_design(registry, args.design_id)
    if record.get("status") not in ACTIVE_STATUSES:
        raise CoordinatorError(f"heartbeat requires an active design, got {record.get('status')!r}")
    updated = update_record(repo, args.design_id, lambda _item: None)
    print(json.dumps({"design_id": args.design_id, "heartbeat_at": updated["heartbeat_at"]}))
    return 0


def cmd_complete(args: argparse.Namespace) -> int:
    repo = repo_root(args.repo)
    registry, _paths = read_registry(repo)
    record = find_design(registry, args.design_id)
    if record.get("status") == "completed":
        if Path(record.get("document_path", "")).resolve() != Path(args.document).expanduser().resolve():
            raise CoordinatorError("completed design names a different document")
        print(json.dumps(record, indent=2, sort_keys=True))
        return 0
    if record.get("status") not in ACTIVE_STATUSES:
        raise CoordinatorError(f"completion requires status 'claimed', got {record.get('status')!r}")
    current = {item["proposal_id"]: item for item in load_accepted_proposals(repo)}.get(record["proposal_id"])
    if current is None or current["source_proposal_sha256"] != record["source_proposal_sha256"]:
        raise CoordinatorError("source proposal is no longer accepted or changed after claim")
    document = Path(args.document).expanduser().resolve()
    errors, document_sha = validate_document(repo, record, document)
    if errors:
        print("Test-design validation failed:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 5
    updated = update_record(repo, args.design_id, lambda item: item.update({
        "status": "completed",
        "completed_at": now_iso(),
        "document_path": str(document),
        "document_sha256": document_sha,
    }))
    print(json.dumps(updated, indent=2, sort_keys=True))
    return 0


def cmd_resolve(args: argparse.Namespace) -> int:
    repo = repo_root(args.repo)
    registry, _paths = read_registry(repo)
    record = find_design(registry, args.design_id)
    if record.get("status") not in ACTIVE_STATUSES:
        raise CoordinatorError(f"resolution requires status 'claimed', got {record.get('status')!r}")
    current = {item["proposal_id"]: item for item in load_accepted_proposals(repo)}.get(record["proposal_id"])
    if current is None or current["source_proposal_sha256"] != record["source_proposal_sha256"]:
        raise CoordinatorError("source proposal is no longer accepted or changed after claim")
    updated = update_record(repo, args.design_id, lambda item: item.update({
        "status": args.outcome,
        "completed_at": now_iso(),
        "resolution_note": args.note,
    }))
    print(json.dumps(updated, indent=2, sort_keys=True))
    return 0


def cmd_release(args: argparse.Namespace) -> int:
    repo = repo_root(args.repo)
    registry, _paths = read_registry(repo)
    record = find_design(registry, args.design_id)
    if record.get("status") not in ACTIVE_STATUSES:
        raise CoordinatorError(f"release requires status 'claimed', got {record.get('status')!r}")
    updated = update_record(repo, args.design_id, lambda item: item.update({
        "status": "cancelled",
        "completed_at": now_iso(),
        "release_reason": args.reason or "user cancelled",
    }))
    print(json.dumps(updated, indent=2, sort_keys=True))
    return 0


def cmd_self_test(_args: argparse.Namespace) -> int:
    with tempfile.TemporaryDirectory(prefix="proposal-designs-") as temporary:
        root = Path(temporary)
        repo = root / "repo"
        docs_worktree = root / "docs-worktree"
        repo.mkdir()
        run_git(repo, "init", "-q")
        run_git(repo, "config", "user.email", "test@example.com")
        run_git(repo, "config", "user.name", "Test")
        (repo / "tracked.txt").write_text("current\n", encoding="utf-8")
        run_git(repo, "add", "tracked.txt")
        run_git(repo, "commit", "-qm", "current")
        run_git(repo, "branch", "docs-wip")
        run_git(repo, "worktree", "add", "-q", str(docs_worktree), "docs-wip")
        (docs_worktree / "docs").mkdir()
        paths = paths_for(repo)
        paths["test_registry"].parent.mkdir(parents=True, exist_ok=True)
        accepted = {
            "proposal_id": "proposal-one",
            "status": "accepted",
            "test_id": "probe:one",
            "area": "gameplay",
            "title": "One probe",
            "base_ref": "origin/master",
            "revision": "source-commit-one",
            "decided_at": "2026-01-02T00:00:00Z",
            "recommended_tier": "manual-only",
            "decision_note": "Keep this manual-only and outside CI.",
            "coverage_gap": "The current apparatus does not cover this path.",
            "priority_rationale": "The path is important and currently untested.",
            "scenario": "Exercise the integrated path once.",
            "oracle": "Require the exact expected state transition.",
            "apparatus": "Add one registered manual-only probe.",
            "cost_and_risks": "One minute, with low expected flakiness.",
        }
        accepted_two = {
            **accepted,
            "proposal_id": "proposal-two",
            "test_id": "probe:two",
            "title": "Two probe",
            "revision": "source-commit-two",
        }
        paths["test_registry"].write_text(json.dumps({
            "schema": TEST_SCHEMA,
            "runs": [],
            "proposals": [accepted, accepted_two, {
                "proposal_id": "proposal-rejected",
                "status": "rejected",
                "test_id": "probe:rejected",
            }],
        }), encoding="utf-8")
        assert [item["proposal_id"] for item in load_accepted_proposals(repo)] == [
            "proposal-one", "proposal-two"
        ]
        claim_args = argparse.Namespace(
            repo=str(repo), base_ref="HEAD", proposal=["proposal-one"], area=[], stale_seconds=DEFAULT_STALE_SECONDS,
        )
        with contextlib.redirect_stdout(io.StringIO()):
            assert cmd_claim(claim_args) == 0
        registry, _paths = read_registry(repo)
        record = registry["designs"][0]
        duplicate_args = argparse.Namespace(
            repo=str(repo), base_ref="HEAD", proposal=["proposal-one"], area=[],
            stale_seconds=DEFAULT_STALE_SECONDS,
        )
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            assert cmd_claim(duplicate_args) == 3
        document = docs_worktree / "docs" / "test_designs" / "probe-one.md"
        document.parent.mkdir(parents=True)
        document.write_text(
            "# One probe test design\n\n"
            "Test design state: `ready for implementation`\n\n"
            "Source proposal: `proposal-one`\n\n"
            "Source test ID: `probe:one`\n\n"
            "Source ref and commit: `origin/master` at `source-commit-one`\n\n"
            f"Designed against: `HEAD` at `{record['revision']}`\n\n"
            "Implementation authorization: `not granted`\n\n"
            "Recommended tier: `manual-only`\n\n"
            + "\n\n".join(
                f"{heading}\n\n"
                + ("Verified." if heading == "## Binding decisions" else "Keep this manual-only and outside CI.")
                for heading in REQUIRED_HEADINGS
            )
            + "\n",
            encoding="utf-8",
        )
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            assert cmd_complete(argparse.Namespace(
                repo=str(repo), design_id=record["design_id"], document=str(document),
            )) == 5
        document.write_text(
            "# One probe test design\n\n"
            "Test design state: `ready for implementation`\n\n"
            "Source proposal: `proposal-one`\n\n"
            "Source test ID: `probe:one`\n\n"
            "Source ref and commit: `origin/master` at `source-commit-one`\n\n"
            f"Designed against: `HEAD` at `{record['revision']}`\n\n"
            "Accepted at: `2026-01-02T00:00:00Z`\n\n"
            "Implementation authorization: `not granted`\n\n"
            "Recommended tier: `manual-only`\n\n"
            + "\n\n".join(
                f"{heading}\n\n"
                + ("Keep this manual-only and outside CI." if heading == "## Binding decisions" else "Verified.")
                for heading in REQUIRED_HEADINGS
            )
            + "\n",
            encoding="utf-8",
        )
        with contextlib.redirect_stdout(io.StringIO()):
            assert cmd_complete(argparse.Namespace(
                repo=str(repo), design_id=record["design_id"], document=str(document),
            )) == 0
        registry, _paths = read_registry(repo)
        completed = find_design(registry, record["design_id"])
        assert completed["status"] == "completed"
        assert completed["document_sha256"] == sha256_text(document.read_text(encoding="utf-8"))
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                cmd_claim(claim_args)
            raise AssertionError("completed proposal design was reclaimed")
        except CoordinatorError:
            pass
        second_claim = argparse.Namespace(
            repo=str(repo), base_ref="HEAD", proposal=["proposal-two"], area=[],
            stale_seconds=DEFAULT_STALE_SECONDS,
        )
        with contextlib.redirect_stdout(io.StringIO()):
            assert cmd_claim(second_claim) == 0
        registry, _paths = read_registry(repo)
        second_record = next(item for item in registry["designs"] if item["proposal_id"] == "proposal-two")
        source_registry = json.loads(paths["test_registry"].read_text(encoding="utf-8"))
        source_registry["proposals"][1]["status"] = "superseded"
        paths["test_registry"].write_text(json.dumps(source_registry), encoding="utf-8")
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                cmd_resolve(argparse.Namespace(
                    repo=str(repo), design_id=second_record["design_id"],
                    outcome="superseded", note="source changed after claim",
                ))
            raise AssertionError("stale proposal design was resolved")
        except CoordinatorError:
            pass
    print("proposal design coordinator self-test: all cases pass")
    return 0


def add_repo_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", default=".", help="any path inside the target Git repository")


def add_design_id_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--design-id", required=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Coordinate accepted $test proposal design docs")
    sub = parser.add_subparsers(dest="subcommand", required=True)

    init = sub.add_parser("init")
    add_repo_arg(init)
    init.set_defaults(func=cmd_init)

    listing = sub.add_parser("list")
    add_repo_arg(listing)
    listing.add_argument("--active", action="store_true")
    listing.add_argument("--json", action="store_true")
    listing.add_argument("--limit", type=int)
    listing.add_argument("--stale-seconds", type=int, default=DEFAULT_STALE_SECONDS)
    listing.set_defaults(func=cmd_list)

    claim = sub.add_parser("claim")
    add_repo_arg(claim)
    claim.add_argument("--base-ref")
    claim.add_argument("--proposal", action="append", default=[])
    claim.add_argument("--area", action="append", default=[])
    claim.add_argument("--stale-seconds", type=int, default=DEFAULT_STALE_SECONDS)
    claim.set_defaults(func=cmd_claim)

    show = sub.add_parser("show")
    add_repo_arg(show)
    add_design_id_arg(show)
    show.set_defaults(func=cmd_show)

    heartbeat = sub.add_parser("heartbeat")
    add_repo_arg(heartbeat)
    add_design_id_arg(heartbeat)
    heartbeat.set_defaults(func=cmd_heartbeat)

    complete = sub.add_parser("complete")
    add_repo_arg(complete)
    add_design_id_arg(complete)
    complete.add_argument("--document", required=True)
    complete.set_defaults(func=cmd_complete)

    resolve = sub.add_parser("resolve")
    add_repo_arg(resolve)
    add_design_id_arg(resolve)
    resolve.add_argument("--outcome", choices=sorted(RESOLUTION_STATUSES), required=True)
    resolve.add_argument("--note", required=True)
    resolve.set_defaults(func=cmd_resolve)

    release = sub.add_parser("release")
    add_repo_arg(release)
    add_design_id_arg(release)
    release.add_argument("--reason")
    release.set_defaults(func=cmd_release)

    self_test = sub.add_parser("self-test")
    self_test.set_defaults(func=cmd_self_test)
    return parser


def main() -> int:
    try:
        args = build_parser().parse_args()
        return args.func(args)
    except (CoordinatorError, subprocess.CalledProcessError) as exc:
        print(f"proposal design coordinator: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
