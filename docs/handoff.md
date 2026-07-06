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
- Bad `keep` beats are accepted to avoid wedging the stream, but they are not
  fed into the JPEG core.
- Unsupported frames are accepted and drained through TLAST without feeding the
  JPEG core; single-beat and multi-beat discard/recovery paths are covered.
- Frames with incomplete RGB words are drained through TLAST without completing
  a JPEG frame; recovery after clear is covered.
- If the expected final pixel arrives without TLAST, the wrapper completes that
  configured JPEG input frame, flags the protocol error, and drains extra input
  beats until TLAST before a clear pulse permits the next frame.
- The clear pulse resets the sticky flag, wrapper coordinates, and buffered core
  pipeline state so partial frames such as early-TLAST packets cannot
  contaminate the next valid frame.

KV260 top-level input:

- `HjpegKv260Top` and `HjpegKv260AxiLiteTop` expose a 32-bit AXI-stream RGB
  input for AXI DMA compatibility.
- Bits `[7:0]`, `[15:8]`, and `[23:16]` are R, G, and B.
- Bits `[31:24]` are ignored.
- The low three `keep` bits must be set. The fourth `keep` bit is ignored, but
  missing lower RGB `keep` bits are rejected and covered at both KV260 tops.

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

The AXI-Lite wrapper accepts independent AW and W channel handshakes, honors
byte write strobes, and holds read/write responses stable under host
backpressure. Unmapped AXI-Lite reads return zero, and unmapped writes complete
without changing mapped control/status registers.

## Recent Progress

Recent baseline commits before this handoff update, newest first. Use
`git log --oneline` as the source of truth if this list drifts again:

- `7d73e68 fix: preflight duplicate stream endpoints`
- `34895c9 test: cover duplicate stream endpoint cli`
- `2746cf9 fix: reject duplicate stream endpoints`
- `562ab70 fix: record stream device run evidence`
- `fc0251b fix: align vivado complete evidence gate`
- `bf845df fix: verify ppm input match evidence`
- `c632d0a fix: require source input path evidence`
- `4b13809 fix: require captured jpeg path evidence`
- `3a4d142 test: cover floorplan report script`
- `72f0fbd feat: regenerate kv260 floorplan report`
- `4ca5c37 feat: require vivado floorplan evidence`
- `6e50e63 feat: record chiselsim tool versions`
- `1850385 fix: summarize vivado evidence paths`
- `31edfee fix: summarize vivado base address consistency`
- `e81a54e test: check generated vivado evidence via cli`
- `05fe5a2 test: accept generated vivado evidence`
- `7ef93be test: assert run complete evidence flag`
- `84a7e77 fix: verify complete run evidence flag`
- `05e5da1 docs: refresh structured vivado evidence handoff`
- `2f4f78f fix: require structured vivado clock evidence`
- `bbc939e fix: gate vivado clock target evidence`
- `caa6c71 docs: align hardware evidence checks`
- `5c71f34 fix: verify marker count evidence`
- `a2c746f fix: verify restart evidence in run summaries`
- `f5e669b docs: refresh continuation handoff`
- `0520f5e test: cover host restart marker wraparound`
- `8209a72 test: cover restart marker wraparound`
- `34d90aa fix: validate vivado evidence arguments`
- `b30e9c6 fix: validate vivado evidence diagnostics`
- `05bbf6c fix: require complete vivado evidence gate`
- `e67da29 fix: validate complete run evidence diagnostics`
- `caf9177 fix: require explicit jpeg validation evidence`
- `940451c docs: align evidence and padding notes`
- `74053f3 fix: require string axi lite device evidence`
- `cd2eda4 fix: validate vivado record file metadata`
- `0b2a936 test: require hashes in vivado evidence json`
- `65315aa fix: require hashed vivado evidence records`
- `267563b test: cover unsupported axi frame drain`
- `e40bcaa feat: record expected jpeg baseline tables`
- `fb73c32 feat: record expected jpeg marker order`
- `b71efd1 feat: record expected jpeg component shape`
- `947ec29 feat: record expected jpeg marker counts`
- `7d97243 feat: record expected jpeg table hashes`
- `d95c542 feat: record expected rst marker count`
- `11a5b0d test: cover configured axi wrapper equivalence`
- `398378a feat: record run validation expectations`
- `9ea42be feat: record jpeg validation expectations`
- `e63b094 feat: record vivado checked count`
- `dbddfb0 feat: record vivado checker arguments`
- `4c03938 feat: record decoder elapsed evidence`
- `9a92ff6 feat: record decoder output sizes`
- `a37718b feat: record decoder argv evidence`
- `779bf2c feat: record jpeg scan payload hash`
- `3f9d9d2 feat: group jpeg marker count evidence`
- `d92bbce feat: validate jpeg dht table order`
- `b5c1df9 feat: validate jpeg dqt table order`
- `5299dd2 fix: validate jpeg quantization selectors`
- `cc3ef2a fix: validate jpeg sos table selectors`
- `bd3feeb feat: record jfif app0 field evidence`
- `69f47d4 test: cover jfif app0 length validation`
- `4eb8f9f fix: validate jfif app0 fields`
- `a871713 fix: reject malformed jfif app0 markers`
- `00fe4e1 fix: reject zero jpeg sampling factors`
- `654ae18 fix: reject zero jpeg frame dimensions`
- `246a26c fix: reject zero jpeg quantization values`
- `baf6f92 fix: require jfif app0 markers`
- `63ae06c fix: reject unsupported jpeg header markers`
- `5e80087 test: cover core output backpressure`
- `ccb8b6a fix: reject invalid jpeg huffman symbols`
- `9c77d6e fix: reject oversubscribed jpeg huffman tables`
- `c280b98 fix: reject oversized jpeg huffman tables`
- `d93131f fix: reject empty jpeg huffman tables`
- `9d2ebf4 fix: reject duplicate jpeg table definitions`
- `de95eea fix: reject extra vivado script arguments`
- `0c2597b fix: validate vivado bitstream job count`
- `a1619c7 fix: validate host evidence config records`
- `e809d61 fix: validate jpeg expectations before io`
- `0dea2ce fix: validate host run config before io`
- `d48e990 fix: validate host jpeg table payloads`
- `3292d43 fix: reject empty vivado evidence files`
- `f56dedd fix: reject empty vivado packaging filelist`
- `e3b3b7f test: verify vivado block design handoff`
- `9168c8d docs: require vivado checkpoint evidence`
- `9e11948 fix: check ppm limits before payload`
- `855aa2f fix: reject invalid host dimensions`
- `7302552 fix: reject invalid host config ranges`
- `68138d1 fix: reject invalid host cli limits`
- `ec235dd fix: validate host capture limits`
- `e636357 fix: reject invalid vivado thresholds`
- `edc8706 docs: clarify finite host timeouts`
- `26b5229 fix: reject nonfinite host timeouts`
- `9167ec7 fix: reject nonfinite host transfer evidence`
- `61c11ed test: cover positive host transfer rates`
- `0641af7 fix: reject negative host transfer evidence`
- `de82ce2 test: cover zero elapsed host evidence`
- `1d721a8 test: exhaust host control word cases`
- `596c331 test: cover host control word helper`
- `1bf4dd3 refactor: share host control word helper`
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
fragile decoded-color assertions. A second equivalence regression covers the
configured 4:2:0 path with restart markers and JFIF APP0 disabled.

