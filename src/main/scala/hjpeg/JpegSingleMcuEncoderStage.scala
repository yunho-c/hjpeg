// See README.md for license details.

package hjpeg

import chisel3._
import chisel3.util._

/** Emits a complete baseline JPEG byte stream for one 8x8, 4:4:4 MCU.
  *
  * This is the first frame-level assembly slice: it joins the header generator,
  * block entropy encoder, entropy byte packer, scan flush, and EOI marker. It is
  * intentionally scoped to one MCU so the byte-stream contract can be verified
  * before raster block buffering and multi-MCU scheduling are added.
  */
class JpegSingleMcuEncoderStage(coefficientBits: Int = 16) extends Module {
  val io = IO(new Bundle {
    val config = Input(new FrameConfig(HjpegConfig()))
    val input = Flipped(Decoupled(new ZigZagMinimumCodedUnit(coefficientBits)))
    val output = Decoupled(new EncodedByte(HjpegConfig()))
    val busy = Output(Bool())
  })

  val sIdle :: sHeader :: sStartBlock :: sBlock :: sFlush :: sEoiHigh :: sEoiLow :: Nil = Enum(7)
  val state = RegInit(sIdle)
  val component = RegInit(0.U(3.W))
  val mcu = Reg(new ZigZagMinimumCodedUnit(coefficientBits))
  val previousDc = RegInit(VecInit(Seq.fill(HjpegConstants.Components)(0.S(coefficientBits.W))))
  val yBlockCount = Mux(mcu.yBlockCount === 0.U, 1.U, mcu.yBlockCount)
  val cbComponent = yBlockCount
  val crComponent = yBlockCount + 1.U

  io.input.ready := state === sIdle
  when(io.input.fire) {
    mcu := io.input.bits
    previousDc.foreach(_ := 0.S)
    component := 0.U
    state := sHeader
  }

  val header = Module(new JpegHeaderStage())
  header.io.config := io.config
  header.io.start := io.input.fire

  val blockEncoder = Module(new JpegBlockEntropyStage(coefficientBits))
  blockEncoder.io.input.valid := state === sStartBlock
  val isLuminance = component < yBlockCount
  val predictorIndex = Mux(isLuminance, 0.U, Mux(component === cbComponent, 1.U, 2.U))
  blockEncoder.io.previousDc := previousDc(predictorIndex)
  blockEncoder.io.isLuminance := isLuminance
  blockEncoder.io.input.bits := MuxCase(
    mcu.y,
    Seq(
      (component === 1.U) -> mcu.y1,
      (component === 2.U) -> mcu.y2,
      (component === 3.U) -> mcu.y3,
      (component === cbComponent) -> mcu.cb,
      (component === crComponent) -> mcu.cr
    )
  )

  val packer = Module(new JpegBitRunPacker())
  packer.io.input.valid := state === sBlock && blockEncoder.io.output.valid
  packer.io.input.bits := blockEncoder.io.output.bits
  packer.io.flush := state === sFlush
  blockEncoder.io.output.ready := state === sBlock && packer.io.input.ready

  val outputValid = WireDefault(false.B)
  val outputByte = WireDefault(0.U(8.W))
  val outputLast = WireDefault(false.B)

  header.io.output.ready := false.B
  packer.io.output.ready := false.B

  switch(state) {
    is(sHeader) {
      outputValid := header.io.output.valid
      outputByte := header.io.output.bits.byte
      outputLast := false.B
      header.io.output.ready := io.output.ready
    }
    is(sBlock, sFlush) {
      outputValid := packer.io.output.valid
      outputByte := packer.io.output.bits.byte
      outputLast := false.B
      packer.io.output.ready := io.output.ready
    }
    is(sEoiHigh) {
      outputValid := true.B
      outputByte := 0xff.U
    }
    is(sEoiLow) {
      outputValid := true.B
      outputByte := 0xd9.U
      outputLast := true.B
    }
  }

  io.output.valid := outputValid
  io.output.bits.byte := outputByte
  io.output.bits.last := outputLast

  when(state === sHeader && header.io.done) {
    state := sStartBlock
  }.elsewhen(state === sStartBlock && blockEncoder.io.input.fire) {
    state := sBlock
  }.elsewhen(state === sBlock && !blockEncoder.io.busy && !blockEncoder.io.output.valid) {
    previousDc(predictorIndex) := blockEncoder.io.currentDc
    when(component === crComponent) {
      state := sFlush
    }.otherwise {
      component := component + 1.U
      state := sStartBlock
    }
  }.elsewhen(state === sFlush && packer.io.idle) {
    state := sEoiHigh
  }.elsewhen(state === sEoiHigh && io.output.fire) {
    state := sEoiLow
  }.elsewhen(state === sEoiLow && io.output.fire) {
    state := sIdle
  }

  io.busy := state =/= sIdle
}
