"""Legacy history import end to end, through the CLI, against fixture repositories.

The fixture adapter supplies history through its legacy_history hook, read from a
JSON file the test writes; the legacy store's files live in a scratch directory
outside the repository. No compiler, display or network is involved. Preservation
is checked on the ledger's schema, version and logical rows, and byte for byte on
every other file in the lab directory.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import io
import json
import os
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest import mock
import uuid
import zipfile

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from quruntul import cli, history  # noqa: E402
from quruntul.common import digest  # noqa: E402
from quruntul.state import State  # noqa: E402
from tests.test_lab import PROBE, LabFixture  # noqa: E402

HISTORY_ADAPTER = '''
import hashlib, json, os, sys
from pathlib import Path

class Fixture:
    name = "fixture"
    flake_trials = 3

    def suites(self, ctx):
        spec = (ctx.checkout / "spec.json").read_text()
        probe = (ctx.checkout / "probe.py").read_text()
        Suite, digest = ctx.Suite, ctx.digest
        return [Suite(id="unit", kind="ci", framework="hspec", description="fake hspec suite",
                      area="core", identity=digest(spec), trial_seconds=30, batch_seconds=300, priority=20),
                Suite(id="probe-a", kind="probe", framework="command", description="a slow observation",
                      area="lua", identity=digest(probe), checks=["alpha", "beta"],
                      trial_seconds=30, batch_seconds=300),
                Suite(id="probe-b", kind="probe", framework="command", description="never run here",
                      area="lua", identity=digest(["b", probe]), checks=["alpha", "beta"],
                      trial_seconds=30, batch_seconds=300)]

    def prepare(self, ctx, suite):
        if suite.framework == "hspec":
            exe = str(ctx.checkout / "fake_hspec.py")
            return ctx.Prepared(argv=[exe], cwd=str(ctx.checkout), environment={})
        return ctx.Prepared(argv=[sys.executable, str(ctx.checkout / "probe.py")], cwd=str(ctx.checkout), environment={})

    def legacy_history(self, ctx):
        if ctx.version < (0, 4, 0):
            raise RuntimeError("this adapter's legacy_history needs quruntul 0.4.0 or later")
        return json.loads(Path(os.environ["FIXTURE_HISTORY"]).read_text())

def adapter():
    return Fixture()
'''

STORE = "fixture:codex-test"


class Crash(BaseException):
    """An interruption no cleanup handler sees, like a killed process."""


def ref(kind, ident, store=STORE):
    return dict(store=store, kind=kind, id=ident)


class Legacy:
    """A legacy history, built record by record, whose files live in a scratch store."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.records = {name: [] for name, _ in history.LISTS}

    def document(self) -> dict:
        return json.loads(json.dumps(self.records))

    def evidence(self, ident, owner, role, mode, name=None, content=None, size=None):
        entry = dict(source=ref("evidence", ident), owner=owner, role=role, mode=mode)
        if mode != "absent":
            path = self.root / name
            if content is not None:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content if isinstance(content, bytes) else content.encode())
            entry.update(path=str(path), size=path.stat().st_size if size is None else size)
        self.records["evidence"].append(entry)
        return entry

    def run(self, ident, target, finished, log=True, **fields):
        record = dict(source=ref("run", ident), target=target, revision="legacy-" + ident,
                      started=fields.pop("started", finished), finished=finished, status="passed",
                      interpretation="clean", provenance=dict(registry="codex-test-coordinator/v1"))
        record.update(fields)
        self.records["runs"].append(record)
        self.evidence(f"{ident}/report", ref("run", ident), "report", "copy", f"{ident}/report.md", f"# {ident}\n")
        if log:
            self.evidence(f"{ident}/log", ref("run", ident), "log", "copy", f"{ident}/run.log", f"log of {ident}\n")
        else:
            self.evidence(f"{ident}/log", ref("run", ident), "log", "absent")
        return record

    def observation(self, ident, run, number=1, disposition="no action", assessment=None):
        record = dict(source=ref("observation", ident), run=ref("run", run), number=number,
                      title=f"observation {ident}", area="lua", disposition=disposition, document=dict(kind="product"))
        if assessment:
            record["assessment"] = ref("assessment", assessment)
        self.records["observations"].append(record)
        return record

    def assessment(self, ident, observations, document=True):
        record = dict(source=ref("assessment", ident), status="approved", created="2026-09-23T10:00:00Z",
                      observations=[ref("observation", o) for o in observations])
        self.records["assessments"].append(record)
        if document:
            self.evidence(f"{ident}/document", ref("assessment", ident), "document", "copy",
                          f"{ident}/assessment.md", f"## {ident}\n")
        else:
            self.evidence(f"{ident}/document", ref("assessment", ident), "document", "absent")
        return record

    def proposal(self, ident, target, status="designed", refers=(), store=STORE):
        record = dict(source=ref("proposal", ident, store), target=target, lane="test", status=status,
                      created="2026-09-10T10:00:00Z", refers=list(refers), document=dict(question="q"))
        self.records["proposals"].append(record)
        return record


