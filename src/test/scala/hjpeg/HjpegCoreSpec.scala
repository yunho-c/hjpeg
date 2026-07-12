// See README.md for license details.

package hjpeg

import chisel3._
import chisel3.simulator.scalatest.ChiselSim
import java.io.ByteArrayInputStream
import javax.imageio.ImageIO
import org.scalatest.freespec.AnyFreeSpec
import org.scalatest.matchers.must.Matchers

class HjpegCoreSpec extends AnyFreeSpec with Matchers with ChiselSim {
  private case class FrameEmission(bytes: Seq[Int], cycles: Int)

  private def pokeConfig(
      dut: HjpegCore,
      width: Int = 8,
      height: Int = 8,
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
      subsample: Boolean = false,
      restartInterval: Int = 0,
      emitJfif: Boolean = true): Seq[Int] = {
    emitFrame(dut, width, height, subsample, restartInterval, emitJfif) { _ =>
      (r, g, b)
    }
  }

  private def emitFrame(
      dut: HjpegCore,
      width: Int,
      height: Int,
      subsample: Boolean = false,
      restartInterval: Int = 0,
      emitJfif: Boolean = true)(pixelAt: Int => (Int, Int, Int)): Seq[Int] = {
    emitFrameWithStats(dut, width, height, subsample, restartInterval, emitJfif)(pixelAt).bytes
  }

  private def emitFrameWithStats(
      dut: HjpegCore,
      width: Int,
      height: Int,
      subsample: Boolean = false,
      restartInterval: Int = 0,
      emitJfif: Boolean = true,
      outputReadyAt: Int => Boolean = _ => true)(pixelAt: Int => (Int, Int, Int)): FrameEmission = {
    dut.reset.poke(true.B)
    dut.clock.step()
    dut.reset.poke(false.B)

    pokeConfig(dut, width, height, subsample, restartInterval, emitJfif)
    dut.io.clearProtocolError.poke(false.B)
    dut.io.output.ready.poke(true.B)

    val bytes = scala.collection.mutable.ArrayBuffer.empty[Int]
    val pixels = width * height
    var nextPixel = 0
    var sawLast = false
    var cycles = 0
    while (!sawLast) {
      assert(cycles < pixels * 4096 + JpegHeaderBytes.MaxHeaderLength + 4096, "timeout waiting for HjpegCore output")
      val outputReady = outputReadyAt(cycles)
      dut.io.output.ready.poke(outputReady.B)
      if (dut.io.output.valid.peek().litToBoolean && outputReady) {
        bytes += dut.io.output.bits.byte.peek().litValue.toInt
        sawLast = dut.io.output.bits.last.peek().litToBoolean
      }

      if (nextPixel < pixels && dut.io.input.ready.peek().litToBoolean) {
        val (r, g, b) = pixelAt(nextPixel)
        pokePixel(dut, nextPixel, width, r, g, b)
        nextPixel += 1
      } else {
        dut.io.input.valid.poke(false.B)
      }

      dut.clock.step()
      cycles += 1
    }
    dut.io.input.valid.poke(false.B)
    dut.io.output.ready.poke(true.B)
    FrameEmission(bytes.toSeq, cycles)
  }

  private def averageLuma(image: java.awt.image.BufferedImage, xStart: Int, xEnd: Int): Double = {
    var total = 0.0
    var count = 0
    for {
      y <- 0 until image.getHeight
      x <- xStart until xEnd
    } {
      val rgb = image.getRGB(x, y)
      val r = (rgb >> 16) & 0xff
      val g = (rgb >> 8) & 0xff
      val b = rgb & 0xff
      total += 0.299 * r + 0.587 * g + 0.114 * b
      count += 1
    }
    total / count
  }

