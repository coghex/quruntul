# Authorized performance delivery

Read this reference only after the user explicitly asks `$performance` to
implement or run the delivery pipeline for one completed assessment whose
outcome is `recommendations`, or for one named accepted `PFN-nnn`. The
assessment supplies evidence and scope; it does not itself grant authority.

## 1. Freeze one delivery contract

Select exactly one finding. Record its assessment ID, finding ID, source run
IDs, mechanism, smallest implementation boundary, semantic invariants, ordered
validation profiles, comparison keys, controls, and decision rules.

For every validation experiment, identify a compatible before report on the
current base. It must preserve the same logical comparison key, build method,
workload, instrument semantics, and relevant code. If no such retained report
exists—or relevant code/build/workload inputs changed—delegate a base-revision
`$profile` subagent first. A diagnostic hotspot alone is not before/after
evidence.

Verify the premise against the current upstream base and current code before
editing. If the hotspot moved, the mechanism changed, or later measurements
superseded the finding, return to assessment mode. Do not reinterpret stale
evidence into a nearby optimization.

Delivery authority is limited to:

- one isolated implementation branch and worktree;
- the minimal accepted code/test change;
- relevant repository semantic gates;
- one local candidate commit;
- the finding's validation profiles, delegated sequentially;
- a push of that exact branch; and
- one standalone PR when every acceptance gate passes.

It does not authorize an issue, issue linkage, merge, unrelated cleanup,
documentation publication, or full CI unless the user separately requests it.

Claim the delivery atomically before creating or adopting its worktree:

```bash
python3 <profile-skill-dir>/scripts/profile_coordinator.py delivery-claim \
  --repo <repo> --assessment-id <assessment-id> --finding-id <PFN-nnn> \
  --branch <branch> --worktree-path <absolute-path> --base-ref origin/master
```

Resume an existing active claim instead of duplicating it. A terminal delivery
for the same assessment/finding blocks a second PR.

## 2. Use the implementation lane

Fetch the upstream base and create a purpose-built branch such as
`codex/perf-<finding-slug>` in a new isolated worktree based on the current
upstream revision. Never put production changes in the persistent
`profile-lab` worktree or a repository's documentation worktree. Preserve the
primary checkout and all unrelated user changes.

An existing purpose-built implementation worktree may be adopted only when it
was created for this exact finding, its base and diff are known, and it has no
unrelated changes. State that adoption rather than pretending a new worktree
was created.

Create or adopt the worktree through the coordinator's `delivery-worktree`
command. The coordinator records its branch, path, base, and adoption state;
it does not own or publish the branch.

Read the worktree's repository instructions and the contracts for the affected
area. Implement only the assessment's smallest supported boundary. Do not
combine a queue rewrite, runtime-default change, generalized refactor, or
second hotspot merely because the worktree is already open.

## 3. Prove semantics before measuring

Run the cheapest repository gates that cover the change. Add focused
before/after or frozen-reference tests when the optimization is intended to be
observationally identical. For deterministic generated output, require the
existing baselines to pass unchanged; never rebaseline drift to make a
performance patch pass. Run `git diff --check` and review the complete diff.

Reject or repair the candidate before profiling when any semantic gate fails.
Do not use a performance improvement to trade away an invariant the assessment
said to preserve.

Commit the accepted candidate locally and record both its base revision and
candidate revision. `$profile` measures commits, not dirty worktrees. From
this point the candidate commit is immutable: amending, rebasing, merging the
base, or adding a fix creates a new candidate that must repeat semantic and
performance validation.

Record the clean commit with `delivery-update --status candidate-ready
--candidate-revision <commit>`.

## 4. Delegate the validation profiles

Every validation experiment must run in a fresh subagent that explicitly
invokes the sibling `$profile` skill. Run subagents sequentially because the
profiling lab has one serialized persistent worktree; never start two profile
runs in parallel.

Advance the delivery to validation before spawning the first candidate run:

```bash
python3 <profile-skill-dir>/scripts/profile_coordinator.py delivery-update \
  --repo <repo> --delivery-id <delivery-id> --status validating
```

Give each subagent only the concrete experiment contract:

