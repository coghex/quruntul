#!/usr/bin/env python3
"""A stand-in for an Hspec test executable, faithful to the CLI surface quruntul uses.

It reads `spec.json` from its working directory: {"examples": ["Group/Sub/example", ...],
"fail": {"Group/Sub/example": [trial numbers that fail]}, "crash_on": [trial numbers],
"stray": a line printed at column 0 after the first example of a run (as a child process's
diagnostics would be), "omit": [examples a run executes but never reports],
"dry_fail": a message --dry-run prints before exiting 1, "garble": true (write an unreadable
failure report), "report_dir": true (the failure report path is a directory from the start, so
reading it fails with an OSError), "leak": true (leave a live child in
the process group), "stop": {"after": example, "how": "crash" | "hang" | "interrupt"} (stop
once that example is reported; "interrupt" first sends SIGINT to $FIXTURE_INTERRUPT_PID)}.
$FAKE_HSPEC_SPEC names another spec file, and $FAKE_HSPEC_RECORD a file that receives the
argv and the FIXTURE_* environment. QURUNTUL_TRIAL names the trial. It honours --dry-run,
--match, --format=checks, --failure-report, --fail-on=empty and prints the checks
formatter's layout. `occurrences` maps paths to lists of passed/failed/pending marks,
so twins may disagree (dry runs still pass). `no_report` omits the failure file.
`also_run` prints listed paths despite match selectors, modelling over-selection.
"""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def haskell(text):
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def main(argv):
    sys.stdout.reconfigure(line_buffering=True)
    spec = json.loads(Path(os.environ.get("FAKE_HSPEC_SPEC", "spec.json")).read_text())
    if os.environ.get("FAKE_HSPEC_RECORD"):
        Path(os.environ["FAKE_HSPEC_RECORD"]).write_text(json.dumps(dict(
            argv=argv, env={k: v for k, v in os.environ.items() if k.startswith("FIXTURE_")})))
    if Path("override.json").exists():
        # Outside the fixture suite's identity: changes behaviour without re-queueing tests.
        spec.update(json.loads(Path("override.json").read_text()))
    dry = "--dry-run" in argv
    if dry and spec.get("dry_fail"):
        print(spec["dry_fail"])
        return 1
    patterns, report = [], None
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg in ("--match", "-m"):
            patterns.append(argv[index + 1])
            index += 1
        elif arg.startswith("--match="):
            patterns.append(arg.split("=", 1)[1])
        elif arg.startswith("--failure-report="):
            report = arg.split("=", 1)[1]
        index += 1
    trial = int(os.environ.get("QURUNTUL_TRIAL", "0"))
    if report and not dry and spec.get("report_dir"):
        os.makedirs(report, exist_ok=True)
    if trial in spec.get("crash_on", []):
        os.kill(os.getpid(), 9)
    chosen = [p for p in spec["examples"] if not patterns or any(pat in "/" + p + "/" for pat in patterns) or (not dry and p in spec.get("also_run", []))]
    if not chosen:
        print("0 examples, 0 failures")
        return 1
    seen, marks = {}, []
    for path in chosen:
        occurrence = seen.get(path, 0)
        seen[path] = occurrence + 1
        overrides = spec.get("occurrences", {}).get(path, [])
        mark = ("passed" if dry else overrides[occurrence] if occurrence < len(overrides) else
                "failed" if trial in spec.get("fail", {}).get(path, []) else
                "pending" if path in spec.get("pending", []) else "passed")
        marks.append(mark)
    failed = [p for p, mark in zip(chosen, marks) if mark == "failed"]
    previous = []
    for path, outcome in zip(chosen, marks):
        parts = path.split("/")
        groups = parts[:-1]
        shared = 0
        while shared < min(len(previous), len(groups)) and previous[shared] == groups[shared]:
            shared += 1
        for depth in range(shared, len(groups)):
            print("  " * depth + groups[depth])
        mark = {"failed": "✘", "pending": "‐", "passed": "✔"}[outcome]
        if not dry and path in spec.get("omit", []):
            continue
        print("  " * len(groups) + parts[-1] + f" [{mark}]")
        previous = groups
        if not dry and spec.get("stray") and path == chosen[0]:
            print(spec["stray"])
        stop = spec.get("stop") or {}
        if not dry and stop.get("after") == path:
            if stop["how"] == "crash":
                os.kill(os.getpid(), 9)
            if stop["how"] == "interrupt":
                os.kill(int(os.environ["FIXTURE_INTERRUPT_PID"]), signal.SIGINT)
            time.sleep(60)
    print()
    if failed:
        print("Failures:")
        for path in failed:
            print("  " + path)
    print(f"Finished in 0.0001 seconds\n{len(chosen)} examples, {len(failed)} failures")
    if report and not spec.get("report_dir") and not spec.get("no_report"):
        paths = ", ".join("([" + ",".join(haskell(g) for g in p.split("/")[:-1]) + "]," + haskell(p.split("/")[-1]) + ")"
                          for p in failed)
        Path(report).write_text("garbled" if spec.get("garble") else
                                f"FailureReport {{failureReportSeed = 1, failureReportPaths = [{paths}]}}")
    if not dry and spec.get("leak"):
        subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
