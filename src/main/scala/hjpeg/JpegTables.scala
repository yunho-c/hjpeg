// See README.md for license details.

package hjpeg

import chisel3._

object JpegTables {
  private def canonicalCodeTable(bitsByLength: Seq[Int], symbols: Seq[Int]): Map[Int, (Int, Int)] = {
    require(bitsByLength.length == HjpegConstants.MaxHuffmanCodeBits)
    var code = 0
    var symbolIndex = 0
    var table = Map.empty[Int, (Int, Int)]

    for (length <- 1 to HjpegConstants.MaxHuffmanCodeBits) {
      for (_ <- 0 until bitsByLength(length - 1)) {
        table += symbols(symbolIndex) -> (code, length)
        code += 1
        symbolIndex += 1
      }
      code = code << 1
    }

    require(symbolIndex == symbols.length)
    table
  }

  val ZigZagOrder: Seq[Int] = Seq(
    0, 1, 8, 16, 9, 2, 3, 10,
    17, 24, 32, 25, 18, 11, 4, 5,
    12, 19, 26, 33, 40, 48, 41, 34,
    27, 20, 13, 6, 7, 14, 21, 28,
    35, 42, 49, 56, 57, 50, 43, 36,
    29, 22, 15, 23, 30, 37, 44, 51,
    58, 59, 52, 45, 38, 31, 39, 46,
    53, 60, 61, 54, 47, 55, 62, 63
  )

  val StandardLuminanceQuant: Seq[Int] = Seq(
    16, 11, 10, 16, 24, 40, 51, 61,
    12, 12, 14, 19, 26, 58, 60, 55,
    14, 13, 16, 24, 40, 57, 69, 56,
    14, 17, 22, 29, 51, 87, 80, 62,
    18, 22, 37, 56, 68, 109, 103, 77,
    24, 35, 55, 64, 81, 104, 113, 92,
    49, 64, 78, 87, 103, 121, 120, 101,
    72, 92, 95, 98, 112, 100, 103, 99
  )

  val StandardChrominanceQuant: Seq[Int] = Seq(
    17, 18, 24, 47, 99, 99, 99, 99,
    18, 21, 26, 66, 99, 99, 99, 99,
    24, 26, 56, 99, 99, 99, 99, 99,
    47, 66, 99, 99, 99, 99, 99, 99,
    99, 99, 99, 99, 99, 99, 99, 99,
    99, 99, 99, 99, 99, 99, 99, 99,
    99, 99, 99, 99, 99, 99, 99, 99,
    99, 99, 99, 99, 99, 99, 99, 99
  )

