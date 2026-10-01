---
name: profile
description: "Run one coordinated, local-only performance experiment for a committed Git revision in a persistent profiling worktree, selecting a specific area, workload, metric, and instrument and retaining reproducible reports and raw artifacts. Use when the user invokes `$profile`, asks to profile a subsystem, establish a performance baseline, or measure CPU, allocation, memory, GC, concurrency, or rendering behavior through this lab. Do not use for an ordinary test command or to implement an optimization. `$profile N` runs N experiments in sequence; `$profile <hint>` narrows the question."
---

# Profile

Measure one well-formed performance question and leave immutable local evidence
for `$performance`. Do not modify production code, file an issue, open a PR,
push a branch, publish documentation, or recommend implementation changes as if
they were established by the measurement.

Run this skill in a **Class B** session (see the `model-classes` skill).

The lab is deliberately local. Its `profile-lab` branch never carries unique
commits and only fast-forwards from the upstream base. The persistent worktree
may detach temporarily at a committed target revision. Registry state, the
generated coordinator Markdown, reports, logs, and raw artifacts live under the
repository's common Git directory at `codex-profile/` and never enter Git.

Use `scripts/profile_coordinator.py` for every worktree, claim, command, report,
and restoration transition. Never hand-edit `registry.json` or
`coordinator.md`.

## Parse the argument

Every quruntul skill parses its argument the same way. A leading positive integer
`N` means N experiments run **serially**, each a fresh `$profile` invocation
(and a fresh subagent when you can delegate) with its own refresh, claim,
report and restore. Anything after it is the hint below. Stop a sequence early
when an experiment ends `blocked` for a reason that is not specific to it, when
no unanswered question remains, or when the user says stop. The persistent lab
is single-owner by design: two experiments never run at once, and exit status 3
means another session holds it — report that rather than waiting in a loop.
After each experiment post one line: profile id, outcome, report path.

## 1. Resolve and refresh the lab

Resolve the current Git top level without changing it, then inspect local state:

```bash
python3 <skill-dir>/scripts/profile_coordinator.py init --repo <repo>
python3 <skill-dir>/scripts/profile_coordinator.py list --repo <repo> --active
python3 <skill-dir>/scripts/profile_coordinator.py list --repo <repo> --limit 30
python3 <skill-dir>/scripts/profile_coordinator.py analysis-list \
  --repo <repo> --assessments
```

Completed `$performance` assessments are the lab's decision and follow-up
backlog. Read recent assessments with outcome `follow-up-measurement` before
selecting a question. Their `Follow-up profiling` sections define experiments
for later invocations. Ordinary `$performance` assessment mode does not run
those experiments; an explicitly authorized `$performance` delivery workflow
may delegate each one to a fresh `$profile` subagent at an immutable candidate
commit.

When the user supplies an assessment ID, `PFN-nnn`, proposed `profile_id`, or
descriptive follow-up, resolve that exact experiment. With no hint, prefer the
highest-priority still-unanswered experiment from the newest relevant
`follow-up-measurement` assessment over inventing an unrelated profile. Before
claiming it, verify that no active or completed equivalent run already answers
it and that current code has not made its premise stale. Cite the originating
assessment and finding in the claim rationale. If an older assessment omitted
a proposed `profile_id` or `comparison_key`, choose stable values without
changing its question.

Refresh before selecting an experiment:

```bash
python3 <skill-dir>/scripts/profile_coordinator.py refresh \
  --repo <repo> --base-ref origin/master [--target-ref <committed-ref>]
```

Use the repository's actual upstream default when it is not `origin/master`.
Without `--target-ref`, profile the refreshed base. An explicit target ref may
name another committed revision; the snapshot still records the base against
which the lab branch is maintained. Save the returned `snapshot_id`, exact
revision, and worktree path.

The target is always a commit. A dirty source checkout is recorded but its
uncommitted changes are not copied or measured. Say this plainly when it could
surprise the user. Do not synthesize a commit, apply a patch, stash, or copy
dirty files into the lab.

Refresh is serialized and refuses a dirty lab, an active run, or a
non-fast-forward branch. Never repair those states with a force checkout,
reset, clean, deletion, rebase, or local commit.

Read the target snapshot's `AGENTS.md`, project instructions, build policy,
existing benchmark/profiling apparatus, and relevant historical measurements.
For Synarchy, read [references/synarchy-workloads.md](references/synarchy-workloads.md)
completely before selecting commands. For Hetoimasia, read
[references/hetoimasia.md](references/hetoimasia.md) completely.

## 2. Select one performance question

Treat text after `$profile` as an optional area, metric, workload, or ref hint.
One run owns exactly one tuple:

```text
area + workload + metric family + primary instrument
```

Give it a stable semantic `profile_id`, such as
`worldgen:w128-init:cpu-cost-centres`, and a stable `comparison_key` that names
the logical experiment rather than a date, port, or revision. State one
question that the evidence can answer.

Do not turn on every profiling facility. Instrumentation changes runtime
behavior, and different tools answer different questions. Keep these families
separate unless a repository-owned experiment explicitly defines a controlled
multi-phase method:

- production throughput, allocation, GC, and peak residency;
- CPU or allocation attribution by cost centre;
- retained-heap shape over time;
- GC pause and concurrency scheduling through an eventlog;
- renderer/GPU timing and residency; and
- application-owned counters or phase timings.

An experiment may include an uninstrumented control, warm-up, and repeated
samples when those are part of answering the one question. It may not expand
into unrelated workloads or instruments.

