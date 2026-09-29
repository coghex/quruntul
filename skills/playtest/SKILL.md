---
name: playtest
description: "Run one bounded, agent-driven naive-player session of a game through the quruntul lab, interpret what the player saw and tried, and write a quruntul report with observations. `$playtest [N] [goal]`. `$test` falls back to it when no probe is due. Use only when the user invokes $playtest, or `$test` delegates here. Scaffolding in repositories whose adapter offers no playtest yet."
---

# Playtest

A playtest asks one player-visible question — *can a newcomer figure out what
to do next?* — by letting an uninformed agent play from screenshots only, then
interpreting the trace. It complements probes: probes check contracts, a
playtest checks perception.

## Route by repository

Resolve the Git top level and read its `AGENTS.md`.

- **Synarchy** (whether or not it has a quruntul adapter, until that adapter
  implements `playtest()`): read [references/synarchy.md](references/synarchy.md)
  completely and follow it. Its harness (`tools/playtest/`) and fixed player profiles are
  the working implementation. For `$playtest N`, run it once per iteration
  under the loop rules in the installed `$test` skill.
- **`.quruntul/adapter.py` exists:** read the adapter. If it defines no
  `playtest()` capability — true of Hetoimasia today, which has no gameplay yet
  — report that playtesting is not available in this repository and why, and
  stop. When `$test` delegated here, return that answer so it can audit coverage
  instead.
- **Otherwise:** report that there is no adapter and stop.

## The quruntul contract, for when an adapter adds playtests

This section is the target design. It is scaffolding until an adapter
implements `playtest()`; do not improvise a harness to satisfy it.

- **Capability.** `adapter.playtest(ctx)` returns the harness command, its
  supported player profiles, its render modes, and the goals the current build
  can reach. Players are fixed audited profiles, never an arbitrary model
  override. Isolation rules — screenshot-only input, no repository access, no
  outside tools — belong to the adapter's harness and must be provable before a
  run.
- **Selection.** One persona and one goal per run, a stable semantic id
  `playtest:<question>`, and bounded turns, wall-clock, per-decision time and
  player tokens. Prefer a question not recently exercised after player-visible
  code changed. A new seed or wording is not a new question.
- **Execution.** The run goes through quruntul like a probe: a claim on the
  playtest, a pinned checkout, the guardian owning the session, the trace kept
  under the run's evidence directory — never only in a disposable worktree.
  Offscreen by default; a windowed run needs the owner's approval for that
  session.
- **Interpretation.** Read the full trace and inspect the actual screenshots
  for the bookends and every turn with confusion, a stuck loop, a rejected
  action or a goal claim. Keep the player/oracle boundary: correlate engine
  state only after the session. Confusion and failure to reach the goal are
  observations to verify, not automatic defects.
- **Report.** The same `quruntul-result/v1` report as `$test`, attached with
  `quruntul report attach`, recording persona, goal, player profile, every
  bound, stop reason, tokens used, the furthest state reached, the trace path
  and every screenshot inspected.
