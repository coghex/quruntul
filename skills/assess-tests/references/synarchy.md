---
name: assess-tests
description: Assess, correlate, verify, and deduplicate observations from local coordinated test reports, and turn accepted missing-test proposals into durable test-design documents. Use when the user invokes `$assess-tests`, asks to digest test observations, supplies test-result selectors, or approves/revises a pending assessment. Do not use to execute tests, implement proposed tests, modify production code, create tracker issues, or publish documentation.
---

# Assess Tests

Run two independent intake lanes on a fresh invocation:

1. assess unconsumed `OBS-nnn` sections from completed `$test` and `$playtest`
   reports; and
2. turn accepted, undesigned `$test` missing-test proposals into one durable test
   design document per proposal.

The observation lane remains one conservative, current-code assessment with an
explicit approval gate. The proposal lane is planning, not assessment: never
turn an accepted proposal into a finding, and never count it as an observation.
An empty lane does not block useful work in the other lane.

Use `scripts/assessment_coordinator.py` for every observation claim, assessment
proposal, report, and assessment worktree transition. Use
`scripts/proposal_design_coordinator.py` for every accepted-proposal claim and
design completion. Never hand-edit either registry.

All coordination state remains local under the repository's common Git
directory at `codex-test/`. Never add, commit, push, or publish coordinator
state, assessment reports, or logs.

## 1. Resolve and inspect both queues

Resolve the repository root and initialize shared state:

```bash
python3 <skill-dir>/scripts/assessment_coordinator.py init --repo <repo>
python3 <skill-dir>/scripts/assessment_coordinator.py list --repo <repo>
python3 <skill-dir>/scripts/assessment_coordinator.py list --repo <repo> --active
python3 <skill-dir>/scripts/proposal_design_coordinator.py init --repo <repo>
python3 <skill-dir>/scripts/proposal_design_coordinator.py list --repo <repo>
python3 <skill-dir>/scripts/proposal_design_coordinator.py list --repo <repo> --active
```

Interpret fresh-invocation arguments as follows:

- no argument: every currently unassessed, unclaimed observation and every
  accepted, unclaimed, undesigned test proposal;
- a test run ID: every unassessed observation in that run;
- `<run-id>/OBS-nnn`: that exact observation;
- a `*.test-result.md` path: every unassessed observation in that report;
- `proposal:<proposal-id>`: that exact accepted proposal; and
- otherwise: an area hint matched independently against observation area, test
  ID, and title, and proposal area, test ID, and title.

Combine all observation selectors into one assessment. Never split its
observations among agents: a downstream symptom in one run may be explained by
the primary failure in another. Each accepted test proposal remains an
independent design and gets its own document.

An invocation that names `approve <assessment-id>`, revises a known assessment,
or cancels one operates only on that existing assessment. Do not opportunistically
claim new observations or proposals during a follow-up approval turn.

Fetch the current upstream default without changing any checkout. Use the
repository's upstream ref when it is not `origin/master`:

```bash
git -C <repo> fetch --prune origin
```

Claim only nonempty selected lanes. Translate observation selectors to
repeatable `--run`, `--observation`, `--report`, or `--area` flags:

```bash
python3 <skill-dir>/scripts/assessment_coordinator.py claim \
  --repo <repo> --base-ref origin/master [observation selectors]
```

Translate proposal selectors to repeatable `--proposal` or `--area` flags:

```bash
python3 <skill-dir>/scripts/proposal_design_coordinator.py claim \
  --repo <repo> --base-ref origin/master [proposal selectors]
```

Both claims are atomic. If either exits 3, report every structured owner and do
not duplicate that source. If one queue is empty, continue with the other. Stop
for no-work only when neither queue contains selected available input. A claim
that exits 2 with `no matching ... available` after listing a terminal source
means that source was already consumed; it is not an active-owner conflict or a
reason to reopen it.

For an observation claim, save the returned `assessment_id` and create its
detached current-code worktree:

```bash
python3 <skill-dir>/scripts/assessment_coordinator.py worktree \
  --repo <repo> --assessment-id <id>
```

Read current code there. Read source reports/logs from the claim paths; inspect
old code with `git show <tested-revision>:<path>` when provenance or later drift
matters. Never modify implementation or run an expensive probe.

