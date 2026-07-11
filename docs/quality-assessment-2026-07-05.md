# hjpeg Quality Assessment — 2026-07-05 Snapshot

This document records a design, architecture, implementation, verification, and
hardware-readiness assessment of `hjpeg`. The requested snapshot filename is
dated 2026-07-05; the source review and live verification summarized here were
performed against commit `29a9cfe` on 2026-07-10. Later changes may invalidate
individual findings.

## Summary

Overall rating: **6/10**

`hjpeg` is a thoughtful, unusually well-tested prototype with a credible
baseline JPEG pipeline and a disciplined proof model. It is not yet a reliable
or performant hardware accelerator. The current implementation has strong
stage boundaries, extensive protocol handling, and useful Vivado and host
collateral, but its main Scala test suite is not green, multi-block image
content is currently regressed, internal processing is highly serialized, and
physical KV260 operation has not been demonstrated.

| Area | Rating | Assessment |
| --- | ---: | --- |
| System design | 7.5/10 | Clear goals, sensible correctness-first priorities, and honest completion criteria |
| RTL architecture | 6.5/10 | Good stage boundaries and protocol handling, but heavily serialized and not throughput-oriented |
| RTL code quality | 6/10 | Generally readable and explicit, but the assessed checkout has a multi-block content regression |
| Verification design | 7/10 | Excellent breadth and traceability, weakened by a currently failing main suite |
| Host and Vivado tooling | 6/10 | Extremely thorough, but overly monolithic and partly duplicated |
| Hardware readiness | 4.5/10 | Bitstream and timing evidence exists, but physical encoding and useful throughput are unproven |

## Follow-up Status — 2026-07-10

The overall rating and findings below remain the historical assessment of
commit `29a9cfe`. The primary correctness and test-gate findings have since
been resolved by subsequent commits:

- The shared raster transforms now issue exactly one input handshake for each
  component block before waiting for its result. This fixes stale/duplicated
  transform results across horizontal MCUs in both 4:4:4 and 4:2:0 paths.
- The multi-MCU luminance regression now passes, including the decoded
  dark/bright image contrast check.
- The 4:4:4 stream encoder now prioritizes the Cb and Cr component selectors
  over the `y1`/`y2` storage aliases used only by 4:2:0. The decoded red/blue
  recognizability regression now passes.
- Latency-sensitive tests now wait for ready/valid handshakes with separately
  named hang bounds instead of assuming one header byte per cycle.
- Additional regressions cover distinct horizontal 4:2:0 MCUs, consecutive
  standalone reference blocks, 4:4:4 chroma selection, and legal transform
  pipeline overlap under output backpressure.
- Explicit 1080p30/100 MHz performance and resource targets now define cycle
  budgets and evidence levels. Ordered transform overlap, a two-term-per-cycle
  DCT, and registered exact reciprocal quantization reduce the measured 16x16
  fixture from the earlier 15,744-cycle baseline to 7,048 cycles. The remaining
  gap is still large and no updated Vivado timing/resource evidence is claimed.

Current verification after those changes:

```text
sbt test under Homebrew JDK 26:
  120 tests run, 120 passed

./mill --no-server _.test:
  138/138, SUCCESS

python3 scripts/host/hjpeg_host_test.py:
  234 tests passed

python3 scripts/vivado/check_reports_test.py:
  59 tests passed

python3 scripts/dev/check_chiselsim_env_test.py:
  10 tests passed

python3 scripts/dev/generate_design_graphs_test.py:
  11 tests passed
```

The Python suites and syntax checks are now part of GitHub Actions. The
ChiselSim environment test that previously inherited the caller's `SHELL` now
passes an explicit empty environment for the simulated Windows/MSYS case.

The remaining assessment priorities are a much smaller transform initiation
interval, stronger randomized/formal verification, BRAM-friendly buffering and
collection/processing overlap, host-tool modularization, fresh Vivado closure
for the changed RTL, and physical KV260 validation. No new Vivado or board
evidence is claimed by this follow-up.

