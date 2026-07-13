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
    coefficientBits: Int = 16,
    inputLanes: Int = 1)
    extends Module {
  require(inputLanes > 0 && inputLanes <= 4, "unified raster input supports one to four lanes")

  val io = IO(new Bundle {
    val config = Input(new FrameConfig(c))
    val input = Flipped(Decoupled(new RgbPixelGroup(c, inputLanes)))
    val output = Decoupled(new ZigZagMinimumCodedUnitPacket(coefficientBits))
  })

  private val BandRows = HjpegConstants.BlockDim * 2
  private val ReadLanes = 8
  private val ColumnBankCount = 4
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

  val ySampleBanks = Seq.fill(BankCount)(SyncReadMem(BankSamples, SInt(sampleBits.W)))
  val cbSampleBanks = Seq.fill(BankCount)(SyncReadMem(BankSamples, SInt(sampleBits.W)))
  val crSampleBanks = Seq.fill(BankCount)(SyncReadMem(BankSamples, SInt(sampleBits.W)))
  val y0Block = Reg(Vec(HjpegConstants.BlockSize, SInt(sampleBits.W)))
  val y1Block = Reg(Vec(HjpegConstants.BlockSize, SInt(sampleBits.W)))
  val y2Block = Reg(Vec(HjpegConstants.BlockSize, SInt(sampleBits.W)))
  val y3Block = Reg(Vec(HjpegConstants.BlockSize, SInt(sampleBits.W)))
  val cbBlock = Reg(Vec(HjpegConstants.BlockSize, SInt(sampleBits.W)))
  val crBlock = Reg(Vec(HjpegConstants.BlockSize, SInt(sampleBits.W)))

  val subsampled = io.config.enableChromaSubsample
  val laneWriteBanks = Wire(Vec(inputLanes, UInt(BankIndexBits.W)))
  val laneWriteAddresses = Wire(Vec(inputLanes, UInt(BankAddressBits.W)))
  val laneY = Wire(Vec(inputLanes, SInt(sampleBits.W)))
  val laneCb = Wire(Vec(inputLanes, SInt(sampleBits.W)))
  val laneCr = Wire(Vec(inputLanes, SInt(sampleBits.W)))
  val laneLastInBand = Wire(Vec(inputLanes, Bool()))

  for (lane <- 0 until inputLanes) {
    val pixel = io.input.bits.pixels(lane)
    val rowInBand = pixel.y(3, 0)
    val writeBankRow = rowInBand >> 1
    val writeBankColumn = pixel.x >> ColumnBankBits
    val writeLocalAddress = writeBankRow * BankColumns.U + writeBankColumn
    val (yComponent, cbComponent, crComponent) =
      JpegColorConversion.rgbToYCbCr(pixel.r, pixel.g, pixel.b, c.pixelBits)

    laneWriteBanks(lane) := Cat(rowInBand(0), pixel.x(ColumnBankBits - 1, 0))
    laneWriteAddresses(lane) :=
      (writeBuffer * BandBankSamples.U + writeLocalAddress)(BankAddressBits - 1, 0)
    laneY(lane) := (yComponent.zext - 128.S)(sampleBits - 1, 0).asSInt
    laneCb(lane) := (cbComponent.zext - 128.S)(sampleBits - 1, 0).asSInt
    laneCr(lane) := (crComponent.zext - 128.S)(sampleBits - 1, 0).asSInt
    laneLastInBand(lane) :=
      pixel.x === io.config.xsize - 1.U &&
        (rowInBand === (BandRows - 1).U || pixel.y === io.config.ysize - 1.U)
  }

  val lastPixelInBand = laneLastInBand.asUInt.orR
  val lastPixelY = PriorityMux(
    (0 until inputLanes).map(lane => laneLastInBand(lane) -> io.input.bits.pixels(lane).y))
  val lastPixelRowInBand = lastPixelY(3, 0)

  io.input.ready := !bufferReady(writeBuffer)

  when(io.input.fire) {
    for (bank <- 0 until BankCount) {
      val laneMatches = VecInit((0 until inputLanes).map(lane => laneWriteBanks(lane) === bank.U))
      assert(PopCount(laneMatches) <= 1.U, "RGB input lanes must map to distinct raster banks")
      when(laneMatches.asUInt.orR) {
        val writeAddress = PriorityMux(
          (0 until inputLanes).map(lane => laneMatches(lane) -> laneWriteAddresses(lane)))
        val writeY = PriorityMux((0 until inputLanes).map(lane => laneMatches(lane) -> laneY(lane)))
        val writeCb = PriorityMux((0 until inputLanes).map(lane => laneMatches(lane) -> laneCb(lane)))
        val writeCr = PriorityMux((0 until inputLanes).map(lane => laneMatches(lane) -> laneCr(lane)))
        ySampleBanks(bank).write(writeAddress, writeY)
        cbSampleBanks(bank).write(writeAddress, writeCb)
        crSampleBanks(bank).write(writeAddress, writeCr)
      }
    }
    when(lastPixelInBand) {
      bufferReady(writeBuffer) := true.B
      bufferLast(writeBuffer) := lastPixelY + 1.U >= io.config.ysize
      bufferLastRow(writeBuffer) := lastPixelRowInBand
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
    state := sLoad
  }

  val loadGroupRow = loadSample(2, 1) << 1
  val loadGroupCol = Mux(loadSample(0), ColumnBankCount.U, 0.U)

  // Each read uses both row parities and all four column banks: lanes 0..3
  // fetch one row and lanes 4..7 fetch the following row.
  val yBaseRow420 = Mux(loadPhase(1), HjpegConstants.BlockDim.U, 0.U(4.W))
  val yBaseCol420 = Mux(loadPhase(0), HjpegConstants.BlockDim.U, 0.U(c.coordBits.W))

  // 4:2:0 chroma reads two horizontally adjacent 2x2 source footprints per
  // cycle. An even output index ensures the pair never crosses an 8-pixel row.
  val chromaOutputBase420 = loadSample << 1
  val chromaBaseRow420 = chromaOutputBase420(5, 3) << 1
  val chromaBaseCol420 = blockX + (chromaOutputBase420(2, 0) << 1)

  // 4:4:4 reads top or bottom 8-row stripe in four columns from two rows.
  val fullBaseRow444 = Mux(stripeHalf, HjpegConstants.BlockDim.U, 0.U(4.W))

  val yLaneReadBanks420 = Wire(Vec(ReadLanes, UInt(BankIndexBits.W)))
  val yLaneReadAddresses420 = Wire(Vec(ReadLanes, UInt(BankAddressBits.W)))
  val chromaLaneReadBanks420 = Wire(Vec(ReadLanes, UInt(BankIndexBits.W)))
  val chromaLaneReadAddresses420 = Wire(Vec(ReadLanes, UInt(BankAddressBits.W)))
  val fullLaneReadBanks444 = Wire(Vec(ReadLanes, UInt(BankIndexBits.W)))
  val fullLaneReadAddresses444 = Wire(Vec(ReadLanes, UInt(BankAddressBits.W)))
  val selectedLaneReadBanks = Wire(Vec(ReadLanes, UInt(BankIndexBits.W)))
  val selectedLaneReadAddresses = Wire(Vec(ReadLanes, UInt(BankAddressBits.W)))

  for (lane <- 0 until ReadLanes) {
    val laneRowOffset = (lane / ColumnBankCount).U
    val laneColumn = (lane % ColumnBankCount).U
    val yRequestedRow420 = yBaseRow420 + loadGroupRow + laneRowOffset
    val yReadRow420 =
      Mux(yRequestedRow420 > lastRowInBand, lastRowInBand, yRequestedRow420(3, 0))
    val yRequestedCol420 = blockX + yBaseCol420 + loadGroupCol + laneColumn
    val yReadCol420 = Mux(yRequestedCol420 >= io.config.xsize, io.config.xsize - 1.U, yRequestedCol420)
    yLaneReadBanks420(lane) := Cat(yReadRow420(0), yReadCol420(ColumnBankBits - 1, 0))
    val yReadLocalAddress420 = (yReadRow420 >> 1) * BankColumns.U + (yReadCol420 >> ColumnBankBits)
    yLaneReadAddresses420(lane) :=
      (activeReadBuffer * BandBankSamples.U + yReadLocalAddress420)(BankAddressBits - 1, 0)

    val chromaFootprint = lane / 4
    val chromaFootprintLane = lane % 4
    val chromaRequestedRow420 = chromaBaseRow420 + (chromaFootprintLane / 2).U
    val chromaRequestedCol420 =
      chromaBaseCol420 + (chromaFootprint * 2).U + (chromaFootprintLane % 2).U
    val chromaReadRow420 =
      Mux(chromaRequestedRow420 > lastRowInBand, lastRowInBand, chromaRequestedRow420(3, 0))
    val chromaReadCol420 =
      Mux(chromaRequestedCol420 >= io.config.xsize, io.config.xsize - 1.U, chromaRequestedCol420)
    chromaLaneReadBanks420(lane) := Cat(chromaReadRow420(0), chromaReadCol420(ColumnBankBits - 1, 0))
    val chromaReadLocalAddress420 =
      (chromaReadRow420 >> 1) * BankColumns.U + (chromaReadCol420 >> ColumnBankBits)
    chromaLaneReadAddresses420(lane) :=
      (activeReadBuffer * BandBankSamples.U + chromaReadLocalAddress420)(BankAddressBits - 1, 0)

    val fullRequestedRow444 = fullBaseRow444 + loadGroupRow + laneRowOffset
    val fullReadRow444 =
      Mux(fullRequestedRow444 > lastRowInBand, lastRowInBand, fullRequestedRow444(3, 0))
    val fullRequestedCol444 = blockX + loadGroupCol + laneColumn
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
    for (lane <- 0 until ReadLanes) {
      when(bankReadEnable && laneMatches(lane)) {
        assert(
          selectedLaneReadAddresses(lane) === bankReadAddress,
          "same-bank raster reads must use an identical replicated-edge address")
      }
    }
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
        when(loadSample === ((HjpegConstants.BlockSize / 2) - 1).U) {
          loadAllIssued := true.B
        }.otherwise {
          loadSample := loadSample + 1.U
        }
      }
    }.otherwise {
      when(loadSample === ((HjpegConstants.BlockSize / ReadLanes) - 1).U) {
        loadAllIssued := true.B
      }.otherwise {
        loadSample := loadSample + 1.U
      }
    }
  }

  when(state === sLoad && loadReadValid) {
    when(subsampled) {
      when(loadReadPhase < 4.U) {
        for (lane <- 0 until ReadLanes) {
          val blockIndex = Cat(
            loadReadSample(2, 1),
            (lane / ColumnBankCount).U(1.W),
            loadReadSample(0),
            (lane % ColumnBankCount).U(ColumnBankBits.W))
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
        for (output <- 0 until 2) {
          val base = output * 4
          val cbSum =
            (cbLoadSamples(base) +& cbLoadSamples(base + 1)) +&
              (cbLoadSamples(base + 2) +& cbLoadSamples(base + 3))
          val crSum =
            (crLoadSamples(base) +& crLoadSamples(base + 1)) +&
              (crLoadSamples(base + 2) +& crLoadSamples(base + 3))
          val outputIndex = Cat(loadReadSample(4, 0), output.U(1.W))
          cbBlock(outputIndex) := (cbSum >> 2).asSInt
          crBlock(outputIndex) := (crSum >> 2).asSInt
        }
        when(loadReadSample === ((HjpegConstants.BlockSize / 2) - 1).U) {
          state := sTransform
        }
      }
    }.otherwise {
      for (lane <- 0 until ReadLanes) {
        val blockIndex = Cat(
          loadReadSample(2, 1),
          (lane / ColumnBankCount).U(1.W),
          loadReadSample(0),
          (lane % ColumnBankCount).U(ColumnBankBits.W))
        y0Block(blockIndex) := yBankReadData(loadReadBanks(lane))
        cbBlock(blockIndex) := cbBankReadData(loadReadBanks(lane))
        crBlock(blockIndex) := crBankReadData(loadReadBanks(lane))
      }
      when(loadReadSample === ((HjpegConstants.BlockSize / ReadLanes) - 1).U) {
        state := sTransform
      }
    }
  }

  val mcuWidth = Mux(subsampled, BandRows.U, HjpegConstants.BlockDim.U)
  val lastMcuInRow = blockX + mcuWidth >= io.config.xsize
  val hasBottomStripe = lastRowInBand >= HjpegConstants.BlockDim.U
  val finalStripeInBand = subsampled || stripeHalf || !hasBottomStripe

  val mcuTransform = Module(new JpegParallelMcuTransformStage(sampleBits, coefficientBits))
  mcuTransform.io.input.valid := state === sTransform
  mcuTransform.io.input.bits.mcu.yBlockCount := Mux(subsampled, 4.U, 1.U)
  mcuTransform.io.input.bits.mcu.quality := io.config.quality
  mcuTransform.io.input.bits.mcu.y.samples := y0Block
  mcuTransform.io.input.bits.mcu.y1.samples := y1Block
  mcuTransform.io.input.bits.mcu.y2.samples := y2Block
  mcuTransform.io.input.bits.mcu.y3.samples := y3Block
  mcuTransform.io.input.bits.mcu.cb.samples := cbBlock
  mcuTransform.io.input.bits.mcu.cr.samples := crBlock
  mcuTransform.io.input.bits.last := lastMcuInRow && finalStripeInBand && currentBandLast
  io.output <> mcuTransform.io.output

  // The raster loader advances when raw samples have been copied into the
  // independent transform pipeline, not when transformed coefficients leave.
  when(mcuTransform.io.input.fire) {
    when(lastMcuInRow) {
      when(!subsampled && !stripeHalf && hasBottomStripe) {
        blockX := 0.U
        stripeHalf := true.B
        loadPhase := 0.U
        loadSample := 0.U
        loadAllIssued := false.B
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
      state := sLoad
    }
  }
}
