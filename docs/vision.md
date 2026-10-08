# Quruntul vision and design guardrails

**Status: accepted by the owner on 2026-10-01.** This is the heading that
`$guide` reviews against. It was drafted from the owner's statement of purpose
and decisions about its audience, agents, profiling, gating and platforms
(2026-10-01), the contract in [the design](design.md), the
[working agreements](../AGENTS.md), and the owner decisions of 2026-09-30
(D-1 to D-18) in the shakedown and legacy-import design record. That record
was removed from this tree on 2026-10-08 and stays in this repository's
history (commits `e4696b8`, `0a9db4c`, `0a1c7e3`, `aea8e06`).
It was revised on 2026-10-08 for the owner's decision that the harness stays
generic and independently adoptable (V-1, V-4, V-8, V-12).

Quruntul holds the testing framework for the owner's other projects. It is one
local testing lab — a standard-library Python engine and the Codex skills that
drive it — that measures, observes and diagnoses other repositories' tests. It
has no product of its own: its value is the evidence it gives the projects
that adopt it about which tests are flaky, which probes are due, and what
those runs showed.

This document holds the direction: what the lab must stay true to, and where it
is heading. Concrete, unambiguous rules (statuses, lanes, claims, formats,
schemas) live in [the design](design.md), which is the contract. When the two
disagree, raise it with the owner; do not quietly pick one. Current work and
coverage belong in the tracker and [guide reports](guide/).

New explicit owner decisions can revise this document. Record the decision and
its date beside the change; do not change intent to fit new code or an agent
recommendation. Preserve these principle IDs; retire a principle rather than
renumbering.

## Purpose and boundaries

### V-1. A lab for the owner's projects that belongs to none of them

