// See README.md for license details.

package hjpeg

import chisel3._
import chisel3.util._

/** Transforms one level-shifted component block into quantized zig-zag order. */
class JpegBlockTransformStage(sampleBits: Int = 9, coefficientBits: Int = 16) extends Module {
  val io = IO(new Bundle {
    val quality = Input(UInt(7.W))
    val isLuminance = Input(Bool())
    val input = Flipped(Decoupled(new LevelShiftedSampleBlock(sampleBits)))
    val output = Decoupled(new ZigZagCoefficientBlock(coefficientBits))
  })

  val dct = Module(new Dct8x8Stage(sampleBits, coefficientBits))
  val quantize = Module(new QuantizeBlockStage(coefficientBits))
  val zigZag = Module(new ZigZagBlockStage(coefficientBits))

  dct.io.input <> io.input
  quantize.io.quality := io.quality
  quantize.io.isLuminance := io.isLuminance
  quantize.io.input <> dct.io.output
  zigZag.io.input <> quantize.io.output
  io.output <> zigZag.io.output
}
