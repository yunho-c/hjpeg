// See README.md for license details.

package hjpeg

import chisel3._
import chisel3.util._

private[hjpeg] object QuantizeQualityScale {
  def value(quality: Int): Int = {
    val clamped = quality.max(1).min(100)
    if (clamped < 50) 5000 / clamped else 200 - 2 * clamped
  }
}

/** Four-lane, banked quantizer for natural-order DCT blocks.
  *
  * Four adjacent coefficients are issued each cycle. Every lane retains the
  * exact quality scaling, floor-reciprocal estimate, and multiply-back
  * correction used by [[QuantizeBlockStage]]. The quality scale is an exact
  * constant ROM for the seven-bit input; registered scaling, numerator, and
  * reciprocal stages keep the combinational timing cones short. Two banks
  * allow the next block to be captured and processed while the previous result
  * is waiting at the output. Under normal flow the processing engine starts one
  * block every 16 cycles and preserves input order under arbitrary output
  * backpressure.
  */
class PipelinedQuantizeBlockStage(coefficientBits: Int = 16) extends Module {
  val io = IO(new Bundle {
    val quality = Input(UInt(7.W))
    val isLuminance = Input(Bool())
    val input = Flipped(Decoupled(new DctCoefficientBlock(coefficientBits)))
    val output = Decoupled(new QuantizedCoefficientBlock(coefficientBits))
  })

  private val Banks = 2
  private val Lanes = 4
  private val numeratorBits = coefficientBits + 2
  private val reciprocalFractionBits = QuantizeReciprocal.fractionBits(coefficientBits)
  private val reciprocalBits = reciprocalFractionBits + 1

  val bankFree :: bankPending :: bankProcessing :: bankComplete :: Nil = Enum(4)
  val bankStates = RegInit(VecInit(Seq.fill(Banks)(bankFree)))
  val inputCoefficients = Reg(Vec(Banks, Vec(HjpegConstants.BlockSize, SInt(coefficientBits.W))))
  val outputCoefficients = Reg(Vec(Banks, Vec(HjpegConstants.BlockSize, SInt(coefficientBits.W))))
  val qualities = Reg(Vec(Banks, UInt(7.W)))
  val luminance = Reg(Vec(Banks, Bool()))

  val inputBank = RegInit(0.U(1.W))
  io.input.ready := bankStates(inputBank) === bankFree
  when(io.input.fire) {
    for (index <- 0 until HjpegConstants.BlockSize) {
      inputCoefficients(inputBank)(index) := io.input.bits.coefficients(index)
    }
    qualities(inputBank) := io.quality
    luminance(inputBank) := io.isLuminance
    bankStates(inputBank) := bankPending
    inputBank := ~inputBank
  }

  val processActive = RegInit(false.B)
  val processGroup = RegInit(0.U(4.W))
  val processBank = RegInit(0.U(1.W))
  val processReadBank = RegInit(0.U(1.W))
  val processCanStart = !processActive && bankStates(processReadBank) === bankPending
  val issueValid = processActive || processCanStart
  val issueGroup = Mux(processActive, processGroup, 0.U)
  val issueBank = Mux(processActive, processBank, processReadBank)

  when(processCanStart) {
    bankStates(processReadBank) := bankProcessing
  }

  when(issueValid) {
    when(issueGroup === 15.U) {
      processActive := false.B
      processReadBank := ~processReadBank
    }.otherwise {
      processActive := true.B
      processGroup := issueGroup + 1.U
      processBank := issueBank
    }
  }

  // Quality scaling is shared across all four lanes. A 128-entry constant ROM
  // replaces the variable 5000/quality divider, which is both exact for the
  // seven-bit input and substantially shorter in synthesis.
  val luminanceTable = VecInit(JpegTables.StandardLuminanceQuant.map(_.U(8.W)))
  val chrominanceTable = VecInit(JpegTables.StandardChrominanceQuant.map(_.U(8.W)))
  val qualityScales = VecInit((0 until 128).map(quality => QuantizeQualityScale.value(quality).U(13.W)))
  val reciprocals = VecInit((0 to 255).map { divisor =>
    (if (divisor == 0) 0 else QuantizeReciprocal.value(divisor, coefficientBits)).U(reciprocalBits.W)
  })

  // Stage 1 selects the banked coefficients and constant table/quality values.
  val tableValid = RegInit(false.B)
  val tableBank = Reg(UInt(1.W))
  val tableGroup = Reg(UInt(4.W))
  val tableCoefficients = Reg(Vec(Lanes, SInt(coefficientBits.W)))
  val tableBases = Reg(Vec(Lanes, UInt(8.W)))
  val tableQualityScale = Reg(UInt(13.W))

