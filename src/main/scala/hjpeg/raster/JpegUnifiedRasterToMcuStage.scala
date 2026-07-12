// See README.md for license details.

package hjpeg

import chisel3._
import chisel3.util._

/** Shared two-band raster store and MCU generator for 4:4:4 and 4:2:0.
  *
  * Both modes use the same eight banked 16-row storage. A 4:2:0 band emits one
  * row of 16x16 MCUs; a 4:4:4 band emits its top and bottom 8-row MCU stripes
  * in raster order. Sharing storage and the block transform avoids duplicating
  * the dominant BRAM/DSP structures when chroma mode is runtime-selectable.
  */
class JpegUnifiedRasterToMcuStage(
    c: HjpegConfig = HjpegConfig(),
    sampleBits: Int = 9,
    coefficientBits: Int = 16)
    extends Module {
  val io = IO(new Bundle {
    val config = Input(new FrameConfig(c))
    val input = Flipped(Decoupled(new RgbPixel(c)))
    val output = Decoupled(new ZigZagMinimumCodedUnitPacket(coefficientBits))
  })

  private val BandRows = HjpegConstants.BlockDim * 2
  private val ReadLanes = 4
  private val ColumnBankCount = ReadLanes
  private val ColumnBankBits = log2Ceil(ColumnBankCount)
  private val BankCount = ColumnBankCount * 2
  private val BankIndexBits = log2Ceil(BankCount)
  private val BankColumns = (c.maxFrameWidth + ColumnBankCount - 1) / ColumnBankCount
  private val BankRows = BandRows / 2
  private val BandBankSamples = BankRows * BankColumns
  private val BufferCount = 2
  private val BankSamples = BufferCount * BandBankSamples
  private val BankAddressBits = log2Ceil(BankSamples).max(1)

  val sIdle :: sLoad :: sTransform :: sEmit :: Nil = Enum(4)
  val state = RegInit(sIdle)
  val writeBuffer = RegInit(0.U(1.W))
  val nextReadBuffer = RegInit(0.U(1.W))
  val activeReadBuffer = RegInit(0.U(1.W))
  val bufferReady = RegInit(VecInit(Seq.fill(BufferCount)(false.B)))
  val bufferLast = Reg(Vec(BufferCount, Bool()))
  val bufferLastRow = Reg(Vec(BufferCount, UInt(4.W)))
  val blockX = RegInit(0.U(c.coordBits.W))
  val stripeHalf = RegInit(false.B)
  val currentBandLast = RegInit(false.B)
  val lastRowInBand = RegInit(0.U(4.W))
  val loadPhase = RegInit(0.U(3.W))
  val loadSample = RegInit(0.U(6.W))
  val loadReadPhase = Reg(UInt(3.W))
  val loadReadSample = Reg(UInt(6.W))
  val loadReadBanks = Reg(Vec(ReadLanes, UInt(BankIndexBits.W)))
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

  val subsampled = io.config.enableChromaSubsample
  val rowInBand = io.input.bits.y(3, 0)
  val writeBank = Cat(rowInBand(0), io.input.bits.x(ColumnBankBits - 1, 0))
  val writeBankRow = rowInBand >> 1
  val writeBankColumn = io.input.bits.x >> ColumnBankBits
  val writeLocalAddress = writeBankRow * BankColumns.U + writeBankColumn
  val writeAddress =
    (writeBuffer * BandBankSamples.U + writeLocalAddress)(BankAddressBits - 1, 0)
  val lastPixelInBand =
    io.input.bits.x === io.config.xsize - 1.U &&
      (rowInBand === (BandRows - 1).U || io.input.bits.y === io.config.ysize - 1.U)

  val (yComponent, cbComponent, crComponent) =
    JpegColorConversion.rgbToYCbCr(io.input.bits.r, io.input.bits.g, io.input.bits.b, c.pixelBits)

  io.input.ready := !bufferReady(writeBuffer)

  when(io.input.fire) {
    for (bank <- 0 until BankCount) {
      when(writeBank === bank.U) {
        ySampleBanks(bank).write(writeAddress, (yComponent.zext - 128.S)(sampleBits - 1, 0).asSInt)
        cbSampleBanks(bank).write(writeAddress, (cbComponent.zext - 128.S)(sampleBits - 1, 0).asSInt)
        crSampleBanks(bank).write(writeAddress, (crComponent.zext - 128.S)(sampleBits - 1, 0).asSInt)
      }
    }
    when(lastPixelInBand) {
      bufferReady(writeBuffer) := true.B
      bufferLast(writeBuffer) := io.input.bits.y + 1.U >= io.config.ysize
      bufferLastRow(writeBuffer) := rowInBand
      writeBuffer := ~writeBuffer
    }
  }

  when(state === sIdle && bufferReady(nextReadBuffer)) {
    activeReadBuffer := nextReadBuffer
    blockX := 0.U
    stripeHalf := false.B
    currentBandLast := bufferLast(nextReadBuffer)
    lastRowInBand := bufferLastRow(nextReadBuffer)
    loadPhase := 0.U
    loadSample := 0.U
    loadAllIssued := false.B
    issueBlock := 0.U
    captureBlock := 0.U
    state := sLoad
  }

  val loadGroupRow = loadSample(3, 1)
  val loadGroupCol = Mux(loadSample(0), ReadLanes.U, 0.U)

  // 4:2:0 luminance reads one four-sample group at a time from four blocks.
  val yBaseRow420 = Mux(loadPhase(1), HjpegConstants.BlockDim.U, 0.U(4.W))
  val yBaseCol420 = Mux(loadPhase(0), HjpegConstants.BlockDim.U, 0.U(c.coordBits.W))
  val yRequestedRow420 = yBaseRow420 + loadGroupRow
  val yReadRow420 = Mux(yRequestedRow420 > lastRowInBand, lastRowInBand, yRequestedRow420(3, 0))

  // 4:2:0 chroma reads one complete 2x2 source footprint per cycle.
  val chromaBaseRow420 = loadSample(5, 3) << 1
  val chromaBaseCol420 = blockX + (loadSample(2, 0) << 1)

  // 4:4:4 reads top or bottom 8-row stripe in two four-sample groups per row.
  val fullBaseRow444 = Mux(stripeHalf, HjpegConstants.BlockDim.U, 0.U(4.W))
  val fullRequestedRow444 = fullBaseRow444 + loadGroupRow
  val fullReadRow444 = Mux(fullRequestedRow444 > lastRowInBand, lastRowInBand, fullRequestedRow444(3, 0))

  val yLaneReadBanks420 = Wire(Vec(ReadLanes, UInt(BankIndexBits.W)))
  val yLaneReadAddresses420 = Wire(Vec(ReadLanes, UInt(BankAddressBits.W)))
  val chromaLaneReadBanks420 = Wire(Vec(ReadLanes, UInt(BankIndexBits.W)))
  val chromaLaneReadAddresses420 = Wire(Vec(ReadLanes, UInt(BankAddressBits.W)))
  val fullLaneReadBanks444 = Wire(Vec(ReadLanes, UInt(BankIndexBits.W)))
  val fullLaneReadAddresses444 = Wire(Vec(ReadLanes, UInt(BankAddressBits.W)))
  val selectedLaneReadBanks = Wire(Vec(ReadLanes, UInt(BankIndexBits.W)))
  val selectedLaneReadAddresses = Wire(Vec(ReadLanes, UInt(BankAddressBits.W)))

  for (lane <- 0 until ReadLanes) {
    val yRequestedCol420 = blockX + yBaseCol420 + loadGroupCol + lane.U
    val yReadCol420 = Mux(yRequestedCol420 >= io.config.xsize, io.config.xsize - 1.U, yRequestedCol420)
    yLaneReadBanks420(lane) := Cat(yReadRow420(0), yReadCol420(ColumnBankBits - 1, 0))
    val yReadLocalAddress420 = (yReadRow420 >> 1) * BankColumns.U + (yReadCol420 >> ColumnBankBits)
    yLaneReadAddresses420(lane) :=
      (activeReadBuffer * BandBankSamples.U + yReadLocalAddress420)(BankAddressBits - 1, 0)

    val chromaRequestedRow420 = chromaBaseRow420 + (lane / 2).U
    val chromaRequestedCol420 = chromaBaseCol420 + (lane % 2).U
    val chromaReadRow420 =
      Mux(chromaRequestedRow420 > lastRowInBand, lastRowInBand, chromaRequestedRow420(3, 0))
    val chromaReadCol420 =
      Mux(chromaRequestedCol420 >= io.config.xsize, io.config.xsize - 1.U, chromaRequestedCol420)
    chromaLaneReadBanks420(lane) := Cat(chromaReadRow420(0), chromaReadCol420(ColumnBankBits - 1, 0))
    val chromaReadLocalAddress420 =
      (chromaReadRow420 >> 1) * BankColumns.U + (chromaReadCol420 >> ColumnBankBits)
    chromaLaneReadAddresses420(lane) :=
      (activeReadBuffer * BandBankSamples.U + chromaReadLocalAddress420)(BankAddressBits - 1, 0)

    val fullRequestedCol444 = blockX + loadGroupCol + lane.U
    val fullReadCol444 =
      Mux(fullRequestedCol444 >= io.config.xsize, io.config.xsize - 1.U, fullRequestedCol444)
    fullLaneReadBanks444(lane) := Cat(fullReadRow444(0), fullReadCol444(ColumnBankBits - 1, 0))
    val fullReadLocalAddress444 =
      (fullReadRow444 >> 1) * BankColumns.U + (fullReadCol444 >> ColumnBankBits)
    fullLaneReadAddresses444(lane) :=
      (activeReadBuffer * BandBankSamples.U + fullReadLocalAddress444)(BankAddressBits - 1, 0)

    val selected420Bank = Mux(loadPhase === 4.U, chromaLaneReadBanks420(lane), yLaneReadBanks420(lane))
    val selected420Address =
      Mux(loadPhase === 4.U, chromaLaneReadAddresses420(lane), yLaneReadAddresses420(lane))
    selectedLaneReadBanks(lane) := Mux(subsampled, selected420Bank, fullLaneReadBanks444(lane))
    selectedLaneReadAddresses(lane) := Mux(subsampled, selected420Address, fullLaneReadAddresses444(lane))
  }

  val loadReadEnable = state === sLoad && !loadAllIssued
  val yBankReadData = Wire(Vec(BankCount, SInt(sampleBits.W)))
  val cbBankReadData = Wire(Vec(BankCount, SInt(sampleBits.W)))
  val crBankReadData = Wire(Vec(BankCount, SInt(sampleBits.W)))
  for (bank <- 0 until BankCount) {
    val laneMatches = VecInit((0 until ReadLanes).map(lane => selectedLaneReadBanks(lane) === bank.U))
    val bankReadEnable = loadReadEnable && laneMatches.asUInt.orR
    val bankReadAddress = PriorityMux(
      (0 until ReadLanes).map(lane => laneMatches(lane) -> selectedLaneReadAddresses(lane)))
    yBankReadData(bank) := ySampleBanks(bank).read(bankReadAddress, bankReadEnable)
    cbBankReadData(bank) := cbSampleBanks(bank).read(bankReadAddress, bankReadEnable)
    crBankReadData(bank) := crSampleBanks(bank).read(bankReadAddress, bankReadEnable)
  }
  val loadReadValid = RegNext(loadReadEnable, false.B)

  when(loadReadEnable) {
    loadReadPhase := loadPhase
    loadReadSample := loadSample
    loadReadBanks := selectedLaneReadBanks
    when(subsampled) {
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
    }.otherwise {
      when(loadSample === ((HjpegConstants.BlockSize / ReadLanes) - 1).U) {
        loadSample := 0.U
        when(loadPhase === 2.U) {
          loadAllIssued := true.B
        }.otherwise {
          loadPhase := loadPhase + 1.U
        }
      }.otherwise {
        loadSample := loadSample + 1.U
      }
    }
  }

  when(state === sLoad && loadReadValid) {
    when(subsampled) {
      when(loadReadPhase < 4.U) {
        for (lane <- 0 until ReadLanes) {
          val blockIndex = Cat(loadReadSample(3, 0), lane.U(ColumnBankBits.W))
          val yLoadSample = yBankReadData(loadReadBanks(lane))
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
          cbLoadSamples(lane) := cbBankReadData(loadReadBanks(lane))
          crLoadSamples(lane) := crBankReadData(loadReadBanks(lane))
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
    }.otherwise {
      for (lane <- 0 until ReadLanes) {
        val blockIndex = Cat(loadReadSample(3, 1), loadReadSample(0), lane.U(ColumnBankBits.W))
        switch(loadReadPhase) {
          is(0.U) { y0Block(blockIndex) := yBankReadData(loadReadBanks(lane)) }
          is(1.U) { cbBlock(blockIndex) := cbBankReadData(loadReadBanks(lane)) }
          is(2.U) { crBlock(blockIndex) := crBankReadData(loadReadBanks(lane)) }
        }
      }
      when(loadReadPhase === 2.U && loadReadSample === ((HjpegConstants.BlockSize / ReadLanes) - 1).U) {
        state := sTransform
        issueBlock := 0.U
        captureBlock := 0.U
      }
    }
  }

  val transform = Module(new JpegBlockTransformStage(sampleBits, coefficientBits))
  val blocksPerMcu = Mux(subsampled, 6.U, 3.U)
  transform.io.quality := io.config.quality
  transform.io.isLuminance := Mux(subsampled, issueBlock < 4.U, issueBlock === 0.U)
  transform.io.input.valid := state === sTransform && issueBlock < blocksPerMcu

  for (sample <- 0 until HjpegConstants.BlockSize) {
    val y01Sample = Mux(issueBlock === 0.U, y0Block(sample), y1Block(sample))
    val y23Sample = Mux(issueBlock === 2.U, y2Block(sample), y3Block(sample))
    val ySample420 = Mux(issueBlock < 2.U, y01Sample, y23Sample)
    val chromaSample420 = Mux(issueBlock === 4.U, cbBlock(sample), crBlock(sample))
    val sample420 = Mux(issueBlock < 4.U, ySample420, chromaSample420)
    val sample444 = Mux(issueBlock === 0.U, y0Block(sample), Mux(issueBlock === 1.U, cbBlock(sample), crBlock(sample)))
    transform.io.input.bits.samples(sample) := Mux(subsampled, sample420, sample444)
  }

  transform.io.output.ready := state === sTransform

  when(transform.io.input.fire) {
    issueBlock := issueBlock + 1.U
  }

  when(transform.io.output.fire) {
    when(subsampled) {
      switch(captureBlock) {
        is(0.U) { y0Coefficients := transform.io.output.bits; captureBlock := 1.U }
        is(1.U) { y1Coefficients := transform.io.output.bits; captureBlock := 2.U }
        is(2.U) { y2Coefficients := transform.io.output.bits; captureBlock := 3.U }
        is(3.U) { y3Coefficients := transform.io.output.bits; captureBlock := 4.U }
        is(4.U) { cbCoefficients := transform.io.output.bits; captureBlock := 5.U }
        is(5.U) { crCoefficients := transform.io.output.bits; state := sEmit }
      }
    }.otherwise {
      switch(captureBlock) {
        is(0.U) { y0Coefficients := transform.io.output.bits; captureBlock := 1.U }
        is(1.U) { cbCoefficients := transform.io.output.bits; captureBlock := 2.U }
        is(2.U) { crCoefficients := transform.io.output.bits; state := sEmit }
      }
    }
  }

  val mcuWidth = Mux(subsampled, BandRows.U, HjpegConstants.BlockDim.U)
  val lastMcuInRow = blockX + mcuWidth >= io.config.xsize
  val hasBottomStripe = lastRowInBand >= HjpegConstants.BlockDim.U
  val finalStripeInBand = subsampled || stripeHalf || !hasBottomStripe

  io.output.valid := state === sEmit
  io.output.bits.mcu.yBlockCount := Mux(subsampled, 4.U, 1.U)
  io.output.bits.mcu.y := y0Coefficients
  io.output.bits.mcu.y1 := Mux(subsampled, y1Coefficients, y0Coefficients)
  io.output.bits.mcu.y2 := Mux(subsampled, y2Coefficients, y0Coefficients)
  io.output.bits.mcu.y3 := Mux(subsampled, y3Coefficients, y0Coefficients)
  io.output.bits.mcu.cb := cbCoefficients
  io.output.bits.mcu.cr := crCoefficients
  io.output.bits.last := lastMcuInRow && finalStripeInBand && currentBandLast

  when(io.output.fire) {
    when(lastMcuInRow) {
      when(!subsampled && !stripeHalf && hasBottomStripe) {
        blockX := 0.U
        stripeHalf := true.B
        loadPhase := 0.U
        loadSample := 0.U
        loadAllIssued := false.B
        issueBlock := 0.U
        captureBlock := 0.U
        state := sLoad
      }.otherwise {
        bufferReady(activeReadBuffer) := false.B
        nextReadBuffer := ~nextReadBuffer
        state := sIdle
        blockX := 0.U
        stripeHalf := false.B
      }
    }.otherwise {
      blockX := blockX + mcuWidth
      loadPhase := 0.U
      loadSample := 0.U
      loadAllIssued := false.B
      issueBlock := 0.U
      captureBlock := 0.U
      state := sLoad
    }
  }
}
