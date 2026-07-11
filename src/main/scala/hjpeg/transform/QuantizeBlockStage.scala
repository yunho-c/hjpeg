// See README.md for license details.

package hjpeg

import chisel3._
import chisel3.util._

private[hjpeg] object QuantizeReciprocal {
  def fractionBits(coefficientBits: Int): Int = {
    require(coefficientBits >= 8 && coefficientBits <= 28)
    coefficientBits + 1
  }

  def value(divisor: Int, coefficientBits: Int): Int = {
    require(divisor > 0)
    (1 << fractionBits(coefficientBits)) / divisor
  }

  def divide(numerator: Int, divisor: Int, coefficientBits: Int): Int = {
    val estimate =
      ((numerator.toLong * value(divisor, coefficientBits)) >> fractionBits(coefficientBits)).toInt
    if (numerator - estimate * divisor >= divisor) estimate + 1 else estimate
  }
}

/** Quantizes one natural-order 8x8 DCT coefficient block.
  *
  * Input and output coefficients use natural raster order. Zig-zag reordering is
  * a separate stage so quantization remains table-index-aligned.
  *
  * Rounding is nearest with halves away from zero:
  *
  *   quantized = sign(coefficient) * ((abs(coefficient) + table / 2) / table)
  *
  * Division uses a floor reciprocal with `coefficientBits + 1` fractional
  * bits. The initial quotient cannot exceed the exact quotient and is at most
  * one low across the supported numerator range; a multiply-back remainder
  * check supplies that possible final quotient bit exactly.
  */
class QuantizeBlockStage(coefficientBits: Int = 16) extends Module {
  val io = IO(new Bundle {
    val quality = Input(UInt(7.W))
    val isLuminance = Input(Bool())
    val input = Flipped(Decoupled(new DctCoefficientBlock(coefficientBits)))
    val output = Decoupled(new QuantizedCoefficientBlock(coefficientBits))
  })

  private val numeratorBits = coefficientBits + 2
  private val reciprocalFractionBits = QuantizeReciprocal.fractionBits(coefficientBits)
  private val reciprocalBits = reciprocalFractionBits + 1

  val sIdle :: sQuantize :: sDrain :: sOutput :: Nil = Enum(4)
  val state = RegInit(sIdle)
  val index = RegInit(0.U(6.W))
  val quality = Reg(UInt(7.W))
  val isLuminance = Reg(Bool())
  val inputCoefficients = Reg(Vec(HjpegConstants.BlockSize, SInt(coefficientBits.W)))
  val outputCoefficients = Reg(Vec(HjpegConstants.BlockSize, SInt(coefficientBits.W)))
  val lookupValid = RegInit(false.B)
  val lookupIndex = Reg(UInt(6.W))
  val lookupNumerator = Reg(UInt(numeratorBits.W))
  val lookupDivisor = Reg(UInt(8.W))
  val lookupNegative = Reg(Bool())
  val estimateValid = RegInit(false.B)
  val estimateIndex = Reg(UInt(6.W))
  val estimateNumerator = Reg(UInt(numeratorBits.W))
  val estimateDivisor = Reg(UInt(8.W))
  val estimateNegative = Reg(Bool())
  val estimateQuotient = Reg(UInt(numeratorBits.W))

  io.input.ready := state === sIdle
  io.output.valid := state === sOutput
  for (coefficientIndex <- 0 until HjpegConstants.BlockSize) {
    io.output.bits.coefficients(coefficientIndex) := outputCoefficients(coefficientIndex)
  }

  when(io.input.fire) {
    quality := io.quality
    isLuminance := io.isLuminance
    for (coefficientIndex <- 0 until HjpegConstants.BlockSize) {
      inputCoefficients(coefficientIndex) := io.input.bits.coefficients(coefficientIndex)
    }
    index := 0.U
    state := sQuantize
  }

  val tableValue = Module(new JpegQuantTableValue())
  tableValue.io.quality := quality
  tableValue.io.isLuminance := isLuminance
  tableValue.io.index := index

  val coefficient = inputCoefficients(index)
  val negative = coefficient < 0.S
  val magnitude = Mux(negative, -coefficient, coefficient).asUInt
  val coefficientDivisor = tableValue.io.value
  val roundedNumerator = magnitude.pad(numeratorBits) + (coefficientDivisor >> 1).pad(numeratorBits)
  val reciprocals = VecInit((0 to 255).map { divisor =>
    (if (divisor == 0) 0 else QuantizeReciprocal.value(divisor, coefficientBits)).U(reciprocalBits.W)
  })
  val reciprocalProduct = lookupNumerator * reciprocals(lookupDivisor)
  val nextEstimatedQuotient =
    reciprocalProduct(reciprocalFractionBits + numeratorBits - 1, reciprocalFractionBits)
  val estimatedProduct = estimateQuotient * estimateDivisor
  val remainder = estimateNumerator.pad(estimatedProduct.getWidth) - estimatedProduct
  val correctedQuotient = estimateQuotient + (remainder >= estimateDivisor).asUInt
  val signedRounded = correctedQuotient.asSInt

  lookupValid := state === sQuantize
  when(state === sQuantize) {
    lookupIndex := index
    lookupNumerator := roundedNumerator
    lookupDivisor := coefficientDivisor
    lookupNegative := negative
    when(index === (HjpegConstants.BlockSize - 1).U) {
      state := sDrain
    }.otherwise {
      index := index + 1.U
    }
  }

  estimateValid := lookupValid
  when(lookupValid) {
    estimateIndex := lookupIndex
    estimateNumerator := lookupNumerator
    estimateDivisor := lookupDivisor
    estimateNegative := lookupNegative
    estimateQuotient := nextEstimatedQuotient
  }

  when(estimateValid) {
    outputCoefficients(estimateIndex) := Mux(estimateNegative, -signedRounded, signedRounded)
    when(estimateIndex === (HjpegConstants.BlockSize - 1).U) {
      state := sOutput
    }
  }

  when(io.output.fire) {
    state := sIdle
  }
}
