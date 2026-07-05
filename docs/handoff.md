# hjpeg Continuation Handoff

This document is for a new agent or developer picking up `hjpeg` without the
conversation history. Treat the current checkout as authoritative, but use this
as the fastest map of what has been built, what has been verified, and what
still blocks completion.

## Project Objective

Build a fully functional, performant, and simple hardware-accelerated baseline
JPEG encoder in Chisel, initially targeting AMD/Xilinx Kria KV260.

The completion bar is not just "the Scala tests pass." The project should only
be called complete after the generated RTL has been packaged in Vivado, built
into a KV260 bitstream, run on real hardware through AXI DMA, and produced a
JPEG that standard software can decode and validate.

## Current Status

The project is past scaffolding. It is a working simulated RTL prototype with
KV260 integration collateral, but it is not hardware-complete yet.

Implemented and tested in simulation:

- Baseline JPEG byte-stream generation from raster RGB input.
- 4:4:4 and 4:2:0 encoding paths.
- Edge padding for frame dimensions that do not align to MCU boundaries.
- RGB to YCbCr conversion.
- 8x8 DCT.
- Quality-scaled standard quantization tables.
- Zig-zag ordering.
- DC differencing and AC run-length coding.
- Baseline Huffman coding.
- SOI, optional JFIF APP0, DQT, SOF0, DHT, optional DRI, SOS, RST, and EOI
  marker generation.
- Entropy bit packing and `0xff` byte stuffing.
- Ready/valid flow control and sticky protocol-error reporting.
- AXI-stream-shaped RGB input and JPEG-byte output wrapper.
- AXI-Lite control/status wrapper for KV260-oriented IP packaging.
- Host helpers for PPM packing, AXI-Lite register access, stream-device DMA
  flow, and JPEG dimension validation.
- Vivado TCL entry points for synthesis, IP packaging, block design creation,
  bitstream/XSA export, and report checking.

Not yet proven:

- Vivado IP packaging on a machine with Vivado.
- Vivado block design validation.
- Synthesis, implementation, timing closure, utilization headroom, bitstream,
  and XSA generation.
- Programming a real KV260 and moving pixels through AXI DMA.
- A hardware-produced JPEG captured from the board and validated with standard
  software.
- Throughput/resource performance against a target frame size and clock.

## Important Entry Points

Start with these files:

- `AGENTS.md`: agent rules, project goal, architecture defaults, verification
  expectations, and common commands.
- `README.md`: user-facing project summary, build commands, Vivado flow, and
  host helper examples.
- `docs/architecture.md`: architecture overview and integration direction.
- `docs/kv260-bringup.md`: evidence checklist for calling the KV260 path
  complete.
- `src/main/scala/hjpeg/HjpegCore.scala`: top of the simulated JPEG datapath.
- `src/main/scala/hjpeg/HjpegAxiStreamCore.scala`: RGB AXI-stream wrapper.
- `src/main/scala/hjpeg/HjpegKv260AxiLiteTop.scala`: KV260 AXI-Lite control
  and stream top.
- `scripts/vivado/*.tcl`: Vivado build and packaging flow.
- `scripts/host/hjpeg_host.py`: host-side packing, register, DMA-device, and
  JPEG validation helper.

## Source Layout

Core Chisel modules live in `src/main/scala/hjpeg`:

- `HjpegConfig.scala`: static widths, frame limits, output width, and
  platform-facing constants.
- `HjpegBundles.scala`: frame config, RGB pixel, YCbCr pixel, encoded byte,
  AXI-Lite, and AXI-stream bundles.
- `RgbToYCbCrStage.scala`: fixed-point color conversion.
- `YCbCrLevelShiftStage.scala`: unsigned YCbCr to signed DCT-domain samples.
- `JpegRasterToMcuStage.scala`: 4:4:4 raster buffering and edge padding.
- `JpegRasterToSubsampledMcuStage.scala`: 4:2:0 raster buffering,
  subsampling, and edge padding.
