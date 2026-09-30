"""quruntul: the command line every skill drives. Output is JSON on stdout."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
import zipfile
import os

from quruntul import select
from quruntul.common import LabError, command
from quruntul.lab import Lab, new_owner

LANES = ("flake", "deflake", "test", "playtest", "profile", "performance", "assess")


def resolve_arg(lab: Lab, lane: str, words: list[str]) -> dict:
    """Parse a skill's argument the same way for every skill (docs/design.md)."""
    iterations = 1
    rest = list(words)
    if rest and re.fullmatch(r"[0-9]+", rest[0]):
        iterations = int(rest.pop(0))
        if iterations < 1:
            raise LabError("an iteration count must be a positive integer")
    text = " ".join(rest).strip()
    result = dict(lane=lane, iterations=iterations, target=None, hint=None, text=text)
    if not text:
        return result
    tests = {t["id"]: t for t in lab.state.tests()}
    suites = {s["id"] for s in lab.state.suites()}
    if text in tests:
        result["target"] = dict(kind="test", id=text, status=tests[text]["status"])
        return result
    if text in suites:
        result["target"] = dict(kind="suite", id=text)
        return result
    lowered = text.lower()
    suite_hits = sorted(s for s in suites if lowered in s.lower())
    test_hits = sorted(t for t in tests if lowered in t.lower())
    if len(suite_hits) == 1 and lane != "test" or (lane == "test" and len(suite_hits) == 1 and not test_hits):
        result["target"] = dict(kind="suite", id=suite_hits[0], matched=text)
    elif not suite_hits and len(test_hits) == 1:
        result["target"] = dict(kind="test", id=test_hits[0], matched=text, status=tests[test_hits[0]]["status"])
    else:
        result["hint"] = text
        if suite_hits or test_hits:
            result["ambiguous"] = dict(suites=suite_hits[:20], tests=test_hits[:20])
    return result


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="quruntul", description=__doc__)
    p.add_argument("--repo", default=".", help="any path inside the repository")
    sub = p.add_subparsers(dest="op", required=True)

    sub.add_parser("status", help="summary of the ledger; regenerates ledger.md")
    t = sub.add_parser("tests", help="list tests")
    t.add_argument("--status")
    t.add_argument("--suite")
    r = sub.add_parser("resolve-arg", help="classify a skill argument: count, target or hint")
    r.add_argument("lane", choices=LANES)
    r.add_argument("words", nargs="*")

    f = sub.add_parser("flake", help="one flake batch")
    f.add_argument("--target", help="a suite or test id; default: the next suite with unmeasured tests")
    f.add_argument("--ref", help="a committed revision; anything but the upstream head is candidate evidence")
    f.add_argument("--trials", type=int)
    f.add_argument("--owner")

    sd = sub.add_parser("shakedown", help="one trial of every applicable suite at the upstream head; advisory")
    sd.add_argument("--target", help="one suite id; default: every suite that applies on this platform")
    sd.add_argument("--owner")

    tt = sub.add_parser("test", help="one probe execution")
    tt.add_argument("--target")
    tt.add_argument("--hint")
    tt.add_argument("--ref")
    tt.add_argument("--desktop", action="store_true", help="only after the owner approved this desktop session")
    tt.add_argument("--owner")

    rep = sub.add_parser("report", help="run reports")
    rep.add_argument("action", choices=["attach", "path", "show"])
    rep.add_argument("run")
    show = sub.add_parser("show", help="one run's full record")
    show.add_argument("run")
    runs = sub.add_parser("runs", help="recent runs")
    runs.add_argument("--lane")
    runs.add_argument("--limit", type=int, default=20)
    runs.add_argument("--active", action="store_true")

    m = sub.add_parser("mark", help="owner-directed status change (e.g. a stable test that failed in CI)")
    m.add_argument("test")
    m.add_argument("--status", required=True, choices=["flaky", "stable", "new"])
    m.add_argument("--reason", required=True)
    m.add_argument("--evidence", default="")
    n = sub.add_parser("note", help="attach a note (e.g. a diagnosis path) to a test")
    n.add_argument("test")
    n.add_argument("--text", required=True)

    d = sub.add_parser("deflake", help="claim, hand off or release a flaky test")
    d.add_argument("action", choices=["select", "pr", "release"])
    d.add_argument("test", nargs="?")
    d.add_argument("--owner", required=True)
    d.add_argument("--pr")
    d.add_argument("--reason")
    d.add_argument("--lease", type=float, default=1800)

    hb = sub.add_parser("heartbeat", help="keep an owner's claims alive")
    hb.add_argument("--owner", required=True)
    rl = sub.add_parser("release", help="release an owner's claims")
    rl.add_argument("--owner", required=True)
    rl.add_argument("--resource")
    sub.add_parser("claims", help="live claims")

    o = sub.add_parser("observations", help="observations from attached reports")
    o.add_argument("--status", choices=["open", "claimed", "assessed"])
    o.add_argument("--run")
    a = sub.add_parser("assess", help="observation assessments")
    a.add_argument("action", choices=["claim", "propose", "approve", "issue", "release", "show", "list"])
    a.add_argument("id", nargs="?")
    a.add_argument("--owner")
    a.add_argument("--run", action="append", default=[])
    a.add_argument("--observation", action="append", default=[])
    a.add_argument("--area")
    a.add_argument("--file")
    a.add_argument("--sha")
    a.add_argument("--finding")
    a.add_argument("--url")
    a.add_argument("--reason")
    a.add_argument("--status")

    df = sub.add_parser("defer")
    df.add_argument("id")
    df.add_argument("--reason", required=True)
    df.add_argument("--resume-when", required=True)
    rs = sub.add_parser("resume")
    rs.add_argument("id")
    rs.add_argument("--evidence", required=True)
    pr = sub.add_parser("propose", help="record a missing-test proposal from a JSON file")
    pr.add_argument("file")
    pc = sub.add_parser("proposal-close")
    pc.add_argument("id")
    pc.add_argument("--status", required=True, choices=["accepted", "rejected", "designed", "implemented", "superseded"])
    pc.add_argument("--note", required=True)
    pl = sub.add_parser("proposals")
    pl.add_argument("--status")

    ex = sub.add_parser("export", help="a portable archive of the ledger and all run evidence")
    ex.add_argument("--output", required=True)
    ck = sub.add_parser("checkouts", help="list lab checkouts; --prune removes inactive ones")
    ck.add_argument("--prune", action="store_true")
    ck.add_argument("--apply", action="store_true")
    return p


