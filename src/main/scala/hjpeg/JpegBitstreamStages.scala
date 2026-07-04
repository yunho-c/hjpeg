// See README.md for license details.

package hjpeg

import chisel3._
import chisel3.util._

/** Concatenates an entropy token's Huffman code and amplitude payload.
  *
  * Codes are right-aligned in the low `length` bits and emitted MSB-first by the
  * byte packer.
  */
class JpegEntropyTokenBitsStage(maxBits: Int = 32, amplitudeBits: Int = 16) extends Module {
  val io = IO(new Bundle {
    val token = Input(new JpegEntropyToken(HjpegConstants.MaxHuffmanCodeBits, amplitudeBits))
    val run = Output(new JpegBitRun(maxBits))
  })

  val totalLength = io.token.huffmanLength + io.token.amplitudeLength
  val combined = (io.token.huffmanCode << io.token.amplitudeLength) | io.token.amplitude

  io.run.bits := combined(maxBits - 1, 0)
  io.run.length := totalLength
}

/** Packs variable-length entropy bit runs into bytes and applies JPEG stuffing.
  *
  * Input bit runs are right-aligned and consumed MSB-first. When `flush` is
  * asserted and fewer than eight bits are pending, the final byte is padded with
  * one bits, matching baseline JPEG entropy-segment fill behavior.
  *
  * This first implementation is intentionally conservative: it only accepts a
  * new run when it is not currently emitting a byte.
  */
class JpegBitRunPacker(maxRunBits: Int = 32, bufferBits: Int = 64) extends Module {
  require(bufferBits >= maxRunBits + 8, "bufferBits must hold one run plus one byte")

  val io = IO(new Bundle {
    val input = Flipped(Decoupled(new JpegBitRun(maxRunBits)))
    val flush = Input(Bool())
    val output = Decoupled(new EncodedByte(HjpegConfig()))
    val idle = Output(Bool())
  })

  val bitBuffer = RegInit(0.U(bufferBits.W))
  val bitCount = RegInit(0.U(7.W))
  val stuffingPending = RegInit(false.B)

  val canEmitFullByte = bitCount >= 8.U
  val canEmitFlushByte = io.flush && bitCount > 0.U && bitCount < 8.U
  val emittingDataByte = canEmitFullByte || canEmitFlushByte
  val outputByte = Wire(UInt(8.W))
  val outputLast = Wire(Bool())

  val fullShift = bitCount - 8.U
  val fullByte = (bitBuffer >> fullShift)(7, 0)
  val flushPadCount = 8.U - bitCount
  val flushPadMask = (1.U(8.W) << flushPadCount) - 1.U
  val flushByte = ((bitBuffer << flushPadCount) | flushPadMask)(7, 0)

  outputByte := Mux(stuffingPending, 0.U, Mux(canEmitFullByte, fullByte, flushByte))
  outputLast := canEmitFlushByte && !stuffingPending && outputByte =/= 0xff.U

  io.output.valid := stuffingPending || emittingDataByte
  io.output.bits.byte := outputByte
  io.output.bits.last := outputLast
  io.input.ready := !io.output.valid
  io.idle := !stuffingPending && bitCount === 0.U && !io.input.valid

  when(io.output.fire) {
    when(stuffingPending) {
      stuffingPending := false.B
    }.otherwise {
      when(outputByte === 0xff.U) {
        stuffingPending := true.B
      }

      when(canEmitFullByte) {
        val remainingCount = bitCount - 8.U
        val keepMask = (1.U(bufferBits.W) << remainingCount) - 1.U
        bitBuffer := bitBuffer & keepMask
        bitCount := remainingCount
      }.otherwise {
        bitBuffer := 0.U
        bitCount := 0.U
      }
    }
  }.elsewhen(io.input.fire) {
    bitBuffer := (bitBuffer << io.input.bits.length) | io.input.bits.bits
    bitCount := bitCount + io.input.bits.length
  }
}
