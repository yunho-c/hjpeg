// See README.md for license details.

package hjpeg

import chisel3._
import chisel3.util._

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

  val protocolError = RegInit(false.B)
  val inFrame = RegInit(false.B)

  val xInRange = io.input.bits.x < io.config.xsize
  val yInRange = io.input.bits.y < io.config.ysize
  val isLastPixel =
    io.input.bits.x === io.config.xsize - 1.U && io.input.bits.y === io.config.ysize - 1.U
  val supportedFrame =
    io.config.xsize =/= 0.U &&
      io.config.ysize =/= 0.U &&
      io.config.xsize <= c.maxFrameWidth.U &&
      io.config.ysize <= c.maxFrameHeight.U
  val inputInFrame = supportedFrame && xInRange && yInRange

  val rasterToMcu = Module(new JpegRasterToMcuStage(c))
  val rasterToSubsampledMcu = Module(new JpegRasterToSubsampledMcuStage(c))
  val encoder = Module(new JpegMcuStreamEncoderStage())
  rasterToMcu.io.config := io.config
  rasterToMcu.io.input.valid := io.input.valid && inputInFrame && !io.config.enableChromaSubsample
  rasterToMcu.io.input.bits := io.input.bits
  rasterToSubsampledMcu.io.config := io.config
  rasterToSubsampledMcu.io.input.valid := io.input.valid && inputInFrame && io.config.enableChromaSubsample
  rasterToSubsampledMcu.io.input.bits := io.input.bits

  encoder.io.config := io.config
  val selectedMcuValid = Mux(io.config.enableChromaSubsample, rasterToSubsampledMcu.io.output.valid, rasterToMcu.io.output.valid)
  val selectedMcuBits = Mux(io.config.enableChromaSubsample, rasterToSubsampledMcu.io.output.bits, rasterToMcu.io.output.bits)
  encoder.io.input.valid := selectedMcuValid
  encoder.io.input.bits := selectedMcuBits
  rasterToMcu.io.output.ready := encoder.io.input.ready && !io.config.enableChromaSubsample
  rasterToSubsampledMcu.io.output.ready := encoder.io.input.ready && io.config.enableChromaSubsample
  encoder.io.output.ready := io.output.ready

  io.input.ready := Mux(
    inputInFrame,
    Mux(io.config.enableChromaSubsample, rasterToSubsampledMcu.io.input.ready, rasterToMcu.io.input.ready),
    true.B
  )
  io.output.valid := encoder.io.output.valid
  io.output.bits := encoder.io.output.bits

  when(io.clearProtocolError) {
    protocolError := false.B
  }

  when(io.input.fire) {
    inFrame := inputInFrame && !isLastPixel
    when(!inputInFrame) {
      protocolError := true.B
    }
  }

  io.busy := inFrame || encoder.io.busy || io.input.valid || io.output.valid
  io.protocolError := protocolError
}
