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

/** Scans one MCU through three buffered entropy slots and drains runs in JPEG order.
  *
  * 4:4:4 occupies all three slots once. 4:2:0 first loads Y0/Y1/Y2, then
  * reuses those slots for Y3/Cb/Cr as the first wave drains. The second wave
  * overlaps the remaining first-wave drain, retaining the ordered streaming
  * throughput without duplicating six complete AC scanners.
  */
class JpegParallelMcuEntropyStage(
    coefficientBits: Int = 16,
    queueEntriesPerBlock: Int = 16)
    extends Module {
  private val MaxBlocks = 6
  private val EncoderSlots = 3

  val io = IO(new Bundle {
    val input = Flipped(Decoupled(new ZigZagMinimumCodedUnit(coefficientBits)))
    val previousDc = Input(Vec(HjpegConstants.Components, SInt(coefficientBits.W)))
    val output = Decoupled(new JpegBitRun(32))
    val nextDc = Output(Vec(HjpegConstants.Components, SInt(coefficientBits.W)))
    val done = Output(Bool())
    val busy = Output(Bool())
  })

  val blockEncoders = Seq.fill(EncoderSlots)(
    Module(new JpegBufferedBlockEntropyStage(coefficientBits, queueEntriesPerBlock)))
  val active = RegInit(false.B)
  val activeSubsampled = RegInit(false.B)
  val blockCount = RegInit(3.U(3.W))
  val drainBlock = RegInit(0.U(3.W))
  val nextDc = Reg(Vec(HjpegConstants.Components, SInt(coefficientBits.W)))
  val deferredBlocks = Reg(Vec(EncoderSlots, new ZigZagCoefficientBlock(coefficientBits)))
  val deferredPreviousDc = Reg(Vec(EncoderSlots, SInt(coefficientBits.W)))
  val deferredIsLuminance = Reg(Vec(EncoderSlots, Bool()))
  val reloadPending = RegInit(VecInit(Seq.fill(EncoderSlots)(false.B)))

  val subsampledInput = io.input.bits.yBlockCount === 4.U
  val inputBlockCount = Mux(subsampledInput, MaxBlocks.U, EncoderSlots.U)
  val allInputsReady = VecInit(blockEncoders.map(_.io.input.ready)).asUInt.andR
  io.input.ready := !active && !reloadPending.asUInt.orR && allInputsReady
  val inputFire = io.input.valid && io.input.ready

  val y0Dc = io.input.bits.y.coefficients(0)
  val y1Dc = io.input.bits.y1.coefficients(0)
  val y2Dc = io.input.bits.y2.coefficients(0)
  val y3Dc = io.input.bits.y3.coefficients(0)

  for ((blockEncoder, index) <- blockEncoders.zipWithIndex) {
    val initialBlock = index match {
      case 0 => io.input.bits.y
      case 1 => Mux(subsampledInput, io.input.bits.y1, io.input.bits.cb)
      case _ => Mux(subsampledInput, io.input.bits.y2, io.input.bits.cr)
    }
    val initialPrevious = index match {
      case 0 => io.previousDc(0)
      case 1 => Mux(subsampledInput, y0Dc, io.previousDc(1))
      case _ => Mux(subsampledInput, y1Dc, io.previousDc(2))
    }
    val reloadValid = active && reloadPending(index)

    blockEncoder.io.input.valid := inputFire || reloadValid
    blockEncoder.io.input.bits := Mux(reloadValid, deferredBlocks(index), initialBlock)
    blockEncoder.io.previousDc := Mux(reloadValid, deferredPreviousDc(index), initialPrevious)
    blockEncoder.io.isLuminance := Mux(
      reloadValid,
      deferredIsLuminance(index),
      index.U === 0.U || subsampledInput)

    when(reloadValid && blockEncoder.io.input.ready) {
      reloadPending(index) := false.B
    }
  }

  when(inputFire) {
    active := true.B
    activeSubsampled := subsampledInput
    blockCount := inputBlockCount
    drainBlock := 0.U
    reloadPending.foreach(_ := false.B)

    deferredBlocks(0) := io.input.bits.y3
    deferredBlocks(1) := io.input.bits.cb
    deferredBlocks(2) := io.input.bits.cr
    deferredPreviousDc(0) := y2Dc
    deferredPreviousDc(1) := io.previousDc(1)
    deferredPreviousDc(2) := io.previousDc(2)
    deferredIsLuminance(0) := true.B
    deferredIsLuminance(1) := false.B
    deferredIsLuminance(2) := false.B

    nextDc(0) := Mux(subsampledInput, y3Dc, y0Dc)
    nextDc(1) := io.input.bits.cb.coefficients(0)
    nextDc(2) := io.input.bits.cr.coefficients(0)
  }

  val drainSlot = Mux(drainBlock >= EncoderSlots.U, drainBlock - EncoderSlots.U, drainBlock)(1, 0)
  for ((blockEncoder, index) <- blockEncoders.zipWithIndex) {
    blockEncoder.io.output.ready := active && drainSlot === index.U && io.output.ready
  }

  val selectedOutputValid = MuxLookup(drainSlot, false.B)(
    blockEncoders.zipWithIndex.map { case (encoder, index) => index.U -> encoder.io.output.valid })
  val selectedOutputBits = MuxLookup(drainSlot, blockEncoders.head.io.output.bits)(
    blockEncoders.zipWithIndex.map { case (encoder, index) => index.U -> encoder.io.output.bits })
  val selectedDone = MuxLookup(drainSlot, false.B)(
    blockEncoders.zipWithIndex.map { case (encoder, index) => index.U -> encoder.io.done })

  io.output.valid := active && selectedOutputValid
  io.output.bits := selectedOutputBits
  val finalBlockDone = active && selectedDone && drainBlock === blockCount - 1.U
  io.done := finalBlockDone

  when(active && selectedDone) {
    when(activeSubsampled && drainBlock < EncoderSlots.U) {
      reloadPending(drainSlot) := true.B
    }
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

/** Two-entry ordered MCU entropy pipeline.
  *
  * Each entry owns one three-slot block scanner. The second MCU can therefore
  * scan while the first MCU's runs drain into the shared bit packer. Runs and
  * completion metadata always retire in input order. DC predictors advance at
  * input acceptance from the raw DC coefficients; `seedPreviousDc` is used
  * whenever the pipeline is empty, including frame and restart boundaries.
  */
class JpegPipelinedMcuEntropyStage(coefficientBits: Int = 16) extends Module {
  private val EngineCount = 2

  val io = IO(new Bundle {
    val input = Flipped(Decoupled(new ZigZagMinimumCodedUnitPacket(coefficientBits)))
    val seedPreviousDc = Input(Vec(HjpegConstants.Components, SInt(coefficientBits.W)))
    val output = Decoupled(new JpegBitRun(32))
    val completed = Output(Bool())
    val completedLast = Output(Bool())
    val completedNextDc = Output(Vec(HjpegConstants.Components, SInt(coefficientBits.W)))
    val busy = Output(Bool())
  })

  val engines = Seq.fill(EngineCount)(Module(new JpegParallelMcuEntropyStage(coefficientBits)))
  val occupied = RegInit(VecInit(Seq.fill(EngineCount)(false.B)))
  val slotLast = Reg(Vec(EngineCount, Bool()))
  val enqueueIndex = RegInit(0.U(1.W))
  val drainIndex = RegInit(0.U(1.W))
  val count = RegInit(0.U(2.W))
  val enqueuePreviousDc = Reg(Vec(HjpegConstants.Components, SInt(coefficientBits.W)))

  val targetReady = Mux(enqueueIndex === 0.U, engines(0).io.input.ready, engines(1).io.input.ready)
  val targetOccupied = occupied(enqueueIndex)
  io.input.ready := count =/= EngineCount.U && !targetOccupied && targetReady
  val inputFire = io.input.fire
  val previousDcForInput = Wire(Vec(HjpegConstants.Components, SInt(coefficientBits.W)))
  previousDcForInput := Mux(count === 0.U, io.seedPreviousDc, enqueuePreviousDc)

  for ((engine, index) <- engines.zipWithIndex) {
    engine.io.input.valid := io.input.valid && count =/= EngineCount.U &&
      !targetOccupied && enqueueIndex === index.U
    engine.io.input.bits := io.input.bits.mcu
    engine.io.previousDc := previousDcForInput
  }

  val inputSubsampled = io.input.bits.mcu.yBlockCount === 4.U
  val inputNextDc = Wire(Vec(HjpegConstants.Components, SInt(coefficientBits.W)))
  inputNextDc(0) := Mux(
    inputSubsampled,
    io.input.bits.mcu.y3.coefficients(0),
    io.input.bits.mcu.y.coefficients(0))
  inputNextDc(1) := io.input.bits.mcu.cb.coefficients(0)
  inputNextDc(2) := io.input.bits.mcu.cr.coefficients(0)

  val selectedOutputValid = Mux(drainIndex === 0.U, engines(0).io.output.valid, engines(1).io.output.valid)
  val selectedOutputBits = Mux(drainIndex === 0.U, engines(0).io.output.bits, engines(1).io.output.bits)
  val selectedDone = Mux(drainIndex === 0.U, engines(0).io.done, engines(1).io.done)
  val selectedNextDc = Wire(Vec(HjpegConstants.Components, SInt(coefficientBits.W)))
  selectedNextDc := Mux(drainIndex === 0.U, engines(0).io.nextDc, engines(1).io.nextDc)

  for ((engine, index) <- engines.zipWithIndex) {
    engine.io.output.ready := count =/= 0.U && drainIndex === index.U && io.output.ready
  }
  io.output.valid := count =/= 0.U && selectedOutputValid
  io.output.bits := selectedOutputBits
  io.completed := count =/= 0.U && selectedDone
  io.completedLast := slotLast(drainIndex)
  io.completedNextDc := selectedNextDc
  io.busy := count =/= 0.U

  when(inputFire) {
    occupied(enqueueIndex) := true.B
    slotLast(enqueueIndex) := io.input.bits.last
    enqueueIndex := enqueueIndex ^ 1.U
    enqueuePreviousDc := inputNextDc
  }
  when(io.completed) {
    occupied(drainIndex) := false.B
    drainIndex := drainIndex ^ 1.U
  }

  switch(Cat(inputFire, io.completed)) {
    is("b10".U) { count := count + 1.U }
    is("b01".U) { count := count - 1.U }
  }
}