- repository and exact candidate commit or local branch ref;
- originating assessment ID and `PFN-nnn`;
- stable `profile_id` and `comparison_key`;
- area, fixed workload, metric family, and primary instrument;
- required build/runtime controls and completion boundary; and
- the finding's acceptance or rejection rule.

Require the subagent to use `$profile --target-ref <candidate-commit>` through
the coordinator, retain a validated report and raw artifacts, restore the lab,
and return the run ID and report path. The subagent must not edit code, create
an issue, push the implementation branch, or open a PR.

Record each completed candidate run before evaluating or starting the next:

```bash
python3 <profile-skill-dir>/scripts/profile_coordinator.py delivery-update \
  --repo <repo> --delivery-id <delivery-id> --status validating \
  --validation-run <run-id>
```

After each subagent completes, inspect its report and coordinator state. Check
normal workload completion, report completeness, matching comparison key,
machine/toolchain/build-method/workload fingerprints, measurement semantics,
and the finding's decision rule. Revision and executable digest identify the
two artifacts and are expected to differ. A shared key without matching
controls is not a comparable result. Do not substitute instrumented wall time
for production timing or allocation for residency.

Run the ordered validation list exactly as specified. Conditional later
profiles run only after their prerequisite decision rule passes. If a required
run is blocked, inconclusive, incomparable, regresses the intended metric,
misses its threshold, or introduces a replacement hotspot, stop without a PR
and preserve the implementation branch/worktree for diagnosis.

## 5. Process the new evidence

Claim every new validation report through `profile_coordinator.py` in one
correlated assessment based at the candidate commit. Previously analyzed
source reports cannot be claimed again; cite their completed assessment as
prior accepted evidence and claim only the new reports. Use the ordinary
assessment template and coordinator completion/cleanup gates.

The ordinary assessment outcome still answers what optimization or measurement
should happen next; it is not the PR gate. After completing it, record the
separate delivery decision with `delivery-update --status validated
--validation-assessment-id <id> --validation-decision accepted` only when the
original finding's exact rules passed. Record `stopped` with a reason when they
did not. A separately supported future opportunity does not belong in the
current branch and does not retroactively change the measured candidate.

## 6. Check freshness and open one standalone PR

Fetch the upstream base again before pushing. If the candidate commit changed,
or upstream changed relevant implementation, build inputs, workload, or
profiling apparatus, update the candidate and repeat every semantic and
performance gate. An unrelated upstream change may leave the measured commit
valid; record that judgement and let PR CI test integration.

When freshness requires a new candidate, commit it cleanly and transition back
with `delivery-update --status candidate-ready --candidate-revision <new>`.
This clears the old validation runs/decision; then re-enter `validating` and
repeat the full ordered profile list. Never attach old measurements to the new
commit.

Only when the user authorized PR creation and every gate above passes:

1. push the exact implementation branch without force;
2. open a standalone PR against the upstream default branch;
3. do not create, search for, or attach an issue; and
4. record its URL with `delivery-update --status pr-open --pr-url <url>`; and
5. stop after PR creation—never merge it.

The PR body is the standalone contract. Include the measured problem, minimal
change, semantic invariants, exact candidate revision, focused tests and
unchanged-baseline result, before/after profile run IDs and comparable metrics,
limitations, and the literal statement `No linked issue — standalone
performance change.` Do not publish local reports or raw artifacts; summarize
their relevant measurements.

Report the branch, worktree, commit, profile run IDs, validation assessment,
PR URL, and any CI/review state already visible. If the workflow stops before
PR creation, state the first failed gate and the exact retained recovery state.

## 7. Offer the canonical review handoff

After a standalone performance PR is open and recorded, end the delivery by
asking the user whether to run:

```text
$kanban:pr-review --allow-no-issue <pr-number>
```

This review is a separate external action and is not implied by delivery
authorization. Do not invoke it unless the user explicitly agrees. When the
user grants that permission in the same request, proceed without asking a
second time. Follow the installed `$kanban:pr-review` skill completely; the
`--allow-no-issue` exception permits this intentionally unlinked standalone
PR, while any linked issue remains subject to the canonical approval gate.