def standard(root: Path) -> Legacy:
    """Four runs (two of probe-a, one of the never-declared probe-b, one archived), two observations,
    two assessments and two decided proposals, with every kind of evidence entry."""
    legacy = Legacy(root)
    legacy.run("r1", "probe-a", "2026-08-12T10:00:00Z")
    legacy.run("r2", "probe-a", "2026-08-20T10:00:00+02:00", status="failed", interpretation="observations")
    legacy.evidence("r2/screenshot", ref("run", "r2"), "other", "copy", "r2/screenshot.png", b"\x89PNG fixture")
    # A reference is never read: this file does not even exist.
    legacy.evidence("r2/capture", ref("run", "r2"), "other", "reference", "r2/capture.mp4", size=145_000_000)
    legacy.run("r3", "probe-b", "2026-09-01T12:00:00Z", log=False)
    legacy.run("r4", "playtest:walk", "2026-09-22T09:30:00Z")
    legacy.observation("o1", "r2", 1, "filed fixture#1", assessment="a1")
    legacy.observation("o2", "r4", 1, "no action", assessment="a2")
    legacy.assessment("a1", ["o1"])
    legacy.assessment("a2", ["o2"], document=False)
    legacy.proposal("p1", "probe:new-thing", "designed", refers=[ref("observation", "o1")])
    legacy.proposal("p2", "probe:old", "rejected")
    return legacy


class HistoryFixture(LabFixture):
    initialize = True  # create the ledger first, as any earlier lane would have

    def setUp(self):
        super().setUp()
        (self.work / ".quruntul" / "adapter.py").write_text(HISTORY_ADAPTER)
        self.push("adapter with legacy history")
        self.directory = self.work / ".git" / "quruntul"
        self.history_file = Path(self.temp.name) / "history.json"
        environment = mock.patch.dict(os.environ, {"FIXTURE_HISTORY": str(self.history_file)})
        environment.start()
        self.addCleanup(environment.stop)
        self.legacy_root = Path(self.temp.name) / "legacy"
        if self.initialize:
            self.cli("status")  # the ledger every snapshot compares against

    # -- helpers -------------------------------------------------------------

    def supply(self, legacy):
        document = legacy.document() if isinstance(legacy, Legacy) else legacy
        self.history_file.write_text(json.dumps(document))

    def importing(self, code=0):
        return self.cli("import-history", code=code)

    def refused(self, legacy, *expected):
        self.supply(legacy)
        result = self.importing(code=1)
        self.assertEqual(result["outcome"], "refused")
        text = "\n".join(result["problems"])
        for fragment in expected:
            self.assertIn(fragment, text)
        return result

    def native(self):
        """Native activity an import must leave alone: a flake batch with an open observation, a pending
        proposal, and a stale run that no one has recovered, with its evidence."""
        self.cli("flake", "--target", "unit")
        state = State(self.directory)
        try:
            stale = state.begin("test", "probe-a", "r", "origin/master", True, {})
            state.db.execute("UPDATE runs SET heartbeat='2026-01-01T00:00:00+00:00' WHERE id=?", (stale,))
            (self.directory / "runs" / stale).mkdir(parents=True)
            (self.directory / "runs" / stale / "trial-0001.log").write_text("partial\n")
            state.propose(dict(target_id="probe:native", lane="test", question="q", gap="g", scenario="s",
                               oracle="o", cost="c", tier="probe", revision="r"))
        finally:
            state.db.close()
        return stale

    def ledger(self) -> dict:
        db = sqlite3.connect(self.directory / "ledger.sqlite3")
        try:
            schema = sorted(db.execute("SELECT type, name, sql FROM sqlite_master").fetchall(), key=repr)
            rows = {name: sorted(map(tuple, db.execute(f"SELECT * FROM {name}")), key=repr)
                    for kind, name, _ in schema if kind == "table"}
            return dict(version=db.execute("PRAGMA user_version").fetchone()[0], schema=schema, rows=rows)
        finally:
            db.close()

    def files(self) -> dict:
        return {str(p.relative_to(self.directory)): p.read_bytes() for p in sorted(self.directory.rglob("*"))
                if p.is_file() and not p.name.startswith("ledger.sqlite3") and not p.name.endswith(".lock")}

    def assertUntouched(self, ledger, files, leftovers=False):
        """The ledger exactly as it was, no pre-existing file changed or deleted, and nothing new
        except an interrupted import's unpublished leftovers."""
        self.assertEqual(self.ledger(), ledger)
        now = self.files()
        for name, content in files.items():
            self.assertEqual(now.get(name), content, name)
        extra = set(now) - set(files)
        if leftovers:
            self.assertTrue(extra and all(n.startswith("imported/") for n in extra), extra)
        else:
            self.assertEqual(extra, set())

    def interrupted(self, point, nth=1):
        """Interrupt an import at a checkpoint, with no cleanup: what a killed process leaves."""
        calls = Counter()

        def checkpoint(name):
            calls[name] += 1
            if name == point and calls[name] == nth:
                raise Crash(point)
        with mock.patch.object(history, "_checkpoint", checkpoint), \
                mock.patch.object(history, "_discard", lambda *args: None), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), self.assertRaises(Crash):
            cli.main(["--repo", str(self.work), "import-history"])

    def export(self) -> dict:
        target = Path(self.temp.name) / f"export-{uuid.uuid4().hex[:8]}.zip"
        self.cli("export", "--output", str(target))
        with zipfile.ZipFile(target) as archive:
            return {name: archive.read(name) for name in archive.namelist()}

    def exported_ledger(self, archive) -> sqlite3.Connection:
        path = Path(self.temp.name) / f"exported-{uuid.uuid4().hex[:8]}.sqlite3"
        path.write_bytes(archive["ledger.sqlite3"])
        db = sqlite3.connect(path)
        db.row_factory = sqlite3.Row
        self.addCleanup(db.close)
        return db

    def _suites(self):
        state = State(self.directory)
        try:
            return state.suites()
        finally:
            state.db.close()


