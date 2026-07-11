// See README.md for license details.

package hjpeg

import chisel3._
import chisel3.util._

/** Converts unsigned YCbCr components into signed DCT-domain samples.
  *
  * Baseline JPEG feeds the forward DCT with component samples centered around
  * zero. For 8-bit components this stage maps `[0, 255]` to `[-128, 127]` by
  * subtracting 128 from every component.
  */
class YCbCrLevelShiftStage(c: HjpegConfig = HjpegConfig()) extends Module {
  val io = IO(new Bundle {
    val input = Flipped(Decoupled(new YCbCrPixel(c)))
    val output = Decoupled(new LevelShiftedYCbCrPixel(c))
  })

  io.input.ready := io.output.ready
  io.output.valid := io.input.valid
  io.output.bits.x := io.input.bits.x
  io.output.bits.y := io.input.bits.y
  io.output.bits.ySample := io.input.bits.yComponent.zext - 128.S
  io.output.bits.cbSample := io.input.bits.cb.zext - 128.S
  io.output.bits.crSample := io.input.bits.cr.zext - 128.S
}
