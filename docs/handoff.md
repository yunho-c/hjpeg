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
  flow, and JPEG validation/evidence checks.
- Vivado TCL entry points for synthesis, IP packaging, block design creation,
  bitstream/XSA export, and report checking.

Proven locally with Vivado 2026.1:

- KV260 AXI-Lite top SystemVerilog elaboration.
- KV260 AXI-Lite top synthesis through post-synthesis report generation, with
  the current post-synthesis setup timing/utilization report gate passing at
  the default 100 MHz threshold.
- Vivado IP packaging.
- Vivado block design creation and validation, with remaining non-fatal
  PS/SmartConnect/addressing warnings.
- Vivado bitstream generation, bitstream copy, XSA export with bitstream
  included, and post-synthesis/post-implementation report checking.

Not yet proven:

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
byte write strobes. Unmapped AXI-Lite reads return zero, and unmapped writes
complete without changing mapped control/status registers.

## Recent Progress

Recent commits, newest first:

- `5396bd7 test: cover host control word evidence`
- `3c2ca4b test: cover unmapped axi-lite accesses`
- `9484f80 fix: accept vivado report wording variants`
- `fc367d6 test: cover vivado report failure evidence`
- `d9e6873 feat: record vivado clock utilization evidence`
- `3df0aec feat: gate vivado drc and route reports`
- `4451a3e feat: emit vivado implementation review reports`
- `d76f14a test: cover axi-lite narrow register strobes`
- `b671d6f test: cover axi-lite control strobes`
- `d7bbd82 test: cover axi-lite frame error recovery`
- `94c1788 test: cover axi-lite unsupported frame status`
- `65f3b55 test: cover axi-lite tlast error status`
- `b536c2e test: cover unused axi keep byte`
- `e2351c1 feat: record vivado clock target evidence`
- `c1a1913 feat: record host transfer rates`
- `59fac7c feat: record host transfer timing`
- `2e783c6 feat: record expected run input length`
- `feaa989 feat: record status checkpoint targets`
- `c3fc4c3 feat: record ppm image stats in evidence`
- `9832255 feat: record status target in evidence`
- `50e2afa test: cover axi-lite protocol error clear`
- `2dc5ea7 feat: emit clear-error evidence as json`
- `80339a8 feat: record host capture limits`
- `0c22dd8 feat: capture decoder output evidence`
- `58ea305 feat: verify jpeg jfif emission`
- `3057e3b feat: verify jpeg chroma mode`
- `65071d6 feat: verify jpeg restart interval`
- `a351321 feat: record jpeg restart interval evidence`
- `f800d3c feat: validate jpeg table markers`
- `8b390e2 feat: gate host frames by rtl limits`
- `818acc0 fix: parse vivado utilization columns`
- `bbd42cc fix: gate vivado hold timing explicitly`
- `c216ead feat: check vivado hold slack`
- `9ac6010 docs: refresh evidence workflow handoff`
- `347a537 feat: record encoder config evidence`
- `42e4e7e feat: record decoder command in evidence`
- `e699cce feat: emit host input evidence as json`
- `1cfcc6c feat: include run status checkpoints in evidence`
- `9ac22d8 feat: emit host status evidence as json`
- `a5317a7 feat: hash vivado build artifacts`
- `1f02f2f feat: emit vivado report evidence as json`
- `72bc40f feat: link host input and JPEG evidence`
- `bd4c44f feat: include host JPEG artifact hashes`
- `18ae4cd feat: emit host validation evidence as json`
- `1ba84b0 feat: report host JPEG scan evidence`
- `95eeac4 docs: refresh host validation handoff`
- `7340af8 fix: harden host decoder command parsing`
- `1742948 feat: support external JPEG decoder checks`
- `40b4bc4 test: require JPEG scan data in host validation`
- `d4bd430 feat: generate host bringup test pattern`
- `6dd9e86 feat: check host DMA run status`
- `2240abe fix: close kv260 implementation timing`
- `7dae97e fix: reduce transform synthesis pressure`
- `d3158ae fix: align kv260 dma stream width`
- `2c2b15f fix: unblock windows vivado bringup`
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

