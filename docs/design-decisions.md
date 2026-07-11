# hjpeg Design Decisions

This document records the reasoning behind architectural choices that are not
obvious from the RTL alone. It is not a changelog or an exhaustive interface
specification. Register maps, commands, generated artifacts, and validation
fields belong in source, CLI help, and [`kv260-bringup.md`](kv260-bringup.md).

Each decision has one of these states:

- **Accepted:** part of the intended design unless new evidence justifies a
  change.
- **Provisional:** useful for the current correctness-first implementation, but
  expected to be revisited against performance or resource targets.
- **Superseded:** retained only when its history still explains the current
  design.

## Correctness precedes throughput

**Status:** Accepted

**Decision:** Optimize first for decoder-valid JPEG output, then for stage-level
traceability, and only then for throughput and FPGA resource use.

**Context:** JPEG failures are easiest to isolate at the earliest incorrect
stage. Optimizing an unverified transform or entropy path would make the design
harder to debug without establishing that the emitted file is useful.

**Consequences:**

- Stages remain independently simulatable.
- Small deterministic fixtures take priority over large performance tests.
- Area and latency tradeoffs may be temporarily conservative.
- A timing-clean bitstream is not sufficient if its bytes do not decode.

## Completion requires physical decoder-valid output

**Status:** Accepted

**Decision:** Treat simulation, Vivado construction, and physical-board
operation as separate proof levels. The encoder is complete only after a known
image is transferred through a KV260, the hardware-produced bytes are captured,
and an ordinary JPEG decoder accepts the result with the expected dimensions
and recognizable content.

**Consequences:**

- ChiselSim proves RTL behavior, not board integration.
- Vivado reports prove construction, timing, and resource properties, not DMA
  operation.
- Host and Vivado evidence may be correlated, but neither substitutes for a
  physical run.
- [`kv260-bringup.md`](kv260-bringup.md) defines the completion checklist.

## Use ready/valid stage boundaries

**Status:** Accepted

**Decision:** Use Chisel `Decoupled` ready/valid interfaces at internal
streaming boundaries.

**Context:** Transform, entropy, and output stages have different and sometimes
data-dependent latencies. Explicit flow control permits local testing and
output backpressure without relying on fixed global schedules.

**Consequences:**

- A producer holds `valid` and its payload stable until `ready` is asserted.
- Backpressure behavior is part of each stage's contract and test surface.
- Multi-cycle implementations can replace combinational ones without changing
  the surrounding interface.

## Buffer raster input by MCU-row groups

**Status:** Accepted

**Decision:** Buffer one 8-row stripe for 4:4:4 or one 16-row band for 4:2:0,
then construct MCUs from that storage.

**Context:** Raster-order pixels do not arrive in JPEG block order. A full-frame
buffer is unnecessary, but enough rows must be retained to form every block in
an MCU row and to subsample chroma in 4:2:0 mode.

**Consequences:**

- Storage scales with maximum frame width rather than frame area.
- 4:4:4 produces one Y, Cb, and Cr block per MCU.
- 4:2:0 produces four Y blocks and one downsampled block for each chroma
  component.
- The current single-buffer implementation pauses raster input while processing
  a completed stripe or band.

## Replicate edge samples for partial MCUs

**Status:** Accepted

**Decision:** Pad incomplete right and bottom MCU edges by repeating the final
valid column or row sample.

**Context:** Baseline JPEG operates on complete 8x8 component blocks, while
input dimensions need not be multiples of 8 or 16. Edge replication avoids an
artificial dark border and is deterministic in both sampling modes.

**Consequences:**

- The SOF0 dimensions retain the original image size.
- Padding exists only inside the encoded edge blocks.
- Tests must include dimensions that cross both horizontal and vertical MCU
  boundaries.

## Use deterministic fixed-point transforms

**Status:** Accepted

**Decision:** Implement color conversion and DCT arithmetic with documented
fixed-point coefficients, widths, rounding, and saturation rather than
floating-point hardware.

**Context:** Fixed-point arithmetic is synthesizable, deterministic, and easier
to compare against small software references. `Dct8x8Stage` uses Q14 cosine
constants and rounds the final Q28 result to integer coefficients.

**Consequences:**

- Stage tests should check exact coefficients for simple fixtures.
- Width or rounding changes are observable format-quality changes and require
  focused regressions.
- Decoder success alone is insufficient to detect excessive numerical error.

## Use standard tables and libjpeg-style quality scaling

**Status:** Accepted

**Decision:** Emit the standard baseline luminance and chrominance Huffman and
quantization tables. Scale quantization values from a clamped quality setting in
the range 1 through 100 using the common libjpeg-style rule.

