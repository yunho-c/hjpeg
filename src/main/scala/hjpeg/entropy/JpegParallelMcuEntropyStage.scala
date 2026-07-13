// See README.md for license details.

package hjpeg

import chisel3._
import chisel3.util._

/** One block entropy encoder with a bounded run queue.
  *
  * Buffering lets blocks later in an MCU scan while earlier blocks are drained
  * in JPEG component order.
  */
class JpegBufferedBlockEntropyStage(coefficientBits: Int = 16, queueEntries: Int = 16) extends Module {
  require(queueEntries > 0, "entropy run queue must contain at least one entry")

  val io = IO(new Bundle {
    val input = Flipped(Decoupled(new ZigZagCoefficientBlock(coefficientBits)))
    val previousDc = Input(SInt(coefficientBits.W))
    val isLuminance = Input(Bool())
    val output = Decoupled(new JpegBitRun(32))
    val done = Output(Bool())
  })

  val encoder = Module(new JpegBlockEntropyStage(coefficientBits))
  val runs = Module(new Queue(new JpegBitRun(32), entries = queueEntries, pipe = true))
  val active = RegInit(false.B)

  encoder.io.input.valid := io.input.valid && !active
  encoder.io.input.bits := io.input.bits
  encoder.io.previousDc := io.previousDc
  encoder.io.isLuminance := io.isLuminance
  io.input.ready := !active && encoder.io.input.ready

  runs.io.enq <> encoder.io.output
  io.output <> runs.io.deq

  val fullyDrained = active && !encoder.io.busy && !encoder.io.output.valid && !runs.io.deq.valid
  io.done := fullyDrained

  when(io.input.fire) {
    active := true.B
  }.elsewhen(fullyDrained) {
    active := false.B
  }
}

/** Scans every block of one MCU in parallel and drains bit runs in JPEG order. */
class JpegParallelMcuEntropyStage(
    coefficientBits: Int = 16,
    queueEntriesPerBlock: Int = 16)
    extends Module {
  private val MaxBlocks = 6

  val io = IO(new Bundle {
    val input = Flipped(Decoupled(new ZigZagMinimumCodedUnit(coefficientBits)))
    val previousDc = Input(Vec(HjpegConstants.Components, SInt(coefficientBits.W)))
    val output = Decoupled(new JpegBitRun(32))
    val nextDc = Output(Vec(HjpegConstants.Components, SInt(coefficientBits.W)))
    val done = Output(Bool())
    val busy = Output(Bool())
  })

  val blockEncoders = Seq.fill(MaxBlocks)(
    Module(new JpegBufferedBlockEntropyStage(coefficientBits, queueEntriesPerBlock)))
  val active = RegInit(false.B)
  val blockCount = RegInit(3.U(3.W))
  val drainBlock = RegInit(0.U(3.W))
  val nextDc = Reg(Vec(HjpegConstants.Components, SInt(coefficientBits.W)))

  val subsampledInput = io.input.bits.yBlockCount === 4.U
  val inputBlockCount = Mux(subsampledInput, 6.U, 3.U)
  val allInputsReady = VecInit(blockEncoders.map(_.io.input.ready)).asUInt.andR
  io.input.ready := !active && allInputsReady
  val inputFire = io.input.valid && io.input.ready

  val y0Dc = io.input.bits.y.coefficients(0)
  val y1Dc = io.input.bits.y1.coefficients(0)
  val y2Dc = io.input.bits.y2.coefficients(0)
  val y3Dc = io.input.bits.y3.coefficients(0)

  for ((blockEncoder, index) <- blockEncoders.zipWithIndex) {
    val selectedBlock = index match {
      case 0 => io.input.bits.y
      case 1 => Mux(subsampledInput, io.input.bits.y1, io.input.bits.cb)
      case 2 => Mux(subsampledInput, io.input.bits.y2, io.input.bits.cr)
      case 3 => io.input.bits.y3
      case 4 => io.input.bits.cb
      case _ => io.input.bits.cr
    }
    val previous = index match {
      case 0 => io.previousDc(0)
      case 1 => Mux(subsampledInput, y0Dc, io.previousDc(1))
      case 2 => Mux(subsampledInput, y1Dc, io.previousDc(2))
      case 3 => y2Dc
      case 4 => io.previousDc(1)
      case _ => io.previousDc(2)
    }

    blockEncoder.io.input.valid := inputFire && index.U < inputBlockCount
    blockEncoder.io.input.bits := selectedBlock
    blockEncoder.io.previousDc := previous
    blockEncoder.io.isLuminance := index.U === 0.U || (subsampledInput && index.U < 4.U)
    blockEncoder.io.output.ready := active && drainBlock === index.U && io.output.ready
  }

  when(inputFire) {
    active := true.B
    blockCount := inputBlockCount
    drainBlock := 0.U
    nextDc(0) := Mux(subsampledInput, y3Dc, y0Dc)
    nextDc(1) := io.input.bits.cb.coefficients(0)
    nextDc(2) := io.input.bits.cr.coefficients(0)
  }

  val selectedOutputValid = MuxLookup(drainBlock, false.B)(
    blockEncoders.zipWithIndex.map { case (encoder, index) => index.U -> encoder.io.output.valid })
  val selectedOutputBits = MuxLookup(drainBlock, blockEncoders.head.io.output.bits)(
    blockEncoders.zipWithIndex.map { case (encoder, index) => index.U -> encoder.io.output.bits })
  val selectedDone = MuxLookup(drainBlock, false.B)(
    blockEncoders.zipWithIndex.map { case (encoder, index) => index.U -> encoder.io.done })

  io.output.valid := active && selectedOutputValid
  io.output.bits := selectedOutputBits
  val finalBlockDone = active && selectedDone && drainBlock === blockCount - 1.U
  io.done := finalBlockDone

  when(active && selectedDone) {
    when(drainBlock === blockCount - 1.U) {
      active := false.B
      drainBlock := 0.U
    }.otherwise {
      drainBlock := drainBlock + 1.U
    }
  }

  io.nextDc := nextDc
  io.busy := active
}
