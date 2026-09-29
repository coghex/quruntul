---
name: playtest
description: Run one coordinated, bounded screenshot-driven naive-player session in the shared local test lifecycle, interpret its trace and frames, and write codex-test-result/v1 observations for later $assess-tests correlation. Use only when the user invokes `$playtest` or explicitly asks for this isolated agent-controlled playthrough workflow; do not trigger for ordinary game testing, probes, or requests merely to launch the harness.
---

# Coordinated Playtest

Run exactly one real naive-player session against a refreshed immutable source
snapshot, interpret what the player actually saw and attempted, and publish one
local standard test-result report. Keep the first experience bounded: this is a
single playthrough question, never a campaign.

## Shared lifecycle

Before acting, read the sibling sibling `test` skill's `references/synarchy.md` (Synarchy's `$test` workflow)
completely. Its repository resolution, refresh, registry, snapshot, claim,
detached-worktree, execution, report, cleanup, and local-only rules are the
authoritative shared lifecycle. Follow its sections 1 and 3–7. This skill
replaces only `$test`'s broad candidate-selection step with the playtest-specific
selection below.

Use the sibling coordinator for every lifecycle operation:

```bash
python3 <playtest-skill-dir>/../test/scripts/test_coordinator.py <command> ...
```

Never create a playtest registry, report schema, worktree manager, or copied
coordinator. `$test` and `$playtest` intentionally share the common Git
directory's `codex-test/` state, atomic claims, artifacts, and
`codex-test-result/v1` reports. Use stable `playtest:<semantic-question>` test
IDs so either entry point blocks a duplicate owned by the other.

If the sibling `$test` skill or coordinator is unavailable, stop as blocked;
do not improvise a parallel lifecycle.

## Select one bounded player question

Treat text after `$playtest` as an area or player goal, not necessarily a test
name. Inspect active and recent shared runs, open proposals, and `value-status`
as required by the shared lifecycle. Separately audit recent `playtest:*` runs:
probe and scenario volume does not establish naive-player coverage, while a
new seed, port, or cosmetic wording does not make a repeated playtest question
distinct.

With a hint, choose one concrete player-visible question in that area. Without
a hint, prefer an onboarding or early-game question not recently exercised. On
the first run, use this conservative default unless current code makes it
inapplicable:

- persona: `curious_carl`;
- goal: `Start a new game, create a world, and figure out what to do next.`;
- player: `codex-luna` (`gpt-5.6-luna`, medium effort);
- 12 turns;
- 600 seconds maximum session time;
- 90 seconds maximum per decision;
- 200K player tokens (provider-reported input + output);
- `dt = 2` seconds;
- offscreen rendering.

Later runs may choose another shipped or generated persona and goal when that
asks a materially different player question. Always state one explicit
persona, goal, turn bound, wall-clock bound, decision timeout, `dt`, and port.
Stay conservative unless the user explicitly asks for a longer session. If a
completed 12-turn run made no meaningful progress, a later invocation may use
16–20 turns under the user's standing approval; never extend or retry the
current invocation silently. One invocation owns one persona/goal session.

The user may select `claude-sonnet` (`claude-sonnet-5`, medium effort) instead.
Treat natural requests for “Sonnet” or “Claude” as that fixed profile. Without
a provider request, keep `codex-luna`. Never accept an arbitrary model or effort
override, mix profiles within a run, or fall back from the selected profile.

Do not substitute `--smoke`, `--replay`, `--selftest`, a screenshot check, an
offscreen boot check, a manual scenario, or a behavior probe. Those test the
apparatus or a predetermined contract, not an uninformed player's perception
and decisions. Do not invent scenario-jump setup; the current harness is
cold-boot only.

If recent playtests have saturated the area and no materially distinct
player-visible question remains, stop without running a cosmetic variant.
Explain the saturation and ask for a new goal or recommend `$test` to search
for a different non-CI question. `$playtest` v1 does not create missing-test
proposals.

## Verify the live harness before claiming

In the refreshed base snapshot, read `tools/playtest/README.md`,
`tools/playtest/agent.py`, and `tools/playtest/run.py`. Require all of the
following:

- `--player` exposes exactly `codex-luna` (`gpt-5.6-luna`, medium, Codex CLI)
  and `claude-sonnet` (`claude-sonnet-5`, medium, Claude Code);
