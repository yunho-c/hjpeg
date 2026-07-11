// See README.md for license details.

package hjpeg

import chisel3._
import chisel3.util._

private class DctBlockMetadata extends Bundle {
  val quality = UInt(7.W)
  val isLuminance = Bool()
}

/** Transforms level-shifted component blocks into quantized zig-zag order.
  *
  * The production path uses four-lane, banked DCT and quantizer stages. An
  * eight-entry metadata queue covers their in-flight block capacity and keeps
  * quality/component selection aligned under backpressure. The original
  * single-lane [[Dct8x8Stage]] and [[QuantizeBlockStage]] remain independently
  * tested reference/fallback implementations.
  */
class JpegBlockTransformStage(sampleBits: Int = 9, coefficientBits: Int = 16) extends Module {
  val io = IO(new Bundle {
    val quality = Input(UInt(7.W))
    val isLuminance = Input(Bool())
    val input = Flipped(Decoupled(new LevelShiftedSampleBlock(sampleBits)))
    val output = Decoupled(new ZigZagCoefficientBlock(coefficientBits))
  })

  val dct = Module(new PipelinedDct8x8Stage(sampleBits, coefficientBits))
  val quantize = Module(new PipelinedQuantizeBlockStage(coefficientBits))
  val zigZag = Module(new ZigZagBlockStage(coefficientBits))
  private val metadata = Module(new Queue(new DctBlockMetadata, entries = 8, pipe = true))

  dct.io.input.bits := io.input.bits
  dct.io.input.valid := io.input.valid && metadata.io.enq.ready
  metadata.io.enq.valid := io.input.valid && dct.io.input.ready
  metadata.io.enq.bits.quality := io.quality
  metadata.io.enq.bits.isLuminance := io.isLuminance
  io.input.ready := dct.io.input.ready && metadata.io.enq.ready

  quantize.io.quality := metadata.io.deq.bits.quality
  quantize.io.isLuminance := metadata.io.deq.bits.isLuminance
  quantize.io.input.bits := dct.io.output.bits
  quantize.io.input.valid := dct.io.output.valid && metadata.io.deq.valid
  dct.io.output.ready := quantize.io.input.ready && metadata.io.deq.valid
  metadata.io.deq.ready := quantize.io.input.ready && dct.io.output.valid
  zigZag.io.input <> quantize.io.output
  io.output <> zigZag.io.output
}
