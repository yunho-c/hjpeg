// See README.md for license details.

package hjpeg

import chisel3._
import chisel3.simulator.scalatest.ChiselSim
import java.io.ByteArrayInputStream
import javax.imageio.ImageIO
import org.scalatest.freespec.AnyFreeSpec
import org.scalatest.matchers.must.Matchers

class HjpegKv260AxiLiteTopSpec extends AnyFreeSpec with Matchers with ChiselSim {
  private def init(dut: HjpegKv260AxiLiteTop): Unit = {
    dut.io.sAxiLite.awaddr.poke(0.U)
    dut.io.sAxiLite.awvalid.poke(false.B)
    dut.io.sAxiLite.wdata.poke(0.U)
    dut.io.sAxiLite.wstrb.poke("b1111".U)
    dut.io.sAxiLite.wvalid.poke(false.B)
    dut.io.sAxiLite.bready.poke(false.B)
    dut.io.sAxiLite.araddr.poke(0.U)
    dut.io.sAxiLite.arvalid.poke(false.B)
    dut.io.sAxiLite.rready.poke(false.B)
    dut.io.sAxisRgb.valid.poke(false.B)
    dut.io.sAxisRgb.bits.data.poke(0.U)
    dut.io.sAxisRgb.bits.keep.poke("b1111".U)
    dut.io.sAxisRgb.bits.last.poke(false.B)
    dut.io.mAxisJpeg.ready.poke(true.B)
  }

  private def writeReg(dut: HjpegKv260AxiLiteTop, addr: Int, data: BigInt): Unit = {
    dut.io.sAxiLite.awaddr.poke(addr.U)
    dut.io.sAxiLite.awvalid.poke(true.B)
    dut.io.sAxiLite.wdata.poke(data.U)
    dut.io.sAxiLite.wstrb.poke("b1111".U)
    dut.io.sAxiLite.wvalid.poke(true.B)
    dut.io.sAxiLite.bready.poke(true.B)
    dut.io.sAxiLite.awready.expect(true.B)
    dut.io.sAxiLite.wready.expect(true.B)
    dut.clock.step()
    dut.io.sAxiLite.awvalid.poke(false.B)
    dut.io.sAxiLite.wvalid.poke(false.B)
    dut.io.sAxiLite.bvalid.expect(true.B)
    dut.io.sAxiLite.bresp.expect(0.U)
    dut.clock.step()
    dut.io.sAxiLite.bready.poke(false.B)
  }

  private def writeRegAddressFirst(dut: HjpegKv260AxiLiteTop, addr: Int, data: BigInt): Unit = {
    dut.io.sAxiLite.awaddr.poke(addr.U)
    dut.io.sAxiLite.awvalid.poke(true.B)
    dut.io.sAxiLite.wvalid.poke(false.B)
    dut.io.sAxiLite.bready.poke(true.B)
    dut.io.sAxiLite.awready.expect(true.B)
    dut.clock.step()

    dut.io.sAxiLite.awvalid.poke(false.B)
    dut.io.sAxiLite.wdata.poke(data.U)
    dut.io.sAxiLite.wstrb.poke("b1111".U)
    dut.io.sAxiLite.wvalid.poke(true.B)
    dut.io.sAxiLite.wready.expect(true.B)
    dut.clock.step()

    dut.io.sAxiLite.wvalid.poke(false.B)
    dut.io.sAxiLite.bvalid.expect(true.B)
    dut.io.sAxiLite.bresp.expect(0.U)
    dut.clock.step()
    dut.io.sAxiLite.bready.poke(false.B)
  }

