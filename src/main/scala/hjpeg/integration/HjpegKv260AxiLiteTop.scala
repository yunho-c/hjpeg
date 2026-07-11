// See README.md for license details.

package hjpeg

import chisel3._
import chisel3.util._

object HjpegAxiLiteRegisters {
  val Control = 0x00
  val Status = 0x04
  val XSize = 0x08
  val YSize = 0x0c
  val Quality = 0x10
  val RestartInterval = 0x14

  val ControlClearProtocolErrorBit = 0
  val ControlEnableChromaSubsampleBit = 1
  val ControlEmitJfifBit = 2

  val StatusBusyBit = 0
  val StatusProtocolErrorBit = 1
}

/** KV260-oriented shell with AXI-Lite control and AXI-stream image ports.
  *
  * Register map, all 32-bit little-endian words:
  *
  *   0x00 control: bit0 clear protocol error (write-one pulse),
  *                 bit1 enable 4:2:0 chroma subsampling,
  *                 bit2 emit JFIF APP0 marker
  *   0x04 status:  bit0 busy, bit1 protocol error
  *   0x08 xsize
  *   0x0c ysize
  *   0x10 quality
  *   0x14 restart interval in MCUs, or zero to disable restart markers
  */
class HjpegKv260AxiLiteTop(c: HjpegConfig = HjpegConfig(), axiLiteAddrBits: Int = 12) extends Module {
  val pixelDataBits = c.pixelBits * HjpegConstants.Components
  val dmaInputDataBits = 32

  val io = IO(new Bundle {
    val sAxiLite = new AxiLiteSlave(axiLiteAddrBits, 32)
    val sAxisRgb = Flipped(Decoupled(new AxiStreamWord(dmaInputDataBits)))
    val mAxisJpeg = Decoupled(new AxiStreamWord(c.outputDataBits))
    val busy = Output(Bool())
    val protocolError = Output(Bool())
  })

  val xsize = RegInit(0.U(c.coordBits.W))
  val ysize = RegInit(0.U(c.coordBits.W))
  val quality = RegInit(50.U(7.W))
  val restartInterval = RegInit(0.U(16.W))
  val enableChromaSubsample = RegInit(false.B)
  val emitJfif = RegInit(true.B)
  val clearProtocolErrorPulse = RegInit(false.B)

  clearProtocolErrorPulse := false.B

  val core = Module(new HjpegAxiStreamCore(c))
  core.io.config.xsize := xsize
  core.io.config.ysize := ysize
  core.io.config.quality := quality
  core.io.config.restartInterval := restartInterval
  core.io.config.enableChromaSubsample := enableChromaSubsample
  core.io.config.emitJfif := emitJfif
  core.io.clearProtocolError := clearProtocolErrorPulse
  core.io.input.valid := io.sAxisRgb.valid
  io.sAxisRgb.ready := core.io.input.ready
  core.io.input.bits.data := io.sAxisRgb.bits.data(pixelDataBits - 1, 0)
  core.io.input.bits.keep := io.sAxisRgb.bits.keep((pixelDataBits / 8) - 1, 0)
  core.io.input.bits.last := io.sAxisRgb.bits.last
  io.mAxisJpeg <> core.io.output
  io.busy := core.io.busy
  io.protocolError := core.io.protocolError

  def applyWriteStrobes(current: UInt, data: UInt, strobe: UInt): UInt =
    Cat((0 until 4).reverse.map { byte =>
      Mux(strobe(byte), data(8 * byte + 7, 8 * byte), current(8 * byte + 7, 8 * byte))
    })

  val writeResponseValid = RegInit(false.B)
  val writeAddressPending = RegInit(false.B)
  val writeAddress = Reg(UInt(axiLiteAddrBits.W))
  val writeDataPending = RegInit(false.B)
  val writeData = Reg(UInt(32.W))
  val writeStrobe = Reg(UInt(4.W))
  val readResponseValid = RegInit(false.B)
  val readData = RegInit(0.U(32.W))

  val canAcceptWrite = !writeResponseValid
  io.sAxiLite.awready := canAcceptWrite && !writeAddressPending
  io.sAxiLite.wready := canAcceptWrite && !writeDataPending
  io.sAxiLite.bresp := 0.U
  io.sAxiLite.bvalid := writeResponseValid

