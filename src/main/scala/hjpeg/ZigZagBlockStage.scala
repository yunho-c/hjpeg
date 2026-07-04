// See README.md for license details.

package hjpeg

import chisel3._
import chisel3.util._

/** Reorders one natural-order 8x8 coefficient block into JPEG zig-zag scan order. */
class ZigZagBlockStage(coefficientBits: Int = 16) extends Module {
  val io = IO(new Bundle {
    val input = Flipped(Decoupled(new QuantizedCoefficientBlock(coefficientBits)))
    val output = Decoupled(new ZigZagCoefficientBlock(coefficientBits))
  })

  io.input.ready := io.output.ready
  io.output.valid := io.input.valid

  for (scanIndex <- 0 until HjpegConstants.BlockSize) {
    io.output.bits.coefficients(scanIndex) := io.input.bits.coefficients(JpegTables.ZigZagOrder(scanIndex))
  }
}
