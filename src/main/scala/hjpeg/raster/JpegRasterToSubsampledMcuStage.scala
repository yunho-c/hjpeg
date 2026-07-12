// See README.md for license details.

package hjpeg

import chisel3._
import chisel3.util._

/** Buffers one 16-row raster band and emits 16x16, 4:2:0 MCUs.
  *
  * Each output MCU contains four luminance blocks followed by one downsampled
  * Cb block and one downsampled Cr block. Eight synchronous banks use row
  * parity and column modulo four, allowing four adjacent luma samples or one
  * complete 2x2 chroma footprint to be read per cycle. Edge samples are padded
  * by replicating the last valid row or column.
  */
class JpegRasterToSubsampledMcuStage(
    c: HjpegConfig = HjpegConfig(),
    sampleBits: Int = 9,
    coefficientBits: Int = 16)
    extends Module {
  val io = IO(new Bundle {
    val config = Input(new FrameConfig(c))
    val input = Flipped(Decoupled(new RgbPixel(c)))
    val output = Decoupled(new ZigZagMinimumCodedUnitPacket(coefficientBits))
  })

  private val McuDim = HjpegConstants.BlockDim * 2
  private val ReadLanes = 4
  private val ColumnBankCount = ReadLanes
  private val ColumnBankBits = log2Ceil(ColumnBankCount)
  private val BankCount = ColumnBankCount * 2
  private val BankIndexBits = log2Ceil(BankCount)
  private val BankColumns = (c.maxFrameWidth + ColumnBankCount - 1) / ColumnBankCount
  private val BankRows = McuDim / 2
  private val BankSamples = BankRows * BankColumns
  private val BankAddressBits = log2Ceil(BankSamples).max(1)

  val sCollect :: sLoad :: sTransform :: sEmit :: Nil = Enum(4)
  val state = RegInit(sCollect)
  val blockX = RegInit(0.U(c.coordBits.W))
  val currentBandLast = RegInit(false.B)
  val lastRowInBand = RegInit(0.U(4.W))
  val loadPhase = RegInit(0.U(3.W))
  val loadSample = RegInit(0.U(6.W))
  val loadReadPhase = RegInit(0.U(3.W))
  val loadReadSample = RegInit(0.U(6.W))
  val loadReadYBanks = Reg(Vec(ReadLanes, UInt(BankIndexBits.W)))
  val loadReadChromaBanks = Reg(Vec(ReadLanes, UInt(BankIndexBits.W)))
  val loadAllIssued = RegInit(false.B)
  val issueBlock = RegInit(0.U(3.W))
  val captureBlock = RegInit(0.U(3.W))

  val ySampleBanks = Seq.fill(BankCount)(SyncReadMem(BankSamples, SInt(sampleBits.W)))
  val cbSampleBanks = Seq.fill(BankCount)(SyncReadMem(BankSamples, SInt(sampleBits.W)))
  val crSampleBanks = Seq.fill(BankCount)(SyncReadMem(BankSamples, SInt(sampleBits.W)))
  val y0Block = Reg(Vec(HjpegConstants.BlockSize, SInt(sampleBits.W)))
  val y1Block = Reg(Vec(HjpegConstants.BlockSize, SInt(sampleBits.W)))
  val y2Block = Reg(Vec(HjpegConstants.BlockSize, SInt(sampleBits.W)))
  val y3Block = Reg(Vec(HjpegConstants.BlockSize, SInt(sampleBits.W)))
  val cbBlock = Reg(Vec(HjpegConstants.BlockSize, SInt(sampleBits.W)))
  val crBlock = Reg(Vec(HjpegConstants.BlockSize, SInt(sampleBits.W)))
  val y0Coefficients = Reg(new ZigZagCoefficientBlock(coefficientBits))
  val y1Coefficients = Reg(new ZigZagCoefficientBlock(coefficientBits))
  val y2Coefficients = Reg(new ZigZagCoefficientBlock(coefficientBits))
  val y3Coefficients = Reg(new ZigZagCoefficientBlock(coefficientBits))
  val cbCoefficients = Reg(new ZigZagCoefficientBlock(coefficientBits))
  val crCoefficients = Reg(new ZigZagCoefficientBlock(coefficientBits))

  val rowInBand = io.input.bits.y(3, 0)
  val writeBank = Cat(rowInBand(0), io.input.bits.x(ColumnBankBits - 1, 0))
  val writeBankRow = rowInBand >> 1
  val writeBankColumn = io.input.bits.x >> ColumnBankBits
  val writeAddress = (writeBankRow * BankColumns.U + writeBankColumn)(BankAddressBits - 1, 0)
  val lastPixelInBand =
    io.input.bits.x === io.config.xsize - 1.U &&
      (rowInBand === (McuDim - 1).U || io.input.bits.y === io.config.ysize - 1.U)

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
    when(lastPixelInBand) {
      state := sLoad
      blockX := 0.U
      currentBandLast := io.input.bits.y + 1.U >= io.config.ysize
      lastRowInBand := rowInBand
      loadPhase := 0.U
      loadSample := 0.U
      loadAllIssued := false.B
      issueBlock := 0.U
      captureBlock := 0.U
    }
  }

  val loadGroupRow = loadSample(3, 1)
  val loadGroupCol = Mux(loadSample(0), ReadLanes.U, 0.U)

  val yBaseRow = Mux(loadPhase(1), HjpegConstants.BlockDim.U, 0.U(4.W))
  val yBaseCol = Mux(loadPhase(0), HjpegConstants.BlockDim.U, 0.U(c.coordBits.W))
  val yRequestedRow = yBaseRow + loadGroupRow
  val yReadRow = Mux(yRequestedRow > lastRowInBand, lastRowInBand, yRequestedRow(3, 0))
  val chromaBaseRow = loadSample(5, 3) << 1
  val chromaBaseCol = blockX + (loadSample(2, 0) << 1)

  val yLaneReadBanks = Wire(Vec(ReadLanes, UInt(BankIndexBits.W)))
  val yLaneReadAddresses = Wire(Vec(ReadLanes, UInt(BankAddressBits.W)))
  val chromaLaneReadBanks = Wire(Vec(ReadLanes, UInt(BankIndexBits.W)))
  val chromaLaneReadAddresses = Wire(Vec(ReadLanes, UInt(BankAddressBits.W)))
  for (lane <- 0 until ReadLanes) {
    val yRequestedCol = blockX + yBaseCol + loadGroupCol + lane.U
    val yReadCol = Mux(yRequestedCol >= io.config.xsize, io.config.xsize - 1.U, yRequestedCol)
    yLaneReadBanks(lane) := Cat(yReadRow(0), yReadCol(ColumnBankBits - 1, 0))
    yLaneReadAddresses(lane) :=
      ((yReadRow >> 1) * BankColumns.U + (yReadCol >> ColumnBankBits))(BankAddressBits - 1, 0)

    val chromaRequestedRow = chromaBaseRow + (lane / 2).U
    val chromaRequestedCol = chromaBaseCol + (lane % 2).U
    val chromaReadRow =
      Mux(chromaRequestedRow > lastRowInBand, lastRowInBand, chromaRequestedRow(3, 0))
    val chromaReadCol =
      Mux(chromaRequestedCol >= io.config.xsize, io.config.xsize - 1.U, chromaRequestedCol)
    chromaLaneReadBanks(lane) := Cat(chromaReadRow(0), chromaReadCol(ColumnBankBits - 1, 0))
    chromaLaneReadAddresses(lane) :=
      ((chromaReadRow >> 1) * BankColumns.U + (chromaReadCol >> ColumnBankBits))(BankAddressBits - 1, 0)
  }

  val loadReadEnable = state === sLoad && !loadAllIssued
  val yBankReadData = Wire(Vec(BankCount, SInt(sampleBits.W)))
  val cbBankReadData = Wire(Vec(BankCount, SInt(sampleBits.W)))
  val crBankReadData = Wire(Vec(BankCount, SInt(sampleBits.W)))
  for (bank <- 0 until BankCount) {
    val yLaneMatches = VecInit((0 until ReadLanes).map(lane => yLaneReadBanks(lane) === bank.U))
    val yBankReadEnable = loadReadEnable && loadPhase < 4.U && yLaneMatches.asUInt.orR
    val yBankReadAddress = PriorityMux(
      (0 until ReadLanes).map(lane => yLaneMatches(lane) -> yLaneReadAddresses(lane)))
    yBankReadData(bank) := ySampleBanks(bank).read(yBankReadAddress, yBankReadEnable)

    val chromaLaneMatches =
      VecInit((0 until ReadLanes).map(lane => chromaLaneReadBanks(lane) === bank.U))
    val chromaBankReadEnable = loadReadEnable && loadPhase === 4.U && chromaLaneMatches.asUInt.orR
    val chromaBankReadAddress = PriorityMux(
      (0 until ReadLanes).map(lane => chromaLaneMatches(lane) -> chromaLaneReadAddresses(lane)))
    cbBankReadData(bank) := cbSampleBanks(bank).read(chromaBankReadAddress, chromaBankReadEnable)
    crBankReadData(bank) := crSampleBanks(bank).read(chromaBankReadAddress, chromaBankReadEnable)
  }
  val loadReadValid = RegNext(loadReadEnable, false.B)

  when(loadReadEnable) {
    loadReadPhase := loadPhase
    loadReadSample := loadSample
    loadReadYBanks := yLaneReadBanks
    loadReadChromaBanks := chromaLaneReadBanks
    when(loadPhase < 4.U) {
      when(loadSample === ((HjpegConstants.BlockSize / ReadLanes) - 1).U) {
        loadSample := 0.U
        loadPhase := loadPhase + 1.U
      }.otherwise {
        loadSample := loadSample + 1.U
      }
    }.otherwise {
      when(loadSample === (HjpegConstants.BlockSize - 1).U) {
        loadAllIssued := true.B
      }.otherwise {
        loadSample := loadSample + 1.U
      }
    }
  }

  when(state === sLoad && loadReadValid) {
    when(loadReadPhase < 4.U) {
      for (lane <- 0 until ReadLanes) {
        val blockIndex = Cat(loadReadSample(3, 0), lane.U(ColumnBankBits.W))
        val yLoadSample = yBankReadData(loadReadYBanks(lane))
        switch(loadReadPhase) {
          is(0.U) { y0Block(blockIndex) := yLoadSample }
          is(1.U) { y1Block(blockIndex) := yLoadSample }
          is(2.U) { y2Block(blockIndex) := yLoadSample }
          is(3.U) { y3Block(blockIndex) := yLoadSample }
        }
      }
    }.otherwise {
      val cbLoadSamples = Wire(Vec(ReadLanes, SInt(sampleBits.W)))
      val crLoadSamples = Wire(Vec(ReadLanes, SInt(sampleBits.W)))
      for (lane <- 0 until ReadLanes) {
        cbLoadSamples(lane) := cbBankReadData(loadReadChromaBanks(lane))
        crLoadSamples(lane) := crBankReadData(loadReadChromaBanks(lane))
      }
      val cbSum = (cbLoadSamples(0) +& cbLoadSamples(1)) +& (cbLoadSamples(2) +& cbLoadSamples(3))
      val crSum = (crLoadSamples(0) +& crLoadSamples(1)) +& (crLoadSamples(2) +& crLoadSamples(3))
      cbBlock(loadReadSample) := (cbSum >> 2).asSInt
      crBlock(loadReadSample) := (crSum >> 2).asSInt
      when(loadReadSample === (HjpegConstants.BlockSize - 1).U) {
        state := sTransform
        issueBlock := 0.U
        captureBlock := 0.U
      }
    }
  }

  val transform = Module(new JpegBlockTransformStage(sampleBits, coefficientBits))
  transform.io.quality := io.config.quality
  transform.io.isLuminance := issueBlock < 4.U
  transform.io.input.valid := state === sTransform && issueBlock < 6.U

  for (row <- 0 until HjpegConstants.BlockDim) {
    for (col <- 0 until HjpegConstants.BlockDim) {
      val sample = row * HjpegConstants.BlockDim + col
      val y01Sample = Mux(issueBlock === 0.U, y0Block(sample), y1Block(sample))
      val y23Sample = Mux(issueBlock === 2.U, y2Block(sample), y3Block(sample))
      val ySample = Mux(issueBlock < 2.U, y01Sample, y23Sample)
      val chromaSample = Mux(issueBlock === 4.U, cbBlock(sample), crBlock(sample))
      transform.io.input.bits.samples(sample) := Mux(issueBlock < 4.U, ySample, chromaSample)
    }
  }

  transform.io.output.ready := state === sTransform

  when(transform.io.input.fire) {
    issueBlock := issueBlock + 1.U
  }

  when(transform.io.output.fire) {
    switch(captureBlock) {
      is(0.U) {
        y0Coefficients := transform.io.output.bits
        captureBlock := 1.U
      }
      is(1.U) {
        y1Coefficients := transform.io.output.bits
        captureBlock := 2.U
      }
      is(2.U) {
        y2Coefficients := transform.io.output.bits
        captureBlock := 3.U
      }
      is(3.U) {
        y3Coefficients := transform.io.output.bits
        captureBlock := 4.U
      }
      is(4.U) {
        cbCoefficients := transform.io.output.bits
        captureBlock := 5.U
      }
      is(5.U) {
        crCoefficients := transform.io.output.bits
        state := sEmit
      }
    }
  }

  val lastBlockInBand = blockX + McuDim.U >= io.config.xsize

  io.output.valid := state === sEmit
  io.output.bits.mcu.yBlockCount := 4.U
  io.output.bits.mcu.y := y0Coefficients
  io.output.bits.mcu.y1 := y1Coefficients
  io.output.bits.mcu.y2 := y2Coefficients
  io.output.bits.mcu.y3 := y3Coefficients
  io.output.bits.mcu.cb := cbCoefficients
  io.output.bits.mcu.cr := crCoefficients
  io.output.bits.last := lastBlockInBand && currentBandLast

  when(io.output.fire) {
    when(lastBlockInBand) {
      state := sCollect
      blockX := 0.U
    }.otherwise {
      blockX := blockX + McuDim.U
      loadPhase := 0.U
      loadSample := 0.U
      loadAllIssued := false.B
      issueBlock := 0.U
      captureBlock := 0.U
      state := sLoad
    }
  }
}
