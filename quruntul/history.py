"""Import a repository's legacy `$test` history through the adapter's `legacy_history` hook.

The adapter reads its own legacy store and returns closed history in the shape
docs/design.md defines (Legacy history import). The engine alone validates it
and writes the ledger. Nothing is run, replayed or re-executed.

An import is all or nothing. It validates the complete supplied history, stages
every promised copy under imported/.staging/<import>, publishes the copies into
imported/<import>, and then commits every row in one ledger transaction: that
commit is its success boundary. Before it, a refusal or failure removes the
import's own files, and an interruption leaves only unpublished leftovers that no
reader or export sees and that the next import's recovery removes. After it,
recovery keeps the complete import.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import uuid

from quruntul import adapter as adapters
from quruntul import report
from quruntul.common import LabError, atomic_json, digest, utc
from quruntul.state import SCHEMA

REPORT_SCHEMA = "quruntul-import/v1"
REPORT_NAME = "import-report.json"
LISTS = (("runs", "run"), ("observations", "observation"), ("assessments", "assessment"),
         ("proposals", "proposal"), ("evidence", "evidence"))
KINDS = tuple(kind for _, kind in LISTS)
# (required, optional) fields of each record kind.
FIELDS = {
    "run": ({"source", "target", "revision", "started", "finished", "status"},
            {"interpretation", "document", "provenance"}),
    "observation": ({"source", "run", "number", "title", "disposition"},
                    {"area", "assessment", "document", "provenance"}),
    "assessment": ({"source", "status", "created"}, {"observations", "document", "provenance"}),
    "proposal": ({"source", "target", "lane", "status", "created"}, {"refers", "document", "provenance"}),
    "evidence": ({"source", "owner", "role", "mode"}, {"path", "size", "provenance"}),
}
RUN_STATUSES = ("passed", "failed", "cancelled", "error")
NONTERMINAL = ("pending", "queued", "preparing", "running", "started")
DECIDED = ("rejected", "designed", "implemented")
ROLES = ("report", "log", "document", "other")
MODES = ("copy", "reference", "absent")
CHUNK = 1 << 20
EXCLUDED = "reference: recorded by source path only; its bytes were not copied and are excluded from export"

_read = os.read


def _checkpoint(name: str) -> None:
    """A point at which an import can be interrupted: 'staged', 'published', 'commit', 'ledger' (a new
    ledger built but not yet in place) or 'committed'."""


class Refused(Exception):
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


class CopyFailed(Exception):
    """A promised copy that cannot be taken whole, exactly as declared."""


# -- identities and values ----------------------------------------------------

def identity(value) -> str | None:
    """The namespaced key of a source identity or reference, or None when it is malformed."""
    if not isinstance(value, dict) or set(value) != {"store", "kind", "id"}:
        return None
    if not all(isinstance(value[k], str) and value[k].strip() for k in value) or value["kind"] not in KINDS:
        return None
    return json.dumps([value["store"], value["kind"], value["id"]])


def label(key: str) -> str:
    return "/".join(json.loads(key))


def kind_of(key: str) -> str:
    return json.loads(key)[1]


def local_id(key: str) -> str:
    """The ledger id of an imported run, assessment, proposal or evidence entry: stable per identity."""
    return f"imported-{kind_of(key)}-{hashlib.sha256(key.encode()).hexdigest()[:16]}"


def instant(value) -> datetime | None:
    """An ISO 8601 timestamp with a UTC offset, as an aware datetime."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


# -- validation ------------------------------------------------------------------

def validate(history) -> tuple[dict, list[str]]:
    """Every supplied record by identity key, and every structural problem, never only the first."""
    names = {name for name, _ in LISTS}
    if not isinstance(history, dict) or not set(history) <= names:
        return {}, ["legacy_history must return a mapping whose only keys are " + ", ".join(sorted(names))]
    records, problems, seen = {}, [], {}
    for name, kind in LISTS:
        items = history.get(name, [])
        if not isinstance(items, (list, tuple)):
            problems.append(f"{name} must be a list")
            continue
        for position, record in enumerate(items):
            where = f"{name}[{position}]"
            key = identity(record.get("source")) if isinstance(record, dict) else None
            if key is None or kind_of(key) != kind:
                problems.append(f"{where}: needs a source identity {{store, kind: {kind}, id}}")
                continue
            if key in seen:
                problems.append(f"{label(key)}: supplied twice ({seen[key]} and {where})")
                continue
            seen[key] = where
            problems += _check(kind, record, label(key))
            records[key] = dict(kind=kind, key=key, label=label(key), record=record)
    return records, problems


