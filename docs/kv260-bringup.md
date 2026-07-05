# KV260 Bring-Up Checklist

This checklist defines the evidence needed before `hjpeg` can be called a
complete KV260 hardware JPEG encoder. Run it on a machine with Vivado and a
KV260 board image that exposes the AXI DMA and AXI-Lite address map.

## 1. Generate RTL

```sh
sbt 'runMain hjpeg.ElaborateKv260AxiLiteTop'
```

Expected evidence:

- `generated-kv260-axi-lite-top/filelist.f`
- `generated-kv260-axi-lite-top/HjpegKv260AxiLiteTop.sv`

## 2. Synthesis Smoke Check

```sh
vivado -mode batch -source scripts/vivado/synth_kv260_axi_lite.tcl
python3 scripts/vivado/check_reports.py \
  --artifact build/vivado/hjpeg-kv260-axi-lite/post_synth.dcp \
  --timing build/vivado/hjpeg-kv260-axi-lite/post_synth_timing_summary.rpt \
  --utilization build/vivado/hjpeg-kv260-axi-lite/post_synth_utilization.rpt \
  --json
```

Expected evidence:

- `build/vivado/hjpeg-kv260-axi-lite/post_synth.dcp`
- `build/vivado/hjpeg-kv260-axi-lite/post_synth_timing_summary.rpt`
- `build/vivado/hjpeg-kv260-axi-lite/post_synth_utilization.rpt`
- `check_reports.py` records the checkpoint/report hashes and passes the
  requested timing/utilization gates.

## 3. Package IP

```sh
vivado -mode batch -source scripts/vivado/package_kv260_axi_lite_ip.tcl
```

Expected evidence:

- `build/vivado/ip_repo/hjpeg_kv260_axi_lite_1_0/component.xml`
- Vivado IP packager completes without critical warnings about unmapped clock,
  reset, AXI-Lite, or AXI-stream interfaces.

## 4. Create Block Design

```sh
vivado -mode batch -source scripts/vivado/create_kv260_block_design.tcl
```

Expected evidence:

- `build/vivado/hjpeg-kv260-bd/hjpeg_kv260_bd.xpr`
- Address assignment completes.
- `validate_bd_design` completes successfully.
- The generated HDL wrapper is added to the project and compile order is
  updated.
- The design contains Zynq UltraScale+ PS, AXI DMA, SmartConnect, reset logic,
  interrupt concat, and one `hjpeg_kv260_axi_lite` instance.

## 5. Build Bitstream and XSA

```sh
vivado -mode batch -source scripts/vivado/build_kv260_bitstream.tcl
python3 scripts/vivado/check_reports.py \
  --artifact build/vivado/hjpeg-kv260-artifacts/hjpeg_kv260.bit \
  --artifact build/vivado/hjpeg-kv260-artifacts/hjpeg_kv260.xsa \
  --artifact build/vivado/hjpeg-kv260-artifacts/post_impl.dcp \
  --timing build/vivado/hjpeg-kv260-artifacts/post_synth_timing_summary.rpt \
  --timing build/vivado/hjpeg-kv260-artifacts/post_impl_timing_summary.rpt \
  --hold-timing build/vivado/hjpeg-kv260-artifacts/post_impl_timing_summary.rpt \
  --utilization build/vivado/hjpeg-kv260-artifacts/post_synth_utilization.rpt \
  --utilization build/vivado/hjpeg-kv260-artifacts/post_impl_utilization.rpt \
  --drc build/vivado/hjpeg-kv260-artifacts/post_impl_drc.rpt \
  --route-status build/vivado/hjpeg-kv260-artifacts/post_impl_route_status.rpt \
  --clock-utilization build/vivado/hjpeg-kv260-artifacts/post_impl_clock_utilization.rpt \
  --clock-period-ns 10.0 \
  --json
```

The bitstream script accepts optional project directory, artifact directory, and
Vivado job-count arguments after `-tclargs`; use a positive integer for the job
count. Vivado scripts reject extra positional `-tclargs` so build automation
does not silently ignore misspelled or misplaced arguments.

Expected evidence:

- `build/vivado/hjpeg-kv260-artifacts/hjpeg_kv260.bit`
- `build/vivado/hjpeg-kv260-artifacts/hjpeg_kv260.xsa`
- `build/vivado/hjpeg-kv260-artifacts/post_impl.dcp`
- `post_synth_utilization.rpt`
- `post_synth_timing_summary.rpt`
- `post_impl_utilization.rpt`
- `post_impl_timing_summary.rpt`
- `post_impl_drc.rpt`
- `post_impl_route_status.rpt`
- `post_impl_clock_utilization.rpt`

Pass criteria:

- Synthesis and implementation finish successfully.
- The expected bitstream, XSA, and post-implementation checkpoint artifacts
  exist and are recorded in the JSON evidence.
- Post-implementation timing has nonnegative setup WNS and hold WHS for the
  target clock.
- Post-implementation DRC, route status, and clock utilization reports are
  saved with the bitstream artifacts for implementation/floorplan review.
