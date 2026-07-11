// See README.md for license details.

package hjpeg

import chisel3._
import chisel3.util._

/** Encodes one raster-ordered 8x8 RGB block into a complete baseline JPEG. */
class JpegRgb8x8EncoderStage(c: HjpegConfig = HjpegConfig(), sampleBits: Int = 9, coefficientBits: Int = 16)
    extends Module {
  val io = IO(new Bundle {
    val config = Input(new FrameConfig(c))
    val input = Flipped(Decoupled(new RgbPixel(c)))
    val output = Decoupled(new EncodedByte(c))
    val busy = Output(Bool())
  })

  val rgbToMcu = Module(new JpegRgb8x8ToMcuStage(c, sampleBits, coefficientBits))
  val encoder = Module(new JpegSingleMcuEncoderStage(coefficientBits))

  rgbToMcu.io.quality := io.config.quality
  rgbToMcu.io.input <> io.input

  encoder.io.config := io.config
  encoder.io.input <> rgbToMcu.io.output
  io.output <> encoder.io.output

  io.busy := encoder.io.busy || !rgbToMcu.io.input.ready
}