An attempted decoded-color half-frame assertion was too strict for the current
quality/color path. Use decoded-image checks for broad recognizability, but use
stage or wrapper equivalence tests for exact protocol/lane invariants.
`HjpegCoreSpec` now includes broad decoded-image regressions for both a
left/right luma split and a left-red/right-blue non-flat color split.

Recent host/Vivado helper work made the evidence path machine-readable. The
host helper now supports JSON evidence for `make-test-ppm`, `pack-ppm`,
`config`, `status`, `clear-error`, `validate-jpeg`, and `run-stream-devices`.
`pack-ppm` checks configured frame limits immediately after parsing the PPM
header, before reading the RGB payload.
Standalone status evidence records the AXI-Lite target along with the raw and
decoded status word. Clear-error evidence records the AXI-Lite target and
control word used to pulse sticky fault recovery. Configuration and run
evidence record the encoder control word in numeric and hex form, tying the
chroma/JFIF/clear-error flags back to the AXI-Lite write value. Host register
writes and JSON evidence now share the same `control_value` helper, which has
table-driven coverage for all clear-error/chroma/JFIF bit combinations. The run evidence
ties together the input RGB stream hash, output JPEG hash, AXI-Lite target,
encoder configuration, expected input byte length and match result, status
checkpoints, host-observed transfer elapsed seconds and derived byte rates when
elapsed time is positive, optional decoder command, decoder timeout, and
bounded decoder stdout/stderr. Non-finite or negative elapsed time is rejected
as invalid evidence. RX and decoder timeout values must be finite and positive
when present, and maximum output bytes must be positive. The CLI rejects invalid
capture limits and decoder timeout values
during argument parsing before device I/O or decoder subprocess setup. It also
rejects nonpositive frame dimensions and limits, negative AXI-Lite base
addresses, and out-of-range quality and restart interval values before AXI-Lite
mapping or stream setup. Host JPEG validation now checks more than dimensions:
it requires DQT and DHT
markers, records DQT/DHT table IDs and SOS component table selectors, rejects
dangling table references, records APP0/DQT/DHT/DRI/RST marker counts, records
parsed JFIF APP0 version/density/thumbnail fields, parses DRI restart intervals,
requires exactly one SOF0 and one SOS segment, requires 8-bit three-component
SOF0 shape with nonzero dimensions, requires SOS components to match SOF0
exactly, requires SOF0/SOS component IDs in `[1, 2, 3]` order, requires SOF0
quantization table selectors to be Y `0` and Cb/Cr `1`, requires SOS table
selectors to be Y `0/0` and Cb/Cr `1/1`, requires baseline SOS spectral fields
`0/63/0`, records SOF0 component sampling factors and MCU count, requires
supported 4:4:4 or 4:2:0 sampling, rejects zero SOF0 sampling
factors, requires standard DQT table IDs `{0, 1}` in table order `[0, 1]`, DHT
table order DC0, DC1, AC0, AC1, and 8-bit DQT precision, records DQT/DHT payload
byte counts and SHA-256 hashes,
requires exact DQT/DHT segment counts, requires the standard DC/AC Huffman table
set, rejects duplicate DQT/DHT table definitions, records parsed marker
sequence, grouped marker counts, unstuffed scan-data SHA-256, stuffed entropy
`0xff` byte count, RST marker sequence, and JFIF APP0 signature count, requires
JFIF APP0 fixed fields to match the encoder baseline,
rejects empty, oversized, oversubscribed, or invalid baseline DHT tables,
zero-valued DQT entries, unsupported header markers, malformed, non-JFIF, or
duplicate APP0 markers, RST markers without DRI, out-of-sequence RST markers,
unexpected non-RST/non-EOI markers after SOS, and trailing bytes after EOI, and
can enforce expected restart interval, exact RST marker count for the parsed MCU
count, chroma mode, JFIF APP0 signature presence, quality-matched standard DQT
payloads, and standard DHT payloads. Standalone validation JSON records the
requested dimensions and optional restart/chroma/JFIF/quality/Huffman
expectations that were enforced, including expected marker counts, the derived
expected RST marker count, and expected DQT/DHT payload hashes when table checks
are enabled.
The parsed MCU count comes from SOF0 sampling factors, so 4:2:0 padded frames
use 16x16 MCU geometry when deriving expected RST marker counts and sequences.
`run-stream-devices` enforces those expectations automatically from the
configured AXI-Lite control fields and quality setting and records the same
expectation object, including expected marker counts, expected RST marker count,
and expected table hashes, in run JSON evidence.

