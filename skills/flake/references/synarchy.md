---
name: flake
description: "Operate Synarchy's census-backed de-flake lab: run the next eligible probe, report census status and retained evidence, or defer and resume probes without losing their history. Use when the user invokes $flake, asks to run or inspect the flake lab, wants data about flaky probes, or asks to keep an intentionally blocked probe out of lab selection. Do not use for ordinary test execution or playtesting."
---

# Operate the Flake Lab

Work only in the Synarchy repository. Read its current `AGENTS.md`/`CLAUDE.md`
instructions before acting, and keep the primary checkout clean. The repository
tools are authoritative; do not implement a second selector in this skill.

## Choose the operation

- With no narrower request, reconcile/migrate the census with
  `python3 tools/probe_census.py --seed`, then run one canonical census
  measurement with `python3 tools/deflake.py`. It chooses at most one eligible
  probe, uses the fixed measurement contract, records the result, and retains
  diagnostic artifacts. Do not add a probe override or call a skipped probe
  directly.
- If `tools/deflake.py` returns `no-qualifying-probe`, replenish the lab by
  migrating exactly one manual-only legacy probe to `probe-result/v1`; do not
  keep rerunning an exhausted selector. Use `tools/ci_probes.py --status`,
  `tools/run_probes.py`, and `tools/probe_flake.py` as the authorities. Exclude
  CI-eligible, deferred, and already-protocol probes. Prefer a GPU-free,
  deterministic or targeted probe with modest runtime and assertions that map
  cleanly to stable check identifiers; avoid a known-flaky, needs-GPU,
  scenario-heavy, or worldgen-heavy probe while a smaller suitable candidate
  exists.

  Make the migration in an isolated worktree through the repository's normal
  pull-request lane, one probe per replenishment. Preserve its standalone CLI
  and behavior while adding the no-engine `--describe` path, ordered stable
  checks, the shared reporter, harness-owned engine logs and RTS arguments, and
  protocol registration. Update the protocol-facing documentation and focused
  harness tests. Validate both descriptor purity and a real harnessed run. Do
  not record an unmerged probe measurement in the census or bypass selection;
  after the migration merges, the next lab run seeds the census and lets
  `tools/deflake.py` select it normally.
- For status or data, run `python3 tools/probe_census.py --summary` (add `--json`
  for analysis). Explain measured versus unmeasured, failure counts, staleness,
  tolerance, and deferral separately. A probe failure is evidence to investigate,
  not proof that either the game or the probe is defective.
- To defer a probe, first ensure the census is current with
  `python3 tools/probe_census.py --seed`, then run:

  ```text
  python3 tools/probe_census.py --defer --probe KEY \
    --reason "WHY THE RESULT IS NOT ACTIONABLE NOW" \
    --resume-when "OBJECTIVE CONDITION THAT MAKES IT ACTIONABLE"
  ```

  Require both statements to be concrete and non-blank. Deferral is for a known
  external/content prerequisite, not a way to hide an unexplained failure or
  weaken its acceptable-failure policy.
- To resume a probe, verify the recorded condition against the repository or a
  direct user statement, then run
  `python3 tools/probe_census.py --resume --probe KEY`. Never resume merely
  because time passed.
- To investigate a result, start from the result/handoff and retained artifact
  paths reported by `tools/deflake.py`. Compare the probe's assumptions with the
  current game state and content before proposing a repair. Preserve artifacts
  and census history.

## Enforce deferrals

Treat `census.deferred` as the availability authority. A deferred probe must be
reported with skip reason `deferred` and must never be claimed, run, or measured
by the lab. Do not bypass selection with `tools/probe_flake.py`, edit the JSON by
hand, delete measurements, or change CI classification to simulate a deferral.

The stored reason explains why it is paused. The stored `resume_when` is the
acceptance condition for reconsidering it. Keep both visible in status reports.

## Preserve workflow boundaries

All census mutations go through `tools/probe_census.py`, which resolves the
`docs-wip` worktree by branch and performs a locked atomic update. Do not edit
`docs/probe_census.json` directly. A local census update is not publication;
publish it only when the user explicitly asks, using the repository's documented
docs-landing workflow.

Run only the focused self-tests relevant to code changed in this system. Census
and selector work uses `tools/test_probe_census.py`,
`tools/test_probe_select.py`, and `tools/test_deflake.py`. A protocol migration
uses the probe's focused tests when present, `tools/test_probe_flake.py`,
`tools/test_deflake.py`, its no-engine `--describe` path, and one real harnessed
run. Do not run `make ci` unless the user explicitly requests the full local CI
gate.
