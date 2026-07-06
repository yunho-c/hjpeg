# hjpeg

Hardware-accelerated JPEG encoder in Chisel.

The initial target platform is the AMD/Xilinx Kria KV260. The current tree
contains a functional baseline JPEG encoder datapath with Scala/Chisel build
files, streaming RTL shells, elaboration entry points, and simulator tests.

## Goals

- Baseline JPEG encoder datapath in synthesizable Chisel
- Raster RGB input stream
- FPGA-friendly streaming output path
- KV260-oriented top-level elaboration target
- Incremental test fixtures for each pipeline stage

## Current RTL Shape

`HjpegCore` accepts raster RGB pixels and emits a complete baseline JPEG byte
stream. It supports:

- arbitrary nonzero frame dimensions up to `HjpegConfig.maxFrameWidth` /
  `maxFrameHeight`
- edge padding by replicating the last valid row or column
- 4:4:4 encoding
- 4:2:0 encoding when `enableChromaSubsample` is set
- quality-scaled standard quantization tables
- standard baseline Huffman tables
- optional JFIF APP0 plus SOI/DQT/SOF0/DHT/DRI/SOS/EOI markers
- configurable JPEG restart intervals
- entropy bit packing and `0xff` byte stuffing

The KV260-oriented wrappers are:

- `HjpegKv260Top`: direct `FrameConfig` plus AXI-stream RGB/JPEG ports
- `HjpegKv260AxiLiteTop`: AXI-Lite control/status plus AXI-stream RGB/JPEG
  ports for easier IP packaging

`HjpegAxiStreamCore` uses a 24-bit internal RGB stream with R, G, and B in the
low three bytes and requires `keep = 0b111`. The KV260 wrappers expose a
DMA-compatible 32-bit RGB input stream: bytes 0, 1, and 2 are R, G, and B, byte
3 is ignored, and the low three `keep` bits must be set for every pixel. The
fourth `keep` bit may be clear, but any missing lower RGB byte is a malformed
input word. A partial input word is accepted to avoid wedging the stream, raises
the sticky protocol-error flag, and is not fed into the JPEG core.
Frames that start with unsupported dimensions are discarded through input TLAST
without entering the JPEG core, so clearing the error lets the next valid frame
start cleanly. Frames with incomplete RGB words are also drained through TLAST
without completing a JPEG frame. If the expected final pixel arrives without
TLAST, the wrapper flags the protocol error and drains subsequent input beats
until TLAST before a clear pulse permits the next frame. The clear pulse also
resets buffered encoder pipeline state so early-TLAST or otherwise partial
frames cannot contaminate the next frame. The AXI wrapper tests cover both
single-beat and multi-beat unsupported frame discard/recovery paths, incomplete
RGB word recovery, plus early-TLAST and late-TLAST recovery.

The AXI-Lite control wrapper accepts independent AW and W channel handshakes,
honors byte write strobes on writable registers, and holds read/write responses
stable under host backpressure.

Frame configuration is sampled on the first accepted input pixel and held until
the encoded JPEG frame completes. Host software should update control registers
between frames.

## Requirements

- JDK 21 or newer
- sbt, or the checked-in Mill bootstrap script
- Verilator for simulator-backed tests
- Python 3 for host-side helper scripts

## Build

On Windows or a newly provisioned machine, check the simulator toolchain before
running ChiselSim-backed tests:

```sh
python3 scripts/dev/check_chiselsim_env.py
```

Pass `--json` to save machine-readable toolchain evidence; the report includes
detected `make`, `sh`, and `verilator` paths, first-line `--version` output
when available, and relevant `SHELL`/`MAKESHELL` overrides.
The preflight detects mixed Windows/MSYS setups where svsim-generated Makefiles
can fail before RTL simulation starts. It also warns that forcing MSYS `make` to
use `cmd.exe` is not a reliable workaround because svsim Makefiles include both
Windows clean rules and POSIX shell fragments. If it reports an incompatible
simulator environment, `sbt Test/compile` is still useful as a source-level
gate, but run simulation tests from a compatible Linux/WSL Verilator
environment.