def _check(kind: str, record: dict, name: str) -> list[str]:
    """Every problem in one record. A missing field never hides another field's problem, and no value is
    used as a key before its type is known."""
    required, optional = FIELDS[kind]
    problems = []
    fields = {key for key in record if isinstance(key, str)}
    missing, unknown = sorted(required - fields), sorted(fields - required - optional)
    if missing:
        problems.append(f"{name}: missing {', '.join(missing)}")
    if unknown:
        problems.append(f"{name}: unknown fields {', '.join(unknown)}")
    if len(fields) != len(record):
        problems.append(f"{name}: field names must be text, not "
                        + ", ".join(repr(key) for key in record if not isinstance(key, str)))
    try:
        # Plain JSON survives a round trip unchanged: text keys at every depth, lists not tuples, no NaN.
        plain = json.loads(json.dumps(record, allow_nan=False, sort_keys=True)) == record
    except (TypeError, ValueError):
        plain = False
    if not plain:
        problems.append(f"{name}: not plain JSON")
    for field in ("document", "provenance"):
        if field in record and not isinstance(record[field], dict):
            problems.append(f"{name}: {field} must be an object")

    def text(field):
        if field in record and not (isinstance(record[field], str) and record[field].strip()):
            problems.append(f"{name}: {field} must be nonblank text")

    def when(field):
        if field in record and instant(record[field]) is None:
            problems.append(f"{name}: {field} must be an ISO 8601 timestamp with a UTC offset, not {record[field]!r}")

    def references(field, kinds, many=False):
        if field not in record:
            return
        values = record[field]
        if many and not isinstance(values, list):
            problems.append(f"{name}: {field} must be a list of references")
            return
        for value in values if many else [values]:
            if value is None and not many and field not in required:
                continue
            key = identity(value)
            if key is None or kind_of(key) not in kinds:
                problems.append(f"{name}: {field} must reference a {' or '.join(kinds)} by {{store, kind, id}}")

    def decided(field, accepted, open_item):
        """A status: one accepted value, an open item when it is other text, malformed otherwise."""
        if field not in record or record[field] in accepted:
            return
        if isinstance(record[field], str) and record[field].strip():
            problems.append(f"{name}: {open_item(record[field])}")
        else:
            problems.append(f"{name}: {field} must be text, not {record[field]!r}")

    if kind == "run":
        text("target")
        text("revision")
        when("started")
        when("finished")
        started, finished = instant(record.get("started")), instant(record.get("finished"))
        if started and finished and finished < started:
            problems.append(f"{name}: finished before it started")
        status = record.get("status")
        if "status" not in record:
            pass
        elif isinstance(status, str) and status in NONTERMINAL:
            problems.append(f"{name}: open item: status {status!r} is not terminal")
        elif status not in RUN_STATUSES:
            problems.append(f"{name}: status must be one of {', '.join(RUN_STATUSES)}, not {status!r}")
        if record.get("interpretation") not in (None, *report.STATUSES):
            problems.append(f"{name}: interpretation must be one of {', '.join(report.STATUSES)} or null")
    elif kind == "observation":
        references("run", ("run",))
        references("assessment", ("assessment",))
        number = record.get("number", 1)
        if not isinstance(number, int) or isinstance(number, bool) or number < 1:
            problems.append(f"{name}: number must be a positive integer")
        text("title")
        if not isinstance(record.get("area", ""), str):
            problems.append(f"{name}: area must be text")
        if "disposition" in record and record["disposition"] is None:
            problems.append(f"{name}: open item: the observation has no final disposition")
        else:
            text("disposition")
    elif kind == "assessment":
        when("created")
        references("observations", ("observation",), many=True)
        decided("status", ("approved",), lambda status: f"open item: the assessment is {status!r}, not approved")
    elif kind == "proposal":
        text("target")
        text("lane")
        when("created")
        references("refers", ("run", "observation", "assessment"), many=True)
        decided("status", DECIDED, lambda status: f"open item: the proposal is {status!r}, "
                                                  f"not {', '.join(DECIDED[:-1])} or {DECIDED[-1]}")
    else:
        problems += _check_evidence(record, name)
    return problems


