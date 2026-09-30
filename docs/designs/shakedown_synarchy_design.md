# Shakedown and Synarchy onboarding design

Quruntul measures Hetoimasia today. Synarchy has an adapter but an almost
empty ledger, and its `$test` history still lives in the legacy `codex-test`
registry. This arc does two things. It gives quruntul a cheap way to prove an
adapter launches every suite correctly before an expensive flake batch relies
on it. It then brings Synarchy onto quruntul: shaken down, carrying its
valuable `$test` history, and seeded.

Design state: `ready for issue processing`

Status legend: `[ ]` unprocessed · `[#N]` linked to issue N · `[no-issue]`
reviewed and deliberately not tracked separately · `[deferred]` blocked on a
concrete precondition

## Processing status

- [ ] EPIC. Shake down adapters and bring Synarchy onto quruntul
- [ ] QS-1. Add a one-trial shakedown lane that proves an adapter launches every suite
- [ ] QS-2. Import a repository's legacy `$test` history into the ledger through an adapter hook
- [ ] QS-6. Read Synarchy's `codex-test` registry through the adapter's legacy-history hook
- [ ] QS-3. Shake down every Synarchy suite and repair its adapter
- [ ] QS-4. Size `synarchy-test-headless`'s flake slices from a measured trial
- [ ] QS-5. Seed Synarchy's flake ledger

## Epic contract

- **Goal:** every Synarchy suite is launched correctly by its adapter, proven
  cheaply, and Synarchy's ledger holds its imported `$test` history and a
  complete first flake measurement.
- **Done when:**
  - `quruntul shakedown` exists and passes for Synarchy (and Hetoimasia);
  - the `codex-test` registry's history is in Synarchy's ledger;
  - `$flake` reports `no-candidate` for Synarchy on this platform.
- **Users and operators:** the owner and the Codex/Claude agents that run
  `$flake`, `$test`, `$deflake` and `$assess-tests` in Synarchy and
  Hetoimasia.
- **Arc label:** None proposed (quruntul has no arc labels yet).

## Current state and evidence

Verified on 2026-09-30 against quruntul `3d6b479` and Synarchy `origin/master`.

**Quruntul engine.**
- It has flake, test, deflake, assess and proposal lanes, one SQLite ledger
  per clone, and immutable run evidence (`docs/design.md` Principles, Lanes).
