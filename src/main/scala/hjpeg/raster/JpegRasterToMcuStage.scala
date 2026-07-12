// See README.md for license details.

package hjpeg

import chisel3._
import chisel3.util._

/** Buffers one 8-row raster stripe and emits 8x8, 4:4:4 MCUs.
  *
  * This stage is the first raster-order frame buffer. It accepts pixels in
  * row-major order, stores level-shifted Y/Cb/Cr samples for up to eight rows,
  * then emits MCUs left-to-right for that stripe. Samples are striped across
  * eight synchronous banks by column modulo eight, so one complete 8-sample
  * block row is loaded per cycle. Partial right and bottom edges are padded by
  * broadcasting the final valid column or row sample.
  */
class JpegRasterToMcuStage(c: HjpegConfig = HjpegConfig(), sampleBits: Int = 9, coefficientBits: Int = 16)
    extends Module {
  val io = IO(new Bundle {
    val config = Input(new FrameConfig(c))
    val input = Flipped(Decoupled(new RgbPixel(c)))
    val output = Decoupled(new ZigZagMinimumCodedUnitPacket(coefficientBits))
  })

  private val StripeRows = HjpegConstants.BlockDim
  private val ReadLanes = HjpegConstants.BlockDim
  private val BankCount = ReadLanes
  private val BankIndexBits = log2Ceil(BankCount)
  private val BankColumns = (c.maxFrameWidth + BankCount - 1) / BankCount
  private val BankSamples = StripeRows * BankColumns
  private val BankAddressBits = log2Ceil(BankSamples).max(1)

  val sCollect :: sLoad :: sTransform :: sEmit :: Nil = Enum(4)
  val state = RegInit(sCollect)
  val blockX = RegInit(0.U(c.coordBits.W))
  val currentStripeLast = RegInit(false.B)
  val lastRowInStripe = RegInit(0.U(3.W))
  val loadRow = RegInit(0.U(3.W))
  val loadReadRow = RegInit(0.U(3.W))
  val loadReadBanks = Reg(Vec(ReadLanes, UInt(BankIndexBits.W)))
  val loadAllIssued = RegInit(false.B)
  val issueBlock = RegInit(0.U(2.W))
  val captureBlock = RegInit(0.U(2.W))

  val ySampleBanks = Seq.fill(BankCount)(SyncReadMem(BankSamples, SInt(sampleBits.W)))
  val cbSampleBanks = Seq.fill(BankCount)(SyncReadMem(BankSamples, SInt(sampleBits.W)))
  val crSampleBanks = Seq.fill(BankCount)(SyncReadMem(BankSamples, SInt(sampleBits.W)))
  val yBlock = Reg(Vec(HjpegConstants.BlockSize, SInt(sampleBits.W)))
  val cbBlock = Reg(Vec(HjpegConstants.BlockSize, SInt(sampleBits.W)))
  val crBlock = Reg(Vec(HjpegConstants.BlockSize, SInt(sampleBits.W)))
  val yCoefficients = Reg(new ZigZagCoefficientBlock(coefficientBits))
  val cbCoefficients = Reg(new ZigZagCoefficientBlock(coefficientBits))
  val crCoefficients = Reg(new ZigZagCoefficientBlock(coefficientBits))

  val rowInStripe = io.input.bits.y(2, 0)
  val writeBank = io.input.bits.x(BankIndexBits - 1, 0)
  val writeBankColumn = io.input.bits.x >> BankIndexBits
  val writeAddress = (rowInStripe * BankColumns.U + writeBankColumn)(BankAddressBits - 1, 0)
  val lastPixelInStripe =
    io.input.bits.x === io.config.xsize - 1.U &&
      (rowInStripe === (StripeRows - 1).U || io.input.bits.y === io.config.ysize - 1.U)

  val (yComponent, cbComponent, crComponent) =
    JpegColorConversion.rgbToYCbCr(io.input.bits.r, io.input.bits.g, io.input.bits.b, c.pixelBits)

  io.input.ready := state === sCollect

  when(io.input.fire) {
    for (bank <- 0 until BankCount) {
      when(writeBank === bank.U) {
        ySampleBanks(bank).write(writeAddress, (yComponent.zext - 128.S)(sampleBits - 1, 0).asSInt)
        cbSampleBanks(bank).write(writeAddress, (cbComponent.zext - 128.S)(sampleBits - 1, 0).asSInt)
        crSampleBanks(bank).write(writeAddress, (crComponent.zext - 128.S)(sampleBits - 1, 0).asSInt)
      }
    }
    when(lastPixelInStripe) {
      state := sLoad
      blockX := 0.U
      currentStripeLast := io.input.bits.y + 1.U >= io.config.ysize
      lastRowInStripe := rowInStripe
      loadRow := 0.U
      loadAllIssued := false.B
      issueBlock := 0.U
      captureBlock := 0.U
    }
  }

  val readRow = Mux(loadRow > lastRowInStripe, lastRowInStripe, loadRow)
  val laneReadBanks = Wire(Vec(ReadLanes, UInt(BankIndexBits.W)))
  val laneReadAddresses = Wire(Vec(ReadLanes, UInt(BankAddressBits.W)))
  for (lane <- 0 until ReadLanes) {
    val requestedCol = blockX + lane.U
    val readCol = Mux(requestedCol >= io.config.xsize, io.config.xsize - 1.U, requestedCol)
    laneReadBanks(lane) := readCol(BankIndexBits - 1, 0)
    laneReadAddresses(lane) :=
      (readRow * BankColumns.U + (readCol >> BankIndexBits))(BankAddressBits - 1, 0)
  }

  val loadReadEnable = state === sLoad && !loadAllIssued
  val yBankReadData = Wire(Vec(BankCount, SInt(sampleBits.W)))
  val cbBankReadData = Wire(Vec(BankCount, SInt(sampleBits.W)))
  val crBankReadData = Wire(Vec(BankCount, SInt(sampleBits.W)))
  for (bank <- 0 until BankCount) {
    val laneMatches = VecInit((0 until ReadLanes).map(lane => laneReadBanks(lane) === bank.U))
    val bankReadEnable = loadReadEnable && laneMatches.asUInt.orR
    val bankReadAddress = PriorityMux(
      (0 until ReadLanes).map(lane => laneMatches(lane) -> laneReadAddresses(lane)))
    yBankReadData(bank) := ySampleBanks(bank).read(bankReadAddress, bankReadEnable)
    cbBankReadData(bank) := cbSampleBanks(bank).read(bankReadAddress, bankReadEnable)
    crBankReadData(bank) := crSampleBanks(bank).read(bankReadAddress, bankReadEnable)
  }
  val loadReadValid = RegNext(loadReadEnable, false.B)

  when(loadReadEnable) {
    loadReadRow := loadRow
    loadReadBanks := laneReadBanks
    when(loadRow === (StripeRows - 1).U) {
      loadAllIssued := true.B
    }.otherwise {
      loadRow := loadRow + 1.U
    }
  }

  when(state === sLoad && loadReadValid) {
    for (lane <- 0 until ReadLanes) {
      val blockIndex = Cat(loadReadRow, lane.U(BankIndexBits.W))
      yBlock(blockIndex) := yBankReadData(loadReadBanks(lane))
      cbBlock(blockIndex) := cbBankReadData(loadReadBanks(lane))
      crBlock(blockIndex) := crBankReadData(loadReadBanks(lane))
    }
    when(loadReadRow === (StripeRows - 1).U) {
      state := sTransform
      issueBlock := 0.U
      captureBlock := 0.U
    }
  }

  val transform = Module(new JpegBlockTransformStage(sampleBits, coefficientBits))

  transform.io.quality := io.config.quality
  transform.io.isLuminance := issueBlock === 0.U
  transform.io.input.valid := state === sTransform && issueBlock < HjpegConstants.Components.U

  for (row <- 0 until HjpegConstants.BlockDim) {
    for (col <- 0 until HjpegConstants.BlockDim) {
      val blockSample = row * HjpegConstants.BlockDim + col
      transform.io.input.bits.samples(blockSample) :=
        Mux(issueBlock === 0.U, yBlock(blockSample), Mux(issueBlock === 1.U, cbBlock(blockSample), crBlock(blockSample)))
    }
  }

  transform.io.output.ready := state === sTransform

  when(transform.io.input.fire) {
    issueBlock := issueBlock + 1.U
  }

  when(transform.io.output.fire) {
    when(captureBlock === 0.U) {
      yCoefficients := transform.io.output.bits
      captureBlock := 1.U
    }.elsewhen(captureBlock === 1.U) {
      cbCoefficients := transform.io.output.bits
      captureBlock := 2.U
    }.otherwise {
      crCoefficients := transform.io.output.bits
      state := sEmit
    }
  }

  val lastBlockInStripe = blockX + HjpegConstants.BlockDim.U >= io.config.xsize

  io.output.valid := state === sEmit
  io.output.bits.mcu.yBlockCount := 1.U
  io.output.bits.mcu.y := yCoefficients
  io.output.bits.mcu.y1 := yCoefficients
  io.output.bits.mcu.y2 := yCoefficients
  io.output.bits.mcu.y3 := yCoefficients
  io.output.bits.mcu.cb := cbCoefficients
  io.output.bits.mcu.cr := crCoefficients
  io.output.bits.last := lastBlockInStripe && currentStripeLast

  when(io.output.fire) {
    when(lastBlockInStripe) {
      state := sCollect
      blockX := 0.U
    }.otherwise {
      blockX := blockX + HjpegConstants.BlockDim.U
      loadRow := 0.U
      loadAllIssued := false.B
      issueBlock := 0.U
      captureBlock := 0.U
      state := sLoad
    }
  }
}