def _check_evidence(record: dict, name: str) -> list[str]:
    problems = []
    role, mode = record.get("role"), record.get("mode")
    known_role, known_mode = role in ROLES, mode in MODES  # each then a str, safe to use as a key
    if "role" in record and not known_role:
        problems.append(f"{name}: role must be one of {', '.join(ROLES)}, not {role!r}")
    if "mode" in record and not known_mode:
        problems.append(f"{name}: mode must be one of {', '.join(MODES)}, not {mode!r}")
    if "owner" in record:
        owners = {"report": ("run",), "log": ("run",), "document": ("assessment",)}.get(
            role if known_role else "other", ("run", "assessment"))
        owner = identity(record["owner"])
        if owner is None or kind_of(owner) not in owners:
            problems.append(f"{name}: owner must reference a {' or '.join(owners)} by {{store, kind, id}}")
    if mode == "absent":
        if "path" in record or "size" in record:
            problems.append(f"{name}: an absent entry names no path or size")
    elif known_mode or "path" in record or "size" in record:
        path, size = record.get("path"), record.get("size")
        if not isinstance(path, str) or not os.path.isabs(path) or os.path.basename(path) in ("", ".", ".."):
            problems.append(f"{name}: path must be an absolute path to a file")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            problems.append(f"{name}: size must be the file's size in bytes")
    if not (known_role and known_mode):
        return problems
    if role == "report" and mode != "copy":
        problems.append(f"{name}: a run's report is always copied, never {mode!r}")
    elif role in ("log", "document") and mode == "reference":
        problems.append(f"{name}: an existing {'run log' if role == 'log' else 'assessment document'} is always "
                        "copied, never referenced")
    elif role == "other" and mode == "absent":
        problems.append(f"{name}: only a run log or an assessment document is declared absent")
    return problems


# -- planning --------------------------------------------------------------------

def plan(records: dict, ledger: dict, suites: dict) -> dict:
    """Classify each record against the ledger and find every problem that does not need its files."""
    problems, new, unchanged = [], [], Counter()
    imported = ledger["imported"]
    known = set(records) | set(imported)
    for key, entry in records.items():
        previous = imported.get(key)
        entry["new"] = previous is None
        if previous is None:
            new.append(entry)
        elif previous["digest"] != digest(entry["record"]):
            problems.append(f"{entry['label']}: changed since import {previous['import_id']} recorded it; "
                            "imported history is immutable")
        else:
            unchanged[entry["kind"]] += 1
        for field in ("run", "assessment", "observations", "refers", "owner"):
            value = entry["record"].get(field)
            for reference in value if isinstance(value, list) else [value]:
                target = identity(reference)
                if target is not None and target not in known:
                    problems.append(f"{entry['label']}: {field} {label(target)} is neither supplied nor imported")

    # The evidence manifest: each run's report and log, each assessment's document.
    by_owner = defaultdict(list)
    for entry in records.values():
        if entry["kind"] == "evidence" and identity(entry["record"]["owner"]):
            by_owner[identity(entry["record"]["owner"])].append(entry)
    for key, entry in records.items():
        roles = Counter(e["record"]["role"] for e in by_owner[key])
        if entry["kind"] == "run":
            if roles["report"] != 1:
                problems.append(f"{entry['label']}: needs exactly one report entry, found {roles['report']}")
            if roles["log"] != 1:
                problems.append(f"{entry['label']}: needs exactly one log entry, its copy or its explicit absence; "
                                f"found {roles['log']}")
        elif entry["kind"] == "assessment" and roles["document"] != 1:
            problems.append(f"{entry['label']}: needs exactly one document entry, its copy or its explicit absence; "
                            f"found {roles['document']}")
        previous = imported.get(key)
        if previous and entry["kind"] in ("run", "assessment"):
            if sorted(e["key"] for e in by_owner[key]) != previous["document"].get("manifest"):
                problems.append(f"{entry['label']}: its evidence manifest changed since it was imported")
    sources, destinations = defaultdict(list), defaultdict(list)
    for entry in records.values():
        record = entry["record"]
        owner = identity(record["owner"]) if entry["kind"] == "evidence" else None
        if owner is None:
            continue
        if entry["new"] and owner not in records and owner in imported:
            problems.append(f"{entry['label']}: adds evidence to {label(owner)}, which was imported without it")
        if record["mode"] == "absent" or not isinstance(record.get("path"), str):
            continue
        sources[(owner, os.path.normpath(record["path"]))].append(entry["label"])
        if entry["new"] and record["mode"] == "copy":
            entry["destination"] = f"{local_id(owner)}/{os.path.basename(record['path'])}"
            destinations[entry["destination"]].append(entry["label"])
    for (owner, path), names in sources.items():
        if len(names) > 1:
            problems.append(f"{', '.join(names)}: name the same source file {path} of {label(owner)}")
    for destination, names in destinations.items():
        if len(names) > 1:
            problems.append(f"{', '.join(names)}: map to the same destination {destination}")

    # Matching: a new run attaches to the suite its target names at the upstream head, or is archived.
    attached, archived, newest = defaultdict(list), [], {}
    for entry in new:
        if entry["kind"] != "run":
            continue
        record = entry["record"]
        finished = instant(record["finished"])
        if record["target"] in suites:
            entry["suite"] = record["target"]
            attached[record["target"]].append(entry["label"])
            if finished and (record["target"] not in newest or finished > newest[record["target"]][0]):
                newest[record["target"]] = (finished, entry["label"])
        else:
            entry["suite"] = None
            archived.append(entry["label"])
    for entry in new:
        if entry["kind"] == "observation" and identity(entry["record"]["run"]) and isinstance(
                entry["record"]["number"], int):
            entry["local"] = f"{local_id(identity(entry['record']['run']))}/OBS-{entry['record']['number']:03}"
        elif entry["kind"] != "observation":
            entry["local"] = local_id(entry["key"])
    planned = dict(new=new, unchanged=dict(unchanged), attached=dict(attached), archived=archived, newest=newest,
                   records=records)
    return planned | dict(problems=problems + conflicts(planned, ledger))