class ImportTests(HistoryFixture):
    def test_import_writes_closed_history_once_and_attaches_or_archives_each_run(self):
        stale = self.native()
        suites = {s["id"]: s for s in self._suites()}
        self.supply(standard(self.legacy_root))
        result = self.importing()
        self.assertEqual(result["outcome"], "imported")
        self.assertEqual({k: len(v) for k, v in result["imported"].items()},
                         dict(run=4, observation=2, assessment=2, proposal=2, evidence=12))
        self.assertEqual(result["attached"], {"probe-a": [f"{STORE}/run/r1", f"{STORE}/run/r2"],
                                              "probe-b": [f"{STORE}/run/r3"]})
        self.assertEqual(result["archived"], [f"{STORE}/run/r4"])

        state = State(self.directory)
        self.addCleanup(state.db.close)
        runs = {r["document"]["imported"]["identity"]["id"]: r for r in state.runs(lane="imported")}
        self.assertEqual({k: (r["suite"], r["state"]) for k, r in runs.items()},
                         {"r1": ("probe-a", "passed"), "r2": ("probe-a", "failed"), "r3": ("probe-b", "passed"),
                          "r4": (None, "passed")})
        self.assertTrue(all(r["finished_epoch"] for r in runs.values()))
        self.assertTrue(runs["r4"]["document"]["imported"]["archived"])
        self.assertEqual(runs["r2"]["document"]["record"]["revision"], "legacy-r2")
        self.assertEqual(runs["r2"]["document"]["record"]["provenance"], dict(registry="codex-test-coordinator/v1"))
        # Freshness: probe-a's newest run, at its identity at the upstream head; probe-b is declared, not enumerated.
        after = {s["id"]: s for s in state.suites()}
        self.assertEqual((after["probe-a"]["last_test_run"], after["probe-a"]["last_test_identity"]),
                         ("2026-08-20T08:00:00+00:00", digest(PROBE)))
        self.assertEqual((after["probe-b"]["last_test_run"], after["probe-b"]["enumerated"]),
                         ("2026-09-01T12:00:00+00:00", None))
        self.assertEqual(after["probe-b"]["last_test_identity"], digest(["b", PROBE]))
        self.assertEqual(state.tests(suite="probe-b"), [])
        self.assertEqual(after["unit"], suites["unit"])
        for field in ("document", "identity", "revision", "enumerated"):
            self.assertEqual(after["probe-a"][field], suites["probe-a"][field])
        # None of it enters a queue; the stale native run is not recovered.
        self.assertTrue(all(o["run_id"] not in {r["id"] for r in runs.values()} for o in state.observations("open")))
        self.assertEqual({o["status"] for o in state.observations() if o["id"].startswith("imported-")}, {"assessed"})
        self.assertEqual([p["target_id"] for p in state.proposals("pending")], ["probe:native"])
        self.assertEqual({p["target_id"]: p["status"] for p in state.proposals() if p["id"].startswith("imported-")},
                         {"probe:new-thing": "designed", "probe:old": "rejected"})
        self.assertEqual([r["id"] for r in state.runs(active=True)], [stale])
        assessment = next(a for a in state.assessments() if a["id"].startswith("imported-"))
        self.cli("assess", "issue", assessment["id"], "--finding", "F", "--url", "u", code=2)
        imported_proposal = next(p for p in state.proposals() if p["id"].startswith("imported-"))
        self.cli("proposal-close", imported_proposal["id"], "--status", "accepted", "--note", "n", code=2)

        # A second identical import changes nothing, even after later $test activity.
        ledger, files = self.ledger(), self.files()
        again = self.importing()
        self.assertEqual((again["outcome"], again["unchanged"]),
                         ("unchanged", dict(run=4, observation=2, assessment=2, proposal=2, evidence=12)))
        self.assertUntouched(ledger, files)
        self.cli("test", "--target", "probe-a", code=0)
        ledger, files = self.ledger(), self.files()
        self.assertEqual(self.importing()["outcome"], "unchanged")
        self.assertUntouched(ledger, files)

    def test_freshness_comes_from_the_newest_new_run_and_never_moves_back(self):
        legacy = standard(self.legacy_root)
        (self.work / "probe.py").write_text(PROBE + "# changed\n")
        self.push("change the probe")
        self.supply(legacy)
        self.importing()
        probe = {s["id"]: s for s in self._suites()}["probe-a"]
        self.assertEqual((probe["last_test_run"], probe["last_test_identity"]),
                         ("2026-08-20T08:00:00+00:00", digest(PROBE + "# changed\n")))
        legacy.run("r5", "probe-a", "2026-09-25T10:00:00Z")
        legacy.run("r6", "probe-a", "2026-09-24T10:00:00Z")
        self.supply(legacy)
        self.assertEqual(self.importing()["freshness"]["probe-a"]["last_test_run"], "2026-09-25T10:00:00+00:00")
        legacy.run("r7", "probe-a", "2026-09-25T12:00:00+02:00")  # the same instant as the ledger's
        self.refused(legacy, "suite probe-a: the ledger's last $test run 2026-09-25T10:00:00+00:00 is not earlier")

    def test_later_ledger_freshness_is_refused(self):
        self.cli("test", "--target", "probe-a")
        ledger, files = self.ledger(), self.files()
        self.refused(standard(self.legacy_root), "suite probe-a: the ledger's last $test run")
        self.assertUntouched(ledger, files)

    def test_a_changed_record_under_an_imported_identity_is_refused(self):
        legacy = standard(self.legacy_root)
        self.supply(legacy)
        self.importing()
        ledger, files = self.ledger(), self.files()
        changed = legacy.document()
        changed["observations"][1]["disposition"] = "filed fixture#2"
        self.refused(changed, f"{STORE}/observation/o2: changed since import")
        (self.legacy_root / "r1" / "run.log").write_text("log of r9\n")  # same size, other bytes
        self.refused(legacy, f"{STORE}/evidence/r1/log: {self.legacy_root / 'r1' / 'run.log'} changed since")
        (self.legacy_root / "r1" / "run.log").write_text("log of r1\n")
        legacy.evidence("r1/extra", ref("run", "r1"), "other", "copy", "r1/extra.txt", "x")
        self.refused(legacy, f"{STORE}/run/r1: its evidence manifest changed")
        without = legacy.document()
        without["runs"] = [r for r in without["runs"] if r["source"]["id"] != "r1"]
        without["evidence"] = [e for e in without["evidence"] if e["owner"]["id"] != "r1" or e["source"]["id"] == "r1/extra"]
        self.refused(without, f"{STORE}/evidence/r1/extra: adds evidence to {STORE}/run/r1, which was imported")
        self.assertUntouched(ledger, files)

    def test_duplicate_identities_are_refused_even_when_imported_before(self):
        legacy = standard(self.legacy_root)
        doubled = legacy.document()
        doubled["runs"].append(doubled["runs"][0])
        self.refused(doubled, f"{STORE}/run/r1: supplied twice (runs[0] and runs[4])")
        self.supply(legacy)
        self.importing()
        ledger, files = self.ledger(), self.files()
        self.refused(doubled, f"{STORE}/run/r1: supplied twice")
        self.assertUntouched(ledger, files)

    def test_malformed_records_statuses_timestamps_and_relations_are_refused(self):
        legacy = standard(self.legacy_root)
        document = legacy.document()
        document["runs"][0]["status"] = "exploded"
        document["runs"][1]["finished"] = "2026-08-20T10:00:00"  # no offset
        document["runs"][2]["colour"] = "blue"
        del document["runs"][3]["revision"]
        document["observations"][0]["run"] = ref("proposal", "p1")
        document["observations"][1]["number"] = 0
        document["assessments"][0]["observations"] = "o1"
        document["proposals"][0]["created"] = "yesterday"
        document["evidence"][0]["mode"] = "photocopy"
        document["evidence"][1]["path"] = "relative/run.log"
        document["proposals"].append(dict(source=dict(store=STORE, kind="run", id="p9")))
        # Values of the wrong type are refused, never used as keys, and a missing field hides nothing else.
        document["runs"][3]["status"] = []
        document["evidence"][2]["role"] = []
        document["evidence"][3]["mode"] = {"copy": True}
        document["evidence"][4]["size"] = "10"
        document["observations"][1]["disposition"] = 5
        document["assessments"][1]["status"] = ["approved"]
        document["proposals"][1]["status"] = None
        document["proposals"][1]["refers"] = "o1"
        ledger, files = self.ledger(), self.files()
        self.refused(document,
                     f"{STORE}/run/r1: status must be one of passed, failed, cancelled, error, not 'exploded'",
                     f"{STORE}/run/r2: finished must be an ISO 8601 timestamp with a UTC offset",
                     f"{STORE}/run/r3: unknown fields colour",
                     f"{STORE}/run/r4: missing revision",
                     f"{STORE}/observation/o1: run must reference a run",
                     f"{STORE}/observation/o2: number must be a positive integer",
                     f"{STORE}/assessment/a1: observations must be a list of references",
                     f"{STORE}/proposal/p1: created must be an ISO 8601 timestamp",
                     f"{STORE}/evidence/r1/report: mode must be one of copy, reference, absent",
                     f"{STORE}/evidence/r1/log: path must be an absolute path",
                     "proposals[2]: needs a source identity {store, kind: proposal, id}",
                     f"{STORE}/run/r4: status must be one of passed, failed, cancelled, error, not []",
                     f"{STORE}/evidence/r2/report: role must be one of report, log, document, other, not []",
                     f"{STORE}/evidence/r2/log: mode must be one of copy, reference, absent, not {{'copy': True}}",
                     f"{STORE}/evidence/r2/screenshot: size must be the file's size in bytes",
                     f"{STORE}/observation/o2: disposition must be nonblank text",
                     f"{STORE}/assessment/a2: status must be text, not ['approved']",
                     f"{STORE}/proposal/p2: status must be text, not None",
                     f"{STORE}/proposal/p2: refers must be a list of references")
        self.assertUntouched(ledger, files)

    def test_dangling_references_are_refused(self):
        legacy = standard(self.legacy_root)
        legacy.observation("o3", "r9")
        legacy.assessment("a3", ["o9"])
        legacy.proposal("p3", "probe:third", refers=[ref("assessment", "a9")])
        legacy.evidence("x/log", ref("run", "r8"), "other", "copy", "x/other.txt", "x")
        self.refused(legacy, f"{STORE}/observation/o3: run {STORE}/run/r9 is neither supplied nor imported",
                     f"{STORE}/assessment/a3: observations {STORE}/observation/o9 is neither",
                     f"{STORE}/proposal/p3: refers {STORE}/assessment/a9 is neither",
                     f"{STORE}/evidence/x/log: owner {STORE}/run/r8 is neither")
        # A reference to history imported before resolves.
        self.supply(standard(self.legacy_root))
        self.importing()
        later = Legacy(self.legacy_root)
        later.proposal("p4", "probe:fourth", refers=[ref("observation", "o1"), ref("run", "r4")])
        self.supply(later)
        self.assertEqual(self.importing()["imported"]["proposal"], [f"{STORE}/proposal/p4"])

    def test_duplicate_or_conflicting_evidence_is_refused(self):
        legacy = standard(self.legacy_root)
        legacy.evidence("r1/again", ref("run", "r1"), "other", "copy", "r1/run.log")
        legacy.evidence("r2/elsewhere", ref("run", "r2"), "other", "copy", "elsewhere/screenshot.png", b"other")
        self.refused(legacy, f"{STORE}/evidence/r1/log, {STORE}/evidence/r1/again: name the same source file",
                     f"{STORE}/evidence/r2/screenshot, {STORE}/evidence/r2/elsewhere: map to the same destination")

    def test_open_items_are_refused_by_name(self):
        legacy = standard(self.legacy_root)
        legacy.run("r5", "probe-a", "2026-09-25T10:00:00Z", status="running")
        legacy.observation("o3", "r1", disposition=None)
        legacy.proposal("p3", "probe:third", status="accepted")
        record = legacy.assessment("a3", [])
        record["status"] = "proposed"
        ledger, files = self.ledger(), self.files()
        self.refused(legacy, f"{STORE}/run/r5: open item: status 'running' is not terminal",
                     f"{STORE}/observation/o3: open item: the observation has no final disposition",
                     f"{STORE}/proposal/p3: open item: the proposal is 'accepted'",
                     f"{STORE}/assessment/a3: open item: the assessment is 'proposed', not approved")
        self.assertUntouched(ledger, files)

    def test_proposal_target_collisions_are_refused(self):
        self.native()
        legacy = standard(self.legacy_root)
        legacy.proposal("p3", "probe:native")
        self.refused(legacy, f"{STORE}/proposal/p3: target probe:native already has proposal prop-")
        legacy = standard(self.legacy_root)
        legacy.proposal("p3", "probe:shared")
        legacy.proposal("p4", "probe:shared")
        legacy.proposal("p5", "probe:shared", store="fixture:other-store")
        self.refused(legacy, f"{STORE}/proposal/p3, {STORE}/proposal/p4, fixture:other-store/proposal/p5: "
                             "several proposals for one target probe:shared")
        self.supply(standard(self.legacy_root))
        self.importing()
        later = Legacy(self.legacy_root)
        later.proposal("p6", "probe:new-thing", status="rejected")
        self.refused(later, f"{STORE}/proposal/p6: target probe:new-thing already has proposal imported-proposal-")

    def test_mandatory_evidence_is_copied_and_only_a_missing_file_may_be_declared_absent(self):
        legacy = standard(self.legacy_root)
        document = legacy.document()
        report, log = document["evidence"][0], document["evidence"][1]
        report["mode"], log["mode"] = "reference", "reference"
        assessment = next(e for e in document["evidence"] if e["role"] == "document" and e["mode"] == "copy")
        assessment["mode"] = "reference"
        absent = next(e for e in document["evidence"] if e["source"]["id"] == "r3/report")
        absent.update(mode="absent")
        del absent["path"], absent["size"]
        document["evidence"] = [e for e in document["evidence"] if e["source"]["id"] != "r4/log"]
        self.refused(document, f"{STORE}/evidence/r1/report: a run's report is always copied, never 'reference'",
                     f"{STORE}/evidence/r1/log: an existing run log is always copied",
                     f"{STORE}/evidence/a1/document: an existing assessment document is always copied",
                     f"{STORE}/evidence/r3/report: a run's report is always copied, never 'absent'")
        document = legacy.document()
        document["evidence"] = [e for e in document["evidence"]
                                if e["source"]["id"] not in ("r4/log", "r1/report", "a2/document")]
        self.refused(document, f"{STORE}/run/r4: needs exactly one log entry, its copy or its explicit absence",
                     f"{STORE}/run/r1: needs exactly one report entry, found 0",
                     f"{STORE}/assessment/a2: needs exactly one document entry, its copy or its explicit absence")
        # The standard history declares r3's log and a2's document absent: accepted, and recorded as absent.
        self.supply(legacy)
        result = self.importing()
        self.assertEqual({(a["evidence"], a["role"]) for a in result["absent"]},
                         {(f"{STORE}/evidence/r3/log", "log"), (f"{STORE}/evidence/a2/document", "document")})

    def test_a_missing_wrongly_sized_or_changing_promised_copy_refuses_the_import(self):
        legacy = standard(self.legacy_root)
        ledger, files = self.ledger(), self.files()
        log = self.legacy_root / "r1" / "run.log"
        log.unlink()
        self.refused(legacy, f"{log}: missing")
        log.write_text("log of r1 and more\n")
        self.refused(legacy, f"{log}: declared 10 bytes, but it has 19")
        log.unlink()
        log.mkdir()
        self.refused(legacy, f"{log}: not a regular file")
        log.rmdir()
        log.write_text("log of r1\n")
        original = os.read

        def growing(descriptor, size):
            if os.fstat(descriptor).st_ino == log.stat().st_ino:
                with log.open("a") as stream:
                    stream.write("!")
            return original(descriptor, size)
        with mock.patch.object(history, "_read", growing):
            self.refused(legacy, f"{log}: changed while being read")
        self.assertUntouched(ledger, files)

    def test_export_carries_copies_and_discloses_references_as_excluded(self):
        self.supply(standard(self.legacy_root))
        result = self.importing()
        archive = self.export()
        for copied in result["copied"]:
            content = archive[copied["destination"]]
            self.assertEqual(hashlib.sha256(content).hexdigest(), copied["sha256"])
            self.assertEqual(content, Path(copied["source_path"]).read_bytes())
        exported_report = json.loads(archive[f"imported/{result['import_id']}/import-report.json"])
        self.assertEqual(exported_report["excluded_from_export"], [dict(
            evidence=f"{STORE}/evidence/r2/capture", owner=f"{STORE}/run/r2", role="other",
            source_path=str(self.legacy_root / "r2" / "capture.mp4"), size=145_000_000, reason=history.EXCLUDED)])
        self.assertNotIn("capture.mp4", json.dumps(exported_report["copied"]))
        self.assertFalse([name for name in archive if name.endswith("capture.mp4")])
        db = self.exported_ledger(archive)
        row = db.execute("SELECT * FROM evidence WHERE mode='reference'").fetchone()
        self.assertEqual((row["destination"], row["sha256"]), (None, None))
        self.assertEqual(json.loads(row["document"])["provenance"]["export"], history.EXCLUDED)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM evidence WHERE mode='copy'").fetchone()[0], 9)

    def test_an_export_holds_an_import_whole_or_not_at_all(self):
        self.supply(standard(self.legacy_root))
        original = cli._imported_files

        def import_between(snapshot):
            from quruntul.lab import Lab
            lab = Lab(self.work, log=lambda message: None, migrate=False)
            try:
                self.assertEqual(lab.import_history()["outcome"], "imported")
            finally:
                lab.state.db.close()
            return original(snapshot)
        with mock.patch.object(cli, "_imported_files", import_between):
            archive = self.export()
        self.assertFalse([name for name in archive if name.startswith("imported/")])
        self.assertEqual(self.exported_ledger(archive).execute("SELECT COUNT(*) FROM runs").fetchone()[0], 0)
        archive = self.export()
        self.assertEqual(len([name for name in archive if name.startswith("imported/")]), 10)

    def test_an_adapter_supplying_the_hook_refuses_an_older_engine(self):
        self.supply(standard(self.legacy_root))
        ledger, files = self.ledger(), self.files()
        with mock.patch("quruntul.__version__", "0.3.0"):
            result = self.importing(code=2)
        self.assertIn("legacy_history needs quruntul 0.4.0", result["error"])
        self.assertUntouched(ledger, files)