Run the test suite with sbt:

```sh
sbt test
```

Or with Mill:

```sh
./mill hjpeg.test
```

Generate the core SystemVerilog:

```sh
sbt 'runMain hjpeg.Elaborate'
```

Generate the KV260-oriented top:

```sh
sbt 'runMain hjpeg.ElaborateKv260Top'
```

Generate the KV260 AXI-Lite control top:

```sh
sbt 'runMain hjpeg.ElaborateKv260AxiLiteTop'
```

Run a Vivado synthesis project for the AXI-Lite top, when Vivado is installed:

```sh
vivado -mode batch -source scripts/vivado/synth_kv260_axi_lite.tcl
python3 scripts/vivado/check_reports.py \
  --artifact build/vivado/hjpeg-kv260-axi-lite/post_synth.dcp \
  --timing build/vivado/hjpeg-kv260-axi-lite/post_synth_timing_summary.rpt \
  --utilization build/vivado/hjpeg-kv260-axi-lite/post_synth_utilization.rpt \
  --json
```

Package reusable RTL IP for Vivado:

```sh
vivado -mode batch -source scripts/vivado/package_kv260_axi_lite_ip.tcl
```

Create the first KV260 block-design project around the packaged IP:

```sh
vivado -mode batch -source scripts/vivado/create_kv260_block_design.tcl
```

Build the block design through bitstream generation and export an XSA:

```sh
vivado -mode batch -source scripts/vivado/build_kv260_bitstream.tcl
```

The bitstream script also accepts optional project directory, artifact
directory, and Vivado job-count arguments after `-tclargs`; the job count must
be a positive integer. Vivado scripts reject extra positional `-tclargs` so
automation does not silently ignore misspelled or misplaced arguments.

Check generated timing and utilization reports:

```sh
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
  --require-complete-evidence
```

Add `--json` to include artifact/report paths, resolved paths, byte lengths, SHA-256 hex hashes,
parsed setup WNS and hold WHS values, utilization rows, thresholds, and
target clock period/frequency, DRC violations, route-status counts, required
clock-utilization report hashes, floorplan pblock/placed-cell counts, parsed
address-map AXI-Lite aperture base/high
addresses and byte ranges for `hjpeg_0/s_axi_lite` and
`axi_dma_0/S_AXI_LITE`, duplicate/missing/overlapping address-map interface
checks, the requested input path lists and gate values, checked report/artifact
count, per-category checked counts, required evidence category presence,
per-category passing/failing counts, present and missing category names, failing
category names, required `.bit`/`.xsa`/`.dcp` artifact suffix presence, present and
missing required suffix names, failing required suffix names, required artifact
filename presence for `hjpeg_kv260.bit`, `hjpeg_kv260.xsa`, and `post_impl.dcp`,
address-map filename presence for `hjpeg_kv260_address_map.rpt`,
required report filename presence for `post_synth_timing_summary.rpt`,
`post_impl_timing_summary.rpt`, `post_synth_utilization.rpt`,
`post_impl_utilization.rpt`, `post_impl_drc.rpt`,
`post_impl_route_status.rpt`, `post_impl_clock_utilization.rpt`, and
`post_impl_floorplan.rpt`,
present/missing/failing filename names, required suffix/filename passing/failing
counts, required/present/missing category, suffix, artifact-filename,
address-map-filename, and report-filename counts, aggregate pass/fail counts,
diagnostic failure count, checked/passed/failed path lists, a
`diagnostic_summary` that checks aggregate count/path/category consistency,
complete-evidence required/missing/failing lists, and pass/fail state in
machine-readable build evidence.
Required evidence category presence is based on at least one passing record in
that category, not just a requested input path. Complete Vivado evidence also
requires every supplied required evidence category and required `.bit`/`.xsa`/
`.dcp` artifact suffix to have no failing records, and requires the named
artifacts `hjpeg_kv260.bit`, `hjpeg_kv260.xsa`, and `post_impl.dcp` to be present
and passing, plus the named address-map, timing/utilization/implementation, and
floorplan reports. Complete Vivado evidence also requires the generated
diagnostic summary to be valid, required route-status counts to be present and
zero, address-map hexadecimal fields to match parsed numeric addresses, passing
required records to carry nonempty path/resolved-path file metadata and SHA-256 hashes, and the
floorplan record to include a positive placed-cell count.
Complete Vivado evidence counts only records whose `passed` field is an actual
JSON boolean `true`. Use
`--require-complete-evidence` for full bitstream evidence gates; partial
post-synthesis checks can omit it.
Missing, non-file, or unparseable reports are recorded as structured
JSON failures. Numeric report thresholds must be finite; `--clock-period-ns`
must be finite and positive, and `--max-utilization` must be finite and
nonnegative. Use
`--hold-timing` for post-implementation reports where hold timing is expected
to be closed.