- `Dct8x8Stage.scala`: 8x8 DCT.
- `QuantizeBlockStage.scala`: quality-scaled quantization.
- `ZigZagBlockStage.scala`: JPEG zig-zag ordering.
- `JpegDcEncodeStage.scala`: DC magnitude/category encoding.
- `JpegAcBlockRunLengthStage.scala`: AC run-length, EOB, and ZRL tokens.
- `JpegAcEncodeStage.scala`: AC magnitude/category encoding.
- `JpegBlockEntropyStage.scala`: block-level entropy tokenization.
- `JpegBitstreamStages.scala`: Huffman bit packing and byte stuffing.
- `JpegHeaderStage.scala`: JPEG marker/header stream.
- `JpegSingleMcuEncoderStage.scala`: legacy/small one-MCU encoder stage.
- `JpegMcuStreamEncoderStage.scala`: multi-MCU JPEG stream encoder.
- `HjpegCore.scala`: public raster RGB to encoded JPEG byte stream.
- `HjpegAxiStreamCore.scala`: AXI-stream-shaped RGB/JPEG shell.
- `HjpegKv260Top.scala`: direct-config KV260-oriented top.
- `HjpegKv260AxiLiteTop.scala`: AXI-Lite plus AXI-stream KV260 top.
- `Elaborate.scala`: SystemVerilog generation entry points.

Tests live in `src/test/scala/hjpeg`. Prefer adding focused stage tests before
frame-level tests.

## Current Protocol Contracts

`HjpegCore` input:

- `Decoupled[RgbPixel]`.
- Coordinates are supplied by the caller.
- `config.xsize` and `config.ysize` must be nonzero and within
  `HjpegConfig` limits.
- `enableChromaSubsample = false` selects 4:4:4.
- `enableChromaSubsample = true` selects 4:2:0.
- `restartInterval = 0` disables restart markers.
- `emitJfif` controls APP0 emission.

`HjpegAxiStreamCore` input:

- One RGB pixel per input beat.
- `input.bits.data[7:0]` is R.
- `input.bits.data[15:8]` is G.
- `input.bits.data[23:16]` is B.
- `input.bits.keep` must be `0b111`.
- `input.bits.last` must be asserted on the final pixel of the configured
  frame.
- Bad `keep`, mismatched `last`, and unsupported frame dimensions set the
  sticky protocol-error flag.

KV260 top-level input:

- `HjpegKv260Top` and `HjpegKv260AxiLiteTop` expose a 32-bit AXI-stream RGB
  input for AXI DMA compatibility.
- Bits `[7:0]`, `[15:8]`, and `[23:16]` are R, G, and B.
- Bits `[31:24]` are ignored.
- The low three `keep` bits must be set. The fourth `keep` bit is ignored.

Frame configuration:

- The AXI-stream wrapper snapshots `FrameConfig` on the first accepted input
  beat.
- The snapshot is held until the matching JPEG output frame completes.
- AXI-Lite writes during a frame should be treated as configuration for a later
  frame, not the active frame.

`HjpegKv260AxiLiteTop` register map:

- `0x00 control`: bit 0 clear protocol error pulse, bit 1 enable 4:2:0, bit 2
  emit JFIF APP0.
- `0x04 status`: bit 0 busy, bit 1 protocol error.
- `0x08 xsize`.
- `0x0c ysize`.
- `0x10 quality`.
- `0x14 restart interval in MCUs`.

The AXI-Lite wrapper accepts independent AW and W channel handshakes and honors
byte write strobes.

## Recent Progress

Recent commits, newest first:

