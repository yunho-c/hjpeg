// See README.md for license details.

package hjpeg

import chisel3._
import chisel3.util._

/** Four-lane, bit-exact fixed-point 8x8 DCT.
  *
  * The Q14 cosine matrix has exact even/odd symmetry. For each one-dimensional
  * transform, the input is first reduced to the four butterflies
  * `x0 +/- x7` through `x3 +/- x4`. Even frequencies consume the sums and odd
  * frequencies consume the differences, so each output needs four multiplies
  * instead of eight without changing the integer dot product.
  *
  * Four frequency lanes are issued per cycle. Row and column passes operate
  * concurrently through three banked transpose buffers; two output banks hold
  * completed blocks under downstream backpressure. Pair formation is
  * registered before the multiply/add tree. Each four-term dot product is split
  * into registered two-term partial sums and a short final add; the column sums
  * are also registered before final rounding. No rounding occurs between
  * passes; the final Q28 value uses nearest rounding with halves away from zero,
  * exactly matching [[Dct8x8Stage]]. With an unstalled consumer, blocks are
  * accepted at a 16-cycle interval after the first input.
  */
class PipelinedDct8x8Stage(sampleBits: Int = 9, coefficientBits: Int = 16) extends Module {
  val io = IO(new Bundle {
    val input = Flipped(Decoupled(new LevelShiftedSampleBlock(sampleBits)))
    val output = Decoupled(new DctCoefficientBlock(coefficientBits))
  })

  private val Lanes = 4
  private val RowBanks = 3
  private val OutputBanks = 2
  private val CosineBits = 16
  private val RowPartialBits = sampleBits + 1 + CosineBits + 1
  private val ColumnPartialBits = 33 + CosineBits + 1
  private val constants = Dct8x8Constants.CosineQ14
  private val cosine = VecInit(constants.map(row => VecInit(row.map(_.S(CosineBits.W)))))

  private def roundShiftSigned(value: SInt, shift: Int): SInt = {
    val negative = value < 0.S
    val magnitude = Mux(negative, -value, value).asUInt
    val rounded = (magnitude + (BigInt(1) << (shift - 1)).U) >> shift
    Mux(negative, -rounded.asSInt, rounded.asSInt)
  }

  private def nextRowBank(bank: UInt): UInt = Mux(bank === (RowBanks - 1).U, 0.U, bank + 1.U)

  val rowFree :: rowProcessing :: rowComplete :: rowColumnProcessing :: Nil = Enum(4)
  val outputFree :: outputProcessing :: outputComplete :: Nil = Enum(3)

  val samples = Reg(Vec(HjpegConstants.BlockSize, SInt(sampleBits.W)))
  val rowBuffers = Reg(Vec(RowBanks, Vec(HjpegConstants.BlockSize, SInt(32.W))))
  val outputBuffers = Reg(Vec(OutputBanks, Vec(HjpegConstants.BlockSize, SInt(coefficientBits.W))))
  val rowStates = RegInit(VecInit(Seq.fill(RowBanks)(rowFree)))
  val outputStates = RegInit(VecInit(Seq.fill(OutputBanks)(outputFree)))

  val rowActive = RegInit(false.B)
  val rowGroup = RegInit(0.U(4.W))
  val rowBank = RegInit(0.U(2.W))
  val rowAllocateBank = RegInit(0.U(2.W))

  val rowPairValid = RegInit(false.B)
  val rowPairBank = Reg(UInt(2.W))
  val rowPairGroup = Reg(UInt(4.W))
  val rowPairs = Reg(Vec(Lanes, Vec(Lanes, SInt((sampleBits + 1).W))))

  val rowPartialValid = RegInit(false.B)
  val rowPartialBank = Reg(UInt(2.W))
  val rowPartialGroup = Reg(UInt(4.W))
  val rowPartialSums = Reg(Vec(Lanes, Vec(2, SInt(RowPartialBits.W))))

  val canAcceptInput = rowStates(rowAllocateBank) === rowFree && (!rowActive || rowGroup === 15.U)
  io.input.ready := canAcceptInput

