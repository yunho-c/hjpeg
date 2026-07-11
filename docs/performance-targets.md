# hjpeg Performance Targets

This document turns the project's general throughput goal into provisional,
measurable engineering targets. These targets guide architecture work; they do
not claim that the current RTL or a physical KV260 already meets them.

## Primary Target

The provisional minimum target is:

- frame size: 1920x1080;
- sampling: both 4:4:4 and 4:2:0;
- throughput: 30 complete frames per second;
- programmable-logic clock: 100 MHz;
- input: one 32-bit AXI-stream beat per RGB pixel when accepted;
- output: decoder-valid baseline JPEG with the configured dimensions and
  recognizable content; and
- post-implementation utilization: no tracked fabric resource above 70%,
  leaving headroom for integration and later changes.

Timing must have nonnegative setup WNS and hold WHS at 100 MHz. DRC and route
status must pass the gates in [`kv260-bringup.md`](kv260-bringup.md). The 70%
resource ceiling is provisional and applies independently to LUTs, LUTRAM,
flip-flops, BRAM, and DSPs where Vivado reports a meaningful fabric total.

The frame-rate target is subordinate to correctness: frames that are fast but
malformed, truncated, dimensionally wrong, or visually collapsed do not count.

## Derived Cycle Budgets

At 100 MHz and 30 frames per second, one frame has an average budget of
3,333,333 cycles. A 1920x1080 frame contains:

| Mode | MCU geometry | MCUs/frame | Blocks/frame | Average cycles/MCU | Average cycles/block |
| --- | ---: | ---: | ---: | ---: | ---: |
| 4:4:4 | 8x8 | 32,400 | 97,200 | 102.9 | 34.3 |
| 4:2:0 | 16x16 | 8,160 | 48,960 | 408.5 | 68.1 |

These are whole-system average budgets, not permission for every block to use
the full amount independently. Raster collection, transform, entropy coding,
headers, restart markers, output stalls, and frame transitions all consume the
same frame budget. A future pipelined transform may have a long latency while
meeting the target through a sufficiently small initiation interval.

The 32-bit input stream must carry 62.208 million pixels per second for
1080p30. At 100 MHz this allows about 1.61 clock cycles per input pixel on
average, including any input backpressure.

## Current Simulation Baseline

The block transform now meets the average 4:4:4 block-rate budget in
deterministic simulation, while raster collection/loading and complete-frame
flow remain serialized. ChiselSim regressions measure the following latency
from an accepted boundary to output validity:

| Boundary | Observed cycles | Regression ceiling |
| --- | ---: | ---: |
| Four-lane 8x8 DCT block latency | 35 | 36 |
| Consecutive DCT block initiation | 16 | 16 |
| Four-lane 64-coefficient quantization | 21 | 22 |
| Complete DCT/quantize/zig-zag block latency | 55 | 57 |
| Four-block transform initiation intervals | 16/16/16 | 17 maximum |
| First 4:4:4 MCU after stripe collection | 153 | 160 |
| First 4:2:0 MCU after band collection | 649 | 660 |
| Complete 16x16 4:4:4 test frame | 2,362 | 2,400 |

The block and MCU measurements use quality 50 and deterministic fixtures. The
transform latency is fixed by the current state machines; entropy and complete
frame time can also depend on coefficient content and emitted byte count.

`PipelinedDct8x8Stage` exploits exact symmetry in the existing Q14 cosine
matrix. Four `x0 +/- x7` through `x3 +/- x4` butterflies feed four frequency
lanes; even frequencies use the sums and odd frequencies use the differences.
Each output therefore needs four products rather than eight, with no arithmetic
change. Pair formation is registered, row and column engines overlap through
three banked transpose buffers, and two result banks absorb output
backpressure. There is no intermediate rounding: the final Q28 value retains
nearest rounding with halves away from zero. Deterministic varied blocks match
the prior fixed-point reference coefficient-for-coefficient.

`PipelinedQuantizeBlockStage` processes four adjacent coefficients per cycle.
Its two banks overlap block capture, processing, and output holding. The four
lanes share one quality-scale calculation and retain the existing registered
table lookup, 17-fraction-bit floor-reciprocal estimate, and exact
multiply-back correction. The exhaustive 8,388,480-pair reciprocal proof still
applies, and new RTL tests cover signed extremes, both tables, out-of-range
quality clamping, sustained traffic, and ordered backpressure.

`JpegBlockTransformStage` uses the new stages with an eight-entry ordered
metadata queue. Across varied luminance/chrominance blocks it sustains a
16-cycle block interval, below the 34.3-cycle 4:4:4 budget. Relative to the
previous production path, DCT latency fell from 129 to 35 cycles, quantization
from 66 to 21, and complete transform latency from 196 to 55. First-MCU
processing fell from 398 to 153 cycles for 4:4:4 and from 1,050 to 649 for
4:2:0; the 16x16 frame fixture fell from 3,096 to 2,362 cycles.

The quick 32x16 trace now completes in 3,275 cycles for 4:4:4 and 3,106 for
4:2:0. Within-MCU block intervals are 16 cycles; steady same-stripe 4:4:4 MCU
spacing is 152 cycles and a stripe transition is 408 cycles. Header/entropy
startup dominates these small frames, while serial MCU loading and raster
collection dominate the remaining steady-state gap.

