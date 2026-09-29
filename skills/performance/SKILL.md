---
name: performance
description: Assess, correlate, and verify completed local `$profile` reports and decide whether to act, measure, or stop. When the user explicitly authorizes delivery of an accepted recommendation, implement that one finding in an isolated worktree, validate its immutable commit through sequential `$profile` subagents, and open an unlinked standalone PR if every gate passes. Use when the user invokes `$performance`, asks to analyze local profiling evidence, or asks to carry an accepted performance fix through validation and PR creation. Never create an issue, publish lab documents, or merge.
---

# Performance

Turn completed `$profile` reports into one conservative current-code assessment
and, only with separate explicit authorization, deliver one accepted finding.
The source measurements are evidence, not verdicts: verify their methodology,
comparability, artifacts, and relevance before recommending work.

Use the installed sibling `$profile` skill's
`scripts/profile_coordinator.py` for every report claim, assessment document,
and coordinator-owned lab, analysis, or delivery transition. Use its delivery
commands to claim and record implementation worktrees; ordinary Git operates
inside the claimed worktree. If the sibling skill or coordinator is
unavailable, stop as blocked. Never hand-edit its registry or generated
coordinator.

All reports, assessments, and coordination state remain local under the
repository's common Git directory at `codex-profile/`. Never add, commit, push,
publish, or file them.

## Repositories

The coordinator is repository-agnostic; its state lives under each repository's
common Git directory at `codex-profile/`. Read the repository's `AGENTS.md`
completely — its delivery rules govern the delivery mode below. Hetoimasia
workloads and their comparison rules are in the sibling `profile` skill's
`references/hetoimasia.md`.

## Modes and authorization

- **Assessment mode is the default.** Run sections 1–4, write the assessment,
  and stop. Do not build, profile, edit implementation, commit, push, create an
  issue, or open a PR.
- **Delivery mode requires an explicit user request** to implement or run the
  pipeline for one completed `recommendations` assessment or named `PFN-nnn`.
  A recommendation, user agreement that it sounds useful, or approval of the
  assessment document alone is not delivery authorization.
- Delivery authorization covers only the accepted finding's minimal
  implementation, relevant semantic gates, a local candidate commit, the
  assessment's validation profiles through fresh subagents, branch push, and
  one standalone PR when all gates pass. It never authorizes an issue, merge,
  unrelated optimization, published lab artifact, or broad full-CI run.

When delivery mode applies, first verify the completed assessment and its
current-code premise, then read
[delivery.md](delivery.md) completely and follow it. Do
not create a duplicate assessment for already-consumed source reports.

## 1. Select and claim reports

Resolve the Git repository, then inspect available evidence:

```bash
python3 <profile-skill-dir>/scripts/profile_coordinator.py init --repo <repo>
python3 <profile-skill-dir>/scripts/profile_coordinator.py analysis-list --repo <repo>
python3 <profile-skill-dir>/scripts/profile_coordinator.py analysis-list \
  --repo <repo> --assessments --active
```

Interpret fresh-invocation arguments as follows:

- no argument: all currently unprocessed completed profile reports;
- a profile run ID: that exact report;
- several run IDs: one correlated assessment of that set; and
- otherwise: an area hint matched against area, profile ID, workload, and
  performance question.

Claim the selected reports against the current upstream base:

```bash
python3 <profile-skill-dir>/scripts/profile_coordinator.py analysis-claim \
  --repo <repo> [--run <run-id> ...] [--area <hint>] \
  [--base-ref origin/master]
```

The claim is atomic. Exit status 3 names an existing owner; do not duplicate or
steal it. Save the `assessment_id`, current assessment revision, source report
paths, and source run IDs.

Create an immutable current-code worktree:

```bash
python3 <profile-skill-dir>/scripts/profile_coordinator.py analysis-worktree \
  --repo <repo> --assessment-id <assessment-id>
```

Read current code only from that worktree. Read historical code with
`git show <measured-revision>:<path>` when later drift matters. Do not build,
run a profiler, perform a new benchmark, or edit implementation.

## 2. Verify the evidence before interpreting it

Read every claimed report, command log, and raw artifact needed for its claims.
For each source, establish:

1. whether the workload reached its intended completion boundary;
2. whether compilation, warm-up, and measurement were separated correctly;
3. which values are direct measurements, summaries, profiler attribution, or
   agent interpretation;
4. whether sample count and variation support the stated conclusion;
5. how instrumentation distorted parallelism, wall time, allocation, GC, or
   residency; and
6. whether relevant code or workload inputs changed after the measured commit.

Only compare runs when all of these match:

- `comparison_key`;
- machine fingerprint;
- toolchain fingerprint;
- build-method fingerprint (optimization, instrumentation, flags, and runtime
  support); and
- workload fingerprint.

The revision and executable digest are artifact provenance and should differ
across a real before/after code change. Do not require those artifact identities
to match. For legacy reports that combined method and artifact identity in one
`build_fingerprint`, compare the method components explicitly and retain the
revision/digest difference as provenance.

Even then, verify that measurement semantics, warm-up, sample treatment, and
runtime options match. A shared comparison key expresses intent, not proof.

