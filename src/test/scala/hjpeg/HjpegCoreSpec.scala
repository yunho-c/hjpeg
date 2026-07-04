// See README.md for license details.

package hjpeg

import chisel3._
import chisel3.simulator.scalatest.ChiselSim
import java.io.ByteArrayInputStream
import javax.imageio.ImageIO
import org.scalatest.freespec.AnyFreeSpec
import org.scalatest.matchers.must.Matchers

class HjpegCoreSpec extends AnyFreeSpec with Matchers with ChiselSim {
  private def pokeConfig(dut: HjpegCore, width: Int = 8, height: Int = 8, subsample: Boolean = false): Unit = {
    dut.io.config.xsize.poke(width.U)
    dut.io.config.ysize.poke(height.U)
    dut.io.config.quality.poke(50.U)
    dut.io.config.restartInterval.poke(0.U)
    dut.io.config.enableChromaSubsample.poke(subsample.B)
    dut.io.config.emitJfif.poke(true.B)
  }

  private def pokePixel(dut: HjpegCore, index: Int, width: Int, r: Int, g: Int, b: Int): Unit = {
    dut.io.input.valid.poke(true.B)
    dut.io.input.bits.x.poke((index % width).U)
    dut.io.input.bits.y.poke((index / width).U)
    dut.io.input.bits.r.poke(r.U)
    dut.io.input.bits.g.poke(g.U)
    dut.io.input.bits.b.poke(b.U)
  }

  private def emitFlatFrame(
      dut: HjpegCore,
      width: Int,
      height: Int,
      r: Int,
      g: Int,
      b: Int,
      subsample: Boolean = false): Seq[Int] = {
    dut.reset.poke(true.B)
    dut.clock.step()
    dut.reset.poke(false.B)

    pokeConfig(dut, width, height, subsample)
    dut.io.clearProtocolError.poke(false.B)
    dut.io.output.ready.poke(true.B)

    val bytes = scala.collection.mutable.ArrayBuffer.empty[Int]
    val pixels = width * height
    var nextPixel = 0
    var sawLast = false
    var cycles = 0
    while (!sawLast) {
      assert(cycles < pixels * 8 + JpegHeaderBytes.HeaderLength + 256, "timeout waiting for HjpegCore output")
      if (dut.io.output.valid.peek().litToBoolean) {
        bytes += dut.io.output.bits.byte.peek().litValue.toInt
        sawLast = dut.io.output.bits.last.peek().litToBoolean
      }

      if (nextPixel < pixels && dut.io.input.ready.peek().litToBoolean) {
        pokePixel(dut, nextPixel, width, r, g, b)
        nextPixel += 1
      } else {
        dut.io.input.valid.poke(false.B)
      }

      dut.clock.step()
      cycles += 1
    }
    dut.io.input.valid.poke(false.B)
    bytes.toSeq
  }

  "HjpegCore should emit a complete JPEG for one supported 8x8 RGB frame" in {
    simulate(new HjpegCore()) { dut =>
      val bytes = emitFlatFrame(dut, width = 8, height = 8, r = 128, g = 128, b = 128)

      bytes.length mustBe JpegHeaderBytes.HeaderLength + 4
      bytes.take(2) mustBe Seq(0xff, 0xd8)
      bytes.slice(JpegHeaderBytes.HeaderLength, JpegHeaderBytes.HeaderLength + 2) mustBe Seq(0x28, 0x03)
      bytes.takeRight(2) mustBe Seq(0xff, 0xd9)
      dut.io.protocolError.expect(false.B)
    }
  }

  "HjpegCore should emit a decodable JPEG for a multi-block 16x8 RGB frame" in {
    simulate(new HjpegCore()) { dut =>
      val bytes = emitFlatFrame(dut, width = 16, height = 8, r = 128, g = 128, b = 128)

      bytes.take(2) mustBe Seq(0xff, 0xd8)
      bytes.takeRight(2) mustBe Seq(0xff, 0xd9)
      val image = ImageIO.read(new ByteArrayInputStream(bytes.map(_.toByte).toArray))
      image must not be null
      image.getWidth mustBe 16
      image.getHeight mustBe 8
      dut.io.protocolError.expect(false.B)
    }
  }

