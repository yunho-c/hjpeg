# hjpeg Architecture

`hjpeg` is intended to become a complete hardware JPEG encoder. The current RTL
implements a baseline JPEG datapath and keeps the hardware boundaries stable for
KV260 integration.

## Top-Level Flow

```text
RGB AXI stream
  -> raster coordinate wrapper
  -> RGB ingress
  -> color conversion
  -> MCU/block buffering
  -> DCT
  -> quantization
  -> entropy coding
  -> JPEG marker/scan assembler
  -> byte AXI stream
```

The main core now emits valid baseline JPEG byte streams that decode with
standard Java ImageIO tests. The datapath supports 4:4:4 and 4:2:0 component
sampling, frame dimensions that are not multiples of 8/16 through edge
replication, standard quantization/Huffman tables, optional JFIF APP0 emission,
byte stuffing, and marker assembly. Nonzero restart intervals emit DRI/RST
markers and reset DC predictors at MCU boundaries.

The raster-to-MCU stages buffer one 8-row 4:4:4 stripe or one 16-row 4:2:0 band
at a time. They then load one MCU into small block registers over multiple
cycles before presenting it to the DCT/quantization path. Each raster stage
reuses one block transform across the MCU's component blocks, capturing the
transformed coefficients before emitting the MCU packet. This keeps the stripe
memories to one read and one write port per component and avoids instantiating
three or six parallel DCT/quantization paths per MCU.

`Dct8x8Stage` is also multi-cycle. It captures one 8x8 sample block, computes
the row transform one product term per cycle, computes the column transform one
product term per cycle, then holds the completed coefficient block on its
decoupled output. This intentionally trades latency for a much smaller synthesis
problem than a fully combinational 8x8 two-dimensional DCT or a one-cycle
eight-term accumulation chain.

`QuantizeBlockStage` follows the same area-first direction. It captures the DCT
block, quantizes one coefficient at a time, and uses a small iterative divider
for the rounded coefficient/table division. This removes the previous
64-coefficient combinational divider fanout at the cost of additional cycles per
block.

`JpegHeaderStage` also avoids driving AXI output bytes directly from the
quality-scaled quantization table arithmetic. It emits ordinary marker bytes
through a small output FSM and prepares DQT payload bytes over multiple cycles
before presenting them on the decoupled byte stream.

## Source Layout

- `HjpegConfig.scala`: static widths and JPEG/KV260-facing constants
- `HjpegBundles.scala`: frame, pixel, byte, and AXI stream bundles
- `HjpegCore.scala`: raster RGB to JPEG byte-stream core
- `HjpegAxiStreamCore.scala`: raster RGB AXI stream wrapper
- `HjpegKv260Top.scala`: KV260-oriented elaboration wrapper
- `HjpegKv260AxiLiteTop.scala`: KV260-oriented AXI-Lite control/status wrapper
- `Elaborate.scala`: SystemVerilog generation entry points

## KV260 Integration Direction

The hardware-facing boundary is an AXI4-Stream-shaped RGB input and byte output.
`HjpegKv260AxiLiteTop` adds a small AXI-Lite register map for frame dimensions,
quality, restart interval, chroma mode, JFIF marker emission, and status.
The internal `HjpegAxiStreamCore` RGB stream is 24 bits wide, but the KV260
wrappers expose a DMA-compatible 32-bit input stream. Input bytes 0, 1, and 2
are R, G, and B; byte 3 is ignored; and the low three `keep` bits must be set
for every pixel. Malformed input words raise the sticky protocol-error status.
Frames that start with unsupported dimensions are drained to input TLAST without
feeding the JPEG core, then a clear pulse permits the next valid frame to start.
The AXI-Lite control wrapper captures write address and data independently and
applies byte write strobes for host register updates.
The AXI stream wrapper snapshots the full frame configuration on the first input
pixel and holds it through the matching JPEG output frame, so register writes
take effect on the next frame. Wrapper equivalence tests compare its output
bytes against direct `HjpegCore` output for both the default 4:4:4 path and a
configured 4:2:0/restart/no-JFIF path, and protocol tests cover draining a
multi-beat unsupported input frame through TLAST before recovery.

The current tops are not full Vivado block designs. They are named RTL tops that
can be elaborated and wrapped in platform-specific IP packaging. Board-level
clocking, reset synchronization, DMA connection, interrupts, and bitstream
validation still need Vivado/KV260 work.

## Vivado Collateral

Tracked scripts under `scripts/vivado/` provide the first reproducible
hardware-tool entry points:

- `synth_kv260_axi_lite.tcl` creates a Vivado project for
  `HjpegKv260AxiLiteTop`, reads `generated-kv260-axi-lite-top/filelist.f`, runs
  synthesis for `xck26-sfvc784-2LV-c`, and writes utilization/timing reports
  plus `post_synth.dcp`.
- `package_kv260_axi_lite_ip.tcl` packages the same RTL as reusable Vivado IP
  with explicit clock, reset, AXI-Lite, and AXI-stream bus-interface port maps.
  The packaged AXI-Lite interface exposes a 4 KiB register aperture for the
  control/status map.