Recent local work aligned the KV260-facing input stream with AXI DMA's supported
32-bit stream widths. The internal encoder still consumes 24-bit RGB pixels; the
KV260 tops accept `R,G,B,unused` 32-bit beats and drop the unused byte.

The raster-to-MCU stages were also changed to serially load each MCU from stripe
or band memories into small block registers before DCT. Generated
`mem_15360x9.sv` and `mem_30720x9.sv` now have one read port and one write port,
which removes the previous Vivado synthesis failure caused by too many memory
read ports. A later source edit reuses one `JpegBlockTransformStage` per raster
stage across the MCU's component blocks, reducing the 4:4:4 path from three
parallel block transforms to one and the 4:2:0 path from six to one.

The latest local source edits change `Dct8x8Stage` from a fully combinational
two-dimensional DCT into a multi-cycle row/column engine and change
`QuantizeBlockStage` into a one-coefficient-at-a-time quantizer with an
iterative unsigned divider. The DCT accepts one block, serializes each 8-term
row and column accumulation one product term per cycle, then presents the
completed coefficient block. The quantizer captures a DCT block and serializes
the rounded coefficient/table division so the 64 output coefficients are not all
driven through parallel divider logic.

`JpegHeaderStage` was also changed from a one-byte-per-cycle combinational
header mux into a small byte-generation FSM. Static marker bytes still emit in
order, but DQT payload bytes now latch the selected table entry and quality
scale, multiply in a registered DSP stage, then divide/clamp before presenting
the output byte. This removes the previous header-index-to-DMA-data timing path
seen in the block-design implementation reports.

The non-gray AXI wrapper regression compares the JPEG bytes emitted through
`HjpegAxiStreamCore` against bytes emitted by direct `HjpegCore` input for the
same RGB pattern. This catches wrapper lane/order mistakes without depending on
fragile decoded-color assertions.

An attempted decoded-color half-frame assertion was too strict for the current
quality/color path. Use decoded-image checks for broad recognizability, but use
stage or wrapper equivalence tests for exact protocol/lane invariants.

Recent host/Vivado helper work made the evidence path machine-readable. The
host helper now supports JSON evidence for `make-test-ppm`, `pack-ppm`,
`config`, `status`, `clear-error`, `validate-jpeg`, and `run-stream-devices`.
Standalone status evidence records the AXI-Lite target along with the raw and
decoded status word. Clear-error evidence records the AXI-Lite target and
control word used to pulse sticky fault recovery. Configuration and run
evidence record the encoder control word in numeric and hex form, tying the
chroma/JFIF/clear-error flags back to the AXI-Lite write value. The run evidence
ties together the input RGB stream hash, output JPEG hash, AXI-Lite target,
encoder configuration, status checkpoints, host-observed transfer elapsed
seconds and derived byte rates, optional decoder command, decoder timeout, and
bounded decoder stdout/stderr. Host JPEG validation now checks more
than dimensions: it requires DQT and DHT
markers, records DQT/DHT table IDs and SOS component table selectors, rejects
dangling table references, records APP0/DQT/DHT/DRI/RST marker counts, parses
DRI restart intervals, requires exactly one SOF0 and one SOS segment, requires
8-bit three-component SOF0 shape, requires SOS components to match SOF0 exactly,
requires SOF0/SOS component IDs in `[1, 2, 3]` order, requires baseline SOS
spectral fields `0/63/0`, records SOF0 component sampling factors and MCU count,
requires supported 4:4:4 or 4:2:0 sampling, requires standard DQT table IDs
`{0, 1}` with 8-bit precision, records DQT/DHT payload byte counts and SHA-256
hashes, requires exact DQT/DHT segment counts, requires the standard DC/AC
Huffman table set, records parsed marker sequence, stuffed entropy `0xff` byte
count, RST marker sequence, and JFIF APP0 signature count, rejects RST markers
without DRI, out-of-sequence RST markers, unexpected non-RST/non-EOI markers
after SOS, and trailing bytes after EOI, and can enforce expected restart
interval, exact RST marker count for the parsed MCU count, chroma mode, and
JFIF APP0 signature presence.
`run-stream-devices` enforces those expectations automatically from the
configured AXI-Lite control fields.

