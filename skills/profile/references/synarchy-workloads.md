# Synarchy profiling adapter

Read the target snapshot's `CLAUDE.md` first. This adapter preserves the
profiling-specific rules that materially change measurements; repository
instructions remain authoritative when they evolve.

## Build profiles answer different questions

Production builds use `-O2 -optc-O3`. The persistent worktree already isolates
its build tree from the ordinary checkout, so use its normal ignored production
directory:

```bash
cabal build exe:synarchy --builddir=dist-newstyle
```

The executable has `-rtsopts`, with baked-in `-N -A128M`. Production timing,
allocation, GC, and residency measurements must use this production build and
record every explicit RTS override. `+RTS -s -RTS` is suitable for an RTS
summary; it does not by itself establish retained-object causes or GPU memory.

Cost-centre profiling uses the checked-in profile flag and its own build tree:

```bash
cabal build exe:synarchy --enable-profiling -f profile --builddir=dist-prof
```

That retains production optimization and adds `-fprof-late`. Every run/test in
that build profile must repeat `--enable-profiling -f profile
--builddir=dist-prof` when driven through Cabal.

Never use `-f dev` for general performance work. It changes optimization and,
on macOS, can add sanitizer/debug behavior.

## Cost-centre world generation has two hard constraints

The GHC 9.12.2 profiled RTS used by this repository can segfault when worldgen's
parallel sparks push and pop cost-centre stacks. Override the executable's
baked-in capabilities with `+RTS -N1 -p -RTS`. This makes absolute wall time
unrepresentative; only attribution within that instrumented run is meaningful.

Do not drive a profiled world through `--dump`. Its fixed watchdog can kill the
world thread and truncate a profile. Use `--headless` with an unused port,
issue `world.init(...)`, wait through `world.waitForInit(<caller-controlled
seconds>)`, and call `engine.quit()` only after initialization succeeds. The
complete established recipe and prior interpretation live in
`docs/history/worldgen_timeline_profile_2026-07.md`.

Direct profiler output into `CODEX_PROFILE_ARTIFACT_DIR`. In particular use a
distinct `-po<absolute-output>` for every cost-centre capture so no
`synarchy.prof` file dirties the persistent worktree and concurrent historical
artifacts cannot be overwritten.

## Workload selection

Prefer a repository-owned workload boundary that already proves completion:

- headless world initialization for worldgen phases;
- an exact registered probe only when its setup/actions match the performance
  question;
- a bounded debug-console scenario for simulation or Lua work;
- offscreen rendering for unattended renderer measurements; or
- a real window only with explicit user authorization because it can steal
  focus and requires a graphics-capable desktop.

When using a registered probe, invoke exactly one key through
`tools/run_probes.py --only <key> --exact --jobs 1`; do not invoke its probe
script directly. A functional probe result and a performance measurement are
different outputs: preserve both, and define the interval actually measured.

Never run the complete headless suite, a full probe sweep, `world_check`, or
`make ci` merely to obtain a profile. Those are correctness gates, not
performance workloads.

## Measurement discipline

- Use fixed seeds, world size, plates/ages, arena, scenario, and simulation
  duration. Record them in the workload fingerprint.
- Separate process startup, engine boot, world setup, warm-up, and the measured
  interval when the question concerns only one phase.
- A worldgen cost-centre capture under `-N1` may rank CPU/allocation centres; it
  cannot be compared directly with production `-N` wall time.
- RTS allocated bytes are cumulative traffic, maximum residency is a sampled
  live-heap measure, process RSS includes more than the managed heap, and GPU
  residency is separate again. Name the one actually measured.
- Redirect heap profiles, eventlogs, `.prof` files, timing samples, engine logs,
  and driver scripts to the run artifact directory. The lab worktree must be
  clean enough to reattach to `profile-lab` after reporting.
- Record the executable digest in `artifact_fingerprint`, not in
  `build_method_fingerprint`. Comparable optimization runs necessarily have
  different revisions/digests while retaining the same production or profiled
  build method.
- A nonzero exit, failed readiness boundary, truncated profiler artifact, or
  missing clean shutdown remains evidence and normally yields `inconclusive`
  or `blocked`; never replace it silently with a retry.

Eventlog, heap-census, and GPU tools require their own verified build/runtime
recipes. Confirm the installed toolchain's supported options and the current
repository component before claiming them. If the necessary instrumentation or
stable workload is absent, record the apparatus gap instead of guessing flags.
