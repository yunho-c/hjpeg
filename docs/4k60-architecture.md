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
  cycle; and
- no tracked fabric resource above the existing project evidence ceiling.

Quality-90 `seeded-random` remains a stress case, not a content-independent
60-fps guarantee. Performance evidence must name the fixture and sampling mode.
The integrated default Vivado flow now closes 150 MHz and passes the documented
70% independent-resource ceiling. Physical decoder-valid 4K60 measurements
remain open, so implementation closure is not by itself a completed 4K60 claim.

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
  -tclargs build/vivado/ip_repo-4k60 build/vivado/hjpeg-kv260-4k60-bd 128 150
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

## Ordered Three-Lane Transform Slice

The unified raster stage now owns three lockstep `JpegBlockTransformStage`
instances. A 4:4:4 MCU issues Y, Cb, and Cr in one ordered batch. A 4:2:0 MCU
issues Y0/Y1/Y2 followed by Y3/Cb/Cr after the pipelines' 16-cycle initiation
interval. Atomic input/output handshakes prevent any subset of the three lanes
from accepting or retiring a batch. The MCU exposed to entropy remains in the
standard component order.

For 4:4:4, the loader also captures Y, Cb, and Cr from their independent
memories on the same read instead of repeating identical addresses in three
phases. Focused coefficient-level tests pass for ordered 4:4:4 stripes and a
padded 4:2:0 frame. The decoder-valid quick profiler changes are:

| Quick fixture | One transform | Three ordered transforms | Change |
| --- | ---: | ---: | ---: |
| 32x16 4:4:4 frame | 3,094 cycles | 2,651 cycles | -443 (-14.3%) |
| 32x16 4:2:0 frame | 2,526 cycles | 2,462 cycles | -64 (-2.5%) |
| 4:4:4 steady MCU spacing | 139 cycles | 76.7 cycles | -62.3 (-44.8%) |

Exact UHD post-synthesis evidence for this intermediate at 100 MHz is 38,728
logic LUTs (33.07%), 56,710 registers (24.21%), 99 BRAM tiles (68.75%), 194
DSPs (15.54%), and setup WNS `+1.103 ns`. The additional transforms therefore
remain below the 70% resource gate, but BRAM headroom is only 1.25 percentage
points and later changes must avoid replicated frame storage.

This slice does not overlap loading one MCU with transformation/entropy of
another. Its 4:2:0 loader also still reads four source samples per cycle. Those
serial regions explain why the measured MCU interval remains above the 4K60
budget despite having enough raw transform arithmetic.

## Eight-Sample Raster Read Slice

The shared raster store now reads all eight banks every cycle without changing
its organization or capacity. Lanes 0..3 fetch four adjacent columns from one
row and lanes 4..7 fetch the same columns from the following row. For 4:2:0,
the same ports fetch two adjacent 2x2 chroma footprints and produce two
downsampled values per cycle. Replicated right/bottom edges may cause multiple
logical lanes to request one physical bank; the RTL permits this only when all
such lanes use the same address, so one read can fan out safely.

The quick profiler measures the intended loader reduction:

| Quick fixture | Three-transform loader | Eight-read loader | Change |
| --- | ---: | ---: | ---: |
| 32x16 4:4:4 raster-load phase | 136 cycles | 72 cycles | -64 (-47.1%) |
| 32x16 4:2:0 raster-load phase | 258 cycles | 130 cycles | -128 (-49.6%) |
| 32x16 4:4:4 complete frame | 2,651 cycles | 2,605 cycles | -46 (-1.7%) |
| 32x16 4:2:0 complete frame | 2,462 cycles | 2,398 cycles | -64 (-2.6%) |

Coefficient-level tests pass in both modes, including partial-edge padding.
Exact UHD post-synthesis use at 100 MHz is 39,351 logic LUTs (33.60%), 56,720
registers (24.21%), 99 BRAM tiles (68.75%), and 194 DSPs (15.54%), with setup
WNS `+1.103 ns`. Relative to the three-transform checkpoint, the schedule adds
623 LUTs and 10 registers while BRAM and DSP use remain unchanged.

The exact committed pre-overlap boundary passes 145/145 Scala/Chisel tests
across 27 suites. Together with the unchanged Python totals above, this makes
commit `24eeadb` the rollback point for the upcoming cross-MCU state split.

## Cross-MCU Transform Overlap Slice

`JpegParallelMcuTransformStage` now owns the three block-transform pipelines,
an independently held raw MCU, an eight-entry batch-metadata queue, a partial
4:2:0 assembly slot, and one ordered coefficient-MCU output slot. The raster
loader advances as soon as raw samples are copied into this stage. It no longer
waits for the 56-cycle block-transform latency or for coefficient output to be
consumed.

