// See README.md for license details.

package hjpeg

import chisel3._
import chisel3.util._

/** AXI4-Stream-shaped shell around `HjpegCore`.
  *
  * The input stream is raster RGB. Within `input.bits.data`, bits `[7:0]` are R,
  * `[15:8]` are G, and `[23:16]` are B. The wrapper generates the `x/y`
  * coordinates consumed by `HjpegCore` and checks that input `last` matches the
  * configured frame dimensions. Frames may overlap only while every config
  * field matches the active snapshot.
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

  val core = withReset(reset.asBool || io.clearProtocolError) {
    Module(new HjpegCore(c))
  }
  core.io.clearProtocolError := io.clearProtocolError

  val frameConfig = Reg(new FrameConfig(c))
  // One MCU may be in the encoder while each of the two raster slots owns a
  // later frame. All in-flight frames must share this config snapshot.
  val framesInFlight = RegInit(0.U(2.W))
  val frameConfigActive = framesInFlight =/= 0.U
  val activeConfig = Wire(new FrameConfig(c))
  activeConfig := io.config
  when(frameConfigActive) {
    activeConfig := frameConfig
  }
  core.io.config := activeConfig

  val x = RegInit(0.U(c.coordBits.W))
  val y = RegInit(0.U(c.coordBits.W))
  val inputFrameActive = RegInit(false.B)
  val inputFrameSupported = RegInit(false.B)
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
  val activeInputSupported = Mux(inputFrameActive, inputFrameSupported, inputConfigSupported)
  val lastX = Mux(activeInputWidth === 0.U, 0.U, activeInputWidth - 1.U)
  val lastY = Mux(activeInputHeight === 0.U, 0.U, activeInputHeight - 1.U)
  val expectedLast = x === lastX && y === lastY
  val lateInputLastMissing = activeInputSupported && expectedLast && !io.input.bits.last
  val inputFrameDone = io.input.bits.last
  val expectedKeep = Fill(pixelDataBits / 8, 1.U(1.W))
  val inputKeepValid = io.input.bits.keep === expectedKeep
  val feedCoreInput = activeInputSupported && inputKeepValid
  val inputConfigMatches = io.config.asUInt === frameConfig.asUInt
  val canStartInputFrame =
    !frameConfigActive || (framesInFlight =/= 3.U && inputConfigMatches)
  // Continue an already snapshotted input frame despite live register writes.
  // A later frame may overlap only when it uses the exact active config.
  val inputFrameCanAdvance = inputFrameActive || canStartInputFrame

  core.io.input.valid := io.input.valid && feedCoreInput && inputFrameCanAdvance
  io.input.ready := inputFrameCanAdvance && Mux(feedCoreInput, core.io.input.ready, true.B)
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
    x := 0.U
    y := 0.U
    inputFrameActive := false.B
    inputFrameSupported := false.B
  }

  when(io.input.fire) {
    when(!inputFrameActive) {
      inputFrameActive := true.B
      inputFrameSupported := inputConfigSupported
      inputWidth := io.config.xsize
      inputHeight := io.config.ysize
      when(!frameConfigActive && inputConfigSupported && inputKeepValid) {
        frameConfig := io.config
      }
    }
    when(!activeInputSupported) {
      protocolError := true.B
    }
    when(activeInputSupported && io.input.bits.last =/= expectedLast) {
      protocolError := true.B
    }
    when(!inputKeepValid) {
      protocolError := true.B
      inputFrameSupported := false.B
    }
    when(lateInputLastMissing) {
      x := 0.U
      y := 0.U
      inputFrameSupported := false.B
    }.elsewhen(inputFrameDone) {
      x := 0.U
      y := 0.U
      inputFrameActive := false.B
      inputFrameSupported := false.B
    }.elsewhen(activeInputSupported && x === lastX) {
      x := 0.U
      y := y + 1.U
    }.elsewhen(activeInputSupported) {
      x := x + 1.U
    }
  }

  val acceptedFrameStart =
    io.input.fire && !inputFrameActive && inputConfigSupported && inputKeepValid
  val completedOutputFrame = io.output.fire && io.output.bits.last
  when(io.clearProtocolError) {
    framesInFlight := 0.U
  }.elsewhen(acceptedFrameStart =/= completedOutputFrame) {
    when(acceptedFrameStart) {
      framesInFlight := framesInFlight + 1.U
    }.otherwise {
      framesInFlight := framesInFlight - 1.U
    }
  }

  io.busy := core.io.busy || inputFrameActive || frameConfigActive
  io.protocolError := core.io.protocolError || protocolError
}
