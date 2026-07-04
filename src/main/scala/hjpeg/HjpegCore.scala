// See README.md for license details.

package hjpeg

import chisel3._
import chisel3.util._

/** Initial encoder shell.
  *
  * This module establishes the streaming boundary and frame bookkeeping used by
  * later JPEG stages. The current payload path emits a luma-like byte per input
  * pixel; it is a smoke-test stand-in, not a JPEG bitstream.
  */
class HjpegCore(c: HjpegConfig = HjpegConfig()) extends Module {
  val io = IO(new Bundle {
    val config = Input(new FrameConfig(c))
    val clearProtocolError = Input(Bool())
    val input = Flipped(Decoupled(new RgbPixel(c)))
    val output = Decoupled(new EncodedByte(c))
    val busy = Output(Bool())
    val protocolError = Output(Bool())
  })

  val protocolError = RegInit(false.B)
  val inFrame = RegInit(false.B)

  val xInRange = io.input.bits.x < io.config.xsize
  val yInRange = io.input.bits.y < io.config.ysize
  val isLastPixel =
    io.input.bits.x === io.config.xsize - 1.U && io.input.bits.y === io.config.ysize - 1.U

  val rTerm = io.input.bits.r * 77.U
  val gTerm = io.input.bits.g * 150.U
  val bTerm = io.input.bits.b * 29.U
  val luma = (rTerm +& gTerm +& bTerm) >> 8

  io.input.ready := io.output.ready
  io.output.valid := io.input.valid
  io.output.bits.byte := luma(7, 0)
  io.output.bits.last := isLastPixel

  when(io.clearProtocolError) {
    protocolError := false.B
  }

  when(io.input.fire) {
    inFrame := !isLastPixel
    when(!xInRange || !yInRange || io.config.xsize === 0.U || io.config.ysize === 0.U) {
      protocolError := true.B
    }
  }

  io.busy := inFrame || io.input.valid || io.output.valid
  io.protocolError := protocolError
}