## Assessment Basis

The assessment included:

- project goals, architecture, design decisions, handoff, and bring-up docs;
- all Chisel modules under `src/main/scala/hjpeg`;
- representative stage, core, AXI-stream, AXI-Lite, and integration tests;
- the sbt and Mill build definitions;
- the GitHub Actions workflow;
- host and Vivado helper structure and test suites; and
- the documented Vivado timing and utilization results.

Live checks performed on 2026-07-10:

```text
sbt test under Amazon Corretto JDK 21:
  112 tests run, 99 passed, 13 failed

sbt test under the initially selected Java 26 runtime:
  same 112/99/13 result

python3 scripts/host/hjpeg_host_test.py:
  234 tests passed

python3 scripts/vivado/check_reports_test.py:
  59 tests passed

python3 scripts/dev/check_chiselsim_env_test.py:
  10 tests run, 9 passed, 1 failed

git diff --check:
  passed
```

The repeated Scala result under JDK 21 and Java 26 makes a Java-version-only
explanation unlikely. Vivado was not rerun for this assessment. Timing,
utilization, and bitstream statements below are based on the evidence recorded
in [`kv260-bringup.md`](kv260-bringup.md) and [`handoff.md`](handoff.md), not on
a fresh hardware-tool invocation.

## Strengths

### Clear system goal and proof boundary

The project explicitly prioritizes:

1. decoder-valid JPEG output;
2. stage-level traceability and deterministic tests; and
3. throughput and FPGA resource efficiency.

That ordering is appropriate for a hardware codec. The documentation also
distinguishes simulation, Vivado construction, and physical-board behavior.
The project does not claim completion merely because Scala tests pass or a
bitstream can be generated.

The completion requirement is appropriately concrete: transfer a known image
through a physical KV260, capture hardware-produced bytes, and open the result
with an ordinary JPEG decoder while checking dimensions and recognizable image
content.

### Strong modular decomposition

The RTL separates:

- RGB-to-YCbCr conversion and level shifting;
- raster buffering and MCU construction;
- DCT, quantization, and zig-zag ordering;
- DC and AC tokenization;
- Huffman lookup, bit packing, and byte stuffing;
- marker and header generation;
- complete scan assembly;
- raster-coordinate and AXI-stream protocol handling; and
- KV260 and AXI-Lite integration shells.

These are meaningful stage boundaries rather than superficial wrappers. Most
stages can be simulated independently, which makes it possible to locate the
earliest incorrect representation instead of debugging only final JPEG bytes.

### Serious ready/valid and protocol handling

Internal boundaries use Chisel `Decoupled` interfaces, and many tests exercise
output backpressure. The AXI-stream wrapper handles more than the happy path:

- frame configuration is snapshotted on the first accepted input pixel;
- malformed `keep` values raise a sticky protocol error;
- unsupported frames are drained through TLAST;
- incomplete RGB frames do not enter or complete the core path;
- early and late TLAST conditions are reported;
- clear pulses reset fault state, coordinates, and buffered pipeline state; and
- AXI-Lite address and data channels are accepted independently.

This is one of the strongest aspects of the implementation. The recovery model
is explicit and designed to avoid wedging a DMA producer after malformed input.

### Explicit fixed-point behavior

The fixed-point stages generally document their arithmetic. `Dct8x8Stage` uses
Q14 cosine constants and rounds its Q28 result to integer coefficients.
`QuantizeBlockStage` documents signed rounding as nearest with halves away from
zero. Standard tables and conventional quality scaling are centralized in
`JpegTables.scala`.

That level of specificity is important in RTL: width, rounding, and saturation
changes can be tested as observable behavior rather than treated as incidental
implementation detail.

### Broad verification intent

At the assessed revision, the repository contained approximately 2,500 lines
of Chisel source and 4,300 lines of Scala tests. The tests cover:

- exact color-conversion and DCT fixtures;
- quantization tables and quality scaling;
- zig-zag ordering;
- DC categories and AC EOB/ZRL behavior;
- entropy packing and `0xff` byte stuffing;
- marker structure and frame dimensions;
- 4:4:4 and 4:2:0 modes;
- odd-sized frame padding;
- restart markers and predictor resets;
- actual decoding with Java ImageIO;
- ready/valid backpressure;
- AXI-stream frame errors and recovery;
- AXI-Lite strobes and independent channel handshakes; and
- top-level elaboration and Vivado-script structure.

The host helper tests are also unusually comprehensive. They validate PPM
packing, JPEG marker structure, standard tables, evidence consistency, malformed
inputs, decoder invocation, and run-transcript behavior.

## Critical Finding: Current Correctness Regression

The assessed checkout does not pass its primary Scala quality gate:

```text
Tests run: 112
Passed:    99
Failed:    13
```

Several failures appear to be stale latency or handshake expectations following
the introduction of slower multi-cycle header and transform implementations.
Examples include fixed timeout budgets based on header byte count and tests that
expect the first header byte immediately after accepting an MCU.

Those failures still matter: a repository whose primary suite is red cannot
reliably distinguish new regressions from accepted behavior. More importantly,
the failures are not all test drift.

### Multi-block coefficient mismatch

`JpegRasterToMcuStageSpec` presents one 16x8 stripe whose left block is neutral
gray and whose right block is a brighter gray. The first MCU is correct, but the
second MCU's luminance DC coefficient is `0` instead of the expected `16`.

Relevant locations:

- [`JpegRasterToMcuStage.scala`](../src/main/scala/hjpeg/raster/JpegRasterToMcuStage.scala)
- [`JpegRasterToMcuStageSpec.scala`](../src/test/scala/hjpeg/raster/JpegRasterToMcuStageSpec.scala)

### Decodable output with damaged image content

Two frame-level regressions decode successfully but lose most of their intended
visual separation:

- A left-dark/right-bright image produces decoded luma contrast of about
  `22.91`, below the required `80`.
- A left-red/right-blue image produces channel separation of about `10.5`,
  below the required `60`.

Relevant test:

- [`HjpegCoreSpec.scala`](../src/test/scala/hjpeg/HjpegCoreSpec.scala)

This is a serious failure under the project's own correctness-first policy. A
structurally valid JPEG is insufficient if distinct input blocks collapse into
nearly the same decoded content. The raster-to-MCU mismatch is a plausible
earlier-stage explanation and should be investigated before adjusting the
frame-level thresholds.

## RTL Architecture Assessment

### Good correctness-first structure

The current organization is easy to trace. Stripe and band buffers convert
raster order into MCU order; one transform path produces quantized zig-zag
blocks; the entropy stage maintains component DC predictors; and the stream
encoder arbitrates headers, scan bytes, restart markers, stuffing, and EOI.

The separation between `HjpegCore`, `HjpegAxiStreamCore`, and the KV260 wrappers
is also sensible. The core is not forced to know about AXI-Lite, DMA word width,
or host register layout.

### Not continuously streaming internally

The interfaces are streaming, but the current datapath does not sustain raster
input while transforming a completed stripe or band. Each raster stage follows
roughly this sequence:

```text
collect rows -> load one MCU -> transform its blocks -> emit MCU -> repeat
```

The single buffer stops accepting input outside its collect state. That is a
reasonable correctness-first implementation, but it prevents overlap between
input collection and most downstream work.

### Transform path is highly serialized

One `JpegBlockTransformStage` is shared among all component blocks in an MCU.
The DCT computes one product term per cycle, and quantization performs an
iterative divide for one coefficient at a time.

Approximate transform costs from the state machines are:

- DCT: roughly 1,024 product cycles per 8x8 block;
- quantization: roughly 1,280 divide/control cycles per block;
- 4:4:4: three component blocks per MCU; and
- 4:2:0: six component blocks per MCU.