**Context:** Standard tables simplify interoperability and make headers and
entropy output independently checkable. A conventional quality mapping provides
predictable host-visible behavior without programmable table storage.

**Consequences:**

- Quality zero is treated as one in RTL, while host interfaces reject values
  outside their supported range.
- Quantization values are clamped to the baseline 8-bit range.
- Custom Huffman or quantization tables require an explicit future interface
  and are not implied by the current configuration bundle.

## Support 4:4:4 first and 4:2:0 as an explicit mode

**Status:** Accepted

**Decision:** Keep 4:4:4 as the direct component-block path and select 4:2:0
through `enableChromaSubsample`.

**Context:** The modes have different MCU geometry, buffering, header sampling
factors, and component order. Keeping the choice explicit avoids inferring
format behavior from dimensions or host-side conventions.

**Consequences:**

- The selected mode affects both raster-to-MCU construction and SOF0 metadata.
- Frame-level regressions must decode both modes and cover non-aligned edges.
- Additional subsampling modes should be added only with explicit geometry and
  header contracts.

## Snapshot configuration at the frame boundary

**Status:** Accepted

**Decision:** The AXI-stream wrapper captures `FrameConfig` on the first
accepted input pixel and holds it until the corresponding JPEG output frame
completes.

**Context:** AXI-Lite writes are independent of pixel and output traffic. Using
live control-register values during a frame could mix dimensions, quality,
sampling, or marker behavior within one JPEG.

**Consequences:**

- Mid-frame AXI-Lite writes configure a later frame.
- The public `HjpegCore` expects its direct caller to keep configuration stable
  for an active frame.
- The snapshot is released only after the output byte marked `last` is accepted.

## Drain malformed input frames through TLAST

**Status:** Accepted

**Decision:** Raise a sticky protocol error for unsupported dimensions,
incomplete RGB words, or TLAST mismatches. When a malformed frame cannot safely
enter the JPEG pipeline, accept and drain input through TLAST instead of
backpressuring the stream indefinitely.

**Context:** A DMA producer may continue presenting the remainder of a bad
packet. Refusing those beats can wedge the channel and prevent software from
recovering for the next frame.

**Consequences:**

- Invalid beats do not enter the encoder core.
- Software can observe the sticky fault after the stream drains.
- `clearProtocolError` clears the flag and resets wrapper coordinates and
  buffered core state so a partial frame cannot contaminate the next one.
- Recovery behavior is a protocol contract, not merely diagnostic handling.

## Use a 32-bit DMA input word with RGB in the low bytes

**Status:** Accepted

**Decision:** Keep the internal RGB stream at 24 bits, but expose a 32-bit
KV260-facing input word. Bits `[7:0]`, `[15:8]`, and `[23:16]` carry R, G, and
B; bits `[31:24]` are ignored.

**Context:** A 32-bit stream is convenient for the targeted AXI DMA integration
while preserving the encoder's natural three-byte pixel representation.

**Consequences:**

- The three low `keep` bits are required for every pixel.
- The fourth `keep` bit is ignored with the unused high byte.
- Host packing writes four bytes per pixel in `R, G, B, unused` order.

## Target 1080p30 at a 100 MHz PL clock

**Status:** Provisional

**Decision:** Use decoder-valid 1920x1080 at 30 frames per second in both 4:4:4
and 4:2:0 as the minimum throughput target, with a 100 MHz programmable-logic
clock and no tracked post-implementation fabric resource above 70%.

**Context:** The configured frame limit is already 1920x1080, but “performant”
was previously undefined. Without a resolution, sampling mode, frame rate,
clock, and resource budget, architecture changes cannot be evaluated against a
stable requirement. [`performance-targets.md`](performance-targets.md) derives
the corresponding MCU and block budgets and records the current simulation
baseline.

**Consequences:**

- At 100 MHz, a frame has an average budget of 3,333,333 cycles.
- The transform must eventually achieve a small initiation interval; reducing
  latency without improving sustained block throughput is insufficient.
- Timing/resource claims require fresh Vivado evidence for the exact RTL.
- Final frame-rate claims require a physical KV260 transfer and decoder-valid
  captured output.
- Correctness remains a prerequisite and cannot be traded away to meet the
  throughput number.

**Revisit when:** Board measurements, host bandwidth, or implementation
evidence show that 100 MHz or the 70% resource ceiling is the wrong platform
contract. Change the target explicitly rather than silently redefining success.

## Share one four-lane transform path per raster stage

**Status:** Provisional

