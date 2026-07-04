// See README.md for license details.

package hjpeg

import chisel3._
import chisel3.util._

/** AXI4-Stream-shaped shell around `HjpegCore`.
  *
  * The input stream is raster RGB. Within `input.bits.data`, bits `[7:0]` are R,
  * `[15:8]` are G, and `[23:16]` are B. The wrapper generates the `x/y`
  * coordinates consumed by `HjpegCore` and checks that input `last` matches the
  * configured frame dimensions.
  */
class HjpegAxiStreamCore(c: HjpegConfig = HjpegConfig()) extends Module {
  val pixelDataBits = c.pixelBits * HjpegConstants.Components

  val io = IO(new Bundle {
    val config = Input(new FrameConfig(c))
    val clearProtocolError = Input(Bool())
    val input = Flipped(Decoupled(new AxiStreamWord(pixelDataBits)))
    val output = Decoupled(new AxiStreamWord(c.outputDataBits))
    val busy = Output(Bool())
    val protocolError = Output(Bool())
  })

  val core = Module(new HjpegCore(c))
  core.io.clearProtocolError := io.clearProtocolError

  val frameConfig = Reg(new FrameConfig(c))
  val frameConfigActive = RegInit(false.B)
  val activeConfig = Wire(new FrameConfig(c))
  activeConfig := io.config
  when(frameConfigActive) {
    activeConfig := frameConfig
  }
  core.io.config := activeConfig

  val x = RegInit(0.U(c.coordBits.W))
  val y = RegInit(0.U(c.coordBits.W))
  val inputFrameActive = RegInit(false.B)
  val inputWidth = RegInit(0.U(c.coordBits.W))
  val inputHeight = RegInit(0.U(c.coordBits.W))
  val protocolError = RegInit(false.B)

  val activeInputWidth = Mux(inputFrameActive, inputWidth, io.config.xsize)
  val activeInputHeight = Mux(inputFrameActive, inputHeight, io.config.ysize)
  val inputConfigSupported =
    io.config.xsize =/= 0.U &&
      io.config.ysize =/= 0.U &&
      io.config.xsize <= c.maxFrameWidth.U &&
      io.config.ysize <= c.maxFrameHeight.U
  val lastX = Mux(activeInputWidth === 0.U, 0.U, activeInputWidth - 1.U)
  val lastY = Mux(activeInputHeight === 0.U, 0.U, activeInputHeight - 1.U)
  val expectedLast = x === lastX && y === lastY
  val expectedKeep = Fill(pixelDataBits / 8, 1.U(1.W))
  val inputKeepValid = io.input.bits.keep === expectedKeep

  core.io.input.valid := io.input.valid
  io.input.ready := core.io.input.ready
  core.io.input.bits.x := x
  core.io.input.bits.y := y
  core.io.input.bits.r := io.input.bits.data(c.pixelBits - 1, 0)
  core.io.input.bits.g := io.input.bits.data((2 * c.pixelBits) - 1, c.pixelBits)
  core.io.input.bits.b := io.input.bits.data((3 * c.pixelBits) - 1, 2 * c.pixelBits)

  io.output.valid := core.io.output.valid
  core.io.output.ready := io.output.ready
  io.output.bits.data := core.io.output.bits.byte
  io.output.bits.keep := 1.U
  io.output.bits.last := core.io.output.bits.last

  when(io.clearProtocolError) {
    protocolError := false.B
    frameConfigActive := false.B
  }

  when(io.input.fire) {
    when(!inputFrameActive) {
      inputFrameActive := true.B
      inputWidth := io.config.xsize
      inputHeight := io.config.ysize
      frameConfig := io.config
      frameConfigActive := inputConfigSupported
    }
    when(io.input.bits.last =/= expectedLast) {
      protocolError := true.B
    }
    when(!inputKeepValid) {
      protocolError := true.B
    }
    when(expectedLast) {
      x := 0.U
      y := 0.U
      inputFrameActive := false.B
    }.elsewhen(x === lastX) {
      x := 0.U
      y := y + 1.U
    }.otherwise {
      x := x + 1.U
    }
  }

  when(io.output.fire && io.output.bits.last) {
    frameConfigActive := false.B
  }

  io.busy := core.io.busy || inputFrameActive
  io.protocolError := core.io.protocolError || protocolError
}
