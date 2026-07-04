# hjpeg Architecture

`hjpeg` is intended to become a complete hardware JPEG encoder. The first
scaffold keeps the boundaries stable while the compression stages are built
incrementally.

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

Only the stream shell and smoke-test payload path exist today. The placeholder
payload emits one luma-like byte per input pixel; it is not a valid JPEG
bitstream.

## Source Layout

- `HjpegConfig.scala`: static widths and JPEG/KV260-facing constants
- `HjpegBundles.scala`: frame, pixel, byte, and AXI stream bundles
- `HjpegCore.scala`: initial flow-control shell
- `HjpegAxiStreamCore.scala`: raster RGB AXI stream wrapper
- `HjpegKv260Top.scala`: KV260-oriented elaboration wrapper
- `Elaborate.scala`: SystemVerilog generation entry points

## KV260 Integration Direction

The first hardware-facing boundary is an AXI4-Stream-shaped RGB input and byte
output. This matches the handoff style used by small PL accelerators on Zynq
UltraScale+ MPSoC boards and leaves room for a future AXI-Lite control/status
wrapper around `FrameConfig`.

The current `HjpegKv260Top` is not a Vivado block design. It is a named RTL top
that can be elaborated and then wrapped in platform-specific IP packaging once
the encoder pipeline has enough stable behavior to integrate.