- The DRC report has no Error or Critical Warning violations, and route status
  reports zero unrouted nets and zero routing errors.
- Resource use leaves enough headroom for the intended KV260 platform shell.
- `check_reports.py` exits successfully for the generated timing, utilization,
  DRC, route-status, and clock-utilization reports, with hold timing gated on
  the post-implementation timing report.
- The JSON evidence records artifact/report paths, byte lengths, SHA-256
  hashes, target clock period/frequency, parsed setup WNS and hold WHS values,
  utilization rows, thresholds, DRC violations, route-status counts, required
  clock-utilization report hashes, and pass/fail state. Missing, non-file, or
  unparseable reports are recorded as structured JSON failures. Timing
  thresholds, utilization threshold, and target clock period values must be
  finite. The target clock period must be positive, and the utilization
  threshold must be nonnegative.

Latest local Vivado 2026.1 evidence:

- `build_kv260_bitstream.tcl` completed and wrote `hjpeg_kv260.bit`,
  `hjpeg_kv260.xsa`, and `post_impl.dcp`.
- `check_reports.py` passed on post-synthesis and post-implementation timing
  and utilization reports.
- Latest post-implementation timing is setup WNS `+0.131 ns` and hold WHS
  `+0.010 ns` at the 100 MHz target clock.
- Latest post-implementation utilization is approximately 50,662 CLB LUTs
  (43.26%), 25,619 LUTRAMs (44.48%), 2 BRAM tiles (1.39%), and 17 DSPs
  (1.36%).

## 6. Prepare Host Input

```sh
python3 scripts/host/hjpeg_host.py make-test-ppm input.ppm --width WIDTH --height HEIGHT --json
python3 scripts/host/hjpeg_host.py pack-ppm input.ppm input.rgb --json
```

Expected evidence:

- `input.ppm` is a deterministic non-flat/color test pattern, or another known
  binary P6 PPM fixture with recognizable visual content.
- `input.rgb` size is exactly `width * height * 4` bytes: R, G, B, and one
  ignored padding byte per pixel.
- The JSON evidence records input/output paths, dimensions, byte lengths, and
  SHA-256 hashes for the PPM fixture and packed RGB stream, plus PPM
  per-channel min/max values and non-flat/color flags.
- The input image dimensions are within the configured `HjpegConfig` maximums.
  The host helper defaults to the current KV260 top limit of `1920x1080`; use
  `--max-width` and `--max-height` only for a custom elaboration with different
  limits, and keep those values in the saved JSON evidence. `pack-ppm` checks
  these limits from the PPM header before reading the RGB payload.

## 7. Configure and Run Hardware

Program the KV260 with the generated bitstream and load a board image or driver
stack that exposes AXI DMA MM2S/S2MM transfers as byte-stream device files.
Then run:

```sh
python3 scripts/host/hjpeg_host.py run-stream-devices \
  --base-addr 0xa0000000 \
  --tx-device /dev/hjpeg-mm2s \
  --rx-device /dev/hjpeg-s2mm \
  --input-rgb input.rgb \
  --output-jpeg output.jpg \
  --width WIDTH \
  --height HEIGHT \
  --json
python3 scripts/host/hjpeg_host.py status --base-addr 0xa0000000 --json
python3 scripts/host/hjpeg_host.py clear-error --base-addr 0xa0000000 --json
```

Adjust the `--tx-device` and `--rx-device` paths to match the loaded board
image. Drivers that expose AXI DMA through ioctls or descriptor queues need a
small adapter, but should reuse the same packing, register, and validation
helpers.

Expected evidence:

- Status is `idle` before the transfer starts.
- Status returns to `idle` after the transfer completes.
- `protocol_error` is never reported for the valid frame.
- JSON evidence records the AXI-Lite target and encoder configuration used for
  the run, including the frame limits checked by the host helper.
- Standalone `status --json` evidence records the AXI-Lite target, raw status
  word, decoded `busy` and `protocol_error` flags, and text state.
- Standalone `clear-error --json` evidence records the AXI-Lite target and
  control word pulsed when recovering from a sticky protocol fault.
- The captured output starts with SOI and ends with EOI.

The `run-stream-devices` helper checks the AXI-Lite status register after
configuration, immediately before streaming input, and after validating the
captured JPEG. It exits with an error if `busy` or `protocol_error` is set at
any of those points.

## 8. Validate JPEG Output

```sh
python3 scripts/host/hjpeg_host.py validate-jpeg output.jpg --width WIDTH --height HEIGHT
```

If a standard JPEG decoder is available on the host, include it in the helper
run:

```sh
python3 scripts/host/hjpeg_host.py validate-jpeg output.jpg \
  --width WIDTH \
  --height HEIGHT \
  --restart-interval RESTART_INTERVAL \
  --check-chroma-mode \
  --expect-jfif present \
  --quality QUALITY \
  --require-standard-huffman \
  --decoder-command 'magick identify {jpeg}' \
  --decoder-timeout-seconds 30
```

