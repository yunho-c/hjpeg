# KV260 4K60 Architecture

## Target Contract

The `4k60` branch targets:

- 3840x2160 raster RGB input at 60 complete frames per second;
- runtime-selectable baseline JPEG 4:4:4 and 4:2:0;
- deterministic `gradient-checker`, quality 85, JFIF enabled, and no restart
  interval as the minimum performance benchmark;
- decoder-valid dimensions, marker/table structure, entropy stuffing, and
  recognizable content before throughput counts;
- a 150 MHz architectural clock goal with four packed RGB pixels accepted per
  cycle, subject to routed Vivado proof; and
- no tracked fabric resource above the existing project evidence ceiling.

Quality-90 `seeded-random` remains a stress case, not a content-independent
60-fps guarantee. Performance evidence must name the fixture and sampling mode.

## Reproducible Capacity Budget

Run:

```sh
python3 scripts/dev/analyze_4k60_capacity.py
python3 scripts/dev/analyze_4k60_capacity_test.py
```

At 100 MHz, 4K60 requires 497.664 Mpixel/s, 4.97664 accepted pixels per cycle,
and 1.990656 GB/s of packed 32-bit RGB traffic. One frame contains 8,294,400
pixels and 33,177,600 packed bytes, which still fits the existing 26-bit AXI DMA
length field.

| Mode | MCUs/frame | Blocks/frame | Blocks/s | Transform copies at II=16, 100 MHz | Scaled-current speedup |
| --- | ---: | ---: | ---: | ---: | ---: |
| 4:2:0 | 32,400 | 194,400 | 11.664M | 2 | 5.306x |
| 4:4:4 | 129,600 | 388,800 | 23.328M | 4 | 7.739x |

Four input pixels per cycle need at least 124.416 MHz before backpressure. The
150 MHz goal provides 600 Mpixel/s raw ingress capacity. At that clock, two
II=16 transform pipelines cover 4:2:0 and three cover 4:4:4. Those are capacity
floors, not complete-frame predictions; raster handoff, entropy coding, markers,
and DMA stalls share the same budget.

## UHD Elaboration and Baseline Synthesis

Generate UHD RTL without changing the proven Full-HD default:

```sh
sbt 'runMain hjpeg.ElaborateKv2604k60AxiLiteTop'
vivado -mode batch -source scripts/vivado/synth_kv260_axi_lite.tcl \
  -tclargs generated-kv260-4k60-axi-lite-top \
  build/vivado/hjpeg-kv260-4k60-unified
```

`HjpegTargetConfigs.Kv260Uhd4k` sets maximum dimensions to 3840x2160. The UHD
elaboration uses four RGB pixels per beat, giving it a 128-bit DMA input while
the Full-HD/default elaborations retain their existing 32-bit input.

The first unmodified dual-raster UHD synthesis proved the predicted memory
blocker: 144/144 BRAM tiles (100%), 127 DSPs, 32,789 LUTs, 48,912 registers, and
post-synthesis WNS `+1.103 ns` at 100 MHz. It could synthesize but failed the
resource gate and left no BRAM for integration.

## Shared Raster/Transform Slice

`JpegUnifiedRasterToMcuStage` replaces the two production raster instances with
one two-slot, 16-row banked store and one block transform:

- 4:2:0 emits one row of 16x16 MCUs per stored band;
- 4:4:4 emits the top and bottom 8-row MCU stripes in raster order;
- partial right/bottom edges preserve the existing replication rules; and
- the legacy standalone raster stages remain independently tested but are no
  longer instantiated in `HjpegCore`.

Exact UHD post-synthesis evidence after this slice is:

| Resource/timing | Dual-raster baseline | Unified current | Change |
| --- | ---: | ---: | ---: |
| CLB LUTs | 32,789 (28.00%) | 21,036 (17.96%) | -11,753 |
| Registers | 48,912 (20.88%) | 31,405 (13.41%) | -17,507 |
| BRAM tiles | 144 (100.00%) | 97 (67.36%) | -47 |
| DSPs | 127 (10.18%) | 64 (5.13%) | -63 |
| Post-synthesis WNS at 100 MHz | +1.103 ns | +1.103 ns | unchanged |

The current 16x16 4:4:4 complete-frame regression is 2,267 cycles versus the
previous 2,020 because shared 4:4:4 loading reads four rather than eight samples
per cycle and waits for a 16-row band. This is accepted only as an intermediate
resource-enabling step; the 4K60 datapath still requires vectorized collection.