  val rowIssueValid = rowActive
  val rowSample = rowGroup(3, 1)
  val rowFrequencyBase = Cat(rowGroup(0), 0.U(2.W))

  rowPairValid := rowIssueValid
  when(rowIssueValid) {
    rowPairBank := rowBank
    rowPairGroup := rowGroup
    for (lane <- 0 until Lanes) {
      val frequency = rowFrequencyBase + lane.U
      for (term <- 0 until Lanes) {
        val low = samples(Cat(rowSample, term.U(3.W)))
        val high = samples(Cat(rowSample, (HjpegConstants.BlockDim - 1 - term).U(3.W)))
        rowPairs(lane)(term) := Mux(frequency(0), low -& high, low +& high)
      }
    }

    when(rowGroup === 15.U) {
      rowActive := false.B
    }.otherwise {
      rowGroup := rowGroup + 1.U
    }
  }

  when(io.input.fire) {
    for (index <- 0 until HjpegConstants.BlockSize) {
      samples(index) := io.input.bits.samples(index)
    }
    rowStates(rowAllocateBank) := rowProcessing
    rowBank := rowAllocateBank
    rowAllocateBank := nextRowBank(rowAllocateBank)
    rowGroup := 0.U
    rowActive := true.B
  }

  rowPartialValid := rowPairValid
  when(rowPairValid) {
    rowPartialBank := rowPairBank
    rowPartialGroup := rowPairGroup
    val frequencyBase = Cat(rowPairGroup(0), 0.U(2.W))
    for (lane <- 0 until Lanes) {
      val frequency = frequencyBase + lane.U
      for (partial <- 0 until 2) {
        val firstTerm = partial * 2
        val firstProduct = (rowPairs(lane)(firstTerm) * cosine(frequency)(firstTerm)).asSInt
        val secondProduct = (rowPairs(lane)(firstTerm + 1) * cosine(frequency)(firstTerm + 1)).asSInt
        rowPartialSums(lane)(partial) := firstProduct +& secondProduct
      }
    }
  }

  when(rowPartialValid) {
    val outputRow = rowPartialGroup(3, 1)
    val frequencyBase = Cat(rowPartialGroup(0), 0.U(2.W))
    for (lane <- 0 until Lanes) {
      val frequency = frequencyBase + lane.U
      val sum = rowPartialSums(lane)(0) +& rowPartialSums(lane)(1)
      rowBuffers(rowPartialBank)(Cat(outputRow, frequency)) := sum
    }
    when(rowPartialGroup === 15.U) {
      rowStates(rowPartialBank) := rowComplete
    }
  }

  val columnActive = RegInit(false.B)
  val columnGroup = RegInit(0.U(4.W))
  val columnRowBank = RegInit(0.U(2.W))
  val columnOutputBank = RegInit(0.U(1.W))
  val columnReadBank = RegInit(0.U(2.W))
  val columnAllocateOutputBank = RegInit(0.U(1.W))

  val columnPairValid = RegInit(false.B)
  val columnPairOutputBank = Reg(UInt(1.W))
  val columnPairGroup = Reg(UInt(4.W))
  val columnPairs = Reg(Vec(Lanes, Vec(Lanes, SInt(33.W))))

  val columnPartialValid = RegInit(false.B)
  val columnPartialOutputBank = Reg(UInt(1.W))
  val columnPartialGroup = Reg(UInt(4.W))
  val columnPartialSums = Reg(Vec(Lanes, Vec(2, SInt(ColumnPartialBits.W))))

  // A 33x16-bit product is 49 bits. The registered pair sums are 50 bits and
  // the final widening add is 51 bits, exactly matching the full dot product.
  val columnSumValid = RegInit(false.B)
  val columnSumOutputBank = Reg(UInt(1.W))
  val columnSumGroup = Reg(UInt(4.W))
  val columnSums = Reg(Vec(Lanes, SInt(51.W)))

