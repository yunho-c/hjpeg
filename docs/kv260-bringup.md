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

## 2. Package IP

```sh
vivado -mode batch -source scripts/vivado/package_kv260_axi_lite_ip.tcl
```

Expected evidence:

- `build/vivado/ip_repo/hjpeg_kv260_axi_lite_1_0/component.xml`
- Vivado IP packager completes without critical warnings about unmapped clock,
  reset, AXI-Lite, or AXI-stream interfaces.

## 3. Create Block Design

```sh
vivado -mode batch -source scripts/vivado/create_kv260_block_design.tcl
```

Expected evidence:

- `build/vivado/hjpeg-kv260-bd/hjpeg_kv260_bd.xpr`
- `validate_bd_design` completes successfully.
- The design contains Zynq UltraScale+ PS, AXI DMA, SmartConnect, reset logic,
  interrupt concat, and one `hjpeg_kv260_axi_lite` instance.

## 4. Build Bitstream and XSA

```sh
vivado -mode batch -source scripts/vivado/build_kv260_bitstream.tcl
```

Expected evidence:

- `build/vivado/hjpeg-kv260-artifacts/hjpeg_kv260.bit`
- `build/vivado/hjpeg-kv260-artifacts/hjpeg_kv260.xsa`
- `post_synth_utilization.rpt`
- `post_synth_timing_summary.rpt`
- `post_impl_utilization.rpt`
- `post_impl_timing_summary.rpt`

Pass criteria:

- Synthesis and implementation finish successfully.
- Post-implementation timing has nonnegative worst negative slack for the
  target clock.
- Resource use leaves enough headroom for the intended KV260 platform shell.

## 5. Prepare Host Input

```sh
python3 scripts/host/hjpeg_host.py pack-ppm input.ppm input.rgb
```

Expected evidence:

- `input.rgb` size is exactly `width * height * 3` bytes.
- The input image dimensions are within the configured `HjpegConfig` maximums.

## 6. Configure and Run Hardware

Program the KV260 with the generated bitstream and load a board image or driver
stack that exposes AXI DMA MM2S/S2MM transfers as byte-stream device files.
Then run:

```sh
python3 scripts/host/hjpeg_host.py run-stream-devices \
  --base-addr 0xa0000000 \
  --tx-device /dev/hjpeg-mm2s \
  --rx-device /dev/hjpeg-s2mm \
  --input-rgb input.rgb \
  --output-jpeg output.jpg \
  --width WIDTH \
  --height HEIGHT
python3 scripts/host/hjpeg_host.py status --base-addr 0xa0000000
```

Adjust the `--tx-device` and `--rx-device` paths to match the loaded board
image. Drivers that expose AXI DMA through ioctls or descriptor queues need a
small adapter, but should reuse the same packing, register, and validation
helpers.

Expected evidence:

- Status is `idle` before the transfer starts.
- Status returns to `idle` after the transfer completes.
- `protocol_error` is never reported for the valid frame.
- The captured output starts with SOI and ends with EOI.

## 7. Validate JPEG Output

```sh
python3 scripts/host/hjpeg_host.py validate-jpeg output.jpg --width WIDTH --height HEIGHT
```

Expected evidence:

- The helper reports valid baseline JPEG dimensions.
- A standard JPEG decoder can open `output.jpg`.
- The decoded dimensions match the input.
- Visual content is recognizable for non-flat test images.

## Completion Bar

Do not mark the project complete until the repo has current evidence for:

- Full simulator suite passing.
- Vivado IP packaging passing.
- Block design validation passing.
- Bitstream and XSA generation passing.
- Post-implementation timing/resource reports reviewed.
- A real KV260 run producing at least one decodable JPEG through AXI DMA.
- Host-side validation passing for that hardware-produced JPEG.