Do not claim an improvement or regression from:

- cost-centre percentages alone;
- absolute wall time from a profiled build versus a production build;
- allocation versus residency;
- maximum residency versus long-run growth;
- different seeds, scenarios, machine states, toolchains, or runtime options;
  or
- one noisy sample without a repository-specific deterministic reason.

A sound single diagnostic profile may establish a hotspot or retention shape.
It cannot establish the savings from a change that was not measured.

## 3. Inspect current code and decide what can be done now

Trace each supported hotspot or memory behavior through the measured revision
and current code. Determine whether later work fixed, moved, invalidated, or
made the evidence stale. Correlate sources by cause, not merely by module name.

Each actionable recommendation must state:

- the exact source profiles and measurements supporting it;
- the implicated current code surface and mechanism;
- whether the evidence establishes a characteristic, a comparative change, or
  only a hypothesis;
- the expected direction of improvement without inventing an unmeasured
  magnitude;
- semantic, determinism, memory, concurrency, rendering, and maintenance risks;
- a smallest plausible implementation boundary; and
- the exact follow-up `$profile` experiment that could validate or falsify it.

Translate measurement scale into operational meaning before making the
decision. In particular, distinguish cumulative allocation traffic from live
heap residency, process RSS, RTS-reserved memory, retained growth, and a leak;
distinguish aggregate CPU seconds from elapsed latency; and state whether an
apparently large value is currently a user-visible cost, a tuning lead, or only
a profiler counter. Do not assume the user knows whether a raw number is large.

Prefer recommendations that remove demonstrated repeated work, excessive
allocation, retained ownership, synchronization, or avoidable data movement.
Do not recommend low-level rewrites solely because a function appears in a
profile. Include a no-change disposition when the cost is intentional,
irreducible at the current boundary, already fixed, too uncertain, or not worth
the risk.

If evidence is insufficient, recommend a focused follow-up measurement rather
than an implementation. In assessment mode, do not run it automatically.

Every assessment must choose and say one action class plainly:

- **Act now:** the evidence supports a specific production-code, runtime-
  configuration, or operational change. Name the smallest change and its
  validation profile. A plausible hypothesis alone does not qualify.
- **Measure only:** no production change is justified. Say exactly, "The only
  justified action is another `$profile` experiment; do not change code or
  runtime defaults yet," then rank the smallest experiments that would unlock
  a decision. Each experiment must include a stable proposed `profile_id`,
  `comparison_key`, area, workload, metric family, instrument, controls, and
  decision rule so a later `$profile` invocation can execute it without
  redesigning it.
- **Stop:** neither a production change nor more profiling is warranted. State
  why the cost is expected, superseded, too weak, or not worth pursuing.

Do not mix these classes. An assessment with useful hypotheses but no supported
production change is **Measure only**, even when the hypotheses suggest likely
future implementation work.

## 4. Write and complete the assessment

Create the local skeleton:

```bash
python3 <profile-skill-dir>/scripts/profile_coordinator.py assessment-init \
  --repo <repo> --assessment-id <assessment-id>
```

Edit the returned `*.performance-assessment.md` with `apply_patch`. Replace
every placeholder and give every source report exactly one disposition line.
Use sequential `PFN-nnn` sections for independent findings; combine sources
only when the evidence supports one mechanism.

Set `assessment_outcome` to exactly one of:

- `recommendations`: **Act now** — at least one production code,
  configuration, or operational action is supported now;
- `follow-up-measurement`: **Measure only** — no production action is supported
  and the only justified next action is one or more subsequent `$profile`
  experiments; or
- `no-action`: **Stop** — no production action and no worthwhile follow-up
  profile.

Validate the assessment and consume its sources only after it is complete:

```bash
python3 <profile-skill-dir>/scripts/profile_coordinator.py analysis-complete \
  --repo <repo> --assessment-id <assessment-id> \
  --outcome <recommendations|follow-up-measurement|no-action>
python3 <profile-skill-dir>/scripts/profile_coordinator.py analysis-cleanup \
  --repo <repo> --assessment-id <assessment-id>
```

Fix validation errors rather than bypassing them. Cleanup never forces removal.

If cancelled or blocked before completion, release every source report:

```bash
python3 <profile-skill-dir>/scripts/profile_coordinator.py analysis-release \
  --repo <repo> --assessment-id <assessment-id> --reason <reason>
python3 <profile-skill-dir>/scripts/profile_coordinator.py analysis-cleanup \
  --repo <repo> --assessment-id <assessment-id>
```

Lead the user-facing result with the action class and a literal statement of
what they should do now. For `follow-up-measurement`, explicitly say that more
profiling is the only current action and that code and runtime defaults should
remain unchanged. Then report the assessment ID and current-code revision,
source run count, comparability groups, plain-language magnitude, supported
action or ranked follow-up profiles, assessment path, and cleanup result. Do
not enter delivery mode, create a tracker item, or publish the assessment
without the separate explicit authorization described above. In authorized
delivery mode, the delivery reference—not the assessment itself—is the
workflow and stopping contract.
