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
take effect on the next frame.

The current tops are not full Vivado block designs. They are named RTL tops that
can be elaborated and wrapped in platform-specific IP packaging. Board-level
clocking, reset synchronization, DMA connection, interrupts, and bitstream
validation still need Vivado/KV260 work.

## Vivado Collateral

Tracked scripts under `scripts/vivado/` provide the first reproducible
hardware-tool entry points:

- `synth_kv260_axi_lite.tcl` creates a Vivado project for
  `HjpegKv260AxiLiteTop`, reads `generated-kv260-axi-lite-top/filelist.f`, runs
  synthesis for `xck26-sfvc784-2LV-c`, and writes utilization/timing reports.
- `package_kv260_axi_lite_ip.tcl` packages the same RTL as reusable Vivado IP
  with explicit clock, reset, AXI-Lite, and AXI-stream bus-interface port maps.
  The packaged AXI-Lite interface exposes a 4 KiB register aperture for the
  control/status map.
- `create_kv260_block_design.tcl` creates a first Vivado block design that
  instantiates the Zynq UltraScale+ PS, AXI DMA, SmartConnect, reset logic, and
  packaged `hjpeg_kv260_axi_lite` IP. DMA MM2S drives the RGB input stream and
  DMA S2MM receives the JPEG byte stream.
- `build_kv260_bitstream.tcl` opens that block-design project, runs synthesis
  and implementation through `write_bitstream`, emits timing/utilization
  reports, copies `hjpeg_kv260.bit`, and exports `hjpeg_kv260.xsa` with the
  bitstream included.
- `check_reports.py` hashes generated artifacts, parses Vivado
  timing/utilization reports, and fails when a requested artifact is missing,
  setup WNS is below the requested threshold, hold WHS is below the requested
  threshold for reports passed with `--hold-timing`, or any utilization row
  exceeds the configured percentage. Its `--json` mode emits artifact/report
  hashes, byte lengths, parsed WNS/WHS values, utilization rows, thresholds, and
  pass/fail state for build evidence logs.

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
channel, writes the encoder AXI-Lite configuration/status registers via
`/dev/mem`, and validates returned JPEG files by checking SOI/EOI markers, SOF0
dimensions, 8-bit sample precision, three-component frame shape, DQT/DHT table
markers, optional JFIF APP0 signature, optional DRI restart interval, exactly
one SOF0 and SOS, and non-empty entropy-coded scan data. It also records SOF0
component sampling factors, APP0 and JFIF APP0 counts, DQT/DHT table IDs, and
SOS component table selectors, requires SOF0 and SOS component IDs to be
`[1, 2, 3]`, requires the SOS component list to match SOF0 exactly, requires
baseline SOS spectral fields `0/63/0`, requires DQT IDs `{0, 1}` with 8-bit
precision and exact DQT/DHT segment counts, rejects nonstandard DHT table sets,
rejects dangling table references, requires the encoder's baseline marker order
of optional APP0/JFIF, DQT, SOF0, DHT, optional DRI, SOS, entropy, and EOI,
records the parsed marker sequence and MCU count in JSON evidence, and requires
the SOF0 sampling factors to match the supported 4:4:4 or 4:2:0 modes. The
helper
can also run an external JPEG decoder command so decoder-open evidence is
captured in the same transcript. Standalone validation can require an expected
restart interval, exact RST marker count for the parsed MCU count, and
chroma/JFIF mode, and `run-stream-devices` checks the configured restart
interval, chroma mode, and JFIF setting against the captured JPEG automatically.
The `run-stream-devices` command supports Linux board images that expose DMA
MM2S/S2MM endpoints as byte-stream device files by writing padded RGB bytes to
the TX device and reading JPEG bytes from the RX device until EOI.