  private def writeRegDataFirst(dut: HjpegKv260AxiLiteTop, addr: Int, data: BigInt, strobe: Int = 0xf): Unit = {
    dut.io.sAxiLite.awvalid.poke(false.B)
    dut.io.sAxiLite.wdata.poke(data.U)
    dut.io.sAxiLite.wstrb.poke(strobe.U)
    dut.io.sAxiLite.wvalid.poke(true.B)
    dut.io.sAxiLite.bready.poke(true.B)
    dut.io.sAxiLite.wready.expect(true.B)
    dut.clock.step()

    dut.io.sAxiLite.wvalid.poke(false.B)
    dut.io.sAxiLite.awaddr.poke(addr.U)
    dut.io.sAxiLite.awvalid.poke(true.B)
    dut.io.sAxiLite.awready.expect(true.B)
    dut.clock.step()

    dut.io.sAxiLite.awvalid.poke(false.B)
    dut.io.sAxiLite.bvalid.expect(true.B)
    dut.io.sAxiLite.bresp.expect(0.U)
    dut.clock.step()
    dut.io.sAxiLite.bready.poke(false.B)
  }

  private def readReg(dut: HjpegKv260AxiLiteTop, addr: Int): BigInt = {
    dut.io.sAxiLite.araddr.poke(addr.U)
    dut.io.sAxiLite.arvalid.poke(true.B)
    dut.io.sAxiLite.rready.poke(true.B)
    dut.io.sAxiLite.arready.expect(true.B)
    dut.clock.step()
    dut.io.sAxiLite.arvalid.poke(false.B)
    dut.io.sAxiLite.rvalid.expect(true.B)
    dut.io.sAxiLite.rresp.expect(0.U)
    val data = dut.io.sAxiLite.rdata.peek().litValue
    dut.clock.step()
    dut.io.sAxiLite.rready.poke(false.B)
    data
  }

  private def configure(
      dut: HjpegKv260AxiLiteTop,
      width: Int,
      height: Int,
      subsample: Boolean,
      restartInterval: Int = 0,
      emitJfif: Boolean = true): Unit = {
    writeReg(dut, HjpegAxiLiteRegisters.XSize, width)
    writeReg(dut, HjpegAxiLiteRegisters.YSize, height)
    writeReg(dut, HjpegAxiLiteRegisters.Quality, 50)
    writeReg(dut, HjpegAxiLiteRegisters.RestartInterval, restartInterval)
    val control = (if (emitJfif) BigInt(1 << HjpegAxiLiteRegisters.ControlEmitJfifBit) else BigInt(0)) |
      (if (subsample) BigInt(1 << HjpegAxiLiteRegisters.ControlEnableChromaSubsampleBit) else BigInt(0))
    writeReg(dut, HjpegAxiLiteRegisters.Control, control)
  }

  private def emitFrame(dut: HjpegKv260AxiLiteTop, width: Int, height: Int, inputKeep: Int = 0xf): Seq[Int] = {
    val bytes = scala.collection.mutable.ArrayBuffer.empty[Int]
    val pixels = width * height
    var nextPixel = 0
    var sawLast = false
    var cycles = 0
    while (!sawLast) {
      assert(cycles < pixels * 4096 + JpegHeaderBytes.HeaderLength + 4096, "timeout waiting for AXI-Lite top JPEG output")

      if (dut.io.mAxisJpeg.valid.peek().litToBoolean) {
        dut.io.mAxisJpeg.bits.keep.expect(1.U)
        bytes += dut.io.mAxisJpeg.bits.data.peek().litValue.toInt
        sawLast = dut.io.mAxisJpeg.bits.last.peek().litToBoolean
      }

      if (nextPixel < pixels && dut.io.sAxisRgb.ready.peek().litToBoolean) {
        val gray = BigInt(128) | (BigInt(128) << 8) | (BigInt(128) << 16)
        dut.io.sAxisRgb.valid.poke(true.B)
        dut.io.sAxisRgb.bits.data.poke(gray.U)
        dut.io.sAxisRgb.bits.keep.poke(inputKeep.U)
        dut.io.sAxisRgb.bits.last.poke((nextPixel == pixels - 1).B)
        nextPixel += 1
      } else {
        dut.io.sAxisRgb.valid.poke(false.B)
      }

      dut.clock.step()
      cycles += 1
    }
    dut.io.sAxisRgb.valid.poke(false.B)
    bytes.toSeq
  }

  "HjpegKv260AxiLiteTop should expose control and status registers" in {
    simulate(new HjpegKv260AxiLiteTop(HjpegConfig(maxFrameWidth = 32, maxFrameHeight = 32))) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)
      init(dut)