The host helper defaults input-prep and hardware-run dimensions to the current
KV260 top's `1920x1080` limit. Use `--max-width` and `--max-height` only for a
custom RTL elaboration with different `HjpegConfig` frame limits.

The Vivado report checker supports `--json` plus repeated `--artifact`
arguments so the bitstream, XSA, timing reports, utilization reports, DRC
reports, route-status reports, and clock-utilization reports can be recorded
with byte lengths, hashes, target clock period/frequency, parsed setup WNS and
hold WHS, utilization rows, DRC violations, route-status counts, thresholds,
and pass/fail state. Missing, non-file, or unparseable reports are recorded as
structured JSON failures instead of aborting the transcript. Use `--hold-timing`
for post-implementation timing reports; post-synthesis hold can be negative
before implementation fixes it. The utilization parser handles Vivado's
`Prohibited` column and records hard-system rows such as `PS8` without gating
them against the fabric utilization threshold. The DRC gate fails Error and
Critical Warning violations, the route-status gate fails nonzero unrouted or
routing-error counts, and clock-utilization reports are required and hashed as
review evidence.

## Last Known Local Verification

The current clean checkout has recent host/Vivado evidence-helper updates on top
of the last full Vivado bitstream validation. Local source changes since that
Vivado run have been limited to Python helpers and documentation, not RTL.

Known passing checks:

```sh
CHISEL_FIRTOOL_PATH='C:\Users\G14\GitHub\hjpeg\null\org.chipsalliance\llvm-firtool\cache\1.149.0\bin' \
  sbt 'testOnly hjpeg.HjpegElaborationSpec hjpeg.VivadoScriptsSpec'
CHISEL_FIRTOOL_PATH='C:\Users\G14\GitHub\hjpeg\null\org.chipsalliance\llvm-firtool\cache\1.149.0\bin' \
  sbt 'runMain hjpeg.ElaborateKv260AxiLiteTop'
python3 scripts/host/hjpeg_host_test.py
python3 scripts/vivado/check_reports_test.py
python3 -m py_compile scripts/host/hjpeg_host.py scripts/vivado/check_reports.py
git diff --check
vivado -mode batch -source scripts/vivado/package_kv260_axi_lite_ip.tcl
vivado -mode batch -source scripts/vivado/create_kv260_block_design.tcl
vivado -mode batch -source scripts/vivado/synth_kv260_axi_lite.tcl
vivado -mode batch -source scripts/vivado/build_kv260_bitstream.tcl
python3 scripts/vivado/check_reports.py \
  --artifact build/vivado/hjpeg-kv260-artifacts/hjpeg_kv260.bit \
  --artifact build/vivado/hjpeg-kv260-artifacts/hjpeg_kv260.xsa \
  --timing build/vivado/hjpeg-kv260-artifacts/post_synth_timing_summary.rpt \
  --timing build/vivado/hjpeg-kv260-artifacts/post_impl_timing_summary.rpt \
  --hold-timing build/vivado/hjpeg-kv260-artifacts/post_impl_timing_summary.rpt \
  --utilization build/vivado/hjpeg-kv260-artifacts/post_synth_utilization.rpt \
  --utilization build/vivado/hjpeg-kv260-artifacts/post_impl_utilization.rpt \
  --drc build/vivado/hjpeg-kv260-artifacts/post_impl_drc.rpt \
  --route-status build/vivado/hjpeg-kv260-artifacts/post_impl_route_status.rpt \
  --clock-utilization build/vivado/hjpeg-kv260-artifacts/post_impl_clock_utilization.rpt \
  --json
```

Known local limitations:

- Full ChiselSim tests on Windows/MSYS currently fail before simulation or
  harness compilation because svsim emits Windows-style Makefile/file-list paths
  while MSYS `make`, Verilator, and the MinGW/UCRT C++ toolchain consume parts
  of the flow as POSIX paths. A local shim proved the first `make clean` failure
  can be bypassed, but the generated harness then hit path normalization issues
  and a missing POSIX `getline` symbol in the MinGW build.
