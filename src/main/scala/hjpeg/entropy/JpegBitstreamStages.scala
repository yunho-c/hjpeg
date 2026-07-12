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
  * A run may be accepted in the same cycle that an output byte is transferred.
  * The post-transfer capacity check keeps the buffer bounded, and a stalled
  * output always blocks the input so the visible byte remains stable.
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

  val outputTransfer = io.output.valid && io.output.ready
  val dataByteTransfer = outputTransfer && !stuffingPending
  val countAfterOutput = WireDefault(bitCount)
  val bufferAfterOutput = WireDefault(bitBuffer)

  when(dataByteTransfer) {
    when(canEmitFullByte) {
      val remainingCount = bitCount - 8.U
      val keepMask = (1.U(bufferBits.W) << remainingCount) - 1.U
      countAfterOutput := remainingCount
      bufferAfterOutput := bitBuffer & keepMask
    }.otherwise {
      countAfterOutput := 0.U
      bufferAfterOutput := 0.U
    }
  }

  val countWithInput = countAfterOutput +& io.input.bits.length
  val outputCanAdvance = !io.output.valid || io.output.ready
  io.input.ready := !io.flush && outputCanAdvance && countWithInput <= bufferBits.U
  io.idle := !stuffingPending && bitCount === 0.U && !io.input.valid

  when(outputTransfer) {
    when(stuffingPending) {
      stuffingPending := false.B
    }.otherwise {
      stuffingPending := outputByte === 0xff.U
    }
  }

  when(outputTransfer || io.input.fire) {
    bitBuffer := bufferAfterOutput
    bitCount := countAfterOutput

    when(io.input.fire) {
      val appended = (bufferAfterOutput << io.input.bits.length) | io.input.bits.bits
      bitBuffer := appended(bufferBits - 1, 0)
      bitCount := countWithInput
    }
  }
}
