---
schema: {{schema_json}}
run_id: {{run_id_json}}
profile_id: {{profile_id_json}}
area: {{area_json}}
workload_id: {{workload_id_json}}
metric_family: {{metric_family_json}}
instrument: {{instrument_json}}
measurement_status: "<REPLACE: complete | inconclusive | blocked>"
snapshot_id: {{snapshot_id_json}}
base_ref: {{base_ref_json}}
base_revision: {{base_revision_json}}
target_ref: {{target_ref_json}}
revision: {{revision_json}}
revision_subject: {{revision_subject_json}}
revision_committed_at: {{revision_committed_at_json}}
base_refreshed_at: {{base_refreshed_at_json}}
claimed_at: {{claimed_at_json}}
comparison_key: {{comparison_key_json}}
machine_fingerprint: "<REPLACE: stable hardware and OS identity>"
toolchain_fingerprint: "<REPLACE: compiler, build tool, profiler versions>"
build_method_fingerprint: "<REPLACE: optimization, instrumentation, build flags and runtime support; exclude revision and executable digest>"
artifact_fingerprint: "<REPLACE: measured revision and executable or artifact digest>"
workload_fingerprint: "<REPLACE: scenario, inputs, completion boundary, runtime options>"
execution_status: {{execution_status_json}}
artifact_dir: {{artifact_dir_json}}
---

# Profile result — {{title}}

## Performance question

{{performance_question}}

## Selection rationale

{{selection_rationale}}

## Source version

- Base ref and revision: `{{base_ref_json}}` at `{{base_revision_json}}`
- Target ref and measured revision: `{{target_ref_json}}` at `{{revision_json}}`
- Commit: `{{revision_subject_json}}` (`{{revision_committed_at_json}}`)
- Snapshot refreshed: `{{base_refreshed_at_json}}`
- Source checkout reported dirty: `{{source_dirty}}`; uncommitted content was not measured.

## Environment and comparability

- **Machine:** <REPLACE: hardware model, architecture, core count, and memory>
- **Operating system:** <REPLACE: exact version>
- **Toolchain:** <REPLACE: compiler, Cabal/build tool, profiler and renderer tools>
- **Power and load conditions:** <REPLACE: power source, thermal/load controls, competing work>
- **Build configuration:** <REPLACE: optimization, instrumentation, build directory, relevant environment>
- **Runtime configuration:** <REPLACE: RTS flags, capabilities, arena/port/render mode and relevant environment>
- **Comparison boundary:** <REPLACE: what another run must match; name any known incomparable history>

## Method

- **Workload:** <REPLACE: exact setup, inputs, actions, and completion boundary>
- **Warm-up:** <REPLACE: warm-up policy and why>
- **Sampling:** <REPLACE: sample count, order, summary statistic, variation and outlier policy>
- **Primary instrument:** <REPLACE: tool, mode, sampling/census interval and what it observes>
- **Known distortion:** <REPLACE: how instrumentation changes execution, memory, scheduling or rendering>

### Commands

{{commands}}

## Measurements

<REPLACE: give every raw sample with units, then the stated summary and observed variation. Link raw artifacts.>

## Measurement interpretation

<REPLACE: state only what this experiment establishes. Distinguish direct measurements, attribution, and hypotheses. Do not prescribe a code change.>

Preparation blocker recorded by the coordinator: {{block_reason}}

## Coverage and limitations

<REPLACE: name unmeasured paths, environmental uncertainty, profiler blind spots, and conclusions this run cannot support.>

## Artifacts

- Artifact directory: `{{artifact_dir_json}}`
- <REPLACE: list raw profiles, heap/eventlog files, sample tables, driver scripts, processed output, and command logs with absolute paths>