def conflicts(planned: dict, ledger: dict) -> list[str]:
    """Problems that depend on the ledger's current state; checked again inside the commit."""
    problems = []
    for entry in planned["new"]:
        if entry["key"] in ledger["imported"]:
            problems.append(f"{entry['label']}: was imported meanwhile")
    for suite, (finished, name) in sorted(planned["newest"].items()):
        last = (ledger["suites"].get(suite) or {}).get("last_test_run")
        if last and (instant(last) is None or instant(last) >= finished):
            problems.append(f"suite {suite}: the ledger's last $test run {last} is not earlier than the newest "
                            f"imported run {name} ({_utc(finished)})")
    targets = defaultdict(list)
    for entry in planned["new"]:
        if entry["kind"] == "proposal" and isinstance(entry["record"]["target"], str):
            targets[entry["record"]["target"]].append(entry["label"])
    for target, names in sorted(targets.items()):
        if len(names) > 1:
            problems.append(f"{', '.join(names)}: several proposals for one target {target}")
        if target in ledger["proposals"]:
            problems.append(f"{', '.join(names)}: target {target} already has proposal {ledger['proposals'][target]}")
    observations = defaultdict(list)
    for entry in planned["new"]:
        if entry["kind"] == "observation" and entry.get("local"):
            observations[entry["local"]].append(entry["label"])
    for local, names in sorted(observations.items()):
        if len(names) > 1 or local in ledger["observations"]:
            problems.append(f"{', '.join(names)}: observation {local} is already taken")
    return problems


# -- files -------------------------------------------------------------------------

def _signature(status: os.stat_result) -> tuple:
    return (status.st_dev, status.st_ino, status.st_size, status.st_mtime_ns, status.st_ctime_ns)


def read_copy(path: str, size: int, sink=None) -> str:
    """The SHA-256 of a promised copy, read once and bounded by its declared size; written to `sink` too."""
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        raise CopyFailed(f"{path}: missing") from None
    except OSError as error:
        raise CopyFailed(f"{path}: unreadable ({error.strerror or error})") from None
    if not stat.S_ISREG(before.st_mode):
        raise CopyFailed(f"{path}: not a regular file")
    if before.st_size != size:
        raise CopyFailed(f"{path}: declared {size} bytes, but it has {before.st_size}")
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except OSError as error:
        raise CopyFailed(f"{path}: unreadable ({error.strerror or error})") from None
    try:
        opened = os.fstat(descriptor)
        hasher, total = hashlib.sha256(), 0
        while total <= size:
            try:
                block = _read(descriptor, min(CHUNK, size + 1 - total))
            except OSError as error:
                raise CopyFailed(f"{path}: unreadable ({error.strerror or error})") from None
            if not block:
                break
            total += len(block)
            hasher.update(block)
            if sink is not None:
                sink.write(block)  # a failure to write the copy is the import's own, not the source's
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    try:
        now = os.lstat(path)
    except OSError:
        now = None
    if (total != size or _signature(opened) != _signature(before) or _signature(after) != _signature(before)
            or now is None or _signature(now) != _signature(before)):
        raise CopyFailed(f"{path}: changed while being read")
    return hasher.hexdigest()


