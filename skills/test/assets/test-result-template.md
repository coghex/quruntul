---
schema: {{schema_json}}
run_id: {{run_id_json}}
snapshot_id: {{snapshot_id_json}}
test_id: {{test_id_json}}
area: {{area_json}}
base_ref: {{base_ref_json}}
base_refreshed_at: {{base_refreshed_at_json}}
ci_coverage: {{ci_status_json}}
ci_exclusion_evidence: {{ci_evidence_json}}
execution_status: {{execution_status_json}}
interpretation_status: "pending"
claimed_at: {{claimed_at_json}}
test_finished_at: {{test_finished_at_json}}
revision: {{revision_json}}
revision_subject: {{revision_subject_json}}
revision_committed_at: {{revision_committed_at_json}}
command: {{command_json}}
log_path: {{log_path_json}}
---

# Test result: {{title}}

## Result summary

<REPLACE: State what was exercised, whether the command executed successfully, and the overall interpreted outcome. Keep execution status separate from gameplay judgement.>

## Source version

- **Base ref:** {{base_ref_json}}
- **Tested commit:** {{revision_json}}
- **Commit subject:** {{revision_subject_json}}
- **Commit time:** {{revision_committed_at_json}}
- **Base refreshed:** {{base_refreshed_at_json}}

## Selection rationale

- **CI exclusion evidence:** {{ci_evidence}}
- **Priority rationale:** {{selection_rationale}}

## Execution

- **Setup:** <REPLACE: world/fixture/seed/profile and important preparation>
- **Primary path:** <REPLACE: player actions or system path exercised>
- **Mechanical result:** <REPLACE: exit code, assertions, timing, and crashes>
- **Related existing evidence:** <REPLACE: prior artifacts used without rerunning CI-covered tests, or "None">

## Observations

<REPLACE: Add one section per independently digestible observation using the exact format below. If there are none, delete the sample and write "No reportable observations.">

### OBS-001 — <REPLACE: concise observation title>

- **Category:** <REPLACE: gameplay | balance | UX | regression | performance | harness-gap | noise>
- **Severity:** <REPLACE: blocker | high | medium | low | info>
- **Confidence:** <REPLACE: high | medium | low>
- **Disposition:** <REPLACE: actionable | harness-limitation | expected | inconclusive>
- **Expected:** <REPLACE: the contract, player expectation, or explicit "Exploratory; no fixed expectation">
- **Observed:** <REPLACE: only the observation represented by this section>
- **Evidence:** <REPLACE: log excerpts, metrics, snapshots, state deltas, and artifact paths>
- **Interpretation:** <REPLACE: explain what the evidence does and does not establish>
- **Recommended follow-up:** <REPLACE: one focused next action, or "None">

## Coverage and limitations

- <REPLACE: state what this run covered>
- <REPLACE: state important paths it did not cover>
- <REPLACE: identify timing, randomness, state contamination, or oracle limitations>

## Artifacts

- **Primary log:** {{log_path_json}}
- **Additional artifacts:** <REPLACE: trace, screenshots, dumps, temporary harness, or "None">
