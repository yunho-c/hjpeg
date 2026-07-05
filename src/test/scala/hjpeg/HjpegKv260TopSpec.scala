// See README.md for license details.

package hjpeg

import chisel3._
import chisel3.simulator.scalatest.ChiselSim
import java.io.ByteArrayInputStream
import javax.imageio.ImageIO
import org.scalatest.freespec.AnyFreeSpec
import org.scalatest.matchers.must.Matchers

class HjpegKv260TopSpec extends AnyFreeSpec with Matchers with ChiselSim {
  private def init(dut: HjpegKv260Top): Unit = {
    dut.io.config.xsize.poke(8.U)
    dut.io.config.ysize.poke(8.U)
    dut.io.config.quality.poke(50.U)
    dut.io.config.restartInterval.poke(0.U)
    dut.io.config.enableChromaSubsample.poke(false.B)
    dut.io.config.emitJfif.poke(true.B)
    dut.io.clearProtocolError.poke(false.B)
    dut.io.sAxisRgb.valid.poke(false.B)
    dut.io.sAxisRgb.bits.data.poke(0.U)
    dut.io.sAxisRgb.bits.keep.poke("b1111".U)
    dut.io.sAxisRgb.bits.last.poke(false.B)
    dut.io.mAxisJpeg.ready.poke(true.B)
  }

  private def configure(
      dut: HjpegKv260Top,
      width: Int,
      height: Int,
      subsample: Boolean = false,
      restartInterval: Int = 0,
      emitJfif: Boolean = true): Unit = {
    dut.io.config.xsize.poke(width.U)
    dut.io.config.ysize.poke(height.U)
    dut.io.config.quality.poke(50.U)
    dut.io.config.restartInterval.poke(restartInterval.U)
    dut.io.config.enableChromaSubsample.poke(subsample.B)
    dut.io.config.emitJfif.poke(emitJfif.B)
  }

  private def emitFrame(dut: HjpegKv260Top, width: Int, height: Int, inputKeep: Int = 0xf): Seq[Int] = {
    val bytes = scala.collection.mutable.ArrayBuffer.empty[Int]
    val pixels = width * height
    var nextPixel = 0
    var sawLast = false
    var cycles = 0
    while (!sawLast) {
      assert(cycles < pixels * 4096 + JpegHeaderBytes.HeaderLength + 4096, "timeout waiting for KV260 top JPEG output")

      if (dut.io.mAxisJpeg.valid.peek().litToBoolean) {
        dut.io.mAxisJpeg.bits.keep.expect(1.U)
        bytes += dut.io.mAxisJpeg.bits.data.peek().litValue.toInt
        sawLast = dut.io.mAxisJpeg.bits.last.peek().litToBoolean
      }

      if (nextPixel < pixels && dut.io.sAxisRgb.ready.peek().litToBoolean) {
        val gray = BigInt(128) | (BigInt(128) << 8) | (BigInt(128) << 16) | (BigInt(0xff) << 24)
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

  "HjpegKv260Top should encode a direct-config frame through 32-bit AXI streams" in {
    simulate(new HjpegKv260Top(HjpegConfig(maxFrameWidth = 32, maxFrameHeight = 32))) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)
      init(dut)
      configure(dut, width = 8, height = 8)

      val bytes = emitFrame(dut, width = 8, height = 8, inputKeep = 0x7)
      bytes.take(2) mustBe Seq(0xff, 0xd8)
      bytes.takeRight(2) mustBe Seq(0xff, 0xd9)
      val image = ImageIO.read(new ByteArrayInputStream(bytes.map(_.toByte).toArray))
      image must not be null
      image.getWidth mustBe 8
      image.getHeight mustBe 8
      dut.io.protocolError.expect(false.B)
    }
  }

  "HjpegKv260Top should recover after a direct clear pulse" in {
    simulate(new HjpegKv260Top(HjpegConfig(maxFrameWidth = 32, maxFrameHeight = 32))) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)
      init(dut)
      configure(dut, width = 2, height = 1)

      dut.io.sAxisRgb.valid.poke(true.B)
      dut.io.sAxisRgb.bits.data.poke(0.U)
      dut.io.sAxisRgb.bits.keep.poke("b1111".U)
      dut.io.sAxisRgb.bits.last.poke(true.B)
      dut.io.sAxisRgb.ready.expect(true.B)
      dut.clock.step()
      dut.io.sAxisRgb.valid.poke(false.B)

      dut.io.protocolError.expect(true.B)
      dut.io.clearProtocolError.poke(true.B)
      dut.clock.step()
      dut.io.clearProtocolError.poke(false.B)
      dut.io.protocolError.expect(false.B)

      configure(dut, width = 8, height = 8)
      val bytes = emitFrame(dut, width = 8, height = 8)
      bytes.take(2) mustBe Seq(0xff, 0xd8)
      bytes.takeRight(2) mustBe Seq(0xff, 0xd9)
      val image = ImageIO.read(new ByteArrayInputStream(bytes.map(_.toByte).toArray))
      image must not be null
      image.getWidth mustBe 8
      image.getHeight mustBe 8
      dut.io.protocolError.expect(false.B)
    }
  }
}