A rough architectural estimate based only on those transform costs suggests
sub-1-fps 1080p operation at 100 MHz: approximately 0.4 fps for 4:4:4 and below
1 fps for 4:2:0. This is an inference, not a measured board result, and it omits
some overlap and additional entropy/load costs. It nevertheless shows that the
current design should not yet be described as a performant accelerator.

### Memory use is not yet well balanced

The documented Vivado 2026.1 result reports approximately:

- 50,662 CLB LUTs, or 43.26%;
- 25,619 LUTRAMs, or 44.48%;
- 2 BRAM tiles, or 1.39%; and
- 17 DSPs, or 1.36%.

The high LUTRAM and low BRAM use suggest that the stripe/band memories and
surrounding storage are not yet mapped efficiently for the target FPGA. This is
especially concerning when combined with the highly serialized throughput.
Future work should investigate synchronous BRAM inference, explicit read
latency, banking, and ping-pong row-group buffers.

### Parameterization is incomplete

Some internal modules construct bundles using a default `HjpegConfig()` instead
of receiving and propagating their enclosing configuration. Today's constraints
fix the byte width and common coordinate width, so this has limited immediate
impact. It nevertheless makes the API appear more generic than the entire
implementation actually is.

Either propagate configuration consistently or deliberately reduce the public
parameter surface to the combinations the implementation supports.

## Code Quality Assessment

### RTL source

The RTL is generally readable:

- modules are small;
- state machines have descriptive states;
- widths are explicit;
- fixed-point behavior is documented;
- standard constants are centralized; and
- there is little TODO/FIXME clutter.

The main weaknesses are behavioral rather than stylistic:

- the main suite is red;
- some tests assume obsolete fixed latencies;
- multi-block image content is currently incorrect;
- generic configuration is inconsistently propagated; and
- cycle budgets are loose enough to detect hangs but not to define a useful
  performance contract.

### Host and Vivado tooling

The host and evidence tooling is robust but too concentrated:

- `scripts/host/hjpeg_host.py` is approximately 6,400 lines;
- `scripts/host/hjpeg_host_test.py` is approximately 10,000 lines;
- `scripts/vivado/check_reports.py` is approximately 1,700 lines; and
- `scripts/vivado/check_reports_test.py` is approximately 2,350 lines.

The host module combines PPM parsing, JPEG parsing, table validation, decoder
execution, AXI-Lite access, DMA transport, evidence construction, saved-evidence
validation, Vivado-evidence validation, aggregation, and CLI handling. The tests
provide strong behavioral protection, but the implementation has crossed the
point where a single file helps comprehension.

A better internal organization would separate:

- image and PPM helpers;
- JPEG parsing and validation;
- AXI-Lite register access;
- DMA transport backends;
- host-run evidence schema and validation;
- Vivado-evidence ingestion; and
- CLI command wiring.

The exact JSON contract should remain implementation- and test-defined rather
than being manually duplicated in architecture documentation.

## Verification and CI Assessment

### Strong tests, weak current gate

The verification strategy is conceptually one of the project's best qualities.
It combines deterministic stage fixtures, backpressure tests, frame-level
decoding, wrapper equivalence, protocol recovery, elaboration, and host evidence
checks.

The immediate problem is that the primary suite has remained red after
latency-oriented RTL changes. Timing-closure work must update dependent tests in
the same change. Otherwise the test suite stops serving as an executable design
contract.

### Python suites are absent from CI

The GitHub Actions workflow runs both sbt and Mill tests, but does not run the
host, Vivado-report, or development-environment Python suites. Running two Scala
frontends provides some build-system coverage, but omitting hundreds of Python
tests leaves a large portion of the project outside the automatic gate.

Recommended CI additions:

```sh
python3 scripts/host/hjpeg_host_test.py
python3 scripts/vivado/check_reports_test.py
python3 scripts/dev/check_chiselsim_env_test.py
python3 -m py_compile \
  scripts/host/hjpeg_host.py \
  scripts/vivado/check_reports.py \
  scripts/dev/check_chiselsim_env.py
```

