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
| shakedown | `quruntul shakedown` (recommended by `$flake`) | every suite that applies on this platform, or one | one trial of each whole suite | none; writes nothing about tests or suites |
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
- `failing` — its batch completed and it failed in every trial. A consistent
  failure is not flakiness: the harness or the environment is the first
  suspect, then the product. Its batch raises one `uncertain` observation for
  `$assess-tests`, as does any batch in which tests failed every trial,
  whatever their status (a failing test measured again, a candidate batch);
  `$deflake` never selects it. Like `pending`, it becomes
  `new` again when its suite's inputs change, so a harness or code repair
  re-queues it without an owner mark. Measured again explicitly, it is judged
  afresh: failing every trial keeps it `failing`, failing some makes it
  `flaky`, and passing every trial makes it `stable`.
- `fixing` — a `$deflake` PR is open for it. When that PR merges, the next flake
  selection verifies it on the merged revision: a clean batch makes it stable,
  any failure makes it flaky again.
- `pending` — its batch completed and it was pending in every trial: this
  environment never exercises it (a platform-gated or opt-in example). It is not
  selected again until its suite's inputs change, when it becomes `new`.
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
stable test that fails is a real failure), or to `failing` when every
requested trial ran, completed and failed it (more than one), and a batch in
which every requested trial ran and completed and the test passed every time
moves a `new`, `fixing` or `failing` test to `stable`. A batch cut short (its
budget, a harness error) ran fewer trials than it asked for, so it proves
neither. A batch at any other revision (`--ref`) is **candidate evidence**: it
is recorded, counted and reported, but it never changes status, because a
candidate's failure may be the candidate's own. `$deflake` proves fixes with
exactly such batches.

## Shakedowns

A shakedown proves cheaply that an adapter launches every suite the way CI
does, before flake batches rely on that launch. `quruntul shakedown` runs one
trial of every suite that applies on this platform, or of one suite with
`--target SUITE`, always at the upstream head: it takes no `--ref`, and its
target is a suite, never a test.

- **Launch.** Each suite's trial goes through the same preparation, build,
  enumeration and launch as a flake batch of that whole suite: the same argv,
  working directory, environment, wrapper and adapter hooks. It runs every
  listed test, with no per-test selectors and no `batch_tests` slicing. The
  current enumeration names the tests, whatever the ledger holds. Hooks and the
  process see the suite's first trial (`QURUNTUL_TRIAL=1`), while the run
  numbers its trial rows one per suite; each trial row names its suite, and
  each suite's evidence is in `runs/<run-id>/<suite>/`.
- **Claims.** Each suite takes its `suite:<id>` claim, and a desktop suite
  also the `desktop` claim, so one window-opening suite runs at a time. Desktop
  suites are included, with the consent the adapter's preparation supplies. A
  suite whose claim another owner holds is reported `busy` and not run. Claims
  are released after each suite, whatever its result.
- **Skipped.** A suite that does not apply on this platform, or that is
  deferred, is listed as `skipped` with its reason, even when targeted.
  `--target` naming a deferred suite is refused with the deferral's reason, as
  `$flake` and `$test` refuse one.
