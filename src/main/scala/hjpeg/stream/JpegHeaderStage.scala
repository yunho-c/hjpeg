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
  val Dri: Seq[Int] = Seq(0xff, 0xdd, 0x00, 0x04, 0x00, 0x00)
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
  val SoiLast: Int = Soi.length - 1
  val App0Start: Int = Soi.length
  val Sof0HeightHigh: Int = Sof0Start + 5
  val Sof0HeightLow: Int = Sof0Start + 6
  val Sof0WidthHigh: Int = Sof0Start + 7
  val Sof0WidthLow: Int = Sof0Start + 8
  val Sof0LuminanceSamplingFactor: Int = Sof0Start + 11
  val DriStart: Int = Soi.length + App0.length + DqtLuminancePrefix.length + DqtChrominancePrefix.length + Sof0Prefix.length + Dht.length
  val DriRestartIntervalHigh: Int = DriStart + 4
  val DriRestartIntervalLow: Int = DriStart + 5
  val Header: Seq[Int] = Soi ++ App0 ++ DqtLuminancePrefix ++ DqtChrominancePrefix ++ Sof0Prefix ++ Dht ++ Sos
  val HeaderLength: Int = Header.length
  val HeaderWithDri: Seq[Int] = Soi ++ App0 ++ DqtLuminancePrefix ++ DqtChrominancePrefix ++ Sof0Prefix ++ Dht ++ Dri ++ Sos
  val MaxHeaderLength: Int = HeaderWithDri.length
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

  val sIdle :: sLoadByte :: sQuantMultiply :: sQuantDivide :: sOutput :: Nil = Enum(5)
  val state = RegInit(sIdle)
  val index = RegInit(0.U(log2Ceil(JpegHeaderBytes.MaxHeaderLength).W))
  val outputValid = RegInit(false.B)
  val outputByte = Reg(UInt(8.W))
  val outputLast = Reg(Bool())
  val quantBaseReg = Reg(UInt(8.W))
  val quantScale = Reg(UInt(13.W))
  val quantProduct = Reg(UInt(21.W))
  val staticBytes = VecInit(JpegHeaderBytes.HeaderWithDri.map(_.U(8.W)))
  val zigZag = VecInit(JpegTables.ZigZagOrder.map(_.U(6.W)))
  val luminanceQuant = VecInit(JpegTables.StandardLuminanceQuant.map(_.U(8.W)))
  val chrominanceQuant = VecInit(JpegTables.StandardChrominanceQuant.map(_.U(8.W)))

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

  val clampedQuality = Mux(io.config.quality === 0.U, 1.U, Mux(io.config.quality > 100.U, 100.U, io.config.quality))
  val qualityScale = Mux(clampedQuality < 50.U, 5000.U / clampedQuality, 200.U - (clampedQuality << 1))
  val currentQuantBase = Mux(inLuminanceDqt, luminanceQuant(quantIndex), chrominanceQuant(quantIndex))

  val heightHigh = io.config.ysize(15, 8)
  val heightLow = io.config.ysize(7, 0)
  val widthHigh = io.config.xsize(15, 8)
  val widthLow = io.config.xsize(7, 0)
  val dynamicByte = MuxCase(
    staticBytes(index),
    Seq(
      (index === JpegHeaderBytes.Sof0HeightHigh.U) -> heightHigh,
      (index === JpegHeaderBytes.Sof0HeightLow.U) -> heightLow,
      (index === JpegHeaderBytes.Sof0WidthHigh.U) -> widthHigh,
      (index === JpegHeaderBytes.Sof0WidthLow.U) -> widthLow,
      (index === JpegHeaderBytes.Sof0LuminanceSamplingFactor.U) ->
        Mux(io.config.enableChromaSubsample, 0x22.U, 0x11.U),
      (index === JpegHeaderBytes.DriRestartIntervalHigh.U) -> io.config.restartInterval(15, 8),
      (index === JpegHeaderBytes.DriRestartIntervalLow.U) -> io.config.restartInterval(7, 0)
    )
  )

  when(io.start && state === sIdle) {
    state := sLoadByte
    index := 0.U
  }

  io.output.valid := outputValid
  io.output.bits.byte := outputByte
  io.output.bits.last := outputLast
  io.busy := state =/= sIdle
  io.done := io.output.fire && outputLast

  val currentLast = index === (JpegHeaderBytes.MaxHeaderLength - 1).U
  val currentIsDqt = inLuminanceDqt || inChrominanceDqt
  val scaledQuant = quantProduct / 100.U
  val quantByte = Mux(scaledQuant === 0.U, 1.U, Mux(scaledQuant > 255.U, 255.U, scaledQuant(7, 0)))
  val nextIndex = MuxCase(
    index + 1.U,
    Seq(
      (!io.config.emitJfif && index === JpegHeaderBytes.SoiLast.U) ->
        (JpegHeaderBytes.App0Start + JpegHeaderBytes.App0.length).U,
      (io.config.restartInterval === 0.U && index === (JpegHeaderBytes.DriStart - 1).U) ->
        (JpegHeaderBytes.DriStart + JpegHeaderBytes.Dri.length).U
    )
  )

  when(state === sLoadByte) {
    when(currentIsDqt) {
      quantBaseReg := currentQuantBase
      quantScale := qualityScale
      state := sQuantMultiply
    }.otherwise {
      outputValid := true.B
      outputByte := dynamicByte
      outputLast := currentLast
      state := sOutput
    }
  }

  when(state === sQuantMultiply) {
    quantProduct := quantBaseReg * quantScale + 50.U
    state := sQuantDivide
  }

  when(state === sQuantDivide) {
    outputValid := true.B
    outputByte := quantByte
    outputLast := currentLast
    state := sOutput
  }

  when(state === sOutput && io.output.fire) {
    outputValid := false.B
    when(currentLast) {
      state := sIdle
      index := 0.U
    }.otherwise {
      index := nextIndex
      state := sLoadByte
    }
  }
}
