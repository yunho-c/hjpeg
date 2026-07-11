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

  private val divideBits = coefficientBits + 2

  val sIdle :: sStartCoefficient :: sDivide :: sWriteCoefficient :: sOutput :: Nil = Enum(5)
  val state = RegInit(sIdle)
  val index = RegInit(0.U(6.W))
  val quality = Reg(UInt(7.W))
  val isLuminance = Reg(Bool())
  val inputCoefficients = Reg(Vec(HjpegConstants.BlockSize, SInt(coefficientBits.W)))
  val outputCoefficients = Reg(Vec(HjpegConstants.BlockSize, SInt(coefficientBits.W)))
  val dividend = Reg(UInt(divideBits.W))
  val divisorReg = Reg(UInt(8.W))
  val quotient = Reg(UInt(divideBits.W))
  val remainder = Reg(UInt((divideBits + 1).W))
  val divideBit = Reg(UInt(log2Ceil(divideBits).W))
  val coefficientNegative = Reg(Bool())

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
    state := sStartCoefficient
  }

  val tableValue = Module(new JpegQuantTableValue())
  tableValue.io.quality := quality
  tableValue.io.isLuminance := isLuminance
  tableValue.io.index := index

  val coefficient = inputCoefficients(index)
  val negative = coefficient < 0.S
  val magnitude = Mux(negative, -coefficient, coefficient).asUInt
  val coefficientDivisor = tableValue.io.value
  val roundedNumerator = magnitude.pad(divideBits) + (coefficientDivisor >> 1).pad(divideBits)

  when(state === sStartCoefficient) {
    dividend := roundedNumerator(divideBits - 1, 0)
    divisorReg := tableValue.io.value
    quotient := 0.U
    remainder := 0.U
    divideBit := (divideBits - 1).U
    coefficientNegative := negative
    state := sDivide
  }

  val shiftedRemainder = Cat(remainder(divideBits - 1, 0), dividend(divideBit)).asUInt
  val wideDivisor = divisorReg.pad(divideBits + 1)
  val quotientBit = shiftedRemainder >= wideDivisor
  val nextRemainder = Mux(quotientBit, shiftedRemainder - wideDivisor, shiftedRemainder)
  val quotientMask = 1.U(divideBits.W) << divideBit

  when(state === sDivide) {
    remainder := nextRemainder
    quotient := Mux(quotientBit, quotient | quotientMask, quotient)
    when(divideBit === 0.U) {
      state := sWriteCoefficient
    }.otherwise {
      divideBit := divideBit - 1.U
    }
  }

  val signedRounded = quotient.asSInt

  when(state === sWriteCoefficient) {
    outputCoefficients(index) := Mux(coefficientNegative, -signedRounded, signedRounded)
    when(index === (HjpegConstants.BlockSize - 1).U) {
      state := sOutput
    }.otherwise {
      index := index + 1.U
      state := sStartCoefficient
    }
  }

  when(io.output.fire) {
    state := sIdle
  }
}
