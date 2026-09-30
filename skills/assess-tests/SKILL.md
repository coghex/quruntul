---
name: assess-tests
description: "Turn quruntul observations from $test, $flake and $playtest reports into verified findings and, on approval, filed issues; and turn accepted missing-test proposals into test designs. `$assess-tests` takes every open observation; `$assess-tests <run-id | run-id/OBS-nnn | area>` narrows it; `$assess-tests approve <assessment> <sha>` files the approved issues. Use when the user invokes $assess-tests or asks to digest test observations. Never runs tests or changes code."
---

# Assess tests

Test agents are fallible operators and their observations are leads, not
verdicts. This skill verifies them against the current code, groups them by
cause, drafts one issue per real, untracked problem, and files those issues
after the owner approves the frozen assessment.

## Route by repository

Resolve the Git top level and read its `AGENTS.md` completely.

- **`.quruntul/adapter.py` exists:** use the quruntul workflow below. In
  Synarchy, older `$test`/`$playtest` reports also sit in the legacy
  `codex-test` registry: when that registry still lists unassessed observations,
  assess them with [references/synarchy.md](references/synarchy.md) in a
  separate invocation, never mixed into a quruntul assessment.
- **Synarchy:** read [references/synarchy.md](references/synarchy.md) completely
  and follow it. It uses the `codex-test` coordinators in this skill's
  `scripts/`.
- **Otherwise:** report that there is no adapter and stop.

## Parse the invocation

- `approve <assessment-id> <sha256>` → [File approved issues](#4-file-approved-issues) only.
- `release <assessment-id>` → `quruntul assess release <id> --reason "..."`
  and report that its observations are open again.
- Otherwise the text selects observations: a run id, an exact
  `run-id/OBS-nnn`, or an area; nothing selects every open observation.
  `proposal:<id>` selects one accepted proposal for the design lane.

## 1. Claim

```sh
quruntul observations --status open
quruntul assess claim --owner <owner> [--run <id>]... [--observation <id>]... [--area <text>]
```

The claim is atomic; an observation someone else holds cannot be claimed
twice. Assess the whole claimed batch together — one run's downstream symptom
may be another's cause. With no open observations, go to the proposal lane or
report that there is nothing to assess.

## 2. Verify every observation

Fetch upstream and read the current code in a fresh detached worktree. Never
use the primary checkout. For each observation:

1. Separate what ran, what the assertions said, what behaviour appeared, and
   what the test agent concluded. Its conclusion is a lead.
2. Read the logs and artifacts it cites (under the ledger's
   `runs/<run-id>/`). A missing artifact lowers confidence; it does not license
   invention.
3. Trace the behaviour at the tested revision (`git show <rev>:<path>`) and at
   the current head. Decide whether later commits fixed or invalidated it.
4. For anything actionable, read every open issue title and search open and
   closed issues two or three different ways. Read any epic that might own it.
5. Assign exactly one disposition: `confirmed-product-defect`,
   `confirmed-harness-defect`, `duplicate` (name the issue),
   `fixed-or-superseded`, `expected`, `inconclusive`, or `downstream` (name
   the finding that explains it).

Do not rerun anything here. For an inconclusive observation, recommend one
focused `$test`, `$flake <test>` or `$playtest` follow-up. A `flaky-test`
observation from `$flake` is already tracked in the ledger; its disposition
is usually `duplicate` of the ledger row, with `$deflake <test>` as the
follow-up — do not file an issue for a flaky test unless the owner asks.
An `uncertain` observation that tests failed every trial (status `failing`)
is a consistent failure: check the harness and environment first (build
products, generated files, working directory, environment, launch path), then
the product. Its tests return to `new` by themselves once the suite's inputs
change, so a harness repair needs no ledger mark.

## 3. Draft the assessment and stop

Write one Markdown assessment in a scratch file:

- A header naming the assessment id, the current revision and the source runs.
- One `## FND-nnn — title` section per independent finding: its source
  observations (every claimed observation appears in exactly one finding),
  disposition, evidence, current-code trace, confidence, and follow-up.
- For each `confirmed-*` finding that no existing issue owns, the **exact issue
  draft** as it would be filed — title, existing labels only, and a body in the
  repository's issue convention (for Hetoimasia: `## Background` with the
  verification evidence, numbered observable `## Requirements`, exact
  `## Acceptance` commands, `## Out of scope`, `## Related`, ending with
  `<!-- issue-origin:codex -->`).

Freeze it:

```sh
quruntul assess propose <assessment-id> --file <assessment.md>
```

Present the complete assessment, its id and its sha256, then stop. Approval is
`$assess-tests approve <assessment-id> <sha256>`; changes mean editing the file
and proposing again, which produces a new hash.

## 4. File approved issues

Only for an approval naming the current hash — in a fresh session, show the
stored assessment (`quruntul assess show <id>`) and confirm first.

```sh
quruntul assess approve <assessment-id> --sha <sha256>
```

Then, for each drafted issue in order, recheck that no issue filed since covers
it, write the approved body to a file, create it exactly as approved, and
record it before the next one:

```sh
gh issue create -R <owner/repo> --title "<title>" --label <label>... --body-file <file>
quruntul assess issue <assessment-id> --finding FND-nnn --url <issue-url>
```

If creation fails, stop and report what was filed and what was not; recorded
findings are never filed twice. Report the assessment path, each finding's
disposition and every issue URL. Recommend `$deflake` for flaky tests and
`$test` or `$flake` follow-ups; invoke none.

## Proposal lane: accepted proposals become test designs

`quruntul proposals --status accepted` lists approved missing-test proposals.
For each selected one, write one design document in the repository's
standalone-docs location (for Hetoimasia, the `docs-wip` worktree resolved by
branch, `docs/test_designs/<id>.md`). Verify the gap, apparatus and oracle
against the current code; preserve the proposal's fields and decision note
verbatim; state the tier and why it stays out of CI; mark it
`Implementation authorization: not granted`. Then:

```sh
quruntul proposal-close <proposal-id> --status designed --note "<design path>"
```

A proposal current code already satisfies is closed `implemented` or
`superseded` with the evidence, and gets no design. Never land the document,
implement the test, or promote it to CI here.
