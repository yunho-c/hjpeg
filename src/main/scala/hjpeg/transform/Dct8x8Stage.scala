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
  * Each eight-term dot product is evaluated in one cycle through a balanced
  * sum tree, producing one row or column coefficient per cycle. Independent
  * row and column engines overlap consecutive blocks through a one-block
  * intermediate buffer.
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

  private def balancedSum(values: Seq[SInt]): SInt = {
    require(values.nonEmpty && (values.length & (values.length - 1)) == 0)
    if (values.length == 1) values.head
    else balancedSum(values.grouped(2).map(pair => pair.head +& pair(1)).toSeq)
  }

  val rowActive = RegInit(false.B)
  val rowIndex = RegInit(0.U(6.W))
  val rowBufferValid = RegInit(false.B)
  val columnActive = RegInit(false.B)
  val columnIndex = RegInit(0.U(6.W))
  val outputValid = RegInit(false.B)
  val samples = Reg(Vec(HjpegConstants.BlockSize, SInt(sampleBits.W)))
  val rowTransformed = Reg(Vec(HjpegConstants.BlockSize, SInt(32.W)))
  val columnInput = Reg(Vec(HjpegConstants.BlockSize, SInt(32.W)))
  val coefficients = Reg(Vec(HjpegConstants.BlockSize, SInt(coefficientBits.W)))

  val columnCanStart = !columnActive && (!outputValid || io.output.ready)
  val rowBufferFire = rowBufferValid && columnCanStart

  io.input.ready := !rowActive && (!rowBufferValid || rowBufferFire)
  io.output.valid := outputValid
  for (index <- 0 until HjpegConstants.BlockSize) {
    io.output.bits.coefficients(index) := coefficients(index)
  }

  when(io.input.fire) {
    for (index <- 0 until HjpegConstants.BlockSize) {
      samples(index) := io.input.bits.samples(index)
    }
    rowIndex := 0.U
    rowActive := true.B
  }

  val rowX = rowIndex(5, 3)
  val rowV = rowIndex(2, 0)
  val rowTermSum = balancedSum((0 until HjpegConstants.BlockDim).map { term =>
    (cosine(rowV)(term) * samples(Cat(rowX, term.U(3.W)))).asSInt
  })

  when(rowActive) {
    rowTransformed(rowIndex) := rowTermSum
    when(rowIndex === (HjpegConstants.BlockSize - 1).U) {
      rowActive := false.B
      rowBufferValid := true.B
    }.otherwise {
      rowIndex := rowIndex + 1.U
    }
  }

  when(rowBufferFire) {
    for (index <- 0 until HjpegConstants.BlockSize) {
      columnInput(index) := rowTransformed(index)
    }
    columnIndex := 0.U
    columnActive := true.B
    rowBufferValid := false.B
  }

  val columnU = columnIndex(5, 3)
  val columnV = columnIndex(2, 0)
  val columnTermSum = balancedSum((0 until HjpegConstants.BlockDim).map { term =>
    (cosine(columnU)(term) * columnInput(Cat(term.U(3.W), columnV))).asSInt
  })
  val rounded = roundShiftSigned(columnTermSum, Dct8x8Constants.FractionBits * 2)

  when(columnActive) {
    coefficients(columnIndex) := rounded(coefficientBits - 1, 0).asSInt
    when(columnIndex === (HjpegConstants.BlockSize - 1).U) {
      columnActive := false.B
      outputValid := true.B
    }.otherwise {
      columnIndex := columnIndex + 1.U
    }
  }

  when(io.output.fire) {
    outputValid := false.B
  }
}
