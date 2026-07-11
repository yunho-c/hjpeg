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
  * Four products are evaluated per cycle and combined through a balanced pair
  * sum before accumulation.
  */
class Dct8x8Stage(sampleBits: Int = 9, coefficientBits: Int = 16) extends Module {
  val io = IO(new Bundle {
    val input = Flipped(Decoupled(new LevelShiftedSampleBlock(sampleBits)))
    val output = Decoupled(new DctCoefficientBlock(coefficientBits))
  })

  private val constants = Dct8x8Constants.CosineQ14
  private val cosine = VecInit(constants.map(row => VecInit(row.map(_.S(16.W)))))

  private def roundShiftSigned(value: SInt, shift: Int): SInt = {
    val negative = value < 0.S
    val magnitude = Mux(negative, -value, value).asUInt
    val rounded = (magnitude + (BigInt(1) << (shift - 1)).U) >> shift
    Mux(negative, -rounded.asSInt, rounded.asSInt)
  }

  val sIdle :: sRows :: sColumns :: sOutput :: Nil = Enum(4)
  val state = RegInit(sIdle)
  val rowIndex = RegInit(0.U(6.W))
  val columnIndex = RegInit(0.U(6.W))
  val termIndex = RegInit(0.U(3.W))
  val accumulator = RegInit(0.S(56.W))
  val samples = Reg(Vec(HjpegConstants.BlockSize, SInt(sampleBits.W)))
  val rowTransformed = Reg(Vec(HjpegConstants.BlockSize, SInt(32.W)))
  val coefficients = Reg(Vec(HjpegConstants.BlockSize, SInt(coefficientBits.W)))

  io.input.ready := state === sIdle
  io.output.valid := state === sOutput
  for (index <- 0 until HjpegConstants.BlockSize) {
    io.output.bits.coefficients(index) := coefficients(index)
  }

  when(io.input.fire) {
    for (index <- 0 until HjpegConstants.BlockSize) {
      samples(index) := io.input.bits.samples(index)
    }
    rowIndex := 0.U
    columnIndex := 0.U
    termIndex := 0.U
    accumulator := 0.S
    state := sRows
  }

  val rowX = rowIndex(5, 3)
  val rowV = rowIndex(2, 0)
  val rowProduct = (cosine(rowV)(termIndex) * samples(Cat(rowX, termIndex))).asSInt
  val rowNextTerm = termIndex + 1.U
  val rowNextProduct = (cosine(rowV)(rowNextTerm) * samples(Cat(rowX, rowNextTerm))).asSInt
  val rowThirdTerm = termIndex + 2.U
  val rowThirdProduct = (cosine(rowV)(rowThirdTerm) * samples(Cat(rowX, rowThirdTerm))).asSInt
  val rowFourthTerm = termIndex + 3.U
  val rowFourthProduct = (cosine(rowV)(rowFourthTerm) * samples(Cat(rowX, rowFourthTerm))).asSInt
  val rowFirstPair = rowProduct +& rowNextProduct
  val rowSecondPair = rowThirdProduct +& rowFourthProduct
  val rowTermSum = rowFirstPair +& rowSecondPair
  val rowAccumulated = accumulator +& rowTermSum

  when(state === sRows) {
    when(termIndex === (HjpegConstants.BlockDim - 4).U) {
      rowTransformed(rowIndex) := rowAccumulated(31, 0).asSInt
      termIndex := 0.U
      accumulator := 0.S
      when(rowIndex === (HjpegConstants.BlockSize - 1).U) {
        columnIndex := 0.U
        state := sColumns
      }.otherwise {
        rowIndex := rowIndex + 1.U
      }
    }.otherwise {
      accumulator := rowAccumulated
      termIndex := termIndex + 4.U
    }
  }

  val columnU = columnIndex(5, 3)
  val columnV = columnIndex(2, 0)
  val columnProduct = (cosine(columnU)(termIndex) * rowTransformed(Cat(termIndex, columnV))).asSInt
  val columnNextTerm = termIndex + 1.U
  val columnNextProduct =
    (cosine(columnU)(columnNextTerm) * rowTransformed(Cat(columnNextTerm, columnV))).asSInt
  val columnThirdTerm = termIndex + 2.U
  val columnThirdProduct =
    (cosine(columnU)(columnThirdTerm) * rowTransformed(Cat(columnThirdTerm, columnV))).asSInt
  val columnFourthTerm = termIndex + 3.U
  val columnFourthProduct =
    (cosine(columnU)(columnFourthTerm) * rowTransformed(Cat(columnFourthTerm, columnV))).asSInt
  val columnFirstPair = columnProduct +& columnNextProduct
  val columnSecondPair = columnThirdProduct +& columnFourthProduct
  val columnTermSum = columnFirstPair +& columnSecondPair
  val columnAccumulated = accumulator +& columnTermSum
  val rounded = roundShiftSigned(columnAccumulated, Dct8x8Constants.FractionBits * 2)

  when(state === sColumns) {
    when(termIndex === (HjpegConstants.BlockDim - 4).U) {
      coefficients(columnIndex) := rounded(coefficientBits - 1, 0).asSInt
      termIndex := 0.U
      accumulator := 0.S
      when(columnIndex === (HjpegConstants.BlockSize - 1).U) {
        state := sOutput
      }.otherwise {
        columnIndex := columnIndex + 1.U
      }
    }.otherwise {
      accumulator := columnAccumulated
      termIndex := termIndex + 4.U
    }
  }

  when(io.output.fire) {
    state := sIdle
  }
}
