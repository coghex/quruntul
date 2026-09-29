---
name: test
description: Refresh a local detached test base from upstream, then run one coordinated local-only, non-CI exploratory or regression test—including a screenshot-driven naive-player session—in an immutable per-run Git worktree, or detect diminishing returns and propose one missing test for approval. Atomically avoid tests and proposals already owned by parallel Codex sessions; write unversioned results with exact commit provenance. Use only when the user invokes `$test` or asks for this coordinated isolated testing workflow, with an optional area such as `$test combat`, `$test expedition`, or `$test UI`; do not trigger for ordinary requests merely to run an already-specified test command.
---

# Coordinated Test

Run at most one primary test that CI does not already cover, interpret its evidence, and leave a local standard report that another agent can digest. A bounded screenshot-only naive-player session plus post-session trace/frame interpretation is one primary test, not two. When repeated runs have reached diminishing returns and no valuable existing candidate remains, propose exactly one missing test instead. Do not implement a proposed test, change production implementation, or file issues.

Keep the entire workflow local to this machine. Never add, commit, push, publish, or open a PR for a registry, report, log, test harness, or test-worktree change. Never create a remote branch. A retained ad hoc harness remains uncommitted in its detached local worktree.

Read-only issue-tracker lookups are allowed to recognize already-filed blockers; they do not authorize issue creation, comments, or other tracker changes.

Use `scripts/test_coordinator.py` for every registry, worktree, command, and report lifecycle operation. Never hand-edit `registry.json`.

## 1. Resolve the request and repository

Treat the text after `$test` as an optional area hint, not necessarily a test name. Resolve the current Git top level without changing it.

Initialize and inspect shared state:

```bash
python3 <skill-dir>/scripts/test_coordinator.py init --repo <repo>
python3 <skill-dir>/scripts/test_coordinator.py list --repo <repo> --active
python3 <skill-dir>/scripts/test_coordinator.py list --repo <repo> --limit 30
python3 <skill-dir>/scripts/test_coordinator.py proposal-list --repo <repo> --active
```

Refresh the coordinator-owned detached test base before inspecting or selecting a test:

```bash
python3 <skill-dir>/scripts/test_coordinator.py refresh \
  --repo <repo> --base-ref origin/master
```

Use the repository's real upstream default when it is not `origin/master`; omitting `--base-ref` resolves the current upstream/default branch. Save the returned `snapshot_id`, `revision`, and `base_worktree_path`. Every invocation must refresh and must use its own one-use snapshot. Never reuse a prior snapshot.

The refresh is serialized across sessions. It fetches the source remote, requires movement from the prior test-base commit to be fast-forward, and moves only the coordinator-owned detached base. It never advances the primary checkout, a local branch, or an existing test worktree. If fetch fails, the base is dirty, or the source moved non-fast-forward, stop as blocked instead of testing stale or ambiguous code.

Read the refreshed base worktree's `AGENTS.md`, project testing instructions, workflows, and test apparatus before selecting anything. Never build, test, or edit in the base worktree; it is a read-only selection anchor.

State, snapshots, logs, and `*.test-result.md` reports live under the common Git directory's local `codex-test/` folder. They are intentionally untracked and unavailable to other clones or machines. All linked worktrees on this machine coordinate through atomic locks and one registry. The base and per-run worktrees are sibling detached worktrees in a hidden directory outside the checkout; Git worktrees must not be physically nested.

## 2. Select one primary test

First inspect the recent value signal, scoped by the optional area when one was supplied:

```bash
python3 <skill-dir>/scripts/test_coordinator.py value-status \
  --repo <repo> [--area <area-hint>]
```

This is a conservative heuristic, not a coverage verdict. `diminishing-returns` means at least six recent interpreted runs exist and no more than 25% produced observations in the eight-run window. `apparatus-friction` means blocked or inconclusive runs dominate. A clean run still provides confidence; only treat the sequence as saturated when the remaining candidate audit below also finds no materially distinct, high-value question. Treat old clean history as stale when relevant code or data changed after its tested revisions.

Build a CI-exclusion set from the repository's current workflows and test classification tools. A test is CI-covered even when CI runs it only for selected paths or on one event type. Never execute a CI-covered test through this skill; the user already receives that signal repeatedly.

In Synarchy:

