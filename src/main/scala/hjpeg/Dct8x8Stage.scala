// See README.md for license details.

package hjpeg

import chisel3._
import chisel3.util._

object Dct8x8Constants {
  val FractionBits = 14
  val CosineQ14: Seq[Seq[Int]] = Seq(
    Seq(5793, 5793, 5793, 5793, 5793, 5793, 5793, 5793),
    Seq(8035, 6811, 4551, 1598, -1598, -4551, -6811, -8035),
    Seq(7568, 3135, -3135, -7568, -7568, -3135, 3135, 7568),
    Seq(6811, -1598, -8035, -4551, 4551, 8035, 1598, -6811),
    Seq(5793, -5793, -5793, 5793, 5793, -5793, -5793, 5793),
    Seq(4551, -8035, 1598, 6811, -6811, -1598, 8035, -4551),
    Seq(3135, -7568, 7568, -3135, -3135, 7568, -7568, 3135),
    Seq(1598, -4551, 6811, -8035, 8035, -6811, 4551, -1598)
  )
}

/** Fixed-point orthonormal 8x8 DCT for one level-shifted JPEG component block.
  *
  * Cosine coefficients are Q14. The stage applies a row transform followed by a
  * column transform and rounds the final Q28 result to integer coefficients.
  */
class Dct8x8Stage(sampleBits: Int = 9, coefficientBits: Int = 16) extends Module {
  val io = IO(new Bundle {
    val input = Flipped(Decoupled(new LevelShiftedSampleBlock(sampleBits)))
    val output = Decoupled(new DctCoefficientBlock(coefficientBits))
  })

  private val constants = Dct8x8Constants.CosineQ14

  private def sumSInt(values: Seq[SInt]): SInt =
    values.reduce(_ +& _)

  private def roundShiftSigned(value: SInt, shift: Int): SInt = {
    val negative = value < 0.S
    val magnitude = Mux(negative, -value, value).asUInt
    val rounded = (magnitude + (BigInt(1) << (shift - 1)).U) >> shift
    Mux(negative, -rounded.asSInt, rounded.asSInt)
  }

  io.input.ready := io.output.ready
  io.output.valid := io.input.valid

  val rowTransformed = Wire(Vec(HjpegConstants.BlockDim, Vec(HjpegConstants.BlockDim, SInt(32.W))))
  for (x <- 0 until HjpegConstants.BlockDim) {
    for (v <- 0 until HjpegConstants.BlockDim) {
      val terms = (0 until HjpegConstants.BlockDim).map { y =>
        (constants(v)(y).S(16.W) * io.input.bits.samples(x * HjpegConstants.BlockDim + y)).asSInt
      }
      rowTransformed(x)(v) := sumSInt(terms)
    }
  }

  for (u <- 0 until HjpegConstants.BlockDim) {
    for (v <- 0 until HjpegConstants.BlockDim) {
      val terms = (0 until HjpegConstants.BlockDim).map { x =>
        (constants(u)(x).S(16.W) * rowTransformed(x)(v)).asSInt
      }
      val rounded = roundShiftSigned(sumSInt(terms), Dct8x8Constants.FractionBits * 2)
      io.output.bits.coefficients(u * HjpegConstants.BlockDim + v) := rounded(coefficientBits - 1, 0).asSInt
    }
  }
}
