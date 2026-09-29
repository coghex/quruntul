# Quruntul design

Quruntul is one local testing lab shared by several repositories and several
agent skills. This document is its contract: what the ledger means, what each
lane may do, how skills parse their arguments, what an adapter supplies, and
what a result looks like. The code in `quruntul/` implements it; the skills in
`skills/` drive it.

## Principles

- **One ledger per clone.** Every lane reads and writes the same SQLite
  database in the repository's common Git directory
  (`<git-common-dir>/quruntul/ledger.sqlite3`). Every linked worktree sees it.
  It is local evidence, not a remote backup; `quruntul export` makes a
  portable archive.
- **The engine owns mechanics; agents own judgement.** Selection, claims,
  pinned checkouts, process lifetime, recording and validation are code.
  Interpreting evidence, diagnosing, fixing and writing observations are the
  skills' work.
- **The engine never touches the tracker, pushes, or merges.** Skills do that
  only where this document says so, under the consuming repository's own
  delivery rules.
- **Parallel by default.** Claims are per resource (a suite, a test, an
  observation batch), not one lab-wide lock. Independent agents can run
  different suites at the same time without coordinating by hand.
- **Evidence is immutable.** A completed trial, result or observation is never
  rewritten. Recovery after a crash consumes retained files; it never replays a
  trial.

## Lanes

| Lane | Skill | Selects | Runs | Changes test status |
|---|---|---|---|---|
| flake | `$flake` | tests that have never been measured | a batch of K trials of just those tests | new → stable or flaky |
| deflake | `$deflake` | one flaky test | diagnosis, a fix, before/after batches, a PR | flaky → fixing; fixing → stable/flaky on verification |
| test | `$test` | one probe (local-only, never CI) | one long observational execution | none |
| playtest | `$playtest` | one player question | one bounded agent-driven session | none |
| assess | `$assess-tests` | unassessed observations | nothing; verifies and correlates, then files issues on approval | none |
| profile | `$profile` | one performance question | one reproducible measurement | none |
| performance | `$performance` | completed profiles | nothing; assesses, and delivers one fix on authorization | none |

`$test` never runs a CI test. `$flake` runs everything, CI tests included,
because CI can be flaky too.

## Test status

Every test the adapters enumerate is a row in the ledger, identified as
`<suite>::<path>`. Its status is one of:

- `new` — enumerated, never measured by a flake batch.
- `stable` — its flake batch passed every trial. **A stable test stays stable.**
  Flake never measures it again, however old the measurement is. Only the owner
  moves it back, with `quruntul mark TEST --flaky --evidence ...` after it fails
  somewhere real (CI, production, a user report).
- `flaky` — a flake batch, or the owner, recorded a failure. `$deflake` owns it
  from here.
- `fixing` — a `$deflake` PR is open for it. When that PR merges, the next flake
  selection verifies it on the merged revision: a clean batch makes it stable,
  any failure makes it flaky again.
- `retired` — no longer enumerated at the upstream head. History is kept.

Probes (`kind: probe`) carry the same status for flakiness, and additionally a
`$test` freshness: `$test` re-runs a probe when its source identity changed or
its last observation is older than the adapter's refresh window. Freshness is
about observation, not flakiness; it never changes status.

Status changes are events. The ledger keeps every transition with its evidence.

## Batches

A flake batch runs one suite's selected tests K times in fresh processes,
`K = adapter.flake_trials` (default 10), with a new random seed per trial and
the seed recorded. It stops early only on a harness error or an interruption;
a test failure never stops the batch. Per trial it records every selected
test's outcome (`passed`, `failed`, `pending`), plus the trial's process outcome
(`passed`, `failed`, `crashed`, `timeout`, `harness-error`, `interrupted`).

A test is stable after the batch only if it passed in every trial. A test
missing from a trial's results when the trial crashed or timed out is not
counted as passed. That trial is incomplete, and when no complete trial remains
the test stays `new` and the run is `inconclusive`.

Status changes only for a batch at the upstream head. There, a failure moves a
`new`, `stable` or `fixing` test to `flaky` (an explicit re-measurement of a
stable test that fails is a real failure), and a batch in which every trial
completed and the test passed every time moves a `new` or `fixing` test to
`stable`. A batch at any other revision (`--ref`) is **candidate evidence**: it
is recorded, counted and reported, but it never changes status, because a
candidate's failure may be the candidate's own. `$deflake` proves fixes with
exactly such batches.

## Claims

A claim is a row `(resource, owner, lane, heartbeat)`. Resources are
`suite:<id>`, `test:<id>`, `observations:<assessment>`, `desktop` and
`build:<checkout>`. Acquisition is one SQLite `BEGIN IMMEDIATE` transaction.
A claim whose heartbeat is older than its lease (default 10 minutes) is stale
and may be taken over; the takeover is an event. Long-running work heartbeats.

