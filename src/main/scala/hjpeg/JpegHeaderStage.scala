// See README.md for license details.

package hjpeg

import chisel3._
import chisel3.util._

object JpegHeaderBytes {
  private def segment(marker: Int, payload: Seq[Int]): Seq[Int] = {
    val length = payload.length + 2
    Seq(0xff, marker, (length >> 8) & 0xff, length & 0xff) ++ payload
  }

  private def dhtSegment(tableInfo: Int, bits: Seq[Int], symbols: Seq[Int]): Seq[Int] =
    segment(0xc4, Seq(tableInfo) ++ bits ++ symbols)

  val Soi: Seq[Int] = Seq(0xff, 0xd8)
  val App0: Seq[Int] = segment(
    0xe0,
    Seq(
      0x4a, 0x46, 0x49, 0x46, 0x00, // JFIF\0
      0x01, 0x01, 0x00,             // version 1.1, no density units
      0x00, 0x01, 0x00, 0x01,       // X/Y density
      0x00, 0x00                    // thumbnail size
    )
  )
  val DqtLuminancePrefix: Seq[Int] = segment(0xdb, Seq(0x00) ++ Seq.fill(HjpegConstants.BlockSize)(0))
  val DqtChrominancePrefix: Seq[Int] = segment(0xdb, Seq(0x01) ++ Seq.fill(HjpegConstants.BlockSize)(0))
  val Sof0Prefix: Seq[Int] = segment(
    0xc0,
    Seq(
      0x08, 0x00, 0x00, 0x00, 0x00, // precision, height, width
      0x03,
      0x01, 0x11, 0x00,
      0x02, 0x11, 0x01,
      0x03, 0x11, 0x01
    )
  )
  val Dht: Seq[Int] =
    dhtSegment(0x00, JpegTables.StandardDcLuminanceBits, 0 to HjpegConstants.MaxBaselineDcCategory) ++
      dhtSegment(0x01, JpegTables.StandardDcChrominanceBits, 0 to HjpegConstants.MaxBaselineDcCategory) ++
      dhtSegment(0x10, JpegTables.StandardAcLuminanceBits, JpegTables.StandardAcLuminanceSymbols) ++
      dhtSegment(0x11, JpegTables.StandardAcChrominanceBits, JpegTables.StandardAcChrominanceSymbols)
  val Sos: Seq[Int] = segment(
    0xda,
    Seq(
      0x03,
      0x01, 0x00,
      0x02, 0x11,
      0x03, 0x11,
      0x00, 0x3f, 0x00
    )
  )

  val DqtLuminanceDataStart: Int = Soi.length + App0.length + 5
  val DqtChrominanceDataStart: Int = Soi.length + App0.length + DqtLuminancePrefix.length + 5
  val Sof0Start: Int = Soi.length + App0.length + DqtLuminancePrefix.length + DqtChrominancePrefix.length
  val Sof0HeightHigh: Int = Sof0Start + 5
  val Sof0HeightLow: Int = Sof0Start + 6
  val Sof0WidthHigh: Int = Sof0Start + 7
  val Sof0WidthLow: Int = Sof0Start + 8
  val Sof0LuminanceSamplingFactor: Int = Sof0Start + 11
  val Header: Seq[Int] = Soi ++ App0 ++ DqtLuminancePrefix ++ DqtChrominancePrefix ++ Sof0Prefix ++ Dht ++ Sos
  val HeaderLength: Int = Header.length
}

/** Emits the baseline JPEG header through the start-of-scan marker.
  *
  * The scan entropy bytes and final EOI marker are emitted by downstream scan
  * assembly. Quantization tables are written in JPEG zig-zag order.
  */
class JpegHeaderStage extends Module {
  val io = IO(new Bundle {
    val config = Input(new FrameConfig(HjpegConfig()))
    val start = Input(Bool())
    val output = Decoupled(new EncodedByte(HjpegConfig()))
    val busy = Output(Bool())
    val done = Output(Bool())
  })

  val index = RegInit(0.U(log2Ceil(JpegHeaderBytes.HeaderLength).W))
  val active = RegInit(false.B)
  val staticBytes = VecInit(JpegHeaderBytes.Header.map(_.U(8.W)))
  val zigZag = VecInit(JpegTables.ZigZagOrder.map(_.U(6.W)))

  val inLuminanceDqt =
    index >= JpegHeaderBytes.DqtLuminanceDataStart.U &&
      index < (JpegHeaderBytes.DqtLuminanceDataStart + HjpegConstants.BlockSize).U
  val inChrominanceDqt =
    index >= JpegHeaderBytes.DqtChrominanceDataStart.U &&
      index < (JpegHeaderBytes.DqtChrominanceDataStart + HjpegConstants.BlockSize).U
  val luminanceQuantIndex = index - JpegHeaderBytes.DqtLuminanceDataStart.U
  val chrominanceQuantIndex = index - JpegHeaderBytes.DqtChrominanceDataStart.U
  val quantScanIndex = Mux(inLuminanceDqt, luminanceQuantIndex(5, 0), chrominanceQuantIndex(5, 0))
  val quantIndex = zigZag(quantScanIndex)

  val quantValue = Module(new JpegQuantTableValue())
  quantValue.io.quality := io.config.quality
  quantValue.io.isLuminance := inLuminanceDqt
  quantValue.io.index := quantIndex

  val heightHigh = io.config.ysize(15, 8)
  val heightLow = io.config.ysize(7, 0)
  val widthHigh = io.config.xsize(15, 8)
  val widthLow = io.config.xsize(7, 0)
  val dynamicByte = MuxCase(
    staticBytes(index),
    Seq(
      inLuminanceDqt -> quantValue.io.value,
      inChrominanceDqt -> quantValue.io.value,
      (index === JpegHeaderBytes.Sof0HeightHigh.U) -> heightHigh,
      (index === JpegHeaderBytes.Sof0HeightLow.U) -> heightLow,
      (index === JpegHeaderBytes.Sof0WidthHigh.U) -> widthHigh,
      (index === JpegHeaderBytes.Sof0WidthLow.U) -> widthLow,
      (index === JpegHeaderBytes.Sof0LuminanceSamplingFactor.U) ->
        Mux(io.config.enableChromaSubsample, 0x22.U, 0x11.U)
    )
  )

  when(io.start && !active) {
    active := true.B
    index := 0.U
  }

  io.output.valid := active
  io.output.bits.byte := dynamicByte
  io.output.bits.last := index === (JpegHeaderBytes.HeaderLength - 1).U
  io.busy := active
  io.done := io.output.fire && io.output.bits.last

  when(io.output.fire) {
    when(io.output.bits.last) {
      active := false.B
      index := 0.U
    }.otherwise {
      index := index + 1.U
    }
  }
}
