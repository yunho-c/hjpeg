// See README.md for license details.

package hjpeg

import chisel3._
import chisel3.util._

/** Buffers one 8-row raster stripe and emits 8x8, 4:4:4 MCUs.
  *
  * This stage is the first raster-order frame buffer. It accepts pixels in
  * row-major order, stores level-shifted Y/Cb/Cr samples for eight rows, then
  * emits MCUs left-to-right for that stripe. Frame dimensions must currently be
  * multiples of eight; edge padding belongs in a later extension.
  */
class JpegRasterToMcuStage(c: HjpegConfig = HjpegConfig(), sampleBits: Int = 9, coefficientBits: Int = 16)
    extends Module {
  val io = IO(new Bundle {
    val config = Input(new FrameConfig(c))
    val input = Flipped(Decoupled(new RgbPixel(c)))
    val output = Decoupled(new ZigZagMinimumCodedUnitPacket(coefficientBits))
  })

  private val StripeRows = HjpegConstants.BlockDim
  private val StripeSamples = StripeRows * c.maxFrameWidth
  private val sampleIndexBits = log2Ceil(StripeSamples).max(1)

  val sCollect :: sLoad :: sTransform :: sEmit :: Nil = Enum(4)
  val state = RegInit(sCollect)
  val blockX = RegInit(0.U(c.coordBits.W))
  val currentStripeLast = RegInit(false.B)
  val lastRowInStripe = RegInit(0.U(3.W))
  val loadSample = RegInit(0.U(6.W))
  val transformBlock = RegInit(0.U(2.W))

  val ySamples = Mem(StripeSamples, SInt(sampleBits.W))
  val cbSamples = Mem(StripeSamples, SInt(sampleBits.W))
  val crSamples = Mem(StripeSamples, SInt(sampleBits.W))
  val yBlock = Reg(Vec(HjpegConstants.BlockSize, SInt(sampleBits.W)))
  val cbBlock = Reg(Vec(HjpegConstants.BlockSize, SInt(sampleBits.W)))
  val crBlock = Reg(Vec(HjpegConstants.BlockSize, SInt(sampleBits.W)))
  val yCoefficients = Reg(new ZigZagCoefficientBlock(coefficientBits))
  val cbCoefficients = Reg(new ZigZagCoefficientBlock(coefficientBits))
  val crCoefficients = Reg(new ZigZagCoefficientBlock(coefficientBits))

  val rowInStripe = io.input.bits.y(2, 0)
  val writeIndex = (rowInStripe * c.maxFrameWidth.U + io.input.bits.x)(sampleIndexBits - 1, 0)
  val lastPixelInStripe =
    io.input.bits.x === io.config.xsize - 1.U &&
      (rowInStripe === (StripeRows - 1).U || io.input.bits.y === io.config.ysize - 1.U)

  val (yComponent, cbComponent, crComponent) =
    JpegColorConversion.rgbToYCbCr(io.input.bits.r, io.input.bits.g, io.input.bits.b, c.pixelBits)

  io.input.ready := state === sCollect

  when(io.input.fire) {
    ySamples(writeIndex) := (yComponent.zext - 128.S)(sampleBits - 1, 0).asSInt
    cbSamples(writeIndex) := (cbComponent.zext - 128.S)(sampleBits - 1, 0).asSInt
    crSamples(writeIndex) := (crComponent.zext - 128.S)(sampleBits - 1, 0).asSInt
    when(lastPixelInStripe) {
      state := sLoad
      blockX := 0.U
      currentStripeLast := io.input.bits.y + 1.U >= io.config.ysize
      lastRowInStripe := rowInStripe
      loadSample := 0.U
      transformBlock := 0.U
    }
  }

  val loadRow = loadSample(5, 3)
  val loadCol = loadSample(2, 0)
  val readRow = Mux(loadRow > lastRowInStripe, lastRowInStripe, loadRow)
  val requestedCol = blockX + loadCol
  val readCol = Mux(requestedCol >= io.config.xsize, io.config.xsize - 1.U, requestedCol)
  val readIndex = (readRow * c.maxFrameWidth.U + readCol)(sampleIndexBits - 1, 0)
  val yLoadSample = ySamples(readIndex)
  val cbLoadSample = cbSamples(readIndex)
  val crLoadSample = crSamples(readIndex)

  when(state === sLoad) {
    yBlock(loadSample) := yLoadSample
    cbBlock(loadSample) := cbLoadSample
    crBlock(loadSample) := crLoadSample
    when(loadSample === (HjpegConstants.BlockSize - 1).U) {
      state := sTransform
      transformBlock := 0.U
    }.otherwise {
      loadSample := loadSample + 1.U
    }
  }

  val transform = Module(new JpegBlockTransformStage(sampleBits, coefficientBits))

  transform.io.quality := io.config.quality
  transform.io.isLuminance := transformBlock === 0.U
  transform.io.input.valid := state === sTransform

  for (row <- 0 until HjpegConstants.BlockDim) {
    for (col <- 0 until HjpegConstants.BlockDim) {
      val blockSample = row * HjpegConstants.BlockDim + col
      transform.io.input.bits.samples(blockSample) :=
        Mux(transformBlock === 0.U, yBlock(blockSample), Mux(transformBlock === 1.U, cbBlock(blockSample), crBlock(blockSample)))
    }
  }

  transform.io.output.ready := state === sTransform

  when(transform.io.output.fire) {
    when(transformBlock === 0.U) {
      yCoefficients := transform.io.output.bits
      transformBlock := 1.U
    }.elsewhen(transformBlock === 1.U) {
      cbCoefficients := transform.io.output.bits
      transformBlock := 2.U
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
      loadSample := 0.U
      transformBlock := 0.U
      state := sLoad
    }
  }
}
