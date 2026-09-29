---
name: deflake
description: "Select one unresolved Synarchy probe failure from the flake coordinator, investigate its retained measurement, and record an evidence-backed diagnosis. Use for $deflake, diagnosing recorded flake-lab failures, or continuing a probe diagnosis. Accept a probe key or handoff path. Do not use for collecting new census measurements ($flake), generic debugging, or automatic repair publication."
---

# Diagnose a Recorded Flake-Lab Failure

One invocation investigates one probe and leaves a durable diagnosis. Include
probes that fail every run: distinguish intermittent failures, consistent
failures, missing checks, and harness errors. Do not assume the probe is wrong.

Read the repository's current `AGENTS.md`/`CLAUDE.md`. The coordinator is its
census in the worktree on branch `docs-wip`, together with canonical probe
claims. Keep the primary checkout clean. `$flake` collects measurements;
this skill consumes them. Despite its name, `tools/deflake.py` is the
measurement command, not an automatic diagnosis command.

## Select from the coordinator

Run from Synarchy:

```bash
python3 tools/probe_census.py --seed
python3 tools/probe_census.py --summary --json
python3 tools/probe_claim.py --status --json
```

Resolve `docs-wip` by branch. Read the selected row's current cohort, attempts,
deferral, outcomes, and artifact paths using the census's read/validation APIs.
Read prior notes in `docs/deflake_diagnoses/` if present. Census `attempts`
are measurement attempts, not diagnosis records.

- Honor an explicitly named probe or handoff. Otherwise select an unresolved,
  measured probe over its configured tolerance, or one with unresolved
  FAIL/MISSING targets in retained evidence. Prefer fresh evidence, then the
  newest measurement, then the exact key. Use canonical summary values for
  tolerance and staleness. Do not treat unmeasured probes as failing.
- Exclude deferred and actively claimed probes. For a named deferred probe,
  report its stored reason and resume condition; never bypass the deferral.
- Match prior diagnoses/outcomes by probe, commit, measurement timestamp, and
  retained invocation. Resume unfinished work at its next experiment. Skip an
  already-concluded diagnosis of the same evidence unless explicitly requested
  or new evidence justifies revisiting it. A gate-blocked note does not count
  as a completed diagnosis or hide the original failure.
- Stale failures remain candidates; reproduce their recorded revision before
  comparing with current code. Harness errors are operational failures, not
  valid failure rates. If nothing qualifies, report that and stop; do not
  start a measurement sweep, migrate a probe, or change policy.

State the selected probe, revision, measurement date, failing/total run count,
targets, prior diagnosis state, and selection reason. For example,
`$deflake mental_efficiency` selects that probe when available. Default
selection must remain driven by current coordinator data, not that example.

## Recover authentic evidence

Locate the producer-written `probe-flake-result.json` and adjacent
`probe-flake-result.json-handoff.json` from the recorded artifact paths.
Match probe, commit, timestamp, and invocation to the selected measurement.
Validate the result with `probe_census.validate_result`, then run:

```bash
python3 tools/deflake_diagnosis.py --handoff /absolute/path/to/handoff.json --json
```

This only validates documents; it neither diagnoses nor launches an engine.
Read `tools/README.md`'s `deflake_diagnosis.py` section for the current contract.
Inspect all original FAIL/MISSING targets and retained events, stdout, stderr,
and engine logs. A downstream assertion may merely reflect missing setup data.

Never reconstruct a missing handoff from census statistics, rewrite its origin,
or change validator arguments to force acceptance. The current gate can reject
a real handoff from an isolated worktree because it requires the primary
checkout. Record the exact rejection separately from the probe failure.
Continue ordinary diagnosis from authentic, validated raw results and logs
when sufficient evidence remains, but do not claim canonical diagnosis/repair
acceptance. If evidence is insufficient, record the missing evidence and the
next reproduction needed. Do not silently replace the measurement or target.

## Coordinate and investigate

Before experiments or shared diagnosis-note edits, read and use
`probe_claim_lease.acquire`, `probe_claim_storage.repository_claim_root`, and
`probe_census.record_claim` to acquire and audit canonical probe ownership.
Keep a live owner process with `probe_claim_lease.Renewer` throughout agent
reasoning and experiments; a one-shot acquisition whose lease expires is not
coordination. Recheck deferral and evidence identity after acquisition. If a
competitor won or the selected state changed, release anything owned and
reassess; do not launch conflicting work. `probe_claim.py --probe ... --runs`
runs and ingests a measurement; it is not a claim-only CLI.

Read and apply the installed diagnose skill (`~/.codex/skills/diagnose/SKILL.md`) for
reproduction, hypotheses, causal experiments, and confidence. Work in an
isolated checkout pinned to the recorded commit. Read the probe and relevant
domain contracts; test its assumptions about content, fixtures, timing, and
game behavior. Start with the smallest discriminating experiment and preserve
the original case. Record actual commands, environment, configuration, exits,
and artifacts; distinguish current-code comparisons from baseline evidence.

Use repository engine-preparation and resource-lock helpers, including holds
while installing configuration. Prepare binaries before measurement resource
holds to avoid nested build-lock deadlocks. Follow the repository's lock-wait
policy. Scope an installed required toolchain version to this invocation rather
than changing global defaults. Stop experiments on lost ownership and release
only this invocation's claims/resources on managed exit.

Narrow instrumentation or trial patches are diagnostic experiments. Do not
weaken checks, add arbitrary sleeps, retry until green, or implement a
production fix merely because the probe fails. Distinguish fixture/expectation
defects, production defects, missing content, environment failures, and
unresolved causes. One passing retry does not erase earlier evidence.

## Record and finish

Create or resume one local report at
`docs/deflake_diagnoses/<probe>_<measurement-timestamp>.md` in `docs-wip`.
Include measurement identity; artifact/handoff links; failure counts and
targets; gate acceptance or exact rejection; claim identity; experiments and
results; conclusion and confidence; corrective direction; remaining evidence
gaps and next action. Mark the diagnosis ongoing, concluded, or blocked on
specific evidence. Preserve previous experiments when resuming.

The census owns measurements and policy; reports hold diagnostic narrative.
Never hand-edit census JSON or invent an outcome. Where evidence satisfies a
canonical outcome route, read the relevant `tools/README.md` sections and use
`deflake_diagnosis.py` plus `deflake_outcome.py` to evaluate and record it.
Supply only evidence actually produced. Otherwise leave measurements unchanged,
retain the diagnosis report, and explicitly state that no canonical outcome
was recorded. Do not misclassify a gate or environment failure as inability
to reproduce the probe failure.

Default scope ends with diagnosis and a corrective recommendation, without
creating issues, opening PRs, or publishing docs. If repair delivery is also
authorized, follow repository delivery rules. A canonical probe-repair verdict
requires its accepted original handoff, controlled clean baseline and committed
repair verification on a common base, ten sequential runs per batch under
matching conditions, stable check meanings, and evaluator acceptance. Narrow
diagnostic experiments do not satisfy those gates. Keep accompanying evidence
in the code PR for mixed work.

End with the finding, evidence strength, report path, any workflow limitation,
and ownership cleanup. Diagnosis alone never marks a probe fixed, removes its
failure, changes tolerance, or resumes a deferral.