- `create_kv260_block_design.tcl` creates a first Vivado block design that
  instantiates the Zynq UltraScale+ PS, AXI DMA, SmartConnect, reset logic, and
  packaged `hjpeg_kv260_axi_lite` IP. DMA MM2S drives the RGB input stream and
  DMA S2MM receives the JPEG byte stream. The script assigns addresses,
  validates and saves the block design, generates the HDL wrapper, and refreshes
  compile order.
- `build_kv260_bitstream.tcl` opens that block-design project, runs synthesis
  and implementation through `write_bitstream`, emits timing/utilization
  reports, copies `hjpeg_kv260.bit`, and exports `hjpeg_kv260.xsa` with the
  bitstream included.
- `check_reports.py` hashes generated artifacts, parses Vivado
  timing/utilization/DRC/route-status reports, requires requested
  clock-utilization reports, and fails when a requested artifact is missing or
  empty,
  setup WNS is below the requested threshold, hold WHS is below the requested
  threshold for reports passed with `--hold-timing`, any utilization row
  exceeds the configured percentage, DRC reports Error or Critical Warning
  violations, route status reports unrouted nets or routing errors, or a
  required clock-utilization report is missing or empty. Its `--json` mode emits
  artifact/report SHA-256 hex hashes, byte lengths, target clock
  period/frequency, parsed WNS/WHS values, utilization rows, thresholds, DRC
  violations, route-status counts, clock-utilization report hashes, requested
  input path lists and gate values, checked report/artifact count, per-category
  checked counts, required evidence category presence, per-category
  passing/failing counts, missing category names, required `.bit`/`.xsa`
  artifact suffix presence, required suffix passing/failing counts, aggregate
  pass/fail counts, required/present/missing category and suffix counts,
  diagnostic failure count, passed/failed path lists, and pass/fail state for
  build evidence logs. Required evidence category presence is based on at least
  one passing record in that category, not just a requested input path. Complete
  Vivado evidence counts only records whose `passed` field is an actual JSON
  boolean `true`. Missing or unparseable reports are included as structured
  failure records. Full bitstream gates can pass `--require-complete-evidence`
  to fail unless all required categories and `.bit`/`.xsa` artifact suffixes
  are present.

These scripts are intended to be run after:

```sh
sbt 'runMain hjpeg.ElaborateKv260AxiLiteTop'
```

They are not a replacement for board constraints, software drivers, boot-image
packaging, or on-board validation.

## Host-Side Flow

