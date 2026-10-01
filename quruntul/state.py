"""The ledger: one SQLite database per clone, shared by every lane and worktree.

Every mutation is one BEGIN IMMEDIATE transaction. Completed trials, results and
observations are immutable. Markdown pages and result.json files are views
regenerated from this database; they are never read back as input.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import time
import uuid

from quruntul.common import LabError, text_hash, utc

SCHEMA = 2
STATUSES = ("new", "stable", "flaky", "failing", "fixing", "pending", "retired")
DEFAULT_LEASE = 600

# Schema 1: every lane's tables. A new ledger is created at schema 1 and then
# migrated, so a new ledger and a migrated one are built by the same statements.
BASE = (
    "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, document TEXT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS suites (
        id TEXT PRIMARY KEY, document TEXT NOT NULL, identity TEXT, revision TEXT,
        enumerated TEXT, last_test_run TEXT, last_test_identity TEXT)""",
    """CREATE TABLE IF NOT EXISTS tests (
        id TEXT PRIMARY KEY, suite TEXT NOT NULL, path TEXT NOT NULL, kind TEXT NOT NULL,
        status TEXT NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
        trials INTEGER NOT NULL DEFAULT 0, failures INTEGER NOT NULL DEFAULT 0,
        last_run TEXT, last_outcome TEXT, last_failure TEXT, status_changed TEXT NOT NULL,
        measured_revision TEXT, pr TEXT, note TEXT)""",
    "CREATE INDEX IF NOT EXISTS tests_suite ON tests(suite, status)",
    """CREATE TABLE IF NOT EXISTS runs (
        id TEXT PRIMARY KEY, lane TEXT NOT NULL, suite TEXT, revision TEXT NOT NULL,
        source_ref TEXT NOT NULL, upstream INTEGER NOT NULL, started TEXT NOT NULL,
        heartbeat TEXT NOT NULL, state TEXT NOT NULL, finished_epoch REAL,
        document TEXT NOT NULL, summary TEXT)""",
    """CREATE TABLE IF NOT EXISTS trials (
        run_id TEXT NOT NULL REFERENCES runs(id), number INTEGER NOT NULL,
        state TEXT NOT NULL, document TEXT NOT NULL, PRIMARY KEY(run_id, number))""",
    """CREATE TABLE IF NOT EXISTS results (
        run_id TEXT NOT NULL REFERENCES runs(id), number INTEGER NOT NULL,
        test_id TEXT NOT NULL, outcome TEXT NOT NULL, PRIMARY KEY(run_id, number, test_id))""",
    """CREATE TABLE IF NOT EXISTS claims (
        resource TEXT PRIMARY KEY, owner TEXT NOT NULL, lane TEXT NOT NULL,
        acquired TEXT NOT NULL, heartbeat REAL NOT NULL, lease REAL NOT NULL, document TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS observations (
        id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id), number INTEGER NOT NULL,
        title TEXT NOT NULL, area TEXT, status TEXT NOT NULL, assessment TEXT,
        document TEXT NOT NULL, created TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS assessments (
        id TEXT PRIMARY KEY, status TEXT NOT NULL, owner TEXT NOT NULL,
        document TEXT NOT NULL, created TEXT NOT NULL, updated TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS deferrals (
        id TEXT PRIMARY KEY, reason TEXT NOT NULL, resume_when TEXT NOT NULL, created TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS proposals (
        id TEXT PRIMARY KEY, target_id TEXT UNIQUE NOT NULL, lane TEXT NOT NULL,
        status TEXT NOT NULL, document TEXT NOT NULL, created TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY, at TEXT NOT NULL, kind TEXT NOT NULL,
        subject TEXT NOT NULL, document TEXT NOT NULL)""",
)