## 2. Assess observations

Assume the source test and playtest agents are deliberately the user's most
rudimentary available models. Treat their reports as raw observations from
fallible operators, not as reviews: before retaining any finding,
independently verify its observed behavior, expected contract, root cause,
severity, confidence, disposition, and proposed follow-up against primary
evidence and current authorities. Agreement between multiple such agents may
raise investigation priority, but it is not verification.

For every source observation:

1. Separate command execution, assertion outcomes, observed behavior, and the
   test agent's interpretation. Treat its disposition as a lead, not a verdict.
2. Check the complete primary log and named artifacts needed to support or
   weaken the claim. Missing transient artifacts lower confidence; they do not
   license invention.
3. Trace the relevant behavior at the tested commit and at the assessment's
   current commit. Determine whether later commits fixed, invalidated, or made
   the observation stale.
4. For a potentially actionable finding, read every open issue title, run two
   or three differently phrased all-state searches, and read relevant open epic
   bodies. Do not create or comment on an issue.
5. Correlate the whole batch. Merge observations only when evidence establishes
   one root cause. Mark dependent fallout `downstream`; do not manufacture a
   separate action for each failed assertion.
6. Do not rerun a surprising or inconclusive scenario here. Recommend one
   focused future `$test` or `$playtest` invocation according to whether the
   missing evidence is a predetermined contract or naive-player perception.

Assign exactly one disposition to every source observation:

- `confirmed-product-defect`: current evidence establishes incorrect product behavior;
- `confirmed-harness-defect`: current evidence establishes a faulty or weakened
  test, test-infrastructure path, or reproducible test-environment setup;
- `duplicate`: an existing tracker item already owns the same work;
- `fixed-or-superseded`: current code or a merged change removes the premise;
- `expected`: evidence matches the intended contract;
- `inconclusive`: the evidence cannot distinguish the competing explanations;
- `downstream`: another finding in this assessment explains this observation.

Do not label a gameplay defect confirmed from a harness failure alone. Keep
product and harness findings separate unless one change genuinely must resolve
both.

Observation eligibility is content-driven, not report-status-driven. Assess
every claimed `OBS-nnn` section even when its source report is `blocked` or
`inconclusive`. A blocked report can establish an important harness or
infrastructure defect. Verify that a blocker is current, reproducible, durable,
and not already tracked before recommending durable follow-up.

## 3. Turn accepted proposals into test designs

Process every proposal returned by the proposal-design claim. Proposal
acceptance authorizes this planning document; it does not authorize
implementation, tracker mutation, CI changes, or documentation publication.

### Resolve the docs worktree

Never leave an uncommitted design in the primary checkout. Resolve the docs
worktree by branch, never by a hard-coded path:

```bash
DOCS_WT="$(git worktree list --porcelain \
  | awk '/^worktree /{p=substr($0,10)} /^branch refs\/heads\/docs-wip$/{print p; exit}')"
[ -n "$DOCS_WT" ] || DOCS_WT="$(git rev-parse --show-toplevel)"
```

When the worktree contains `docs/`, use
`$DOCS_WT/docs/test_designs/<test-id-slug>.md`; otherwise use
`$DOCS_WT/test_designs/<test-id-slug>.md`. Resolve collisions by meaning, not by
adding timestamps. Search existing design files for the exact source proposal
ID before creating anything; resume that document when a prior interrupted run
already created it.

Read `assets/test-design-template.md` completely before drafting. Create and
edit the document with `apply_patch`.

### Verify and design

Treat the accepted proposal as approved intent, not current-code evidence:

1. Verify its coverage gap, relevant apparatus, registration surface, CI
   classification, scenario, and oracle against the design claim's current
   `base_ref` and `revision`. Use the claimed revision rather than uncommitted
   primary-checkout state.
2. Preserve the source proposal ID, test ID, source provenance, accepted time,
   recommended tier, and the complete `decision_note`. The decision note is a
   binding constraint; reproduce it verbatim in `## Binding decisions`.
3. Preserve the distinction between command success, setup success, behavioral
   success, failure, and inconclusive evidence. Define retained artifacts and
   cleanup behavior.
4. Specify the production path, fixture boundaries, registration key, likely
   files, reuse opportunities, determinism strategy, cost, flakiness controls,
   and maintenance risks closely enough for a later implementation request.
