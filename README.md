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
3 is ignored, and the low three `keep` bits must be set for every pixel. A
partial input word is accepted to avoid wedging the stream, but raises the
sticky protocol-error flag.
Frames that start with unsupported dimensions are discarded through input TLAST
without entering the JPEG core, so clearing the error lets the next valid frame
start cleanly.

The AXI-Lite control wrapper accepts independent AW and W channel handshakes and
honors byte write strobes on writable registers.

Frame configuration is sampled on the first accepted input pixel and held until
the encoded JPEG frame completes. Host software should update control registers
between frames.

## Requirements

- JDK 21 or newer
- sbt, or the checked-in Mill bootstrap script
- Verilator for simulator-backed tests
- Python 3 for host-side helper scripts

## Build

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
  --timing build/vivado/hjpeg-kv260-artifacts/post_impl_timing_summary.rpt \
  --hold-timing build/vivado/hjpeg-kv260-artifacts/post_impl_timing_summary.rpt \
  --utilization build/vivado/hjpeg-kv260-artifacts/post_impl_utilization.rpt \
  --drc build/vivado/hjpeg-kv260-artifacts/post_impl_drc.rpt \
  --route-status build/vivado/hjpeg-kv260-artifacts/post_impl_route_status.rpt \
  --clock-utilization build/vivado/hjpeg-kv260-artifacts/post_impl_clock_utilization.rpt \
  --clock-period-ns 10.0
```

Add `--json` to include artifact/report paths, byte lengths, SHA-256 hashes,
parsed setup WNS and hold WHS values, utilization rows, thresholds, and
target clock period/frequency, DRC violations, route-status counts, required
clock-utilization report hashes, the requested input path lists and gate values,
and pass/fail state in machine-readable build evidence. Missing, non-file, or
unparseable reports are recorded as structured JSON failures. Numeric report
thresholds must be finite; `--clock-period-ns` must be finite and positive, and
`--max-utilization` must be finite and nonnegative. Use
`--hold-timing` for post-implementation reports where hold timing is expected
to be closed.

These Vivado scripts consume `generated-kv260-axi-lite-top/filelist.f`. Generate
the AXI-Lite top first. The IP packaging script maps the generated clock, reset,
AXI-Lite, and AXI-stream ports onto Vivado bus interfaces and exposes a 4 KiB
AXI-Lite register aperture. The block-design script consumes the packaged IP and
wires it to Zynq UltraScale+ PS, AXI DMA, SmartConnect, and reset/interrupt
plumbing, assigns addresses, validates/saves the block design, and generates the
HDL wrapper. The bitstream script runs synthesis and implementation, writes
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
the RX device until JPEG EOI, rejects trailing bytes already returned after that
EOI, checks status for `busy` / `protocol_error`, and validates the resulting
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
`clear-error`, `validate-jpeg`, or `run-stream-devices` when you want evidence in a
machine-readable form for logs. Input-prep evidence includes dimensions, checked
frame limits, byte lengths, SHA-256 hashes for generated files, and PPM
per-channel min/max values plus non-flat/color flags. Configuration evidence
includes the AXI-Lite target, frame settings,
checked frame limits, quality, restart interval, chroma mode, JFIF setting, and
control word. Host CLI configuration quality must be in `1..100`, and restart
interval values must be in `0..65535`. Frame dimensions and frame limits must
be positive, and AXI-Lite base addresses must be nonnegative. Status evidence
records the AXI-Lite target, raw status word, decoded flags, and text state.
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
SHA-256, decoder command, resolved decoder argv, decoder timeout, and decoder
elapsed seconds when one was provided, plus decoder return code and bounded
stdout/stderr when a decoder command ran, including captured output lengths and
the capture limit. JFIF
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
MCU count, or `0` to require no DRI/RST markers. Pass
`--check-chroma-mode` with `--chroma-subsample` when validating a standalone
4:2:0 file. Pass `--expect-jfif present` or `absent` to check optional JFIF APP0
signature emission. Pass `--quality N` and `--require-standard-huffman` to
check standalone JPEG table payloads. For `run-stream-devices`, the configured
restart interval, chroma mode, JFIF setting, quality-scaled DQT payloads, and
standard DHT payloads are checked against the captured JPEG automatically; the
run evidence also includes the input RGB stream byte length, expected byte
length, and SHA-256, host capture limits, plus the AXI-Lite target and status
checkpoints enforced during the run. Maximum output bytes must be positive, and
RX timeout values must be finite and positive when present. It
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
