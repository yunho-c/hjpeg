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
python3 scripts/vivado/check_reports.py \
  --artifact build/vivado/hjpeg-kv260-artifacts/hjpeg_kv260.bit \
  --artifact build/vivado/hjpeg-kv260-artifacts/hjpeg_kv260.xsa \
  --timing build/vivado/hjpeg-kv260-artifacts/post_synth_timing_summary.rpt \
  --timing build/vivado/hjpeg-kv260-artifacts/post_impl_timing_summary.rpt \
  --hold-timing build/vivado/hjpeg-kv260-artifacts/post_impl_timing_summary.rpt \
  --utilization build/vivado/hjpeg-kv260-artifacts/post_synth_utilization.rpt \
  --utilization build/vivado/hjpeg-kv260-artifacts/post_impl_utilization.rpt \
  --json
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
- The expected bitstream and XSA artifacts exist and are recorded in the JSON
  evidence.
- Post-implementation timing has nonnegative setup WNS and hold WHS for the
  target clock.
- Resource use leaves enough headroom for the intended KV260 platform shell.
- `check_reports.py` exits successfully for the generated timing/utilization
  reports, with hold timing gated on the post-implementation timing report.
- The JSON evidence records artifact/report paths, byte lengths, SHA-256
  hashes, parsed setup WNS and hold WHS values, utilization rows, thresholds,
  and pass/fail state.

Latest local Vivado 2026.1 evidence:

- `build_kv260_bitstream.tcl` completed and wrote `hjpeg_kv260.bit` and
  `hjpeg_kv260.xsa`.
- `check_reports.py` passed on post-synthesis and post-implementation timing
  and utilization reports.
- Latest post-implementation timing is setup WNS `+0.131 ns` and hold WHS
  `+0.010 ns` at the 100 MHz target clock.
- Latest post-implementation utilization is approximately 50,662 CLB LUTs
  (43.26%), 25,619 LUTRAMs (44.48%), 2 BRAM tiles (1.39%), and 17 DSPs
  (1.36%).

## 5. Prepare Host Input

```sh
python3 scripts/host/hjpeg_host.py make-test-ppm input.ppm --width WIDTH --height HEIGHT --json
python3 scripts/host/hjpeg_host.py pack-ppm input.ppm input.rgb --json
```

Expected evidence:

- `input.ppm` is a deterministic non-flat/color test pattern, or another known
  binary P6 PPM fixture with recognizable visual content.
- `input.rgb` size is exactly `width * height * 4` bytes: R, G, B, and one
  ignored padding byte per pixel.
- The JSON evidence records input/output paths, dimensions, byte lengths, and
  SHA-256 hashes for the PPM fixture and packed RGB stream.
- The input image dimensions are within the configured `HjpegConfig` maximums.
  The host helper defaults to the current KV260 top limit of `1920x1080`; use
  `--max-width` and `--max-height` only for a custom elaboration with different
  limits, and keep those values in the saved JSON evidence.

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
  --height HEIGHT \
  --json
python3 scripts/host/hjpeg_host.py status --base-addr 0xa0000000 --json
```

Adjust the `--tx-device` and `--rx-device` paths to match the loaded board
image. Drivers that expose AXI DMA through ioctls or descriptor queues need a
small adapter, but should reuse the same packing, register, and validation
helpers.

Expected evidence:

- Status is `idle` before the transfer starts.
- Status returns to `idle` after the transfer completes.
- `protocol_error` is never reported for the valid frame.
- JSON evidence records the AXI-Lite target and encoder configuration used for
  the run, including the frame limits checked by the host helper.
- Standalone `status --json` evidence records the raw status word, decoded
  `busy` and `protocol_error` flags, and text state.
- The captured output starts with SOI and ends with EOI.

The `run-stream-devices` helper checks the AXI-Lite status register after
configuration, immediately before streaming input, and after validating the
captured JPEG. It exits with an error if `busy` or `protocol_error` is set at
any of those points.

## 7. Validate JPEG Output

```sh
python3 scripts/host/hjpeg_host.py validate-jpeg output.jpg --width WIDTH --height HEIGHT
```

If a standard JPEG decoder is available on the host, include it in the helper
run:

```sh
python3 scripts/host/hjpeg_host.py validate-jpeg output.jpg \
  --width WIDTH \
  --height HEIGHT \
  --decoder-command 'magick identify {jpeg}'
```

Use `--json` with `make-test-ppm`, `pack-ppm`, `config`, `status`,
`validate-jpeg`, or `run-stream-devices` when saving evidence for automation or
later comparison.

Expected evidence:

- The helper reports valid baseline JPEG dimensions and the number of
  entropy-coded scan data bytes, proving the file contains an SOS marker with
  non-empty scan payload.
- The helper records APP0, DQT, DHT, and restart-marker counts. At least one
  DQT and one DHT segment are required for a standalone baseline JPEG.
- The helper reports the total JPEG byte length and SHA-256 so the captured
  artifact can be matched against saved files and logs.
- For `run-stream-devices`, the helper reports the input RGB stream byte length
  and SHA-256 so the output can be tied to the exact input payload.
- For `run-stream-devices --json`, the helper records the AXI-Lite status
  checkpoints enforced after configuration, before transfer, and after
  transfer.
- A standard JPEG decoder can open `output.jpg`; when `--decoder-command` is
  used, that decoder check and command string are part of the JSON evidence.
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