If an already implemented KV260 block-design project is missing only the
floorplan evidence report, regenerate that report without rerunning the full
bitstream flow:

```sh
vivado -mode batch -source scripts/vivado/write_kv260_floorplan_report.tcl
```

The script accepts optional project and artifact directories after `-tclargs`
and writes `build/vivado/hjpeg-kv260-artifacts/post_impl_floorplan.rpt` from the
completed `impl_1` run.

These Vivado scripts consume `generated-kv260-axi-lite-top/filelist.f`. Generate
the AXI-Lite top first. The IP packaging script maps the generated clock, reset,
AXI-Lite, and AXI-stream ports onto Vivado bus interfaces and exposes a 4 KiB
AXI-Lite register aperture. The block-design script consumes the packaged IP and
wires it to Zynq UltraScale+ PS, AXI DMA, SmartConnect, and reset/interrupt
plumbing, assigns addresses, writes an address-map report, validates/saves the
block design, and generates the HDL wrapper. The bitstream script runs synthesis
and implementation, writes
post-synthesis/post-implementation utilization and timing reports, copies the
bitstream, and exports a hardware platform XSA. These scripts do not create a
complete bootable KV260 image or prove on-board behavior.

See `docs/kv260-bringup.md` for the end-to-end evidence checklist before calling
the hardware path complete.

The host-side JPEG validator is intentionally strict about the encoder's
baseline marker sequence: optional APP0/JFIF, DQT, SOF0, DHT, optional DRI, SOS,
entropy-coded scan data, then EOI. This catches marker FSM regressions that may
still look superficially like parseable JPEG files.

For a new agent or developer taking over without project history, read
`docs/handoff.md` first. It summarizes current implementation status, recent
verification, known blockers, and the recommended next steps for Vivado/KV260
bring-up.

## Host Helpers

The host utility prepares payloads and register writes for the KV260 AXI-Lite /
AXI DMA design:

```sh
python3 scripts/host/hjpeg_host.py make-test-ppm input.ppm --width 640 --height 480 --json
python3 scripts/host/hjpeg_host.py pack-ppm input.ppm input.rgb --json
python3 scripts/host/hjpeg_host.py config --base-addr 0xa0000000 --width 640 --height 480 --json
python3 scripts/host/hjpeg_host.py status --base-addr 0xa0000000 --json
python3 scripts/host/hjpeg_host.py clear-error --base-addr 0xa0000000 --json
python3 scripts/host/hjpeg_host.py run-stream-devices \
  --base-addr 0xa0000000 \
  --tx-device /dev/hjpeg-mm2s \
  --rx-device /dev/hjpeg-s2mm \
  --input-rgb input.rgb \
  --output-jpeg output.jpg \
  --width 640 \
  --height 480 \
  --json
python3 scripts/host/hjpeg_host.py validate-jpeg output.jpg --width 640 --height 480
```

