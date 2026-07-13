# hjpeg Architecture

`hjpeg` is a baseline JPEG encoder implemented as a streaming Chisel datapath.
The current RTL accepts raster RGB pixels and emits complete JPEG byte streams
while keeping stable AXI-facing boundaries for KV260 integration.

This document describes the major components, data flow, and integration
boundaries. See [`kv260-bringup.md`](kv260-bringup.md) for commands, generated
artifacts, and the detailed evidence required to validate a hardware build. See
[`design-decisions.md`](design-decisions.md) for the rationale and consequences
behind the major architectural choices and
[`performance-targets.md`](performance-targets.md) for cycle budgets and the
provisional throughput/resource target.

## Top-Level Flow

```text
RGB AXI stream
  -> raster coordinates and protocol checks
  -> RGB to YCbCr conversion
  -> MCU raster buffering and edge padding
  -> 8x8 DCT
  -> quantization and zig-zag ordering
  -> DC/AC tokenization and Huffman coding
  -> entropy packing and byte stuffing
  -> JPEG marker/scan assembly
  -> byte AXI stream
```

`HjpegCore` supports 4:4:4 and 4:2:0 sampling, arbitrary nonzero dimensions
within `HjpegConfig`, standard quality-scaled quantization and Huffman tables,
optional JFIF APP0 emission, and restart intervals. Dimensions that do not end
on an MCU boundary are padded by replicating the final valid row or column.

The encoder produces SOI, DQT, SOF0, DHT, SOS, and EOI markers, with optional
APP0 and DRI/RST markers. Entropy bytes equal to `0xff` are followed by stuffed
zero bytes, and restart boundaries reset the component DC predictors.

## Datapath Organization

The color stages convert one RGB pixel at a time to fixed-point YCbCr and level
shift the samples into the signed DCT domain. The raster stages then reorder
pixels into component blocks:

- 4:4:4 alternates two 8-row stripe slots and forms one Y, Cb, and Cr block per
  MCU.
- 4:2:0 alternates two 16-row band slots and forms four Y blocks plus one
  subsampled Cb and one subsampled Cr block per MCU.

Each raster stage loads one MCU into small block registers over multiple cycles.
The collector writes one slot while the processor reads the other; per-slot
ready and final-row metadata transfer ownership without adding a multiported
memory. It reuses one transform path across the component blocks and captures
the resulting coefficients before emitting the MCU packet. This keeps each
bank to one read and one write port and avoids parallel DCT and quantization
units for every block in an MCU. Component blocks are issued in order whenever
the DCT input is ready, so DCT work for a later component can overlap
quantization of an earlier component. Results are captured in the same order
and retain the quality and luminance/chrominance metadata sampled with their
input block.

`PipelinedDct8x8Stage` is the production separable transform. Exact even/odd
symmetry in the Q14 cosine matrix reduces each eight-term dot product to four
products of `x0 +/- x7` through `x3 +/- x4`. Four frequency lanes issue four
coefficients per cycle. Pair formation and exact two-product partial sums are
registered; a short final add completes each four-term dot product. Row and
column passes overlap through three banked transpose buffers, and two output
banks absorb backpressure. A registered 51-bit column-sum boundary separates
the final add from signed rounding. No rounding occurs between passes, and the
final Q28 rounding is bit-identical to the original transform. It has 38-cycle
single-block latency and a 16-cycle sustained block interval.

`PipelinedQuantizeBlockStage` handles four adjacent coefficients per cycle
through registered table lookup, floor-reciprocal multiplication, and exact
multiply-back correction. The reciprocal lookup and the 21-bit scaled-table
numerator each have dedicated registers so neither division path feeds a DSP
in the same timing stage. Two banks overlap capture, processing, and output
holding. All lanes share one constant-ROM quality-scale lookup and one
four-read, 256-entry reciprocal ROM with an explicit distributed-memory style;
the latter prevents the three production quantizers from consuming six
RAMB18s. Exact nearest rounding with halves away from zero is unchanged. The
stage has 23-cycle first-block latency and a 16-cycle sustained block interval.

`JpegBlockTransformStage` carries quality and luminance/chrominance selection
through an eight-entry ordered metadata queue. DCT output and metadata dequeue
only when the quantizer accepts both, preventing later in-flight blocks from
changing the table selection of an earlier result. The former single-lane
`Dct8x8Stage` and `QuantizeBlockStage` remain independently tested as
reference/fallback implementations but are not instantiated by the active
block transform. The combined DCT/quantize/zig-zag latency is 61 cycles, while
the sustained block interval remains 16 cycles.