Use `--json` with `make-test-ppm`, `pack-ppm`, `config`, `status`,
`clear-error`, `validate-jpeg`, or `run-stream-devices` when saving evidence for automation or
later comparison.

Expected evidence:

- The helper reports valid nonzero baseline JPEG dimensions, 8-bit SOF0 sample
  precision, three-component SOF0 frame shape, exactly one SOF0 and one SOS
  segment, and the number of entropy-coded scan data bytes, proving the file
  contains an SOS marker with non-empty scan payload. It also records the
  stuffed entropy `0xff` byte count and rejects unsupported header markers,
  non-JFIF or duplicate APP0 markers, unexpected non-RST/non-EOI markers after
  SOS, or trailing bytes after EOI.
- The helper records APP0, DQT, DHT, DRI, and restart-marker counts, plus the
  parsed DRI restart interval when present. It requires exactly two DQT
  segments and four DHT segments for the current encoder contract. The helper
  records DQT table IDs `{0, 1}`, 8-bit DQT precision, DQT/DHT payload byte
  counts and SHA-256 hashes, DHT table class/ID pairs, and SOS component table
  selectors, and rejects zero SOF0 dimensions, non-8-bit or non-three-component
  SOF0 frames, nonstandard DQT/DHT table sets or segment counts, duplicate
  DQT/DHT table definitions, non-8-bit DQT tables, empty, oversized,
  oversubscribed, or invalid baseline DHT tables, zero-valued DQT entries, plus
  SOF0 or SOS references to missing DQT/DHT tables. Pass `--quality` and
  `--require-standard-huffman` to standalone `validate-jpeg` to check
  quality-scaled standard DQT payloads and standard DHT payloads. Pass
  `--restart-interval` to standalone `validate-jpeg` to check the expected DRI
  value and exact RST marker count for the parsed MCU count;
  `run-stream-devices` checks this automatically against the configured
  register value and also checks captured DQT/DHT payloads against the
  configured quality and standard Huffman tables. Host CLI restart interval
  values must be in `0..65535`, and configuration quality values must be in
  `1..100`. The helper records the RST marker sequence and rejects RST markers
  without DRI or sequences that do not increment modulo 8 from RST0.
- The helper rejects unsupported baseline header markers and markers that move
  out of the encoder's expected order: optional APP0/JFIF, DQT, SOF0, DHT,
  optional DRI, SOS, entropy data, then EOI. JSON evidence includes the parsed
  `marker_sequence`.
- The helper records SOF0 component sampling factors and the inferred chroma
  mode, records the parsed MCU count, rejects zero SOF0 sampling factors,
  requires supported 4:4:4 or 4:2:0 sampling factors, and requires SOF0/SOS
  component IDs in `[1, 2, 3]` order with the SOS component list covering the
  same three SOF0 components exactly once and SOS spectral fields `Ss=0`,
  `Se=63`, `Ah/Al=0`. Pass
  `--check-chroma-mode` to standalone `validate-jpeg`; `run-stream-devices`
  checks this automatically against the configured chroma mode.
- The helper records APP0 marker count and JFIF APP0 signature count, rejects
  malformed JFIF APP0 segments, non-JFIF APP0 segments, and more than one APP0
  marker, and can enforce the JFIF signature with `--expect-jfif present` or
  `absent`; `run-stream-devices` checks this automatically against the
  configured JFIF control bit.
- The helper reports the total JPEG byte length and SHA-256 so the captured
  artifact can be matched against saved files and logs.
- For `run-stream-devices`, the helper reports the actual and expected input
  RGB stream byte lengths plus SHA-256 so the output can be tied to the exact
  input payload, and rejects trailing bytes already returned after the first
  JPEG EOI instead of writing a truncated artifact. JSON evidence records the
  host capture limits used for maximum output bytes and RX timeout. RX timeout
  values must be finite and positive when present, and maximum output bytes
  must be positive. Frame dimensions and frame limits must be positive, and
  AXI-Lite base addresses must be nonnegative.
- JSON evidence records host-observed transfer elapsed seconds and derived byte
  rates only when elapsed time is positive. Elapsed-time evidence must be finite
  and nonnegative. Use hardware counters or driver timestamps before making
  final throughput claims.
- For `run-stream-devices --json`, the helper records the AXI-Lite status
  checkpoints enforced after configuration, before transfer, and after
  transfer, including the AXI-Lite target sampled for each checkpoint.
- A standard JPEG decoder can open `output.jpg`; when `--decoder-command` is
  used, that decoder check, command string, timeout, return code, and bounded
  stdout/stderr are part of the JSON evidence, and decoder failure or timeout
  fails validation. Decoder timeout values must be finite and positive.
- The decoded dimensions match the input.
- Visual content is recognizable for non-flat test images.

## Completion Bar

Do not mark the project complete until the repo has current evidence for:

- Full simulator suite passing.
- Vivado IP packaging passing.
- Block design validation passing.
- Bitstream and XSA generation passing.
- Post-implementation timing/resource reports reviewed.
- A real KV260 run producing at least one decodable JPEG through AXI DMA.
- Host-side validation passing for that hardware-produced JPEG.