`scripts/host/hjpeg_host.py` provides the first userspace helpers around the
KV260 design. It can generate deterministic non-flat P6 PPM fixtures, packs
binary P6 PPM files into 32-bit-per-pixel RGB stream beats for the AXI DMA MM2S
channel, optionally validates during `run-stream-devices` that the dimensions
of a saved source PPM match the configured frame and that its packed bytes match
the RGB stream sent to the TX device, writes the encoder AXI-Lite
configuration/status registers via
`/dev/mem`, and validates returned JPEG files by checking SOI/EOI markers, SOF0
dimensions, 8-bit sample precision, three-component frame shape, DQT/DHT table
markers, optional JFIF APP0 signature and fixed fields, optional DRI restart
interval, exactly one SOF0 and SOS, non-empty entropy-coded scan data, stuffed
entropy `0xff` byte count, unstuffed scan-data SHA-256, rejection of unsupported
header markers, rejection of malformed, non-JFIF, or duplicate APP0 markers,
rejection of unexpected
non-RST/non-EOI markers after SOS, and rejection of trailing bytes after EOI. It
also records SOF0 component sampling factors, APP0 and JFIF APP0 counts, grouped
marker counts, DQT/DHT table IDs, DQT and DHT table order, table payload byte
counts and SHA-256 hashes, SOS component table selectors, and parsed JFIF APP0
version/density/thumbnail fields, requires SOF0
and SOS component IDs to be `[1, 2, 3]`, requires the SOS component list to match
SOF0 exactly, requires SOF0 quantization table selectors to be Y `0` and Cb/Cr
`1`, requires SOS table selectors to be Y `0/0` and Cb/Cr `1/1`, requires
nonzero SOF0 dimensions, requires baseline SOS spectral fields `0/63/0`,
requires DQT IDs `{0, 1}` in table order `[0, 1]`, DHT table order DC0, DC1,
AC0, AC1, 8-bit DQT precision, and exact DQT/DHT segment counts,
rejects nonstandard DHT table sets and duplicate DQT/DHT table definitions,
rejects zero-valued DQT entries, rejects empty, oversized,
oversubscribed, or invalid baseline DHT tables and dangling table references,
rejects unsupported header markers, rejects malformed, non-JFIF, or duplicate
APP0 markers, requires the encoder's baseline marker order of optional
APP0/JFIF, DQT, SOF0, DHT, optional DRI, SOS, entropy, and EOI, records the
parsed marker sequence, RST marker sequence, and MCU count in JSON evidence,
rejects RST markers without DRI or out-of-sequence RST markers, and requires the
SOF0 sampling factors to be nonzero and match the supported 4:4:4 or 4:2:0
modes. The JSON validation expectations also record the expected marker counts,
expected marker order through SOS and the terminal EOI marker,
SOF0 sample precision, component count, component quantization selectors,
optional SOF0 sampling factors, SOS component table selectors, baseline SOS
spectral fields, minimum scan-data length, DQT/DHT table order, expected chroma
mode when checked, expected JFIF APP0 baseline fields when JFIF is required,
expected restart marker count and RST sequence, and expected DQT/DHT payload
hashes when table checks are enabled. The helper
can also run an external JPEG decoder command with a configurable timeout and
bounded stdout/stderr capture and records the resolved argv so decoder-open
evidence, elapsed seconds, captured output lengths, and capture limit are
captured in the same transcript without risking a hung or oversized validation
run. Standalone
validation can require an expected restart interval, exact RST marker count for
the parsed MCU count, chroma/JFIF mode, quality-matched standard DQT payloads,
and standard DHT payloads; standalone JSON evidence records the expectations
that were enforced, including expected marker counts, the derived expected RST
marker count, and expected DQT/DHT payload hashes when table checks are enabled.
`run-stream-devices` checks the configured restart
interval, chroma mode, JFIF setting, quality-scaled DQT payloads, and standard
DHT payloads against the captured JPEG automatically and records those
expectations in the run JSON evidence. Run JSON evidence also records detailed
input RGB byte-length expectations, whether the actual byte length matched,
AXI-Lite status checkpoints, the checkpoint count, the ordered checkpoint
context list, expected context list, context-list match result, and run-level
all-idle/any-busy/any-protocol-error summaries. It also records a
`hardware_run_summary` with evidence-presence bits and consolidated pass/fail
checks, evidence/check counts, missing evidence group names, and failing check
names for board-run transcripts; the complete-evidence flag requires a passing
decoder check, hashed output JPEG evidence with non-empty scan data, source PPM
evidence with non-flat/color image stats, and positive host-observed transfer
timing with finite positive derived input and output byte rates. Decoder
evidence must include the command string, resolved argv, positive timeout,
nonnegative elapsed time, zero return code, bounded stdout/stderr strings with
matching captured lengths, a positive capture limit, non-truncated captured
output metadata, and an argv list matching the command and JPEG path. Frame
dimensions are cross-checked across the output JPEG,
encoder configuration, validation expectations, source PPM, and expected RGB
stream byte length, and the parsed marker sequence must begin with SOI and end
with EOI. Input RGB evidence must
include positive byte length, a SHA-256 hex hash, a positive expected byte length,
and an actual-vs-expected length match. Capture configuration evidence must
include a positive maximum output byte count and either no timeout or a finite
positive timeout. AXI-Lite target evidence must include a device path,
nonnegative base address, and matching hexadecimal base-address text. Encoder
configuration evidence must include supported dimensions, quality/restart values
in range, boolean control flags, and a control word/hex string matching those
flags. Validation expectations evidence must include the baseline shape, marker
order, table order, SOS spectral fields, and standard-Huffman requirement.
Source PPM evidence must include file and packed-RGB SHA-256 hex hashes,
dimension-consistent RGB and packed byte lengths, an input-byte match, and
non-flat/color image stats. Status evidence must include the detailed
checkpoint list, matching checkpoint count, expected ordered contexts, zero raw
status words, and all checkpoints idle with no protocol error or busy state.
Summary checks recompute checkpoint order and aggregate idle/error/busy flags
from the detailed status records. They also recompute RGB byte-count matches,
PPM-to-input-RGB consistency, and transfer byte rates from the saved lengths,
hashes, and elapsed time.
The summary records total, present, and missing evidence-group counts, total,
passing, and failing check counts, missing evidence group names, and failing
check names for review.
Required boolean evidence fields must be actual JSON booleans.
For final board transcripts, pass `run-stream-devices --require-complete-evidence`
so missing evidence groups turn into a nonzero CLI result; omit it for partial
smoke tests. Run JSON records whether complete evidence was required and which
evidence groups were missing. Saved run JSON can be checked later with
`check-run-evidence`, which fails on malformed JSON, missing
`hardware_run_summary`, a stored summary that does not match recomputed
evidence, failed recorded checks, or incomplete hardware evidence. JSON output
includes aggregate checked/pass/fail transcript counts, diagnostic failure
count, and passed/failed path lists plus the recomputed summary, evidence/check
counts, missing evidence groups, and failing check names for each object-shaped
transcript.
The `run-stream-devices` command supports Linux board images that expose DMA
MM2S/S2MM endpoints as byte-stream device files by writing padded RGB bytes to
the TX device and reading JPEG bytes from the RX device until EOI, while
rejecting trailing bytes already returned after that EOI.