- Latest focused attempt on this Windows/MSYS setup:
  `sbt 'testOnly hjpeg.HjpegCoreSpec'` compiled the updated spec, then all
  simulations failed at svsim `make clean` because the generated Makefile ran
  `for /f "delims=" ...` under `/bin/sh`. This matches the known simulator
  path/shell incompatibility above; it does not validate the new cycle-budget
  regression until run on a compatible simulator setup.
- The current block-design Vivado reports pass the default 100 MHz
  setup/hold/utilization gates. Latest artifact reports show post-synthesis
  setup WNS `+0.807 ns` and post-implementation setup WNS `+0.131 ns`;
  post-implementation hold WHS is `+0.010 ns`. Post-implementation utilization is approximately
  50,662 CLB LUTs (43.26%), 25,619 LUTRAMs (44.48%), 2 BRAM tiles (1.39%), and
  17 DSPs (1.36%).

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
  --artifact build/vivado/hjpeg-kv260-artifacts/hjpeg_kv260.bit \
  --artifact build/vivado/hjpeg-kv260-artifacts/hjpeg_kv260.xsa \
  --timing build/vivado/hjpeg-kv260-artifacts/post_synth_timing_summary.rpt \
  --timing build/vivado/hjpeg-kv260-artifacts/post_impl_timing_summary.rpt \
  --hold-timing build/vivado/hjpeg-kv260-artifacts/post_impl_timing_summary.rpt \
  --utilization build/vivado/hjpeg-kv260-artifacts/post_synth_utilization.rpt \
  --utilization build/vivado/hjpeg-kv260-artifacts/post_impl_utilization.rpt \
  --drc build/vivado/hjpeg-kv260-artifacts/post_impl_drc.rpt \
  --route-status build/vivado/hjpeg-kv260-artifacts/post_impl_route_status.rpt \
  --clock-utilization build/vivado/hjpeg-kv260-artifacts/post_impl_clock_utilization.rpt \
  --json
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
python3 scripts/host/hjpeg_host.py make-test-ppm input.ppm --width WIDTH --height HEIGHT --json
python3 scripts/host/hjpeg_host.py pack-ppm input.ppm input.rgb --json
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
  --height HEIGHT \
  --restart-interval RESTART_INTERVAL \
  --chroma-subsample \
  --decoder-command 'magick identify {jpeg}' \
  --decoder-timeout-seconds 30 \
  --json
python3 scripts/host/hjpeg_host.py status --base-addr 0xa0000000 --json
python3 scripts/host/hjpeg_host.py validate-jpeg output.jpg \
  --width WIDTH \
  --height HEIGHT \
  --restart-interval RESTART_INTERVAL \
  --check-chroma-mode \
  --chroma-subsample \
  --expect-jfif present \
  --json
