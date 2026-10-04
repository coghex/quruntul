---
name: flake
description: "Measure tests for flakiness with the quruntul lab and keep its master ledger of every test, its status and when it last ran. `$flake` measures the next suite with never-measured tests; `$flake N` does that N times; `$flake <test-or-suite>` measures that target; `$flake N <target>` repeats it. Covers every test, CI suites included. Use when the user invokes $flake or asks about flaky tests or the flake ledger. Do not use to fix a flaky test ($deflake) or to run probes for observation ($test)."
---

# Flake

Measure which tests are flaky. Every test the repository's adapter enumerates
is a row in quruntul's ledger with a status:

- `new`: never measured.
- `stable`: its flake batch passed every trial. **It is never measured again by
  default**, however old the measurement is. Only the owner moves it back, after
  it fails somewhere real.
- `flaky`: a batch or the owner recorded a failure. `$deflake` takes it from here.
- `failing`: it failed in every trial of a complete batch. That is a consistent
  failure, not flakiness: suspect the harness or environment first, then the
  product. It goes to `$assess-tests`, never `$deflake`, and returns to `new`
  by itself when its suite's inputs (code or adapter) change.
- `fixing`: a `$deflake` PR is open. After it merges, the next `$flake` verifies
  the test on the merged revision.
- `retired`: no longer present upstream.

Run this skill in a **Class B** session (see the `model-classes` skill).

So `$flake` spends its time only on new tests, tests whose fix just merged, and
explicit targets. It is not a periodic re-test; that is `$test`, which covers
probes.

## Route by repository

Resolve the Git top level and read its `AGENTS.md` (or `CLAUDE.md`) first.

- **`.quruntul/adapter.py` exists:** use the quruntul workflow below.
- **Synarchy** (no adapter yet): read [references/synarchy.md](references/synarchy.md)
  completely and follow it instead; its census lab is unchanged.
- **Otherwise:** report that the repository has no quruntul adapter and stop.
  Never guess a lab from similarly named scripts.

`quruntul` must be on `PATH` (installed by quruntul's `install.py`). If it is
not, report that and stop.

## Parse the argument

```sh
quruntul resolve-arg flake <everything after $flake>
```

It returns `iterations` (default 1) and either a `target` (`test` or `suite`)
or a `hint`. `$flake` takes no hint: if the text matched nothing, or matched
several things (`ambiguous`), show the candidates and ask which one the user
meant. Do not guess.

## One iteration

1. Activate the repository's qualified toolchain as its instructions say. For
   Hetoimasia, that means GHC 9.14.1 and Cabal 3.18.1.0 first on `PATH`.
2. Run exactly one batch. Choose the command by target:
   - With no target: `quruntul flake`.
   - With a target: `quruntul flake --target <id>`.

   Never add `--ref`. That is only for the user's explicit candidate-evidence
   requests, and for `$deflake`. The command fetches upstream, pins a
   detached checkout, builds through the adapter, enumerates, runs the
   selected tests K times with fresh seeds, and records every result.
3. Read its JSON. The main outcomes are:
   - **`complete`:** the batch ran. Read `summary.measured`: which tests
     became `stable`, which became `flaky` (`newly_flaky`), which became
     `failing` (`newly_failing`), and their failure counts.
   - **`no-candidate`:** every test has been measured. This is the lab
     working as designed, not a gap. Report `skipped` reasons and the status
     counts.
   - **`blocked` / `interrupted` / `budget-exhausted`:** the batch could not
     complete. The report names the blocker. Nothing changed status on
     incomplete evidence.
   - **`busy`:** another agent holds that suite. Another selection may still
     succeed; one retry is fine.
4. For a batch with failures, open the failing trials' logs (paths are in the
   run's `report.md` observations) and confirm each failure is a test failure,
   not a harness or environment failure. The engine has already recorded the
   mechanical facts. If a failure is plainly environmental (a disk filled up,
   say), say so. Do not reclassify it in the ledger yourself; tell the owner,
   who can `quruntul mark`.

A failure is not proof of a product bug, and a clean batch is not proof of
stability. Never rerun a batch to get a pass.

## Shake down first

A flake batch is an expensive way to find out that the adapter launches a suite
wrongly. Recommend `quruntul shakedown` to the user after an adapter or harness
change (a new suite, a changed build or launch, a quruntul upgrade) and before
seeding a repository's ledger. It runs one trial of every suite that applies on
this platform (`--target SUITE` for one), launched exactly as a flake batch of
the whole suite, desktop suites included one at a time, at the upstream head
only. Each suite is `clean` or reports every problem it shows: `build-failed`,
`enumeration-failed`, `duplicated`, `incomplete`, `failed` or `unreported`. Each suite with a
problem becomes one observation for `$assess-tests`. It changes no test's
status and records nothing about tests or suites, and flake selection never
reads it, so a failed shakedown does not stop `$flake`. Treat its problems as
reasons to repair the launch before paying for batches.

## Repeating: `$flake N`

Run N iterations **serially**, one batch each. Give each iteration to a fresh
subagent when you can delegate; otherwise run them yourself one after another.
Give each worker this task:

> In `<repo>`, run exactly one `quruntul flake` iteration as the installed
> `$flake` skill describes (`<target args if any>`). Return the command's JSON
> summary and anything notable in the failing logs. Do not start a second batch.

After each iteration, post one line: the iteration number, the suite, the
number of tests that became stable or flaky, and the report path. Stop early
when an iteration returns `no-candidate`, when a blocker is not confined to one
suite (the toolchain is missing, fetch failed, the disk is full), or when the
user says stop. At the end, summarize the totals and the tests that became
flaky, each with its `$deflake <test>` next step.

## Desktop suites

Some suites open windows on this machine (`desktop: true`). The owner decided
on 2026-09-29 that flake measures them like any other test. The lab takes a
single `desktop` claim, so only one window-opening batch runs at a time, and
the adapter supplies the per-command consent. Before a desktop batch, say in
one line that windows will appear. Don't wait for a reply.

## Owner actions you may run when asked

- `quruntul mark <test> --status flaky --reason "..." --evidence "<CI run URL>"`:
  a stable test that failed for real. This is how a test re-enters the queue.
- `quruntul defer <suite-or-test> --reason ... --resume-when ...` and
  `quruntul resume ... --evidence ...`.
- `quruntul status`: the ledger summary, and `ledger.md`, the readable page.
- `quruntul tests --status flaky`.

## Report

For each iteration give the suite, the exact revision, trials run, the tests
that became stable, flaky or failing, the report and ledger paths, and any
limitation. Recommend `$deflake <test>` for new flaky tests and `$assess-tests`
for observations, including every batch with failing tests. Invoke neither.

This skill measures only. It never edits code, files issues or opens PRs.