Quruntul serves the owner's own repositories, not outside users (owner decision
2026-10-01). The engine is still generic, because more than one of those
repositories depends on it. A consuming repository opts in by committing
`.quruntul/adapter.py`, which describes its suites and how to build them; that
adapter is versioned with the code it describes and imported from the pinned
checkout being measured. Everything specific to a consumer — its suites, build
steps, desktop consent, legacy stores — belongs in its adapter, never in the
engine. The engine owns enumeration, selection, repetition, process lifetime
and recording, so every consumer gets the same mechanics. The adapter contract
grows only through optional hooks, kept compatible for the owner's consumers
rather than for a public audience, and an adapter that relies on newer engine
behaviour refuses an older engine through `ctx.version`. See
[design §Adapter](design.md#adapter) and [AGENTS](../AGENTS.md).

The harness stays generic enough for any of the owner's repositories to adopt
on its own (owner decision 2026-10-08). Quruntul's engine, skills, docs and
plans name no consumer-specific selection, budget, special case or onboarding
step, and depend on no consumer's progress. A consumer's adapter,
configuration and policy own its suite selections, trial counts, slice sizes,
deferrals and onboarding, and its own tracker owns that work. Quruntul gains
only generic mechanics that every consumer can use.

### V-2. The engine owns mechanics; Codex agents own judgement

Selection, claims, pinned checkouts, process lifetime, recording and validation
are code, so they behave the same for every agent and every run. Interpreting
evidence, diagnosing, fixing and writing observations are the skills' work.
Every skill parses its argument the same way (`quruntul resolve-arg`). The
engine never interprets evidence beyond the mechanical: which trial passed,
which test failed, which status follows. The skills are Codex skills: Codex
agents drive every lane, and Claude takes part only in reviewing quruntul's own
pull requests (owner decision 2026-10-01). See
[design §Principles](design.md#principles) and
[§Argument grammar](design.md#argument-grammar).

### V-3. The engine never touches a tracker, pushes or merges

Only skills file issues or open pull requests, and only where the design says
so, under the consuming repository's own delivery rules. A skill proposes and
the owner approves before anything is filed. Quruntul's own delivery follows
its [working agreements](../AGENTS.md): one issue, one pull request, one
worktree, reviewed by the other agent.

### V-4. Advisory to every consumer, never a gate

Quruntul reports evidence, files issues on the owner's approval, and opens fix
pull requests under the consuming repository's delivery rules. It never gates a
consuming repository's CI or merges: no status, observation or missing
measurement blocks a consumer's delivery, whose rules stay its own (owner
decision 2026-10-01). Flake, shakedown and seeding campaigns are advisory
stability work: never a required gate for a consumer or for future harness
work (owner decision 2026-10-08).

## Evidence and the ledger

### V-5. One local ledger per clone; evidence is immutable

Every lane and linked worktree shares one SQLite ledger in the repository's
common Git directory. It is local evidence, not a remote backup;
`quruntul export` makes a portable archive. A completed trial, result or
observation is never rewritten, and recovery after a crash consumes retained
files and never replays a trial. A schema change has an explicit migration and
a test, and a ledger is never reset. See [design §Principles](design.md#principles).

### V-6. Measure each test once, and trust the measurement

Flakiness is measured once per test, with K fresh-process trials at the
upstream head; a stable test stays stable, however old its measurement, until
the owner marks it flaky after a real failure. A test that fails every trial is
`failing`, not flaky: the harness and environment are suspected first. A
candidate revision's batch is evidence, never a status change. Observation
(`$test` freshness) is separate from flakiness and never changes a status. See
[design §Test status](design.md#test-status) and [§Batches](design.md#batches).

### V-7. Cheap proof before expensive measurement, as advice

A shakedown proves in one trial per suite that an adapter launches every suite
the way CI does, before ten-trial batches rely on that launch. It is advisory:
flake selection never reads its result, and running it after an adapter or
harness change is a practice the skills recommend, not a gate (owner decisions
D-3 and D-7, 2026-09-30). See [design §Shakedowns](design.md#shakedowns).

### V-8. Legacy history comes in closed, through the adapter

An older lab's history is imported once it is drained: only closed runs,
assessed observations, approved assessments and decided proposals, never open
work. The adapter reads its own legacy store, and the engine alone validates
and writes the ledger, all or nothing. Matched runs count toward `$test`
freshness at the suite's current identity, and everything else is archived,
not dropped (owner decisions D-2, D-4, D-5, D-8 and D-9, 2026-09-30). A
consumer's legacy workflow and store belong to that consumer, not to
quruntul's skills (owner decision 2026-10-08; see
[design §Legacy repositories](design.md#legacy-repositories)). See
[design §Legacy history import](design.md#legacy-history-import).

## Operation

### V-9. Parallel by default, through claims

Claims are per resource — a suite, a test, an observation batch, a checkout's
build, the desktop — not one lab-wide lock, so independent agents in separate
sessions run different suites at once without coordinating by hand. Only one
window-opening run happens at a time, and a desktop run needs the consuming
repository's approval. A claim whose owner stops heartbeating is taken over as
a recorded event. See [design §Claims](design.md#claims).

### V-10. A dependency-free engine, tested without real tools

The engine is standard-library Python 3.11 or later, with no dependencies, so
it installs anywhere the consumers build. Its own tests use temporary
repositories and fixture executables, never a real compiler, display or
network. See [AGENTS](../AGENTS.md).

### V-11. macOS first, with no serious platform dependency

The owner's workflow runs on macOS, so that is where the lab runs and is
proven. The engine takes on no serious platform dependency all the same, and is
expected to work on Linux; a suite names the platforms (Darwin, Linux) it
applies to. The owner's projects balance coverage against efficiency by
validating locally on macOS and in GitHub CI on Linux (owner decision
2026-10-01).

### V-12. Checks are classified by the work they actually do

Finite correctness checks (Hspec assertions, Python checks) and their existing
gates are mandatory. Long game simulations and campaign probes are optional,
local only and never CI, even when Hspec or Python launches them. The class
follows the actual work, never the wrapper's name or framework (owner decision
2026-10-08). Quruntul stays useful for both: running mandatory correctness
checks, and optional local experiments. See
[design §Check classes](design.md#check-classes).

## Status of the lanes

**Built and in use:** the flake, shakedown, deflake, test and assess lanes, the
proposal lane, legacy history import, and duplicate Hspec path detection.

**Consumer onboarding is the consumer's work (owner decision 2026-10-08):**
shaking down a repository's adapter, importing its legacy history, sizing its
suites and seeding its ledger are planned and tracked by that repository,
using these generic lanes. Epic #5 delivered the generic mechanics (the
shakedown lane and the legacy history import) and plans no consumer's
onboarding.

**Planned later (owner decision 2026-10-01):** `$profile` and `$performance`
become engine lanes. No consumer's onboarding is a prerequisite (2026-10-08).
Until then they keep the legacy `codex-profile` coordinator as a temporary
holdover, which is not a defect.

**Deliberately deferred:** `$playtest` runs through quruntul only for a
consumer whose adapter implements `playtest()`.

## Continuing after a context reset

Run `$guide` after a meaningful batch of work. It compares merged code and
changed issues with these principles from the cursor in
[`guide/CURSOR.md`](guide/CURSOR.md). Read [the design](design.md) before
implementing. Update this document only for accepted changes in intent.