- Two agents never measure the same suite at the same time.
- Builds in one pinned checkout are serialized by `build:<checkout>`; trials of
  different suites then run in parallel.
- Suites that open windows (`desktop: true`) also take the single `desktop`
  claim, so only one window-opening run happens at a time.

## Checkouts

Each measured revision gets one detached worktree beside the repository in
`../.<repo>-quruntul/<revision>`, reused by every run at that revision and never
reset. A dirty or moved checkout blocks the run; it is never cleaned by the lab.
`quruntul prune` lists checkouts no active run uses; `--apply` removes them with
`git worktree remove`, never forcing.

## Argument grammar

Every skill accepts the same shapes after its name:

| Argument | Meaning |
|---|---|
| none | one iteration; the lane selects |
| `N` (a positive integer) | N serial iterations, each a fresh selection, each in a fresh subagent when available |
| a test id, suite id or probe id the ledger knows | one iteration targeting it |
| `N <target>` | N iterations targeting it (e.g. N deflake attempts on one test) |
| anything else | a hint (area, question) for lanes that take one (`$test`, `$profile`, `$playtest`) |

`quruntul resolve-arg <lane> <text>` classifies the text so every skill parses
it identically. Loops stop early on a proposal needing approval, a blocker that
is not confined to the selected work, no remaining candidate, or the user
saying stop. Iterations never run concurrently inside one loop; separate
sessions provide parallelism.

## Adapter

A consuming repository commits `.quruntul/adapter.py`. The engine imports it
from the pinned checkout being measured, so the adapter is versioned with the
code it describes. It defines `adapter()` returning an object with:

- `name` — the repository's name.
- `suites(ctx)` — every suite at the checkout: id, kind (`ci`/`probe`),
  framework, description, area, platforms, desktop flag, trial and batch
  limits, `batch_tests` (at most this many unmeasured tests per flake batch, so
  a large suite is measured in slices), and a source identity. Frameworks:
  `hspec` (each example a test), `command` (each declared check a test) and
  `exit` (one test, `run`, decided by the exit status alone).
- `prepare(ctx, suite)` — build it (through `ctx.run`, so the guardian owns the
  process) and return a `Prepared`: the base argv, working directory,
  environment, and provenance (executable hash, build argv).
- `consent(ctx, suite)` — environment additions for a desktop suite, and a
  wrapper argv (e.g. an isolated display) where the platform needs one.
- Optional: `flake_trials`, `refresh_days`, `playtest()`, and three hooks,
  each called as `hook(ctx, suite, trial)`:
  - `trial_env` — extra environment for one trial (`trial` has `number`,
    `prefix` — the trial's artifact path prefix — and `seed`);
  - `outcomes` — read a `command` suite's checks from the probe's own protocol,
    returning `{check: passed|failed|missing|unproven}`, or `None` when the
    trial left nothing readable;
  - `seed` — `{path: {status, reason, evidence}}` for tests the ledger has just
    met, carrying an older lab's verdicts over. Only `new` tests are seeded,
    only to `stable` or `flaky`, and each seed is a recorded status event.

For Hspec suites the engine does the rest: enumeration (`--dry-run
--format=checks`), exact per-test selection (`--match /path/`), per-trial
results (`--format=checks` plus `--failure-report`), and parsing. Command
probes write `quruntul-probe/v1` JSON to `$QURUNTUL_PROBE_RESULT` naming their
declared checks.

## Results and observations

Each run has a directory `<git-common-dir>/quruntul/runs/<run-id>/`:

- `manifest.json` — what was selected, why, at which revision, with which
  environment and harness revision.
- `trial-NNNN.log`, `trial-NNNN.result.json` — raw evidence per trial.
- `result.json` — the machine record, regenerated from the ledger.
- `report.md` — the human/agent report, format `quruntul-result/v1`.

`report.md` has YAML frontmatter (`run_id`, `lane`, `revision`,
`interpretation_status`: `clean`, `observations`, `inconclusive` or `blocked`)
and one `### OBS-nnn — title` section per independent observation, each with
the fields in `quruntul/templates/report.md`. `quruntul report attach RUN`
validates it and ingests each observation into the ledger, where
`$assess-tests` claims and assesses them. Flake batches write their report
automatically: every newly flaky test becomes one observation.

## Legacy repositories

Synarchy's census-based flake lab and its `codex-test`/`codex-profile`
coordinators predate this design. Until Synarchy has an adapter, each skill
routes Synarchy to its preserved legacy workflow under
`skills/<name>/references/synarchy.md`. Nothing here changes those workflows.