5. For a manual-only or exploratory proposal, state exactly why it stays out of
   CI and every required gate, and how `$test` should discover it. Never promote
   a proposal to CI because a design document now exists.
6. Do not silently broaden the scenario, strengthen the oracle beyond available
   evidence, or convert a one-test proposal into an implementation epic.

Use `Test design state: ready for implementation` only when current evidence
supports the accepted scope and no material implementation decision remains.
Use `exploring` when repository drift or an unresolved question could change
the scenario, oracle, ownership, tier, or cost. In both states retain:

```markdown
Implementation authorization: `not granted`
```

If current code proves that the accepted proposal is already implemented or
superseded, do not manufacture a design document. Record the terminal result:

```bash
python3 <skill-dir>/scripts/proposal_design_coordinator.py resolve \
  --repo <repo> --design-id <id> \
  --outcome <already-implemented|superseded> --note <current-evidence>
```

Otherwise validate and record the completed document:

```bash
python3 <skill-dir>/scripts/proposal_design_coordinator.py complete \
  --repo <repo> --design-id <id> --document <absolute-document-path>
```

Fix validation errors rather than bypassing them. If the run is cancelled or a
blocking ambiguity prevents a faithful design, release the claim with
`release --reason`; preserve any partial doc in the docs worktree and report its
path. Never land or push the design document unless the user separately invokes
the repository's documentation-publication workflow.

## 4. Draft the observation assessment and stop for approval

Skip this section when no observations were claimed.

Create the local proposal skeleton only after investigation:

```bash
python3 <skill-dir>/scripts/assessment_coordinator.py proposal-init \
  --repo <repo> --assessment-id <id>
```

Edit the returned `*.proposal.md` with `apply_patch`. Replace every
`<REPLACE: ...>` marker. Use sequential `FND-nnn` headings. Every source
observation must appear in exactly one finding's `Source observations` field;
one finding may own several observations when they share a verified cause.

Validate and freeze the proposed content:

```bash
python3 <skill-dir>/scripts/assessment_coordinator.py propose \
  --repo <repo> --assessment-id <id>
```

Present the complete proposal exactly as stored, plus its assessment ID and
SHA-256. Also report every test-design document created or proposal resolved in
this invocation. Then stop. Do not promote the assessment, mark observations
assessed, file issues, create a versioned findings report, implement a test, or
publish a test design. Ask the user to reply with
`$assess-tests approve <id>` or request changes.

If the user requests changes, edit the same assessment proposal, run `propose`
again, and present the complete new content and hash. If the user cancels, run
`release` and then `cleanup`; say that the observations are available again.

When an invocation claimed proposals but no observations, report the completed
design paths, design states, tested current commit, and any terminal resolutions.
No assessment approval is required because the source proposal was already
approved for planning.

## 5. Promote only an approved observation assessment

An approval is valid only when the conversation establishes that the user saw
the current proposal hash. In a fresh session or after any uncertainty, show
the stored proposal and ask again rather than assuming approval.

After explicit approval, retrieve the record and promote exactly its frozen
hash:

```bash
python3 <skill-dir>/scripts/assessment_coordinator.py show \
  --repo <repo> --assessment-id <id>
python3 <skill-dir>/scripts/assessment_coordinator.py approve \
  --repo <repo> --assessment-id <id> --proposal-sha <approved-sha>
python3 <skill-dir>/scripts/assessment_coordinator.py cleanup \
  --repo <repo> --assessment-id <id>
```

Only `approve` creates the final `codex-test-assessment/v1` report and changes
the source observations from claimed to assessed. Report the final path,
assessment commit, source observation count, finding count, disposition
summary, and cleanup result.

Recommend but never invoke the next lane:

- one confirmed independent product, harness, or test-infrastructure defect
  needing durable repair: `$issue`;
- several confirmed independent concerns needing a durable queue:
  `$draft-report`, then `$process-report`;
- inconclusive evidence: a focused later `$test <area>` or `$playtest <goal>`;
- duplicate, expected, fixed, or downstream-only results: no new tracker item;
- a completed test design: a separate explicit implementation request when the
  user wants the proposed apparatus built.

Never copy the local assessment into a versioned document without a separate
user request and that workflow's own approval boundary.
