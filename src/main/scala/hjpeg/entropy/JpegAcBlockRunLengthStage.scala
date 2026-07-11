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

  val block = Reg(Vec(HjpegConstants.BlockSize, SInt(coefficientBits.W)))
  val scanIndex = RegInit(1.U(6.W))
  val zeroRun = RegInit(0.U(4.W))
  val scanning = RegInit(false.B)

  io.input.ready := !scanning

  when(io.input.fire) {
    for (index <- 0 until HjpegConstants.BlockSize) {
      block(index) := io.input.bits.coefficients(index)
    }
    scanIndex := 1.U
    zeroRun := 0.U
    scanning := true.B
  }

  val current = block(scanIndex)
  val currentNonzero = current =/= 0.S
  val remainingHasNonzero = (1 until HjpegConstants.BlockSize)
    .map(index => scanIndex <= index.U && block(index) =/= 0.S)
    .reduce(_ || _)
  val atLast = scanIndex === (HjpegConstants.BlockSize - 1).U
  val emitEndOfBlock = !currentNonzero && !remainingHasNonzero
  val emitZeroRunLength = !currentNonzero && remainingHasNonzero && zeroRun === 15.U
  val emitCoefficient = currentNonzero
  val emitEvent = emitEndOfBlock || emitZeroRunLength || emitCoefficient

  io.output.valid := scanning && emitEvent
  io.output.bits.runLength := Mux(emitCoefficient, zeroRun, 0.U)
  io.output.bits.coefficient := Mux(emitCoefficient, current, 0.S)
  io.output.bits.emitEndOfBlock := emitEndOfBlock
  io.output.bits.emitZeroRunLength := emitZeroRunLength
  io.busy := scanning

  when(scanning) {
    when(emitEvent) {
      when(io.output.fire) {
        when(emitEndOfBlock || (emitCoefficient && atLast)) {
          scanning := false.B
          scanIndex := 1.U
          zeroRun := 0.U
        }.otherwise {
          scanIndex := scanIndex + 1.U
          zeroRun := 0.U
        }
      }
    }.otherwise {
      scanIndex := scanIndex + 1.U
      zeroRun := zeroRun + 1.U
    }
  }
}