1. Run `python3 tools/ci_probes.py --status` read-only and exclude every `CI-eligible` probe.
2. From the refreshed base worktree, run `python3 tools/probe_census.py --summary --json` read-only. For every registered probe, treat a non-null `deferred` field as the availability authority: exclude that probe before ranking and retain its `reason` and `resume_when` in selection notes. Never infer that time, code churn, staleness, an area hint, or a prior clean result resumes it. Only the census's explicit resume operation makes it eligible again. If the census cannot be read or validated, do not select a registered probe; choose a valuable non-probe candidate or report selection as blocked.
3. Exclude the complete hspec suites, builds, `make ci`, `world_check.py`, audit/self-test gates, module guards, and every other command present in `.github/workflows/ci.yml` or its invoked scripts.
4. Prefer manual gameplay scenarios, non-deferred `manual-only` probes, offscreen/GPU checks unavailable to CI, screenshot-driven naive-player sessions, and focused ad hoc observations that answer a question CI does not ask.
5. Do not repackage a CI assertion as an ad hoc test. An ad hoc test must exercise a materially different player path, integration, balance question, visual result, or emergent behavior.

For a Synarchy probe registered in `tools/run_probes.py`, claim and execute it
through that runner, never by invoking `tools/<name>_probe.py` directly:

```bash
python3 tools/run_probes.py --only <exact-probe-key> --exact --jobs 1 [--port <unused-port>]
```

Confirm the key from the refreshed snapshot's runner inventory. `--exact` keeps
the invocation to one primary probe. The runner acquires the cross-process
`cabal-build` lock, performs its Cabal executable preflight before the probe
timer starts, and passes the resolved binary to the probe. Direct invocation is
the historical hand-run fallback: it starts `cabal run` inside
`probelib.boot`'s engine-READY window and bypasses the build lock, so a cold or
concurrent build can be misreported as a blocked engine boot. Do not switch to
direct invocation because the preflight is waiting for another Cabal user.

When `test_coordinator.py run` executes this runner command, give the outer
coordinator at least 7200 seconds. That budget covers cross-process lock wait,
a runner-uncapped cold Cabal preflight, and the runner's longest registered
key-specific default (currently 3600 seconds); it does not authorize a second
probe or a retry. An
unregistered disposable harness remains eligible under the ordinary rules
above and is not made a registered probe by this requirement.

If an area has no valuable non-CI question that can be answered with current or disposable apparatus, do not spend time rerunning CI. Use the missing-test proposal path only after the value gate qualifies; otherwise report selection as blocked. To run a CI-covered test, the user must make an ordinary explicit test request outside `$test`.

Among eligible non-CI candidates, prefer existing apparatus before inventing a harness. Inspect test documentation, manual test runners, probe classifications, manual scenarios, recent changes, and recent coordinated history.

When the value signal is `diminishing-returns` or `apparatus-friction`, explicitly audit the remaining apparatus before claiming anything:

1. Inventory untested or stale manual-only probes and player scenarios, excluding census-deferred probes, active runs, and open proposals.
2. Map recent coordinated coverage across player-critical domains, broad integration surfaces, and areas with recent churn. Track `playtest:*` screenshot-driven coverage separately: a large number of focused `probe:*` runs does not cover naive perception or prevent visual-playthrough starvation.
3. Identify whether any existing candidate asks a materially new question with a trustworthy oracle. Do not count a new seed, port, or cosmetic command variant as a new question.
4. If a high-value existing candidate remains, select it normally. If only marginal reruns remain, follow **Propose a missing test** below and do not create a run worktree.

With an area hint:

1. Map the hint to relevant code and player behavior.
2. Find the cheapest non-CI test that exercises meaningful behavior in that area.
3. Prefer an integrated or player-visible path when the request is exploratory; prefer a manual-only focused probe when verifying a precise contract.

Without an area hint, rank candidates by:

1. Player or data-loss importance.
2. Breadth of gameplay surface and integration depth.
3. Sparse, narrow, stale, or absent existing coverage.
4. Recent implementation churn not yet represented in coordinated history.
5. Runtime, flakiness, and environmental cost as negative factors.

Do not select an exact `test_id` already active. Avoid recently completed tests when a comparable high-value area has not been exercised. One invocation owns one primary test. If its evidence needs a second execution to resolve, report the uncertainty and recommend that focused follow-up instead of silently expanding this run.

In Synarchy, obey the documented testing tiers for every eligible test. Never run a full probe sweep. Distinguish observational scenario exit status from gameplay success.