  val columnCanStart =
    !columnActive &&
      rowStates(columnReadBank) === rowComplete &&
      outputStates(columnAllocateOutputBank) === outputFree
  val columnIssueValid = columnActive || columnCanStart
  val issuedColumnGroup = Mux(columnActive, columnGroup, 0.U)
  val issuedRowBank = Mux(columnActive, columnRowBank, columnReadBank)
  val issuedOutputBank = Mux(columnActive, columnOutputBank, columnAllocateOutputBank)
  val columnSample = issuedColumnGroup(3, 1)
  val columnFrequencyBase = Cat(issuedColumnGroup(0), 0.U(2.W))

  columnPairValid := columnIssueValid
  when(columnIssueValid) {
    columnPairOutputBank := issuedOutputBank
    columnPairGroup := issuedColumnGroup
    for (lane <- 0 until Lanes) {
      val frequency = columnFrequencyBase + lane.U
      for (term <- 0 until Lanes) {
        val low = rowBuffers(issuedRowBank)(Cat(term.U(3.W), columnSample))
        val high = rowBuffers(issuedRowBank)(Cat((HjpegConstants.BlockDim - 1 - term).U(3.W), columnSample))
        columnPairs(lane)(term) := Mux(frequency(0), low -& high, low +& high)
      }
    }

    when(issuedColumnGroup === 15.U) {
      columnActive := false.B
      rowStates(issuedRowBank) := rowFree
      columnReadBank := nextRowBank(columnReadBank)
    }.otherwise {
      columnActive := true.B
      columnGroup := issuedColumnGroup + 1.U
      columnRowBank := issuedRowBank
      columnOutputBank := issuedOutputBank
    }
  }

  when(columnCanStart) {
    rowStates(columnReadBank) := rowColumnProcessing
    outputStates(columnAllocateOutputBank) := outputProcessing
    columnAllocateOutputBank := ~columnAllocateOutputBank
  }

  columnPartialValid := columnPairValid
  when(columnPairValid) {
    columnPartialOutputBank := columnPairOutputBank
    columnPartialGroup := columnPairGroup
    val frequencyBase = Cat(columnPairGroup(0), 0.U(2.W))
    for (lane <- 0 until Lanes) {
      val frequency = frequencyBase + lane.U
      for (partial <- 0 until 2) {
        val firstTerm = partial * 2
        val firstProduct = (columnPairs(lane)(firstTerm) * cosine(frequency)(firstTerm)).asSInt
        val secondProduct = (columnPairs(lane)(firstTerm + 1) * cosine(frequency)(firstTerm + 1)).asSInt
        columnPartialSums(lane)(partial) := firstProduct +& secondProduct
      }
    }
  }

  columnSumValid := columnPartialValid
  when(columnPartialValid) {
    columnSumOutputBank := columnPartialOutputBank
    columnSumGroup := columnPartialGroup
    for (lane <- 0 until Lanes) {
      columnSums(lane) := columnPartialSums(lane)(0) +& columnPartialSums(lane)(1)
    }
  }

  when(columnSumValid) {
    val outputColumn = columnSumGroup(3, 1)
    val frequencyBase = Cat(columnSumGroup(0), 0.U(2.W))
    for (lane <- 0 until Lanes) {
      val frequency = frequencyBase + lane.U
      val rounded = roundShiftSigned(columnSums(lane), Dct8x8Constants.FractionBits * 2)
      outputBuffers(columnSumOutputBank)(Cat(frequency, outputColumn)) :=
        rounded(coefficientBits - 1, 0).asSInt
    }
    when(columnSumGroup === 15.U) {
      outputStates(columnSumOutputBank) := outputComplete
    }
  }

  val outputReadBank = RegInit(0.U(1.W))
  io.output.valid := outputStates(outputReadBank) === outputComplete
  for (index <- 0 until HjpegConstants.BlockSize) {
    io.output.bits.coefficients(index) := outputBuffers(outputReadBank)(index)
  }

  when(io.output.fire) {
    outputStates(outputReadBank) := outputFree
    outputReadBank := ~outputReadBank
  }
}
