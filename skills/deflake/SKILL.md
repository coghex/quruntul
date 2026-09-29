---
name: deflake
description: "Fix one flaky test from the quruntul ledger: claim it, reproduce the failure, find the cause, fix it in an isolated worktree, prove the fix with before/after flake batches, and open a pull request carrying that evidence. `$deflake` picks the worst unclaimed flaky test; `$deflake <test>` targets one; `$deflake N` fixes N tests in sequence. Use when the user invokes $deflake or asks to fix a flaky test. Never merges."
---

# Deflake

Turn one `flaky` test into a merged-ready fix. One iteration owns one test, from
claim to PR. The ledger moves it to `fixing`; the next `$flake` after the PR
merges verifies it on the merged revision and marks it `stable` or `flaky`.

## Route by repository

Resolve the Git top level and read its `AGENTS.md` (or `CLAUDE.md`) completely.
Its build, test, platform, desktop and **delivery** rules govern everything
below.

- **`.quruntul/adapter.py` exists:** use the quruntul workflow below.
- **Synarchy:** read [references/synarchy.md](references/synarchy.md) and follow
  that diagnosis workflow; Synarchy's census has no fix-and-PR lane yet.
- **Otherwise:** report that the repository has no quruntul adapter and stop.

## Parse the argument

```sh
quruntul resolve-arg deflake <everything after $deflake>
```

`iterations` is how many tests to fix, one after another. A `target` of kind
`test` is the one test to work on; `N <test>` means up to N attempts at that
test, stopping at the first PR. A suite target or a bare hint is not a test:
list that suite's flaky tests (`quruntul tests --suite <id> --status flaky`)
and ask which one.

## One iteration

### 1. Claim

Make an owner token once per iteration, e.g. `deflake-<short-uuid>`, and claim:

```sh
quruntul deflake select [<test>] --owner <owner>
```

It returns the test, its suite, its failing trials with log paths, and its
history, or `no-candidate`. The claim lasts 30 minutes. Run
`quruntul heartbeat --owner <owner>` at least every 10 minutes while you work,
including during long builds. If you stop before a PR, release the test with
`quruntul deflake release <test> --owner <owner> --reason "..."`. Do that on
every exit path: reproduction failed, cause not found, interrupted.

A named test that is `fixing` already has an open PR, so report the PR and stop.
A `stable` or `new` test is accepted only when named explicitly; say it has no
recorded lab failure.

### 2. Reproduce on the recorded evidence

Read the failing trials' logs and seeds. Then measure a **baseline** at the
upstream head:

```sh
quruntul flake --target <test> --trials <N> --owner <owner>
```

Choose N so the failure shows up with good probability. If the observed rate is
p, N ≈ 3/p reproduces it about 95% of the time, bounded by the suite's batch
budget. If the baseline shows no failure, you have not reproduced it yet. Try
the recorded seed through a narrow local experiment, look for load or order
sensitivity, or raise N once. Do not claim a cause you have not reproduced.
If it will not reproduce, release the claim with the evidence and stop. The
test stays `flaky`.

### 3. Diagnose

Work in a fresh isolated worktree from the upstream head, following the
repository's worktree rules. Never work in the primary checkout. Read the test
and the code under test. Form hypotheses and test the smallest discriminating
one first. Usually the flaw is a race in the test (a missing coordination point,
a real-time sleep, an assumed schedule) or a real concurrency or ordering bug
in the code. Tell those apart before fixing anything. Do not:

- weaken or delete an assertion, widen a timeout, add a sleep, or add a retry;
- mark the test pending, or skip it;
- change what the test claims to prove.

If the right fix changes the test's contract, or needs an owner decision,
release the claim with the diagnosis and stop.

### 4. Fix and prove it

Make the smallest fix, with any docs the fix changes, in that worktree. Commit
it (unpushed is fine), then measure the **candidate** with the same N:

```sh
quruntul flake --target <test> --ref <candidate-commit> --trials <N> --owner <owner>
```

Candidate batches are evidence only; they never change the ledger's status. The
fix is proven when the baseline failed and the candidate, with the same N,
passed every trial. Where the repository makes it possible, also show the fix
under the failing trial's recorded seed. A candidate that still fails means
you haven't found the cause; go back to step 3. Also run the owning suite once
in full, as the repository's normal check.

### 5. Open the PR

Follow the repository's delivery rules exactly. For Hetoimasia, that means
AGENTS.md's worktree, validation-planner and pull-request request-block rules,
with code and docs in one PR. Also:

- Title the PR with what the fix changes, not "fix flaky test".
- In the body, give the test id, the cause, the fix, both batch run ids with
  their counts (`baseline: F failed of N`, `candidate: 0 failed of N`), the
  seeds, and the ledger paths. Add the repository's required PR attribution.
- Push the branch and open the PR. Never merge it, approve it, or label it.

Then record the handoff, which releases the claim:

```sh
quruntul deflake pr <test> --owner <owner> --pr <PR-URL-or-number>
```

## Repeating: `$deflake N`

Run iterations **serially**, each on a different test and each in a fresh
subagent when you can delegate. Each worker gets:

> In `<repo>`, complete exactly one `$deflake` iteration (`<test if targeted>`)
> as the installed skill describes, from claim to PR or release. Return the
> test, the outcome (PR URL, or why it was released), and the baseline and
> candidate counts.

After each iteration, post one line with the test, the outcome, and the PR.
Stop early on `no-candidate`, on a blocker that is not specific to one test,
or when the user says stop.

## Report

Give the test, the cause, the fix, the baseline and candidate counts, the PR
URL, and the ledger status (`fixing`). For a released test, say why and what
evidence would let a later attempt succeed.