### Skip blockers already tracked by an open issue

Before claiming a candidate, check relevant prior blocked reports, session context, and linked issues. If a known blocker has no issue link, search the repository's tracker for that specific condition. Verify the issue is currently open, its body covers the actual blocker, and the refreshed source still has the condition; a similar title alone is insufficient.

When a verified open issue already covers a blocker confined to that test or fixture, exclude the affected candidates and select the next valuable eligible test without asking the user. Record the skipped test IDs, issue URL, and reason in the selected run's selection rationale and handoff. A candidate skipped before claim creates no result report and does not count as a completed test. Recheck against each invocation's refreshed source; an open issue is not evidence that an already-landed fix is absent, and a closed issue is not evidence that the tested commit contains the fix.

If discovered after claim, finish that invocation through its normal blocked-report and cleanup boundary; do not execute a replacement test in the same invocation. Include the issue URL and scope evidence in the blocker observation. In the handoff, explicitly say whether another independent test may proceed, list the affected test IDs, and explain why the remaining candidates do not share the blocker. This lets `$autotest` continue with a fresh worker and snapshot.

This exception permits skipping the affected test, not executing it against a current prohibition or changing its command. It applies only when every condition preventing continuation is covered by a verified open issue and an unaffected eligible candidate remains. Shared refresh, coordinator, or environment failures that also prevent other tests still stop the workflow. If no unaffected candidate remains, use the existing selection-blocked or value-gated proposal path. Do not retry an excluded candidate in the same batch without new evidence that its blocker is resolved.

### Screenshot-driven naive-player sessions (Synarchy)

Treat one real `tools/playtest/run.py` LLM session as a first-class existing test apparatus. The naive player sees each current screenshot, narrates its understanding, and acts only through injected player input; post-session interpretation of that same trace and its frames is part of the one primary test.

Select this lane when the question concerns onboarding, discoverability, UI legibility, feedback, player decision-making, or a broad integrated gameplay flow that focused probes cannot represent. Give it extra priority when relevant UI/gameplay changed after the latest completed `playtest:*` run, or when coordinated history has many probe runs but no recent screenshot-driven session. Do not select it merely to meet a quota, and prefer a focused probe for a precise state contract with a trustworthy cheaper oracle.

Do not count `--smoke`, `--replay`, `--selftest`, a screenshot-capture check, or an offscreen-boot probe as a naive-player session. Those validate apparatus or rendering mechanics; they do not test what an uninformed player perceives and attempts. Before proposing a new test for a visual or discoverability gap, check whether one bounded persona/goal session can already answer it.

Before claiming a live session, inspect the refreshed snapshot's `tools/playtest/agent.py`, `run.py`, and README. Require `--player` to expose exactly two complete audited profiles: `codex-luna` (`gpt-5.6-luna`, medium, Codex CLI) and `claude-sonnet` (`claude-sonnet-5`, medium, Claude Code). Default to Luna unless the user requests Claude/Sonnet. Verify the selected CLI login (`codex login status` or `claude auth status`) and its isolation: Codex must disable user rules/configuration and information-acquiring tools; Claude must use safe mode, no persisted session/MCP/skills, and only Read in an empty workspace containing the screenshot. Never accept an arbitrary player model/effort or fall back to another profile. If these constraints cannot be proved, report the lane blocked or select another valuable candidate.

Use `--render-mode offscreen` by default so the run remains unattended and parallel-safe. Use windowed mode only when the user explicitly requests it after being told it steals focus. Choose a distinct unused port, one explicit persona and goal, one selected player profile, and explicit `--turns`, `--max-seconds`, `--decision-timeout`, `--max-player-tokens`, and `--dt` bounds. The conservative defaults are 12 turns, 600 seconds, 90 seconds per decision, and 200K provider-reported input-plus-output player tokens. If a completed 12-turn run made no meaningful progress, a later invocation may use 16–20 turns under the user's standing approval; never extend the current run. One invocation runs one persona/goal session; do not silently turn it into a multi-persona campaign. Use a stable semantic ID such as `playtest:naive-onboarding` based on the gameplay question, not its port or timestamp.

Preserve the session independently of the disposable worktree. Before the claim, create a unique trace directory under the repository's common Git directory at `codex-test/artifacts/` and pass its absolute path through `--trace-dir`; never leave the only trace under the run worktree or ephemeral `/tmp`. Include that exact path and all session arguments in the claimed command.

