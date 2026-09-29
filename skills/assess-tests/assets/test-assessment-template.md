---
schema: "codex-test-assessment/v1"
assessment_id: {{assessment_id_json}}
base_ref: {{base_ref_json}}
assessed_commit: {{revision_json}}
assessed_commit_subject: {{revision_subject_json}}
assessed_commit_time: {{revision_committed_at_json}}
base_resolved_at: {{base_resolved_at_json}}
source_observations: {{source_observations_json}}
---

# Test observation assessment: {{assessment_id}}

## Assessment summary

<REPLACE: Summarize what was assessed, the important causal relationship across observations, and the resulting action queue. Do not imply that an observation was reproduced when it was only traced.>

## Source observations

{{source_observation_bullets}}

## Findings

### FND-001 — <REPLACE: concise assessed finding title>

- **Disposition:** <REPLACE: confirmed-product-defect | confirmed-harness-defect | duplicate | fixed-or-superseded | expected | inconclusive | downstream>
- **Severity:** <REPLACE: blocker | high | medium | low | info>
- **Confidence:** <REPLACE: high | medium | low>
- **Source observations:** <REPLACE: comma-separated exact `<run-id>/OBS-nnn` keys; assign every source key to exactly one finding>
- **Current-version evidence:** <REPLACE: evidence at the assessed commit, including relevant paths and how later changes affect the premise>
- **Tracker deduplication:** <REPLACE: matching issue/epic and relationship, or searches performed and "no match">
- **Interpretation:** <REPLACE: what the evidence establishes, what it does not, and whether other observations are consequences of this cause>
- **Recommended next lane:** <REPLACE: $issue | $draft-report | $test | $playtest | existing issue | no action>
- **Proposed action:** <REPLACE: observable outcome for the next lane, or "None">

## Cross-observation analysis

<REPLACE: Explain shared causes, ordering, contradictions, and downstream effects. Write "No cross-observation dependency established" when appropriate.>

## Coverage and limitations

- <REPLACE: evidence and versions actually inspected>
- <REPLACE: missing artifacts, reproduction limits, timing/randomness, or untested alternatives>
- <REPLACE: state explicitly that no implementation, test execution, or tracker mutation occurred>

## Next-step queue

1. <REPLACE: ordered recommendation naming FND-nnn and the exact next skill/lane, or "No follow-up action">