  "HjpegCore should emit a decodable JPEG across multiple raster stripes" in {
    simulate(new HjpegCore()) { dut =>
      val bytes = emitFlatFrame(dut, width = 16, height = 16, r = 128, g = 128, b = 128)

      bytes.take(2) mustBe Seq(0xff, 0xd8)
      bytes.takeRight(2) mustBe Seq(0xff, 0xd9)
      val image = ImageIO.read(new ByteArrayInputStream(bytes.map(_.toByte).toArray))
      image must not be null
      image.getWidth mustBe 16
      image.getHeight mustBe 16
      dut.io.protocolError.expect(false.B)
    }
  }

  "HjpegCore should pad odd-sized frames into decodable JPEGs" in {
    simulate(new HjpegCore()) { dut =>
      val bytes = emitFlatFrame(dut, width = 10, height = 10, r = 128, g = 128, b = 128)

      bytes.take(2) mustBe Seq(0xff, 0xd8)
      bytes.takeRight(2) mustBe Seq(0xff, 0xd9)
      val image = ImageIO.read(new ByteArrayInputStream(bytes.map(_.toByte).toArray))
      image must not be null
      image.getWidth mustBe 10
      image.getHeight mustBe 10
      dut.io.protocolError.expect(false.B)
    }
  }

  "HjpegCore should emit a decodable 4:2:0 JPEG when chroma subsampling is enabled" in {
    simulate(new HjpegCore()) { dut =>
      val bytes = emitFlatFrame(dut, width = 17, height = 13, r = 128, g = 128, b = 128, subsample = true)

      bytes.take(2) mustBe Seq(0xff, 0xd8)
      bytes(JpegHeaderBytes.Sof0LuminanceSamplingFactor) mustBe 0x22
      bytes.takeRight(2) mustBe Seq(0xff, 0xd9)
      val image = ImageIO.read(new ByteArrayInputStream(bytes.map(_.toByte).toArray))
      image must not be null
      image.getWidth mustBe 17
      image.getHeight mustBe 13
      dut.io.protocolError.expect(false.B)
    }
  }

  "HjpegCore should report out-of-frame coordinates" in {
    simulate(new HjpegCore()) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)

      pokeConfig(dut)
      dut.io.clearProtocolError.poke(false.B)
      dut.io.output.ready.poke(true.B)

      dut.io.input.valid.poke(true.B)
      dut.io.input.bits.x.poke(8.U)
      dut.io.input.bits.y.poke(0.U)
      dut.io.input.bits.r.poke(0.U)
      dut.io.input.bits.g.poke(0.U)
      dut.io.input.bits.b.poke(0.U)
      dut.clock.step()

      dut.io.protocolError.expect(true.B)
      dut.io.input.valid.poke(false.B)
      dut.io.clearProtocolError.poke(true.B)
      dut.clock.step()
      dut.io.protocolError.expect(false.B)
    }
  }

  "HjpegCore should report invalid frame shapes" in {
    simulate(new HjpegCore()) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)

      pokeConfig(dut, width = 0, height = 8)
      dut.io.clearProtocolError.poke(false.B)
      dut.io.output.ready.poke(true.B)

      dut.io.input.valid.poke(true.B)
      dut.io.input.bits.x.poke(0.U)
      dut.io.input.bits.y.poke(0.U)
      dut.io.input.bits.r.poke(0.U)
      dut.io.input.bits.g.poke(0.U)
      dut.io.input.bits.b.poke(0.U)
      dut.io.input.ready.expect(true.B)
      dut.clock.step()

      dut.io.protocolError.expect(true.B)
      dut.io.output.valid.expect(false.B)
    }
  }
}
