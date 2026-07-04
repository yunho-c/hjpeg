// See README.md for license details.

package hjpeg

import chisel3._
import chisel3.util._

/** Quantizes one natural-order 8x8 DCT coefficient block.
  *
  * Input and output coefficients use natural raster order. Zig-zag reordering is
  * a separate stage so quantization remains table-index-aligned.
  *
  * Rounding is nearest with halves away from zero:
  *
  *   quantized = sign(coefficient) * ((abs(coefficient) + table / 2) / table)
  */
class QuantizeBlockStage(coefficientBits: Int = 16) extends Module {
  val io = IO(new Bundle {
    val quality = Input(UInt(7.W))
    val isLuminance = Input(Bool())
    val input = Flipped(Decoupled(new DctCoefficientBlock(coefficientBits)))
    val output = Decoupled(new QuantizedCoefficientBlock(coefficientBits))
  })

  io.input.ready := io.output.ready
  io.output.valid := io.input.valid

  for (index <- 0 until HjpegConstants.BlockSize) {
    val tableValue = Module(new JpegQuantTableValue())
    tableValue.io.quality := io.quality
    tableValue.io.isLuminance := io.isLuminance
    tableValue.io.index := index.U

    val coefficient = io.input.bits.coefficients(index)
    val negative = coefficient < 0.S
    val magnitude = Mux(negative, -coefficient, coefficient).asUInt
    val divisor = tableValue.io.value
    val roundedMagnitude = (magnitude + (divisor >> 1)) / divisor
    val signedRounded = roundedMagnitude.asSInt

    io.output.bits.coefficients(index) := Mux(negative, -signedRounded, signedRounded)
  }
}