- A flake batch runs K trials (the adapter's `flake_trials`, default 10) of a
  suite's selected tests. It changes status only at the upstream head
  (`docs/design.md` Batches).
- The adapter contract has the `trial_env`, `outcomes` and `seed` hooks.
  `seed` pre-fills the first status of tests the ledger has just met, and
  only to `stable` or `flaky` (`docs/design.md` Adapter).
- The test lane records freshness per suite in `suites.last_test_run` and
  `suites.last_test_identity` (`quruntul/state.py`). `$test` selects a probe
  that has never been tested, whose identity has changed, or that is older
  than `refresh_days` (`quruntul/select.py` `test_order`).
- `$assess-tests` still drains the legacy `codex-test` registry through a
  separate path (`docs/design.md` Legacy repositories).

**Lessons from seeding Hetoimasia (2026-09-29).** Three harness defects were
each found only after a full 10-trial batch:
- the Hspec parser misread child-process output (quruntul#2);
- the adapter launched `shader-tests` without a file `run.sh` had deleted
  (hetoimasia#351);
- a consistent failure was labelled flaky (quruntul#3).

Each is the adapter's launch drifting from how CI runs the suite.

**Synarchy.**
- The adapter (synarchy#2760, merged) declares 102 suites. They are:
  - `synarchy-test-headless`, CI, Hspec, sliced into 400-test batches
    (`HSPEC_SLICE`);
  - `synarchy-test-graphical`, a desktop Hspec probe;
  - every registered probe, as a `command` or `exit` suite.
- The ledger has enumerated 4 of them: 10,528 tests, of which 10,526 are the
  headless suite's examples. 10 are `stable`, seeded from the probe census.
- The census (`docs/probe_census.json`, 100 probes) already feeds the
  adapter's `seed` hook and its deferrals.
- The legacy `codex-test` registry (`.git/codex-test/registry.json`, schema
  `codex-test-coordinator/v1`) holds:
  - 359 `$test` runs from 2026-08-12 to 2026-09-22, over 127 targets;
  - execution: 302 passed, 54 failed, 3 cancelled;
  - interpretation: 257 clean, 83 with observations, 13 blocked,
    6 inconclusive;
  - 364 snapshots, 5 assessments and 4 proposals;
  - reports and artifacts beside the registry.
- 309 of those runs, covering 85 targets, name a ledger suite exactly
  (`probe:<name>`). The 42 unmatched targets are 19 `playtest:`, 14 `probe:`
  (renamed or retired), 3 `gameplay:`, 3 `visual:`, and one each of
  `diagnostic:`, `manual:` and `graphics:`.

**Tracker.** coghex/quruntul has no issues. No Synarchy issue covers quruntul
onboarding or the registry.

## Desired experience

- **Adapter authors** run `quruntul shakedown` after changing an adapter or
  onboarding a repository. Within one trial per suite they learn whether every
  suite builds, lists its tests and reports every one of them, and which
  suites fail, before paying for 10-trial batches.
- **In Synarchy**, `$test` knows which probes were already observed and when,
  and the legacy observations and proposals are reachable from quruntul. Its
  first `$flake` sweep then spends time only on genuinely unmeasured tests.

## Scope

### In scope

- A shakedown lane in the quruntul engine, with its CLI and skill guidance.
- A generic, adapter-driven import of legacy `$test` history into the ledger.
- Synarchy's implementation of that import for the `codex-test` registry.
- Shaking down Synarchy's suites and repairing its adapter.
- Sizing the headless suite's slices, then seeding Synarchy's ledger.

### Out of scope

- The `codex-profile` lab (1.2 GB) and `$profile`/`$performance` engine lanes.
  The owner named only the `codex-test` registry as the legacy data to port
  (2026-09-30).
- Per-example Hspec flake history for Synarchy: none was found to exist.
- Synarchy's playtest harness.
- The probe census, which the `seed` hook already imports.
- Engine 0.3.0, the README fixes and the stale `docs-wip` README edit, which
  land as a standalone PR (D-11).

## Design

### Shakedown lane (proposal)

`quruntul shakedown [--target SUITE]` runs one trial of each suite that
applies on this platform (or of the one target) at the upstream head.
- It uses the same prepare, enumerate and launch path as `$flake`, so it tests
  exactly the launch that flake batches will use.
- It records a run in a new lane, `shakedown`, and writes a
  `quruntul-result/v1` report.
- It never changes a test's status.
- Per suite, it reports one of: build failed, enumeration failed, examples
  unreported (`missing`), examples failed, or clean.
- Each non-clean suite becomes one observation for `$assess-tests`.

A shakedown is advisory (D-3): flake selection never reads its result. The
`$flake` skill and the onboarding guidance recommend running one after an
adapter or harness change. It covers every suite that applies on this
platform, desktop suites included under the standing approval, one
window-opening suite at a time. A suite is clean only when it built, every
listed test reported, and none failed (D-7).

### Legacy `$test` history import (proposal)

The engine gains an import step and a new optional adapter hook, named
provisionally `legacy_history(ctx)`. The hook returns records in an
engine-defined shape: runs, their observations, assessments and proposals,
each keyed by a target id. The engine alone owns the ledger schema, so it
validates and writes them; the adapter only reads its repository's legacy
store, keeping the engine free of `codex-test` specifics.
- Imported runs are marked as imported, with provenance: the legacy run id,
  the report path, and the revision. They are never re-executed, and a second
  import writes no duplicates.
- A run whose target names a current suite attaches to that suite. Every
  other run is archived, attached to no suite and counting toward no
  freshness (D-9).
- Imported runs set each matched suite's `last_test_run` and
  `last_test_identity`, the latter to the suite's identity at import time
  (D-4).
- Only closed history is imported (D-5): runs, observations with their final
  dispositions, approved assessments, and decided proposals. None enters an
  open queue.
- The import refuses a legacy store that still has open items, naming them.
  Open means an unassessed observation, or a proposal that is not rejected,
  designed or implemented (D-8). A partial drain therefore cannot be imported
  by mistake.

### Synarchy onboarding

1. Shake down all suites (QS-3), repairing the adapter until every suite is
   clean or has a recorded disposition.
2. Measure one headless trial to size `batch_tests` (QS-4).
3. Seed (QS-5), once the owner has chosen the headless suite's trial count
   from QS-4's measurement (D-10).

## Decisions

### D-1. Shake down, then onboard Synarchy

The owner chose this arc on 2026-09-30, over onboarding Synarchy directly,
engine lanes for profiling and performance, or housekeeping first.
Consequence: the shakedown lane (QS-1) precedes every Synarchy measurement
slice.

### D-2. The legacy data to port is the `codex-test` registry

The owner named the `codex-test` registry (2026-09-30) as the valuable
Synarchy history. Not selected: the probe census, which is already seeded;
`codex-profile`; and a per-example Hspec history, which was not found.
Consequence: the import carries `$test` history, meaning runs, observations,
assessments and proposals, not flake verdicts.

### D-3. A shakedown is advisory; it never gates `$flake`

The owner chose this on 2026-09-30 (resolves Q-2). A shakedown writes its
report and observations, and flake selection ignores its result.
Rejected: skipping a suite in flake selection until its latest shakedown at
its current identity is clean. That would have prevented the Hetoimasia
pattern mechanically, but it demands a shakedown after every adapter or input
change.
Consequence: running the shakedown before expensive batches is a practice the
skills recommend, not something the engine enforces. QS-5's seeding runs
after QS-3's clean shakedown by delivery order.

### D-4. Imported legacy runs count toward `$test` freshness, at the suite's current identity

The owner chose this on 2026-09-30 (resolves Q-3). The import sets each
matched suite's `last_test_run` to its newest imported run, and
`last_test_identity` to the suite's identity at import time. That trusts
that the suite has not changed since the legacy run.
Rejected:
- leaving the identity empty, so every imported probe reads as `changed`;
- keeping the history for reference only.

Consequences:
- Synarchy's `refresh_days` is 7, and the newest legacy run is from
  2026-09-22. So at import every imported probe is already `stale` rather
  than `never-tested`.
- `$test` therefore observes the 15 probe suites the registry never saw
  first, then the imported probes, oldest observation first.
- A probe whose code changed after its legacy run is not flagged `changed`;
  it waits for its refresh window.

### D-5. Drain the legacy open items first, then import closed history

The owner chose this on 2026-09-30 (resolves Q-4). Before the import, the
legacy registry's open observations are assessed through the legacy
`$assess-tests` path (`skills/assess-tests/references/synarchy.md`), and its
proposals are dispositioned. The import then carries only closed history:
- runs;
- observations with their final dispositions;
- approved assessments;
- decided proposals.

Rejected: importing open items into quruntul's queues and retiring the legacy
drain at the same time.

Consequences:
- The legacy drain stays in `$assess-tests` until the import is done.
- The import refuses a registry that still has open items (D-8).
- `$assess-tests` loses its legacy Synarchy route once the import has run.

### D-6. Synarchy-side slices are tracked in coghex/synarchy

The owner chose this on 2026-09-30 (resolves Q-7). QS-3 to QS-6 are filed in
coghex/synarchy, so each Synarchy pull request closes its own issue. The
quruntul epic cross-references them. QS-1 and QS-2 are filed in
coghex/quruntul.
Rejected: filing all slices under the quruntul epic, where Synarchy PRs could
only reference issues in another repository.

### D-7. A shakedown runs every applicable suite; clean means zero failures

The owner chose this on 2026-09-30 (resolves Q-1). A shakedown runs every
suite that applies on this platform, desktop suites included, under the
standing desktop approval and one window-opening suite at a time. A suite is
clean only when all three hold:
- it built;
- every test it listed reported a result;
- none failed.

A failing test makes its suite non-clean, and its observation names the
harness as the first suspect.
Rejected:
- skipping desktop suites unless targeted;
- counting built-and-reported as clean, with failures merely listed.

### D-8. "Drained" means assessed and decided, checked as QS-6's precondition

The owner chose this on 2026-09-30 (resolves Q-9). The legacy store is
drained when both hold:
- every observation has been assessed;
- every proposal is rejected, designed or implemented.

The 3 accepted proposals must be carried through the legacy proposal lane or
closed first. The import refuses a store that fails this bar and names the
open items. The drain is not its own slice: QS-6 verifies the bar before
importing.
Rejected:
- requiring only observations to be assessed;
- a dedicated drain slice.

### D-9. Every unmatched registry target is archived

The owner chose this on 2026-09-30 (resolves Q-5). The 42 targets that name
no current suite are imported as archived history, attached to no suite. They
are:
- 19 `playtest:`;
- 14 renamed or retired `probe:` targets;
- 9 one-offs: `gameplay:`, `visual:`, `diagnostic:`, `manual:` and
  `graphics:`.

Nothing is dropped, and no alias map is kept. Archived runs count toward no
suite's freshness.
Rejected:
- mapping renamed probes to current suites through an adapter alias table;
- leaving playtest runs in the legacy store.

### D-10. The headless suite's trial count is decided after QS-4 measures it

The owner chose this on 2026-09-30. Q-6 stays deliberately open until QS-4
has measured one trial of `synarchy-test-headless`. QS-4 reports the measured
duration and the projected seeding time at 10 trials, then stops and asks the
owner to choose:
- 10 trials, the rule for every other suite;
- or a per-suite reduction, which would need an engine change filed
  separately.

Rejected:
- fixing 10 trials now;
- adding a per-suite trial count to the engine before any measurement.

### D-11. Housekeeping lands outside this epic

The owner chose this on 2026-09-30 (resolves Q-8). The following land as one
standalone quruntul PR, not as a slice here:
- the engine 0.3.0 bump (the `failing` status and the parser fix);
- the README's stale adapter references;
- discarding the stale `docs-wip` README edit.

## Open questions

### Q-1. Which suites does a shakedown run, and what counts as clean?

Resolved by D-7 (every applicable suite, desktop included; clean means
built, all reported, none failed).

### Q-2. Does a failed shakedown gate `$flake`?

Resolved by D-3 (advisory only).

### Q-3. Do imported legacy runs count toward `$test` freshness?

Resolved by D-4 (they count, at the suite's current identity).

### Q-4. What happens to the legacy open observations, assessments and proposals?

Resolved by D-5 (drain them through the legacy path first, then import only
closed history).

### Q-5. What becomes of the 42 unmatched registry targets?

Resolved by D-9 (all archived, attached to no suite).

### Q-6. What seeding cost is acceptable for `synarchy-test-headless`?

Deliberately open (D-10). At 10 trials and 400 tests per slice, its 10,526
examples are about 27 batches, and one trial's duration is unmeasured. QS-4
measures it, reports the projected seeding time, and stops for the owner's
choice between 10 trials and a per-suite reduction. QS-5 does not start
before that choice is recorded.

### Q-7. Where are the Synarchy-side slices tracked?

Resolved by D-6 (in coghex/synarchy, cross-referenced from the quruntul
epic).

### Q-8. Is the housekeeping outside this epic?

Resolved by D-11 (a standalone quruntul PR).

### Q-9. What does "drained" mean, and is the drain tracked?

Resolved by D-8 (assessed and decided, checked by QS-6 as a precondition;
the import refuses a store that fails it).

## Verification strategy

- **Engine:** fixture-driven lab tests in `tests/test_lab.py`, in the style of
  the existing flake-lane tests, covering:
  - a shakedown that catches a build failure, an unreported example and a
    failing example, each as one observation, with no status change;
  - an import that is idempotent, keeps provenance, and attaches matched and
    archives unmatched targets.
- **Hetoimasia regression:** a shakedown of Hetoimasia at the current head
  reports every suite clean, because its ledger is fully seeded and stable.
- **Synarchy:** the shakedown report after QS-3; the ledger's imported run and
  observation counts, which should match the registry's 359 runs and 83
  observation-bearing runs; and `$flake` reporting `no-candidate` after QS-5.

## Delivery plan

### QS-1. Add a one-trial shakedown lane that proves an adapter launches every suite

- **Outcome:** `quruntul shakedown` runs one trial per suite through the
  flake lane's launch path and reports build, enumeration,
  unreported-example and failure problems as observations, without changing
  any status.
- **Scope:** the engine lane, the CLI command, the report, `docs/design.md`,
  and skill guidance for when to run it.
- **Phase:** 1
- **Depends on:** none
- **Ordering:** critical path; can land first
- **Relevant decisions:** D-1, D-3, D-6 (filed in coghex/quruntul), D-7
- **Acceptance signals:** the lab tests above; a clean shakedown of
  Hetoimasia.
- **Out of scope:** gating flake selection (D-3).
- **Open questions:** None

### QS-2. Import a repository's legacy `$test` history into the ledger through an adapter hook

- **Outcome:** an adapter can supply legacy `$test` history, and the engine
  imports it idempotently with provenance, attaching matched targets to
  suites and archiving the rest.
- **Scope:** the hook contract, the import step, its ledger representation,
  and `docs/design.md`.
- **Phase:** 1
- **Depends on:** none
- **Ordering:** independent (parallel with QS-1)
- **Relevant decisions:** D-2, D-4, D-5, D-6 (filed in coghex/quruntul), D-8, D-9
- **Acceptance signals:** fixture tests for idempotence, provenance, matched
  and archived targets, freshness set at the current identity, only closed
  history imported, and refusal of a store with open items.
- **Out of scope:** reading any specific legacy format; profiles; importing
  open observations or proposals (D-5).
- **Open questions:** None

### QS-6. Read Synarchy's `codex-test` registry through the adapter's legacy-history hook

- **Outcome:** Synarchy's ledger holds the registry's runs, observations,
  assessments and proposals as QS-2 defines them.
- **Scope:** the Synarchy adapter hook, running the import, and the adapter
  checks.
- **Phase:** 2
- **Depends on:** QS-2, and the legacy registry drained to D-8's bar
- **Ordering:** not on the shakedown critical path
- **Relevant decisions:** D-2, D-4, D-5, D-6 (filed in coghex/synarchy), D-8, D-9
- **Acceptance signals:** imported counts match the registry; a second
  import changes nothing; the import refuses the store while an item is open;
  `$test`'s order puts never-tested probes before imported ones (D-4).
- **Out of scope:** `codex-profile`; assessing open legacy items, which
  happens before this slice.
- **Open questions:** None

### QS-3. Shake down every Synarchy suite and repair its adapter

- **Outcome:** a shakedown of Synarchy's applicable suites is clean, or
  every non-clean suite has a recorded disposition.
- **Scope:** running QS-1 on Synarchy and adapter fixes in Synarchy.
- **Phase:** 2
- **Depends on:** QS-1
- **Ordering:** critical path
- **Relevant decisions:** D-1, D-6 (filed in coghex/synarchy), D-7
- **Acceptance signals:** the shakedown report; the adapter checks pass.
- **Out of scope:** product fixes to Synarchy tests, which go through
  `$assess-tests` issues.
- **Open questions:** None

### QS-4. Size `synarchy-test-headless`'s flake slices from a measured trial

- **Outcome:** the headless suite's `batch_tests` is set from one measured
  trial's duration, so each flake batch fits its `batch_seconds`. The slice
  reports the projected seeding time at 10 trials, then stops for the
  owner's trial-count choice (D-10).
- **Scope:** measurement, the adapter's slice size, and the recorded owner
  choice. A per-suite trial count, if chosen, is an engine change filed
  separately.
- **Phase:** 2
- **Depends on:** QS-3
- **Ordering:** critical path
- **Relevant decisions:** D-1, D-6 (filed in coghex/synarchy), D-10
- **Acceptance signals:** the recorded measurement and projection; a slice
  batch finishing within `batch_seconds`; the owner's trial-count choice
  recorded.
- **Out of scope:** splitting the Hspec executable.
- **Open questions:** Q-6

### QS-5. Seed Synarchy's flake ledger

- **Outcome:** `$flake` reports `no-candidate` for Synarchy on this platform,
  and every failing or flaky test is assessed.
- **Scope:** running `$flake` to completion, and `$assess-tests` on its
  observations.
- **Phase:** 3
- **Depends on:** QS-3, QS-4 and its recorded trial-count choice (D-10).
  Not QS-6: flake selection reads no test-lane history (`select.flake_order`),
  so the import affects only `$test`.
- **Ordering:** critical path; last
- **Relevant decisions:** D-1, D-3, D-6 (filed in coghex/synarchy), D-10
- **Acceptance signals:** the ledger summary; an approved assessment of every
  observation.
- **Out of scope:** fixing flaky tests (`$deflake`).
- **Open questions:** Q-6