class FreshRepositoryTests(HistoryFixture):
    initialize = False

    def ledger_files(self):
        return sorted(p.name for p in self.directory.glob("ledger.sqlite3*")) if self.directory.exists() else []

    def test_a_refused_first_import_leaves_no_ledger_behind(self):
        legacy = standard(self.legacy_root)
        refusing = legacy.document()
        refusing["proposals"][0]["status"] = "accepted"
        self.refused(refusing, "open item")
        self.assertEqual(self.ledger_files(), [])
        log = self.legacy_root / "r4" / "run.log"
        log.write_text("a log of another size\n")
        self.refused(legacy, f"{log}: declared")
        self.assertEqual(self.ledger_files(), [])
        self.assertFalse([p for p in (self.directory / "imported").rglob("*") if p.is_file()])
        log.write_text("log of r4\n")
        self.supply(legacy)
        self.assertEqual(self.importing()["outcome"], "imported")
        self.assertEqual(self.ledger()["version"], 2)
        self.assertEqual(len(self.ledger()["rows"]["imports"]), 1)


class AtomicityTests(HistoryFixture):
    def check(self, incremental):
        self.native()
        legacy = standard(self.legacy_root)
        newest = "r4"
        if incremental:
            self.supply(legacy)
            self.assertEqual(self.importing()["outcome"], "imported")
            legacy.run("r5", "probe-a", "2026-09-25T10:00:00Z")
            legacy.evidence("r5/trace", ref("run", "r5"), "other", "copy", "r5/trace.json", "{}")
            newest = "r5"
        attempt = legacy.document()
        ledger, files = self.ledger(), self.files()
        refusing = legacy.document()
        refusing["observations"].append(dict(Legacy(self.legacy_root).observation("o9", newest, 9, None)))

        with self.subTest("a refusal"):
            self.refused(refusing, "open item")
            self.assertUntouched(ledger, files)
        with self.subTest("a copy failure after other copies were staged"):
            failing = legacy.document()
            late = Legacy(self.legacy_root).evidence("late", ref("run", newest), "other", "copy", "late.bin", "late",
                                                     size=99)
            failing["evidence"].append(late)
            self.refused(failing, f"{self.legacy_root / 'late.bin'}: declared 99 bytes, but it has 4")
            self.assertUntouched(ledger, files)
        for point, nth in (("staged", 2), ("published", 2)):
            with self.subTest(f"an interruption once {nth} files are {point}"):
                self.supply(attempt)
                self.interrupted(point, nth)
                self.assertUntouched(ledger, files, leftovers=True)
                if point == "published":
                    published = [n for n in set(self.files()) - set(files) if not n.startswith("imported/.staging/")]
                    self.assertEqual(len(published), nth)
                # Unpublished leftovers are invisible to export.
                archive = self.export()
                self.assertEqual({n for n in archive if n.startswith("imported/")},
                                 {n for n in files if n.startswith("imported/")})
                files["ledger.md"] = self.files()["ledger.md"]  # regenerated by export, not by the import
                # Recovery, at the start of the next import: only the interrupted import's own files go.
                result = self.refused(refusing, "open item")
                self.assertEqual(len(result["recovered"]), 1)
                self.assertUntouched(ledger, files)
        self.supply(attempt)
        self.assertEqual(self.importing()["outcome"], "imported")

    def test_a_failed_first_import_leaves_everything_as_it_was(self):
        self.check(incremental=False)

    def test_a_failed_incremental_import_leaves_everything_as_it_was(self):
        self.check(incremental=True)

    def test_an_interruption_after_the_commit_keeps_the_complete_import(self):
        self.native()
        legacy = standard(self.legacy_root)
        for step in ("first", "incremental"):
            with self.subTest(step):
                if step == "incremental":
                    legacy.run("r5", "probe-a", "2026-09-25T10:00:00Z")
                self.supply(legacy)
                self.interrupted("committed")
                committed = self.ledger()
                [leftover] = os.listdir(self.directory / "imported" / ".staging")
                retry = self.importing()
                self.assertEqual((retry["outcome"], retry["recovered"]), ("unchanged", [leftover]))
                self.assertFalse(os.listdir(self.directory / "imported" / ".staging"))
                self.assertEqual(self.ledger(), committed)
                state = State(self.directory)
                try:
                    [latest] = [i for i in state.imports() if i["id"] == leftover]
                finally:
                    state.db.close()
                for copied in latest["document"]["copied"]:
                    content = (self.directory / copied["destination"]).read_bytes()
                    self.assertEqual(hashlib.sha256(content).hexdigest(), copied["sha256"])
        probe = {s["id"]: s for s in self._suites()}["probe-a"]
        self.assertEqual(probe["last_test_run"], "2026-09-25T10:00:00+00:00")

    def test_freshness_recorded_between_validation_and_commit_refuses_the_import(self):
        self.supply(standard(self.legacy_root))

        def concurrent(name):
            if name == "commit":
                other = State(self.directory)
                try:
                    other.declare_suite(dict(id="probe-a"))
                    other.tested("probe-a", "a concurrent $test")
                finally:
                    other.db.close()
        with mock.patch.object(history, "_checkpoint", concurrent):
            self.refused(standard(self.legacy_root), "suite probe-a: the ledger's last $test run")
        self.assertFalse([n for n in self.files() if n.startswith("imported/")])
        ledger = self.ledger()
        self.assertEqual(ledger["rows"]["imports"], [])
        self.assertEqual({s["id"]: s["last_test_identity"] for s in self._suites()}, {"probe-a": "a concurrent $test"})


