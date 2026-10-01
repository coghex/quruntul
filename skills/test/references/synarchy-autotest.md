---
name: autotest
description: Run the installed $test skill serially for a requested number of iterations, or continuously when no count is supplied. Use only when the user invokes $autotest or explicitly asks for this repeated coordinated-test workflow; do not use for ordinary test commands.
---

# Autotest

Orchestrate repeated, independent `$test` invocations. `$test` remains the sole authority for selecting, coordinating, executing, interpreting, reporting, and cleaning up each test; do not copy or weaken its workflow here.

Run this in a **Class B** session (see the `model-classes` skill). Its workers inherit this session's class, so they run on Class B too.

Keep this as a foreground loop in the current conversation. Do not create a daemon, scheduled task, background shell loop, recurring goal, or persistent service.

## Parse the invocation

- `$autotest N` runs `N` iterations, where `N` is a positive decimal integer.
- `$autotest` with no argument is unbounded and continues until a stopping condition below occurs.
- Reject zero, negative, non-integer, or extra arguments with a concise usage correction. Do not reinterpret them as a `$test` area hint.

Start immediately after a valid invocation; do not ask for confirmation.

## Load the delegated workflow

Before the first iteration, locate the installed `test` skill and read its complete current `SKILL.md`. Stop if it is unavailable. Its rules and the repository's current agent instructions apply in full to every iteration. Repetition grants no additional permission to change implementation, publish anything, bypass approvals, run CI-covered tests, or widen one `$test` invocation beyond one primary test or one proposal.

Resolve the current Git repository once for the loop. Each `$test` iteration still performs its own required refresh and uses its own one-use snapshot; never reuse a snapshot, run worktree, claim, or report from an earlier iteration.

## Run serially

Maintain `completed / target` progress for bounded runs and a monotonically increasing iteration number for unbounded runs.

When collaboration workers are available, use one fresh worker for each iteration so long-running test evidence does not accumulate in the orchestrator's context. This skill explicitly authorizes that delegation. Keep exactly one test worker active at a time; never parallelize iterations. Give the worker the repository path and this bounded task:

```text
Use $test in <absolute-repository-path> and complete exactly one invocation of the installed test skill. Read and follow that skill and the repository instructions in full. Do not begin a second test. Return its standard handoff, including whether it attached a validated result report, reached a missing-test proposal, or was blocked. Apply its open-issue blocker rule and return skipped test IDs, issue links, and whether an independent test may proceed.
```

Workers inherit this session's class (Class B): do not override the worker model or reasoning effort. If delegation is unavailable, perform the same single `$test` invocation locally before beginning another.

An iteration counts as completed when `$test` attaches its validated standard `*.test-result.md` report, regardless of whether its interpretation is `clean`, `observations`, `inconclusive`, or `blocked`. A missing-test proposal is an approval boundary, not a completed test iteration. A blocker reached before a report is attached is also not a completed iteration.

After every completed iteration, give a concise progress update naming the test ID, interpreted outcome, observation count, and report path. Continue immediately after `clean`, `observations`, or `inconclusive`; findings remain local reports and do not authorize fixes or issue creation. Keep the user updated at least once per minute while a worker is still running.

Also continue after a blocked invocation when `$test` verifies that its blocker is already tracked by an open issue, confined to the affected candidates, and leaves an independent eligible test available. Carry the skipped test IDs, issue links, and reasons into each fresh worker's task so it can recheck them against its refreshed source and avoid repeating the same blocker. Skips before claim do not count toward the target; an attached validated blocked report still counts under the normal rule above. Keep interpretation `blocked` and report the skip plainly; do not relabel it as a pass or retry it to fill the count.

## Stop and pause correctly

Stop starting new iterations when any of these occurs:

- A bounded run reaches its requested count.
- The user says to stop, pause, or cancel. Normally let the active `$test` iteration reach its safe report/cleanup boundary, but interrupt immediately if the user explicitly says to stop now.
- `$test` produces a missing-test proposal and requires explicit approval. Present the complete proposal and pause without counting it or starting another iteration.
- An iteration is blocked and does not qualify for `$test`'s open-issue skip rule above. Report whether that terminal invocation counted and do not retry the same systemic condition in a tight loop. Stop when no unaffected eligible candidate remains; do not cycle through known blocked candidates.
- Continuing would cross an authorization, safety, cost, or environment boundary that `$test` itself does not permit.

For an unbounded invocation, completing one test is not a terminal condition. Continue serially until one of the conditions above occurs; do not send a final response merely because an iteration finished.

At termination, summarize the requested mode, completed iteration count, outcomes and observation total, report paths, skipped candidates with issue links, and the exact stopping reason. Do not invoke `$assess-tests` automatically.