With an area hint, inspect its runtime path, existing workload apparatus, and
recent history. Without a hint, rank candidates by:

1. a still-unanswered follow-up explicitly prioritized by `$performance`;
2. player impact, latency, memory pressure, or dominant runtime cost;
3. stale or absent profiling evidence after relevant code changed;
4. likelihood that the result distinguishes actionable hypotheses;
5. availability of a reproducible workload and observable completion boundary;
6. runtime, environmental sensitivity, and instrumentation distortion as costs.

Avoid a recent equivalent run when relevant code and workload inputs have not
changed. Do not use a new seed or cosmetic flag variation as a new question.
If no trustworthy workload exists, end with a `blocked` report explaining the
missing apparatus; do not improvise an unverifiable benchmark.

Before any build or run, claim the selected tuple:

```bash
python3 <skill-dir>/scripts/profile_coordinator.py claim \
  --repo <repo> \
  --snapshot-id <snapshot-id> \
  --profile-id <stable-id> \
  --area <area> \
  --workload-id <stable-workload-id> \
  --metric-family <metric-family> \
  --instrument <primary-instrument> \
  --question <one-answerable-question> \
  --rationale <why-this-is-the-best-next-experiment> \
  --comparison-key <stable-logical-comparison-key>
```

An exit status of 3 means another session owns the single persistent lab. Do
not duplicate it, wait in a hidden loop, or seize its worktree.

## 3. Prepare and execute reproducibly

Detach the persistent worktree at the claimed revision:

```bash
python3 <skill-dir>/scripts/profile_coordinator.py prepare \
  --repo <repo> --run-id <run-id>
```

Do every repository read, build, workload run, and artifact interpretation from
that worktree. Do not edit it. Put driver scripts, profiler output, dumps, and
processed data in the absolute `artifact_dir` returned by the claim. The
coordinator exposes the same path as `CODEX_PROFILE_ARTIFACT_DIR` to commands.

Run each build, warm-up, and measurement command through the coordinator:

```bash
python3 <skill-dir>/scripts/profile_coordinator.py exec \
  --repo <repo> --run-id <run-id> --phase <build|warmup|measure> \
  --timeout <seconds> [--final] -- <argv> ...
```

Use `--final` only on the last planned measurement command. A failed, timed-out,
or cancelled command automatically reaches the reporting boundary. Do not
silently rerun a surprising sample or replace it with a cleaner result. Record
the uncertainty and make a later coordinated run the follow-up.

If setup becomes impossible before execution, use:

```bash
python3 <skill-dir>/scripts/profile_coordinator.py block \
  --repo <repo> --run-id <run-id> --reason <specific-blocker>
```

Choose a method appropriate to the question:

- Separate compilation and warm-up from measured intervals.
- Fix the workload inputs and completion boundary.
- Record every relevant environment variable and runtime option.
- Use enough samples to show variation for noisy production timing; preserve
  every raw sample and state the summary statistic and outlier policy.
- Distinguish cold startup, warm steady state, throughput, latency, allocation,
  residency, and growth over time. None is a synonym for another.
- Never compare absolute values from instrumented and production builds.
- Never infer a speedup or regression from percentages within one profile.

Record machine, OS, toolchain, build, workload, and runtime details precisely.
The report separates build-method identity (optimization, instrumentation,
flags, and runtime support) from artifact identity (revision and executable
digest). A before/after code change should keep the method fingerprint stable
while changing artifact provenance. The method and workload fingerprints must
change whenever a control changes enough to make comparison unsafe. Matching
`comparison_key` values express intended logical comparability; matching
controls establish actual comparability.

## 4. Write one validated report

Create the skeleton after the run reaches `awaiting-report`:

```bash
python3 <skill-dir>/scripts/profile_coordinator.py report-init \
  --repo <repo> --run-id <run-id>
```

Edit the returned `*.profile-result.md` with `apply_patch`. Replace every
`<REPLACE: ...>` marker. Set `measurement_status` to exactly one of:

- `complete`: the method produced interpretable evidence;
- `inconclusive`: execution finished but the evidence cannot answer the
  question; or
- `blocked`: setup or environment prevented a meaningful measurement.

The report must preserve exact source provenance, commands, logs, raw sample
paths, environment fingerprints, workload boundary, summary method, observed
variation, and instrumentation limitations. Interpret only what was measured.
Do not turn a hotspot into a proposed code change here; that judgement belongs
to `$performance`.

Validate and attach it:

```bash
python3 <skill-dir>/scripts/profile_coordinator.py report \
  --repo <repo> --run-id <run-id> \
  --outcome <complete|inconclusive|blocked>
```

Fix validation errors rather than bypassing them.

## 5. Restore the persistent lab and hand off

After attaching the report, reattach the clean persistent worktree:

```bash
python3 <skill-dir>/scripts/profile_coordinator.py restore \
  --repo <repo> --run-id <run-id>
```

Restoration never forces away files. If profiler output accidentally dirtied
the worktree, preserve it and report the path; do not clean or reset it.

Report the profile ID and question, target ref and exact revision, workload,
instrument, execution and measurement outcomes, important measurement summary,
report and artifact paths, comparison key, and restoration result. Recommend
invoking `$performance` when the user wants interpretation, but never invoke it
automatically.

When the run came from a performance follow-up, also report the originating
assessment ID and `PFN-nnn`. Do not claim that the follow-up authorizes a code
or runtime-default change; a later `$performance` invocation makes that
decision after processing the new evidence.
