# hjpeg Architecture

`hjpeg` is a baseline JPEG encoder implemented as a streaming Chisel datapath.
The current RTL accepts raster RGB pixels and emits complete JPEG byte streams
while keeping stable AXI-facing boundaries for KV260 integration.

This document describes the major components, data flow, and integration
boundaries. See [`kv260-bringup.md`](kv260-bringup.md) for commands, generated
artifacts, and the detailed evidence required to validate a hardware build.

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

- 4:4:4 buffers one 8-row stripe and forms one Y, Cb, and Cr block per MCU.
- 4:2:0 buffers one 16-row band and forms four Y blocks plus one subsampled Cb
  and one subsampled Cr block per MCU.

Each raster stage loads one MCU into small block registers over multiple cycles.
It reuses one transform path across the component blocks and captures the
resulting coefficients before emitting the MCU packet. This keeps the stripe
memories to one read and one write port per component and avoids parallel DCT
and quantization units for every block in an MCU.

`Dct8x8Stage` is a multi-cycle separable transform. It captures one block,
computes row and column products iteratively, and holds the completed
coefficient block until its consumer accepts it. `QuantizeBlockStage` similarly
processes one coefficient at a time with a small iterative divider. Both stages
favor a smaller synthesis problem over single-cycle block latency.

After quantization, coefficients are reordered into JPEG zig-zag order. The
entropy stages difference DC coefficients per component, encode AC zero runs
with EOB and ZRL handling, select the baseline Huffman codes, pack variable
length codes into bytes, and apply `0xff` byte stuffing.

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
holds it until the matching JPEG output frame completes. Configuration writes
during an active frame therefore apply to a later frame.

Unsupported dimensions and incomplete RGB input words are drained through
input TLAST without entering or completing a JPEG frame. If the expected final
pixel arrives without TLAST, the configured frame may complete, but the wrapper
drains subsequent beats through TLAST and keeps the protocol fault asserted
until it is cleared.

## Source Layout

The main RTL groups are:

- `HjpegConfig.scala` and `HjpegBundles.scala`: static limits, frame settings,
  pixel/block data, and AXI-shaped interfaces.
- `RgbToYCbCrStage.scala` and `YCbCrLevelShiftStage.scala`: color conversion and
  signed sample preparation.
- `JpegRasterToMcuStage.scala` and `JpegRasterToSubsampledMcuStage.scala`:
  raster buffering, edge padding, and 4:4:4/4:2:0 MCU construction.
- `Dct8x8Stage.scala`, `QuantizeBlockStage.scala`, and
  `ZigZagBlockStage.scala`: coefficient transform and preparation.
- `JpegDcEncodeStage.scala`, `JpegAcBlockRunLengthStage.scala`,
  `JpegAcEncodeStage.scala`, and `JpegBlockEntropyStage.scala`: JPEG coefficient
  tokenization.
- `JpegBitstreamStages.scala`, `JpegHeaderStage.scala`, and
  `JpegMcuStreamEncoderStage.scala`: Huffman packing, marker generation, and
  complete JPEG stream assembly.
- `HjpegCore.scala`: raster RGB to JPEG byte-stream core.
- `HjpegAxiStreamCore.scala`: raster-coordinate and AXI-stream protocol shell.
- `HjpegKv260Top.scala` and `HjpegKv260AxiLiteTop.scala`: KV260-facing wrappers.
- `Elaborate.scala`: SystemVerilog generation entry points.

Focused ChiselSim tests for these stages live under `src/test/scala/hjpeg`.

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
stable under host backpressure.

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
  PS, AXI DMA, SmartConnect, reset, and interrupt infrastructure.
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
opens the result with an ordinary JPEG decoder.