```

The device paths and base address may need adjustment for the actual board
image. If the DMA driver exposes ioctls or descriptor queues instead of simple
byte-stream device files, add a new backend around the existing packing,
register access, and validation helpers. Omit `--chroma-subsample` for a 4:4:4
run. Pass `--no-jfif` to `run-stream-devices` and
`--expect-jfif absent` to standalone validation if APP0 emission is disabled.

Hardware completion evidence should include:

- The board was programmed with the generated bitstream.
- Status is idle before and after the transfer.
- No protocol error is reported for a valid frame.
- Captured output starts with SOI and ends with EOI.
- Captured output contains no trailing bytes after the first EOI.
- `validate-jpeg` confirms the expected dimensions, 8-bit SOF0 sample
  precision, exactly one SOF0 and one SOS segment, three-component SOF0 frame
  shape, DQT/DHT presence, non-empty entropy-coded scan data, SOF0/SOS component
  ID order, exact SOS component coverage, baseline SOS spectral fields, 8-bit
  DQT precision, exact DQT table set, exact DQT/DHT segment counts, standard
  DC/AC Huffman table set, SOF0 component sampling factors, chroma mode, DRI
  restart interval, exact RST marker count for the parsed MCU count, JFIF APP0
  signature presence, and valid DQT/DHT table references from SOF0/SOS, with
  the expected baseline marker order through SOS and EOI.
- JSON evidence records the bitstream/XSA/report hashes, PPM/RGB input hashes,
  AXI-Lite target, encoder configuration, host capture limits, status
  checkpoints, output JPEG hash, scan payload length, SOF0/SOS marker counts,
  SOF0 sample precision, SOF0/SOS component ID order, SOS component coverage,
  SOS spectral fields, DQT precision, DQT table set, DQT/DHT segment counts,
  component sampling factors, marker counts, parsed marker sequence, stuffed
  entropy `0xff` byte count, JFIF APP0 signature count, restart interval and
  RST sequence evidence, DQT/DHT table IDs and payload hashes, Huffman table
  set, SOS table selectors, chroma mode, JFIF evidence, decoder command,
  decoder timeout, and bounded decoder stdout/stderr.
- A standard JPEG decoder opens the result.
- A non-flat/color image decodes into recognizable visual content.

The `run-stream-devices` host helper now checks and records AXI-Lite status
after configuration, before streaming RGB input, and after validating the
captured JPEG. It fails if the encoder reports `busy` or `protocol_error` at
any of those points, or if the captured JPEG metadata contradicts the configured
restart interval, chroma mode, or JFIF setting. It also rejects trailing bytes
already returned after the first JPEG EOI instead of writing a truncated
artifact. `make-test-ppm` can generate a deterministic non-flat/color P6 PPM
fixture for repeatable visual checks when no external image is available; PPM
JSON evidence records channel min/max values plus non-flat/color flags. Pass
`--decoder-command 'magick identify {jpeg}'` or an equivalent installed decoder
command to `validate-jpeg` or `run-stream-devices` when you want the standard
decoder-open check captured in JSON evidence. Use
`--decoder-timeout-seconds` to bound that subprocess; the default is 30
seconds. Successful decoder stdout/stderr are captured in bounded JSON fields,
which is useful for commands that print decoded dimensions.

## Known Blockers And Bottlenecks

- KV260 hardware access was unavailable. This blocks final completion.
- KV260 board execution is not proven. The Vivado flow now builds a bitstream
  and XSA and passes post-synthesis/post-implementation report gates, but no
  transfer has been run through AXI DMA on real hardware.
- Chisel/Verilator frame-level tests are not instant. A focused AXI wrapper
  frame-level spec recently took about 76 seconds.
- On Windows, avoid running multiple sbt commands in parallel; the launcher can
  collide on boot locks and named pipes.
- Current decoded-color checks are good for broad recognizability but should
  not be used as exact color-lane invariants. Prefer stage-level checks or
  wrapper-vs-core byte equivalence for precise lane/protocol claims.
- `HjpegCoreSpec` includes a small 16x16 4:4:4 cycle-budget regression using
  the existing ChiselSim frame driver. This is a local performance drift guard,
  not proof of KV260 hardware throughput.

## Suggested Next Work

If the new PC has Vivado:

1. Run `sbt test` and the Python helper tests to establish a software baseline.
2. Regenerate `generated-kv260-axi-lite-top/`.
3. Run IP packaging.
4. Run block design creation and confirm `validate_bd_design`.
5. Run bitstream/XSA generation.
6. Run `check_reports.py` with `--artifact`, timing/utilization/DRC,
   route-status, clock-utilization report paths, `--hold-timing` for
   post-implementation timing, and `--json`.
7. Save the report/artifact JSON evidence, then move to KV260 board validation.

If the new PC has KV260 access too:

1. Use the bitstream/XSA from the Vivado flow.
2. Confirm the AXI-Lite base address and DMA device model.
3. Run a small PPM through `hjpeg_host.py` using the JSON evidence options.
4. Validate the captured JPEG with `--json`, marker/chroma/JFIF/restart
   expectations, a standard decoder command, and a decoder timeout.
5. Save the command JSON records and enough output evidence to update
   `docs/kv260-bringup.md`.

If the new PC does not have Vivado or hardware:

1. Expand simulator coverage for backpressure and longer frames.
2. Add more non-flat/color image regressions at frame level.
3. Broaden performance-oriented cycle checks beyond the current small
   `HjpegCoreSpec` regression.
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