`make-test-ppm` writes a deterministic non-flat binary P6 PPM pattern for
repeatable board bring-up. `pack-ppm` accepts binary P6 PPM and writes one
32-bit little-endian stream beat per pixel: R, G, B, and one ignored zero byte.
By default, host-side input preparation and hardware configuration reject frames
outside the default RTL top's `1920x1080` limit; pass `--max-width` and
`--max-height` only when testing a custom elaboration with different
`HjpegConfig` frame limits. `pack-ppm` checks these limits from the PPM header
before reading the RGB payload.
`run-stream-devices` targets Linux board images that expose AXI DMA MM2S/S2MM
endpoints as byte-stream device files: it configures AXI-Lite registers through
`/dev/mem`, writes the padded RGB stream to the TX device, captures bytes from
the RX device until JPEG EOI, rejects identical TX/RX endpoint paths before
device I/O, rejects trailing bytes already returned after that EOI, checks status
for `busy` / `protocol_error`, and validates the resulting
dimensions, quality-matched DQT payloads, standard DHT payloads, and non-empty
scan data. DMA
drivers that use ioctls or buffer queues still need a small adapter around the
same host-side packing and validation helpers.

To fold a standard decoder into the validation transcript, pass a command with
`--decoder-command`. The helper replaces `{jpeg}` with the output path, or
appends the path when no placeholder is present. The decoder subprocess is
bounded by `--decoder-timeout-seconds`, which defaults to 30 seconds. JSON
evidence records that the decoder passed, the command string used, the timeout
value, the resolved argv, elapsed seconds, the return code, bounded
stdout/stderr, captured stdout/stderr lengths, and the capture limit. Decoder
timeout values must
be finite and positive:

```sh
python3 scripts/host/hjpeg_host.py validate-jpeg output.jpg \
  --width 640 \
  --height 480 \
  --restart-interval 0 \
  --check-chroma-mode \
  --expect-jfif present \
  --quality 50 \
  --require-standard-huffman \
  --decoder-command 'magick identify {jpeg}' \
  --decoder-timeout-seconds 30
```

