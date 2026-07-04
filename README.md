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

RGB AXI-stream input words pack R, G, and B in the low three bytes and must
present `keep = 0b111` for every pixel. A partial input word is accepted to
avoid wedging the stream, but raises the sticky protocol-error flag.

The AXI-Lite control wrapper accepts independent AW and W channel handshakes and
honors byte write strobes on writable registers.

## Requirements

- JDK 21 or newer
- sbt, or the checked-in Mill bootstrap script
- Verilator for simulator-backed tests

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

These Vivado scripts consume `generated-kv260-axi-lite-top/filelist.f`. Generate
the AXI-Lite top first. They do not create a complete KV260 block design or
bitstream.

## Versions

- Scala 2.13.18
- Chisel 7.13.0
- ScalaTest 3.2.19
- sbt 1.12.13
- Mill 1.1.7

## License

GPLv3. See [LICENSE](LICENSE).
