# Quruntul

One local testing lab for my repositories, and the Codex skills that drive it.

| Skill | Does |
|---|---|
| `$flake` | measures every test once for flakiness and keeps the master ledger |
| `$deflake` | fixes a flaky test and opens a PR carrying before/after evidence |
| `$test` | runs one due local-only probe (never CI) and reports observations |
| `$playtest` | runs one bounded naive-player session (scaffolding where there is no gameplay) |
| `$assess-tests` | verifies observations and files the issues the owner approves |
| `$profile` | runs one reproducible performance experiment |
| `$performance` | assesses profiles and, on authorization, delivers one fix |

Every skill takes the same argument: nothing (the lab chooses), `N` (N serial
iterations), a test/suite/probe id (that target), `N <target>`, or a hint.
`$test 10` runs ten probes; `$flake 10` measures ten suites; `$deflake 3` fixes
three flaky tests.

The name is the mountain of trials; the `q` makes it tab-complete.

## Quick start

Run the skills from a Codex session in the repository you're testing.
`$flake`, `$deflake`, `$test`, `$playtest` and `$assess-tests` need the repository
to commit `.quruntul/adapter.py` (Synarchy and Hetoimasia do, for example).
`$profile` and `$performance` work in any repository. A typical order:

1. **Check the adapter (recommended).** After an adapter or harness change, and
   before seeding a ledger, `quruntul shakedown` runs one trial of every suite that
   applies on this platform and reports any that fail to build, list or finish. It's advisory: it changes no
   status, and measuring doesn't wait on it.
2. **Measure flakiness.** `$flake 10` measures ten suites that still have
   never-measured tests. `quruntul status` shows where the ledger stands.
3. **Act on the results.** `$deflake` takes the worst unclaimed `flaky` test and
   opens a PR with before/after evidence. A test that fails every trial is
   `failing`, not flaky: take it to `$assess-tests`. Only the owner moves a stable
   test back to flaky after it fails in CI, or an agent does it when the owner asks:
   `quruntul mark TEST --status flaky --reason "failed in CI" --evidence URL`.
4. **Run the local-only probes.** `$test` runs the next due probe; `$playtest`
   runs a naive-player session where the adapter offers one. Each run leaves a
   report whose `OBS-nnn` observations wait for assessment.
5. **Turn observations into issues.** `$assess-tests` checks open observations
   against the current code and proposes an assessment with an id and sha256.
   Read it with `quruntul assess show <id>`, then approve it with
   `$assess-tests approve <id> <sha256>`, which files the issues it drafted.
   Nothing is filed before that approval.
6. **Profile.** `$profile` runs one reproducible performance experiment;
   `$performance` assesses the reports and delivers a fix only when you
   authorize it. Both still use the `codex-profile` coordinator.

## How it fits together

- **One ledger per clone** — `<git-common-dir>/quruntul/ledger.sqlite3`, shared
  by every lane and linked worktree, with a regenerated `ledger.md` beside it.
  Every enumerated test is a row: `new` → `stable` or `flaky` → `fixing` →
  `stable`. A stable test is never measured again unless the owner marks it
  flaky after a real failure. A test that fails every trial is `failing`, not
  flaky: it goes to `$assess-tests` and is re-queued when its suite's inputs
  change.
- **Claims, not a global lock** — agents in separate sessions run different
  suites in parallel; builds in one checkout serialize; only one window-opening
  suite runs at a time.
- **Adapters** — a repository opts in by committing `.quruntul/adapter.py`,
  describing its suites and how to build them. The engine does enumeration,
  per-test selection, repetition, process lifetime and recording.
- **One report format** — every run leaves `runs/<id>/report.md`
  (`quruntul-result/v1`) whose `OBS-nnn` observations `$assess-tests` consumes.

Read [docs/design.md](docs/design.md) for the full contract.

## Install

Needs Python 3.11+, Git and, for PR/issue steps, an authenticated `gh`.

```sh
gh repo clone coghex/quruntul ~/work/quruntul
python3 ~/work/quruntul/install.py            # preview
python3 ~/work/quruntul/install.py --apply    # link skills and the command
```

`install.py` symlinks each skill into `~/.codex/skills/` (so `git pull` updates
them), links `bin/quruntul` into `~/.local/bin/`, and moves any skill it
replaces — and the retired `autotest` — into a dated
`~/.codex/skills-superseded-*/`. It never deletes anything.

## Using the command directly

```sh
quruntul status                         # summary; writes ledger.md
quruntul flake                          # one batch of the next suite with new tests
quruntul flake --target SUITE_OR_TEST   # measure that
quruntul flake --target TEST --rts-capabilities 4  # one isolated -N4 ledger row
quruntul flake --target TEST --trial-deadline 120  # every trial under a hard 120 s deadline; a timeout, signal kill or nonzero exit counts as a failure of TEST
quruntul shakedown                      # one trial of every suite that applies here; changes no status
quruntul shakedown --target SUITE       # just that suite
quruntul tests --status flaky
quruntul mark TEST --status flaky --reason "failed in CI" --evidence URL
quruntul test                           # one due probe
quruntul report attach RUN              # validate a completed report, ingest observations
quruntul import-history                 # import the adapter's closed legacy $test history; all or nothing
quruntul export --output /path/lab.zip  # portable archive of ledger and evidence
```

## Adding a repository

Commit `.quruntul/adapter.py` defining `adapter()`, which returns an object
with `name`, `suites(ctx)` and `prepare(ctx, suite)`; see
[docs/design.md#adapter](docs/design.md#adapter) and the fixture adapter in
`tests/test_lab.py`. Hspec suites need nothing else: the engine enumerates
examples, selects them exactly, and reads per-example results. Command probes
report `quruntul-probe/v1` JSON to `$QURUNTUL_PROBE_RESULT`.

Before seeding a repository, and after changing its adapter, run
`quruntul shakedown`: one trial of every suite, launched as a flake batch would
launch it, reporting any suite that fails to build, list its tests, complete
its trial or report every test, without touching the ledger.

`$flake`, `$deflake`, `$test`, `$playtest` and `$assess-tests` choose by what the
checkout contains: with `.quruntul/adapter.py` they use quruntul; without it, they
report that the repository has no quruntul adapter and stop. `$playtest`
additionally requires the adapter's `playtest()` capability. `$profile` and
`$performance` keep the `codex-profile` coordinator in every repository for now.

## Development

```sh
python3 -m unittest discover -s tests -t .
```

The engine is standard-library Python. Tests use a real Git repository and a
fake Hspec executable, so they need no compiler.