The stage can replace its raw MCU in the cycle that the prior MCU's final batch
enters all three transforms. Metadata identifies 4:4:4's single batch versus
4:2:0's first/final batches, preserves frame-final status, and remains aligned
while output is backpressured. A dedicated overlap test retires four 4:4:4 MCUs
at exact 16-cycle intervals. Decoder-backed vector tests pass in both chroma
modes, and the stalled-output test produces byte-identical JPEG data.

The quick profiler now measures every 4:4:4 block-transform initiation interval
at exactly 16 cycles (minimum, median, 95th percentile, and maximum). The tiny
32x16 4:4:4 frame falls from 2,605 to 2,574 cycles. Its complete-frame change is
small because JPEG header and entropy traffic dominate that fixture; the
transform schedule, not the quick-frame total, is the capacity evidence. For
4:2:0, batches within one MCU are 16 cycles apart and the measured gap to the
next MCU is 50 cycles, consistent with its now-dominant 64-cycle raw loader.

Exact UHD post-synthesis use at 100 MHz is 39,722 logic LUTs (33.92%), 28 LUTRAM
cells, 63,241 registers (27.00%), 99 BRAM tiles (68.75%), and 194 DSPs (15.54%),
with setup WNS `+1.103 ns`. The overlap storage therefore costs 371 logic LUTs
and 6,521 registers relative to `24eeadb`, without adding BRAM or DSPs.

Verification at this transform-overlap checkpoint was 146/146 Scala/Chisel
tests across 28 suites, plus the unchanged 5 capacity-model, 235 host-flow, and
59 Vivado-report parser tests. The added stage-level regression proves four
ordered 4:4:4 MCU outputs at exact 16-cycle intervals.

## Reused Buffered Block Entropy Slice

`JpegParallelMcuEntropyStage` uses three physical buffered block encoders in
both modes. Each retains the timing-safe four-coefficient AC scanner and writes
bit runs into a 16-entry queue. 4:4:4 loads Y/Cb/Cr once. 4:2:0 first loads
Y0/Y1/Y2, then reloads the same slots with Y3/Cb/Cr as the corresponding first
wave drains. A single selector drains logical blocks in strict JPEG order, so
the bit packer, byte stuffing, DC predictor rules, restart markers, and scan
syntax remain unchanged.

Deferred registers retain the second-wave coefficient blocks, predecessor DC
values, and luminance selectors. Y1/Y2/Y3 still use Y0/Y1/Y2 as their DC
predecessors while Cb and Cr use the frame predictors. A focused software-model
test compares every emitted Huffman/amplitude run for distinct 4:4:4 and 4:2:0
blocks, including output stalls, logical block order, and final predictor state.
The existing decoder-backed stream/core/top tests cover cross-MCU differences,
restart resets/marker cycling, header stalls, and byte-exact backpressure.

Relative to the prior six-encoder checkpoint, scanner reuse has a small bounded
cycle cost while retaining all maintained targets:

| Fixture | Six physical encoders | Three reused slots | Change |
| --- | ---: | ---: | ---: |
| 32x16 4:4:4 complete frame | 2,485 cycles | 2,501 cycles | +16 (+0.6%) |
| 32x16 4:2:0 complete frame | 2,346 cycles | 2,356 cycles | +10 (+0.4%) |
| 4:4:4 steady MCU mean spacing | 55.3 cycles | 56.3 cycles | +1.0 (+1.9%) |
| 256x64 q90 4:4:4 | 28,445 cycles | 28,474 cycles | +29 (+0.1%) |

The current 256x64 quality-90 seeded-random frame emits the same 22,882
decoder-valid bytes, accepts input at 1.240 cycles/pixel, and produces 0.804
output bytes/cycle. The two-frame version completes in 52,776 cycles, accepts
input at 1.361 cycles/pixel, and has a 103-cycle frame-transition MCU interval.
Both remain inside the 1.61-cycle/pixel input target. This stress case is still
primarily byte-output limited and is deliberately not the q85 4K60 contract.

The quantizers now use explicit four-read distributed reciprocal ROMs instead
of replicated inferred block ROMs. Exact standalone UHD synthesis at a 10 ns
reporting constraint falls from 49,990 to 45,168 CLB LUTs, from 76,612 to
73,402 registers, and from 99 to 96 BRAM tiles; DSP use remains 194. That is a
4,822-LUT, 3,210-register, three-BRAM-tile reduction. Reciprocal arithmetic,
23-cycle quantizer latency, and 16-cycle block initiation remain bit-exact.

