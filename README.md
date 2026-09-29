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

## How it fits together

- **One ledger per clone** — `<git-common-dir>/quruntul/ledger.sqlite3`, shared
  by every lane and linked worktree, with a regenerated `ledger.md` beside it.
  Every enumerated test is a row: `new` → `stable` or `flaky` → `fixing` →
  `stable`. A stable test is never measured again unless the owner marks it
  flaky after a real failure.
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
quruntul tests --status flaky
quruntul mark TEST --status flaky --reason "failed in CI" --evidence URL
quruntul test                           # one due probe
quruntul report attach RUN              # validate a completed report, ingest observations
quruntul export --output /path/lab.zip  # portable archive of ledger and evidence
```

## Adding a repository

Commit `.quruntul/adapter.py` defining `adapter()`, which returns an object
with `name`, `suites(ctx)` and `prepare(ctx, suite)`; see
[docs/design.md#adapter](docs/design.md#adapter) and the fixture adapter in
`tests/test_lab.py`. Hspec suites need nothing else: the engine enumerates
examples, selects them exactly, and reads per-example results. Command probes
report `quruntul-probe/v1` JSON to `$QURUNTUL_PROBE_RESULT`.

Skills choose by what the checkout contains: with `.quruntul/adapter.py` they use
quruntul, otherwise Synarchy falls back to its preserved legacy workflow in
`skills/<name>/references/synarchy.md`. Hetoimasia's adapter is
[hetoimasia#322](https://github.com/coghex/hetoimasia/pull/322) and Synarchy's is
[synarchy#2760](https://github.com/coghex/synarchy/pull/2760); `$playtest` keeps
Synarchy on its own harness either way.

## Development

```sh
python3 -m unittest discover -s tests -t .
```

The engine is standard-library Python. Tests use a real Git repository and a
fake Hspec executable, so they need no compiler.
