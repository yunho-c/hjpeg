// See README.md for license details.

package hjpeg

import chisel3._

/** Encodes one baseline JPEG DC coefficient delta into Huffman and amplitude bits.
  *
  * JPEG DC coefficients are differentially coded per component. The output token
  * keeps the Huffman prefix and amplitude payload separate so a later bit-packer
  * can append them in order.
  */
class JpegDcEncodeStage(coefficientBits: Int = 16) extends Module {
  val differenceBits = coefficientBits + 1

  val io = IO(new Bundle {
    val current = Input(SInt(coefficientBits.W))
    val previous = Input(SInt(coefficientBits.W))
    val isLuminance = Input(Bool())
    val difference = Output(SInt(differenceBits.W))
    val token = Output(new JpegEntropyToken(HjpegConstants.MaxHuffmanCodeBits, differenceBits))
    val valid = Output(Bool())
  })

  val difference = Wire(SInt(differenceBits.W))
  difference := io.current - io.previous

  val magnitude = Module(new JpegMagnitudeValue(differenceBits))
  magnitude.io.value := difference

  val huffman = Module(new JpegDcHuffmanCode())
  huffman.io.isLuminance := io.isLuminance
  huffman.io.category := magnitude.io.category

  io.difference := difference
  io.token.huffmanCode := huffman.io.code
  io.token.huffmanLength := huffman.io.length
  io.token.amplitude := magnitude.io.amplitude
  io.token.amplitudeLength := magnitude.io.category
  io.valid := huffman.io.valid
}