def _copy(path: str, size: int, staged: Path) -> str:
    staged.parent.mkdir(parents=True, exist_ok=True)
    with staged.open("xb") as sink:
        sha = read_copy(path, size, sink)
        sink.flush()
        os.fsync(sink.fileno())
    return sha


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def recover(state, directory: Path) -> list[str]:
    """Settle every interrupted import's leftovers. Never deletes a path an import did not create."""
    staging = directory / ".staging"
    settled = []
    for stage in sorted(staging.iterdir()) if staging.is_dir() else []:
        if stage.is_dir():
            _settle(state, directory, stage.name)
            settled.append(stage.name)
    return settled


def _settle(state, directory: Path, import_id: str) -> None:
    """Keep a committed import's files; remove an uncommitted one's, as its journal lists them."""
    stage = directory / ".staging" / import_id
    if not state.import_committed(import_id):
        try:
            destinations = json.loads((stage / "journal.json").read_text())["destinations"]
        except (OSError, ValueError, KeyError, TypeError):
            destinations = []  # the journal precedes every copy, so nothing was staged or published
        final = directory / import_id
        for relative in destinations:
            if Path(relative).is_absolute() or ".." in Path(relative).parts:
                continue
            target = final / relative
            if target.is_file() and not target.is_symlink():
                target.unlink()
            parent = target.parent
            while parent != final.parent and parent.is_dir():
                try:
                    parent.rmdir()  # only an empty directory: never anything else
                except OSError:
                    break
                parent = parent.parent
    shutil.rmtree(stage, ignore_errors=True)


def _discard(state, directory: Path, import_id: str) -> None:
    _settle(state, directory, import_id)


# -- the import ------------------------------------------------------------------

def run(lab) -> dict:
    """One owner-run import of the adapter's legacy history at the upstream head."""
    directory = lab.directory / "imported"
    with lab._file_lock("import.lock"):
        recovered = recover(lab.state, directory)
        upstream_ref, revision = lab.upstream()
        checkout = lab.checkout(revision)
        adapter, declared = lab.load(checkout, revision)
        suites = {s.id: s for s in declared}
        base = dict(revision=revision, recovered=recovered)
        hook = getattr(adapter, "legacy_history", None)
        if hook is None:
            return base | dict(outcome="refused", problems=[
                f"the adapter at {revision[:12]} supplies no legacy_history hook"])
        try:
            history = hook(adapters.Context(checkout, revision, None, None, lab.log))
        except LabError:
            raise
        except Exception as error:
            raise LabError(f"adapter legacy_history failed: {type(error).__name__}: {error}") from error
        records, problems = validate(history)
        if problems:
            return base | dict(outcome="refused", problems=problems)
        ledger = lab.state.import_view()
        planned = plan(records, ledger, suites)
        problems = planned["problems"]
        for entry in records.values():
            record = entry["record"]
            if entry["kind"] == "evidence" and not entry["new"] and record["mode"] == "copy":
                recorded = ledger["evidence"][ledger["imported"][entry["key"]]["local_id"]]["sha256"]
                try:
                    if read_copy(record["path"], record["size"]) != recorded:
                        problems.append(f"{entry['label']}: {record['path']} changed since it was imported")
                except CopyFailed as error:
                    problems.append(f"{entry['label']}: {error}")
        if problems:
            return base | dict(outcome="refused", problems=problems)
        if not planned["new"]:
            return base | dict(outcome="unchanged", unchanged=planned["unchanged"])
        return base | _publish(lab, directory, planned, suites, revision, upstream_ref, adapter)


