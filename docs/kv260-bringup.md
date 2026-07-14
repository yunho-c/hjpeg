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
- `build/vivado/hjpeg-kv260-bd/hjpeg_kv260_address_map.rpt`
- Address assignment completes.
- `validate_bd_design` completes successfully.
- The generated HDL wrapper is added to the project and compile order is
  updated.
- The design contains Zynq UltraScale+ PS, AXI DMA, SmartConnect, reset logic,
  interrupt concat, and one `hjpeg_kv260_axi_lite` instance.
- The AXI DMA records a 256-beat MM2S burst size and disabled MM2S
  store-and-forward. SmartConnect adapts requests to the PS HP port.

## 5. Build Bitstream and XSA

```sh
vivado -mode batch -source scripts/vivado/build_kv260_bitstream.tcl
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
  --clock-period-ns 10.0 \
  --require-complete-evidence \
  --json
```

The bitstream script accepts optional project directory, artifact directory, and
Vivado job-count arguments after `-tclargs`; use a positive integer for the job
count. Vivado scripts reject extra positional `-tclargs` so build automation
does not silently ignore misspelled or misplaced arguments.

For an already implemented block-design project that predates
`post_impl_floorplan.rpt`, regenerate only the floorplan evidence with:

```sh
vivado -mode batch -source scripts/vivado/write_kv260_floorplan_report.tcl
```

The floorplan script accepts optional project and artifact directories after
`-tclargs`, reopens the completed `impl_1` run, and writes
`build/vivado/hjpeg-kv260-artifacts/post_impl_floorplan.rpt`.

Expected evidence:

- `build/vivado/hjpeg-kv260-artifacts/hjpeg_kv260.bit`
- `build/vivado/hjpeg-kv260-artifacts/hjpeg_kv260.xsa`
- `build/vivado/hjpeg-kv260-artifacts/post_impl.dcp`
- `build/vivado/hjpeg-kv260-bd/hjpeg_kv260_address_map.rpt`
- `post_synth_utilization.rpt`
- `post_synth_timing_summary.rpt`
- `post_impl_utilization.rpt`
- `post_impl_hierarchical_utilization.rpt` (hierarchy diagnostic; not a
  separate complete-evidence category)
- `post_impl_timing_summary.rpt`
- `post_impl_drc.rpt`
- `post_impl_route_status.rpt`
- `post_impl_clock_utilization.rpt`
- `post_impl_floorplan.rpt`

Pass criteria:

- Synthesis and implementation finish successfully.
- The expected bitstream, XSA, and post-implementation checkpoint artifacts
  exist and are recorded in the JSON evidence.
- Post-implementation timing has nonnegative setup WNS and hold WHS for the
  target clock.
- Post-implementation DRC, route status, clock utilization, and floorplan reports are
  saved with the bitstream artifacts for implementation/floorplan review.
- The DRC report has no Error or Critical Warning violations, and route status
  reports zero unrouted nets and zero routing errors.
- Resource use leaves enough headroom for the intended KV260 platform shell.
- The utilization ceiling applies to independent fabric resources. Preserve
  aggregate physical `CLB` occupancy in the evidence for placement review, but
  do not gate it as a second copy of the LUT/register budgets; use route status,
  DRC, and routed setup/hold timing as the physical-implementation gates.
- `check_reports.py` exits successfully for the generated timing, utilization,
  DRC, route-status, clock-utilization, floorplan, and address-map reports, with
  hold timing gated on the post-implementation timing report.
