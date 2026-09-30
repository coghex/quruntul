#!/usr/bin/env python3
"""A stand-in for an Hspec test executable, faithful to the CLI surface quruntul uses.

It reads `spec.json` from its working directory: {"examples": ["Group/Sub/example", ...],
"fail": {"Group/Sub/example": [trial numbers that fail]}, "crash_on": [trial numbers],
"stray": a line printed at column 0 after the first example of a run (as a child process's
diagnostics would be), "omit": [examples a run executes but never reports]}.
QURUNTUL_TRIAL names the trial. It honours --dry-run, --match, --format=checks,
--failure-report, --fail-on=empty and prints the checks formatter's layout.
"""
import json
import os
import sys
from pathlib import Path


def haskell(text):
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def main(argv):
    spec = json.loads(Path("spec.json").read_text())
    dry = "--dry-run" in argv
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
    if trial in spec.get("crash_on", []):
        os.kill(os.getpid(), 9)
    chosen = [p for p in spec["examples"] if not patterns or any(pat in "/" + p + "/" for pat in patterns)]
    if not chosen:
        print("0 examples, 0 failures")
        return 1
    failed = [] if dry else [p for p in chosen if trial in spec.get("fail", {}).get(p, [])]
    pending = set() if dry else set(spec.get("pending", []))
    previous = []
    for path in chosen:
        parts = path.split("/")
        groups = parts[:-1]
        shared = 0
        while shared < min(len(previous), len(groups)) and previous[shared] == groups[shared]:
            shared += 1
        for depth in range(shared, len(groups)):
            print("  " * depth + groups[depth])
        mark = "✘" if path in failed else "‐" if path in pending else "✔"
        if not dry and path in spec.get("omit", []):
            continue
        print("  " * len(groups) + parts[-1] + f" [{mark}]")
        previous = groups
        if not dry and spec.get("stray") and path == chosen[0]:
            print(spec["stray"])
    print()
    if failed:
        print("Failures:")
        for path in failed:
            print("  " + path)
    print(f"Finished in 0.0001 seconds\n{len(chosen)} examples, {len(failed)} failures")
    if report:
        paths = ", ".join("([" + ",".join(haskell(g) for g in p.split("/")[:-1]) + "]," + haskell(p.split("/")[-1]) + ")"
                          for p in failed)
        Path(report).write_text(f"FailureReport {{failureReportSeed = 1, failureReportPaths = [{paths}]}}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
