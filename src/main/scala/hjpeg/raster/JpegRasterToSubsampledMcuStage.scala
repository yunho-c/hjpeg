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

  val sCollect :: sLoad :: sTransform :: sEmit :: Nil = Enum(4)
  val state = RegInit(sCollect)
  val blockX = RegInit(0.U(c.coordBits.W))
  val currentBandLast = RegInit(false.B)
  val lastRowInBand = RegInit(0.U(4.W))
  val loadPhase = RegInit(0.U(3.W))
  val loadSample = RegInit(0.U(6.W))
  val transformBlock = RegInit(0.U(3.W))
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
  val y0Coefficients = Reg(new ZigZagCoefficientBlock(coefficientBits))
  val y1Coefficients = Reg(new ZigZagCoefficientBlock(coefficientBits))
  val y2Coefficients = Reg(new ZigZagCoefficientBlock(coefficientBits))
  val y3Coefficients = Reg(new ZigZagCoefficientBlock(coefficientBits))
  val cbCoefficients = Reg(new ZigZagCoefficientBlock(coefficientBits))
  val crCoefficients = Reg(new ZigZagCoefficientBlock(coefficientBits))

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
      transformBlock := 0.U
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
          state := sTransform
          transformBlock := 0.U
        }.otherwise {
          loadSample := loadSample + 1.U
        }
      }.otherwise {
        chromaSubSample := chromaSubSample + 1.U
      }
    }
  }

  val transform = Module(new JpegBlockTransformStage(sampleBits, coefficientBits))
  transform.io.quality := io.config.quality
  transform.io.isLuminance := transformBlock < 4.U
  transform.io.input.valid := state === sTransform

  private def clampedIndex(row: UInt, col: UInt): UInt = {
    val readRow = Mux(row > lastRowInBand, lastRowInBand, row(3, 0))
    val readCol = Mux(col >= io.config.xsize, io.config.xsize - 1.U, col)
    (readRow * c.maxFrameWidth.U + readCol)(sampleIndexBits - 1, 0)
  }

  for (row <- 0 until HjpegConstants.BlockDim) {
    for (col <- 0 until HjpegConstants.BlockDim) {
      val sample = row * HjpegConstants.BlockDim + col
      val y01Sample = Mux(transformBlock === 0.U, y0Block(sample), y1Block(sample))
      val y23Sample = Mux(transformBlock === 2.U, y2Block(sample), y3Block(sample))
      val ySample = Mux(transformBlock < 2.U, y01Sample, y23Sample)
      val chromaSample = Mux(transformBlock === 4.U, cbBlock(sample), crBlock(sample))
      transform.io.input.bits.samples(sample) := Mux(transformBlock < 4.U, ySample, chromaSample)
    }
  }

  transform.io.output.ready := state === sTransform

  when(transform.io.output.fire) {
    switch(transformBlock) {
      is(0.U) {
        y0Coefficients := transform.io.output.bits
        transformBlock := 1.U
      }
      is(1.U) {
        y1Coefficients := transform.io.output.bits
        transformBlock := 2.U
      }
      is(2.U) {
        y2Coefficients := transform.io.output.bits
        transformBlock := 3.U
      }
      is(3.U) {
        y3Coefficients := transform.io.output.bits
        transformBlock := 4.U
      }
      is(4.U) {
        cbCoefficients := transform.io.output.bits
        transformBlock := 5.U
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
      transformBlock := 0.U
      chromaSubSample := 0.U
      cbAccumulator := 0.S
      crAccumulator := 0.S
      state := sLoad
    }
  }
}