- The JSON evidence records artifact/report paths, resolved paths, byte lengths, SHA-256 hex
  hashes, target clock period/frequency, parsed setup WNS and hold WHS values,
  utilization rows, thresholds, DRC violations, route-status counts plus the
  required and missing route-status count names, required clock-utilization
  report hashes, floorplan pblock/placed-cell counts, parsed address-map AXI-Lite aperture
  base/high addresses and byte ranges for `hjpeg_0/s_axi_lite` and
  `axi_dma_0/S_AXI_LITE`, duplicate/missing/overlapping address-map interface
  checks, requested input path lists and gate values, checked report/artifact
  count, per-category checked counts, required evidence category presence,
  per-category passing/failing counts, present and missing category names,
  failing category names, required `.bit`/`.xsa`/`.dcp` artifact suffix presence,
  `exists: true`, positive byte lengths, and well-formed SHA-256 hex hashes for
  passing records in every required evidence category,
  present and missing required suffix names, failing required suffix names,
  required artifact filename presence for `hjpeg_kv260.bit`, `hjpeg_kv260.xsa`,
  and `post_impl.dcp`, address-map filename presence for
  `hjpeg_kv260_address_map.rpt`, required report filename presence for
  `post_synth_timing_summary.rpt`, `post_impl_timing_summary.rpt`,
  `post_synth_utilization.rpt`, `post_impl_utilization.rpt`,
  `post_impl_drc.rpt`, `post_impl_route_status.rpt`, and
  `post_impl_clock_utilization.rpt`, and `post_impl_floorplan.rpt`,
  present/missing/failing filename names,
  required suffix/filename passing/failing counts, required/present/missing category,
  suffix, artifact-filename, address-map-filename, and report-filename counts,
  aggregate pass/fail counts, diagnostic failure count, strict JSON integer
  checked/passed/failed inventory counts, checked/passed/failed path lists, a
  `diagnostic_summary` that checks aggregate count/path/category consistency
  with strict JSON integer checked, passing, and failing category counts,
  complete-evidence required/missing/failing lists, and pass/fail state.
  Complete Vivado flow evidence has
  `all_required_present` and `all_required_suffixes_present` true, with no
  failing records in the required evidence categories or required `.bit`/`.xsa`/
  `.dcp` artifact suffixes, and with the named artifacts `hjpeg_kv260.bit`,
  `hjpeg_kv260.xsa`, and `post_impl.dcp` present and passing, plus the named
  address-map report `hjpeg_kv260_address_map.rpt` and the named
  timing/utilization/implementation and floorplan reports. The floorplan record
  must include a positive placed-cell count. The complete-evidence flag also
  requires a valid diagnostic summary, required route-status counts present as
  actual JSON integers equal to zero, address-map hexadecimal fields matching parsed numeric addresses, and
  nonempty path/resolved-path file metadata with resolved paths matching the
  recorded paths plus SHA-256 hashes on passing required records in both
  generated and saved evidence.
  `all_required_present` requires at least one passing record in each required
  category, not just a requested input path. Complete Vivado evidence counts only
  records whose `passed` field is an actual JSON boolean `true`. Missing,
  non-file, or unparseable reports are recorded as structured JSON failures.
  Full bitstream evidence gates should pass
  `--require-complete-evidence`; partial post-synthesis checks can omit it.
  Timing thresholds, utilization threshold, and target clock period values must
  be finite. The target clock period must be positive,
  and the utilization threshold must be nonnegative.

Latest local Vivado 2026.1 evidence:

- `build_kv260_bitstream.tcl` completed and wrote `hjpeg_kv260.bit`,
  `hjpeg_kv260.xsa`, and `post_impl.dcp`.
- `check_reports.py` passed on post-synthesis and post-implementation timing
  and utilization reports.
- Current instrumented post-implementation timing is setup WNS `+0.097 ns` and
  hold WHS `+0.010 ns` at the 100 MHz target clock.
- Current post-implementation utilization is 35,885 CLB LUTs (30.64%), 690
  LUTRAMs (1.20%), 55,074 registers (23.51%), 76 BRAM tiles (52.78%), 127 DSPs
  (10.18%), and 8,224 CLBs (56.17%). The complete twelve-record report set
  passes the default checker and the project's provisional 70% resource
  ceiling.

## 6. Prepare Host Input

```sh
python3 scripts/host/hjpeg_host.py make-test-ppm input.ppm --width WIDTH --height HEIGHT --json
# Add --pattern seeded-random for the entropy-heavy stress fixture.
python3 scripts/host/hjpeg_host.py pack-ppm input.ppm input.rgb --json
```

Expected evidence:

- `input.ppm` is a deterministic non-flat/color test pattern, or another known
  binary P6 PPM fixture with recognizable visual content.
- `input.rgb` size is exactly `width * height * 4` bytes: R, G, B, and one
  ignored padding byte per pixel.
