---
schema: {{schema_json}}
assessment_id: {{assessment_id_json}}
assessment_outcome: "<REPLACE: recommendations | follow-up-measurement | no-action>"
base_ref: {{base_ref_json}}
assessment_revision: {{revision_json}}
assessment_revision_subject: {{revision_subject_json}}
assessment_revision_committed_at: {{revision_committed_at_json}}
claimed_at: {{claimed_at_json}}
source_run_count: {{source_run_count}}
source_run_ids: {{source_run_ids_json}}
---

# Performance assessment

## Source profile reports

{{source_reports}}

## Source disposition

{{source_dispositions}}

## Evidence quality and comparability

<REPLACE: group only genuinely comparable reports; identify diagnostic-only, distorted, stale, blocked, or incomplete evidence and explain why.>

## Current-code verification

<REPLACE: describe the relevant behavior at each measured revision and the assessment revision, including later fixes or drift.>

## Decision now

<REPLACE: Begin with exactly one plain action class: Act now, Measure only, or Stop. State what the user should do now and what they should not change. Explain the important measurements in operational terms, including whether allocation is cumulative traffic, residency is live heap, and memory-in-use is distinct from OS RSS.>

## Findings and recommendations

### PFN-001 — <REPLACE: concise evidence-backed title; remove this whole example when there is no finding>

- **Source profiles:** <REPLACE: run IDs and exact measurements>
- **Confidence:** <REPLACE: high | medium | low, with reason>
- **Established evidence:** <REPLACE: characteristic, comparison, or hypothesis>
- **Current mechanism:** <REPLACE: current code surface and why it produces the measured behavior>
- **Action now:** <REPLACE: supported production change, one exact future profile, or no action; never present a hypothesis as an implementation action>
- **Implementation boundary:** <REPLACE: smallest supported code or configuration change, or explicitly state that none is justified yet>
- **Expected effect:** <REPLACE: direction only unless a controlled comparison measured magnitude>
- **Risks and invariants:** <REPLACE: semantics, determinism, memory, concurrency, rendering, maintenance>
- **Validation or follow-up profile:** <REPLACE: stable proposed profile_id and comparison_key plus exact refs, area, workload, metric family, instrument, controls, and decision rule; state none only for Stop>

## Follow-up profiling

<REPLACE: For Measure only, list the subsequent $profile experiments in priority order with stable proposed profile_id and comparison_key. For Act now, list validation profiles. For Stop, state why none is warranted. Do not run them.>

## Boundaries

This assessment does not authorize implementation, profiling execution, issue
creation, pull requests, publication, or changes to production code.