An eight-coefficient scanner experiment failed 100 MHz synthesis at WNS
`-1.158 ns`; sixteen coefficients failed at `-3.676 ns`. Both experiments were
fully reverted. The retained design reuses independent four-coefficient
scanners without lengthening their combinational priority chains.

The retained implementation passes 148/148 Scala/Chisel tests across 29 suites.
Python verification passes 235 host-flow, 59 Vivado-report, 10
ChiselSim-environment, 11 design-graph, 11 performance-trace, and 5
capacity-model tests (331 total). The five-scenario regenerated performance
capture passes as a separate simulator-backed test.

## 150 MHz Timing Closure

Timing closure required explicit boundaries at the longest physical paths,
without changing coefficient arithmetic, JPEG ordering, or throughput:

- the DCT registers two-product partial sums in both row and column dot
  products, then performs a short exact final add before the existing column
  rounding boundary;
- the quantizer registers both reciprocal lookup and the 21-bit scaled-table
  numerator before their downstream multiply/divide logic;
- each AC scanner inserts a one-entry pipelined event queue before Huffman
  selection;
- RGB conversion is registered before raster-bank writes; and
- the unified raster loader registers bank read requests and all 24 component
  responses before bank selection and destination-block assembly.

The DCT remains bit-exact and sustains a 16-cycle block initiation interval.
Focused simulation observes 38-cycle DCT, 23-cycle quantizer, and 61-cycle
complete-transform latency. The AC scanner has one fill cycle and then sustains
one ordered event per cycle. Raster requests still issue one eight-sample group
per cycle. The complete decoder-backed core and AXI suites remain exact; the
16x16 4:4:4 fixture is 2,036 cycles against its 2,300-cycle ceiling.

Standalone UHD synthesis at a 10 ns reporting constraint uses 45,168 CLB LUTs,
73,402 registers, 96 BRAM tiles, and 194 DSPs. Setup WNS is `+4.198 ns`; the
resource changes therefore preserve the established 100 MHz timing result.

The integrated default implementation requests 150 MHz from the PS and reports
an actual 6.666 ns period, or 150.015 MHz. It is fully routed and meets every
timing constraint: setup WNS is `+0.232 ns`, TNS is `0`, there are no failing
setup endpoints, and hold WHS is `+0.010 ns`. All 119,201 routable nets are
fully routed with zero routing errors. DRC has warnings but no Error or Critical
Warning violations. The flow emits a nonempty bitstream, bitstream-bearing XSA,
and routed checkpoint. It now also emits a depth-10 hierarchical utilization
report; `report_checkpoint_hierarchy.tcl` can regenerate the same diagnostic
from any saved DCP.

Post-implementation use is 48,674 CLB LUTs (41.56%), 80,343 registers (34.30%),
99.5 BRAM tiles (69.10%), and 194 DSPs (15.54%). Physical placement touches
11,446/14,640 CLBs, or 78.18%. The complete twelve-record Vivado evidence gate
passes the documented 70% independent-resource ceiling. The aggregate `CLB`
row remains present in JSON as informational placement evidence because it
double-counts LUT/register resources and timing-driven placement deliberately
spreads logic. Clean routing, DRC, and positive routed setup/hold slack are the
physical implementation gates; the 78.18% figure is still reported rather than
hidden.

Artifact SHA-256 values for this exact implementation are
`ccd23d32aded07cc6c3dcdb3485e15dea143e695992322f222af907cedf52eb1` for
the bitstream,
`63b764b9e5adff83c36cbfd5d3d6d831f59ba74d96793acfaddee2de61a39e75`
for the XSA, and
`f9329a518601049ef27e5d26eadc72be761717334d0f12085c9362ccccf64d35`
for the routed checkpoint.

Current verification is 148/148 Scala/Chisel tests across 29 suites. The Python
side passes 5 capacity-model, 10 ChiselSim-environment, 11 design-graph, 11
performance-trace, 59 Vivado-report, and 235 host-flow tests.

## Remaining Architecture

Implementation order is:

1. Program the exact 150 MHz bitstream on a KV260 and measure the q85 UHD
   fixture through the documented DMA path in both chroma modes.
2. Add ordered multi-run drain/packing or widen JPEG output only if that q85
   target trace demonstrates that entropy traffic still misses the frame
   budget on hardware.
3. Require first-input through output-TLAST to fit 2,500,000 cycles at 150 MHz,
   transfer all 33,177,600 input bytes, preserve clean DMA/protocol status, and
   decode both captures with standard software.

Do not claim completed 4K60 from capacity arithmetic or routed timing alone.
Completion still requires physical decoder-valid DMA measurements in both
sampling modes.