- **Problems.** Each suite records every problem present, never only the first,
  each with its affected tests and evidence paths, always in this order:
  - `build-failed`: preparation or build failed; the adapter's error and the
    build logs.
  - `enumeration-failed`: listing the tests failed; the reason and the log.
  - `incomplete`: the trial did not complete (it crashed, timed out, was
    interrupted, or ended in a harness or setup error). It keeps the process
    outcome, its detail and its log, and lists every test without a result.
  - `failed`: every test that reported failure, including those reported
    before an incomplete trial stopped.
  - `unreported`: in a completed trial, every listed test that reported no
    result.

  A trial completed when its process passed or failed; any other outcome makes
  it incomplete, whatever successes it reported. A test without a result is
  never counted as passed. For `command` suites a check reported `missing`, or
  not reported at all, is unreported (or incomplete in an incomplete trial).
  Protocol evidence that cannot be read (a malformed probe report, an
  undeclared check of any type, an exit status that disagrees with the checks,
  an Hspec failure report that cannot be read or parsed, a hook that read
  nothing) makes a completed trial a harness error, with the guardian's own
  outcome kept beside it. Each channel (the trial log, the failure report, the
  probe's checks) is read on its own, so any failure still readable is kept,
  after an interruption too. Such a problem belongs to its suite alone: the
  shakedown records it and goes on to the next suite.
- **Summary rule.** A suite's result is its first problem, or `clean` when it
  has none: it built, listed its tests, ran a trial that completed, and every
  listed test reported a result with none failed. Hspec `pending` and probe
  `unproven` results are reported results, not failures, so they leave a suite
  clean; the report lists them as not passed. A suite not run is `skipped`,
  `busy` or, after an interruption, `not-run`.
- **Independence and interruption.** A suite's problem never stops the next
  suite. An interruption stops the shakedown: the running suite is `incomplete`
  with `interrupted` as its process outcome, keeping whatever its trial had
  already reported, the suites not reached are `not-run`, and everything
  recorded is kept. Recovery never replays a trial.
- **Report and observations.** The run is lane `shakedown`, with a
  `quruntul-result/v1` report giving each suite's result, all of its problems
  and the tests for each. Each suite with a problem is one `harness`
  observation covering all of its problems, naming the harness and
  environment as the first suspect and recommending `$assess-tests`, which
  claims and assesses it like any other. Skipped, busy and not-run suites raise
  no observation. The report is `clean` when every suite that applies ran
  clean, `observations` when any suite has a problem, and `inconclusive`
  otherwise.
- **Ledger rule.** A shakedown writes nothing about tests or suites: no test is
  added, retired, re-queued or changed in status, and no suite's declaration,
  identity, enumeration or `$test` freshness changes, even for an undeclared
  suite or an empty ledger. Where its enumeration differs from the ledger
  (tests the ledger lacks, and ledger tests no longer listed), the report says
  so and nothing is recorded. It writes only its own run, trials, results,
  report and observations, and its claims.
- **Advisory.** Flake and `$test` selection never read a shakedown's result. Run
  one after an adapter or harness change and before seeding a repository.

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
code it describes. `ctx.version` is the engine's version as a tuple; an adapter
that relies on newer engine behaviour refuses an older engine. 0.2.0 added the
rule that exact per-test selection supersedes a suite's own `--match`
selectors (its `--skip` selectors still apply), which profile suites need.
0.3.0 added the `failing` status for a test that fails every requested trial,
and reads Hspec trial output against the suite's enumerated paths. 0.4.0 added
the optional `legacy_history` hook and `quruntul import-history`; an adapter
supplying the hook refuses an older engine. It defines `adapter()` returning an
object with:

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
- Optional: `legacy_history(ctx)` — the repository's legacy `$test` history,
  for `quruntul import-history` alone (see
  [Legacy history import](#legacy-history-import)). No lane calls it, and an
  adapter without it behaves exactly as before.

For Hspec suites the engine does the rest: enumeration (`--dry-run
--format=checks`), exact per-test selection (`--match /path/`), per-trial
results (`--format=checks` plus `--failure-report`), and parsing. A trial's
output is read against the suite's enumerated paths, so a line the suite's own
processes print (a child's diagnostics at column 0, say) is ignored rather than
read as a group that misnames later examples. A selected test that a completed
trial never reports is `missing`: it stays unmeasured, and the batch report
raises a harness observation instead of calling the batch clean. Command
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
automatically: every newly flaky test becomes one observation. Shakedowns do
too: every suite with a problem becomes one observation.

Imported legacy history keeps its copied evidence under
`<git-common-dir>/quruntul/imported/<import-id>/`, beside that import's
`import-report.json`. `quruntul export` archives the ledger, every file under
`runs/`, and exactly the imported files the exported ledger snapshot records,
so an archive holds each import whole or not at all.

## Legacy history import

`quruntul import-history` imports a repository's legacy `$test` history, which
its adapter reads from the legacy store and supplies through the
`legacy_history(ctx)` hook. It runs only when the owner runs it; no lane
imports on its own. It never runs, replays or re-executes a run or a trial,
during an import or during recovery. It reports what it imported, attached,
archived and left unchanged, or, when it refuses, every problem it found, never
only the first. A refusal exits 1 and changes nothing.

### The hook

The adapter is loaded from the upstream head, pinned once: its suite
declarations, the suites' identities and the hook all come from that one
revision, and `ctx` cannot run processes. The import prepares, enumerates,
seeds and executes nothing, and recovers no native run. `legacy_history(ctx)`
returns a mapping whose only keys are `runs`, `observations`, `assessments`,
`proposals` and `evidence`, each a list (a missing key is an empty list). Every
record has a `source` identity, `{store, kind, id}`: the stable source store,
the record kind (`run`, `observation`, `assessment`, `proposal` or `evidence`,
matching its list), and the source record's id. A reference to another record
has the same shape. Records are plain JSON; `document` and `provenance`, where
allowed, are objects kept as supplied. The fields are:

| Kind | Required | Optional |
|---|---|---|
| run | `source`, `target`, `revision`, `started`, `finished`, `status` | `interpretation`, `document`, `provenance` |
| observation | `source`, `run`, `number`, `title`, `disposition` | `area`, `assessment`, `document`, `provenance` |
| assessment | `source`, `status`, `created` | `observations`, `document`, `provenance` |
| proposal | `source`, `target`, `lane`, `status`, `created` | `refers`, `document`, `provenance` |
| evidence | `source`, `owner`, `role`, `mode` | `path`, `size`, `provenance` |

The adapter normalizes its store's own vocabulary to these values:

- A run's `target` is the source target name, matched exactly against suite
  ids. Its `status` is terminal: `passed`, `failed`, `cancelled` or `error`.
  `pending`, `queued`, `preparing`, `running` and `started` are open items.
  `interpretation`, when present, is a report status (`clean`, `observations`,
  `inconclusive`, `blocked`). `started` and `finished` (its completion time)
  are ISO 8601 timestamps with a UTC offset, and `finished` is not before
  `started`.
- An observation refers to its `run`, and optionally to the `assessment` that
  decided it. `number` is its positive number within the run. `disposition` is
  its final disposition as text; `null` means it was never assessed, which is
  an open item.
- An assessment's `status` is `approved` when the source approved it; any other
  value is an open item. `observations` lists those it covers.
- A proposal's `status` is `rejected`, `designed` or `implemented`; any other
  value is undecided, an open item. `refers` lists the runs, observations or
  assessments it came from.

### Validation

Before anything is written, the import validates the complete supplied history
and refuses, naming each problem, rather than dropping a record or failing
partway. It refuses:

- a malformed record, status, timestamp or relation: a missing or unknown
  field, a value outside the vocabularies above, a timestamp without an
  offset, or a reference to the wrong kind of record;
- a reference to a run, assessment or observation that is neither supplied nor
  already imported;
- two supplied records sharing an identity, even one imported before;
- an open item: a nonterminal run, an observation without a final
  disposition, an unapproved assessment, or an undecided proposal;
- duplicate or conflicting evidence entries: two entries for the same source
  file of the same record, or two that map to the same destination;
- any manifest or conflict problem below.

### Closed history, matching and freshness

An import writes only closed history. Runs are ledger runs in lane
`imported`, finished, with their source identity, revision, provenance and the
whole record kept. Observations are `assessed`, with their final disposition.
Assessments are `approved`, and proposals keep their decided status.
Imported assessments and proposals are closed: issues are not recorded against
them and their decisions are not changed. None of it enters an open queue, and
the existing limit of one proposal per target holds: an imported proposal
occupies its target as any proposal does.

A new run whose target names a suite the adapter declares at the upstream head
attaches to that suite. Every other run is archived: attached to no suite, and
counting toward no freshness. For each matched suite, only its new imported
runs count. Its `last_test_run` becomes the newest one's completion time and
its `last_test_identity` the suite's identity at the upstream head at import
time, so several runs of one suite contribute only their newest. A matched
suite the ledger has never declared gets its declaration row, without any
enumeration. An existing suite row changes only these two fields.

### Evidence manifest

Each evidence entry is its own record. It names the run or assessment it
belongs to (`owner`), its `role`, its `mode`, and, unless it is absent, its
absolute source `path` and declared `size` in bytes. Roles are `report` and
`log` (a run's), `document` (an assessment's), and `other` (an image or small
artifact of either). Modes are `copy`, `reference` and `absent`:

- Every supplied run has exactly one report entry, always `copy`, and exactly
  one log entry: its `copy`, or `absent` when the source history never had
  one. Every supplied assessment likewise has exactly one document entry,
  `copy` or `absent`. A manifest that omits one, or declares a report, an
  existing log or an existing assessment document `reference`, is refused. A
  promised file that is simply missing is a refusal, never an absence.
- Any other file may be `copy` or `reference`. The engine never discovers files
  by walking directories and applies no size threshold of its own: copy work is
  bounded by the declared sizes and their total.
- A `copy` is copied to `imported/<import-id>/<owner>/<file name>`, a
  destination no earlier path can occupy, and its source path, destination
  and SHA-256 are recorded in the entry's provenance. A promised copy that is
  missing, unreadable, not a regular file, a different size from its
  declaration, or changed while being read refuses the whole import, naming the
  file and the reason. It is never truncated, omitted or downgraded to a
  reference.
- A `reference` is recorded by source path and provenance only, and is never
  read. It is disclosed as excluded from export both in the import report's
  `excluded_from_export` and in the entry's provenance; both are exported.
  Nothing describes its bytes as copied.
- An `absent` entry names no path or size and is recorded as absent.

### Idempotence and conflicts

Idempotence is judged against immutable source content and evidence, never
against engine-generated import timestamps, local destinations or current
suite matching. A record whose identity was imported before with identical
content, and, for a copy, the same SHA-256, is a no-op even after later ledger
activity, and keeps its original attachment and identity snapshot. A record
whose content or copied bytes changed under an imported identity refuses the
import, as does any change to a run's or assessment's set of evidence entries
(its manifest). Only genuinely new records are checked for conflicts and
applied to freshness. An import with nothing new changes nothing and writes no
file.

It refuses, naming each conflict, when:

- a matched suite's ledger `last_test_run` is later than or equal to its newest
  new run's completion time, compared as UTC instants;
- a new proposal's target already has a proposal, native or imported, or two
  new proposals share a target, whatever their stores.

### All or nothing

A successful import adds its records and evidence and changes only the
specified freshness of matched suites; every other ledger row, queue and
evidence file stays as it was, and completed evidence is never rewritten.
Source files and the source store are never modified. One import runs at a
time, under the `import.lock` file lock, and it proceeds in order:

1. Validate the complete history and check it against the ledger.
2. Stage every copy under `imported/.staging/<import-id>/`, after writing a
   journal of the import's own destinations.
3. Publish the staged copies and the import report into
   `imported/<import-id>/`.
4. Commit every row in one ledger transaction, after checking freshness and
   proposal conflicts again inside it, so activity since step 1 cannot
   invalidate the import. This durable commit is the import's success
   boundary.
5. Remove the staging directory.

Before the commit, a refusal or failure removes the import's own files, and an
interruption leaves only unpublished leftovers. No reader and no export sees
them: export takes imported files only from its ledger snapshot. The next
import first recovers: for an uncommitted import it removes exactly the files
its journal names and the empty directories they leave, and for a committed one
it keeps every record and copy and removes only the staging directory. Recovery
never deletes a pre-existing path and never resets the ledger. Failed imports,
and imports with nothing new, regenerate no view.

### Migration

Schema 2 adds the `imports`, `imported` and `evidence` tables. The migration is
explicit and idempotent: every other command upgrades a populated schema-1
ledger in place, in one transaction, keeping every native row, piece of
evidence and queue, and recording the new schema version; a ledger already at
schema 2 is left alone. A migration that cannot apply fails and leaves the
ledger as it was. `quruntul import-history` instead migrates inside its own
commit, so a refused or interrupted import leaves an older ledger at its
schema. The ledger is never reset.

## Legacy repositories

Synarchy's census-based flake lab and its `codex-test`/`codex-profile`
coordinators predate this design. A checkout without `.quruntul/adapter.py`
routes Synarchy to its preserved legacy workflow under
`skills/<name>/references/synarchy.md`; a checkout with one uses quruntul, with
the census still authoritative for deferrals and seeding each probe's first
status through the adapter's `seed` hook. `$playtest` keeps Synarchy on its own
harness until its adapter implements `playtest()`, and `$assess-tests` still
drains the legacy `codex-test` registry separately until that registry's closed
history is imported through `legacy_history`. `$profile`/`$performance`
keep their `codex-profile` coordinator in every repository for now.