**Decision:** Reuse one `JpegBlockTransformStage` across all component blocks in
an MCU. Within that shared path, issue four exactly factorized DCT coefficients
and four exact reciprocal-quantized coefficients per cycle. Sustain one block
every 16 cycles while preserving the existing block-level `Decoupled`
interfaces and coefficient results.

**Context:** Earlier parallel and combinational structures created excessive
memory-port, synthesis, and timing pressure. Serialization made the transform
smaller and its intermediate values easier to test.

**Consequences:**

- The 4:4:4 path issues three ordered blocks per MCU through one transform.
- The 4:2:0 path issues six ordered blocks per MCU through one transform.
- Exact Q14 cosine symmetry reduces every eight-term DCT dot product to four
  products of `x0 +/- x7` through `x3 +/- x4`; this changes neither intermediate
  integer sums nor final Q28 rounding.
- Four row and four column lanes overlap through three transpose banks. Two
  result banks retain output order under backpressure.
- Four quantizer lanes share one quality-scale calculation and retain the
  registered floor-reciprocal estimate plus exact multiply-back correction.
  Two banks overlap capture, processing, and output holding.
- An eight-entry metadata queue aligns quality and component selection with all
  in-flight DCT blocks.
- DCT, quantizer, and complete-transform input/output intervals are 16 cycles in
  deterministic RTL simulation, below the 34.3-cycle 4:4:4 block budget.
- Current implementation closes 100 MHz timing at setup WNS `+0.245 ns` and
  hold WHS `+0.010 ns`, with 127 DSPs, but consumes 90.51% of CLBs. The timing
  result is acceptable; the resource result fails the provisional ceiling.
- Serial raster collection and MCU loading, rather than block-transform issue
  rate, now dominate simulated frame throughput.
- Resource savings and timing closure must be judged together with measured
  frame throughput.

**Revisit when:** BRAM-oriented storage work materially changes routing or
resource pressure, or if raster overlap changes the required transform
buffering/issue contract.

## Serialize stripe-memory reads

**Status:** Provisional

**Decision:** Load MCU samples from stripe or band storage over multiple cycles
before starting the shared transform.

**Context:** Reading every block sample in parallel creates a memory with too
many read ports for practical FPGA inference. Serial loading gives each
component store a bounded access pattern.

**Consequences:**

- MCU construction adds load latency before transform latency.
- The current memories consume substantial LUTRAM in the documented Vivado
  implementation despite low BRAM use.
- The access pattern is compatible with a future synchronous-memory design, but
  the current implementation should not be assumed optimal.

**Revisit when:** Optimizing resource use or continuous input throughput.
Investigate BRAM-friendly `SyncReadMem`, explicit read latency, banking, and
ping-pong stripe or band buffers.

## Generate header bytes with a multi-cycle state machine

**Status:** Accepted

**Decision:** Emit static marker bytes through a small state machine and prepare
quality-scaled DQT bytes over registered multiply/divide steps.

**Context:** Driving the output byte directly from header index decoding and
quantization arithmetic created an avoidable timing path to the DMA interface.

**Consequences:**

- Header emission is not one byte per clock for every byte.
- Tests and performance budgets must wait on handshakes rather than assume a
  fixed cycle count equal to header length.
- Header latency is small relative to frame transforms and is accepted in
  exchange for timing isolation.

## Keep the first host transport backend simple

**Status:** Provisional

**Decision:** Implement the initial board runner for Linux systems that expose
DMA MM2S and S2MM endpoints as byte-stream device files.

**Context:** This provides a small, testable end-to-end host path without
embedding a particular ioctl or descriptor-queue ABI into image packing,
register access, or JPEG validation.

**Consequences:**

- `run-stream-devices` is a transport backend, not the general DMA abstraction.
- Drivers using ioctls or descriptor queues need separate adapters.
- New transports should reuse PPM packing, AXI-Lite configuration, JPEG
  validation, and evidence generation.

## Keep evidence strict but outside the RTL architecture

**Status:** Accepted

**Decision:** Emit strict JSON records for host runs and Vivado checks, hash the
relevant artifacts, and cross-check configuration, address-map, status, input,
output, and decoder observations for final evidence.

**Context:** A collection of successful commands is difficult to audit after
the fact. Structured evidence makes missing proof and disagreement between build
and runtime configuration visible.

**Consequences:**

- Complete-evidence gates are appropriate for final runs; partial smoke tests
  may omit them.
- Exact evidence fields remain defined by implementation and tests rather than
  this design document.
- Evidence code should be modularized when its size begins to obscure the
  underlying proof model.
