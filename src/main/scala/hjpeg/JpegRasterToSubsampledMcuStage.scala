// See README.md for license details.

package hjpeg

import chisel3._
import chisel3.util._

/** Buffers one 16-row raster band and emits 16x16, 4:2:0 MCUs.
  *
  * Each output MCU contains four luminance blocks followed by one downsampled
  * Cb block and one downsampled Cr block. Edge samples are padded by replicating
  * the last valid row or column.
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
  private val BandSamples = McuDim * c.maxFrameWidth
  private val sampleIndexBits = log2Ceil(BandSamples).max(1)

  val sCollect :: sLoad :: sEmit :: Nil = Enum(3)
  val state = RegInit(sCollect)
  val blockX = RegInit(0.U(c.coordBits.W))
  val currentBandLast = RegInit(false.B)
  val lastRowInBand = RegInit(0.U(4.W))
  val loadPhase = RegInit(0.U(3.W))
  val loadSample = RegInit(0.U(6.W))
  val chromaSubSample = RegInit(0.U(2.W))
  val cbAccumulator = RegInit(0.S((sampleBits + 2).W))
  val crAccumulator = RegInit(0.S((sampleBits + 2).W))

  val ySamples = Mem(BandSamples, SInt(sampleBits.W))
  val cbSamples = Mem(BandSamples, SInt(sampleBits.W))
  val crSamples = Mem(BandSamples, SInt(sampleBits.W))
  val y0Block = Reg(Vec(HjpegConstants.BlockSize, SInt(sampleBits.W)))
  val y1Block = Reg(Vec(HjpegConstants.BlockSize, SInt(sampleBits.W)))
  val y2Block = Reg(Vec(HjpegConstants.BlockSize, SInt(sampleBits.W)))
  val y3Block = Reg(Vec(HjpegConstants.BlockSize, SInt(sampleBits.W)))
  val cbBlock = Reg(Vec(HjpegConstants.BlockSize, SInt(sampleBits.W)))
  val crBlock = Reg(Vec(HjpegConstants.BlockSize, SInt(sampleBits.W)))

  val rowInBand = io.input.bits.y(3, 0)
  val writeIndex = (rowInBand * c.maxFrameWidth.U + io.input.bits.x)(sampleIndexBits - 1, 0)
  val lastPixelInBand =
    io.input.bits.x === io.config.xsize - 1.U &&
      (rowInBand === (McuDim - 1).U || io.input.bits.y === io.config.ysize - 1.U)

  val (yComponent, cbComponent, crComponent) =
    JpegColorConversion.rgbToYCbCr(io.input.bits.r, io.input.bits.g, io.input.bits.b, c.pixelBits)

  io.input.ready := state === sCollect

  when(io.input.fire) {
    ySamples(writeIndex) := (yComponent.zext - 128.S)(sampleBits - 1, 0).asSInt
    cbSamples(writeIndex) := (cbComponent.zext - 128.S)(sampleBits - 1, 0).asSInt
    crSamples(writeIndex) := (crComponent.zext - 128.S)(sampleBits - 1, 0).asSInt
    when(lastPixelInBand) {
      state := sLoad
      blockX := 0.U
      currentBandLast := io.input.bits.y + 1.U >= io.config.ysize
      lastRowInBand := rowInBand
      loadPhase := 0.U
      loadSample := 0.U
      chromaSubSample := 0.U
      cbAccumulator := 0.S
      crAccumulator := 0.S
    }
  }

  val loadRow = loadSample(5, 3)
  val loadCol = loadSample(2, 0)

  val yBaseRow = Mux(loadPhase(1), HjpegConstants.BlockDim.U, 0.U(4.W))
  val yBaseCol = Mux(loadPhase(0), HjpegConstants.BlockDim.U, 0.U(c.coordBits.W))
  val yReadRow = yBaseRow + loadRow
  val yReadCol = blockX + yBaseCol + loadCol

  val chromaBaseRow = loadRow << 1
  val chromaBaseCol = blockX + (loadCol << 1)
  val chromaReadRow = chromaBaseRow + chromaSubSample(1)
  val chromaReadCol = chromaBaseCol + chromaSubSample(0)

  val yReadIndex = clampedIndex(yReadRow, yReadCol)
  val chromaReadIndex = clampedIndex(chromaReadRow, chromaReadCol)
  val yLoadSample = ySamples(yReadIndex)
  val cbLoadSample = cbSamples(chromaReadIndex)
  val crLoadSample = crSamples(chromaReadIndex)

  when(state === sLoad) {
    when(loadPhase < 4.U) {
      switch(loadPhase) {
        is(0.U) { y0Block(loadSample) := yLoadSample }
        is(1.U) { y1Block(loadSample) := yLoadSample }
        is(2.U) { y2Block(loadSample) := yLoadSample }
        is(3.U) { y3Block(loadSample) := yLoadSample }
      }
      when(loadSample === (HjpegConstants.BlockSize - 1).U) {
        loadSample := 0.U
        loadPhase := loadPhase + 1.U
      }.otherwise {
        loadSample := loadSample + 1.U
      }
    }.otherwise {
      val cbNextSum = Mux(chromaSubSample === 0.U, 0.S, cbAccumulator) + cbLoadSample
      val crNextSum = Mux(chromaSubSample === 0.U, 0.S, crAccumulator) + crLoadSample
      cbAccumulator := cbNextSum
      crAccumulator := crNextSum
      when(chromaSubSample === 3.U) {
        cbBlock(loadSample) := (cbNextSum >> 2).asSInt
        crBlock(loadSample) := (crNextSum >> 2).asSInt
        chromaSubSample := 0.U
        cbAccumulator := 0.S
        crAccumulator := 0.S
        when(loadSample === (HjpegConstants.BlockSize - 1).U) {
          state := sEmit
        }.otherwise {
          loadSample := loadSample + 1.U
        }
      }.otherwise {
        chromaSubSample := chromaSubSample + 1.U
      }
    }
  }

  val y0Transform = Module(new JpegBlockTransformStage(sampleBits, coefficientBits))
  val y1Transform = Module(new JpegBlockTransformStage(sampleBits, coefficientBits))
  val y2Transform = Module(new JpegBlockTransformStage(sampleBits, coefficientBits))
  val y3Transform = Module(new JpegBlockTransformStage(sampleBits, coefficientBits))
  val cbTransform = Module(new JpegBlockTransformStage(sampleBits, coefficientBits))
  val crTransform = Module(new JpegBlockTransformStage(sampleBits, coefficientBits))

  val transforms = Seq(y0Transform, y1Transform, y2Transform, y3Transform, cbTransform, crTransform)
  for (transform <- transforms) {
    transform.io.quality := io.config.quality
    transform.io.input.valid := state === sEmit
  }
  y0Transform.io.isLuminance := true.B
  y1Transform.io.isLuminance := true.B
  y2Transform.io.isLuminance := true.B
  y3Transform.io.isLuminance := true.B
  cbTransform.io.isLuminance := false.B
  crTransform.io.isLuminance := false.B

  private def clampedIndex(row: UInt, col: UInt): UInt = {
    val readRow = Mux(row > lastRowInBand, lastRowInBand, row(3, 0))
    val readCol = Mux(col >= io.config.xsize, io.config.xsize - 1.U, col)
    (readRow * c.maxFrameWidth.U + readCol)(sampleIndexBits - 1, 0)
  }

  for (row <- 0 until HjpegConstants.BlockDim) {
    for (col <- 0 until HjpegConstants.BlockDim) {
      val sample = row * HjpegConstants.BlockDim + col
      y0Transform.io.input.bits.samples(sample) := y0Block(sample)
      y1Transform.io.input.bits.samples(sample) := y1Block(sample)
      y2Transform.io.input.bits.samples(sample) := y2Block(sample)
      y3Transform.io.input.bits.samples(sample) := y3Block(sample)
      cbTransform.io.input.bits.samples(sample) := cbBlock(sample)
      crTransform.io.input.bits.samples(sample) := crBlock(sample)
    }
  }

  val allTransformsValid = transforms.map(_.io.output.valid).reduce(_ && _)
  val allTransformsReady = io.output.ready && allTransformsValid
  for (transform <- transforms) {
    transform.io.output.ready := allTransformsReady
  }

  val lastBlockInBand = blockX + McuDim.U >= io.config.xsize

  io.output.valid := state === sEmit && allTransformsValid
  io.output.bits.mcu.yBlockCount := 4.U
  io.output.bits.mcu.y := y0Transform.io.output.bits
  io.output.bits.mcu.y1 := y1Transform.io.output.bits
  io.output.bits.mcu.y2 := y2Transform.io.output.bits
  io.output.bits.mcu.y3 := y3Transform.io.output.bits
  io.output.bits.mcu.cb := cbTransform.io.output.bits
  io.output.bits.mcu.cr := crTransform.io.output.bits
  io.output.bits.last := lastBlockInBand && currentBandLast

  when(io.output.fire) {
    when(lastBlockInBand) {
      state := sCollect
      blockX := 0.U
    }.otherwise {
      blockX := blockX + McuDim.U
      loadPhase := 0.U
      loadSample := 0.U
      chromaSubSample := 0.U
      cbAccumulator := 0.S
      crAccumulator := 0.S
      state := sLoad
    }
  }
}