  val StandardDcLuminanceBits: Seq[Int] = Seq(0, 1, 5, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0)
  val StandardDcChrominanceBits: Seq[Int] = Seq(0, 3, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0)
  private val DcSymbols = 0 to HjpegConstants.MaxBaselineDcCategory
  val StandardAcLuminanceBits: Seq[Int] = Seq(0, 2, 1, 3, 3, 2, 4, 3, 5, 5, 4, 4, 0, 0, 1, 125)
  val StandardAcChrominanceBits: Seq[Int] = Seq(0, 2, 1, 2, 4, 4, 3, 4, 7, 5, 4, 4, 0, 1, 2, 119)
  val StandardAcLuminanceSymbols: Seq[Int] = Seq(
    0x01, 0x02, 0x03, 0x00, 0x04, 0x11, 0x05, 0x12,
    0x21, 0x31, 0x41, 0x06, 0x13, 0x51, 0x61, 0x07,
    0x22, 0x71, 0x14, 0x32, 0x81, 0x91, 0xa1, 0x08,
    0x23, 0x42, 0xb1, 0xc1, 0x15, 0x52, 0xd1, 0xf0,
    0x24, 0x33, 0x62, 0x72, 0x82, 0x09, 0x0a, 0x16,
    0x17, 0x18, 0x19, 0x1a, 0x25, 0x26, 0x27, 0x28,
    0x29, 0x2a, 0x34, 0x35, 0x36, 0x37, 0x38, 0x39,
    0x3a, 0x43, 0x44, 0x45, 0x46, 0x47, 0x48, 0x49,
    0x4a, 0x53, 0x54, 0x55, 0x56, 0x57, 0x58, 0x59,
    0x5a, 0x63, 0x64, 0x65, 0x66, 0x67, 0x68, 0x69,
    0x6a, 0x73, 0x74, 0x75, 0x76, 0x77, 0x78, 0x79,
    0x7a, 0x83, 0x84, 0x85, 0x86, 0x87, 0x88, 0x89,
    0x8a, 0x92, 0x93, 0x94, 0x95, 0x96, 0x97, 0x98,
    0x99, 0x9a, 0xa2, 0xa3, 0xa4, 0xa5, 0xa6, 0xa7,
    0xa8, 0xa9, 0xaa, 0xb2, 0xb3, 0xb4, 0xb5, 0xb6,
    0xb7, 0xb8, 0xb9, 0xba, 0xc2, 0xc3, 0xc4, 0xc5,
    0xc6, 0xc7, 0xc8, 0xc9, 0xca, 0xd2, 0xd3, 0xd4,
    0xd5, 0xd6, 0xd7, 0xd8, 0xd9, 0xda, 0xe1, 0xe2,
    0xe3, 0xe4, 0xe5, 0xe6, 0xe7, 0xe8, 0xe9, 0xea,
    0xf1, 0xf2, 0xf3, 0xf4, 0xf5, 0xf6, 0xf7, 0xf8,
    0xf9, 0xfa
  )
  val StandardAcChrominanceSymbols: Seq[Int] = Seq(
    0x00, 0x01, 0x02, 0x03, 0x11, 0x04, 0x05, 0x21,
    0x31, 0x06, 0x12, 0x41, 0x51, 0x07, 0x61, 0x71,
    0x13, 0x22, 0x32, 0x81, 0x08, 0x14, 0x42, 0x91,
    0xa1, 0xb1, 0xc1, 0x09, 0x23, 0x33, 0x52, 0xf0,
    0x15, 0x62, 0x72, 0xd1, 0x0a, 0x16, 0x24, 0x34,
    0xe1, 0x25, 0xf1, 0x17, 0x18, 0x19, 0x1a, 0x26,
    0x27, 0x28, 0x29, 0x2a, 0x35, 0x36, 0x37, 0x38,
    0x39, 0x3a, 0x43, 0x44, 0x45, 0x46, 0x47, 0x48,
    0x49, 0x4a, 0x53, 0x54, 0x55, 0x56, 0x57, 0x58,
    0x59, 0x5a, 0x63, 0x64, 0x65, 0x66, 0x67, 0x68,
    0x69, 0x6a, 0x73, 0x74, 0x75, 0x76, 0x77, 0x78,
    0x79, 0x7a, 0x82, 0x83, 0x84, 0x85, 0x86, 0x87,
    0x88, 0x89, 0x8a, 0x92, 0x93, 0x94, 0x95, 0x96,
    0x97, 0x98, 0x99, 0x9a, 0xa2, 0xa3, 0xa4, 0xa5,
    0xa6, 0xa7, 0xa8, 0xa9, 0xaa, 0xb2, 0xb3, 0xb4,
    0xb5, 0xb6, 0xb7, 0xb8, 0xb9, 0xba, 0xc2, 0xc3,
    0xc4, 0xc5, 0xc6, 0xc7, 0xc8, 0xc9, 0xca, 0xd2,
    0xd3, 0xd4, 0xd5, 0xd6, 0xd7, 0xd8, 0xd9, 0xda,
    0xe2, 0xe3, 0xe4, 0xe5, 0xe6, 0xe7, 0xe8, 0xe9,
    0xea, 0xf2, 0xf3, 0xf4, 0xf5, 0xf6, 0xf7, 0xf8,
    0xf9, 0xfa
  )

  val StandardDcLuminanceCodes: Seq[(Int, Int)] = {
    val table = canonicalCodeTable(StandardDcLuminanceBits, DcSymbols)
    DcSymbols.map(symbol => table(symbol))
  }

  val StandardDcChrominanceCodes: Seq[(Int, Int)] = {
    val table = canonicalCodeTable(StandardDcChrominanceBits, DcSymbols)
    DcSymbols.map(symbol => table(symbol))
  }

  val StandardAcLuminanceCodesBySymbol: Seq[(Int, Int)] = {
    val table = canonicalCodeTable(StandardAcLuminanceBits, StandardAcLuminanceSymbols)
    (0 until 256).map(symbol => table.getOrElse(symbol, (0, 0)))
  }

  val StandardAcChrominanceCodesBySymbol: Seq[(Int, Int)] = {
    val table = canonicalCodeTable(StandardAcChrominanceBits, StandardAcChrominanceSymbols)
    (0 until 256).map(symbol => table.getOrElse(symbol, (0, 0)))
  }
}

/** Returns one quality-scaled baseline JPEG quantization table entry.
  *
  * `index` addresses the table in natural raster order. `quality` is clamped to
  * `[1, 100]` and follows the common libjpeg scaling rule used by the optional
  * Python reference.
  */
class JpegQuantTableValue extends Module {
  val io = IO(new Bundle {
    val quality = Input(UInt(7.W))
    val isLuminance = Input(Bool())
    val index = Input(UInt(6.W))
    val value = Output(UInt(8.W))
  })

  private val luminance = VecInit(JpegTables.StandardLuminanceQuant.map(_.U(8.W)))
  private val chrominance = VecInit(JpegTables.StandardChrominanceQuant.map(_.U(8.W)))

