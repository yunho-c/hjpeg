// See README.md for license details.

package hjpeg

import chisel3._
import chisel3.util._

/** Emits one complete baseline JPEG stream from one or more 8x8, 4:4:4 MCUs.
  *
  * The first accepted MCU starts a frame and emits SOI through SOS. Entropy data
  * is packed continuously across subsequent MCUs, preserving the DC predictors
  * for Y, Cb, and Cr. The MCU packet marked `last` flushes entropy padding and
  * emits the EOI marker.
  */
class JpegMcuStreamEncoderStage(coefficientBits: Int = 16) extends Module {
  val io = IO(new Bundle {
    val config = Input(new FrameConfig(HjpegConfig()))
    val input = Flipped(Decoupled(new ZigZagMinimumCodedUnitPacket(coefficientBits)))
    val output = Decoupled(new EncodedByte(HjpegConfig()))
    val busy = Output(Bool())
  })

  val sIdle :: sHeader :: sWaitMcu :: sStartBlock :: sBlock :: sFlush :: sEoiHigh :: sEoiLow :: Nil = Enum(8)
  val state = RegInit(sIdle)
  val component = RegInit(0.U(3.W))
  val currentMcuLast = RegInit(false.B)
  val currentMcu = Reg(new ZigZagMinimumCodedUnit(coefficientBits))
  val previousDc = RegInit(VecInit(Seq.fill(HjpegConstants.Components)(0.S(coefficientBits.W))))
  val yBlockCount = Mux(currentMcu.yBlockCount === 0.U, 1.U, currentMcu.yBlockCount)
  val cbComponent = yBlockCount
  val crComponent = yBlockCount + 1.U

  val acceptingFirstMcu = state === sIdle && io.input.valid
  io.input.ready := state === sIdle || state === sWaitMcu

  when(io.input.fire) {
    currentMcu := io.input.bits.mcu
    currentMcuLast := io.input.bits.last
    component := 0.U
    when(state === sIdle) {
      previousDc.foreach(_ := 0.S)
      state := sHeader
    }.otherwise {
      state := sStartBlock
    }
  }

  val header = Module(new JpegHeaderStage())
  header.io.config := io.config
  header.io.start := acceptingFirstMcu && io.input.ready

  val blockEncoder = Module(new JpegBlockEntropyStage(coefficientBits))
  blockEncoder.io.input.valid := state === sStartBlock
  val isLuminance = component < yBlockCount
  val predictorIndex = Mux(isLuminance, 0.U, Mux(component === cbComponent, 1.U, 2.U))
  blockEncoder.io.previousDc := previousDc(predictorIndex)
  blockEncoder.io.isLuminance := isLuminance
  blockEncoder.io.input.bits := MuxCase(
    currentMcu.y,
    Seq(
      (component === 1.U) -> currentMcu.y1,
      (component === 2.U) -> currentMcu.y2,
      (component === 3.U) -> currentMcu.y3,
      (component === cbComponent) -> currentMcu.cb,
      (component === crComponent) -> currentMcu.cr
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
      header.io.output.ready := io.output.ready
    }
    is(sBlock, sFlush) {
      outputValid := packer.io.output.valid
      outputByte := packer.io.output.bits.byte
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
      when(currentMcuLast) {
        state := sFlush
      }.otherwise {
        state := sWaitMcu
      }
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
