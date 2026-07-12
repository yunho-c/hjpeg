// See README.md for license details.

package hjpeg

import chisel3._
import chisel3.util._

/** Scans one zig-zag-ordered block into baseline JPEG AC run-length events.
  *
  * The DC coefficient at index 0 is ignored. The output stream emits:
  *
  * - coefficient events with the zero run preceding that coefficient,
  * - ZRL events for runs of 16 zeros before a later nonzero coefficient, and
  * - one EOB event when the remaining AC coefficients are all zero.
  */
class JpegAcBlockRunLengthStage(coefficientBits: Int = 16) extends Module {
  val io = IO(new Bundle {
    val input = Flipped(Decoupled(new ZigZagCoefficientBlock(coefficientBits)))
    val output = Decoupled(new JpegAcRunLengthEvent(coefficientBits))
    val busy = Output(Bool())
  })

  private val ScanLanes = 4

  val block = Reg(Vec(HjpegConstants.BlockSize, SInt(coefficientBits.W)))
  val scanIndex = RegInit(1.U(6.W))
  val zeroRun = RegInit(0.U(4.W))
  val hasAcNonzero = RegInit(false.B)
  val lastNonzeroIndex = RegInit(0.U(6.W))
  val scanning = RegInit(false.B)

  io.input.ready := !scanning

  val acNonzeroMask = VecInit(
    (1 until HjpegConstants.BlockSize).map(index => io.input.bits.coefficients(index) =/= 0.S)).asUInt

  when(io.input.fire) {
    for (index <- 0 until HjpegConstants.BlockSize) {
      block(index) := io.input.bits.coefficients(index)
    }
    scanIndex := 1.U
    zeroRun := 0.U
    hasAcNonzero := acNonzeroMask.orR
    lastNonzeroIndex := Mux(
      acNonzeroMask.orR,
      (HjpegConstants.BlockSize - 1).U - PriorityEncoder(Reverse(acNonzeroMask)),
      0.U)
    scanning := true.B
  }

  val eventRunLength = WireDefault(0.U(4.W))
  val eventCoefficient = WireDefault(0.S(coefficientBits.W))
  val eventEndOfBlock = WireDefault(false.B)
  val eventZeroRunLength = WireDefault(false.B)
  val eventFinishesBlock = WireDefault(false.B)

  var eventFound = false.B
  var nextZeroRun = zeroRun
  var scannedCount = 0.U(3.W)

  for (lane <- 0 until ScanLanes) {
    val candidateIndex = scanIndex +& lane.U
    val candidateInBlock = candidateIndex < HjpegConstants.BlockSize.U
    val candidate = block(candidateIndex(5, 0))
    val candidatePastLastNonzero = !hasAcNonzero || candidateIndex > lastNonzeroIndex
    val candidateActive = !eventFound && candidateInBlock
    val emitEndOfBlock = candidateActive && candidatePastLastNonzero
    val emitCoefficient = candidateActive && !candidatePastLastNonzero && candidate =/= 0.S
    val emitZeroRunLength =
      candidateActive && !candidatePastLastNonzero && candidate === 0.S && nextZeroRun === 15.U
    val emitEvent = emitEndOfBlock || emitCoefficient || emitZeroRunLength

    when(emitEvent) {
      eventRunLength := Mux(emitCoefficient, nextZeroRun, 0.U)
      eventCoefficient := Mux(emitCoefficient, candidate, 0.S)
      eventEndOfBlock := emitEndOfBlock
      eventZeroRunLength := emitZeroRunLength
      eventFinishesBlock :=
        emitEndOfBlock || (emitCoefficient && candidateIndex === (HjpegConstants.BlockSize - 1).U)
    }

    val consumedZero =
      candidateActive && !candidatePastLastNonzero && candidate === 0.S && !emitZeroRunLength
    nextZeroRun = Mux(emitEvent, 0.U, Mux(consumedZero, nextZeroRun + 1.U, nextZeroRun))
    scannedCount = Mux(candidateActive, (lane + 1).U, scannedCount)
    eventFound = eventFound || emitEvent
  }

  io.output.valid := scanning && eventFound
  io.output.bits.runLength := eventRunLength
  io.output.bits.coefficient := eventCoefficient
  io.output.bits.emitEndOfBlock := eventEndOfBlock
  io.output.bits.emitZeroRunLength := eventZeroRunLength
  io.busy := scanning

  when(scanning) {
    when(eventFound) {
      when(io.output.fire) {
        when(eventFinishesBlock) {
          scanning := false.B
          scanIndex := 1.U
          zeroRun := 0.U
        }.otherwise {
          scanIndex := scanIndex + scannedCount
          zeroRun := nextZeroRun
        }
      }
    }.otherwise {
      scanIndex := scanIndex + scannedCount
      zeroRun := nextZeroRun
    }
  }
}