  val clampedQuality = Mux(io.quality === 0.U, 1.U, Mux(io.quality > 100.U, 100.U, io.quality))
  val qualityScale = Mux(clampedQuality < 50.U, 5000.U / clampedQuality, 200.U - (clampedQuality << 1))
  val base = Mux(io.isLuminance, luminance(io.index), chrominance(io.index))
  val scaled = ((base * qualityScale) + 50.U) / 100.U

  io.value := Mux(scaled === 0.U, 1.U, Mux(scaled > 255.U, 255.U, scaled(7, 0)))
}

/** Maps a zig-zag scan position to its natural raster-order coefficient index. */
class JpegZigZagIndex extends Module {
  val io = IO(new Bundle {
    val scanIndex = Input(UInt(6.W))
    val rasterIndex = Output(UInt(6.W))
  })

  private val zigZag = VecInit(JpegTables.ZigZagOrder.map(_.U(6.W)))
  io.rasterIndex := zigZag(io.scanIndex)
}

/** Computes JPEG magnitude category and amplitude bits for a signed value. */
class JpegMagnitudeValue(valueBits: Int = 16) extends Module {
  val io = IO(new Bundle {
    val value = Input(SInt(valueBits.W))
    val category = Output(UInt(5.W))
    val amplitude = Output(UInt(valueBits.W))
  })

  val negative = io.value < 0.S
  val magnitude = Mux(negative, -io.value, io.value).asUInt
  val category = Wire(UInt(5.W))
  category := 0.U
  for (bits <- 1 until valueBits) {
    when(magnitude >= (BigInt(1) << (bits - 1)).U && magnitude < (BigInt(1) << bits).U) {
      category := bits.U
    }
  }

  val mask = (1.U(valueBits.W) << category) - 1.U
  io.category := category
  io.amplitude := Mux(category === 0.U, 0.U, Mux(negative, mask - magnitude, magnitude))
}

/** Looks up the default baseline JPEG DC Huffman code for a magnitude category. */
class JpegDcHuffmanCode extends Module {
  val io = IO(new Bundle {
    val isLuminance = Input(Bool())
    val category = Input(UInt(5.W))
    val code = Output(UInt(HjpegConstants.MaxHuffmanCodeBits.W))
    val length = Output(UInt(5.W))
    val valid = Output(Bool())
  })

  private def vecFrom(codes: Seq[(Int, Int)]): (Vec[UInt], Vec[UInt]) = {
    val padded = codes ++ Seq.fill(16 - codes.length)((0, 0))
    (
      VecInit(padded.map { case (code, _) => code.U(HjpegConstants.MaxHuffmanCodeBits.W) }),
      VecInit(padded.map { case (_, length) => length.U(5.W) })
    )
  }

  private val (luminanceCodes, luminanceLengths) = vecFrom(JpegTables.StandardDcLuminanceCodes)
  private val (chrominanceCodes, chrominanceLengths) = vecFrom(JpegTables.StandardDcChrominanceCodes)
  val tableIndex = io.category(3, 0)
  val inRange = io.category <= HjpegConstants.MaxBaselineDcCategory.U

  io.code := Mux(io.isLuminance, luminanceCodes(tableIndex), chrominanceCodes(tableIndex))
  io.length := Mux(io.isLuminance, luminanceLengths(tableIndex), chrominanceLengths(tableIndex))
  io.valid := inRange
}

/** Looks up the default baseline JPEG AC Huffman code for an 8-bit AC symbol. */
class JpegAcHuffmanCode extends Module {
  val io = IO(new Bundle {
    val isLuminance = Input(Bool())
    val symbol = Input(UInt(8.W))
    val code = Output(UInt(HjpegConstants.MaxHuffmanCodeBits.W))
    val length = Output(UInt(5.W))
    val valid = Output(Bool())
  })

  private def vecFrom(codes: Seq[(Int, Int)]): (Vec[UInt], Vec[UInt]) =
    (
      VecInit(codes.map { case (code, _) => code.U(HjpegConstants.MaxHuffmanCodeBits.W) }),
      VecInit(codes.map { case (_, length) => length.U(5.W) })
    )

  private val (luminanceCodes, luminanceLengths) = vecFrom(JpegTables.StandardAcLuminanceCodesBySymbol)
  private val (chrominanceCodes, chrominanceLengths) = vecFrom(JpegTables.StandardAcChrominanceCodesBySymbol)

  io.code := Mux(io.isLuminance, luminanceCodes(io.symbol), chrominanceCodes(io.symbol))
  io.length := Mux(io.isLuminance, luminanceLengths(io.symbol), chrominanceLengths(io.symbol))
  io.valid := io.length =/= 0.U
}