During interpretation:

1. Read `meta.json`, the complete `turns.jsonl`, `replay.jsonl`, `inspection-plan.json`, the primary command log, and the retained engine log. Use the deterministic inspection plan as a queue, never as a verdict.
2. Inspect the actual pre/post screenshots for the session bookends and every turn implicated by confusion notes, stuck loops, rejected/no-op actions, missing feedback, goal claims, or engine anomalies. Use image inspection; do not infer visual facts from hashes, filenames, widget dumps, or player prose alone.
3. Keep the player/oracle boundary intact. The player receives only its allowed screenshot/manual/persona/memory inputs. Oracle state may be correlated with frames and player expectations only after the session has finished.
4. Do not automatically invoke `tools/playtest/critic.py` or another paid model. The current Codex `$test` session interprets the trace and frames. A separately requested critic run must be explicit and independently coordinated.
5. Treat confusion, misclicks, failure to reach the goal, and a `done` claim as observations to verify, not automatic product defects. Separate player-visible friction from expected behavior, balance concerns, harness limitations, and uncertainty.

The standard report must record the persona, goal, backend/model/effort, turn/wall-clock/decision/player-token budgets, stop reason, compact run tokens (input + output), local aggregate usage-ledger path, reached gameplay state, trace path, and screenshots inspected. Account-plan remaining is unavailable from the noninteractive CLIs unless a provider explicitly returns a trustworthy token count; never infer it from the session budget. Give every independent verified friction point its own `OBS-nnn`; a clean result still states what the player actually reached and which visual paths were not exercised.

### Propose a missing test

Propose one new test only when the recent value signal indicates diminishing returns or apparatus friction and the remaining-candidate audit finds no valuable existing test. With an area hint, keep the proposal in that area. Without one, choose the highest-priority uncovered player or data-integrity risk using the same ranking above.

Verify the gap against current CI, existing hspec coverage, probes, coordinated history, and open proposals. The proposed test must ask a behavior question that existing apparatus cannot answer; do not merely make a broader suite, repeat a clean scenario, or duplicate an accepted proposal.

Choose a stable future `test_id`, then atomically record the proposal against this invocation's one-use snapshot:

```bash
python3 <skill-dir>/scripts/test_coordinator.py proposal-create \
  --repo <repo> \
  --snapshot-id <snapshot-id-from-refresh> \
  --test-id <stable-future-test-id> \
  --area <area> \
  --title <short-title> \
  --gap <specific-coverage-gap-and-evidence> \
  --rationale <importance-and-why-now> \
  --scenario <player-actions-fixture-and-boundaries> \
  --oracle <observable-pass-fail-criteria> \
  --apparatus <implementation-shape-and-likely-files> \
  --recommended-tier <ci|manual-only|exploratory> \
  --cost <runtime-flakiness-and-maintenance-risks>
```

If this exits 3, another session already coordinated that snapshot or test. Select a different uncovered gap and retry once; never duplicate the proposal.

Present the complete proposal with these fields:

- Proposal ID, source ref, and source commit.
- Proposed test ID, title, area, and recommended tier.
- Coverage gap and concrete evidence that current tests miss it.
- Why the gap matters now.
- Scenario and fixture, including the player path and excluded paths.
- Oracle: exact observations or state transitions that determine success.
- Apparatus and likely implementation files.
- Expected runtime, flakiness, maintenance cost, and major risks.

Stop for explicit approval. Do not create a worktree, implement the test, modify the repository, or turn the gap into a finding. A later user decision can be recorded locally with `proposal-close --proposal-id <id> --status <accepted|rejected|implemented|superseded> [--note <note>]`; acceptance authorizes planning only unless the user separately asks to implement it.

## 3. Claim before expensive setup

Give the test a stable semantic ID, such as `gameplay:first-aid`, `probe:movement`, or `playtest:offscreen-new-player`. Classify its reason for being outside CI as `manual-only`, `outside-ci`, or `ad-hoc`, and record the workflow or classifier evidence before execution. Claim the exact argv command atomically:

```bash
python3 <skill-dir>/scripts/test_coordinator.py claim \
  --repo <repo> \
  --snapshot-id <snapshot-id-from-refresh> \
  --test-id <stable-id> \
  --area <area-or-auto> \
  --ci-status <manual-only|outside-ci|ad-hoc> \
  --ci-evidence <workflow-or-classifier-evidence> \
  --rationale <short-selection-rationale> \
  -- <command> <arg> ...
```

