---
name: test
description: "Run one local-only probe through the quruntul lab: never a CI test, often long, chosen because it has never run, its code changed, or its last observation is stale. Interpret the evidence and write a quruntul report whose observations `$assess-tests` can turn into issues. `$test` picks the next probe; `$test N` runs N in sequence (this replaces $autotest); `$test <probe>` targets one; `$test <area>` narrows the choice. Falls back to `$playtest` when no probe is due. Use only when the user invokes $test or asks for this coordinated probe workflow."
---

# Test

Run one probe: a local-only test that CI never runs, chosen because it is due.
Observe what happens, and leave a report another agent can act on. Probes are
the lab's advanced, often long experiments, and some only run this way. `$test`
never runs a CI test; flakiness in CI tests is `$flake`'s job.

## Route by repository

Resolve the Git top level and read its `AGENTS.md` (or `CLAUDE.md`) completely.

- **`.quruntul/adapter.py` exists:** use the quruntul workflow below.
- **Synarchy:** read [references/synarchy.md](references/synarchy.md) completely
  and follow it for each iteration. Its coordinator is in this skill's
  `scripts/`. For `$test N` in Synarchy, run that workflow once per iteration
  under the loop rules below; they replace the old `$autotest`, whose text is
  kept in [references/synarchy-autotest.md](references/synarchy-autotest.md).
- **Otherwise:** report that the repository has no quruntul adapter and stop.

`quruntul` must be on `PATH`; if it isn't, report that and stop.

## Parse the argument

```sh
quruntul resolve-arg test <everything after $test>
```

It returns `iterations`, plus either a `target` or a `hint`:

- A `suite` target is a probe to run.
- A `test` target: run its suite.
- A `hint` narrows selection by area, id or description.

An `ambiguous` result lists the candidates; pick the one the text plainly means,
or ask.

## One iteration

1. Activate the repository's qualified toolchain.
2. Run exactly one probe: `quruntul test`, adding `--target <suite>` for a
   target or `--hint "<hint>"` for a hint. Probes that open windows need
   `--desktop`, and only after the owner has approved that specific session.
   Say what will happen and wait for approval. The owner's standing desktop
   approval covers pull-request work, not periodic testing.
3. Handle its outcome:
   - **`no-candidate`:** every probe is fresh, deferred or unavailable here.
     Go to [When nothing is due](#when-nothing-is-due).
   - **`busy`:** another agent owns that probe. Run again once, without a
     target.
   - **`complete`, `blocked`, `interrupted`, or `budget-exhausted`:** the
     execution is recorded. Interpret it.
4. **Interpret.** Read the complete log (`log` in the JSON), and any artifacts
   it names. Keep four questions apart:
   - Did the command run?
   - Did its assertions pass?
   - What behaviour did it show?
   - What does that establish? A product defect, a harness defect, an
     environment limit, expected behaviour, or only uncertainty?

   A long observational probe that exited 0 has not necessarily "passed".
   Timing variation is not a defect without evidence. Don't rerun the probe
   in this iteration. If the result needs a second run to resolve, say so and
   name the follow-up.
5. **Report.** Complete the `report.md` at the path the command returned:
   - Replace every `<REPLACE: ...>` marker.
   - Set `interpretation_status` to `clean`, `observations`, `inconclusive`
     or `blocked`.
   - Give each independent observation its own `### OBS-nnn — title` section
     with every field. Don't merge separate issues, and don't split one cause
     into several.
   - Classify a blocker as a harness, infrastructure or environment
     observation. A `blocked` report needs at least one observation.
   - State limitations even when the run was clean.

   Then attach it; validation errors must be fixed, never bypassed:

   ```sh
   quruntul report attach <run-id>
   ```

## When nothing is due

1. If the result says `playtest: true`, the adapter offers playtests. Follow the
   installed `$playtest` skill for this iteration instead.
2. Otherwise, audit coverage. Read the `skipped` reasons and the pending
   proposals. Deferred, busy or wrong-platform probes are not coverage gaps.
   If existing probes are exhausted **and** you can verify a distinct gap that
   CI, the Hspec suites and existing probes all miss, record one proposal:

   ```sh
   quruntul propose <file.json>
   ```

   The JSON needs exactly these fields: `target_id` (a stable future suite id),
   `lane` (`test`), `question`, `gap` with evidence, `scenario`, `oracle`,
   `cost`, `tier` (`probe`), and `revision`. A proposal that already exists
   returns `already-proposed`. Present the whole proposal and stop for approval.
   Don't implement it. The owner can later record a decision with
   `quruntul proposal-close <id> --status accepted|rejected --note ...`.
3. If there is no real gap, say that every probe is fresh, and when the
   earliest one becomes due.

## Repeating: `$test N`

Run N iterations **serially**; don't run two probes at once. Give each
iteration to a fresh subagent when you can delegate, so a long probe's evidence
doesn't fill this conversation. Each worker gets:

> In `<repo>`, complete exactly one `$test` iteration (`<target/hint args>`) as
> the installed skill describes, including the attached report. Return the
> probe, run id, interpretation status, observation count, report path, and
> whether it hit a proposal or a blocker.

An iteration counts when its report is attached, whatever its status. After
each one, post a line with the probe, the status, the observation count and the
report path. Keep the user updated while a long probe runs. Stop when:

- N iterations have completed;
- a proposal needs approval (present it and pause);
- a blocker isn't confined to one probe (toolchain, fetch, disk, shared
  environment);
- nothing is due and there's no playtest fallback;
- or the user says stop.

At the end, summarize the probes, the statuses, the observation total and the
report paths. Recommend `$assess-tests` for the observations, but never invoke
it yourself.

## Boundaries

Never change production code, file issues, or publish anything here. Findings
are local reports until `$assess-tests` assesses them. Respect deferrals: a
deferred probe resumes only with `quruntul resume <id> --evidence ...` after
its condition is verified, never because time has passed.