class MigrationTests(HistoryFixture):
    def downgrade(self, *extra):
        """A populated ledger as the schema-1 engine left it."""
        self.native()
        db = sqlite3.connect(self.directory / "ledger.sqlite3")
        try:
            for table in ("evidence", "imported", "imports"):
                db.execute(f"DROP TABLE {table}")
            for statement in extra:
                db.execute(statement)
            db.execute("PRAGMA user_version=1")
            db.commit()
        finally:
            db.close()
        return self.ledger()

    def test_a_populated_ledger_migrates_in_place_once(self):
        before = self.downgrade()
        self.cli("tests")
        after = self.ledger()
        self.assertEqual(after["version"], 2)
        self.assertEqual({k: v for k, v in after["rows"].items() if k in before["rows"]}, before["rows"])
        self.assertEqual({k: after["rows"][k] for k in ("imports", "imported", "evidence")},
                         dict(imports=[], imported=[], evidence=[]))
        self.cli("tests")
        self.assertEqual(self.ledger(), after)

    def test_a_migration_that_cannot_apply_leaves_the_ledger_as_it_was(self):
        before = self.downgrade("CREATE TABLE imports (stray TEXT)")
        files = self.files()
        result = self.cli("tests", code=2)
        self.assertIn("cannot migrate the ledger from schema 1 to 2", result["error"])
        self.assertUntouched(before, files)

    def test_a_refused_or_interrupted_import_leaves_an_older_schema_unmigrated(self):
        before = self.downgrade()
        files = self.files()
        legacy = standard(self.legacy_root)
        refusing = legacy.document()
        refusing["proposals"][0]["status"] = "accepted"
        self.refused(refusing, "open item")
        self.assertUntouched(before, files)
        self.supply(legacy)
        self.interrupted("published", 3)
        self.assertUntouched(before, files, leftovers=True)
        self.refused(refusing, "open item")
        self.assertUntouched(before, files)
        self.supply(legacy)
        self.assertEqual(self.importing()["outcome"], "imported")
        after = self.ledger()
        self.assertEqual(after["version"], 2)
        for table, rows in before["rows"].items():
            if table in ("tests", "trials", "results", "claims", "deferrals", "metadata"):
                self.assertEqual(after["rows"][table], rows, table)
            elif table == "suites":  # only the matched suites' freshness changes
                self.assertEqual([r[:5] for r in after["rows"][table]], [r[:5] for r in rows])
            else:
                self.assertLessEqual(set(map(repr, rows)), set(map(repr, after["rows"][table])), table)


class WithoutHookTests(LabFixture):
    def test_an_adapter_without_the_hook_imports_nothing_and_changes_nothing(self):
        self.cli("flake", "--target", "unit")
        directory = self.work / ".git" / "quruntul"
        files = {p: p.read_bytes() for p in directory.rglob("*") if p.is_file() and "sqlite3" not in p.name}
        db = sqlite3.connect(directory / "ledger.sqlite3")
        rows = {t: sorted(map(tuple, db.execute(f"SELECT * FROM {t}")), key=repr)
                for (t,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        db.close()
        result = self.cli("import-history", code=1)
        self.assertEqual(result["problems"], [f"the adapter at {result['revision'][:12]} supplies no "
                                              "legacy_history hook"])
        db = sqlite3.connect(directory / "ledger.sqlite3")
        self.assertEqual({t: sorted(map(tuple, db.execute(f"SELECT * FROM {t}")), key=repr) for t in rows}, rows)
        db.close()
        self.assertEqual({p: p.read_bytes() for p in files}, files)
        self.assertFalse((directory / "imported").exists())


if __name__ == "__main__":
    unittest.main()