Add `--json` to `make-test-ppm`, `pack-ppm`, `config`, `status`,
`clear-error`, `validate-jpeg`, `run-stream-devices`, or `check-run-evidence`
when you want evidence in a machine-readable form for logs. Input-prep evidence
includes dimensions, checked frame limits, byte lengths, SHA-256 hex hashes for
generated files, and PPM per-channel min/max values plus non-flat/color flags.
`run-stream-devices`
accepts `--input-ppm` to validate that the saved source PPM dimensions match
the configured frame and that its packed RGB bytes exactly match `--input-rgb`;
JSON evidence then records the PPM stats, PPM-derived packed RGB byte length
and SHA-256 hex, and packed-RGB match result.
Configuration evidence
includes the AXI-Lite target, frame settings,
checked frame limits, quality, restart interval, chroma mode, JFIF setting, and
control word. Host CLI configuration quality must be in `1..100`, and restart
interval values must be in `0..65535`. Frame dimensions and frame limits must
be positive, and AXI-Lite base addresses must be nonnegative. Status evidence
records each checkpoint context, AXI-Lite target, raw status word, decoded
flags, and text state.
Clear-error evidence records the AXI-Lite target and control word pulsed to clear sticky protocol faults. JPEG validation
evidence includes dimensions, SOF0 8-bit sample
precision, exactly one SOF0 and one SOS segment, three-component frame shape,
scan-data byte count, unstuffed scan-data SHA-256, stuffed entropy `0xff` byte
count, SOF0 component ID order, sampling factors, MCU count, decoded chroma
mode, exact SOS component order and coverage, baseline SOS spectral fields,
SOS component table selectors,
DQT/DHT table IDs, exact DC/AC Huffman table set, exact DQT table set, DQT table
order, exact DHT table order, DQT 8-bit precision, DQT/DHT payload byte counts
and SHA-256 hashes, APP0 and JFIF APP0 counts, parsed JFIF APP0
version/density/thumbnail fields, exact DQT/DHT segment counts,
DQT/DHT/DRI/restart marker counts, a grouped `marker_counts` object,
parsed marker sequence,
parsed DRI restart interval, RST marker sequence, total JPEG byte length,
SHA-256, standalone validation expectations including derived expected RST
marker count, expected RST marker sequence, expected marker counts, expected
marker order through SOS and EOI, expected SOF0 precision and component count,
expected SOF0/SOS component shape, expected SOS spectral fields, expected
minimum scan-data length, expected DQT/DHT table order, expected chroma mode
when checked, expected JFIF APP0 baseline fields when JFIF is required, and
expected DQT/DHT payload hashes when table checks are enabled, decoder command,
resolved decoder argv, decoder
timeout, and decoder elapsed seconds when one was provided, plus decoder return
code and bounded stdout/stderr when a decoder command ran, including captured
output lengths and the capture limit. JFIF
APP0 segments must match the encoder's baseline version, density, and
no-thumbnail fields. The validator rejects non-8-bit or
non-three-component SOF0 frames, zero SOF0 dimensions, duplicate SOF0/SOS
markers, nonstandard SOF0/SOS component IDs, mismatched SOS component lists,
nonstandard SOF0 quantization table selectors, nonstandard SOS table selectors,
unsupported SOF0 sampling factors, zero SOF0 sampling factors, non-baseline SOS
spectral fields, unsupported header markers, malformed, non-JFIF, or duplicate
APP0 markers, unexpected non-RST/non-EOI markers after SOS, nonstandard DQT/DHT
table sets or segment counts, duplicate DQT/DHT table definitions, swapped DQT
or DHT table order, non-8-bit DQT tables, zero-valued DQT entries, empty,
oversized, oversubscribed, or invalid baseline DHT tables, RST markers without
DRI, RST markers that do not increment modulo 8 from RST0, trailing bytes after
EOI, SOF0 or SOS references to missing DQT/DHT tables, and decoder commands that
fail or time out. Pass
`validate-jpeg --restart-interval N` to require the parsed DRI interval to match
`N` and the scan to contain the expected number of RST markers for the parsed
MCU count, or `0` to require no DRI/RST markers. The parsed MCU count comes from
SOF0 sampling factors, so 4:2:0 padded dimensions use 16x16 MCU geometry when
deriving expected RST marker counts and sequences. Pass
`--check-chroma-mode` with `--chroma-subsample` when validating a standalone
4:2:0 file. Pass `--expect-jfif present` or `absent` to check optional JFIF APP0
signature emission. Pass `--quality N` and `--require-standard-huffman` to
check standalone JPEG table payloads. For `run-stream-devices`, the configured
restart interval, chroma mode, JFIF setting, quality-scaled DQT payloads, and
standard DHT payloads are checked against the captured JPEG automatically; the
run evidence also records those validation expectations, the input RGB stream
byte length, expected byte length, whether those lengths matched, and SHA-256 hex,
host capture limits, plus the AXI-Lite target, status checkpoints enforced
during the run, status checkpoint count, actual and expected status checkpoint
context lists, whether those lists matched, and run-level summaries for
all-idle, any-busy, and any-protocol-error checkpoints. The JSON record also
includes `hardware_run_summary`, which collects evidence-presence bits and
pass/fail booleans for the recorded run checks, the ordered required evidence
group list, ordered recorded check names, evidence/check counts, failing check
names, passing check names, missing and present evidence group names, and
whether complete hardware-run evidence was captured. Complete
hardware-run evidence requires an explicit `jpeg_validation_passed` flag,
a captured output JPEG path and resolved path, positive parsed dimensions, hashes, and non-empty
scan data, source PPM supplied through
`--input-ppm`, non-flat/color source-image stats, positive host-observed
transfer timing with finite positive derived input and output byte rates, and a
passing decoder check from `--decoder-command` with the command string, resolved
argv matching the command and JPEG path, positive timeout, nonnegative elapsed
time, zero return code, stdout/stderr strings with matching captured output
lengths, output lengths within the positive capture limit, and non-truncated
captured output evidence.
The summary also cross-checks
JPEG dimensions against the encoder configuration, validation expectations,
source PPM dimensions, and expected RGB stream byte length, and requires the
parsed marker sequence to begin with SOI and end with EOI. It also cross-checks
the grouped marker counts against the scalar APP0/JFIF
APP0/DQT/SOF0/DHT/SOS/DRI/RST counts, the RST sequence length against the
recorded RST count, and the recorded marker counts/RST sequence against
validation expectations when those expectations are present, expected marker
order through SOS and terminal EOI against the parsed JPEG marker sequence,
expected minimum scan-data length against the parsed entropy scan length,
expected DQT/DHT table order and SOS spectral fields against the parsed JPEG,
expected SOF0 component records and SOS component selectors against the parsed
JPEG, expected JFIF APP0 presence and fixed APP0 fields against the parsed JPEG,
the expected chroma mode against the parsed JPEG chroma mode when chroma
checking was requested, and expected DQT payload hashes against the parsed DQT table hashes
when quality checking was requested, and expected DHT table hashes against the
parsed DHT table hashes when standard-Huffman checking was requested. Those
expected hash records are required when the corresponding quality or
standard-Huffman check is requested. Input RGB evidence
must include a nonempty path and resolved path, positive byte length, a SHA-256 hex hash, a positive expected byte
length, and a boolean actual-vs-expected length-match flag that matches the
recomputed result. Stream-device evidence must
include nonempty TX/RX device paths, resolved identities that match the raw
paths, and distinct raw and resolved endpoints. Capture configuration evidence
must include a positive maximum output byte count and either no timeout or a
finite positive timeout. AXI-Lite
target evidence must include a non-empty string device path, nonnegative base
address, and matching hexadecimal base-address text. Encoder configuration
evidence must include supported dimensions, quality/restart values in range,
boolean control flags, and a control word/hex string matching those flags. Validation
expectations evidence must include the baseline shape, marker order, table
order, marker counts, restart marker count/sequence when applicable, SOS
spectral fields, matching marker order, matching minimum scan-data length,
matching table order, matching SOS spectral fields, matching SOF0/SOS component
records, matching JFIF APP0 policy and fields, matching chroma mode when
requested, matching DQT payload hashes when quality checking was requested,
matching DHT table hashes when
standard-Huffman checking was requested, and standard-Huffman requirement.
Source PPM
evidence must include a nonempty path and resolved path, file and packed-RGB SHA-256 hex hashes,
positive dimension-consistent RGB and packed byte lengths, a recomputed input-RGB
length/hash match with a boolean packed-RGB match flag, and
non-flat/color image stats. Status evidence must include the detailed checkpoint
list, matching checkpoint count, expected ordered contexts, per-checkpoint
AXI-Lite targets matching the run target, zero raw status words, and all
checkpoints idle with no protocol error or busy state. Summary checks recompute
checkpoint order, checkpoint target matches, decoded status text/flags, and
boolean aggregate expected-contexts/idle/error/busy flags from the detailed
status records. They also
recompute RGB byte-count matches,
PPM-to-input-RGB consistency, and transfer byte rates from the saved lengths,
hashes, and elapsed time.
The summary records the required evidence group names, total, present, and
missing evidence-group counts, recorded check names, total, passing, and failing
check counts, present and missing evidence group names, and the names of passing
and failing checks for review.
Required boolean evidence fields must be actual JSON booleans.
Pass `run-stream-devices --require-complete-evidence` for final board evidence
gates; omit it for partial hardware smoke tests that intentionally skip source
PPM or decoder evidence. Run JSON records whether complete evidence was
required, whether complete evidence was captured, which evidence groups were
missing, and which complete-evidence checks failed.
Saved run JSON can be checked later with:

