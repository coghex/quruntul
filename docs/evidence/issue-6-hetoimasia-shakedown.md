# Issue #6 live acceptance: a shakedown of Hetoimasia at its upstream head

Recorded 2026-09-30 on the owner's macOS (Darwin, arm64) machine, for
[quruntul#6](https://github.com/coghex/quruntul/issues/6). The engine ran from
this branch without being installed. Hetoimasia's adapter, source, tests and
suites were not changed. Everything below was read back from the ledger, the
run's own files and Git.

## What ran

- **Engine:** quruntul commit `952ea295917f712a085a3c11e7c798cd32638c74`
  (0.3.0; branch `issue-6`, the shakedown commit `6719632` merged with
  `origin/master` `860c45e`). The run records it as its harness: path
  `~/work/quruntul/.worktrees/issue-6-shakedown`, `dirty: false`. Later
  commits on this branch add only this record.
- **Command** (the launcher puts its own checkout's package first on
  `sys.path`, so nothing global is used):

  ```sh
  python3 ~/work/quruntul/.worktrees/issue-6-shakedown/bin/quruntul --repo ~/work/hetoimasia shakedown
  ```

- **Upstream:** the configured `origin` (`https://github.com/coghex/hetoimasia.git`),
  `origin/master` = `46e6bad6757cc336d5d81e47c211011a713345e6` ("Merge pull
  request #353 from coghex/issue-324-split-validation-planner"). This matched
  `git ls-remote origin refs/heads/master` immediately before the run, and the
  run's own fetch resolved the same commit.
- **Contention check immediately before:** no live claims, no active runs, and
  no cabal, ghc, test or quruntul processes (only the Kanban dashboards).
- **Toolchain:** the adapter's own route. `ghc` on `PATH` was 9.12.2, so the
  adapter linked ghcup's `ghc-9.14.1` into its private shim. Cabal was
  3.18.1.0.
- **Checkout:** the engine made its standard detached lab checkout
  `~/work/.hetoimasia-quruntul/46e6bad6757c…` (`git worktree add --detach`)
  and built in it.

## Result

Run `230615c2-a250-40ae-b7bd-3cb59d7edd71`, lane `shakedown`, state
`complete`, upstream `yes`, interpretation **`clean`**, **no observations**.
Its trials ran from 08:41:15Z to 08:57:12Z, one suite at a time.

| Suite | Result | Listed | Notes |
|---|---|---:|---|
| hetoimasia-tests | clean | 11 | |
| foundation-tests | clean | 397 | |
| runtime-tests | clean | 208 | |
| glfw-tests | clean | 577 | |
| lua-host-tests | clean | 158 | |
| linux-confinement-probe | skipped | — | platform-inapplicable: runs on Linux |
| gpu-model-tests | clean | 172 | |
| diagnostics-tests | clean | 80 | |
| integration-tests | clean | 92 | |
| native-tests | clean | 325 | |
| shader-tests | clean | 14 | |
| triangle-tests | clean | 4 | |
| vulkan-native-tests | clean | 121 | desktop; 1 reported `pending` |
| workflow-tests | clean | 556 | 3 listed tests the ledger lacks (below) |
| x11-helper-tests | clean | 19 | |
| glfw-native-tests | clean | 94 | desktop; 2 reported `pending` |
| glfw-native-tests:glfw-wayland | skipped | — | platform-inapplicable: runs on Linux |
| macos-confinement-probe | clean | 24 | |
| wayland-helper-tests | clean | 11 | |
| lua-hazard-probes | clean | 1 | |

Results: 18 trials, every one `passed`. Of the 2,864 listed tests, 2,861
`passed` and 3 reported `pending`. None failed, none went unreported, and no
trial was incomplete.

- **Pending.** The three `pending` examples need a person at the machine:
  - a physical monitor attach or detach;
  - moving, resizing and using the menu bar, once in each native suite.

  They are the ledger's three existing `pending` tests. Under the issue's
  summary rule they are reported non-failure results, so the report lists them
  as not passed.
- **Desktop.** Windows opened for `vulkan-native-tests` (08:45:51–08:45:57Z)
  and `glfw-native-tests` (08:56:12–08:56:22Z), one suite at a time, under
  each suite's `desktop` claim. Both used the adapter's consent path:
  `vulkan-native-tests` launched through its wrapper
  `bash tools/vulkan/run.sh native hetoimasia-gpu-vulkan-glfw:test:vulkan-native-tests --`,
  and `glfw-native-tests` launched with the adapter's
  `HETOIMASIA_NATIVE_SESSION=desktop` on its prepared environment.
- **Reported, not recorded:** `workflow-tests` listed three tests that are
  new at `46e6bad` and absent from the ledger:
  - `Validation execution/execution provenance/runs a clean candidate that carries the complete planner module graph`
  - `Validation execution/execution provenance/refuses an edited planner helper before any of its code runs`
  - `Quruntul adapter/test_planner_helpers_come_from_the_checkout_being_measured`

  No listed-in-ledger test had vanished.

**Evidence** (under `~/work/hetoimasia/.git/quruntul/runs/230615c2-a250-40ae-b7bd-3cb59d7edd71/`):
- `report.md`: `quruntul-result/v1`, `interpretation_status: clean`; sha256
  `1d96b5b798596c534a5c2ae3cc86b3656cb99ec4303d75884a3d3c22c87aab3c`.
- `result.json`: sha256
  `fc94311f567d1172556950df8c2a33f43a76c35a98c77a55235a2a1b449e3d3c`.
- `<suite>/`: each ran suite's `build.log`, `enumerate.log` and
  `trial-0001.log`, each with its `.result.json`, plus `trial-0001.failures`.

## The ledger is unchanged

Snapshots were taken read-only (`sqlite3 -readonly`, and Python `sqlite3`
with `mode=ro`) of every row and every column of both `tests` and `suites`,
including `suites.document`, immediately before and after the run.

| Snapshot | Before | After |
|---|---|---|
| `tests` rows × columns | 2,861 × 16 | 2,861 × 16 |
| `suites` rows × columns | 20 × 7 | 20 × 7 |
| `sqlite3 "select * from tests order by id; select * from suites order by id;"` sha256 (1,288,008 bytes, 2,881 lines) | `00165d468c95a7d7fb0712c7f16f3fd6ef65d31a52efee9c3bed5f46f0827d48` | `00165d468c95a7d7fb0712c7f16f3fd6ef65d31a52efee9c3bed5f46f0827d48` |
| The issue's original query (suites without `document`) sha256 | `a4010d10e7d96945085256ed2320e980379bb6f2a66d5de3b559d5d59e71dfd7` | `a4010d10e7d96945085256ed2320e980379bb6f2a66d5de3b559d5d59e71dfd7` |
| Canonical JSON of both tables (column names and rows) sha256 | `cfc859caf8c1c56ea1bf4bd0c58f2971c731ef7b07f9aacf943ffe63ffe00206` | `cfc859caf8c1c56ea1bf4bd0c58f2971c731ef7b07f9aacf943ffe63ffe00206` |
| Test statuses | pending 3, stable 2,858 | pending 3, stable 2,858 |
| `runs` / `trials` / `observations` / `events` | 41 / 256 / 7 / 2,907 | 42 / 274 / 7 / 2,907 |

The only rows the run added are its own: one run and its 18 trials, with
their results. It added no observations and no events. Afterwards no claim was
live and no run was active.

## Hetoimasia's adapter and source are unchanged

| | Before | After |
|---|---|---|
| Primary `~/work/hetoimasia` HEAD | `46e6bad6757c…` (`master`, even with `origin/master`) | `46e6bad6757c…` |
| Primary `git status --porcelain` | empty | empty |
| `.quruntul/adapter.py` sha256 (primary) | `51057407098dd51bf9a97c52bb0ecbf0bf51b2b5a0e12aee17e425822fb2d51c` | `51057407098dd51bf9a97c52bb0ecbf0bf51b2b5a0e12aee17e425822fb2d51c` |
| `.quruntul/adapter.py` sha256 at `46e6bad` / in the lab checkout | `51057407…2d51c` (from `git show`) | `51057407…2d51c` |
| Lab checkout tracked status (`--untracked-files=no`) | — (created by the run) | empty |