After quantization, coefficients are reordered into JPEG zig-zag order. The
entropy stages difference DC coefficients per component, encode AC zero runs
with EOB and ZRL handling, select the baseline Huffman codes, pack variable
length codes into bytes, and apply `0xff` byte stuffing. AC scanning examines
four ordered coefficients per cycle while emitting at most one ordered event;
captured last-nonzero metadata terminates trailing-zero scans without a wide
remaining-block reduction. A one-entry pipelined queue registers the detected
run before Huffman selection while sustaining one event per cycle after its
single fill cycle. The packer can accept the next run while an output
byte transfers when its post-transfer buffer has capacity.

At MCU level, three buffered block-entropy slots hold independent scanners and
run queues. 4:4:4 loads Y/Cb/Cr once. 4:2:0 first loads Y0/Y1/Y2, then reuses
the same physical slots for Y3/Cb/Cr as the first wave drains. Logical block
order, DC predecessor selection, restart behavior, and the single ordered
packer stream remain unchanged while avoiding three duplicate scanners.

`JpegHeaderStage` emits marker bytes through a small output state machine. It
prepares quality-scaled DQT payload bytes over multiple cycles rather than
placing table arithmetic directly on the output-byte path. The stream encoder
arbitrates header, entropy, restart-marker, and EOI output while preserving
ready/valid backpressure.

## Streaming and Frame Boundaries

Internal streaming boundaries use `Decoupled` ready/valid interfaces. A stage
must hold its output stable while `valid` is asserted and `ready` is low.

`HjpegCore` receives explicit pixel coordinates. `HjpegAxiStreamCore` generates
those coordinates from the raster stream, checks the input `last` position, and
reports malformed input through a sticky `protocolError` flag. A
`clearProtocolError` pulse clears the fault and resets buffered pipeline state.

The AXI-stream wrapper snapshots `FrameConfig` on the first accepted pixel and
holds it while any frame uses that snapshot. A bounded two-bit count represents
one frame in the encoder plus one in each raster slot. A new frame may overlap
only when every configuration field exactly matches the snapshot and the count
is below three. A differently configured frame remains backpressured until the
active group drains. Configuration writes therefore never change marker,
sampling, quality, or restart behavior of queued frames.

Unsupported dimensions and incomplete RGB input words are drained through
input TLAST without entering or completing a JPEG frame. If the expected final
pixel arrives without TLAST, the configured frame may complete, but the wrapper
drains subsequent beats through TLAST and keeps the protocol fault asserted
until it is cleared.

## Source Layout

The main RTL groups under `src/main/scala/hjpeg` are:

- the package root: shared configuration/bundles/tables and `HjpegCore`;
- `color/`: RGB conversion and level shifting;
- `raster/`: 4:4:4 and 4:2:0 synchronous raster buffering, padding, and MCU
  construction; 4:4:4 uses eight column banks and 4:2:0 uses row-parity/four-
  column banking, while both explicitly pipeline one-cycle block-RAM reads;
- `transform/`: DCT, quantization, zig-zag ordering, and their composition;
- `entropy/`: DC/AC tokenization, Huffman lookup, packing, and byte stuffing;
- `stream/`: JPEG header and complete multi-MCU stream assembly;
- `integration/`: AXI-stream, AXI-Lite, KV260, and elaboration entry points; and
- `reference/`: single-MCU and fixed 8x8 reference paths outside the active
  `HjpegCore` datapath.

The directories organize the source by hardware role; every file remains in
Scala package `hjpeg`. Focused ChiselSim tests mirror the same directories under
`src/test/scala/hjpeg`.

## Generated Design Views

Run `./scripts/dev/generate-design-graphs` from the repository root to elaborate
the current KV260 AXI-Lite top and generate module views under
`build/design-graphs/`. The helper emits:

- an instance hierarchy with repeated hardware instances expanded;
- a deduplicated module-dependency graph with instance names on its edges;
- focused dependency cones for the AXI wrapper, encoder core, block transform,
  and MCU stream encoder; and
- Mermaid, Graphviz DOT, and, when Graphviz is installed, SVG versions.

The graphs are derived from Verilator's elaborated representation of the
generated SystemVerilog rather than from the Scala call graph. They therefore
describe the hardware Chisel emitted, including generated memories and
parameterized module variants. They show ownership and instantiation, not
ready/valid timing, state transitions, or synthesized signal cones. Use
simulation waveforms for temporal behavior and Vivado schematics for the
synthesized or implemented design.

## KV260 Integration

The internal `HjpegAxiStreamCore` input is 24 bits wide: R occupies bits
`[7:0]`, G bits `[15:8]`, and B bits `[23:16]`, with `keep = 0b111`. The KV260
wrappers expose a DMA-compatible 32-bit input. Its low three bytes retain the
same RGB order, the high byte is ignored, and all three low `keep` bits must be
set.