- `e1cdde3 test: validate AXI RGB lane order`
- `e53b2e5 test: validate non-flat JPEG content`
- `4285331 feat: check Vivado timing reports`
- `1682f3e feat: run host stream DMA devices`
- `1d63ac6 docs: add KV260 bringup checklist`
- `a7a1f40 feat: add KV260 bitstream build script`
- `6c2ba81 feat: add KV260 host helper`
- `a2ed3bc feat: add KV260 block design script`
- `54849f1 feat: map Vivado IP bus interfaces`
- `372f778 fix: recover after unsupported AXI frames`
- `52aef3a feat: snapshot AXI frame config`
- `39a567d fix: isolate invalid core input beats`
- `933f370 feat: support decoupled AXI-Lite writes`
- `e54313e feat: validate AXI stream RGB byte lanes`
- `729d335 feat: honor optional JFIF marker control`
- `c7a33eb feat: support JPEG restart intervals`
- `ffab04c feat: implement baseline JPEG encoder pipeline`
- `22abffd docs: add agent guide`
- `ace5d7e chore: scaffold Chisel JPEG encoder`
- `2ad86d6 Initial commit`

The most recent work added a non-gray AXI wrapper regression. It compares the
JPEG bytes emitted through `HjpegAxiStreamCore` against bytes emitted by direct
`HjpegCore` input for the same RGB pattern. This catches wrapper lane/order
mistakes without depending on fragile decoded-color assertions.

An attempted decoded-color half-frame assertion was too strict for the current
quality/color path. Use decoded-image checks for broad recognizability, but use
stage or wrapper equivalence tests for exact protocol/lane invariants.

## Last Known Local Verification

The last local checkout was clean after commit `e1cdde3`.

Known passing checks:

```sh
sbt 'testOnly hjpeg.HjpegAxiStreamCoreSpec'
python3 scripts/host/hjpeg_host_test.py
python3 scripts/vivado/check_reports_test.py
git diff --check
```

Known limitation from the previous environment:

- `sbt test` was not rerun after the final test slice because the execution
  environment blocked the required escalation for user-level sbt caches.
- `vivado` was not on `PATH`, so no Vivado/IP/block-design/bitstream evidence
  was available there.

On a new machine, run these first:

```sh
git status --short
sbt test
./mill _.test
python3 scripts/host/hjpeg_host_test.py
python3 scripts/vivado/check_reports_test.py
```

If both sbt and Mill are too redundant for the moment, prefer `sbt test` first
because most recent local verification used sbt.

## Generated Artifacts

Generated RTL directories may exist locally but should not be treated as source
of truth or committed:

- `generated/`
- `generated-kv260-top/`
- `generated-kv260-axi-lite-top/`
- `build/`
- `target/`
- `out/`

Regenerate them when needed:

```sh
sbt 'runMain hjpeg.Elaborate'
sbt 'runMain hjpeg.ElaborateKv260Top'
sbt 'runMain hjpeg.ElaborateKv260AxiLiteTop'
```

Vivado scripts consume:

```text
generated-kv260-axi-lite-top/filelist.f
```

## Vivado Flow To Prove Next

If Vivado is available, prioritize this sequence:

```sh
sbt 'runMain hjpeg.ElaborateKv260AxiLiteTop'
vivado -mode batch -source scripts/vivado/package_kv260_axi_lite_ip.tcl
vivado -mode batch -source scripts/vivado/create_kv260_block_design.tcl
vivado -mode batch -source scripts/vivado/build_kv260_bitstream.tcl
python3 scripts/vivado/check_reports.py \
  --timing build/vivado/hjpeg-kv260-artifacts/post_synth_timing_summary.rpt \
  --timing build/vivado/hjpeg-kv260-artifacts/post_impl_timing_summary.rpt \
  --utilization build/vivado/hjpeg-kv260-artifacts/post_synth_utilization.rpt \
  --utilization build/vivado/hjpeg-kv260-artifacts/post_impl_utilization.rpt
```

Expected evidence is listed in `docs/kv260-bringup.md`.

Important: passing these Vivado commands proves the tool flow and bitstream
build, not board behavior. Board behavior still requires programming a KV260
and capturing output from the DMA path.

## KV260 Hardware Flow To Prove

After bitstream/XSA generation, program the board and use a Linux image or
driver stack that exposes the AXI-Lite aperture and AXI DMA channels.

