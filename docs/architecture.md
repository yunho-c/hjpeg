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
cycles before presenting it to the DCT/quantization path. This keeps the stripe
memories to one read and one write port per component in generated RTL, which is
more compatible with FPGA block RAM inference than fanning out a whole MCU as
combinational memory reads.

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
- `check_reports.py` parses Vivado timing/utilization reports and fails when
  WNS is below the requested threshold or any utilization row exceeds the
  configured percentage.

These scripts are intended to be run after:

```sh
sbt 'runMain hjpeg.ElaborateKv260AxiLiteTop'
```

They are not a replacement for board constraints, software drivers, boot-image
packaging, or on-board validation.

## Host-Side Flow

`scripts/host/hjpeg_host.py` provides the first userspace helpers around the
KV260 design. It packs binary P6 PPM files into 32-bit-per-pixel RGB stream
beats for the AXI DMA MM2S channel, writes the encoder AXI-Lite
configuration/status registers via `/dev/mem`, and validates returned JPEG files
by checking SOI/EOI markers and SOF0 dimensions. The `run-stream-devices`
command also supports Linux board images that expose DMA MM2S/S2MM endpoints as
byte-stream device files by writing padded RGB bytes to the TX device and
reading JPEG bytes from the RX device until EOI.
