// See README.md for license details.

package hjpeg

import chisel3._
import chisel3.util._

/** Encodes one zig-zag quantized block into JPEG entropy bit runs.
  *
  * The stage emits the DC difference token first, then AC run-length tokens.
  * Output bit runs are right-aligned and MSB-first, matching
  * `JpegBitRunPacker`.
  */
class JpegBlockEntropyStage(coefficientBits: Int = 16) extends Module {
  val io = IO(new Bundle {
    val input = Flipped(Decoupled(new ZigZagCoefficientBlock(coefficientBits)))
    val previousDc = Input(SInt(coefficientBits.W))
    val isLuminance = Input(Bool())
    val output = Decoupled(new JpegBitRun(32))
    val currentDc = Output(SInt(coefficientBits.W))
    val busy = Output(Bool())
  })

  val sIdle :: sDc :: sLoadAc :: sAc :: Nil = Enum(4)
  val state = RegInit(sIdle)
  val block = Reg(Vec(HjpegConstants.BlockSize, SInt(coefficientBits.W)))
  val previousDc = RegInit(0.S(coefficientBits.W))
  val isLuminance = RegInit(true.B)

  io.input.ready := state === sIdle
  when(io.input.fire) {
    for (index <- 0 until HjpegConstants.BlockSize) {
      block(index) := io.input.bits.coefficients(index)
    }
    previousDc := io.previousDc
    isLuminance := io.isLuminance
    state := sDc
  }

  val dcEncode = Module(new JpegDcEncodeStage(coefficientBits))
  dcEncode.io.current := block(0)
  dcEncode.io.previous := previousDc
  dcEncode.io.isLuminance := isLuminance

  val dcBits = Module(new JpegEntropyTokenBitsStage(32, coefficientBits + 1))
  dcBits.io.token := dcEncode.io.token

  val acScanner = Module(new JpegAcBlockRunLengthStage(coefficientBits))
  acScanner.io.input.valid := state === sLoadAc
  for (index <- 0 until HjpegConstants.BlockSize) {
    acScanner.io.input.bits.coefficients(index) := block(index)
  }

  val acEncode = Module(new JpegAcEncodeStage(coefficientBits))
  acEncode.io.runLength := acScanner.io.output.bits.runLength
  acEncode.io.coefficient := acScanner.io.output.bits.coefficient
  acEncode.io.emitEndOfBlock := acScanner.io.output.bits.emitEndOfBlock
  acEncode.io.emitZeroRunLength := acScanner.io.output.bits.emitZeroRunLength
  acEncode.io.isLuminance := isLuminance

  val acBits = Module(new JpegEntropyTokenBitsStage(32, coefficientBits))
  acBits.io.token := acEncode.io.token

  val dcOutputValid = state === sDc && dcEncode.io.valid
  val acOutputValid = state === sAc && acScanner.io.output.valid && acEncode.io.valid
  io.output.valid := dcOutputValid || acOutputValid
  io.output.bits := Mux(dcOutputValid, dcBits.io.run, acBits.io.run)

  acScanner.io.output.ready := state === sAc && io.output.ready && acEncode.io.valid

  when(state === sDc && io.output.fire) {
    state := sLoadAc
  }.elsewhen(state === sLoadAc && acScanner.io.input.fire) {
    state := sAc
  }.elsewhen(state === sAc && !acScanner.io.busy && !acScanner.io.output.valid) {
    state := sIdle
  }

  io.currentDc := block(0)
  io.busy := state =/= sIdle
}