- The JSON evidence records input/output paths, dimensions, byte lengths, and
  SHA-256 hex hashes for the PPM fixture and packed RGB stream, plus PPM
  per-channel min/max values and non-flat/color flags. Run evidence records the
  expected packed RGB byte length and whether the actual input length matched it.
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
  --input-ppm input.ppm \
  --output-jpeg output.jpg \
  --width WIDTH \
  --height HEIGHT \
  --decoder-command 'magick identify {jpeg}' \
  --require-complete-evidence \
  --json > run.json
python3 scripts/host/hjpeg_host.py status --base-addr 0xa0000000 --json
python3 scripts/host/hjpeg_host.py clear-error --base-addr 0xa0000000 --json
python3 scripts/host/hjpeg_host.py check-run-evidence run.json \
  --vivado-evidence vivado.json \
  --json
```

Adjust the `--tx-device` and `--rx-device` paths to match the loaded board
image. Drivers that expose AXI DMA through ioctls or descriptor queues need a
small adapter, but should reuse the same packing, register, and validation
helpers.

If the board has initialized PS clocks and DDR but no usable Linux DMA device,
run the intrusive XSDB/JTAG backend instead:

```sh
xsdb scripts/host/run_kv260_xsdb_dma.tcl \
  build/vivado/hjpeg-kv260-artifacts/hjpeg_kv260.bit \
  input.rgb output.jpg WIDTH HEIGHT QUALITY RESTART_INTERVAL 1 1
python3 scripts/host/hjpeg_host.py validate-jpeg output.jpg \
  --width WIDTH --height HEIGHT --restart-interval RESTART_INTERVAL \
  --chroma-subsample --check-chroma-mode --expect-jfif present \
  --quality QUALITY --require-standard-huffman \
  --decoder-command 'ffmpeg -v error -i {jpeg} -f null -' --json
