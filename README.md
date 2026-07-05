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

Check generated timing and utilization reports:

```sh
python3 scripts/vivado/check_reports.py \
  --timing build/vivado/hjpeg-kv260-artifacts/post_impl_timing_summary.rpt \
  --utilization build/vivado/hjpeg-kv260-artifacts/post_impl_utilization.rpt
```

These Vivado scripts consume `generated-kv260-axi-lite-top/filelist.f`. Generate
the AXI-Lite top first. The IP packaging script maps the generated clock, reset,
AXI-Lite, and AXI-stream ports onto Vivado bus interfaces and exposes a 4 KiB
AXI-Lite register aperture. The block-design script consumes the packaged IP and
wires it to Zynq UltraScale+ PS, AXI DMA, SmartConnect, and reset/interrupt
plumbing. The bitstream script runs synthesis and implementation, writes
post-synthesis/post-implementation utilization and timing reports, copies the
bitstream, and exports a hardware platform XSA. These scripts do not create a
complete bootable KV260 image or prove on-board behavior.

See `docs/kv260-bringup.md` for the end-to-end evidence checklist before calling
the hardware path complete.

For a new agent or developer taking over without project history, read
`docs/handoff.md` first. It summarizes current implementation status, recent
verification, known blockers, and the recommended next steps for Vivado/KV260
bring-up.

## Host Helpers

The host utility prepares payloads and register writes for the KV260 AXI-Lite /
AXI DMA design:

```sh
python3 scripts/host/hjpeg_host.py pack-ppm input.ppm input.rgb
python3 scripts/host/hjpeg_host.py config --base-addr 0xa0000000 --width 640 --height 480
python3 scripts/host/hjpeg_host.py status --base-addr 0xa0000000
python3 scripts/host/hjpeg_host.py run-stream-devices \
  --base-addr 0xa0000000 \
  --tx-device /dev/hjpeg-mm2s \
  --rx-device /dev/hjpeg-s2mm \
  --input-rgb input.rgb \
  --output-jpeg output.jpg \
  --width 640 \
  --height 480
python3 scripts/host/hjpeg_host.py validate-jpeg output.jpg --width 640 --height 480
```

`pack-ppm` accepts binary P6 PPM and writes one 32-bit little-endian stream beat
per pixel: R, G, B, and one ignored zero byte. `run-stream-devices` targets
Linux board images that expose AXI DMA MM2S/S2MM endpoints as byte-stream device
files: it configures AXI-Lite registers through `/dev/mem`, writes the padded
RGB stream to the TX device, captures bytes from the RX device until JPEG EOI,
and validates the resulting dimensions. DMA drivers that use ioctls or buffer
queues still need a small adapter around the same host-side packing and
validation helpers.

## Versions

- Scala 2.13.18
- Chisel 7.13.0
- ScalaTest 3.2.19
- sbt 1.12.13
- Mill 1.1.7

## License

GPLv3. See [LICENSE](LICENSE).
