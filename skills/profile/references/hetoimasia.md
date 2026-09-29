# Profiling Hetoimasia

Read the repository's `AGENTS.md`, `docs/toolchain.md` and `docs/vision.md`
(V-7 and V-10) before selecting a question. Hetoimasia has no gameplay and no
established benchmark apparatus yet, so the first experiments in an area should
establish a trustworthy workload and baseline rather than chase a hotspot.

## Build and environment rules

- Activate the qualified toolchain (GHC 9.14.1, Cabal 3.18.1.0) first on `PATH`;
  the repository's instructions say how. Never change the machine's default GHC.
- CPU-only packages (foundation, runtime, the GPU model, diagnostics, the Lua
  host) build with `--project-file cabal.project.cpu`. The Vulkan backend and
  window integration build only through `bash tools/vulkan/run.sh`, against the
  provisioned native prefix.
- Record `+RTS -s` output for throughput, allocation, GC and residency from an
  ordinary optimised build. Cost-centre profiling needs profiling libraries for
  every dependency; if they are not installed, report that as a blocker rather
  than switching builds mid-experiment. Never compare absolute times between a
  profiled and an ordinary build.
- Anything that opens a window — the triangle sample, `glfw-native-tests`,
  `vulkan-native-tests` — disrupts the owner's desktop. Periodic profiling is not
  covered by the owner's standing approval for pull-request work: describe the
  disruption and wait for approval for that session. On Linux the isolated X11
  display (`bash tools/display/x11.sh -- <command>`) needs no approval.

## Questions the vision already asks

- **Foreign-call boundaries (V-7, Vulkan D-28).** Measure the audited `unsafe`
  recording subset against safe imports on the same recording workload before
  claiming a speedup; a binding-wide flag is not assumed to be an optimisation.
- **Asynchronous logging (V-7, runtime RR-5/RR-6).** Producer latency and
  throughput with the bounded asynchronous adapter versus synchronous logging,
  including saturation and truncation behaviour.
- **Bounded messaging (V-5).** Channel and latest-value snapshot throughput and
  allocation under a fixed producer/consumer workload.
- **GPU model transitions.** Allocation and time per frame-lifecycle transition in
  `hetoimasia-gpu-vulkan-model`, a pure workload that needs no device.
- **Lua host.** Per-call overhead of trusted in-process bindings under the VM
  owner, and the cost of the re-entry refusal check.
- **Frame pacing (needs the desktop or isolated X11).** Present-request and
  present-fence cadence of the triangle sample; the VK-16 verdict's numbers
  (`docs/graphics_owner_interaction_verdict.md`) are the existing baseline for
  modal-loop pacing.

A workload that does not exist yet ends the experiment as `blocked` with the
missing apparatus named, so `$performance` can recommend building it. Do not
improvise an unverifiable benchmark.