```

The last two XSDB arguments select 4:2:0 and JFIF. The runner stops Cortex-A53
#0, selects the aggregate APU debug target for physical DDR reads/writes,
programs the PL, uses reserved DDR buffers, checks both DMA status words and
encoder status, verifies the full MM2S length, and writes exactly the S2MM
reported byte count. The APU selection is required when Linux leaves the A53
MMU enabled: Cortex-A53 debug targets otherwise interpret `dow -data` addresses
as virtual and can fault on the reserved physical DDR addresses. The runner
therefore disrupts a running Linux system and is a lab validation path, not a
production driver. The block design configures a 26-bit
DMA length field; the prior 14-bit default could not carry a packed 1080p frame
without an early TLAST. Current defaults reserve `0x60000000..0x63ffffff` for
the largest permitted MM2S input and begin the maximum-length S2MM buffer at
`0x64000000`, so UHD input cannot overlap the output buffer. The runner accepts
optional trailing PL-clock-Hz and maximum-frame-cycle arguments, and
`HJPEG_XSDB_PREFLIGHT_ONLY=1` validates all files, lengths, and DDR ranges
without connecting to hardware. See `4k60-architecture.md` for the exact
3840x2160 q85 commands and 2,500,000-cycle gate.

Expected evidence:

- Status is `idle` before the transfer starts.
- Status returns to `idle` after the transfer completes.
- `protocol_error` is never reported for the valid frame.
- JSON evidence records the AXI-Lite target and encoder configuration used for
  the run, including the frame limits checked by the host helper.
- JSON evidence records an `arguments` object with the requested AXI-Lite
  target, stream endpoints, input/output paths, frame settings, capture limits,
  decoder command, and complete-evidence flag.
- If `--input-ppm` is provided, JSON evidence records the source PPM dimensions,
  SHA-256 hex, non-flat/color stats, PPM-derived packed RGB byte length and
  SHA-256 hex, and that its packed RGB bytes match `--input-rgb`; mismatches fail
  before device I/O.
- Standalone `status --json` evidence records the AXI-Lite target, raw status
  word, decoded `busy` and `protocol_error` flags, and text state.
- Standalone `clear-error --json` evidence records the AXI-Lite target and
  control word pulsed when recovering from a sticky protocol fault.
- `run-stream-devices --json` evidence includes `hardware_run_summary` with
  the ordered required evidence group list, recorded run-check booleans,
  ordered recorded check names, evidence/check counts, missing evidence group
  names, present evidence group names, passing check names, failing check names,
  and `complete_hardware_run_evidence`. A valid final board transcript should have
  `all_recorded_checks_passed` and `complete_hardware_run_evidence` true,
  which requires an explicit `jpeg_validation_passed` flag, a captured output
  JPEG path plus hashes and non-empty scan data,
  `--input-ppm` source evidence with non-flat/color stats, a passing
  `--decoder-command` check, and positive transfer timing with finite positive
  derived input and output byte rates. Decoder evidence must include the command
  string, resolved argv, positive timeout, nonnegative elapsed time, zero return
  code, stdout/stderr strings with matching captured lengths, output lengths
  within the positive capture limit, non-truncated captured output metadata,
  and an argv list matching the command and JPEG path. The summary requires
  positive parsed output JPEG dimensions and cross-checks them against the
  encoder configuration, validation expectations, source PPM dimensions, and expected RGB stream byte
  length, requires the parsed marker sequence to begin with SOI and end with
  EOI, cross-checks grouped marker counts against scalar
  APP0/JFIF APP0/DQT/SOF0/DHT/SOS/DRI/RST counts, verifies RST sequence length
  against the recorded RST count, and checks marker-count/RST expectations when
  present. Input RGB evidence must include a nonempty path and resolved path, positive byte length,
  a SHA-256 hex hash, a positive expected byte length, and a boolean
  actual-vs-expected length-match flag that matches the recomputed result.
  Stream-device evidence must include nonempty TX/RX device paths, resolved
  identities that match the raw paths, and distinct raw and resolved endpoints.
  Capture configuration evidence must include a positive maximum output byte
  count and either no timeout or a finite positive timeout. AXI-Lite target
  evidence must include a non-empty string device path, nonnegative base address,
  and matching hexadecimal base-address text. Encoder configuration evidence
  must include strict JSON integer dimensions matching the JPEG dimensions and
  supported by the frame limits, quality/restart values in range, boolean
  control flags, and a control word/hex string matching those flags. Validation
  expectations evidence must include strict JSON integer dimensions matching the
  JPEG dimensions, the baseline shape, marker order, marker counts, restart
  marker count/sequence when applicable, table order, SOS spectral fields, and
  standard-Huffman requirement. Source PPM evidence must include strict JSON
  integer dimensions matching the JPEG dimensions, a nonempty path and resolved
  path, file and packed-RGB SHA-256 hex hashes,
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
- Use `run-stream-devices --require-complete-evidence` for the final board
  transcript so missing source PPM, decoder, transfer timing, or status evidence
  fails the command. Omit it only for intentional partial smoke tests. Run JSON
  records whether complete evidence was required, whether complete evidence was
  captured, which evidence groups were missing, plus which complete-evidence
  checks failed.
- Saved run JSON can be checked later with `check-run-evidence`, which fails if
  `hardware_run_summary` is missing, `all_recorded_checks_passed` is false, or
  `complete_hardware_run_evidence` is false. The checker recomputes
  `hardware_run_summary` from the saved transcript and fails if the stored
  summary does not match the recomputed evidence, if
  `complete_hardware_run_evidence_required` and
  `arguments.require_complete_evidence` are not JSON boolean `true`, if the
  `arguments` object does not match the recorded run transcript, if the
  top-level `complete_hardware_run_evidence` flag is missing, not a JSON
  boolean, or stale, or if the
  recorded missing-evidence and failing-check diagnostic lists do not match the
  recomputed summary. Aggregate JSON should also report
  `summary_all_checked` and `summary_all_matched` as true for final evidence,
  proving every supplied run transcript had a recomputable summary and every
  recomputed summary matched. The host and Vivado helpers emit strict JSON for evidence
  output, and saved run/Vivado evidence files must be strict JSON; non-standard
  constants such as `NaN` and `Infinity` are rejected as malformed evidence.
  When `--vivado-evidence`
  points at the `check_reports.py --json` output saved from the bitstream build,
  it also extracts the passing `hjpeg_0/s_axi_lite` address-map base address and
  requires that Vivado transcript to have JSON boolean `true` values for
  `passed`, `complete_vivado_flow_evidence`,
  `complete_vivado_flow_evidence_required`, and
  `arguments.require_complete_evidence`, plus required `.bit`,
  `.xsa`, and `.dcp`
  artifact suffix evidence and required `hjpeg_kv260.bit`, `hjpeg_kv260.xsa`, and
  `post_impl.dcp` filename evidence, plus required `hjpeg_kv260_address_map.rpt`
  filename evidence, plus required `post_synth_timing_summary.rpt`,
  `post_impl_timing_summary.rpt`, `post_synth_utilization.rpt`,
  `post_impl_utilization.rpt`, `post_impl_drc.rpt`,
  `post_impl_route_status.rpt`, `post_impl_clock_utilization.rpt`, and
  `post_impl_floorplan.rpt` filename evidence, with
  `post_impl_timing_summary.rpt` also present as passing
  hold-timing evidence and matching missing/failing filename, hold-timing, and
  suffix lists empty, a Vivado `arguments` object that matches the recorded
  artifact, address-map, report, hold-timing, floorplan, clock-period,
  timing-threshold, and utilization-threshold evidence, with timing records
  matched against the deduplicated union of setup and hold timing arguments,
  finite positive
  `clock_period_ns` and
  `clock_frequency_mhz` values that match each other, `clock_target.valid` and
  top-level `clock_target_valid` true, a floorplan record with a positive
  placed-cell count, a required
  evidence-category summary showing every required category present and passing,
  a top-level `complete_vivado_flow_evidence` flag and complete-evidence
  missing/failing lists matching nested evidence summaries,
  zero diagnostic failures and failed paths in the Vivado summary,
  checked paths matching passed paths, a `diagnostic_summary` object that
  matches the aggregate Vivado fields and has `valid` true, aggregate counts and
  path lists that match the nested artifact/report records, positive
  per-category checked counts whose sum matches the total checked count and
  match the per-category pass/fail totals, plus a passing route-status record
  with zero unrouted nets and zero nets with routing errors. Vivado numeric
  transcript fields such as address-map base/high
  addresses, clock period/frequency, evidence-category counts, summary counts,
  and route-status counts must also be actual JSON numbers, not booleans. The
  checker fails if the run transcript's AXI-Lite base address does not match the
  Vivado build evidence or if multiple Vivado evidence files report conflicting
  HJPEG base addresses.
  JSON output includes the aggregate checked/pass/fail
  transcript counts, diagnostic failure count, and checked/passed/failed path
  lists, summary checked, matched, and mismatched counts and paths, aggregate
  evidence group present/missing counts, aggregate evidence group present/missing
  names, aggregate recorded/passing/failing check counts and names, Vivado
  address-map evidence counts, aggregate raw/resolved stream endpoint counts and
  device lists, aggregate AXI-Lite target device/base-address counts and lists,
  aggregate frame dimensions, encoder configuration values, and validation
  expectation values, aggregate status-check context/flag values and host
  transfer rates, aggregate capture/input byte-count and source-image values,
  aggregate JPEG structure/hash values and decoder result values, aggregate
  JPEG/input artifact path/resolved-path and decoder-command inventories, Vivado
  checked/passed/failed evidence path and resolved-path lists,
  parsed HJPEG base addresses, HJPEG base-address count and consistency flag,
  plus the recomputed summary, evidence/check counts, missing evidence groups,
  and failing check names for each object-shaped transcript.
- The captured output starts with SOI and ends with EOI.

The `run-stream-devices` helper checks the AXI-Lite status register after
configuration, immediately before streaming input, and after validating the
captured JPEG. The recorded checkpoint context names are `after configuration`,
`before transfer`, and `after validation`. It exits with an error if `busy` or
`protocol_error` is set at any of those points. It also rejects identical TX/RX
stream-device paths before device I/O.

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
  unstuffed scan-data SHA-256 and stuffed entropy `0xff` byte count and rejects
  unsupported header markers, non-JFIF or duplicate APP0 markers, unexpected
  non-RST/non-EOI markers after SOS, or trailing bytes after EOI.
- The helper records APP0, DQT, DHT, DRI, and restart-marker counts as scalar
  fields and as a grouped `marker_counts` object, plus the parsed DRI restart
  interval when present. It requires exactly two DQT segments and four DHT
  segments for the current encoder contract. The helper records DQT table IDs
  `{0, 1}`, DQT table order `[0, 1]`, DHT table order DC0, DC1, AC0, AC1, 8-bit
  DQT precision, DQT/DHT payload byte counts and SHA-256 hashes, DHT table
  class/ID pairs, and SOS component table selectors, and rejects zero SOF0
  dimensions, non-8-bit or non-three-component
  SOF0 frames, nonstandard DQT/DHT table sets or segment counts, duplicate
  DQT/DHT table definitions, swapped DQT or DHT table order, non-8-bit DQT
  tables, empty, oversized, oversubscribed, or invalid baseline DHT tables,
  zero-valued DQT entries, plus SOF0 or SOS references to missing DQT/DHT
  tables. Pass
  `--quality` and
  `--require-standard-huffman` to standalone `validate-jpeg` to check
  quality-scaled standard DQT payloads and standard DHT payloads. Standalone
  validation JSON evidence records the expected dimensions and optional
  restart/chroma/JFIF/quality/Huffman checks that were enforced, including the
  expected marker counts, derived expected RST marker count, and expected
  DQT/DHT payload hashes when table checks are enabled. Pass
  `--restart-interval` to standalone `validate-jpeg` to check the expected DRI
  value and exact RST marker count for the parsed MCU count;
  the parsed MCU count comes from SOF0 sampling factors, so 4:2:0 padded frames
  use 16x16 MCU geometry for expected RST marker counts and sequences;
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
  same three SOF0 components exactly once, SOF0 quantization table selectors set
  to Y `0` and Cb/Cr `1`, SOS table selectors set to Y `0/0` and Cb/Cr `1/1`,
  and SOS spectral fields `Ss=0`, `Se=63`, `Ah/Al=0`. Pass
  `--check-chroma-mode` to standalone `validate-jpeg`; `run-stream-devices`
  checks this automatically against the configured chroma mode. The JSON
  `validation_expectations` block records these expected SOF0/SOS component
  selectors, baseline spectral fields, marker order through SOS and EOI, RST
  marker sequence, SOF0 precision/component count, minimum scan-data length,
  DQT/DHT table order, expected JFIF APP0 baseline fields when JFIF is
  required, and expected chroma mode alongside the parsed evidence.
- The helper records APP0 marker count, JFIF APP0 signature count, and parsed
  JFIF APP0 version/density/thumbnail fields, requires JFIF APP0 fixed fields
  to match the encoder baseline, rejects malformed JFIF APP0 segments,
  non-JFIF APP0 segments, and more than one APP0 marker, and can enforce the
  JFIF signature with `--expect-jfif present` or `absent`; `run-stream-devices`
  checks this automatically against the configured JFIF control bit.
- The helper reports the total JPEG byte length and SHA-256 hex so the captured
  artifact can be matched against saved files and logs.
- For `run-stream-devices`, the helper reports the actual and expected input
  RGB stream byte lengths plus SHA-256 hex so the output can be tied to the exact
  input payload, and rejects trailing bytes already returned after the first
  JPEG EOI instead of writing a truncated artifact. JSON evidence records the
  validation expectations enforced from the configured dimensions, restart
  interval, expected marker counts, derived expected RST marker count, chroma
  mode, JFIF setting, quality, standard Huffman contract, expected marker
  order, expected minimum scan-data length, expected table order, expected SOS
  spectral fields, expected SOF0/SOS component records, expected JFIF APP0
  fields, and expected DQT/DHT payload hashes. `check-run-evidence` requires
  strict JSON integer validation dimensions matching the parsed JPEG dimensions,
  then cross-checks the expected marker order through SOS and terminal EOI
  against the parsed JPEG marker sequence, expected minimum scan-data length
  against the parsed entropy
  scan length, expected DQT/DHT table order and SOS spectral fields against the
  parsed JPEG, the expected SOF0/SOS component records against the parsed JPEG,
  expected JFIF APP0
  presence and fixed APP0 fields against the parsed JPEG, the expected chroma
  mode against the parsed JPEG chroma mode when chroma checking was requested,
  expected DQT payload hashes against parsed DQT table hashes when quality
  checking was requested, and expected DHT table hashes
  against parsed DHT table hashes when standard-Huffman checking was requested.
  Complete validation evidence requires the expected DQT/DHT hash records
  whenever the corresponding quality or standard-Huffman check is requested,
  and the validation quality value must be absent or an actual JSON integer in
  `1..100`, not a boolean, while the validation restart interval must be absent
  or an actual JSON integer in `0..65535`, not a boolean,
  plus the host capture limits used for maximum output bytes and RX timeout. RX
  timeout values must be finite and positive when present, and maximum output
  bytes must be positive. Frame dimensions and frame limits must be positive,
  and AXI-Lite base addresses must be nonnegative.
- JSON evidence records host-observed transfer elapsed seconds and derived byte
  rates only when elapsed time is positive. Elapsed-time evidence must be finite
  and nonnegative. `complete_hardware_run_evidence` requires positive elapsed
  time and finite positive derived input and output byte rates. Use hardware
  counters or driver timestamps before
  making final throughput claims.
- `frame-timing --clock-hz CLOCK --max-frame-cycles LIMIT --json` records the
  split 64-bit PL counter, completed-frame count, clean idle/protocol status,
  derived time/FPS, and a machine-readable target result. Its stable
  completed-count/high/low/high/completed-count read avoids accepting a torn
  counter value. The command exits nonzero on a target, status, or
  completed-frame check failure.
- For `run-stream-devices --json`, the helper records the AXI-Lite status
  checkpoints enforced after configuration, before transfer, and after
  validation, including the AXI-Lite target sampled for each checkpoint, the
  checkpoint count, actual and expected checkpoint context lists, whether those
  lists matched, and run-level summaries for all-idle, any-busy, and
  any-protocol-error checkpoints.
- A standard JPEG decoder can open `output.jpg`; when `--decoder-command` is
  used, that decoder check, command string, resolved argv, timeout, return
  code, elapsed seconds, bounded stdout/stderr, captured output lengths, and
  capture limit are part of the JSON evidence, and decoder failure or timeout
  fails validation. Decoder timeout values must be finite and positive.
- The decoded dimensions match the input.
- Visual content is recognizable for non-flat test images.

## Current Physical KV260 Evidence (2026-07-12)

The exact instrumented routed image was programmed over the on-board FTDI JTAG
cable into a physical KV260 revB / K26 target. Artifact evidence is:

- bitstream: 7,797,910 bytes, SHA-256
  `f42f9ad1861999a37636bf10a675bf7fa02206bdcadad73e086452b522205b8f`;
- XSA SHA-256
  `b9a06f2ec04f1df5fb9c42f7c142e1c4f46c67b397e335dac7aa4343a132863f`;
- post-implementation DCP SHA-256
  `9ba795a8da4cd1cfb225ace129f207299eed0b07b6487129ffd1012540e664a8`.

The matching strict Vivado evidence contains 12 passing records, zero failing
records, zero unrouted nets, and zero routing errors. The read-only frame timer
at `0x18`/`0x1c` measures from the first accepted input beat through accepted
output TLAST; `0x20` confirms one completed frame. JTAG polling time is recorded
separately and is not used for FPS.

Five exact-image JTAG-driven AXI DMA runs passed structural validation and
FFmpeg decoding:

| Fixture | Mode | Cycles | FPS at 100 MHz | JPEG bytes | JPEG SHA-256 |
| --- | --- | ---: | ---: | ---: | --- |
| 17x13 q85, restart 1 | 4:2:0 | 2,178 | 45,913.68 | 738 | `41c0a8470992dd07469005a7fd11a1ea15af47bc46f18828ebf054aaa690b959` |
| 1920x1080 gradient/checker q85 | 4:2:0 | 2,210,885 | 45.230756 | 151,020 | `be9217b91465ccad42822d8eb87cb0f278a3f9f0182ac32e1d44129a1d50f461` |
| 1920x1080 gradient/checker q85 | 4:4:4 | 3,224,557 | 31.012012 | 168,562 | `9b8d511c658e2342880a924976e8e4dc52e44a03a90fa14882faffb9332bd029` |
| 1920x1080 seeded-random q90 | 4:2:0 | 2,211,207 | 45.224169 | 1,702,123 | `0b4a541059adfa349a9ad4a95e7208863b4305f33fe0629d7d8700c60b6d128a` |
| 1920x1080 seeded-random q90 | 4:4:4 | 3,734,574 | 26.776816 | 3,021,074 | `1be4e57d71e6a5ac66f82ac688253beb2407cb4e1e6da26d6af14d06a415c4c2` |

The gradient/checker PPM and packed RGB SHA-256 values are
`c6aa316e2b2dbc2ad39f5e23c83420d44d116ed4d4b8d8748af38375af4bd771`
and `ad3ff48774e62e0ddc0e8778c8dd651b7d628fa57769f8e05f0132fd745f6f0e`.
The seeded-random values are
`cb1544b0e93b0f59a26fe500ab9715c4048d7fb40e20ec0e39fd9809260530ee`
and `bb79cfd6aa684f98f6d15090059caef6664045583823f8d3d27713cd9374b791`.
For every run, MM2S and S2MM ended at `0x00001002` (IOC plus idle), encoder
status returned to `0x00000000`, completed-frame count was one, and no DMA or
protocol error was reported.

The defined q85 gradient/checker benchmark clears 1080p30 in both modes. The
q90 seeded-random 4:4:4 stress result does not; JPEG entropy work and byte count
are content-dependent, so the target is not a universal worst-case guarantee.

## Current Physical 4K60 Evidence (2026-07-13)

The final 128-bit-input, dual-MCU-entropy image was built with 256-beat MM2S
bursts and MM2S store-and-forward disabled. The strict complete Vivado gate has
twelve passing records. Routed setup WNS is `+0.006 ns`, hold WHS is
`+0.010 ns`, and post-implementation utilization is 58,899 LUTs (50.29%),
83,590 registers (35.69%), 97 BRAM tiles (67.36%), and 194 DSPs (15.54%).

The exact artifact directory is
`build/vivado/hjpeg-kv260-4k60-artifacts-150-dual-mcu-burst256-nomsf/`:

- bitstream SHA-256:
  `6c8217d5ada789bf0701ec5142b955e99e4f628dffc800a9d1f13eb052131a24`;
- XSA SHA-256:
  `f478eb04afc8d7915e84c6b5aa0b2d80903c1c3d9b792a89e2830f75383c413e`;
- routed DCP SHA-256:
  `411c3ea23eea12ccad5aab8bed13bc292966fccaa05c252b95cfce9983642d59`.

The deterministic input PPM and packed RGB hashes are
`90f60458344d93e7b10e6b6c86f5a02817a6c0d7db41f9e37460180c4aeb6d06`
and
`d5e8ec5febdcc909aadb7b33d31da9160fc92c88b35f026a81f2fb72c7e2edae`.
Both physical runs transferred all 33,177,600 input bytes, ended MM2S and S2MM
at `0x00001002`, returned encoder status `0x00000000`, counted one completed
frame, ended with `RUN_OK`, passed the 2,500,000-cycle gate, and decoded with
FFmpeg:

| Fixture | Mode | Cycles | FPS at 150 MHz | JPEG bytes | JPEG SHA-256 |
| --- | --- | ---: | ---: | ---: | --- |
| 3840x2160 gradient/checker q85 | 4:4:4 | 2,090,494 | 71.753375 | 609,217 | `75de142ca238e8e3d3803e79478872e7fe79aa77488af3355ded665c6643b360` |
| 3840x2160 gradient/checker q85 | 4:2:0 | 2,219,916 | 67.570124 | 529,549 | `6d9b73bdba60c617ce15c06ca08d677514fb70b6cd3c0fb44b269058aacf69ba` |

The output hashes match captures from the earlier four-beat-DMA build, while
the PL frame counts fell from roughly 4.329 million cycles. This isolates the
improvement to ingress transport rather than JPEG contents. These runs complete
the active branch's defined 4K60 benchmark contract in both sampling modes.

## Completion Bar

Do not mark the project complete until the repo has current evidence for:

- Full simulator suite passing.
- Vivado IP packaging passing.
- Block design validation passing.
- Bitstream and XSA generation passing.
- Post-implementation timing/resource reports reviewed.
- A real KV260 run producing at least one decodable JPEG through AXI DMA.
- Host-side validation passing for that hardware-produced JPEG.

All functional completion items, the defined 1920x1080-at-30-fps baseline, and
the defined 3840x2160-at-60-fps q85 benchmark in both sampling modes have
current evidence. Production Linux DMA/driver coexistence and separately
defined higher-entropy throughput are follow-on integration/performance work.