A current-code 64x64 seeded-random quality-90 capture adds the missing
high-entropy evidence. The 4:4:4 frame completes in 19,563 cycles and has
post-first-stripe MCU intervals of 222--230 cycles. Its `mcu_output` boundary
has one 1,574-cycle startup stall followed by 55 repeated 64--76-cycle stalls,
so entropy consumption is a sustained 4:4:4 bottleneck for this content. The
4:2:0 frame completes in 16,277 cycles, has a stable 650-cycle post-first-band
MCU interval, and has only one contiguous 1,318-cycle startup stall; serialized
MCU loading remains its dominant steady-state limit. Both scenarios retain a
16-cycle transform initiation interval and decode at the expected dimensions.

Using only the new MCU regression ceilings gives optimistic 1080p throughput
ceilings of roughly 19.3 fps for 4:4:4 and 18.6 fps for 4:2:0 at 100 MHz.
Actual frame throughput will be lower because those estimates omit raster band
collection, entropy, markers, and other flow-control work. The transform-rate
deficit is resolved in simulation; the next architectural limit is overlapping
raster collection, MCU loading, and transform work. These are architectural gap
indicators, not timing closure or board measurements.

## Performance Trace Workflow

Run the integrated simulation profiler with:

```sh
./scripts/dev/generate-performance-trace
```

The default `quick` 32x16 fixtures provide repeated MCUs in both sampling modes
and a controlled 4:4:4 output-stall comparison. The optional
`--profile steady-state` matrix uses 64x64 flat, smooth-gradient,
checkerboard, and seeded pseudo-random frames at qualities 10, 50, and 90 in
both sampling modes. Generated artifacts live under `build/performance-traces/`:

- `trace.json` is a portable Chrome Trace file for Perfetto. Each ready/valid
  boundary has transfer, downstream-blocked, upstream-starved, and idle spans;
  DCT, quantizer, zig-zag, and complete-transform transactions have separate
  latency lanes. Simulation-only raster and encoder FSM values have explicit
  phase lanes.
- `phases.csv` records one raster and encoder phase value per simulated cycle.
- `metrics.json` and `metrics.csv` contain frame rate extrapolation at 100 MHz,
  transfer and stall counts, latency and initiation distributions, and target
  comparisons.
- `pipeline-*.mmd`, `.dot`, and, when Graphviz is installed, `.svg` summarize
  each scenario against the cycles/pixel, cycles/block, and cycles/MCU budgets
  above.
- `scenarios.csv`, `samples.csv`, and `phases.csv` are the raw deterministic
  capture and can be passed back with `--capture-dir` to reproduce the rendered
  artifacts.

The transform target uses sustained intervals between component blocks within
an MCU. Longer gaps between MCUs remain visible in the trace and the unfiltered
stage initiation distribution, but belong to raster/MCU supply rather than the
transform's own acceptance capacity. A `valid && !ready` span means that the
boundary is blocked by its consumer; `!valid && ready` means it is starved by
its producer. Comparisons across boundaries must account for token type:
pixels, coefficient blocks, MCUs, entropy runs, and bytes are not interchangeable.

Phase metrics distinguish:

- raster startup from the first input pixel through the first MCU handoff;
- encoder startup through the first emitted JPEG byte and first entropy block;
- transform-input intervals within one MCU and between consecutive MCUs;
- MCU intervals within a stripe/band and across stripe/band transitions; and
- steady-state MCU intervals after the first stripe/band, excluding transition
  intervals.

This classification keeps one-time header/startup behavior and raster refill
gaps out of content-dependent steady-state entropy conclusions.

The small-frame FPS value is useful for comparing revisions, not as a direct
1080p prediction. The simulation assumes a 100 MHz clock, while only Vivado can
establish that the elaborated design closes timing and only KV260 execution can
establish DMA-inclusive hardware throughput.

## Evidence Levels

Performance claims must identify their evidence level:

1. **Simulation contract:** deterministic cycle counts or ceilings under a
   stated ready/valid pattern.
2. **Vivado construction:** post-implementation timing and utilization for the
   exact RTL revision and target clock.
3. **Board measurement:** host-observed bytes, elapsed time, frames per second,
   protocol status, and decoder validation from a physical KV260 run.

Simulation extrapolation cannot prove clock closure or physical throughput.
Vivado reports cannot prove DMA behavior or decoder-valid hardware output.

## Optimization Direction

The current gap is too large for timeout tuning or small state-machine changes.
Term-level DCT unrolling and row/column overlap are complete. Current smooth
and high-entropy traces show two independent sustained limits, so the next
throughput architecture should prioritize:

1. at least two-coefficient-per-cycle AC scanning plus enough run buffering or
   packer overlap to remove repeated high-entropy 4:4:4 MCU stalls;
2. BRAM-friendly banked synchronous stripe/band storage with widened MCU reads;
3. ping-pong buffering so raster collection overlaps MCU processing without
   hiding a sustained downstream mismatch behind arbitrary FIFO depth;
4. a measured MCU queue sized from the resulting producer/consumer rates; and
5. fresh high-entropy traces and post-implementation evidence after each
   material architecture change.

Each optimization must retain the stage-level coefficient fixtures, complete
JPEG decoding tests, recognizable-content checks, and ready/valid behavior.
After a material RTL change, regenerate Vivado evidence before updating any
resource or clock claim.