The host helper defaults input-prep and hardware-run dimensions to the current
KV260 top's `1920x1080` limit. Use `--max-width` and `--max-height` only for a
custom RTL elaboration with different `HjpegConfig` frame limits.

The Vivado report checker supports `--json` plus repeated `--artifact`
arguments so the bitstream, XSA, timing reports, utilization reports, DRC
reports, route-status reports, clock-utilization reports, floorplan reports, and address-map
reports can be recorded with paths, resolved paths, byte lengths, hashes, target clock
period/frequency, parsed setup WNS and hold WHS, utilization rows, DRC
violations, route-status counts, thresholds, parsed address-map AXI-Lite
aperture base/high addresses and byte ranges for `hjpeg_0/s_axi_lite` and
`axi_dma_0/S_AXI_LITE`, duplicate/missing/overlapping address-map interface
checks, requested input path lists and gate values, checked report/artifact
count, per-category checked counts, required evidence category presence,
present and missing category names, failing category names, per-category
passing/failing counts, required `.bit`/`.xsa`/`.dcp` artifact suffix presence,
present and missing required suffix names, failing required suffix names,
required artifact filename presence for `hjpeg_kv260.bit`, `hjpeg_kv260.xsa`,
and `post_impl.dcp`, address-map filename presence for
`hjpeg_kv260_address_map.rpt`, required report filename presence for
`post_synth_timing_summary.rpt`, `post_impl_timing_summary.rpt`,
`post_synth_utilization.rpt`, `post_impl_utilization.rpt`,
`post_impl_drc.rpt`, `post_impl_route_status.rpt`, and
`post_impl_clock_utilization.rpt`, `post_impl_floorplan.rpt`,
present/missing/failing filename names,
required suffix/filename passing/failing counts, aggregate pass/fail counts,
required/present/missing category, suffix, artifact-filename,
address-map-filename, and report-filename counts, diagnostic failure count,
checked/passed/failed path lists, a `diagnostic_summary` that checks aggregate
count/path/category consistency, complete-evidence required/missing/failing
lists, and pass/fail state.
Complete Vivado flow evidence should have `all_required_present` and
`all_required_suffixes_present` true, with no failing records in the required
evidence categories or required `.bit`/`.xsa`/`.dcp` artifact suffixes, and with
the named artifacts `hjpeg_kv260.bit`, `hjpeg_kv260.xsa`, and `post_impl.dcp`
present and passing, plus the named address-map report
`hjpeg_kv260_address_map.rpt` and the named timing/utilization/implementation
and floorplan reports. Complete Vivado evidence also requires a finite positive clock target:
top-level `clock_period_ns` and `clock_frequency_mhz` must agree, the structured
`clock_target` record must carry finite/positive/matching flags, and both
`clock_target.valid` and top-level `clock_target_valid` must be strict JSON
booleans set to true. The host saved-run checker rejects Vivado evidence whose
structured clock target is missing, tampered, or inconsistent with the
top-level clock fields. The Vivado checker's complete-evidence flag also gates
on a valid diagnostic summary, required route-status counts present and zero,
address-map hexadecimal fields matching parsed numeric addresses, nonempty
path/resolved-path file metadata plus SHA-256 hashes on passing required
records, and positive floorplan placed-cell evidence.
`all_required_present` requires at least one passing record in each required
category, not just a requested input path. Complete Vivado evidence counts only
records whose `passed` field is an actual JSON boolean `true`. Missing,
non-file, or unparseable reports are recorded as structured JSON failures
instead of aborting the transcript. Vivado numeric transcript fields such as
address-map base/high addresses, clock period/frequency, evidence-category
counts, summary counts, and route-status counts must be actual JSON numbers,
not booleans. Full bitstream gates should pass
`--require-complete-evidence`; partial post-synthesis checks can omit it.
Requested artifacts, clock-utilization reports, floorplan reports, and address-map reports must be
non-empty. Address-map reports must include parseable base addresses for both
the HJPEG AXI-Lite control aperture and the AXI DMA control aperture, and those
control apertures must be unique and non-overlapping when high addresses are
reported. Use
`--hold-timing` for post-implementation timing reports; post-synthesis hold can
be negative before implementation fixes it. The utilization parser handles Vivado's
`Prohibited` column and records hard-system rows such as `PS8` without gating
them against the fabric utilization threshold. The DRC gate fails Error and
Critical Warning violations, the route-status gate fails nonzero unrouted or
routing-error counts, positive floorplan placed-cell counts, and clock-utilization/address-map/floorplan reports are required
and recorded as review evidence. The checker rejects non-finite timing thresholds,
non-finite clock periods, nonpositive clock periods, non-finite utilization
thresholds, and negative utilization thresholds before JSON evidence can record
meaningless gate values.

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
python scripts/host/hjpeg_host_test.py
python scripts/vivado/check_reports_test.py
python -m py_compile scripts/host/hjpeg_host.py scripts/vivado/check_reports.py
git diff --check
vivado -mode batch -source scripts/vivado/package_kv260_axi_lite_ip.tcl
vivado -mode batch -source scripts/vivado/create_kv260_block_design.tcl
vivado -mode batch -source scripts/vivado/synth_kv260_axi_lite.tcl
python scripts/vivado/check_reports.py \
  --artifact build/vivado/hjpeg-kv260-axi-lite/post_synth.dcp \
  --timing build/vivado/hjpeg-kv260-axi-lite/post_synth_timing_summary.rpt \
  --utilization build/vivado/hjpeg-kv260-axi-lite/post_synth_utilization.rpt \
  --json