  val writeAddressFire = io.sAxiLite.awvalid && io.sAxiLite.awready
  val writeDataFire = io.sAxiLite.wvalid && io.sAxiLite.wready
  val effectiveWriteAddress = Mux(writeAddressFire, io.sAxiLite.awaddr, writeAddress)
  val effectiveWriteData = Mux(writeDataFire, io.sAxiLite.wdata, writeData)
  val effectiveWriteStrobe = Mux(writeDataFire, io.sAxiLite.wstrb, writeStrobe)
  val writeFire =
    canAcceptWrite &&
      (writeAddressPending || writeAddressFire) &&
      (writeDataPending || writeDataFire)

  when(writeAddressFire && !writeFire) {
    writeAddress := io.sAxiLite.awaddr
    writeAddressPending := true.B
  }
  when(writeDataFire && !writeFire) {
    writeData := io.sAxiLite.wdata
    writeStrobe := io.sAxiLite.wstrb
    writeDataPending := true.B
  }

  when(writeFire) {
    switch(effectiveWriteAddress) {
      is(HjpegAxiLiteRegisters.Control.U) {
        val control = Cat(0.U(29.W), emitJfif, enableChromaSubsample, 0.U(1.W))
        val nextControl = applyWriteStrobes(control, effectiveWriteData, effectiveWriteStrobe)
        clearProtocolErrorPulse := nextControl(HjpegAxiLiteRegisters.ControlClearProtocolErrorBit)
        enableChromaSubsample := nextControl(HjpegAxiLiteRegisters.ControlEnableChromaSubsampleBit)
        emitJfif := nextControl(HjpegAxiLiteRegisters.ControlEmitJfifBit)
      }
      is(HjpegAxiLiteRegisters.XSize.U) {
        xsize := applyWriteStrobes(xsize.pad(32), effectiveWriteData, effectiveWriteStrobe)(c.coordBits - 1, 0)
      }
      is(HjpegAxiLiteRegisters.YSize.U) {
        ysize := applyWriteStrobes(ysize.pad(32), effectiveWriteData, effectiveWriteStrobe)(c.coordBits - 1, 0)
      }
      is(HjpegAxiLiteRegisters.Quality.U) {
        quality := applyWriteStrobes(quality.pad(32), effectiveWriteData, effectiveWriteStrobe)(6, 0)
      }
      is(HjpegAxiLiteRegisters.RestartInterval.U) {
        restartInterval := applyWriteStrobes(restartInterval.pad(32), effectiveWriteData, effectiveWriteStrobe)(15, 0)
      }
    }
    writeAddressPending := false.B
    writeDataPending := false.B
    writeResponseValid := true.B
  }.elsewhen(io.sAxiLite.bvalid && io.sAxiLite.bready) {
    writeResponseValid := false.B
  }

  val canAcceptRead = !readResponseValid
  val readFire = io.sAxiLite.arvalid && canAcceptRead
  io.sAxiLite.arready := canAcceptRead
  io.sAxiLite.rresp := 0.U
  io.sAxiLite.rvalid := readResponseValid
  io.sAxiLite.rdata := readData

  when(readFire) {
    readData := MuxLookup(io.sAxiLite.araddr, 0.U(32.W))(
      Seq(
        HjpegAxiLiteRegisters.Control.U -> Cat(
          0.U(29.W),
          emitJfif,
          enableChromaSubsample,
          0.U(1.W)
        ),
        HjpegAxiLiteRegisters.Status.U -> Cat(
          0.U(30.W),
          core.io.protocolError,
          core.io.busy
        ),
        HjpegAxiLiteRegisters.XSize.U -> xsize.pad(32),
        HjpegAxiLiteRegisters.YSize.U -> ysize.pad(32),
        HjpegAxiLiteRegisters.Quality.U -> quality.pad(32),
        HjpegAxiLiteRegisters.RestartInterval.U -> restartInterval.pad(32)
      )
    )
    readResponseValid := true.B
  }.elsewhen(io.sAxiLite.rvalid && io.sAxiLite.rready) {
    readResponseValid := false.B
  }
}
