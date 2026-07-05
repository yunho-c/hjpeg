// See README.md for license details.

package hjpeg

import chisel3._
import chisel3.util._

/** KV260-oriented RTL top.
  *
  * This is a stable elaboration target for the programmable-logic encoder
  * block. Board-specific clocking, reset synchronization, DMA, and AXI-Lite
  * packaging should wrap this module outside the core encoder tree.
  */
class HjpegKv260Top(c: HjpegConfig = HjpegConfig()) extends Module {
  val pixelDataBits = c.pixelBits * HjpegConstants.Components
  val dmaInputDataBits = 32

  val io = IO(new Bundle {
    val config = Input(new FrameConfig(c))
    val clearProtocolError = Input(Bool())
    val sAxisRgb = Flipped(Decoupled(new AxiStreamWord(dmaInputDataBits)))
    val mAxisJpeg = Decoupled(new AxiStreamWord(c.outputDataBits))
    val busy = Output(Bool())
    val protocolError = Output(Bool())
  })

  val core = Module(new HjpegAxiStreamCore(c))
  core.io.config := io.config
  core.io.clearProtocolError := io.clearProtocolError
  core.io.input.valid := io.sAxisRgb.valid
  io.sAxisRgb.ready := core.io.input.ready
  core.io.input.bits.data := io.sAxisRgb.bits.data(pixelDataBits - 1, 0)
  core.io.input.bits.keep := io.sAxisRgb.bits.keep((pixelDataBits / 8) - 1, 0)
  core.io.input.bits.last := io.sAxisRgb.bits.last
  io.mAxisJpeg <> core.io.output
  io.busy := core.io.busy
  io.protocolError := core.io.protocolError
}