vivado -mode batch -source scripts/vivado/build_kv260_bitstream.tcl
python scripts/vivado/check_reports.py \
  --artifact build/vivado/hjpeg-kv260-artifacts/hjpeg_kv260.bit \
  --artifact build/vivado/hjpeg-kv260-artifacts/hjpeg_kv260.xsa \
  --artifact build/vivado/hjpeg-kv260-artifacts/post_impl.dcp \
  --address-map build/vivado/hjpeg-kv260-bd/hjpeg_kv260_address_map.rpt \
  --timing build/vivado/hjpeg-kv260-artifacts/post_synth_timing_summary.rpt \
  --timing build/vivado/hjpeg-kv260-artifacts/post_impl_timing_summary.rpt \
  --hold-timing build/vivado/hjpeg-kv260-artifacts/post_impl_timing_summary.rpt \
  --utilization build/vivado/hjpeg-kv260-artifacts/post_synth_utilization.rpt \
  --utilization build/vivado/hjpeg-kv260-artifacts/post_impl_utilization.rpt \
  --drc build/vivado/hjpeg-kv260-artifacts/post_impl_drc.rpt \
  --route-status build/vivado/hjpeg-kv260-artifacts/post_impl_route_status.rpt \
  --clock-utilization build/vivado/hjpeg-kv260-artifacts/post_impl_clock_utilization.rpt \
  --floorplan build/vivado/hjpeg-kv260-artifacts/post_impl_floorplan.rpt \
  --require-complete-evidence \
  --json