```sh
python3 scripts/host/hjpeg_host.py check-run-evidence run.json \
  --vivado-evidence vivado.json \
  --json
```

The saved-evidence checker recomputes `hardware_run_summary` from the transcript
and fails if the stored summary does not match the recomputed evidence, if the
run did not record JSON boolean `true` for
`complete_hardware_run_evidence_required`, if the top-level
`complete_hardware_run_evidence` flag is missing, is not a JSON boolean, or does
not match the recomputed summary, or if the recorded missing-evidence and failing-check
diagnostic lists do not match the recomputed summary. The host and Vivado
helpers emit strict JSON for evidence output, and saved run/Vivado evidence
files must be strict JSON; non-standard constants such as `NaN` and `Infinity`
are rejected as malformed evidence. When
`--vivado-evidence` points at `check_reports.py --json` output, it also extracts
the passing `hjpeg_0/s_axi_lite` address-map base address. The Vivado transcript
must have JSON boolean `true` values for `passed`,
`complete_vivado_flow_evidence`, `complete_vivado_flow_evidence_required`, and
`arguments.require_complete_evidence`, the required `.bit`,
`.xsa`, and `.dcp` artifact suffix evidence true, and the required `hjpeg_kv260.bit`,
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
and the top-level `clock_target_valid` flag true. It also requires the Vivado
evidence-category summary to show every required category present and passing,
the floorplan record to include a positive placed-cell count,
the top-level `complete_vivado_flow_evidence` flag and complete-evidence
missing/failing lists to match nested evidence summaries,
zero diagnostic failures and failed paths in the Vivado summary, checked paths
matching passed paths, a `diagnostic_summary` object that matches the aggregate
Vivado fields and has `valid` true, positive per-category checked counts whose
sum matches the total checked count and match the per-category pass/fail totals,
plus a passing route-status record with zero unrouted nets and zero nets with
routing errors. Address-map evidence must also keep its hexadecimal address strings
consistent with the parsed numeric base/high address fields.
The checker fails if the run
transcript's AXI-Lite base address does not match the Vivado build evidence, or
if multiple Vivado evidence files report conflicting HJPEG base addresses. Its JSON output
includes aggregate checked/pass/fail transcript counts, diagnostic failure count,
checked/passed/failed path lists, summary checked, matched, and
mismatched counts and paths, aggregate evidence group present/missing counts and
names, aggregate recorded/passing/failing check counts and names, Vivado
address-map evidence counts, aggregate raw/resolved stream endpoint counts and
device lists, aggregate AXI-Lite target device/base-address counts and lists,
aggregate frame dimensions, encoder configuration values, and validation
expectation values, aggregate status-check context/flag values and host transfer
rates, aggregate capture/input byte-count and source-image values, aggregate
JPEG structure/hash values and decoder result values, aggregate JPEG/input
artifact path/resolved-path and decoder-command inventories, Vivado
checked/passed/failed evidence path and resolved-path lists,
parsed HJPEG base addresses, HJPEG base-address count and consistency flag, plus
the recomputed summary, evidence/check counts, missing evidence groups, and
failing check names for each object-shaped transcript.

Maximum output bytes must be positive, and RX timeout values must be finite and
positive when present. It
also records host-observed transfer elapsed seconds and derived byte rates when
elapsed time is positive. Elapsed-time evidence must be finite and nonnegative.
Use hardware counters or driver timestamps for final throughput claims.

## Versions

- Scala 2.13.18
- Chisel 7.13.0
- ScalaTest 3.2.19
- sbt 1.12.13
- Mill 1.1.7

## License

GPLv3. See [LICENSE](LICENSE).
