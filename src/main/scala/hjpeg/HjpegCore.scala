// See README.md for license details.

package hjpeg

import chisel3._
import chisel3.util._

/** Multi-lane JPEG encoder datapath used by scalar and vector stream shells. */
class HjpegGroupedCore(c: HjpegConfig = HjpegConfig(), inputLanes: Int = 1) extends Module {
  require(inputLanes > 0 && inputLanes <= 4, "grouped core supports one to four RGB lanes")

  val io = IO(new Bundle {
    val config = Input(new FrameConfig(c))
    val clearProtocolError = Input(Bool())
    val input = Flipped(Decoupled(new RgbPixelGroup(c, inputLanes)))
    val output = Decoupled(new EncodedByte(c))
    val busy = Output(Bool())
    val protocolError = Output(Bool())
  })

  val protocolError = RegInit(false.B)
  val inFrame = RegInit(false.B)

  val firstPixel = io.input.bits.pixels.head
  val lastPixel = io.input.bits.pixels.last
  val laneCoordinatesValid = VecInit((0 until inputLanes).map { lane =>
    val pixel = io.input.bits.pixels(lane)
    pixel.x === firstPixel.x + lane.U && pixel.y === firstPixel.y
  })
  val lanesInRange = VecInit((0 until inputLanes).map { lane =>
    val pixel = io.input.bits.pixels(lane)
    pixel.x < io.config.xsize && pixel.y < io.config.ysize
  })
  val supportedFrame =
    io.config.xsize =/= 0.U &&
      io.config.ysize =/= 0.U &&
      io.config.xsize <= c.maxFrameWidth.U &&
      io.config.ysize <= c.maxFrameHeight.U &&
      io.config.xsize % inputLanes.U === 0.U
  val inputInFrame = supportedFrame && laneCoordinatesValid.asUInt.andR && lanesInRange.asUInt.andR
  val isLastGroup =
    lastPixel.x === io.config.xsize - 1.U && lastPixel.y === io.config.ysize - 1.U

  val rasterToMcu = Module(new JpegUnifiedRasterToMcuStage(c, inputLanes = inputLanes))
  val encoder = Module(new JpegMcuStreamEncoderStage())
  rasterToMcu.io.config := io.config
  rasterToMcu.io.input.valid := io.input.valid && inputInFrame
  rasterToMcu.io.input.bits := io.input.bits

  encoder.io.config := io.config
  encoder.io.input.valid := rasterToMcu.io.output.valid
  encoder.io.input.bits := rasterToMcu.io.output.bits
  rasterToMcu.io.output.ready := encoder.io.input.ready
  encoder.io.output.ready := io.output.ready

  io.input.ready := Mux(inputInFrame, rasterToMcu.io.input.ready, true.B)
  io.output.valid := encoder.io.output.valid
  io.output.bits := encoder.io.output.bits

  when(io.clearProtocolError) {
    protocolError := false.B
  }

  when(io.input.fire) {
    inFrame := inputInFrame && !isLastGroup
    when(!inputInFrame) {
      protocolError := true.B
    }
  }

  io.busy := inFrame || encoder.io.busy || io.input.valid || io.output.valid
  io.protocolError := protocolError
}

/** JPEG encoder core.
  *
  * The integrated public path accepts raster RGB frames and emits a complete
  * baseline JPEG byte stream. Edge MCUs are padded by replicating the last valid
  * row or column. `enableChromaSubsample` selects 4:2:0; otherwise the encoder
  * emits 4:4:4.
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

  val grouped = Module(new HjpegGroupedCore(c, inputLanes = 1))
  grouped.io.config := io.config
  grouped.io.clearProtocolError := io.clearProtocolError
  grouped.io.input.valid := io.input.valid
  io.input.ready := grouped.io.input.ready
  grouped.io.input.bits.pixels(0) := io.input.bits
  io.output <> grouped.io.output
  io.busy := grouped.io.busy
  io.protocolError := grouped.io.protocolError
}