  private def averageChannel(
      image: java.awt.image.BufferedImage,
      xStart: Int,
      xEnd: Int,
      channel: Int): Double = {
    var total = 0.0
    var count = 0
    for {
      y <- 0 until image.getHeight
      x <- xStart until xEnd
    } {
      val rgb = image.getRGB(x, y)
      total += ((rgb >> channel) & 0xff)
      count += 1
    }
    total / count
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

  "HjpegCore should preserve recognizable non-flat image content" in {
    simulate(new HjpegCore()) { dut =>
      val width = 16
      val height = 16
      val bytes = emitFrame(dut, width = width, height = height) { index =>
        val x = index % width
        val gray = if (x < width / 2) 32 else 224
        (gray, gray, gray)
      }

      bytes.take(2) mustBe Seq(0xff, 0xd8)
      bytes.takeRight(2) mustBe Seq(0xff, 0xd9)
      val image = ImageIO.read(new ByteArrayInputStream(bytes.map(_.toByte).toArray))
      image must not be null
      image.getWidth mustBe width
      image.getHeight mustBe height
      averageLuma(image, width / 2, width) - averageLuma(image, 0, width / 2) must be > 80.0
      dut.io.protocolError.expect(false.B)
    }
  }

  "HjpegCore should preserve recognizable non-flat color content" in {
    simulate(new HjpegCore()) { dut =>
      val width = 16
      val height = 16
      val bytes = emitFrame(dut, width = width, height = height) { index =>
        val x = index % width
        if (x < width / 2) (224, 32, 32) else (32, 32, 224)
      }

      bytes.take(2) mustBe Seq(0xff, 0xd8)
      bytes.takeRight(2) mustBe Seq(0xff, 0xd9)
      val image = ImageIO.read(new ByteArrayInputStream(bytes.map(_.toByte).toArray))
      image must not be null
      image.getWidth mustBe width
      image.getHeight mustBe height
      averageChannel(image, 0, width / 2, 16) - averageChannel(image, 0, width / 2, 0) must be > 60.0
      averageChannel(image, width / 2, width, 0) - averageChannel(image, width / 2, width, 16) must be > 60.0
      dut.io.protocolError.expect(false.B)
    }
  }

  "HjpegCore should keep a small 4:4:4 frame within the local cycle budget" in {
    simulate(new HjpegCore()) { dut =>
      val width = 16
      val height = 16
      val emission = emitFrameWithStats(dut, width = width, height = height) { index =>
        val x = index % width
        val y = index / width
        val value = (x * 13 + y * 17) & 0xff
        (value, 255 - value, (value / 2) + 64)
      }

      emission.bytes.take(2) mustBe Seq(0xff, 0xd8)
      emission.bytes.takeRight(2) mustBe Seq(0xff, 0xd9)
      info(s"16x16 4:4:4 frame latency: ${emission.cycles} cycles")
      withClue(s"cycles=${emission.cycles}, pixels=${width * height}") {
        emission.cycles must be < 2300
      }
      dut.io.protocolError.expect(false.B)
    }
  }

  "HjpegCore should preserve output bytes under output backpressure" in {
    val width = 8
    val height = 8
    def pixelAt(index: Int): (Int, Int, Int) = {
      val x = index % width
      val y = index / width
      ((x * 31 + y * 5) & 0xff, (64 + x * 17) & 0xff, (255 - y * 29) & 0xff)
    }

    var expected = Seq.empty[Int]
    simulate(new HjpegCore()) { dut =>
      expected = emitFrame(dut, width = width, height = height)(pixelAt)
      dut.io.protocolError.expect(false.B)
    }

    simulate(new HjpegCore()) { dut =>
      val stalled = emitFrameWithStats(
        dut,
        width = width,
        height = height,
        outputReadyAt = cycle => (cycle % 4) != 1 && (cycle % 9) != 5)(pixelAt).bytes

      stalled mustBe expected
      val image = ImageIO.read(new ByteArrayInputStream(stalled.map(_.toByte).toArray))
      image must not be null
      image.getWidth mustBe width
      image.getHeight mustBe height
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

  "HjpegCore should preserve recognizable non-flat color content in 4:2:0 mode" in {
    simulate(new HjpegCore()) { dut =>
      val width = 16
      val height = 16
      val bytes = emitFrame(dut, width = width, height = height, subsample = true) { index =>
        val x = index % width
        if (x < width / 2) (224, 32, 32) else (32, 32, 224)
      }

      bytes.take(2) mustBe Seq(0xff, 0xd8)
      bytes.takeRight(2) mustBe Seq(0xff, 0xd9)
      val image = ImageIO.read(new ByteArrayInputStream(bytes.map(_.toByte).toArray))
      image must not be null
      image.getWidth mustBe width
      image.getHeight mustBe height
      averageChannel(image, 0, width / 2, 16) - averageChannel(image, 0, width / 2, 0) must be > 60.0
      averageChannel(image, width / 2, width, 0) - averageChannel(image, width / 2, width, 16) must be > 60.0
      dut.io.protocolError.expect(false.B)
    }
  }

  "HjpegCore should emit decodable restart-marked JPEGs" in {
    simulate(new HjpegCore()) { dut =>
      val bytes = emitFlatFrame(dut, width = 16, height = 8, r = 128, g = 128, b = 128, restartInterval = 1)

      bytes.take(2) mustBe Seq(0xff, 0xd8)
      bytes.slice(JpegHeaderBytes.DriStart, JpegHeaderBytes.DriStart + JpegHeaderBytes.Dri.length) mustBe
        Seq(0xff, 0xdd, 0x00, 0x04, 0x00, 0x01)
      bytes.sliding(2).count(_ == Seq(0xff, 0xd0)) mustBe 1
      bytes.takeRight(2) mustBe Seq(0xff, 0xd9)
      val image = ImageIO.read(new ByteArrayInputStream(bytes.map(_.toByte).toArray))
      image must not be null
      image.getWidth mustBe 16
      image.getHeight mustBe 8
      dut.io.protocolError.expect(false.B)
    }
  }

  "HjpegCore should emit decodable JPEGs without JFIF APP0" in {
    simulate(new HjpegCore()) { dut =>
      val bytes = emitFlatFrame(dut, width = 8, height = 8, r = 128, g = 128, b = 128, emitJfif = false)

      bytes.take(2) mustBe Seq(0xff, 0xd8)
      bytes.slice(2, 4) mustBe Seq(0xff, 0xdb)
      bytes.sliding(2).exists(_ == Seq(0xff, 0xe0)) mustBe false
      bytes.takeRight(2) mustBe Seq(0xff, 0xd9)
      val image = ImageIO.read(new ByteArrayInputStream(bytes.map(_.toByte).toArray))
      image must not be null
      image.getWidth mustBe 8
      image.getHeight mustBe 8
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
      dut.io.output.valid.expect(false.B)
      dut.io.input.valid.poke(false.B)
      dut.clock.step()
      dut.io.busy.expect(false.B)
      dut.io.clearProtocolError.poke(true.B)
      dut.clock.step()
      dut.io.protocolError.expect(false.B)
      dut.io.busy.expect(false.B)
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
      dut.io.input.valid.poke(false.B)
      dut.clock.step()
      dut.io.busy.expect(false.B)
    }
  }
}