def _publish(lab, directory: Path, planned: dict, suites: dict, revision: str, upstream_ref: str, adapter) -> dict:
    import_id = "import-" + uuid.uuid4().hex[:12]
    at = utc()
    stage, final = directory / ".staging" / import_id, directory / import_id
    copies = [e for e in planned["new"] if e["kind"] == "evidence" and e["record"]["mode"] == "copy"]
    destinations = [e["destination"] for e in copies] + [REPORT_NAME]
    try:
        stage.mkdir(parents=True)
        atomic_json(stage / "journal.json", dict(import_id=import_id, destinations=destinations))
        # Recovery finds the import only through its journal: make the stage's own entry durable first.
        for folder in (stage.parent, directory, directory.parent):
            _fsync_directory(folder)
        for entry in copies:
            record = entry["record"]
            entry["sha256"] = _copy(record["path"], record["size"], stage / "files" / entry["destination"])
            _checkpoint("staged")
        document = _report(import_id, at, revision, upstream_ref, adapter, planned, suites)
        atomic_json(stage / "files" / REPORT_NAME, document)
        final.mkdir()
        for relative in destinations:
            (final / relative).parent.mkdir(parents=True, exist_ok=True)
            os.rename(stage / "files" / relative, final / relative)
            _checkpoint("published")
        # Every directory entry the import created, up to the lab directory, is durable before the commit.
        for folder in {(final / relative).parent for relative in destinations} | {final, directory,
                                                                                  directory.parent}:
            _fsync_directory(folder)
        _checkpoint("commit")
        _commit(lab.state, planned, suites, import_id, at, revision, document, stage)
    except CopyFailed as error:
        _discard(lab.state, directory, import_id)
        return dict(outcome="refused", problems=[str(error)])
    except Refused as refusal:
        _discard(lab.state, directory, import_id)
        return dict(outcome="refused", problems=refusal.problems)
    except BaseException:
        _discard(lab.state, directory, import_id)
        raise
    _checkpoint("committed")
    shutil.rmtree(stage, ignore_errors=True)
    return document | dict(outcome="imported", report=str(final / REPORT_NAME))


def _report(import_id, at, revision, upstream_ref, adapter, planned, suites) -> dict:
    """The import's own report, published beside its evidence and exported with it."""
    new = planned["new"]
    evidence = [e for e in new if e["kind"] == "evidence"]
    return dict(
        schema=REPORT_SCHEMA, import_id=import_id, at=at, revision=revision, upstream_ref=upstream_ref,
        adapter=str(getattr(adapter, "name", "")),
        imported={kind: [e["label"] for e in new if e["kind"] == kind] for kind in KINDS},
        unchanged=planned["unchanged"], attached=planned["attached"], archived=planned["archived"],
        freshness={suite: dict(last_test_run=_utc(finished), last_test_identity=suites[suite].identity, run=name)
                   for suite, (finished, name) in sorted(planned["newest"].items())},
        copied=[dict(evidence=e["label"], owner=label(identity(e["record"]["owner"])), role=e["record"]["role"],
                     source_path=e["record"]["path"], size=e["record"]["size"], sha256=e["sha256"],
                     destination=f"imported/{import_id}/{e['destination']}")
                for e in evidence if e["record"]["mode"] == "copy"],
        absent=[dict(evidence=e["label"], owner=label(identity(e["record"]["owner"])), role=e["record"]["role"])
                for e in evidence if e["record"]["mode"] == "absent"],
        excluded_from_export=[dict(evidence=e["label"], owner=label(identity(e["record"]["owner"])),
                                   role=e["record"]["role"], source_path=e["record"]["path"],
                                   size=e["record"]["size"], reason=EXCLUDED)
                              for e in evidence if e["record"]["mode"] == "reference"])