def run(args, lab: Lab):
    s = lab.state
    op = args.op
    if op == "status":
        return lab.status()
    if op == "tests":
        return s.tests(args.suite, args.status)
    if op == "resolve-arg":
        return resolve_arg(lab, args.lane, args.words)
    if op == "flake":
        owner = args.owner or new_owner("flake")
        try:
            return lab.flake(owner, args.target, args.ref, args.trials)
        finally:
            if not args.owner:
                s.release(owner)
    if op == "shakedown":
        owner = args.owner or new_owner("shakedown")
        try:
            return lab.shakedown(owner, args.target)
        finally:
            if not args.owner:
                s.release(owner)
    if op == "test":
        owner = args.owner or new_owner("test")
        try:
            return lab.test(owner, args.target, args.hint, args.ref, args.desktop)
        finally:
            if not args.owner:
                s.release(owner)
    if op == "report":
        if args.action == "attach":
            return lab.attach(args.run)
        path = lab.directory / "runs" / args.run / "report.md"
        return dict(path=str(path)) if args.action == "path" else dict(path=str(path), text=path.read_text())
    if op == "show":
        return s.run(args.run)
    if op == "runs":
        return [dict(id=r["id"], lane=r["lane"], suite=r["suite"], state=r["state"], started=r["started"],
                     revision=r["revision"], summary=r["summary"])
                for r in s.runs(args.lane, args.limit, True if args.active else None)]
    if op == "mark":
        test = s.test(args.test)
        if not test:
            raise LabError(f"unknown test {args.test!r}")
        s.set_status(args.test, args.status, args.reason, dict(evidence=args.evidence, by="owner"))
        lab.render()
        return dict(outcome="marked", test=args.test, previous=test["status"], status=args.status)
    if op == "note":
        s.note(args.test, args.text)
        return dict(outcome="noted", test=args.test)
    if op == "deflake":
        return deflake(args, lab)
    if op == "heartbeat":
        return dict(renewed=s.heartbeat(args.owner))
    if op == "release":
        return dict(released=s.release(args.owner, args.resource))
    if op == "claims":
        return s.claims()
    if op == "observations":
        return s.observations(args.status, args.run)
    if op == "assess":
        return assess(args, lab)
    if op == "defer":
        s.defer(args.id, args.reason, args.resume_when)
        return dict(outcome="deferred", id=args.id)
    if op == "resume":
        s.resume(args.id, args.evidence)
        return dict(outcome="resumed", id=args.id)
    if op == "propose":
        identifier, created = s.propose(json.loads(Path(args.file).read_text()))
        return dict(outcome="proposed" if created else "already-proposed", proposal=identifier)
    if op == "proposal-close":
        s.close_proposal(args.id, args.status, args.note)
        return dict(outcome=args.status, proposal=args.id)
    if op == "proposals":
        return s.proposals(args.status)
    if op == "export":
        return export(lab, Path(args.output))
    if op == "checkouts":
        found = lab.checkouts()
        if args.prune:
            for item in found:
                if item["active"]:
                    item["pruned"] = "kept: an active run uses it"
                elif args.apply:
                    try:
                        command(["git", "worktree", "remove", item["path"]], lab.root, timeout=300)
                        item["pruned"] = "removed"
                    except LabError as error:
                        item["pruned"] = f"kept: {error}"
                else:
                    item["pruned"] = "would remove (pass --apply)"
        return found
    raise LabError(f"unknown operation {op}")