- each decision is isolated from repository access and outside information:
  Codex disables user rules/config, web, plugins, skills, shell, and
  multi-agent tools; Claude uses safe mode, no persisted session or MCP/skills,
  and only reads the screenshot copied into its fresh empty workspace;
- the selected profile's login check succeeds (`codex login status` or
  `claude auth status`);
- live compact usage reports input + output for the turn and run, and the
  remaining player-token budget;
- the harness stops on unavailable usage and applies the projected-next-turn
  reserve to `--max-player-tokens`;
- `--render-mode offscreen` is available;
- the requested goal fits the current cold-boot action vocabulary.

If any condition fails, do not change provider/model or weaken isolation.
Report the lane blocked. Never use windowed mode unless the user explicitly
requests it after being told it steals focus. Account-plan token remaining is
not available from either noninteractive CLI; report it as unavailable rather
than estimating it from the run budget.

Classify the session `outside-ci`. Cite the refreshed repository's playtest
documentation and `tools/ci_probes.py` exclusion as evidence. Do not run a CI
test as preflight.

## Preserve and claim the session

Before claiming, allocate a unique absolute trace directory under the common
Git directory's `codex-test/artifacts/`; the disposable run worktree must never
hold the only trace. Include that path and every bound in the exact claimed
command, for example:

```bash
python3 tools/playtest/run.py \
  --render-mode offscreen \
  --port <unused-port> \
  --persona <persona> \
  --goal <goal> \
  --player <codex-luna|claude-sonnet> \
  --turns <turns> \
  --max-seconds <seconds> \
  --decision-timeout <seconds> \
  --max-player-tokens <tokens> \
  --dt <seconds> \
  --trace-dir <absolute-artifact-directory>
```

Claim this exact command with the shared coordinator before creating the run
worktree or performing expensive setup. Use the coordinator to create the
detached worktree and execute with a timeout that allows bounded engine boot
and teardown beyond the session's own wall-clock limit. Never retry, add a
second persona, or expand the turn budget inside the same invocation.

## Interpret the completed playthrough

The primary test includes post-session interpretation; it is not a second
test. Read:

- `meta.json`;
- every record in `turns.jsonl` and `replay.jsonl`;
- `inspection-plan.json`, using it as a deterministic inspection queue rather
  than a verdict;
- the coordinator command log and retained `engine.log`;
- the actual pre/post screenshots for the session bookends and every turn
  implicated by confusion, notes, repeated actions, stuck detection,
  rejected/no-op actions, missing feedback, a goal claim, or an engine anomaly.

Use image inspection for visual claims. Never infer appearance from hashes,
widget dumps, filenames, or the player's prose. Keep the actor/oracle boundary
intact during the session; only after it ends may interpretation correlate the
player's expectation with widgets, events, action outcomes, and engine state.

Do not automatically run `tools/playtest/critic.py` or any other paid model.
The current Codex session interprets the trace. A separately requested critic
run is outside this invocation.

Treat confusion, misclicks, failure to reach the goal, and `done` as evidence
to verify, not automatic defects. Separate product friction, expected behavior,
balance concerns, harness defects, and uncertainty. Do not modify production
code, repair the harness, file issues, or run a follow-up test.

## Report and hand off

Use the shared coordinator's standard report lifecycle. In addition to every
required `codex-test-result/v1` field, record:

- persona, goal, player backend/model/effort;
- turn, wall-clock, decision, player-token, and `dt` bounds;
- stop reason; compact tokens this run (input + output); and the local aggregate
  usage-ledger path. State that account-plan remaining is unavailable unless a
  provider actually supplied a trustworthy token count;
- furthest gameplay state reached;
- durable trace path;
- every screenshot actually inspected;
- visual and gameplay paths not exercised.

Give each independent verified point its own sequential `OBS-nnn`. A clean
report has no observation sections but still states what the player reached.
Use `inconclusive` or `blocked` honestly when appropriate.

After attaching the report, perform normal shared-worktree cleanup. Report the
tested commit, session result, interpreted outcome, observation count, report
and trace paths, and cleanup result. Recommend `$assess-tests` as the single
correlation and disposition lane for observations, but never invoke it
automatically or mark observations consumed.
