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

The current correctness-first implementation is intentionally serialized.
Deterministic ChiselSim regressions measure the following latency from an
accepted boundary to output validity:

| Boundary | Observed cycles | Regression ceiling |
| --- | ---: | ---: |
| 8x8 DCT block | 512 | 512 |
| 64-coefficient quantization | 704 | 704 |
| Complete DCT/quantize/zig-zag block | 1,217 | 1,217 |
| First 4:4:4 MCU after stripe collection | 2,695 | 2,700 |
| First 4:2:0 MCU after band collection | 5,261 | 5,300 |
| Complete 16x16 4:4:4 test frame | 11,136 | 11,200 |

The block and MCU measurements use quality 50 and deterministic fixtures. The
transform latency is fixed by the current state machines; entropy and complete
frame time can also depend on coefficient content and emitted byte count.

The raster stages issue the next component block when the DCT becomes ready
while the previous block is still being quantized. This ordered overlap keeps
one transform instance but reduced the measured 4:4:4 MCU latency by about 29%,
the 4:2:0 MCU latency by about 36%, and the 16x16 frame latency by about 29%.

The quantizer performs two exact restoring-division quotient bits per cycle.
Compared with the earlier one-bit divider, this reduced quantizer latency by
45%, complete block-transform latency by 25%, current 4:4:4/4:2:0 MCU latency
by about 22%/20%, and current 16x16 frame latency by about 22%. Exact rounded
division is checked across luminance and chrominance tables at multiple quality
settings.

The DCT evaluates two exact Q14 product terms per cycle. This halves DCT
latency and, relative to the immediately preceding baseline, reduced complete
block-transform latency by about 30%, 4:4:4/4:2:0 MCU latency by about 30%/29%,
and 16x16 frame latency by about 29%. Varied deterministic blocks are checked
coefficient-for-coefficient against the fixed-point software calculation.

Using only the MCU regression ceilings gives optimistic 1080p throughput
ceilings of roughly 1.14 fps for 4:4:4 and 2.31 fps for 4:2:0 at 100 MHz.
Actual frame throughput will be lower because those estimates omit some raster,
entropy, marker, and flow-control work. They are architectural gap indicators,
not board measurements.

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
The next throughput architecture should prioritize:

1. a block-transform initiation interval compatible with the 34.3-cycle 4:4:4
   budget, even if end-to-end transform latency remains longer;
2. BRAM-friendly synchronous stripe/band storage;
3. ping-pong buffering so raster collection overlaps MCU processing;
4. pipelined or reciprocal-multiply quantization instead of one iterative
   division per coefficient;
5. DCT pipelining and, only when justified by synthesis evidence, limited
   transform replication; and
6. measured entropy/output capacity so the transform is not optimized past a
   downstream bottleneck.

Each optimization must retain the stage-level coefficient fixtures, complete
JPEG decoding tests, recognizable-content checks, and ready/valid behavior.
After a material RTL change, regenerate Vivado evidence before updating any
resource or clock claim.
