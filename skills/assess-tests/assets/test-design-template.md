# <REPLACE: test title> test design

Test design state: `<REPLACE: exploring | ready for implementation>`

Source proposal: `<REPLACE: exact proposal ID>`

Source test ID: `<REPLACE: exact test ID>`

Source ref and commit: `<REPLACE: proposal source ref and exact commit>`

Designed against: `<REPLACE: current base ref and exact commit>`

Accepted at: `<REPLACE: proposal decision time>`

Implementation authorization: `not granted`

Recommended tier: `<REPLACE: ci | manual-only | exploratory>`

## Purpose and coverage gap

<REPLACE: State the behavior question, why current apparatus cannot answer it,
and why the test is worth maintaining.>

## Binding decisions

- <REPLACE: Preserve the accepted decision note verbatim.>
- <REPLACE: Record fixed scope, exclusions, and selection policy.>

## Current repository evidence

<REPLACE: Verify the proposal against the current design commit. Name relevant
apparatus, registries, CI classifiers, production paths, and any drift since
the source proposal.>

## Scenario and boundaries

<REPLACE: Define setup, player or API actions, retained evidence, graded path,
and explicitly excluded paths.>

## Oracle

<REPLACE: Define exact observable pass, failure, setup-failure, and inconclusive
conditions. Keep command success separate from behavior success.>

## Apparatus and integration

<REPLACE: Name the implementation shape and likely files, registration key,
shared helpers, ownership boundaries, and cleanup requirements.>

## Reliability and cost

<REPLACE: Record expected runtime, environment needs, deterministic fixture
strategy, polling versus sleeps, flakiness risks, and maintenance risks.>

## Implementation plan

1. <REPLACE: dependency-ordered implementation step>
2. <REPLACE: registration and classification step>
3. <REPLACE: focused validation step>

## Validation and handoff

<REPLACE: State the focused checks that prove the apparatus works, how the
coordinated test skill should discover it, and what must remain outside CI or
required gates when that is a binding decision.>

## Open questions

<REPLACE: List material unresolved questions, or `None`. If any question could
change scope, keep the design state `exploring`.>