# Each migration upgrades the schema before it by one, inside one transaction.
# Plain CREATE TABLE, so a ledger it cannot apply to fails and is left as it was.
MIGRATIONS = {
    # Legacy history imports (docs/design.md, Legacy history import).
    2: (
        """CREATE TABLE imports (
            id TEXT PRIMARY KEY, at TEXT NOT NULL, revision TEXT NOT NULL, report TEXT NOT NULL,
            document TEXT NOT NULL)""",
        """CREATE TABLE imported (
            identity TEXT PRIMARY KEY, kind TEXT NOT NULL, local_id TEXT NOT NULL,
            import_id TEXT NOT NULL REFERENCES imports(id), digest TEXT NOT NULL, document TEXT NOT NULL)""",
        """CREATE TABLE evidence (
            id TEXT PRIMARY KEY, owner TEXT NOT NULL, role TEXT NOT NULL, mode TEXT NOT NULL,
            source_path TEXT, size INTEGER, destination TEXT, sha256 TEXT,
            import_id TEXT NOT NULL REFERENCES imports(id), document TEXT NOT NULL)""",
    ),
}


class State:
    def __init__(self, directory: Path, migrate: bool = True):
        """Open the ledger. An older schema is migrated in place unless `migrate` is false,
        which leaves it for a caller that migrates inside its own transaction."""
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.directory / "ledger.sqlite3", timeout=30, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout=30000")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if not 0 <= self.version <= SCHEMA:
            raise LabError(f"unsupported ledger schema {self.version}; this quruntul supports {SCHEMA}")
        if self.version == 0:
            with self.transaction():
                for statement in BASE:
                    self.db.execute(statement)
                self.db.execute("PRAGMA user_version=1")
                self.upgrade()
            self.version = SCHEMA
        elif self.version < SCHEMA and migrate:
            self.migrate()
        self.db.execute("PRAGMA foreign_keys=ON")

    # -- schema ---------------------------------------------------------------

    def migrate(self) -> None:
        """Upgrade an older ledger in place: every migration in one transaction, or none of them.

        Idempotent: a ledger already at this schema is left alone. It never resets a ledger."""
        previous = self.version
        if previous == SCHEMA:
            return
        try:
            with self.transaction():
                self.upgrade()
        except sqlite3.Error as error:
            raise LabError(f"cannot migrate the ledger from schema {previous} to {SCHEMA} ({error}); "
                           "it is unchanged") from error
        self.version = SCHEMA

    def upgrade(self) -> None:
        """Apply the pending migrations inside the caller's transaction; the caller commits or rolls back."""
        if not self.db.in_transaction:
            raise LabError("migrations run inside a transaction")
        current = self.db.execute("PRAGMA user_version").fetchone()[0]
        for version in range(current + 1, SCHEMA + 1):
            for statement in MIGRATIONS[version]:
                self.db.execute(statement)
            self.db.execute(f"PRAGMA user_version={version}")

    # -- plumbing -----------------------------------------------------------

    @contextmanager
    def transaction(self):
        if self.db.in_transaction:
            yield
            return
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def event(self, kind: str, subject: str, document: dict) -> None:
        self.db.execute("INSERT INTO events(at,kind,subject,document) VALUES(?,?,?,?)",
                        (utc(), kind, subject, json.dumps(document, sort_keys=True)))

    def events(self, subject: str | None = None, limit: int = 200) -> list[dict]:
        query = "SELECT * FROM events" + (" WHERE subject=?" if subject else "") + " ORDER BY id DESC LIMIT ?"
        args = (subject, limit) if subject else (limit,)
        return [dict(r) | {"document": json.loads(r["document"])} for r in self.db.execute(query, args)]

    # -- claims -------------------------------------------------------------

    def claim(self, resource: str, owner: str, lane: str, document: dict | None = None,
              lease: float = DEFAULT_LEASE) -> dict | None:
        """Take a resource, or return the live holder. A stale claim is taken over."""
        now = time.time()
        with self.transaction():
            row = self.db.execute("SELECT * FROM claims WHERE resource=?", (resource,)).fetchone()
            if row and row["owner"] != owner and now - row["heartbeat"] < row["lease"]:
                return dict(row) | {"document": json.loads(row["document"])}
            if row and row["owner"] != owner:
                self.event("claim-takeover", resource, dict(previous=dict(row), owner=owner))
            self.db.execute("INSERT OR REPLACE INTO claims VALUES(?,?,?,?,?,?,?)",
                            (resource, owner, lane, utc(), now, lease, json.dumps(document or {})))
        return None

    def heartbeat(self, owner: str) -> int:
        with self.transaction():
            return self.db.execute("UPDATE claims SET heartbeat=? WHERE owner=?", (time.time(), owner)).rowcount

    def release(self, owner: str, resource: str | None = None) -> int:
        with self.transaction():
            if resource:
                return self.db.execute("DELETE FROM claims WHERE owner=? AND resource=?", (owner, resource)).rowcount
            return self.db.execute("DELETE FROM claims WHERE owner=?", (owner,)).rowcount

    def claims(self, live_only: bool = True) -> list[dict]:
        now = time.time()
        rows = [dict(r) | {"document": json.loads(r["document"])} for r in self.db.execute("SELECT * FROM claims")]
        return [r for r in rows if not live_only or now - r["heartbeat"] < r["lease"]]

    def held(self, resource: str) -> dict | None:
        row = next((c for c in self.claims() if c["resource"] == resource), None)
        return row

    # -- suites and tests ----------------------------------------------------

    def suite(self, suite_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM suites WHERE id=?", (suite_id,)).fetchone()
        return dict(row) | {"document": json.loads(row["document"])} if row else None

    def suites(self) -> list[dict]:
        return [dict(r) | {"document": json.loads(r["document"])} for r in self.db.execute("SELECT * FROM suites ORDER BY id")]

    def declare_suite(self, suite: dict) -> None:
        with self.transaction():
            row = self.db.execute("SELECT id FROM suites WHERE id=?", (suite["id"],)).fetchone()
            if row:
                self.db.execute("UPDATE suites SET document=? WHERE id=?", (json.dumps(suite, sort_keys=True), suite["id"]))
            else:
                self.db.execute("INSERT INTO suites(id,document) VALUES(?,?)", (suite["id"], json.dumps(suite, sort_keys=True)))

    def enumerated(self, suite: dict, identity: str, revision: str, paths: list[str], upstream: bool) -> dict:
        """Record what the suite contains at a revision. New paths become `new`.

        Only an enumeration of the upstream head retires tests that vanished, and
        a retired test that reappears is `new` again: it is different code now.
        """
        now = utc()
        added, retired, revived = [], [], []
        with self.transaction():
            self.declare_suite(suite)
            present = {f"{suite['id']}::{p}": p for p in paths}
            known = {r["id"]: dict(r) for r in self.db.execute("SELECT * FROM tests WHERE suite=?", (suite["id"],))}
            for test_id, path in present.items():
                row = known.get(test_id)
                if row is None:
                    self.db.execute(
                        "INSERT INTO tests(id,suite,path,kind,status,first_seen,last_seen,status_changed) VALUES(?,?,?,?,?,?,?,?)",
                        (test_id, suite["id"], path, suite["kind"], "new", now, now, now))
                    added.append(test_id)
                elif row["status"] == "retired" and upstream:
                    self._status(test_id, "new", "reappeared at the upstream head", {})
                    self.db.execute("UPDATE tests SET last_seen=? WHERE id=?", (now, test_id))
                    revived.append(test_id)
                else:
                    self.db.execute("UPDATE tests SET last_seen=?, kind=? WHERE id=?", (now, suite["kind"], test_id))
            if upstream:
                # A test that was pending everywhere, or failed every trial, is
                # queued again once its suite's inputs change: it may run in
                # this environment now, or the harness or code that failed it
                # may have been repaired.
                previous = self.db.execute("SELECT identity FROM suites WHERE id=?", (suite["id"],)).fetchone()
                if previous and previous["identity"] and previous["identity"] != identity:
                    for test_id, row in known.items():
                        if row["status"] in ("pending", "failing") and test_id in present:
                            self._status(test_id, "new", f"suite inputs changed since it was {row['status']}",
                                         {"revision": revision})
                for test_id, row in known.items():
                    if test_id not in present and row["status"] != "retired":
                        self._status(test_id, "retired", "no longer enumerated at the upstream head", {"revision": revision})
                        retired.append(test_id)
                self.db.execute("UPDATE suites SET identity=?, revision=?, enumerated=? WHERE id=?",
                                (identity, revision, now, suite["id"]))
            if added:
                self.event("enumerated", suite["id"], dict(revision=revision, added=len(added)))
        return dict(added=added, retired=retired, revived=revived)

    def tests(self, suite: str | None = None, status: str | None = None) -> list[dict]:
        query, args = "SELECT * FROM tests WHERE 1=1", []
        if suite:
            query += " AND suite=?"
            args.append(suite)
        if status:
            query += " AND status=?"
            args.append(status)
        return [dict(r) for r in self.db.execute(query + " ORDER BY id", args)]

    def test(self, test_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM tests WHERE id=?", (test_id,)).fetchone()
        return dict(row) if row else None

    def _status(self, test_id: str, status: str, reason: str, evidence: dict) -> None:
        if status not in STATUSES:
            raise LabError(f"unknown status {status!r}")
        row = self.db.execute("SELECT status FROM tests WHERE id=?", (test_id,)).fetchone()
        if row is None:
            raise LabError(f"unknown test {test_id!r}")
        if row["status"] == status:
            return
        self.db.execute("UPDATE tests SET status=?, status_changed=? WHERE id=?", (status, utc(), test_id))
        self.event("status", test_id, dict(previous=row["status"], status=status, reason=reason, evidence=evidence))

    def set_status(self, test_id: str, status: str, reason: str, evidence: dict | None = None,
                   pr: str | None = None) -> None:
        if not reason.strip():
            raise LabError("a status change needs a reason")
        with self.transaction():
            self._status(test_id, status, reason, evidence or {})
            if pr is not None:
                self.db.execute("UPDATE tests SET pr=? WHERE id=?", (pr, test_id))

    def note(self, test_id: str, note: str) -> None:
        with self.transaction():
            if not self.db.execute("UPDATE tests SET note=? WHERE id=?", (note, test_id)).rowcount:
                raise LabError(f"unknown test {test_id!r}")
            self.event("note", test_id, dict(note=note))

    # -- runs ---------------------------------------------------------------

    def begin(self, lane: str, suite: str | None, revision: str, source_ref: str, upstream: bool,
              document: dict) -> str:
        identifier = str(uuid.uuid4())
        with self.transaction():
            self.db.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                            (identifier, lane, suite, revision, source_ref, int(upstream), utc(), utc(),
                             "preparing", None, json.dumps(document, sort_keys=True), None))
        return identifier

    def run_heartbeat(self, identifier: str, owner: str | None = None) -> None:
        with self.transaction():
            self.db.execute("UPDATE runs SET heartbeat=? WHERE id=? AND finished_epoch IS NULL", (utc(), identifier))
            if owner:
                self.db.execute("UPDATE claims SET heartbeat=? WHERE owner=?", (time.time(), owner))

    def update_run(self, identifier: str, document: dict, state: str = "running") -> None:
        with self.transaction():
            self.db.execute("UPDATE runs SET document=?, state=?, heartbeat=? WHERE id=? AND finished_epoch IS NULL",
                            (json.dumps(document, sort_keys=True), state, utc(), identifier))

    def start_trial(self, identifier: str, number: int, document: dict) -> None:
        with self.transaction():
            self.db.execute("INSERT INTO trials VALUES(?,?,'running',?)", (identifier, number, json.dumps(document)))

    def finish_trial(self, identifier: str, number: int, result: dict, outcomes: dict[str, str]) -> None:
        with self.transaction():
            old = self.db.execute("SELECT state, document FROM trials WHERE run_id=? AND number=?",
                                  (identifier, number)).fetchone()
            if old is None:
                raise LabError("trial was never started")
            if old["state"] != "running":
                if json.loads(old["document"]) != result:
                    raise LabError("a completed trial is immutable")
                return
            self.db.execute("UPDATE trials SET state=?, document=? WHERE run_id=? AND number=?",
                            (result["outcome"], json.dumps(result), identifier, number))
            for test_id, outcome in outcomes.items():
                self.db.execute("INSERT OR IGNORE INTO results VALUES(?,?,?,?)", (identifier, number, test_id, outcome))

    def trials(self, identifier: str) -> list[dict]:
        return [dict(r) | {"document": json.loads(r["document"])} for r in self.db.execute(
            "SELECT * FROM trials WHERE run_id=? ORDER BY number", (identifier,))]

    def results(self, identifier: str) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM results WHERE run_id=? ORDER BY number, test_id",
                                                 (identifier,))]

    def finish(self, identifier: str, state: str, summary: dict) -> None:
        with self.transaction():
            row = self.db.execute("SELECT finished_epoch FROM runs WHERE id=?", (identifier,)).fetchone()
            if row is None:
                raise LabError("unknown run")
            if row[0] is not None:
                return
            self.db.execute("UPDATE runs SET state=?, summary=?, finished_epoch=?, heartbeat=? WHERE id=?",
                            (state, json.dumps(summary, sort_keys=True), time.time(), utc(), identifier))

    def run(self, identifier: str) -> dict:
        row = self.db.execute("SELECT * FROM runs WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise LabError(f"unknown run {identifier!r}")
        return (dict(row) | {"document": json.loads(row["document"]),
                             "summary": json.loads(row["summary"]) if row["summary"] else None,
                             "trials": self.trials(identifier), "results": self.results(identifier)})

    def runs(self, lane: str | None = None, limit: int = 50, active: bool | None = None) -> list[dict]:
        query, args = "SELECT * FROM runs WHERE 1=1", []
        if lane:
            query += " AND lane=?"
            args.append(lane)
        if active is True:
            query += " AND finished_epoch IS NULL"
        elif active is False:
            query += " AND finished_epoch IS NOT NULL"
        query += " ORDER BY started DESC LIMIT ?"
        args.append(limit)
        return [dict(r) | {"document": json.loads(r["document"]),
                           "summary": json.loads(r["summary"]) if r["summary"] else None}
                for r in self.db.execute(query, args)]

    def record_measurement(self, test_id: str, run_id: str, revision: str, trials: int, failures: int,
                           outcome: str) -> None:
        """Counters for one test after a batch; status decisions are separate."""
        with self.transaction():
            self.db.execute(
                "UPDATE tests SET trials=trials+?, failures=failures+?, last_run=?, last_outcome=?, "
                "last_failure=CASE WHEN ?>0 THEN ? ELSE last_failure END, measured_revision=? WHERE id=?",
                (trials, failures, utc(), outcome, failures, utc(), revision, test_id))

    def tested(self, suite_id: str, identity: str) -> None:
        with self.transaction():
            self.db.execute("UPDATE suites SET last_test_run=?, last_test_identity=? WHERE id=?",
                            (utc(), identity, suite_id))

    # -- observations and assessments ---------------------------------------

    def add_observations(self, run_id: str, observations: list[dict]) -> list[str]:
        ids = []
        with self.transaction():
            for number, observation in enumerate(observations, 1):
                identifier = f"{run_id}/OBS-{number:03}"
                existing = self.db.execute("SELECT document FROM observations WHERE id=?", (identifier,)).fetchone()
                if existing:
                    if json.loads(existing["document"]) != observation:
                        raise LabError(f"{identifier} is already recorded with different content")
                else:
                    self.db.execute("INSERT INTO observations VALUES(?,?,?,?,?,?,?,?,?)",
                                    (identifier, run_id, number, observation["title"], observation.get("area"),
                                     "open", None, json.dumps(observation, sort_keys=True), utc()))
                ids.append(identifier)
        return ids

    def observations(self, status: str | None = None, run_id: str | None = None) -> list[dict]:
        query, args = "SELECT * FROM observations WHERE 1=1", []
        if status:
            query += " AND status=?"
            args.append(status)
        if run_id:
            query += " AND run_id=?"
            args.append(run_id)
        return [dict(r) | {"document": json.loads(r["document"])} for r in self.db.execute(query + " ORDER BY created, id", args)]

    def claim_observations(self, owner: str, selected: list[str]) -> str:
        if not selected:
            raise LabError("no open observations match the selection")
        identifier = "assess-" + str(uuid.uuid4())[:8]
        with self.transaction():
            for observation in selected:
                row = self.db.execute("SELECT status FROM observations WHERE id=?", (observation,)).fetchone()
                if row is None or row["status"] != "open":
                    raise LabError(f"{observation} is not open")
                self.db.execute("UPDATE observations SET status='claimed', assessment=? WHERE id=?", (identifier, observation))
            self.db.execute("INSERT INTO assessments VALUES(?,?,?,?,?,?)",
                            (identifier, "claimed", owner, json.dumps(dict(observations=selected)), utc(), utc()))
            self.event("assessment-claimed", identifier, dict(owner=owner, observations=selected))
        return identifier

    def assessment(self, identifier: str) -> dict:
        row = self.db.execute("SELECT * FROM assessments WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise LabError(f"unknown assessment {identifier!r}")
        return dict(row) | {"document": json.loads(row["document"])}

    def assessments(self, status: str | None = None) -> list[dict]:
        query = "SELECT * FROM assessments" + (" WHERE status=?" if status else "") + " ORDER BY created DESC"
        return [dict(r) | {"document": json.loads(r["document"])} for r in self.db.execute(query, (status,) if status else ())]

    def propose_assessment(self, identifier: str, text: str) -> str:
        sha = text_hash(text)
        with self.transaction():
            record = self.assessment(identifier)
            if record["status"] not in ("claimed", "proposed"):
                raise LabError(f"assessment is {record['status']}")
            document = record["document"] | dict(proposal=text, proposal_sha256=sha)
            self.db.execute("UPDATE assessments SET status='proposed', document=?, updated=? WHERE id=?",
                            (json.dumps(document), utc(), identifier))
            self.event("assessment-proposed", identifier, dict(sha256=sha))
        return sha

    def approve_assessment(self, identifier: str, sha: str) -> dict:
        with self.transaction():
            record = self.assessment(identifier)
            if record["status"] != "proposed" or record["document"].get("proposal_sha256") != sha:
                raise LabError("approval must name the current proposed assessment's sha256")
            self.db.execute("UPDATE assessments SET status='approved', updated=? WHERE id=?", (utc(), identifier))
            self.db.execute("UPDATE observations SET status='assessed' WHERE assessment=?", (identifier,))
            self.event("assessment-approved", identifier, dict(sha256=sha))
        return self.assessment(identifier)

    def record_issue(self, identifier: str, finding: str, url: str) -> None:
        with self.transaction():
            record = self.assessment(identifier)
            if record["document"].get("imported"):
                raise LabError(f"{identifier} is imported legacy history; it is closed")
            if record["status"] not in ("approved", "filed"):
                raise LabError("issues are recorded only for an approved assessment")
            filed = record["document"].get("issues", {})
            if finding in filed and filed[finding] != url:
                raise LabError(f"{finding} already recorded as {filed[finding]}")
            filed[finding] = url
            document = record["document"] | dict(issues=filed)
            self.db.execute("UPDATE assessments SET status='filed', document=?, updated=? WHERE id=?",
                            (json.dumps(document), utc(), identifier))
            self.event("issue-filed", identifier, dict(finding=finding, url=url))

    def release_assessment(self, identifier: str, reason: str) -> None:
        with self.transaction():
            record = self.assessment(identifier)
            if record["status"] not in ("claimed", "proposed"):
                raise LabError(f"assessment is {record['status']}; only an unapproved one can be released")
            self.db.execute("UPDATE observations SET status='open', assessment=NULL WHERE assessment=?", (identifier,))
            self.db.execute("UPDATE assessments SET status='released', updated=? WHERE id=?", (utc(), identifier))
            self.event("assessment-released", identifier, dict(reason=reason))

    # -- deferrals and proposals --------------------------------------------

    def deferred(self) -> dict[str, dict]:
        return {r["id"]: dict(r) for r in self.db.execute("SELECT * FROM deferrals")}

    def defer(self, key: str, reason: str, resume_when: str) -> None:
        if not reason.strip() or not resume_when.strip():
            raise LabError("a deferral needs a concrete reason and an objective resume condition")
        with self.transaction():
            self.db.execute("INSERT OR REPLACE INTO deferrals VALUES(?,?,?,?)", (key, reason, resume_when, utc()))
            self.event("deferred", key, dict(reason=reason, resume_when=resume_when))

    def resume(self, key: str, evidence: str) -> None:
        if not evidence.strip():
            raise LabError("resuming needs evidence that the condition now holds")
        with self.transaction():
            if not self.db.execute("DELETE FROM deferrals WHERE id=?", (key,)).rowcount:
                raise LabError(f"{key} is not deferred")
            self.event("resumed", key, dict(evidence=evidence))

    def proposals(self, status: str | None = None) -> list[dict]:
        query = "SELECT * FROM proposals" + (" WHERE status=?" if status else "") + " ORDER BY created"
        return [dict(r) | {"document": json.loads(r["document"])} for r in self.db.execute(query, (status,) if status else ())]

    PROPOSAL_FIELDS = {"target_id", "lane", "question", "gap", "scenario", "oracle", "cost", "tier", "revision"}

    def propose(self, proposal: dict) -> tuple[str, bool]:
        if not isinstance(proposal, dict) or set(proposal) != self.PROPOSAL_FIELDS or not all(
                isinstance(v, str) and v.strip() for v in proposal.values()):
            raise LabError("a proposal needs exactly: " + ", ".join(sorted(self.PROPOSAL_FIELDS)))
        with self.transaction():
            existing = self.db.execute("SELECT id FROM proposals WHERE target_id=?", (proposal["target_id"],)).fetchone()
            if existing:
                return existing[0], False
            identifier = "prop-" + str(uuid.uuid4())[:8]
            self.db.execute("INSERT INTO proposals VALUES(?,?,?,?,?,?)",
                            (identifier, proposal["target_id"], proposal["lane"], "pending", json.dumps(proposal), utc()))
            self.event("proposed", identifier, proposal)
        return identifier, True

    def close_proposal(self, identifier: str, status: str, note: str) -> None:
        if status not in ("accepted", "rejected", "designed", "implemented", "superseded") or not note.strip():
            raise LabError("a proposal disposition needs a status and a nonblank note")
        with self.transaction():
            row = self.db.execute("SELECT document FROM proposals WHERE id=?", (identifier,)).fetchone()
            if row is None:
                raise LabError(f"unknown proposal {identifier!r}")
            if json.loads(row["document"]).get("imported"):
                raise LabError(f"{identifier} is imported legacy history; its decision is closed")
            self.db.execute("UPDATE proposals SET status=? WHERE id=?", (status, identifier))
            self.event("proposal-" + status, identifier, dict(note=note))

    # -- imported history ----------------------------------------------------

    def schema(self) -> int:
        """The schema on disk now; another process may have migrated it since this one opened."""
        return self.db.execute("PRAGMA user_version").fetchone()[0]

    def import_view(self) -> dict:
        """What an import checks itself against. A ledger not yet migrated has imported nothing."""
        imported, evidence = {}, {}
        if self.schema() >= 2:
            imported = {r["identity"]: dict(r) | {"document": json.loads(r["document"])}
                        for r in self.db.execute("SELECT * FROM imported")}
            evidence = {r["id"]: dict(r) for r in self.db.execute("SELECT * FROM evidence")}
        return dict(imported=imported, evidence=evidence,
                    proposals={r["target_id"]: r["id"] for r in self.db.execute("SELECT id, target_id FROM proposals")},
                    suites={r["id"]: dict(r) for r in self.db.execute("SELECT * FROM suites")},
                    observations={r[0] for r in self.db.execute("SELECT id FROM observations")})

    def import_committed(self, identifier: str) -> bool:
        return self.schema() >= 2 and self.db.execute(
            "SELECT 1 FROM imports WHERE id=?", (identifier,)).fetchone() is not None

    def imports(self) -> list[dict]:
        if self.schema() < 2:
            return []
        return [dict(r) | {"document": json.loads(r["document"])}
                for r in self.db.execute("SELECT * FROM imports ORDER BY at, id")]
