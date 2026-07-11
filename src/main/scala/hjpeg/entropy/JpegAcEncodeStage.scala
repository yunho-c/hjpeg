// See README.md for license details.

package hjpeg

import chisel3._
import chisel3.util.Cat

/** Encodes one baseline JPEG AC event into Huffman and amplitude bits.
  *
  * `emitEndOfBlock` selects symbol `0x00`. `emitZeroRunLength` selects symbol
  * `0xf0`. Otherwise the symbol is `{runLength, magnitudeCategory}` and the
  * coefficient contributes amplitude bits.
  */
class JpegAcEncodeStage(coefficientBits: Int = 16) extends Module {
  val io = IO(new Bundle {
    val runLength = Input(UInt(4.W))
    val coefficient = Input(SInt(coefficientBits.W))
    val emitEndOfBlock = Input(Bool())
    val emitZeroRunLength = Input(Bool())
    val isLuminance = Input(Bool())
    val symbol = Output(UInt(8.W))
    val token = Output(new JpegEntropyToken(HjpegConstants.MaxHuffmanCodeBits, coefficientBits))
    val valid = Output(Bool())
  })

  val magnitude = Module(new JpegMagnitudeValue(coefficientBits))
  magnitude.io.value := io.coefficient

  val coefficientSymbol = Cat(io.runLength, magnitude.io.category(3, 0))
  io.symbol := Mux(io.emitEndOfBlock, 0x00.U, Mux(io.emitZeroRunLength, 0xf0.U, coefficientSymbol))

  val huffman = Module(new JpegAcHuffmanCode())
  huffman.io.isLuminance := io.isLuminance
  huffman.io.symbol := io.symbol

  io.token.huffmanCode := huffman.io.code
  io.token.huffmanLength := huffman.io.length
  io.token.amplitude := Mux(io.emitEndOfBlock || io.emitZeroRunLength, 0.U, magnitude.io.amplitude)
  io.token.amplitudeLength := Mux(io.emitEndOfBlock || io.emitZeroRunLength, 0.U, magnitude.io.category)
  io.valid := huffman.io.valid && (io.emitEndOfBlock || io.emitZeroRunLength || magnitude.io.category =/= 0.U)
}