  tableValid := issueValid
  when(issueValid) {
    tableBank := issueBank
    tableGroup := issueGroup
    tableQualityScale := qualityScales(qualities(issueBank))
    for (lane <- 0 until Lanes) {
      val index = Cat(issueGroup, lane.U(2.W))
      tableCoefficients(lane) := inputCoefficients(issueBank)(index)
      tableBases(lane) := Mux(luminance(issueBank), luminanceTable(index), chrominanceTable(index))
    }
  }

  // Stage 2 performs quality scaling. The division is by the constant 100;
  // registering its result keeps the multiply/constant-divide cone separate
  // from coefficient selection and reciprocal multiplication.
  val divisorValid = RegInit(false.B)
  val divisorBank = Reg(UInt(1.W))
  val divisorGroup = Reg(UInt(4.W))
  val divisorCoefficients = Reg(Vec(Lanes, SInt(coefficientBits.W)))
  val divisors = Reg(Vec(Lanes, UInt(8.W)))

  divisorValid := tableValid
  when(tableValid) {
    divisorBank := tableBank
    divisorGroup := tableGroup
    for (lane <- 0 until Lanes) {
      val scaled = ((tableBases(lane) * tableQualityScale) + 50.U) / 100.U
      divisorCoefficients(lane) := tableCoefficients(lane)
      divisors(lane) := Mux(scaled === 0.U, 1.U, Mux(scaled > 255.U, 255.U, scaled(7, 0)))
    }
  }

  // Stage 3 forms the rounded unsigned numerators.
  val lookupValid = RegInit(false.B)
  val lookupBank = Reg(UInt(1.W))
  val lookupGroup = Reg(UInt(4.W))
  val lookupNumerators = Reg(Vec(Lanes, UInt(numeratorBits.W)))
  val lookupDivisors = Reg(Vec(Lanes, UInt(8.W)))
  val lookupNegatives = Reg(Vec(Lanes, Bool()))

  lookupValid := divisorValid
  when(divisorValid) {
    lookupBank := divisorBank
    lookupGroup := divisorGroup
    for (lane <- 0 until Lanes) {
      val coefficient = divisorCoefficients(lane)
      val negative = coefficient < 0.S
      val magnitude = Mux(negative, -coefficient, coefficient).asUInt
      val divisor = divisors(lane)
      lookupNumerators(lane) := magnitude.pad(numeratorBits) + (divisor >> 1).pad(numeratorBits)
      lookupDivisors(lane) := divisor
      lookupNegatives(lane) := negative
    }
  }

  val estimateValid = RegInit(false.B)
  val estimateBank = Reg(UInt(1.W))
  val estimateGroup = Reg(UInt(4.W))
  val estimateNumerators = Reg(Vec(Lanes, UInt(numeratorBits.W)))
  val estimateDivisors = Reg(Vec(Lanes, UInt(8.W)))
  val estimateNegatives = Reg(Vec(Lanes, Bool()))
  val estimateQuotients = Reg(Vec(Lanes, UInt(numeratorBits.W)))

  estimateValid := lookupValid
  when(lookupValid) {
    estimateBank := lookupBank
    estimateGroup := lookupGroup
    for (lane <- 0 until Lanes) {
      val reciprocalProduct = lookupNumerators(lane) * reciprocals(lookupDivisors(lane))
      estimateNumerators(lane) := lookupNumerators(lane)
      estimateDivisors(lane) := lookupDivisors(lane)
      estimateNegatives(lane) := lookupNegatives(lane)
      estimateQuotients(lane) :=
        reciprocalProduct(reciprocalFractionBits + numeratorBits - 1, reciprocalFractionBits)
    }
  }

  when(estimateValid) {
    for (lane <- 0 until Lanes) {
      val estimatedProduct = estimateQuotients(lane) * estimateDivisors(lane)
      val remainder = estimateNumerators(lane).pad(estimatedProduct.getWidth) - estimatedProduct
      val corrected = estimateQuotients(lane) + (remainder >= estimateDivisors(lane)).asUInt
      val signed = corrected.asSInt
      outputCoefficients(estimateBank)(Cat(estimateGroup, lane.U(2.W))) :=
        Mux(estimateNegatives(lane), -signed, signed)
    }
    when(estimateGroup === 15.U) {
      bankStates(estimateBank) := bankComplete
    }
  }

  val outputBank = RegInit(0.U(1.W))
  io.output.valid := bankStates(outputBank) === bankComplete
  for (index <- 0 until HjpegConstants.BlockSize) {
    io.output.bits.coefficients(index) := outputCoefficients(outputBank)(index)
  }
  when(io.output.fire) {
    bankStates(outputBank) := bankFree
    outputBank := ~outputBank
  }
}