Prepare input:

```sh
python3 scripts/host/hjpeg_host.py pack-ppm input.ppm input.rgb
```

Run stream-device flow if the DMA endpoints are byte-stream device files:

```sh
python3 scripts/host/hjpeg_host.py run-stream-devices \
  --base-addr 0xa0000000 \
  --tx-device /dev/hjpeg-mm2s \
  --rx-device /dev/hjpeg-s2mm \
  --input-rgb input.rgb \
  --output-jpeg output.jpg \
  --width WIDTH \
  --height HEIGHT
python3 scripts/host/hjpeg_host.py status --base-addr 0xa0000000
python3 scripts/host/hjpeg_host.py validate-jpeg output.jpg --width WIDTH --height HEIGHT
```

The device paths and base address may need adjustment for the actual board
image. If the DMA driver exposes ioctls or descriptor queues instead of simple
byte-stream device files, add a new backend around the existing packing,
register, and validation helpers.

Hardware completion evidence should include:

- The board was programmed with the generated bitstream.
- Status is idle before and after the transfer.
- No protocol error is reported for a valid frame.
- Captured output starts with SOI and ends with EOI.
- `validate-jpeg` confirms the expected dimensions.
- A standard JPEG decoder opens the result.
- A non-flat/color image decodes into recognizable visual content.

## Known Blockers And Bottlenecks

- Vivado was unavailable in the previous environment. This is the largest
  practical blocker to moving from simulated RTL to hardware evidence.
- KV260 hardware access was unavailable. This blocks final completion.
- Chisel/Verilator frame-level tests are not instant. A focused AXI wrapper
  frame-level spec recently took about 76 seconds.
- sbt may need write access to user-level caches such as `~/.sbt`, `~/.ivy2`,
  or coursier caches. In sandboxed environments, this may require escalation.
- Current decoded-color checks are good for broad recognizability but should
  not be used as exact color-lane invariants. Prefer stage-level checks or
  wrapper-vs-core byte equivalence for precise lane/protocol claims.

## Suggested Next Work

If the new PC has Vivado:

1. Run `sbt test` and the Python helper tests to establish a software baseline.
2. Regenerate `generated-kv260-axi-lite-top/`.
3. Run IP packaging.
4. Run block design creation and confirm `validate_bd_design`.
5. Run bitstream/XSA generation.
6. Run `check_reports.py` on post-synth and post-impl reports.
7. Fix the first concrete Vivado error rather than guessing from the Chisel.

If the new PC has KV260 access too:

1. Use the bitstream/XSA from the Vivado flow.
2. Confirm the AXI-Lite base address and DMA device model.
3. Run a small PPM through `hjpeg_host.py`.
4. Validate the captured JPEG.
5. Save the command transcript and enough report/output evidence to update
   `docs/kv260-bringup.md`.

If the new PC does not have Vivado or hardware:

1. Expand simulator coverage for backpressure and longer frames.
2. Add more non-flat/color image regressions at frame level.
3. Add performance-oriented counters or tests around cycles per pixel/frame.
4. Keep each slice committed and avoid large rewrites unless a test exposes a
   structural issue.

## Development Rules For Future Agents

- Keep edits narrow and commit focused slices.
- Do not commit generated RTL or build outputs.
- Read the relevant module and its spec before changing behavior.
- Prefer small deterministic tests for a single stage or protocol invariant.
- Use `docs/kv260-bringup.md` as the completion checklist.
- Do not mark the project complete without real Vivado and KV260 evidence.
- When a test fails at the JPEG byte-stream level, debug the earliest stage that
  can explain the mismatch rather than starting from final bytes alone.
- For hardware issues, separate these failure classes:
  - Chisel elaboration/SystemVerilog generation.
  - Vivado IP packaging/interface metadata.
  - Block design wiring/validation.
  - Timing/resource closure.
  - AXI-Lite register access.
  - AXI DMA transfer semantics.
  - JPEG datapath correctness.