Exact-current verification for this slice is 142/142 Scala tests across 27
suites, including complete decoder-backed core/wrapper coverage and two focused
unified-raster mode tests. The capacity-model suite has five passing tests.
The regenerated quick profiler emits the same decoder-valid byte counts and
measures 3,094 cycles for its 32x16 4:4:4 fixture and 2,526 cycles for 4:2:0;
steady 4:4:4 MCU spacing is 139 cycles. These small-frame numbers diagnose the
shared loader and are not a 4K60 extrapolation.

## Four-Pixel Raster Ingress Slice

`HjpegGroupedCore` and `HjpegAxiStreamCore` now accept one to four adjacent RGB
pixels per beat. The UHD top selects four lanes. Each external 128-bit beat is
four little-endian 32-bit pixel words: each word carries R/G/B in its low three
bytes, its high byte is ignored, and its low three `TKEEP` bits must be set.
The host's existing four-byte-per-pixel packed files therefore need no format
conversion. Widths on the vector path must be divisible by four; malformed or
unsupported frames retain the existing drain-to-TLAST recovery behavior.

The shared raster store writes the four adjacent pixels to four distinct
column banks in one cycle. Four independent fixed-point RGB-to-YCbCr converters
feed those writes. Decoder-backed simulation covers non-MCU-aligned 12x10
frames in both 4:4:4 and 4:2:0, so lane ordering and right/bottom edge padding
are checked together.

Exact UHD post-synthesis evidence for the 128-bit top at 100 MHz is:

| Resource/timing | Unified scalar input | Unified four-pixel input | Change |
| --- | ---: | ---: | ---: |
| Logic LUTs | 21,036 (17.96%) | 21,634 (18.47%) | +598 |
| Registers | 31,405 (13.41%) | 31,401 (13.41%) | -4 |
| BRAM tiles | 97 (67.36%) | 97 (67.36%) | unchanged |
| DSPs | 64 (5.13%) | 76 (6.09%) | +12 |
| Post-synthesis WNS at 100 MHz | +1.103 ns | +1.103 ns | unchanged |

The standalone synthesis reports 215/189 bonded I/O sites because it treats
all AXI signals as package pins. They are internal connections in the PS/DMA
block design, so routed block-design utilization is the applicable I/O gate.
Create the matching 128-bit DMA design with:

```sh
vivado -mode batch -source scripts/vivado/package_kv260_axi_lite_ip.tcl \
  -tclargs generated-kv260-4k60-axi-lite-top build/vivado/ip_repo-4k60
vivado -mode batch -source scripts/vivado/create_kv260_block_design.tcl \
  -tclargs build/vivado/ip_repo-4k60 build/vivado/hjpeg-kv260-4k60-bd 128
```

Vivado 2026.1 successfully packaged this UHD IP and validated the 128-bit DMA
block design. The generated design records `c_m_axis_mm2s_tdata_width = 128`
and preserves the expected encoder/DMA AXI-Lite address map. This is interface
construction evidence; implementation timing and hardware behavior are later
gates.

The raw ingress ceiling is now 400 Mpixel/s at the measured 100 MHz synthesis
clock and 600 Mpixel/s at the 150 MHz target. Only the latter exceeds the
497.664 Mpixel/s 4K60 requirement, and downstream transform/entropy capacity
still prevents an end-to-end claim.

Exact-current verification after this slice is 145/145 Scala/Chisel tests
across 27 suites, plus 5 capacity-model, 235 host-flow, and 59 Vivado-report
parser tests. The Scala total includes decoder-backed vector tests at both the
internal 96-bit RGB boundary and the external 128-bit KV260 boundary.

## Remaining Architecture

Implementation order is:

1. Add ordered parallel block transforms: two lanes for 4:2:0 and three for
   4:4:4 at the 150 MHz goal, or an evidence-backed equivalent.
2. Parallelize entropy by independently encoded restart intervals with ordered
   byte-aligned merge, or demonstrate another design that meets the same rate
   without changing decoder-visible coefficient order.
3. Widen the JPEG AXI stream if measured entropy traffic approaches the
   byte-oriented clock limit; the q85 benchmark's scaled output rate alone does
   not require it.
4. Close routed 150 MHz timing/resources, then
   measure first-input through output-TLAST cycles on a physical KV260 and
   decode both modes with standard software.

Do not claim 4K60 from capacity arithmetic or post-synthesis reports. Completion
requires routed 150 MHz evidence and physical decoder-valid DMA measurements in
both sampling modes.
