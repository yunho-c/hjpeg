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

  val sCollect :: sEmit :: Nil = Enum(2)
  val state = RegInit(sCollect)
  val blockX = RegInit(0.U(c.coordBits.W))
  val currentBandLast = RegInit(false.B)
  val lastRowInBand = RegInit(0.U(4.W))

  val ySamples = Mem(BandSamples, SInt(sampleBits.W))
  val cbSamples = Mem(BandSamples, SInt(sampleBits.W))
  val crSamples = Mem(BandSamples, SInt(sampleBits.W))

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
      state := sEmit
      blockX := 0.U
      currentBandLast := io.input.bits.y + 1.U >= io.config.ysize
      lastRowInBand := rowInBand
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

  private def chromaAverage(samples: Mem[SInt], row: Int, col: Int): SInt = {
    val row0 = (row * 2).U
    val row1 = (row * 2 + 1).U
    val col0 = blockX + (col * 2).U
    val col1 = blockX + (col * 2 + 1).U
    val sum =
      samples(clampedIndex(row0, col0)) +&
        samples(clampedIndex(row0, col1)) +&
        samples(clampedIndex(row1, col0)) +&
        samples(clampedIndex(row1, col1))
    (sum >> 2).asSInt
  }

  for (row <- 0 until HjpegConstants.BlockDim) {
    for (col <- 0 until HjpegConstants.BlockDim) {
      val sample = row * HjpegConstants.BlockDim + col
      y0Transform.io.input.bits.samples(sample) := ySamples(clampedIndex(row.U, blockX + col.U))
      y1Transform.io.input.bits.samples(sample) := ySamples(clampedIndex(row.U, blockX + (col + HjpegConstants.BlockDim).U))
      y2Transform.io.input.bits.samples(sample) := ySamples(clampedIndex((row + HjpegConstants.BlockDim).U, blockX + col.U))
      y3Transform.io.input.bits.samples(sample) :=
        ySamples(clampedIndex((row + HjpegConstants.BlockDim).U, blockX + (col + HjpegConstants.BlockDim).U))
      cbTransform.io.input.bits.samples(sample) := chromaAverage(cbSamples, row, col)
      crTransform.io.input.bits.samples(sample) := chromaAverage(crSamples, row, col)
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
    }
  }
}