      configure(dut, width = 17, height = 13, subsample = true)

      readReg(dut, HjpegAxiLiteRegisters.XSize) mustBe 17
      readReg(dut, HjpegAxiLiteRegisters.YSize) mustBe 13
      readReg(dut, HjpegAxiLiteRegisters.Quality) mustBe 50
      readReg(dut, HjpegAxiLiteRegisters.RestartInterval) mustBe 0
      readReg(dut, HjpegAxiLiteRegisters.Control) mustBe 0x6
      readReg(dut, HjpegAxiLiteRegisters.Status) mustBe 0
    }
  }

  "HjpegKv260AxiLiteTop should accept independent AXI-Lite write address and data channels" in {
    simulate(new HjpegKv260AxiLiteTop(HjpegConfig(maxFrameWidth = 32, maxFrameHeight = 32))) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)
      init(dut)

      writeRegAddressFirst(dut, HjpegAxiLiteRegisters.XSize, 23)
      writeRegDataFirst(dut, HjpegAxiLiteRegisters.YSize, 19)

      readReg(dut, HjpegAxiLiteRegisters.XSize) mustBe 23
      readReg(dut, HjpegAxiLiteRegisters.YSize) mustBe 19
    }
  }

  "HjpegKv260AxiLiteTop should honor AXI-Lite write strobes" in {
    simulate(new HjpegKv260AxiLiteTop(HjpegConfig(maxFrameWidth = 512, maxFrameHeight = 512))) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)
      init(dut)

      writeReg(dut, HjpegAxiLiteRegisters.XSize, 0x1234)
      writeRegDataFirst(dut, HjpegAxiLiteRegisters.XSize, 0x00ab, strobe = 0x1)
      readReg(dut, HjpegAxiLiteRegisters.XSize) mustBe 0x12ab

      writeRegDataFirst(dut, HjpegAxiLiteRegisters.XSize, 0xcd00, strobe = 0x2)
      readReg(dut, HjpegAxiLiteRegisters.XSize) mustBe 0xcdab
    }
  }

  "HjpegKv260AxiLiteTop should clear protocol errors through AXI-Lite control" in {
    simulate(new HjpegKv260AxiLiteTop(HjpegConfig(maxFrameWidth = 32, maxFrameHeight = 32))) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)
      init(dut)
      configure(dut, width = 1, height = 1, subsample = true, emitJfif = true)

      dut.io.sAxisRgb.valid.poke(true.B)
      dut.io.sAxisRgb.bits.data.poke(0.U)
      dut.io.sAxisRgb.bits.keep.poke("b0011".U)
      dut.io.sAxisRgb.bits.last.poke(true.B)
      dut.io.sAxisRgb.ready.expect(true.B)
      dut.clock.step()
      dut.io.sAxisRgb.valid.poke(false.B)

      (readReg(dut, HjpegAxiLiteRegisters.Status) &
        BigInt(1 << HjpegAxiLiteRegisters.StatusProtocolErrorBit)) mustBe
        BigInt(1 << HjpegAxiLiteRegisters.StatusProtocolErrorBit)

      val clearAndPersistentControl =
        BigInt(1 << HjpegAxiLiteRegisters.ControlClearProtocolErrorBit) |
          BigInt(1 << HjpegAxiLiteRegisters.ControlEnableChromaSubsampleBit) |
          BigInt(1 << HjpegAxiLiteRegisters.ControlEmitJfifBit)
      writeReg(dut, HjpegAxiLiteRegisters.Control, clearAndPersistentControl)

      (readReg(dut, HjpegAxiLiteRegisters.Status) &
        BigInt(1 << HjpegAxiLiteRegisters.StatusProtocolErrorBit)) mustBe 0
      readReg(dut, HjpegAxiLiteRegisters.Control) mustBe
        (BigInt(1 << HjpegAxiLiteRegisters.ControlEnableChromaSubsampleBit) |
          BigInt(1 << HjpegAxiLiteRegisters.ControlEmitJfifBit))
    }
  }

  "HjpegKv260AxiLiteTop should report early input TLAST through AXI-Lite status" in {
    simulate(new HjpegKv260AxiLiteTop(HjpegConfig(maxFrameWidth = 32, maxFrameHeight = 32))) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)
      init(dut)
      configure(dut, width = 2, height = 1, subsample = false)

      dut.io.sAxisRgb.valid.poke(true.B)
      dut.io.sAxisRgb.bits.data.poke(0.U)
      dut.io.sAxisRgb.bits.keep.poke("b1111".U)
      dut.io.sAxisRgb.bits.last.poke(true.B)
      dut.io.sAxisRgb.ready.expect(true.B)
      dut.clock.step()
      dut.io.sAxisRgb.valid.poke(false.B)

      (readReg(dut, HjpegAxiLiteRegisters.Status) &
        BigInt(1 << HjpegAxiLiteRegisters.StatusProtocolErrorBit)) mustBe
        BigInt(1 << HjpegAxiLiteRegisters.StatusProtocolErrorBit)
    }
  }

  "HjpegKv260AxiLiteTop should encode a configured frame through AXI streams" in {
    simulate(new HjpegKv260AxiLiteTop(HjpegConfig(maxFrameWidth = 32, maxFrameHeight = 32))) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)
      init(dut)
      configure(dut, width = 17, height = 13, subsample = true, restartInterval = 1)

      val bytes = emitFrame(dut, width = 17, height = 13)
      bytes.take(2) mustBe Seq(0xff, 0xd8)
      bytes(JpegHeaderBytes.Sof0LuminanceSamplingFactor) mustBe 0x22
      bytes.slice(JpegHeaderBytes.DriStart, JpegHeaderBytes.DriStart + JpegHeaderBytes.Dri.length) mustBe
        Seq(0xff, 0xdd, 0x00, 0x04, 0x00, 0x01)
      bytes.sliding(2).count(_ == Seq(0xff, 0xd0)) mustBe 1
      bytes.takeRight(2) mustBe Seq(0xff, 0xd9)
      val image = ImageIO.read(new ByteArrayInputStream(bytes.map(_.toByte).toArray))
      image must not be null
      image.getWidth mustBe 17
      image.getHeight mustBe 13
      readReg(dut, HjpegAxiLiteRegisters.Status) mustBe 0
    }
  }

  "HjpegKv260AxiLiteTop should honor the emit JFIF control bit" in {
    simulate(new HjpegKv260AxiLiteTop(HjpegConfig(maxFrameWidth = 32, maxFrameHeight = 32))) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)
      init(dut)
      configure(dut, width = 8, height = 8, subsample = false, emitJfif = false)

      val bytes = emitFrame(dut, width = 8, height = 8)
      bytes.take(2) mustBe Seq(0xff, 0xd8)
      bytes.slice(2, 4) mustBe Seq(0xff, 0xdb)
      bytes.sliding(2).exists(_ == Seq(0xff, 0xe0)) mustBe false
      val image = ImageIO.read(new ByteArrayInputStream(bytes.map(_.toByte).toArray))
      image must not be null
      image.getWidth mustBe 8
      image.getHeight mustBe 8
      readReg(dut, HjpegAxiLiteRegisters.Control) mustBe 0
      readReg(dut, HjpegAxiLiteRegisters.Status) mustBe 0
    }
  }

  "HjpegKv260AxiLiteTop should ignore keep for the unused fourth input byte" in {
    simulate(new HjpegKv260AxiLiteTop(HjpegConfig(maxFrameWidth = 32, maxFrameHeight = 32))) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)
      init(dut)
      configure(dut, width = 8, height = 8, subsample = false)

      val bytes = emitFrame(dut, width = 8, height = 8, inputKeep = 0x7)
      bytes.take(2) mustBe Seq(0xff, 0xd8)
      bytes.takeRight(2) mustBe Seq(0xff, 0xd9)
      val image = ImageIO.read(new ByteArrayInputStream(bytes.map(_.toByte).toArray))
      image must not be null
      image.getWidth mustBe 8
      image.getHeight mustBe 8
      readReg(dut, HjpegAxiLiteRegisters.Status) mustBe 0
    }
  }
}
