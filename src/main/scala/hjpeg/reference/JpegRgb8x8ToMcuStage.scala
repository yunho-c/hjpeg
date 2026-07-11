// See README.md for license details.

package hjpeg

import chisel3._
import chisel3.util._

/** Collects one 8x8 RGB block and emits quantized Y/Cb/Cr zig-zag blocks.
  *
  * Pixels are consumed in raster order. Chroma is currently full-resolution
  * 4:4:4, matching the header emitted by `JpegHeaderStage`.
  */
class JpegRgb8x8ToMcuStage(c: HjpegConfig = HjpegConfig(), sampleBits: Int = 9, coefficientBits: Int = 16)
    extends Module {
  val io = IO(new Bundle {
    val quality = Input(UInt(7.W))
    val input = Flipped(Decoupled(new RgbPixel(c)))
    val output = Decoupled(new ZigZagMinimumCodedUnit(coefficientBits))
  })

  val sCollect :: sStartTransforms :: sWaitTransforms :: Nil = Enum(3)
  val state = RegInit(sCollect)
  val writeIndex = RegInit(0.U(6.W))
  val ySamples = Reg(Vec(HjpegConstants.BlockSize, SInt(sampleBits.W)))
  val cbSamples = Reg(Vec(HjpegConstants.BlockSize, SInt(sampleBits.W)))
  val crSamples = Reg(Vec(HjpegConstants.BlockSize, SInt(sampleBits.W)))

  io.input.ready := state === sCollect

  val (yComponent, cbComponent, crComponent) =
    JpegColorConversion.rgbToYCbCr(io.input.bits.r, io.input.bits.g, io.input.bits.b, c.pixelBits)

  when(io.input.fire) {
    ySamples(writeIndex) := (yComponent.zext - 128.S)(sampleBits - 1, 0).asSInt
    cbSamples(writeIndex) := (cbComponent.zext - 128.S)(sampleBits - 1, 0).asSInt
    crSamples(writeIndex) := (crComponent.zext - 128.S)(sampleBits - 1, 0).asSInt
    when(writeIndex === (HjpegConstants.BlockSize - 1).U) {
      state := sStartTransforms
    }.otherwise {
      writeIndex := writeIndex + 1.U
    }
  }

  val yTransform = Module(new JpegBlockTransformStage(sampleBits, coefficientBits))
  val cbTransform = Module(new JpegBlockTransformStage(sampleBits, coefficientBits))
  val crTransform = Module(new JpegBlockTransformStage(sampleBits, coefficientBits))

  yTransform.io.quality := io.quality
  yTransform.io.isLuminance := true.B
  cbTransform.io.quality := io.quality
  cbTransform.io.isLuminance := false.B
  crTransform.io.quality := io.quality
  crTransform.io.isLuminance := false.B

  val allTransformsInputReady =
    yTransform.io.input.ready && cbTransform.io.input.ready && crTransform.io.input.ready
  val startTransforms = state === sStartTransforms && allTransformsInputReady
  yTransform.io.input.valid := startTransforms
  cbTransform.io.input.valid := startTransforms
  crTransform.io.input.valid := startTransforms
  for (index <- 0 until HjpegConstants.BlockSize) {
    yTransform.io.input.bits.samples(index) := ySamples(index)
    cbTransform.io.input.bits.samples(index) := cbSamples(index)
    crTransform.io.input.bits.samples(index) := crSamples(index)
  }

  val allTransformsValid =
    yTransform.io.output.valid && cbTransform.io.output.valid && crTransform.io.output.valid
  val allTransformsReady =
    state === sWaitTransforms && io.output.ready && allTransformsValid

  when(startTransforms) {
    state := sWaitTransforms
  }

  yTransform.io.output.ready := allTransformsReady
  cbTransform.io.output.ready := allTransformsReady
  crTransform.io.output.ready := allTransformsReady

  io.output.valid := state === sWaitTransforms && allTransformsValid
  io.output.bits.yBlockCount := 1.U
  io.output.bits.y := yTransform.io.output.bits
  io.output.bits.y1 := yTransform.io.output.bits
  io.output.bits.y2 := yTransform.io.output.bits
  io.output.bits.y3 := yTransform.io.output.bits
  io.output.bits.cb := cbTransform.io.output.bits
  io.output.bits.cr := crTransform.io.output.bits

  when(io.output.fire) {
    state := sCollect
    writeIndex := 0.U
  }
}