The development-environment helper test should first be isolated from the
caller's `SHELL` environment; the observed failure expected `SHELL=None` but
inherited `/opt/homebrew/bin/fish`.

### Missing verification layers

The current suite is broad but still lacks:

- randomized differential comparison against a software JPEG stage model;
- longer-frame regressions that cross many stripe/band boundaries;
- systematic ready/valid stall injection at multiple internal boundaries;
- formal assertions for stable payload under backpressure and frame-state
  invariants;
- coverage reporting; and
- measured end-to-end cycles for representative resolutions.

These are secondary to restoring the existing correctness suite.

## Hardware Readiness

The documented Vivado flow is substantial. It includes RTL elaboration, IP
packaging, block-design construction, synthesis, implementation, bitstream and
XSA generation, timing/utilization/DRC/route/floorplan reports, and structured
report checking.

The recorded post-implementation result claims setup WNS `+0.131 ns` and hold
WHS `+0.010 ns` at 100 MHz. That is meaningful construction evidence, but the
margins are narrow and the result predates this live assessment.

The missing proof remains decisive:

- no physical KV260 DMA transfer has been demonstrated;
- no JPEG captured from the FPGA has been decoded;
- host-observed transfer rates have not been established; and
- no useful frame-rate target has been shown.

Hardware readiness therefore remains below the quality of the simulation and
build collateral.

## Recommended Priorities

### 1. Restore image correctness

Debug the second-horizontal-MCU mismatch at the raster-to-MCU boundary. Confirm
that each block register is loaded from the intended `blockX`, that transform
input changes only when accepted, and that shared-transform output is captured
into the correct component and MCU slot.

Do not weaken the recognizable-image assertions merely to make the suite green.
Use them as frame-level confirmation after the stage mismatch is fixed.

### 2. Restore a trustworthy Scala test baseline

Classify the remaining failures into:

- genuine RTL defects;
- stale timeout budgets following multi-cycle changes; and
- testbench handshake mistakes.

Replace assumptions such as “header length equals header cycles” with waits on
ready/valid handshakes plus conservative, separately named timeout bounds.

### 3. Put all maintained tests in CI

Add the Python suites and compilation checks to GitHub Actions. Avoid treating
the host and evidence code as optional collateral when it defines the physical
completion path.

### 4. Establish explicit performance targets

Choose target resolution, sampling mode, frame rate, clock, and acceptable
resource use. Add counters or deterministic cycle tests for:

- one DCT block;
- one quantized block;
- one 4:4:4 and one 4:2:0 MCU;
- one stripe or band; and
- representative complete frames.

### 5. Rework buffering and transform throughput

After correctness is restored:

- infer BRAM-friendly synchronous memories;
- add ping-pong stripe or band buffers;
- overlap raster collection with MCU processing;
- pipeline the DCT and quantization path; and
- consider limited transform replication based on the resource budget.

### 6. Modularize the host tooling

Split the host helper by responsibility while preserving CLI behavior and the
existing tests. Define stable internal records for evidence rather than
recomputing similar field inventories in multiple large functions.

### 7. Complete physical-board validation

Only after the correctness suite is green and a fresh bitstream is built:

- program a KV260;
- confirm the generated AXI-Lite address map;
- transfer a deterministic non-flat image through DMA;
- capture the output JPEG;
- validate it structurally and with an external decoder; and
- record dimensions, content, timing, hashes, and resource evidence.

## Final Characterization

`hjpeg` demonstrates good engineering instincts, strong correctness scaffolding,
and credible FPGA integration work. Its architecture is appropriate for
building a baseline implementation that can be inspected and tested stage by
stage. The project is currently better described as a **promising,
correctness-first hardware codec prototype** than as a finished JPEG
accelerator.

The path forward is clear: repair multi-block image correctness, restore the
test gate, measure the serialized architecture honestly, and then redesign the
buffering and transform path around explicit throughput and resource targets.