```

Most recent focused verification:

```sh
python scripts/host/hjpeg_host_test.py      # 204 tests
python scripts/vivado/check_reports_test.py # 52 tests
git diff --check                            # CRLF warnings only
```

Known local limitations:

- Run `python3 scripts/dev/check_chiselsim_env.py` before ChiselSim-backed
  tests on Windows or a newly provisioned machine. It reports the detected
  `make`, `sh`, and `verilator` paths, first-line `--version` output, and
  relevant `SHELL`/`MAKESHELL` overrides, flags the known Windows/MSYS
  incompatibility below, and exits nonzero when simulator-backed tests are
  expected to fail before RTL execution. Use `--json` when saving this as
  handoff evidence.
- Full ChiselSim tests on Windows/MSYS currently fail before simulation or
  harness compilation because svsim emits Windows-style Makefile/file-list paths
  while MSYS `make`, Verilator, and the MinGW/UCRT C++ toolchain consume parts
  of the flow as POSIX paths. A local shim proved the first `make clean` failure
  can be bypassed, but the generated harness then hit path normalization issues
  and a missing POSIX `getline` symbol in the MinGW build. Forcing `SHELL` or
  `MAKESHELL` to `cmd.exe` is also not a reliable workaround because generated
  svsim Makefiles mix Windows clean rules with POSIX fragments such as
  `$(shell pwd)` and replay pipelines.
- Latest focused attempt on this Windows/MSYS setup:
  `sbt 'testOnly hjpeg.HjpegAxiStreamCoreSpec'` compiled the updated spec, then
  all simulations failed at svsim `make clean` because the generated Makefile
  ran `for /f "delims=" ...` under `/bin/sh`. This matches the known simulator
  path/shell incompatibility above; it does not validate the new AXI wrapper
  regression until run on a compatible simulator setup.
- The current block-design Vivado reports pass the default 100 MHz
  setup/hold/utilization gates. The standalone synthesis smoke evidence hashes
  `post_synth.dcp`. Latest block-design artifact reports include the bitstream,
  XSA, and post-implementation checkpoint, and show post-synthesis setup WNS
  `+0.807 ns` and post-implementation setup WNS `+0.131 ns`;
  post-implementation hold WHS is `+0.010 ns`. Post-implementation utilization is approximately
  50,662 CLB LUTs (43.26%), 25,619 LUTRAMs (44.48%), 2 BRAM tiles (1.39%), and
  17 DSPs (1.36%). `write_kv260_floorplan_report.tcl` regenerated
  `post_impl_floorplan.rpt` from the existing implemented project with 107,114
  placed primitive cells, and the complete Vivado evidence checker passed with
  12 checked records including the floorplan report.

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
vivado -mode batch -source scripts/vivado/write_kv260_floorplan_report.tcl # only needed for older implemented builds missing post_impl_floorplan.rpt
python3 scripts/vivado/check_reports.py \
  --artifact build/vivado/hjpeg-kv260-artifacts/hjpeg_kv260.bit \
  --artifact build/vivado/hjpeg-kv260-artifacts/hjpeg_kv260.xsa \
  --artifact build/vivado/hjpeg-kv260-artifacts/post_impl.dcp \
  --address-map build/vivado/hjpeg-kv260-bd/hjpeg_kv260_address_map.rpt \
  --timing build/vivado/hjpeg-kv260-artifacts/post_synth_timing_summary.rpt \
  --timing build/vivado/hjpeg-kv260-artifacts/post_impl_timing_summary.rpt \
  --hold-timing build/vivado/hjpeg-kv260-artifacts/post_impl_timing_summary.rpt \
  --utilization build/vivado/hjpeg-kv260-artifacts/post_synth_utilization.rpt \
  --utilization build/vivado/hjpeg-kv260-artifacts/post_impl_utilization.rpt \
  --drc build/vivado/hjpeg-kv260-artifacts/post_impl_drc.rpt \
  --route-status build/vivado/hjpeg-kv260-artifacts/post_impl_route_status.rpt \
  --clock-utilization build/vivado/hjpeg-kv260-artifacts/post_impl_clock_utilization.rpt \
  --floorplan build/vivado/hjpeg-kv260-artifacts/post_impl_floorplan.rpt \
  --require-complete-evidence \
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
  --input-ppm input.ppm \
  --output-jpeg output.jpg \
  --width WIDTH \
  --height HEIGHT \
  --restart-interval RESTART_INTERVAL \
  --chroma-subsample \
  --decoder-command 'magick identify {jpeg}' \
  --decoder-timeout-seconds 30 \
  --require-complete-evidence \
  --json > run.json
python3 scripts/host/hjpeg_host.py status --base-addr 0xa0000000 --json
python3 scripts/host/hjpeg_host.py check-run-evidence run.json \
  --vivado-evidence vivado.json \
  --json
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
- JSON evidence records the bitstream/XSA/report SHA-256 hex hashes, PPM/RGB
  input SHA-256 hex hashes, AXI-Lite target, encoder configuration, host capture
  limits, status
  checkpoints, captured output JPEG path and resolved path, output JPEG hash, scan payload length, SOF0/SOS marker counts,
  unstuffed scan-data SHA-256, SOF0 sample precision, SOF0/SOS component ID
  order, SOS component coverage, SOS spectral fields, DQT precision, DQT table
  set, DQT/DHT segment counts, component sampling factors, marker counts,
  parsed marker sequence, stuffed entropy `0xff` byte count, JFIF APP0
  signature count, restart interval and RST sequence evidence, DQT/DHT table
  IDs and payload hashes, Huffman table set, SOS table selectors, chroma mode,
  JFIF evidence, standalone validation expectations including expected marker
  counts, expected marker order through SOS and EOI, expected RST marker count
  and sequence, expected SOF0 precision and component count, expected SOF0/SOS
  component selectors, expected SOS spectral fields, expected minimum scan-data
  length, expected DQT/DHT table order, expected JFIF APP0 baseline fields when
  JFIF is required, expected chroma mode when checked, and expected DQT/DHT
  payload hashes, hardware-run evidence-presence bits, ordered required
  evidence group names, ordered recorded check names, evidence/check counts,
  present and missing evidence group names, passing and failing check names,
  consolidated run-check booleans,
  `all_recorded_checks_passed`, `complete_hardware_run_evidence` with
  an explicit `jpeg_validation_passed` flag, hashed output JPEG, source PPM,
  positive transfer timing with finite positive derived input/output byte rates,
  and passing decoder evidence required,
  decoder command, resolved decoder argv, decoder timeout, decoder elapsed
  seconds, decoder stdout/stderr, captured decoder output lengths, and decoder
  output capture limit. Decoder summary evidence requires the command
  string, resolved argv, positive timeout, nonnegative elapsed time, zero return
  code, stdout/stderr strings with matching captured lengths, output lengths
  within the positive capture limit, non-truncated decoder output metadata, and
  an argv list matching the command and JPEG path.
  The hardware-run summary requires positive parsed output JPEG dimensions and
  cross-checks frame dimensions across the output JPEG, encoder configuration,
  validation expectations, source PPM, and expected RGB stream byte length, and
  requires a captured JPEG path and resolved path and the parsed marker sequence to begin with
  SOI and end with EOI. It also cross-checks grouped marker
  counts against scalar APP0/JFIF APP0/DQT/SOF0/DHT/SOS/DRI/RST counts,
  verifies RST sequence length against the recorded RST count, and checks
  marker-count/RST expectations when present. Input RGB evidence must include
  a nonempty path and resolved path, positive byte length, a SHA-256 hex hash, a positive expected byte length, and
  a boolean actual-vs-expected length-match flag that matches the recomputed
  result. Stream-device evidence must include
  nonempty TX/RX device paths, resolved identities that match the raw paths, and
  distinct raw and resolved endpoints. Capture configuration evidence must
  include a positive maximum output byte count and either no timeout or a finite
  positive timeout. AXI-Lite target evidence must include a non-empty string
  device path, nonnegative base address, and matching hexadecimal base-address
  text. Encoder configuration evidence must include supported dimensions,
  quality/restart values in range, boolean control flags, and a control word/hex
  string matching those flags.
  Validation expectations evidence must include the baseline shape, marker
  order, marker counts, restart marker count/sequence when applicable, table
  order, SOS spectral fields, and standard-Huffman requirement.
  Source PPM evidence must include a nonempty path and resolved path, file and packed-RGB SHA-256 hex hashes,
  positive dimension-consistent RGB and packed byte lengths, a recomputed input-RGB
  length/hash match with a boolean packed-RGB match flag, and
  non-flat/color image stats. Status evidence must include the detailed
  checkpoint list, matching checkpoint count, expected ordered contexts,
  per-checkpoint AXI-Lite targets matching the run target, zero raw status
  words, and all checkpoints idle with no protocol error or busy state. Summary
  checks recompute checkpoint order, checkpoint target matches, decoded status
  text/flags, and boolean aggregate expected-contexts/idle/error/busy flags
  from the detailed status records. They also recompute
  RGB byte-count matches, PPM-to-input-RGB consistency, and transfer byte rates
  from the saved lengths, hashes, and elapsed time. Required boolean evidence
  fields must be actual JSON booleans. Required numeric evidence fields such as
  byte lengths, dimensions, status words, base addresses, timeouts, elapsed
  seconds, derived rates, and count summaries must be actual JSON numbers;
  booleans are rejected even though Python treats them as integers. The summary
  records the required
  evidence group names, total, present, and missing evidence-group counts,
  recorded check names, total, passing, and failing check counts, missing
  and present evidence group names, and passing and failing check names for
  review.
- A standard JPEG decoder opens the result.
- A non-flat/color image decodes into recognizable visual content.

The `run-stream-devices` host helper now checks and records AXI-Lite status
after configuration, before streaming RGB input, and after validating the
captured JPEG. It fails if the encoder reports `busy` or `protocol_error` at
any of those points, if TX/RX stream-device paths are identical, or if the
captured JPEG metadata contradicts the configured
restart interval, chroma mode, or JFIF setting. Saved-run evidence checking also
cross-checks expected marker order through SOS and terminal EOI against the
parsed JPEG marker sequence, expected minimum scan-data length against the
parsed entropy scan length, expected DQT/DHT table order and SOS spectral fields
against the parsed JPEG, expected SOF0/SOS component records against the
parsed JPEG, expected JFIF APP0 presence and fixed APP0 fields against the
parsed JPEG, the expected chroma mode against the parsed JPEG chroma mode when
chroma checking was requested and expected DQT payload hashes against the parsed
DQT table hashes when quality checking was requested, plus expected DHT table
hashes against parsed DHT table hashes when standard-Huffman checking was
requested. Complete validation evidence requires those expected DQT/DHT hash
records whenever the corresponding quality or standard-Huffman check is
requested. It also rejects trailing bytes already returned after the first JPEG
EOI instead of writing a truncated artifact, and its JSON
evidence records the dimensions, restart interval, chroma mode, JFIF setting,
quality, standard Huffman expectations, status
checkpoint count, actual and expected checkpoint context lists, context-list
match result, and run-level all-idle/any-busy/any-protocol-error summaries that
were checked against the captured JPEG. `make-test-ppm` can generate a deterministic non-flat/color P6 PPM
fixture for repeatable visual checks when no external image is available; PPM
JSON evidence records channel min/max values plus non-flat/color flags.
`run-stream-devices --input-ppm` validates the source PPM dimensions and packed
RGB bytes against the configured frame and `--input-rgb` before device I/O, then
records the PPM stats, PPM-derived packed RGB byte length and SHA-256 hex, and
packed-RGB match result in run JSON evidence. Pass
`--decoder-command 'magick identify {jpeg}'` or an equivalent installed decoder
command to `validate-jpeg` or `run-stream-devices` when you want the standard
decoder-open check captured in JSON evidence. The evidence records the resolved
argv after `{jpeg}` substitution or path appending. Use
`--decoder-timeout-seconds` to bound that subprocess; the default is 30
seconds. Successful decoder elapsed seconds and stdout/stderr are captured in
bounded JSON fields with captured lengths and the configured capture limit,
which is useful for commands that print decoded dimensions.
Use `run-stream-devices --require-complete-evidence` for final board evidence
gates; omit it for partial smoke tests that intentionally skip source PPM or
decoder evidence. Run JSON records whether complete evidence was required and
whether complete evidence was captured, which evidence groups were missing,
plus which complete-evidence checks failed.
Saved run JSON can be checked later with
`check-run-evidence`, which fails on malformed JSON, missing
`hardware_run_summary`, a stored summary that does not match recomputed
evidence, a transcript that did not request complete-evidence gating, diagnostic
missing-evidence or failing-check lists that do not match recomputed evidence,
missing or non-boolean top-level `complete_hardware_run_evidence` gates, failed
recorded checks, or incomplete hardware evidence. The host and Vivado helpers
emit strict JSON for evidence output, and saved run/Vivado evidence files must
be strict JSON; non-standard constants such as `NaN` and `Infinity` are rejected
as malformed evidence. Pass
`--vivado-evidence` with the saved `check_reports.py --json` bitstream evidence
to cross-check the run transcript's AXI-Lite base address against the Vivado
`hjpeg_0/s_axi_lite` address-map entry. The Vivado transcript must have JSON
boolean `true` values for `passed`, `complete_vivado_flow_evidence`,
`complete_vivado_flow_evidence_required`, and
`arguments.require_complete_evidence`, the required `.bit`, `.xsa`,
and `.dcp` artifact suffix evidence true, and the required `hjpeg_kv260.bit`,
`hjpeg_kv260.xsa`, `post_impl.dcp`, `hjpeg_kv260_address_map.rpt`,
`post_synth_timing_summary.rpt`, `post_impl_timing_summary.rpt`,
`post_synth_utilization.rpt`, `post_impl_utilization.rpt`,
`post_impl_drc.rpt`, `post_impl_route_status.rpt`, and
`post_impl_clock_utilization.rpt`, and `post_impl_floorplan.rpt` filename
evidence true, with
`post_impl_timing_summary.rpt` also present as passing hold-timing evidence and
matching missing/failing filename, hold-timing, and suffix lists empty. The
Vivado clock target must include finite positive `clock_period_ns` and
`clock_frequency_mhz` values that match each other, with `clock_target.valid`
and the top-level `clock_target_valid` flag true, and the Vivado checked path
list must match the passed path list. Its top-level
`complete_vivado_flow_evidence` flag and complete-evidence missing/failing lists
must match nested evidence summaries, and its `diagnostic_summary` object must
match the aggregate Vivado fields with `valid` true. Its per-category checked
counts must be positive, sum to the total checked count, and match the
per-category pass/fail totals. The floorplan record must include a positive
placed-cell count. Supplying multiple Vivado evidence files is
allowed only when they agree on the same HJPEG base address.
JSON output includes aggregate checked/pass/fail transcript counts, diagnostic
failure count, checked/passed/failed path lists, summary checked, matched, and
mismatched counts and paths, aggregate evidence group present/missing counts,
aggregate evidence group present/missing names, aggregate recorded/passing/failing
check counts and names, Vivado evidence counts, Vivado checked/passed/failed
evidence path and resolved-path lists, aggregate raw/resolved stream endpoint counts and device
lists, aggregate AXI-Lite target device/base-address counts and lists, parsed
HJPEG base addresses, HJPEG base-address count and consistency flag, aggregate
frame dimensions, encoder configuration values, and validation expectation
values, aggregate status-check context/flag values and host transfer rates,
aggregate capture/input byte-count and source-image values, aggregate JPEG/input
structure/hash values and decoder result values, aggregate JPEG/input artifact
path/resolved-path and decoder-command inventories, plus the recomputed summary,
evidence/check counts, missing evidence groups, and failing check names for each
object-shaped transcript.

## Known Blockers And Bottlenecks

- KV260 hardware access was unavailable. This blocks final completion.
- KV260 board execution is not proven. The Vivado flow now builds a bitstream
  and XSA and passes post-synthesis/post-implementation report gates, but no
  transfer has been run through AXI DMA on real hardware.
- Chisel/Verilator frame-level tests are not instant. A focused AXI wrapper
  frame-level spec recently took about 76 seconds.
- On Windows, avoid running multiple sbt commands in parallel; the launcher can
  collide on boot locks and named pipes.
- On the current Windows/MSYS setup, ChiselSim may fail before simulation when
  `make` invokes `/bin/sh` on a generated Windows `cmd` clean rule containing
  `for /f "delims="`. Treat that as a local simulator toolchain issue when
  `sbt Test/compile` succeeds.
- `HjpegCoreSpec` includes focused byte-equivalence coverage for output
  backpressure on the core output stream. Broader long-frame and throughput
  checks are still useful, but keep them focused because frame-level simulation
  cost grows quickly.
- Current decoded-color checks are good for broad recognizability but should
  not be used as exact color-lane invariants. Prefer stage-level checks or
  wrapper-vs-core byte equivalence for precise lane/protocol claims; the AXI
  wrapper now has exact byte-equivalence coverage for default 4:4:4 and
  configured 4:2:0/restart/no-JFIF frames, and `HjpegCoreSpec` has broad luma
  and red/blue decoded-frame recognizability checks.
- `HjpegCoreSpec` includes a small 16x16 4:4:4 cycle-budget regression using
  the existing ChiselSim frame driver. This is a local performance drift guard,
  not proof of KV260 hardware throughput.
- Restart interval coverage now includes stage-level RTL and host-side JPEG
  validation regressions for RST marker numbering wrapping from RST7 back to
  RST0.

## Suggested Next Work

If the new PC has Vivado:

1. Run `sbt test` and the Python helper tests to establish a software baseline.
2. Regenerate `generated-kv260-axi-lite-top/`.
3. Run IP packaging.
4. Run block design creation and confirm `validate_bd_design` plus the generated
   `build/vivado/hjpeg-kv260-bd/hjpeg_kv260_address_map.rpt`.
5. Run bitstream/XSA generation.
6. Run `check_reports.py` with `--artifact`, `--address-map`,
   timing/utilization/DRC, route-status, clock-utilization, floorplan report paths,
   `--hold-timing` for post-implementation timing, `--require-complete-evidence`,
   and `--json`.
7. Save the report/artifact JSON evidence, then move to KV260 board validation.

If the new PC has KV260 access too:

1. Use the bitstream/XSA from the Vivado flow.
2. Confirm the AXI-Lite base address from the Vivado address-map report and the
   DMA device model.
3. Run a small PPM through `hjpeg_host.py` using the JSON evidence options.
4. Validate the captured JPEG with `--json`, marker/chroma/JFIF/restart
   expectations, a standard decoder command, and a decoder timeout.
5. Use `run-stream-devices --require-complete-evidence` for the final board
   run transcript.
6. Run `check-run-evidence` on the saved run JSON.
7. Save the command JSON records and enough output evidence to update
   `docs/kv260-bringup.md`.

If the new PC does not have Vivado or hardware:

1. Expand simulator coverage for backpressure and longer frames.
2. Add more non-flat/color image regressions at frame level if they cover a new
   shape, chroma mode, or protocol condition.
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
