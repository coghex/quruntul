#!/usr/bin/env python3
"""Atomic local coordination for the $assess-tests skill.

The script consumes observation sections from completed codex-test-result/v1
reports produced through the shared $test/$playtest lifecycle. Claims and
proposals are local bookkeeping; observations become assessed only when an
explicitly approved proposal is promoted byte-for-byte.
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


SCHEMA = "codex-test-assessment-coordinator/v1"
TEST_SCHEMA = "codex-test-coordinator/v1"
REPORT_SCHEMA = "codex-test-result/v1"
ASSESSMENT_SCHEMA = "codex-test-assessment/v1"
ACTIVE_STATUSES = {"claimed", "worktree-ready", "awaiting-approval"}
TERMINAL_STATUSES = {"completed", "cancelled", "abandoned", "blocked"}
DISPOSITIONS = {
    "confirmed-product-defect",
    "confirmed-harness-defect",
    "duplicate",
    "fixed-or-superseded",
    "expected",
    "inconclusive",
    "downstream",
}
SEVERITIES = {"blocker", "high", "medium", "low", "info"}
CONFIDENCES = {"high", "medium", "low"}
NEXT_LANES = {"$issue", "$draft-report", "$test", "$playtest", "existing issue", "no action"}
DEFAULT_STALE_SECONDS = 7 * 24 * 60 * 60


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
    common = common_git_dir(repo)
    test_root = common / "codex-test"
    root = test_root / "assessments"
    fingerprint = hashlib.sha256(str(common).encode()).hexdigest()[:8]
    canonical = common.parent if common.name == ".git" else repo
    worktrees = canonical.parent / f".codex-test-assessment-worktrees-{canonical.name}-{fingerprint}"
    return {
        "test_root": test_root,
        "test_registry": test_root / "registry.json",
        "test_reports": test_root / "reports",
        "root": root,
        "registry": root / "registry.json",
        "lock": root / "registry.lock",
        "proposals": root / "proposals",
        "reports": root / "reports",
        "worktrees": worktrees,
    }


def ensure_dirs(paths: dict[str, Path]) -> None:
    for key in ("root", "proposals", "reports", "worktrees"):
        paths[key].mkdir(parents=True, exist_ok=True)


def empty_registry() -> dict[str, Any]:
    return {"schema": SCHEMA, "updated_at": now_iso(), "assessments": []}


@contextlib.contextmanager
def locked_registry(repo: Path):
    paths = paths_for(repo)
    ensure_dirs(paths)
    with paths["lock"].open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if paths["registry"].exists():
            try:
                registry = json.loads(paths["registry"].read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise CoordinatorError(f"cannot read assessment registry: {exc}") from exc
            if registry.get("schema") != SCHEMA or not isinstance(registry.get("assessments"), list):
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


def find_assessment(registry: dict[str, Any], assessment_id: str) -> dict[str, Any]:
    for record in registry["assessments"]:
        if record.get("assessment_id") == assessment_id:
            return record
    raise CoordinatorError(f"unknown assessment id {assessment_id!r}")


def update_record(repo: Path, assessment_id: str, update: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    with locked_registry(repo) as (registry, _paths):
        record = find_assessment(registry, assessment_id)
        update(record)
        record["heartbeat_at"] = now_iso()
        return json.loads(json.dumps(record))


def reap_stale(registry: dict[str, Any], stale_seconds: int) -> None:
    now = dt.datetime.now(dt.timezone.utc)
    for record in registry["assessments"]:
        if record.get("status") not in ACTIVE_STATUSES:
            continue
        stamp = parse_iso(record.get("heartbeat_at") or record.get("claimed_at"))
        if stamp and (now - stamp).total_seconds() > stale_seconds:
            record.update({
                "status": "abandoned",
                "completed_at": now_iso(),
                "abandon_reason": f"stale claim exceeded {stale_seconds}s",
            })


def parse_frontmatter(text: str) -> dict[str, Any]:
    match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    if not match:
        raise CoordinatorError("report has no valid frontmatter")
    values: dict[str, Any] = {}
    for line in match.group(1).splitlines():
        if ":" not in line:
            continue
        key, raw = line.split(":", 1)
        raw = raw.strip()
        try:
            values[key.strip()] = json.loads(raw)
        except json.JSONDecodeError:
            values[key.strip()] = raw
    return values


def parse_observations(record: dict[str, Any]) -> list[dict[str, Any]]:
    path = Path(record.get("report_path", ""))
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8")
    frontmatter = parse_frontmatter(text)
    if frontmatter.get("schema") != REPORT_SCHEMA or frontmatter.get("run_id") != record.get("run_id"):
        raise CoordinatorError(f"test report metadata does not match registry: {path}")
    matches = list(re.finditer(r"^### (OBS-\d{3}) — (.+)$", text, re.MULTILINE))
    observations: list[dict[str, Any]] = []
    for index, match in enumerate(matches):
        next_obs = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        next_section = re.search(r"^## ", text[match.end():next_obs], re.MULTILINE)
        end = match.end() + next_section.start() if next_section else next_obs
        section = text[match.start():end].rstrip() + "\n"
        obs_id = match.group(1)
        observations.append({
            "source_key": f"{record['run_id']}/{obs_id}",
            "run_id": record["run_id"],
            "observation_id": obs_id,
            "title": match.group(2).strip(),
            "area": record.get("area", ""),
            "test_id": record.get("test_id", ""),
            "tested_revision": record.get("revision", ""),
            "report_path": str(path.resolve()),
            "log_path": record.get("log_path", ""),
            "observation_sha256": sha256_text(section),
        })
    return observations


def load_test_observations(repo: Path) -> list[dict[str, Any]]:
    paths = paths_for(repo)
    if not paths["test_registry"].is_file():
        raise CoordinatorError(f"no local coordinated-test registry at {paths['test_registry']}")
    try:
        test_registry = json.loads(paths["test_registry"].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CoordinatorError(f"cannot read local coordinated-test registry: {exc}") from exc
    if test_registry.get("schema") != TEST_SCHEMA or not isinstance(test_registry.get("runs"), list):
        raise CoordinatorError("unsupported local coordinated-test registry schema")
    observations: list[dict[str, Any]] = []
    for run in test_registry["runs"]:
        if run.get("status") != "completed":
            continue
        observations.extend(parse_observations(run))
    return observations


def observation_states(registry: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    active: dict[str, dict[str, Any]] = {}
    completed: dict[str, dict[str, Any]] = {}
    for record in registry["assessments"]:
        target = active if record.get("status") in ACTIVE_STATUSES else completed if record.get("status") == "completed" else None
        if target is None:
            continue
        for observation in record.get("observations", []):
            target[observation["source_key"]] = record
    return active, completed


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
    raise CoordinatorError("cannot resolve an assessment base ref")


def slug(text: str) -> str:
    return (re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "observations")[:40]


def selected_observations(args: argparse.Namespace, observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selectors_present = bool(args.run or args.observation or args.report or args.area)
    if not selectors_present:
        return observations
    reports = {str(Path(path).expanduser().resolve()) for path in args.report}
    areas = [value.casefold() for value in args.area]
    result = []
    for item in observations:
        haystack = " ".join((item["area"], item["test_id"], item["title"])).casefold()
        if (
            item["run_id"] in args.run
            or item["source_key"] in args.observation
            or item["report_path"] in reports
            or any(area in haystack for area in areas)
        ):
            result.append(item)
    return result


def snapshot(repo: Path, base_ref: str) -> dict[str, str]:
    resolved = run_git(repo, "rev-parse", "--verify", f"{base_ref}^{{commit}}", check=False)
    if resolved.returncode != 0:
        raise CoordinatorError(f"cannot resolve assessment base ref {base_ref!r}")
    revision = resolved.stdout.strip()
    return {
        "base_ref": base_ref,
        "revision": revision,
        "revision_subject": run_git(repo, "show", "-s", "--format=%s", revision).stdout.strip(),
        "revision_committed_at": run_git(repo, "show", "-s", "--format=%cI", revision).stdout.strip(),
        "base_resolved_at": now_iso(),
    }


def cmd_init(args: argparse.Namespace) -> int:
    repo = repo_root(args.repo)
    with locked_registry(repo) as (_registry, paths):
        print(json.dumps({key: str(value) for key, value in paths.items()}, indent=2, sort_keys=True))
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    repo = repo_root(args.repo)
    with locked_registry(repo) as (registry, paths):
        reap_stale(registry, args.stale_seconds)
        records = json.loads(json.dumps(registry["assessments"]))
    if args.active:
        records = [record for record in records if record.get("status") in ACTIVE_STATUSES]
    records.sort(key=lambda record: record.get("claimed_at", ""), reverse=True)
    if args.limit:
        records = records[:args.limit]
    observations = load_test_observations(repo)
    registry, _ = read_registry(repo)
    active, completed = observation_states(registry)
    available = [item for item in observations if item["source_key"] not in active and item["source_key"] not in completed]
    if args.json:
        print(json.dumps({"available": available, "assessments": records, "registry": str(paths["registry"])}, indent=2))
        return 0
    print(f"Available observations: {len(available)}")
    for item in available:
        print(f"  {item['source_key']}  [{item['area']}]  {item['title']}")
    if not records:
        print("No matching assessments.")
    else:
        print("\nAssessments:")
        for record in records:
            print(f"  {record['assessment_id']}  {record['status']}  {len(record.get('observations', []))} observation(s)")
    print(f"\nRegistry: {paths['registry']}")
    return 0


def cmd_claim(args: argparse.Namespace) -> int:
    repo = repo_root(args.repo)
    all_observations = load_test_observations(repo)
    selected = selected_observations(args, all_observations)
    explicitly_selected = bool(args.run or args.observation or args.report or args.area)
    with locked_registry(repo) as (registry, paths):
        reap_stale(registry, args.stale_seconds)
        active, completed = observation_states(registry)
        conflicts = [active[item["source_key"]] for item in selected if item["source_key"] in active]
        if conflicts and explicitly_selected:
            owners = {record["assessment_id"]: record for record in conflicts}
            print(json.dumps({"error": "already-claimed", "owners": list(owners.values())}, indent=2), file=sys.stderr)
            return 3
        selected = [
            item for item in selected
            if item["source_key"] not in active and item["source_key"] not in completed
        ]
        if not selected:
            raise CoordinatorError("no matching unassessed observations are available")
        snap = snapshot(repo, args.base_ref or default_base_ref(repo))
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        areas = "-".join(sorted({item["area"] for item in selected}))
        assessment_id = f"{stamp}-{slug(areas)}-{secrets.token_hex(3)}"
        record = {
            "assessment_id": assessment_id,
            "status": "claimed",
            "claimed_at": now_iso(),
            "heartbeat_at": now_iso(),
            "repo_root": str(repo),
            "observations": selected,
            **snap,
            "proposal_path": str(paths["proposals"] / f"{assessment_id}.proposal.md"),
            "report_path": str(paths["reports"] / f"{assessment_id}.test-assessment.md"),
        }
        registry["assessments"].append(record)
        print(json.dumps(record, indent=2, sort_keys=True))
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    repo = repo_root(args.repo)
    registry, _paths = read_registry(repo)
    print(json.dumps(find_assessment(registry, args.assessment_id), indent=2, sort_keys=True))
    return 0


def cmd_worktree(args: argparse.Namespace) -> int:
    repo = repo_root(args.repo)
    registry, paths = read_registry(repo)
    record = find_assessment(registry, args.assessment_id)
    if record.get("status") != "claimed":
        raise CoordinatorError(f"worktree creation requires status 'claimed', got {record.get('status')!r}")
    target = paths["worktrees"] / args.assessment_id
    if target.exists():
        raise CoordinatorError(f"assessment worktree already exists: {target}")
    result = run_git(repo, "worktree", "add", "--detach", str(target), record["revision"], check=False)
    if result.returncode != 0:
        update_record(repo, args.assessment_id, lambda item: item.update({
            "status": "blocked",
            "completed_at": now_iso(),
            "worktree_error": result.stderr.strip() or result.stdout.strip(),
        }))
        print(result.stderr or result.stdout, file=sys.stderr)
        return 4
    updated = update_record(repo, args.assessment_id, lambda item: item.update({
        "status": "worktree-ready", "worktree_path": str(target),
    }))
    print(json.dumps(updated, indent=2, sort_keys=True))
    return 0


def fill_template(template: str, record: dict[str, Any]) -> str:
    keys = [item["source_key"] for item in record["observations"]]
    bullets = []
    for item in record["observations"]:
        bullets.append(
            f"- `{item['source_key']}` — {item['title']} "
            f"(tested `{item['tested_revision']}`, report `{item['report_path']}`)"
        )
    replacements = {
        "{{assessment_id_json}}": json.dumps(record["assessment_id"]),
        "{{base_ref_json}}": json.dumps(record["base_ref"]),
        "{{revision_json}}": json.dumps(record["revision"]),
        "{{revision_subject_json}}": json.dumps(record["revision_subject"]),
        "{{revision_committed_at_json}}": json.dumps(record["revision_committed_at"]),
        "{{base_resolved_at_json}}": json.dumps(record["base_resolved_at"]),
        "{{source_observations_json}}": json.dumps(keys),
        "{{assessment_id}}": record["assessment_id"],
        "{{source_observation_bullets}}": "\n".join(bullets),
    }
    for marker, value in replacements.items():
        template = template.replace(marker, value)
    return template


def cmd_proposal_init(args: argparse.Namespace) -> int:
    repo = repo_root(args.repo)
    registry, _paths = read_registry(repo)
    record = find_assessment(registry, args.assessment_id)
    if record.get("status") not in {"worktree-ready", "awaiting-approval"}:
        raise CoordinatorError(f"proposal initialization is not allowed from {record.get('status')!r}")
    destination = Path(record["proposal_path"])
    if destination.exists():
        print(destination)
        return 0
    template = (Path(__file__).resolve().parent.parent / "assets" / "test-assessment-template.md").read_text(encoding="utf-8")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(fill_template(template, record), encoding="utf-8")
    print(destination)
    return 0


def field_value(section: str, field: str) -> str | None:
    match = re.search(rf"^- \*\*{re.escape(field)}:\*\*\s*(.+)$", section, re.MULTILINE)
    return match.group(1).strip() if match else None


def validate_proposal(path: Path, record: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
    errors: list[str] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"cannot read proposal: {exc}"], {}
    keys = [item["source_key"] for item in record["observations"]]
    required = [
        f'schema: "{ASSESSMENT_SCHEMA}"',
        f'assessment_id: {json.dumps(record["assessment_id"])}',
        f'base_ref: {json.dumps(record["base_ref"])}',
        f'assessed_commit: {json.dumps(record["revision"])}',
        f'source_observations: {json.dumps(keys)}',
        "## Assessment summary",
        "## Source observations",
        "## Findings",
        "## Cross-observation analysis",
        "## Coverage and limitations",
        "## Next-step queue",
    ]
    for marker in required:
        if marker not in text:
            errors.append(f"missing {marker!r}")
    if "<REPLACE:" in text:
        errors.append("proposal still contains <REPLACE: ...> markers")
    matches = list(re.finditer(r"^### (FND-\d{3}) — (.+)$", text, re.MULTILINE))
    ids = [match.group(1) for match in matches]
    expected = [f"FND-{index:03d}" for index in range(1, len(ids) + 1)]
    if not matches:
        errors.append("proposal requires at least one FND-nnn section")
    elif ids != expected:
        errors.append("finding IDs must be unique and sequential from FND-001")
    assigned: dict[str, int] = {key: 0 for key in keys}
    dispositions: list[str] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else text.find("\n## Cross-observation analysis", match.end())
        if end < 0:
            end = len(text)
        section = text[match.start():end]
        required_fields = (
            "Disposition", "Severity", "Confidence", "Source observations",
            "Current-version evidence", "Tracker deduplication", "Interpretation",
            "Recommended next lane", "Proposed action",
        )
        values = {field: field_value(section, field) for field in required_fields}
        for field, value in values.items():
            if value is None:
                errors.append(f"{match.group(1)} missing field {field!r}")
        disposition = values.get("Disposition")
        if disposition and disposition not in DISPOSITIONS:
            errors.append(f"{match.group(1)} has invalid disposition {disposition!r}")
        elif disposition:
            dispositions.append(disposition)
        severity = values.get("Severity")
        if severity and severity not in SEVERITIES:
            errors.append(f"{match.group(1)} has invalid severity {severity!r}")
        confidence = values.get("Confidence")
        if confidence and confidence not in CONFIDENCES:
            errors.append(f"{match.group(1)} has invalid confidence {confidence!r}")
        lane = values.get("Recommended next lane")
        if lane and lane not in NEXT_LANES:
            errors.append(f"{match.group(1)} has invalid next lane {lane!r}")
        sources = values.get("Source observations") or ""
        named = re.findall(r"[A-Za-z0-9][A-Za-z0-9._-]*/OBS-\d{3}", sources)
        for source in named:
            if source not in assigned:
                errors.append(f"{match.group(1)} names unknown source observation {source!r}")
            else:
                assigned[source] += 1
    for key, count in assigned.items():
        if count != 1:
            errors.append(f"source observation {key!r} must be assigned exactly once; got {count}")
    return errors, {"finding_count": len(matches), "dispositions": dispositions, "sha256": sha256_text(text)}


def cmd_propose(args: argparse.Namespace) -> int:
    repo = repo_root(args.repo)
    registry, _paths = read_registry(repo)
    record = find_assessment(registry, args.assessment_id)
    if record.get("status") not in {"worktree-ready", "awaiting-approval"}:
        raise CoordinatorError(f"proposal is not allowed from {record.get('status')!r}")
    path = Path(record["proposal_path"])
    errors, summary = validate_proposal(path, record)
    if errors:
        print("Proposal validation failed:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 5
    updated = update_record(repo, args.assessment_id, lambda item: item.update({
        "status": "awaiting-approval",
        "proposed_at": now_iso(),
        "proposal_sha256": summary["sha256"],
        "finding_count": summary["finding_count"],
        "proposed_dispositions": summary["dispositions"],
    }))
    print(json.dumps(updated, indent=2, sort_keys=True))
    return 0


def atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="assessment.", suffix=".md", dir=destination.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(source.read_bytes())
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def cmd_approve(args: argparse.Namespace) -> int:
    repo = repo_root(args.repo)
    registry, _paths = read_registry(repo)
    record = find_assessment(registry, args.assessment_id)
    if record.get("status") == "completed":
        if record.get("proposal_sha256") != args.proposal_sha:
            raise CoordinatorError("completed assessment hash does not match the requested approval")
        print(json.dumps(record, indent=2, sort_keys=True))
        return 0
    if record.get("status") != "awaiting-approval":
        raise CoordinatorError(f"approval requires status 'awaiting-approval', got {record.get('status')!r}")
    proposal = Path(record["proposal_path"])
    errors, summary = validate_proposal(proposal, record)
    if errors:
        raise CoordinatorError("stored proposal no longer validates; run propose again")
    if summary["sha256"] != record.get("proposal_sha256") or summary["sha256"] != args.proposal_sha:
        raise CoordinatorError("proposal content changed or the approved SHA-256 does not match")
    report = Path(record["report_path"])
    atomic_copy(proposal, report)
    updated = update_record(repo, args.assessment_id, lambda item: item.update({
        "status": "completed",
        "approved_at": now_iso(),
        "completed_at": now_iso(),
        "report_sha256": summary["sha256"],
        "finding_count": summary["finding_count"],
        "dispositions": summary["dispositions"],
    }))
    print(json.dumps(updated, indent=2, sort_keys=True))
    return 0


def cmd_heartbeat(args: argparse.Namespace) -> int:
    repo = repo_root(args.repo)
    registry, _paths = read_registry(repo)
    record = find_assessment(registry, args.assessment_id)
    if record.get("status") not in ACTIVE_STATUSES:
        raise CoordinatorError(f"heartbeat requires an active assessment, got {record.get('status')!r}")
    record = update_record(repo, args.assessment_id, lambda _item: None)
    print(json.dumps({"assessment_id": args.assessment_id, "heartbeat_at": record["heartbeat_at"]}))
    return 0


def cmd_release(args: argparse.Namespace) -> int:
    repo = repo_root(args.repo)
    registry, _paths = read_registry(repo)
    record = find_assessment(registry, args.assessment_id)
    if record.get("status") not in ACTIVE_STATUSES:
        raise CoordinatorError(f"release requires an active assessment, got {record.get('status')!r}")
    updated = update_record(repo, args.assessment_id, lambda item: item.update({
        "status": "cancelled", "completed_at": now_iso(), "release_reason": args.reason or "user cancelled",
    }))
    print(json.dumps(updated, indent=2, sort_keys=True))
    return 0


def cmd_cleanup(args: argparse.Namespace) -> int:
    repo = repo_root(args.repo)
    registry, _paths = read_registry(repo)
    record = find_assessment(registry, args.assessment_id)
    if record.get("status") not in TERMINAL_STATUSES:
        raise CoordinatorError(f"cleanup requires a terminal assessment, got {record.get('status')!r}")
    worktree_text = record.get("worktree_path")
    if not worktree_text:
        print("No assessment worktree was recorded.")
        return 0
    worktree = Path(worktree_text)
    if not worktree.exists():
        update_record(repo, args.assessment_id, lambda item: item.update({"worktree_removed_at": now_iso()}))
        print("Assessment worktree was already absent.")
        return 0
    result = run_git(repo, "worktree", "remove", str(worktree), check=False)
    if result.returncode != 0:
        print("Worktree preserved because normal removal refused it:", file=sys.stderr)
        print(result.stderr or result.stdout, file=sys.stderr)
        return 4
    update_record(repo, args.assessment_id, lambda item: item.update({"worktree_removed_at": now_iso()}))
    print(f"Removed clean assessment worktree {worktree}")
    return 0


def cmd_self_test(_args: argparse.Namespace) -> int:
    with tempfile.TemporaryDirectory(prefix="assess-tests-") as temporary:
        repo = Path(temporary) / "repo"
        repo.mkdir()
        run_git(repo, "init", "-q")
        run_git(repo, "config", "user.email", "test@example.com")
        run_git(repo, "config", "user.name", "Test")
        (repo / "tracked.txt").write_text("current\n", encoding="utf-8")
        run_git(repo, "add", "tracked.txt")
        run_git(repo, "commit", "-qm", "current")
        revision = run_git(repo, "rev-parse", "HEAD").stdout.strip()
        paths = paths_for(repo)
        paths["test_reports"].mkdir(parents=True)
        report = paths["test_reports"] / "run-one.test-result.md"
        report.write_text(
            "---\nschema: \"codex-test-result/v1\"\nrun_id: \"run-one\"\n"
            "interpretation_status: \"observations\"\n---\n\n"
            "# Test result\n\n## Observations\n\n"
            "### OBS-001 — Something happened\n\n- **Evidence:** yes\n\n"
            "### OBS-002 — A related symptom happened\n\n- **Evidence:** also yes\n\n"
            "## Coverage and limitations\n\n- one\n",
            encoding="utf-8",
        )
        playtest_report = paths["test_reports"] / "run-playtest.test-result.md"
        playtest_report.write_text(
            "---\nschema: \"codex-test-result/v1\"\nrun_id: \"run-playtest\"\n"
            "interpretation_status: \"observations\"\n---\n\n"
            "# Test result\n\n## Observations\n\n"
            "### OBS-001 — The player could not find the visible action\n\n"
            "- **Evidence:** retained screenshot and trace\n\n"
            "## Coverage and limitations\n\n- one bounded persona\n",
            encoding="utf-8",
        )
        blocked_report = paths["test_reports"] / "run-blocked.test-result.md"
        blocked_report.write_text(
            "---\nschema: \"codex-test-result/v1\"\nrun_id: \"run-blocked\"\n"
            "interpretation_status: \"blocked\"\n---\n\n"
            "# Test result\n\n## Observations\n\n"
            "### OBS-001 — The harness cannot reach its primary path\n\n"
            "- **Category:** harness-gap\n"
            "- **Evidence:** setup stopped before product behavior was exercised\n\n"
            "## Coverage and limitations\n\n- blocked before the primary path\n",
            encoding="utf-8",
        )
        clean_report = paths["test_reports"] / "run-clean.test-result.md"
        clean_report.write_text(
            "---\nschema: \"codex-test-result/v1\"\nrun_id: \"run-clean\"\n"
            "interpretation_status: \"clean\"\n---\n\n"
            "# Test result\n\n## Observations\n\nNone.\n\n"
            "## Coverage and limitations\n\n- clean run\n",
            encoding="utf-8",
        )
        paths["test_registry"].parent.mkdir(parents=True, exist_ok=True)
        paths["test_registry"].write_text(json.dumps({
            "schema": TEST_SCHEMA,
            "runs": [
                {
                    "run_id": "run-one", "status": "completed",
                    "interpretation_outcome": "observations", "area": "gameplay",
                    "test_id": "probe:one", "revision": revision,
                    "report_path": str(report),
                    "log_path": str(paths["test_root"] / "one.log"),
                },
                {
                    "run_id": "run-playtest", "status": "completed",
                    "interpretation_outcome": "observations", "area": "onboarding",
                    "test_id": "playtest:naive-onboarding", "revision": revision,
                    "report_path": str(playtest_report),
                    "log_path": str(paths["test_root"] / "playtest.log"),
                },
                {
                    "run_id": "run-blocked", "status": "completed",
                    "interpretation_outcome": "blocked", "area": "test-infrastructure",
                    "test_id": "diagnostic:blocked", "revision": revision,
                    "report_path": str(blocked_report),
                    "log_path": str(paths["test_root"] / "blocked.log"),
                },
                {
                    "run_id": "run-clean", "status": "completed",
                    "interpretation_outcome": "clean", "area": "gameplay",
                    "test_id": "probe:clean", "revision": revision,
                    "report_path": str(clean_report),
                    "log_path": str(paths["test_root"] / "clean.log"),
                },
            ],
        }), encoding="utf-8")
        loaded = load_test_observations(repo)
        assert {item["source_key"] for item in loaded} == {
            "run-one/OBS-001", "run-one/OBS-002", "run-playtest/OBS-001",
            "run-blocked/OBS-001",
        }
        selected_blocked = selected_observations(argparse.Namespace(
            run=[], observation=[], report=[str(blocked_report)], area=[],
        ), loaded)
        assert [item["source_key"] for item in selected_blocked] == ["run-blocked/OBS-001"]
        claim_args = argparse.Namespace(
            repo=str(repo), base_ref="HEAD", run=[], observation=[], report=[], area=[],
            stale_seconds=DEFAULT_STALE_SECONDS,
        )
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            assert cmd_claim(claim_args) == 0
        registry, _ = read_registry(repo)
        assessment_id = registry["assessments"][0]["assessment_id"]
        record = find_assessment(registry, assessment_id)
        assert {item["test_id"] for item in record["observations"]} == {
            "probe:one", "playtest:naive-onboarding", "diagnostic:blocked"
        }
        duplicate_args = argparse.Namespace(
            repo=str(repo), base_ref="HEAD", run=["run-playtest"],
            observation=[], report=[], area=[], stale_seconds=DEFAULT_STALE_SECONDS,
        )
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            assert cmd_claim(duplicate_args) == 3
            assert cmd_worktree(argparse.Namespace(repo=str(repo), assessment_id=assessment_id)) == 0
            assert cmd_proposal_init(argparse.Namespace(repo=str(repo), assessment_id=assessment_id)) == 0
        registry, _ = read_registry(repo)
        record = find_assessment(registry, assessment_id)
        proposal = Path(record["proposal_path"])
        text = proposal.read_text(encoding="utf-8")
        text = re.sub(r"<REPLACE:[^>]+>", "None", text)
        source_keys = [item["source_key"] for item in record["observations"]]
        text = text.replace("- **Disposition:** None", "- **Disposition:** inconclusive")
        text = text.replace("- **Severity:** None", "- **Severity:** low")
        text = text.replace("- **Confidence:** None", "- **Confidence:** medium")
        assigned_sources = ", ".join(f"`{key}`" for key in source_keys)
        text = text.replace("- **Source observations:** None", f"- **Source observations:** {assigned_sources}")
        text = text.replace("- **Recommended next lane:** None", "- **Recommended next lane:** $playtest")
        proposal.write_text(text, encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            assert cmd_propose(argparse.Namespace(repo=str(repo), assessment_id=assessment_id)) == 0
        registry, _ = read_registry(repo)
        record = find_assessment(registry, assessment_id)
        wrong = argparse.Namespace(repo=str(repo), assessment_id=assessment_id, proposal_sha="0" * 64)
        try:
            cmd_approve(wrong)
            raise AssertionError("wrong proposal hash was accepted")
        except CoordinatorError:
            pass
        with contextlib.redirect_stdout(io.StringIO()):
            assert cmd_approve(argparse.Namespace(
                repo=str(repo), assessment_id=assessment_id, proposal_sha=record["proposal_sha256"],
            )) == 0
            assert cmd_cleanup(argparse.Namespace(repo=str(repo), assessment_id=assessment_id)) == 0
        registry, _ = read_registry(repo)
        completed = find_assessment(registry, assessment_id)
        assert completed["status"] == "completed"
        assert Path(completed["report_path"]).is_file()
        assert Path(completed["report_path"]).read_bytes() == proposal.read_bytes()
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                cmd_claim(claim_args)
            raise AssertionError("completed observation was reclaimed")
        except CoordinatorError:
            pass
    print("assessment coordinator self-test: all cases pass")
    return 0


def add_repo_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", default=".", help="any path inside the target Git repository")


def add_id_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--assessment-id", required=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Atomic coordinator for the Codex $assess-tests skill")
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
    claim.add_argument("--run", action="append", default=[])
    claim.add_argument("--observation", action="append", default=[])
    claim.add_argument("--report", action="append", default=[])
    claim.add_argument("--area", action="append", default=[])
    claim.add_argument("--stale-seconds", type=int, default=DEFAULT_STALE_SECONDS)
    claim.set_defaults(func=cmd_claim)

    show = sub.add_parser("show")
    add_repo_arg(show)
    add_id_arg(show)
    show.set_defaults(func=cmd_show)

    worktree = sub.add_parser("worktree")
    add_repo_arg(worktree)
    add_id_arg(worktree)
    worktree.set_defaults(func=cmd_worktree)

    heartbeat = sub.add_parser("heartbeat")
    add_repo_arg(heartbeat)
    add_id_arg(heartbeat)
    heartbeat.set_defaults(func=cmd_heartbeat)

    proposal_init = sub.add_parser("proposal-init")
    add_repo_arg(proposal_init)
    add_id_arg(proposal_init)
    proposal_init.set_defaults(func=cmd_proposal_init)

    propose = sub.add_parser("propose")
    add_repo_arg(propose)
    add_id_arg(propose)
    propose.set_defaults(func=cmd_propose)

    approve = sub.add_parser("approve")
    add_repo_arg(approve)
    add_id_arg(approve)
    approve.add_argument("--proposal-sha", required=True)
    approve.set_defaults(func=cmd_approve)

    release = sub.add_parser("release")
    add_repo_arg(release)
    add_id_arg(release)
    release.add_argument("--reason")
    release.set_defaults(func=cmd_release)

    cleanup = sub.add_parser("cleanup")
    add_repo_arg(cleanup)
    add_id_arg(cleanup)
    cleanup.set_defaults(func=cmd_cleanup)

    self_test = sub.add_parser("self-test")
    self_test.set_defaults(func=cmd_self_test)
    return parser


def main() -> int:
    try:
        args = build_parser().parse_args()
        return args.func(args)
    except (CoordinatorError, subprocess.CalledProcessError) as exc:
        print(f"assessment coordinator: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