def _commit(state, planned, suites, import_id, at, revision, document, stage: Path) -> None:
    """Every row of the import in one transaction, after checking the ledger again inside it.

    On a repository with no ledger the transaction builds a new one inside the import's stage, and the
    commit is linking it into place: until then a failure or an interruption leaves no ledger at all."""
    records = planned["records"]
    built = stage / "ledger.sqlite3" if state.uncreated else None
    if built:
        state.build(built)
    db = state.db
    with state.transaction():
        try:
            state.upgrade()
        except sqlite3.Error as error:
            raise LabError(f"cannot migrate the ledger to schema {SCHEMA} ({error}); it is unchanged") from error
        problems = conflicts(planned, state.import_view())
        if problems:
            raise Refused(problems)
        db.execute("INSERT INTO imports VALUES(?,?,?,?,?)", (import_id, at, revision,
                                                              f"imported/{import_id}/{REPORT_NAME}",
                                                              json.dumps(document, sort_keys=True)))
        ledger = {key: row["local_id"] for key, row in state.import_view()["imported"].items()}
        locals_ = ledger | {e["key"]: e["local"] for e in planned["new"]}
        manifests = defaultdict(list)
        for entry in records.values():
            if entry["kind"] == "evidence":
                manifests[identity(entry["record"]["owner"])].append(entry["key"])
        order = {kind: n for n, kind in enumerate(("run", "assessment", "observation", "proposal", "evidence"))}
        for entry in sorted(planned["new"], key=lambda e: order[e["kind"]]):
            record, key, local = entry["record"], entry["key"], entry["local"]
            source = record["source"]
            marker = dict(identity=source, import_id=import_id, at=at)
            if entry["kind"] == "run":
                suite = entry["suite"]
                marker |= dict(target=record["target"], archived=suite is None,
                               suite_identity=suites[suite].identity if suite else None)
                db.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (
                    local, "imported", suite, record["revision"], f"imported:{source['store']}", 0,
                    _utc(instant(record["started"])), _utc(instant(record["finished"])), record["status"],
                    instant(record["finished"]).timestamp(), json.dumps(dict(imported=marker, record=record),
                                                                        sort_keys=True),
                    json.dumps(dict(imported=True, interpretation=record.get("interpretation")), sort_keys=True)))
            elif entry["kind"] == "assessment":
                db.execute("INSERT INTO assessments VALUES(?,?,?,?,?,?)", (
                    local, "approved", f"imported:{source['store']}", json.dumps(dict(
                        imported=marker, record=record,
                        observations=[locals_[identity(o)] for o in record.get("observations", [])]),
                        sort_keys=True), _utc(instant(record["created"])), _utc(instant(record["created"]))))
            elif entry["kind"] == "observation":
                assessment = identity(record.get("assessment"))
                db.execute("INSERT INTO observations VALUES(?,?,?,?,?,?,?,?,?)", (
                    local, locals_[identity(record["run"])], record["number"], record["title"], record.get("area"),
                    "assessed", locals_[assessment] if assessment else None,
                    json.dumps(dict(imported=marker, record=record, disposition=record["disposition"]),
                               sort_keys=True), at))
            elif entry["kind"] == "proposal":
                db.execute("INSERT INTO proposals VALUES(?,?,?,?,?,?)", (
                    local, record["target"], record["lane"], record["status"],
                    json.dumps(dict(imported=marker, record=record,
                                    refers=[locals_[identity(r)] for r in record.get("refers", [])]),
                               sort_keys=True), _utc(instant(record["created"]))))
            else:
                mode = record["mode"]
                destination = f"imported/{import_id}/{entry['destination']}" if mode == "copy" else None
                provenance = dict(record.get("provenance", {}), source_path=record.get("path"),
                                  size=record.get("size"))
                if mode == "copy":
                    provenance |= dict(destination=destination, sha256=entry["sha256"])
                elif mode == "reference":
                    provenance |= dict(export=EXCLUDED)
                else:
                    provenance |= dict(absent="this file never existed in the source history")
                db.execute("INSERT INTO evidence VALUES(?,?,?,?,?,?,?,?,?,?)", (
                    local, locals_[identity(record["owner"])], record["role"], mode, record.get("path"),
                    record.get("size"), destination, entry.get("sha256"), import_id,
                    json.dumps(dict(imported=marker, record=record, provenance=provenance), sort_keys=True)))
            db.execute("INSERT INTO imported VALUES(?,?,?,?,?,?)", (
                key, entry["kind"], local, import_id, digest(record),
                json.dumps(dict(manifest=sorted(manifests[key])) if entry["kind"] in ("run", "assessment") else {},
                           sort_keys=True)))
        for suite_id, (finished, _) in sorted(planned["newest"].items()):
            if not db.execute("SELECT 1 FROM suites WHERE id=?", (suite_id,)).fetchone():
                db.execute("INSERT INTO suites(id,document) VALUES(?,?)",
                           (suite_id, json.dumps(suites[suite_id].record(), sort_keys=True)))
            db.execute("UPDATE suites SET last_test_run=?, last_test_identity=? WHERE id=?",
                       (_utc(finished), suites[suite_id].identity, suite_id))
        state.event("imported", import_id, dict(revision=revision, imported={
            kind: n for kind, n in Counter(e["kind"] for e in planned["new"]).items()}))
    if built:
        _checkpoint("ledger")
        state.publish_built()
    state.version = state.schema()