`HjpegKv260AxiLiteTop` adds control registers for dimensions, quality, restart
interval, chroma mode, and JFIF emission, plus busy and sticky protocol-error
status. AXI-Lite write-address and write-data channels are accepted
independently, writable registers honor byte strobes, and responses remain
stable under host backpressure. Read-only offsets `0x18`/`0x1c` hold the last
completed frame's 64-bit latency in PL cycles and `0x20` counts completed output
frames. A three-entry timestamp FIFO pairs first accepted input beats with
accepted output TLAST beats in frame order, matching the maximum three in-flight
frames.

The RTL tops are integration boundaries, not complete board designs. Platform
clocking, reset synchronization, PS configuration, DMA, address assignment,
interrupts, constraints, and software access are supplied by the Vivado and
host layers.

## Vivado Collateral

Tracked scripts under `scripts/vivado/` provide a reproducible path from
elaborated RTL to a KV260 bitstream:

- `synth_kv260_axi_lite.tcl` creates the project and runs synthesis.
- `package_kv260_axi_lite_ip.tcl` packages explicit clock, reset, AXI-Lite, and
  AXI-stream interfaces as reusable IP.
- `create_kv260_block_design.tcl` connects the encoder to the Zynq UltraScale+
  PS, AXI DMA, SmartConnect, reset, and interrupt infrastructure. The DMA uses
  a 26-bit buffer-length field so a packed 3840x2160 input fits in one MM2S
  transaction and produces exactly one frame-ending TLAST. Its optional stream
  width is 32 bits for the scalar top or 128 bits for the four-pixel UHD top.
- `build_kv260_bitstream.tcl` runs implementation, writes the bitstream and
  reports, and exports an XSA.
- `write_kv260_floorplan_report.tcl` regenerates floorplan evidence from an
  existing implementation run.
- `check_reports.py` validates artifacts, address assignment, timing,
  utilization, DRC, routing, clocking, and floorplan reports.

The flow consumes `generated-kv260-axi-lite-top/filelist.f`, produced by
`hjpeg.ElaborateKv260AxiLiteTop`. `check_reports.py` can gate a partial
post-synthesis run or require the complete bitstream evidence set, and its
strict JSON output makes the build checks reproducible.

See [`kv260-bringup.md`](kv260-bringup.md) for exact commands, expected
filenames, and pass criteria. A successful Vivado build does not by itself prove
DMA operation or JPEG encoding on a physical board.

## Host-Side Flow

`scripts/host/hjpeg_host.py` provides the userspace boundary around the KV260
design. It can:

- generate deterministic P6 PPM fixtures and pack them into the DMA RGB layout;
- configure and inspect the AXI-Lite registers through `/dev/mem`;
- drive byte-stream DMA endpoints and capture the JPEG output;
- validate JPEG structure, configured frame properties, standard tables,
  restart behavior, and external-decoder compatibility; and
- emit and recheck strict JSON transcripts for hardware evidence.

`run-stream-devices` is the initial DMA backend. It targets Linux board images
that expose MM2S and S2MM as byte-stream device files. Drivers based on ioctls
or descriptor queues should add a separate transport backend while reusing the
packing, register, JPEG validation, and evidence helpers.

`scripts/host/run_kv260_xsdb_dma.tcl` is the intrusive lab backend for boards
without those Linux devices. After PS clocks and DDR are initialized, it stops
A53 #0, programs the PL, loads packed RGB into DDR, drives AXI-Lite and simple
DMA registers through JTAG, and reads exactly the S2MM-reported JPEG bytes back.
It is useful for deterministic physical validation, but debugger polling is not
a precise performance timer. The runner instead reports the hardware cycle
registers as `FRAME_TIMING`, which is independent of JTAG polling overhead.

Hardware evidence connects four boundaries: the source PPM and packed RGB
stream, the requested encoder configuration, AXI-Lite status observations, and
the captured JPEG plus external-decoder result. The checker hashes artifacts,
recomputes summary fields from their underlying records, and can cross-check a
run against the Vivado address map and build evidence. Partial smoke tests may
omit the complete-evidence gate; final board validation must require it.

See [`kv260-bringup.md`](kv260-bringup.md) for the board procedure and evidence
criteria. CLI help and `scripts/host/hjpeg_host_test.py` are the source of truth
for individual options and validation behavior.

## Completion Boundary

Simulation establishes stage behavior and complete JPEG generation in the RTL
model. Vivado establishes that the design can be packaged, placed, routed, and
timed for the target part. Completion additionally requires a physical KV260
run that transfers a known image through DMA, captures the encoder's bytes, and
opens the result with an ordinary JPEG decoder. The 2026-07-12 KV260 evidence
meets this functional boundary for both a small padded/restart frame and a
1920x1080 frame in both chroma modes. At 100 MHz the quality-85 deterministic
benchmark measures 45.23 fps in 4:2:0 and 31.01 fps in 4:4:4. A seeded-random
quality-90 stress frame measures 45.22 and 26.78 fps respectively, so the
30-fps result is a defined-benchmark claim, not a content-independent bound.
