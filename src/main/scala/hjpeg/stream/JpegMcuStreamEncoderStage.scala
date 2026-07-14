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

  val sIdle :: sHeader :: sWaitMcu :: sStartBlock :: sBlock :: sRestartFlush :: sRestartHigh :: sRestartLow :: sFlush :: sEoiHigh :: sEoiLow :: Nil = Enum(11)
  val state = RegInit(sIdle)
  val previousDc = RegInit(VecInit(Seq.fill(HjpegConstants.Components)(0.S(coefficientBits.W))))
  val restartMcuCount = RegInit(0.U(16.W))
  val acceptedRestartMcuCount = RegInit(0.U(16.W))
  val restartMarker = RegInit(0.U(3.W))
  val frameLastEnqueued = RegInit(false.B)
  val restartEnabled = io.config.restartInterval =/= 0.U
  val nextRestartMcuCount = restartMcuCount + 1.U
  val restartDueAfterCurrentMcu = restartEnabled && nextRestartMcuCount >= io.config.restartInterval

  val header = Module(new JpegHeaderStage())
  header.io.config := io.config
  val acceptingFirstMcu = state === sIdle && io.input.fire
  header.io.start := acceptingFirstMcu

  val mcuEntropy = Module(new JpegPipelinedMcuEntropyStage(coefficientBits))
  val inputStateAllows = state === sIdle || state === sHeader || state === sWaitMcu || state === sBlock
  val restartInputLimitReached =
    restartEnabled && acceptedRestartMcuCount >= io.config.restartInterval
  mcuEntropy.io.input.valid :=
    io.input.valid && inputStateAllows && !frameLastEnqueued && !restartInputLimitReached
  mcuEntropy.io.input.bits := io.input.bits
  val zeroDc = Wire(Vec(HjpegConstants.Components, SInt(coefficientBits.W)))
  zeroDc.foreach(_ := 0.S)
  mcuEntropy.io.seedPreviousDc := Mux(state === sIdle, zeroDc, previousDc)
  io.input.ready :=
    inputStateAllows && !frameLastEnqueued && !restartInputLimitReached && mcuEntropy.io.input.ready
  // Test-only performance probes retain the established lane-zero name.
  val blockEncoder = mcuEntropy.engines.head.blockEncoders.head.encoder

  val packer = Module(new JpegBitRunPacker())
  packer.io.input.valid := state === sBlock && mcuEntropy.io.output.valid
  packer.io.input.bits := mcuEntropy.io.output.bits
  packer.io.flush := state === sFlush || state === sRestartFlush
  mcuEntropy.io.output.ready := state === sBlock && packer.io.input.ready

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
    is(sBlock, sRestartFlush, sFlush) {
      outputValid := packer.io.output.valid
      outputByte := packer.io.output.bits.byte
      packer.io.output.ready := io.output.ready
    }
    is(sRestartHigh) {
      outputValid := true.B
      outputByte := 0xff.U
    }
    is(sRestartLow) {
      outputValid := true.B
      outputByte := 0xd0.U | restartMarker
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

  when(io.input.fire) {
    when(io.input.bits.last) {
      frameLastEnqueued := true.B
    }
    when(restartEnabled) {
      acceptedRestartMcuCount := acceptedRestartMcuCount + 1.U
    }
  }

  when(acceptingFirstMcu) {
    previousDc.foreach(_ := 0.S)
    restartMcuCount := 0.U
    acceptedRestartMcuCount := Mux(restartEnabled, 1.U, 0.U)
    restartMarker := 0.U
    state := sHeader
  }.elsewhen(state === sHeader && header.io.done) {
    state := sBlock
  }.elsewhen(state === sWaitMcu && io.input.fire) {
    state := sBlock
  }.elsewhen(state === sBlock && mcuEntropy.io.completed) {
    previousDc := mcuEntropy.io.completedNextDc
    when(mcuEntropy.io.completedLast) {
      state := sFlush
    }.elsewhen(restartDueAfterCurrentMcu) {
      state := sRestartFlush
    }.otherwise {
      restartMcuCount := nextRestartMcuCount
      state := sBlock
    }
  }.elsewhen(state === sRestartFlush && packer.io.idle) {
    state := sRestartHigh
  }.elsewhen(state === sRestartHigh && io.output.fire) {
    state := sRestartLow
  }.elsewhen(state === sRestartLow && io.output.fire) {
    previousDc.foreach(_ := 0.S)
    restartMcuCount := 0.U
    acceptedRestartMcuCount := 0.U
    restartMarker := restartMarker + 1.U
    state := sWaitMcu
  }.elsewhen(state === sFlush && packer.io.idle) {
    state := sEoiHigh
  }.elsewhen(state === sEoiHigh && io.output.fire) {
    state := sEoiLow
  }.elsewhen(state === sEoiLow && io.output.fire) {
    previousDc.foreach(_ := 0.S)
    restartMcuCount := 0.U
    acceptedRestartMcuCount := 0.U
    frameLastEnqueued := false.B
    state := sIdle
  }

  io.busy := state =/= sIdle
}