If this exits 3, another session owns that exact test. Select a different candidate; never wait for or duplicate it. Save the returned `run_id`.

Create the detached run worktree at the exact commit recorded by the one-use base snapshot:

```bash
python3 <skill-dir>/scripts/test_coordinator.py worktree --repo <repo> --run-id <run-id>
```

Perform all further repository reads, builds, generated fixtures, and test execution from the returned local worktree. A later `$test` refresh can move the shared base without changing this run's detached commit. Do not write to the primary checkout or base. Do not commit worktree changes. If investigation before execution takes a long time, refresh the claim with `heartbeat`.

## 4. Execute through the coordinator

Run the claimed command with logging and automatic heartbeats:

```bash
python3 <skill-dir>/scripts/test_coordinator.py run \
  --repo <repo> --run-id <run-id> --timeout <reasonable-seconds>
```

The command's nonzero exit is evidence, not a reason to skip interpretation. Inspect the complete log, relevant state snapshots, traces, screenshots, or dumps. Do not rerun a flaky or surprising result inside the same invocation; record the uncertainty and identify a stable follow-up test so a later coordinated run can claim it independently.

When the existing apparatus cannot answer a high-value question but a small disposable harness can answer it in this run, create that harness inside the isolated worktree. Do not modify production code. Preserve a dirty worktree rather than deleting an evidentiary harness. If answering the question requires a durable new test and the value gate qualifies, use the proposal path before claiming a run instead.

## 5. Interpret conservatively

Separate these questions:

- Did the test command execute successfully?
- Did assertions pass?
- What gameplay or UX behavior was observed?
- Does the evidence establish a product defect, a balance concern, a harness limitation, expected behavior, or only uncertainty?

Use already-completed coordinated history or existing artifacts to contextualize surprising integrated behavior. Do not call an observational runner “passed” merely because it exited 0. Do not call timing variation a defect without enough evidence.

## 6. Write one standard report

Create the skeleton:

```bash
python3 <skill-dir>/scripts/test_coordinator.py report-init --repo <repo> --run-id <run-id>
```

Edit the returned `*.test-result.md` with `apply_patch`. Replace every `<REPLACE: ...>` marker and set frontmatter `interpretation_status` to exactly one of:

- `clean`: no reportable observation; use no `OBS-nnn` sections.
- `observations`: one or more separately digestible observations.
- `inconclusive`: execution completed but cannot support a judgement.
- `blocked`: the environment or setup prevented meaningful execution; include
  at least one `OBS-nnn` section describing the blocking condition so
  `$assess-tests` can verify, deduplicate, and route it.

Keep each independent observation in its own `### OBS-nnn — ...` section with all template fields. Do not combine separate bugs, balance notes, harness gaps, or noise into one observation. Evidence must name concrete artifacts or measurements. State limitations even for a clean result.

Every blocked report must preserve the blocker as a structured observation,
even when it establishes no product defect or appears transient. Classify the
observation conservatively as a harness, infrastructure, environment, or
authorization limitation; assessment decides whether it warrants a durable
issue, a focused later test, or no action.

The report must name the workflow, classifier output, or other repository evidence that establishes the primary test is not CI-covered. Do not rerun a CI test merely to supply supporting evidence; cite an existing artifact when it helps interpretation.

Keep the generated `Source version` section intact. It records the refreshed base ref, exact tested commit, commit subject/time, and refresh time so results remain interpretable after upstream advances.

Validate and attach the report:

```bash
python3 <skill-dir>/scripts/test_coordinator.py report \
  --repo <repo> --run-id <run-id> --outcome <outcome>
```

Fix validation errors rather than bypassing them.

## 7. Clean up safely and hand off

After the report is attached, attempt normal worktree removal:

```bash
python3 <skill-dir>/scripts/test_coordinator.py cleanup --repo <repo> --run-id <run-id>
```

This deliberately does not force removal. If Git refuses because the worktree contains an ad hoc harness or other evidence, preserve it and record the path.

For a completed run, report the primary test, source ref and tested commit, execution result, interpreted outcome, observation count, report path, and whether the worktree was removed. Mention expensive or high-value next tests without running a second primary test. For a proposal invocation, report the complete proposal and approval boundary instead; no result report or run worktree exists.
