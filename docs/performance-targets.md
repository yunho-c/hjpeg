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

The block transform and isolated MCU loaders now meet their average mode
budgets in deterministic simulation, while raster collection/processing and
complete-frame flow remain serialized. ChiselSim regressions measure the
following latency from an accepted boundary to output validity:

| Boundary | Observed cycles | Regression ceiling |
| --- | ---: | ---: |
| Four-lane 8x8 DCT block latency | 35 | 36 |
| Consecutive DCT block initiation | 16 | 16 |
| Four-lane 64-coefficient quantization | 21 | 22 |
| Complete DCT/quantize/zig-zag block latency | 55 | 57 |
| Four-block transform initiation intervals | 16/16/16 | 17 maximum |
| First 4:4:4 MCU after stripe collection | 98 | 100 |
| First 4:2:0 MCU after band collection | 266 | 270 |
| Complete 16x16 4:4:4 test frame | 2,146 | 2,300 |

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
processing first fell from 398 to 154 cycles for 4:4:4 and from 1,050 to 650
for 4:2:0 with the pipelined transform and synchronous scalar reads. Banked
reads reduce those current measurements further to 98 and 266 cycles; the
16x16 frame fixture first fell from 2,364 to 2,252 cycles; entropy lookahead
reduces it further to 2,146 cycles.

The last pre-BRAM quick 32x16 trace completed in 3,275 cycles for 4:4:4 and
3,106 for 4:2:0. Within-MCU block intervals are 16 cycles; steady same-stripe
4:4:4 MCU spacing is 152 cycles and a stripe transition is 408 cycles.
Header/entropy startup dominates these small frames, while serial MCU loading
and raster collection dominate the remaining steady-state gap.

A current-code, banked-block-RAM 64x64 seeded-random quality-90 capture covers
the four-coefficient AC scanner and overlapped bit packer. The 4:4:4 frame
completes in 12,032 cycles, 1.59x faster than the scalar entropy scanner, and
has 99-cycle post-first-stripe steady MCU intervals. Entropy block work falls
from 14,025 to 5,814 cycles, or 30.28 cycles/block. Its `mcu_output` boundary
has only one 1,500-cycle startup stall instead of repeated steady-state stalls.
The 4:2:0 frame completes in 9,999 cycles, 1.26x faster than the scalar entropy
scanner, and has 267-cycle post-first-band steady MCU intervals. Entropy block
work falls from 7,251 to 3,003 cycles, or 31.28 cycles/block, and `mcu_output`
likewise has only one 1,432-cycle startup stall. Both scenarios retain a
16-cycle transform initiation interval, emit the same byte counts as the
scalar-scanner baseline, and decode at the expected dimensions. Short
one-to-three-cycle run-to-packer stalls remain around byte emission and
stuffing, but no sustained entropy mismatch remains in these fixtures.

Using only the new MCU regression ceilings gives optimistic 1080p throughput
ceilings of roughly 30.9 fps for 4:4:4 and 45.4 fps for 4:2:0 at 100 MHz.
Actual frame throughput will be lower because those estimates omit raster band
collection, markers, and other flow-control work. The transform, isolated
loader, and measured high-entropy block-rate deficits are resolved in
simulation; the next architectural limit is overlapping raster collection with
processing. These are architectural gap indicators, not timing closure or board
measurements.

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
Term-level DCT unrolling, row/column overlap, four-coefficient AC lookahead, and
packer input/output overlap are complete. Current high-entropy traces no longer
show repeated MCU stalls, so the next throughput architecture should prioritize:

1. ping-pong buffering so raster collection overlaps MCU processing without
   hiding a sustained downstream mismatch behind arbitrary FIFO depth;
2. a measured MCU queue sized from the resulting producer/consumer rates; and
3. fresh high-entropy traces and post-implementation evidence after each
   material architecture change.

Each optimization must retain the stage-level coefficient fixtures, complete
JPEG decoding tests, recognizable-content checks, and ready/valid behavior.
After a material RTL change, regenerate Vivado evidence before updating any
resource or clock claim.
