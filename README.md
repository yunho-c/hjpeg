# hjpeg

Hardware-accelerated JPEG encoder in Chisel.

The initial target platform is the AMD/Xilinx Kria KV260. The current tree is a
scaffold modeled after the early `hjxl` Chisel setup: Scala/Chisel build files,
CI, a streaming RTL shell, elaboration entry points, and simulator tests.

## Goals

- Baseline JPEG encoder datapath in synthesizable Chisel
- Raster RGB input stream
- FPGA-friendly streaming output path
- KV260-oriented top-level elaboration target
- Incremental test fixtures for each pipeline stage

## Current RTL Shape

The first checked-in core is intentionally small. It defines the frame
configuration, RGB pixel stream, encoded byte stream, AXI4-Stream-shaped shell,
and KV260 top-level wrapper. The payload path currently emits a simple luma byte
for each accepted RGB pixel so that flow control, frame boundaries, and
elaboration are testable before the JPEG stages land.

Planned pipeline stages:

1. RGB to YCbCr conversion
2. MCU/block buffering and chroma subsampling
3. 8x8 DCT
4. Quantization
5. Zig-zag ordering and run-length coding
6. Huffman entropy coding
7. JFIF/JPEG marker and scan assembly
8. AXI stream and KV260 host handoff

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

## Versions

- Scala 2.13.18
- Chisel 7.13.0
- ScalaTest 3.2.19
- sbt 1.12.13
- Mill 1.1.7

## License

GPLv3. See [LICENSE](LICENSE).