def deflake(args, lab: Lab):
    s = lab.state
    if args.action == "select":
        claims = {c["resource"]: c for c in s.claims()}
        if args.test:
            test = s.test(args.test)
            if not test:
                raise LabError(f"unknown test {args.test!r}")
            if test["status"] not in ("flaky", "new", "stable"):
                raise LabError(f"{args.test} is {test['status']}; deflake works on flaky tests"
                               + (f" (PR {test['pr']})" if test["pr"] else ""))
            candidates = [test]
        else:
            candidates = select.deflake_order(s.tests(status="flaky"), claims, s.deferred(), args.owner)
        for test in candidates:
            holder = s.claim("test:" + test["id"], args.owner, "deflake", dict(status=test["status"]), args.lease)
            if holder:
                continue
            history = [dict(run=r["run_id"], trial=r["number"], outcome=r["outcome"],
                            log=str(lab.directory / "runs" / r["run_id"] / f"trial-{r['number']:04}.log"))
                       for r in s.db.execute("SELECT * FROM results WHERE test_id=? AND outcome IN ('failed','incomplete') "
                                             "ORDER BY rowid DESC LIMIT 20", (test["id"],))]
            return dict(outcome="claimed", owner=args.owner, test=test, suite=s.suite(test["suite"]),
                        failing_trials=history, events=s.events(test["id"], 20),
                        next_step="Diagnose, fix in an isolated worktree, prove it with "
                                  "`quruntul flake --target TEST --ref CANDIDATE --trials N --owner OWNER` against a "
                                  "baseline batch, open the PR, then `quruntul deflake pr TEST --pr URL --owner OWNER`.")
        return dict(outcome="no-candidate", flaky=len(s.tests(status="flaky")),
                    claimed=[c["resource"] for c in claims.values() if c["resource"].startswith("test:")])
    if not args.test:
        raise LabError("name the test")
    holder = s.held("test:" + args.test)
    if not holder or holder["owner"] != args.owner:
        raise LabError(f"{args.test} is not claimed by {args.owner}")
    if args.action == "pr":
        if not args.pr:
            raise LabError("--pr is required")
        s.set_status(args.test, "fixing", f"deflake PR {args.pr}", dict(pr=args.pr), pr=args.pr)
        s.release(args.owner, "test:" + args.test)
        lab.render()
        return dict(outcome="fixing", test=args.test, pr=args.pr,
                    next_step="When the PR merges, the next $flake verifies the test on the merged revision.")
    if not args.reason:
        raise LabError("--reason is required")
    s.event("deflake-released", args.test, dict(owner=args.owner, reason=args.reason))
    s.release(args.owner, "test:" + args.test)
    return dict(outcome="released", test=args.test)


def assess(args, lab: Lab):
    s = lab.state
    if args.action == "list":
        return s.assessments(args.status)
    if args.action == "claim":
        if not args.owner:
            raise LabError("--owner is required")
        chosen = []
        for o in s.observations("open"):
            if args.observation and o["id"] not in args.observation:
                continue
            if args.run and o["run_id"] not in args.run:
                continue
            if args.area and args.area.lower() not in (o["title"] + " " + (o["area"] or "")).lower():
                continue
            chosen.append(o["id"])
        identifier = s.claim_observations(args.owner, chosen)
        return dict(outcome="claimed", assessment=identifier,
                    observations=[o for o in s.observations("claimed") if o["id"] in chosen])
    if not args.id:
        raise LabError("name the assessment")
    if args.action == "show":
        return s.assessment(args.id)
    if args.action == "propose":
        text = Path(args.file).read_text()
        return dict(outcome="proposed", assessment=args.id, sha256=s.propose_assessment(args.id, text))
    if args.action == "approve":
        return s.approve_assessment(args.id, args.sha)
    if args.action == "issue":
        s.record_issue(args.id, args.finding, args.url)
        return dict(outcome="recorded", assessment=args.id, finding=args.finding, url=args.url)
    s.release_assessment(args.id, args.reason or "released")
    return dict(outcome="released", assessment=args.id)


def export(lab: Lab, target: Path) -> dict:
    target = target.resolve()
    if target.exists() or target.is_relative_to(lab.directory):
        raise LabError("export needs a new archive path outside the ledger directory")
    lab.render()
    temporary = target.with_name(target.name + f".{os.getpid()}.tmp")
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
            backup = lab.directory / "ledger-export.sqlite3"
            lab.state.db.execute("VACUUM INTO ?", (str(backup),))
            archive.write(backup, "ledger.sqlite3")
            backup.unlink()
            archive.write(lab.directory / "ledger.md", "ledger.md")
            for artifact in sorted((lab.directory / "runs").rglob("*")):
                if artifact.is_file():
                    archive.write(artifact, artifact.relative_to(lab.directory))
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return dict(outcome="exported", path=str(target))


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    lab = None
    try:
        lab = Lab(args.repo, log=lambda message: print(message, file=sys.stderr, flush=True))
        result = run(args, lab)
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
        if isinstance(result, dict) and result.get("outcome") in ("blocked", "interrupted", "budget-exhausted"):
            return 1
        return 0
    except (LabError, OSError, ValueError) as error:
        print(json.dumps(dict(outcome="error", error=str(error))), file=sys.stderr)
        return 2
    finally:
        if lab is not None:
            lab.state.db.close()


if __name__ == "__main__":
    raise SystemExit(main())
