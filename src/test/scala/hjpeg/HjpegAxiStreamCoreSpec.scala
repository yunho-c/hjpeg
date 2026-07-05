// See README.md for license details.

package hjpeg

import chisel3._
import chisel3.simulator.scalatest.ChiselSim
import java.io.ByteArrayInputStream
import javax.imageio.ImageIO
import org.scalatest.freespec.AnyFreeSpec
import org.scalatest.matchers.must.Matchers

class HjpegAxiStreamCoreSpec extends AnyFreeSpec with Matchers with ChiselSim {
  private def pokeConfig(
      dut: HjpegAxiStreamCore,
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

  private def pushPixel(dut: HjpegAxiStreamCore, index: Int, last: Boolean): Unit = {
    pushRgbPixel(dut, r = 128, g = 128, b = 128, last)
  }

  private def pushRgbPixel(dut: HjpegAxiStreamCore, r: Int, g: Int, b: Int, last: Boolean): Unit = {
    dut.io.input.valid.poke(true.B)
    dut.io.input.bits.keep.poke(7.U)
    dut.io.input.bits.data.poke((BigInt(r) | (BigInt(g) << 8) | (BigInt(b) << 16)).U)
    dut.io.input.bits.last.poke(last.B)
    dut.io.input.ready.expect(true.B)
    dut.clock.step()
  }

  private def collectFrame(dut: HjpegAxiStreamCore, maxCycles: Int): Seq[Int] = {
    val bytes = scala.collection.mutable.ArrayBuffer.empty[Int]
    var sawLast = false
    var cycles = 0
    val effectiveMaxCycles = maxCycles.max(80000)
    while (!sawLast) {
      assert(cycles < effectiveMaxCycles, "timeout waiting for AXI JPEG output")
      if (dut.io.output.valid.peek().litToBoolean) {
        dut.io.output.bits.keep.expect(1.U)
        bytes += dut.io.output.bits.data.peek().litValue.toInt
        sawLast = dut.io.output.bits.last.peek().litToBoolean
      }
      dut.clock.step()
      cycles += 1
    }
    bytes.toSeq
  }

  private def emitAxiFrame(
      dut: HjpegAxiStreamCore,
      width: Int,
      height: Int,
      subsample: Boolean = false,
      restartInterval: Int = 0,
      emitJfif: Boolean = true,
      readyAt: Int => Boolean = _ => true)(pixelAt: Int => (Int, Int, Int)): Seq[Int] = {
    pokeConfig(
      dut,
      width = width,
      height = height,
      subsample = subsample,
      restartInterval = restartInterval,
      emitJfif = emitJfif)
    dut.io.clearProtocolError.poke(false.B)
    dut.io.input.valid.poke(false.B)
    dut.io.output.ready.poke(false.B)

    val bytes = scala.collection.mutable.ArrayBuffer.empty[Int]
    val pixels = width * height
    var nextPixel = 0
    var sawLast = false
    var cycles = 0
    val maxCycles = pixels * 4096 + JpegHeaderBytes.MaxHeaderLength + 4096
    while (!sawLast) {
      assert(cycles < maxCycles, "timeout waiting for backpressured AXI JPEG output")

      val outputReady = readyAt(cycles)
      dut.io.output.ready.poke(outputReady.B)
      if (dut.io.output.valid.peek().litToBoolean && outputReady) {
        dut.io.output.bits.keep.expect(1.U)
        bytes += dut.io.output.bits.data.peek().litValue.toInt
        sawLast = dut.io.output.bits.last.peek().litToBoolean
      }

      if (nextPixel < pixels && dut.io.input.ready.peek().litToBoolean) {
        val (r, g, b) = pixelAt(nextPixel)
        dut.io.input.valid.poke(true.B)
        dut.io.input.bits.keep.poke(7.U)
        dut.io.input.bits.data.poke((BigInt(r) | (BigInt(g) << 8) | (BigInt(b) << 16)).U)
        dut.io.input.bits.last.poke((nextPixel == pixels - 1).B)
        nextPixel += 1
      } else {
        dut.io.input.valid.poke(false.B)
      }

      dut.clock.step()
      cycles += 1
    }
    dut.io.input.valid.poke(false.B)
    dut.io.output.ready.poke(true.B)
    bytes.toSeq
  }

  private def pokeCoreConfig(
      dut: HjpegCore,
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

  private def pokeCorePixel(dut: HjpegCore, index: Int, width: Int, r: Int, g: Int, b: Int): Unit = {
    dut.io.input.valid.poke(true.B)
    dut.io.input.bits.x.poke((index % width).U)
    dut.io.input.bits.y.poke((index / width).U)
    dut.io.input.bits.r.poke(r.U)
    dut.io.input.bits.g.poke(g.U)
    dut.io.input.bits.b.poke(b.U)
  }

  private def emitCoreFrame(
      dut: HjpegCore,
      width: Int,
      height: Int,
      subsample: Boolean = false,
      restartInterval: Int = 0,
      emitJfif: Boolean = true)(pixelAt: Int => (Int, Int, Int)): Seq[Int] = {
    dut.reset.poke(true.B)
    dut.clock.step()
    dut.reset.poke(false.B)

    pokeCoreConfig(dut, width, height, subsample, restartInterval, emitJfif)
    dut.io.clearProtocolError.poke(false.B)
    dut.io.output.ready.poke(true.B)

    val bytes = scala.collection.mutable.ArrayBuffer.empty[Int]
    val pixels = width * height
    var nextPixel = 0
    var sawLast = false
    var cycles = 0
    while (!sawLast) {
      assert(cycles < pixels * 4096 + JpegHeaderBytes.MaxHeaderLength + 4096, "timeout waiting for HjpegCore output")
      if (dut.io.output.valid.peek().litToBoolean) {
        bytes += dut.io.output.bits.byte.peek().litValue.toInt
        sawLast = dut.io.output.bits.last.peek().litToBoolean
      }

      if (nextPixel < pixels && dut.io.input.ready.peek().litToBoolean) {
        val (r, g, b) = pixelAt(nextPixel)
        pokeCorePixel(dut, nextPixel, width, r, g, b)
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

  "HjpegAxiStreamCore should generate raster coordinates and emit a JPEG frame" in {
    simulate(new HjpegAxiStreamCore()) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)

      pokeConfig(dut, width = 16, height = 8)
      dut.io.clearProtocolError.poke(false.B)
      dut.io.output.ready.poke(true.B)

      for (index <- 0 until 16 * 8) {
        pushPixel(dut, index, last = index == 16 * 8 - 1)
      }
      dut.io.input.valid.poke(false.B)

      val bytes = collectFrame(dut, JpegHeaderBytes.HeaderLength + 128)

      bytes.take(2) mustBe Seq(0xff, 0xd8)
      bytes.takeRight(2) mustBe Seq(0xff, 0xd9)
      val image = ImageIO.read(new ByteArrayInputStream(bytes.map(_.toByte).toArray))
      image must not be null
      image.getWidth mustBe 16
      image.getHeight mustBe 8
      dut.io.protocolError.expect(false.B)
    }
  }

  "HjpegAxiStreamCore should hold frame config stable after the first input beat" in {
    simulate(new HjpegAxiStreamCore()) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)

      pokeConfig(dut, width = 16, height = 8)
      dut.io.clearProtocolError.poke(false.B)
      dut.io.output.ready.poke(true.B)

      pushPixel(dut, 0, last = false)
      dut.io.config.xsize.poke(8.U)
      dut.io.config.ysize.poke(8.U)
      dut.io.config.enableChromaSubsample.poke(true.B)
      dut.io.config.emitJfif.poke(false.B)

      for (index <- 1 until 16 * 8) {
        pushPixel(dut, index, last = index == 16 * 8 - 1)
      }
      dut.io.input.valid.poke(false.B)

      val bytes = collectFrame(dut, JpegHeaderBytes.HeaderLength + 128)

      bytes.take(2) mustBe Seq(0xff, 0xd8)
      bytes.slice(2, 20) mustBe JpegHeaderBytes.App0
      bytes(JpegHeaderBytes.Sof0WidthHigh) mustBe 0x00
      bytes(JpegHeaderBytes.Sof0WidthLow) mustBe 0x10
      bytes(JpegHeaderBytes.Sof0HeightHigh) mustBe 0x00
      bytes(JpegHeaderBytes.Sof0HeightLow) mustBe 0x08
      bytes(JpegHeaderBytes.Sof0LuminanceSamplingFactor) mustBe 0x11
      val image = ImageIO.read(new ByteArrayInputStream(bytes.map(_.toByte).toArray))
      image must not be null
      image.getWidth mustBe 16
      image.getHeight mustBe 8
      dut.io.protocolError.expect(false.B)
    }
  }

  "HjpegAxiStreamCore should encode non-gray AXI RGB frames like direct HjpegCore input" in {
    val width = 16
    val height = 8
    def pixelAt(index: Int): (Int, Int, Int) = {
      val x = index % width
      val y = index / width
      if (((x + y) & 3) == 0) {
        (240, 24, 80)
      } else if (x < width / 2) {
        (24, 200, 64)
      } else {
        (40, 56, 232)
      }
    }

    var expected = Seq.empty[Int]
    simulate(new HjpegCore()) { dut =>
      expected = emitCoreFrame(dut, width, height)(pixelAt)
      dut.io.protocolError.expect(false.B)
    }

    simulate(new HjpegAxiStreamCore()) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)

      pokeConfig(dut, width = width, height = height)
      dut.io.clearProtocolError.poke(false.B)
      dut.io.output.ready.poke(true.B)

      for (index <- 0 until width * height) {
        val (r, g, b) = pixelAt(index)
        pushRgbPixel(dut, r, g, b, last = index == width * height - 1)
      }
      dut.io.input.valid.poke(false.B)

      collectFrame(dut, JpegHeaderBytes.HeaderLength + 512) mustBe expected
      dut.io.protocolError.expect(false.B)
    }
  }

  "HjpegAxiStreamCore should preserve configured 4:2:0 restart frames like direct HjpegCore input" in {
    val width = 17
    val height = 13
    val restartInterval = 1
    def pixelAt(index: Int): (Int, Int, Int) = {
      val x = index % width
      val y = index / width
      (
        (32 + x * 9 + y * 3) & 0xff,
        (220 - x * 5 + y * 11) & 0xff,
        (80 + x * 7 + y * 13) & 0xff)
    }

    var expected = Seq.empty[Int]
    simulate(new HjpegCore()) { dut =>
      expected = emitCoreFrame(
        dut,
        width,
        height,
        subsample = true,
        restartInterval = restartInterval,
        emitJfif = false)(pixelAt)
      dut.io.protocolError.expect(false.B)
    }

    simulate(new HjpegAxiStreamCore()) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)

      pokeConfig(
        dut,
        width = width,
        height = height,
        subsample = true,
        restartInterval = restartInterval,
        emitJfif = false)
      dut.io.clearProtocolError.poke(false.B)
      dut.io.output.ready.poke(true.B)

      for (index <- 0 until width * height) {
        val (r, g, b) = pixelAt(index)
        pushRgbPixel(dut, r, g, b, last = index == width * height - 1)
      }
      dut.io.input.valid.poke(false.B)

      val bytes = collectFrame(dut, JpegHeaderBytes.MaxHeaderLength + 1024)
      bytes mustBe expected
      bytes.slice(2, 4) mustBe Seq(0xff, 0xdb)
      bytes(
        JpegHeaderBytes.Sof0LuminanceSamplingFactor - JpegHeaderBytes.App0.length) mustBe 0x22
      bytes.sliding(2).exists(_ == Seq(0xff, 0xe0)) mustBe false
      bytes.sliding(2).count(_ == Seq(0xff, 0xd0)) must be > 0
      val image = ImageIO.read(new ByteArrayInputStream(bytes.map(_.toByte).toArray))
      image must not be null
      image.getWidth mustBe width
      image.getHeight mustBe height
      dut.io.protocolError.expect(false.B)
    }
  }

  "HjpegAxiStreamCore should preserve output bytes under AXI backpressure" in {
    val width = 8
    val height = 8
    def pixelAt(index: Int): (Int, Int, Int) = {
      val x = index % width
      val y = index / width
      ((x * 29 + y * 7) & 0xff, (255 - x * 17) & 0xff, (y * 31 + 48) & 0xff)
    }

    var expected = Seq.empty[Int]
    simulate(new HjpegAxiStreamCore()) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)

      expected = emitAxiFrame(dut, width, height)(pixelAt)
      dut.io.protocolError.expect(false.B)
    }

    simulate(new HjpegAxiStreamCore()) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)

      val stalled = emitAxiFrame(
        dut,
        width,
        height,
        readyAt = cycle => (cycle % 5) != 2 && (cycle % 11) != 7)(pixelAt)

      stalled mustBe expected
      val image = ImageIO.read(new ByteArrayInputStream(stalled.map(_.toByte).toArray))
      image must not be null
      image.getWidth mustBe width
      image.getHeight mustBe height
      dut.io.protocolError.expect(false.B)
    }
  }

  "HjpegAxiStreamCore should report mismatched input last" in {
    simulate(new HjpegAxiStreamCore()) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)

      pokeConfig(dut)
      dut.io.clearProtocolError.poke(false.B)
      dut.io.output.ready.poke(true.B)
      dut.io.input.valid.poke(true.B)
      dut.io.input.bits.keep.poke(7.U)
      dut.io.input.bits.data.poke(0.U)
      dut.io.input.bits.last.poke(true.B)
      dut.clock.step()

      dut.io.protocolError.expect(true.B)
    }
  }

  "HjpegAxiStreamCore should report incomplete RGB input words" in {
    simulate(new HjpegAxiStreamCore()) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)

      pokeConfig(dut, width = 1, height = 1)
      dut.io.clearProtocolError.poke(false.B)
      dut.io.output.ready.poke(true.B)
      dut.io.input.valid.poke(true.B)
      dut.io.input.bits.keep.poke("b011".U)
      dut.io.input.bits.data.poke(0.U)
      dut.io.input.bits.last.poke(true.B)
      dut.io.input.ready.expect(true.B)
      dut.clock.step()

      dut.io.protocolError.expect(true.B)

      dut.io.input.valid.poke(false.B)
      dut.io.clearProtocolError.poke(true.B)
      dut.clock.step()
      dut.io.protocolError.expect(false.B)
    }
  }

  "HjpegAxiStreamCore should recover after an unsupported input frame is discarded" in {
    simulate(new HjpegAxiStreamCore()) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)

      pokeConfig(dut, width = 0, height = 8)
      dut.io.clearProtocolError.poke(false.B)
      dut.io.output.ready.poke(true.B)
      dut.io.input.valid.poke(true.B)
      dut.io.input.bits.keep.poke(7.U)
      dut.io.input.bits.data.poke(0.U)
      dut.io.input.bits.last.poke(true.B)
      dut.io.input.ready.expect(true.B)
      dut.clock.step()

      dut.io.protocolError.expect(true.B)
      dut.io.busy.expect(false.B)
      dut.io.output.valid.expect(false.B)

      dut.io.input.valid.poke(false.B)
      dut.io.clearProtocolError.poke(true.B)
      dut.clock.step()
      dut.io.clearProtocolError.poke(false.B)
      dut.io.protocolError.expect(false.B)
      dut.io.busy.expect(false.B)

      pokeConfig(dut, width = 8, height = 8)
      for (index <- 0 until 8 * 8) {
        pushPixel(dut, index, last = index == 8 * 8 - 1)
      }
      dut.io.input.valid.poke(false.B)

      val bytes = collectFrame(dut, JpegHeaderBytes.HeaderLength + 128)

      bytes.take(2) mustBe Seq(0xff, 0xd8)
      bytes.takeRight(2) mustBe Seq(0xff, 0xd9)
      val image = ImageIO.read(new ByteArrayInputStream(bytes.map(_.toByte).toArray))
      image must not be null
      image.getWidth mustBe 8
      image.getHeight mustBe 8
      dut.io.protocolError.expect(false.B)
    }
  }

  "HjpegAxiStreamCore should drain extra beats after a missing final TLAST" in {
    simulate(new HjpegAxiStreamCore()) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)

      pokeConfig(dut, width = 1, height = 1)
      dut.io.clearProtocolError.poke(false.B)
      dut.io.output.ready.poke(false.B)

      dut.io.input.valid.poke(true.B)
      dut.io.input.bits.keep.poke(7.U)
      dut.io.input.bits.data.poke(0.U)
      dut.io.input.bits.last.poke(false.B)
      dut.io.input.ready.expect(true.B)
      dut.clock.step()
      dut.io.protocolError.expect(true.B)
      dut.io.busy.expect(true.B)

      dut.io.input.bits.last.poke(true.B)
      dut.io.input.ready.expect(true.B)
      dut.clock.step()
      dut.io.input.valid.poke(false.B)
      dut.io.protocolError.expect(true.B)

      dut.io.output.ready.poke(true.B)
      val firstBytes = collectFrame(dut, JpegHeaderBytes.HeaderLength + 128)
      firstBytes.take(2) mustBe Seq(0xff, 0xd8)
      firstBytes.takeRight(2) mustBe Seq(0xff, 0xd9)

      dut.io.clearProtocolError.poke(true.B)
      dut.clock.step()
      dut.io.clearProtocolError.poke(false.B)
      dut.io.protocolError.expect(false.B)

      pokeConfig(dut, width = 8, height = 8)
      for (index <- 0 until 8 * 8) {
        pushPixel(dut, index, last = index == 8 * 8 - 1)
      }
      dut.io.input.valid.poke(false.B)

      val recoveredBytes = collectFrame(dut, JpegHeaderBytes.HeaderLength + 128)
      recoveredBytes.take(2) mustBe Seq(0xff, 0xd8)
      recoveredBytes.takeRight(2) mustBe Seq(0xff, 0xd9)
      val image = ImageIO.read(new ByteArrayInputStream(recoveredBytes.map(_.toByte).toArray))
      image must not be null
      image.getWidth mustBe 8
      image.getHeight mustBe 8
      dut.io.protocolError.expect(false.B)
    }
  }

  "HjpegAxiStreamCore should drain unsupported input frames until TLAST" in {
    simulate(new HjpegAxiStreamCore()) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)

      pokeConfig(dut, width = 0, height = 8)
      dut.io.clearProtocolError.poke(false.B)
      dut.io.output.ready.poke(true.B)
      dut.io.input.valid.poke(true.B)
      dut.io.input.bits.keep.poke(7.U)
      dut.io.input.bits.data.poke(0.U)

      dut.io.input.bits.last.poke(false.B)
      dut.io.input.ready.expect(true.B)
      dut.clock.step()
      dut.io.protocolError.expect(true.B)
      dut.io.busy.expect(true.B)
      dut.io.output.valid.expect(false.B)

      dut.io.input.bits.last.poke(false.B)
      dut.io.input.ready.expect(true.B)
      dut.clock.step()
      dut.io.protocolError.expect(true.B)
      dut.io.busy.expect(true.B)
      dut.io.output.valid.expect(false.B)

      dut.io.input.bits.last.poke(true.B)
      dut.io.input.ready.expect(true.B)
      dut.clock.step()
      dut.io.protocolError.expect(true.B)
      dut.io.busy.expect(false.B)
      dut.io.output.valid.expect(false.B)

      dut.io.input.valid.poke(false.B)
      dut.io.clearProtocolError.poke(true.B)
      dut.clock.step()
      dut.io.clearProtocolError.poke(false.B)
      dut.io.protocolError.expect(false.B)
      dut.io.busy.expect(false.B)

      pokeConfig(dut, width = 8, height = 8)
      for (index <- 0 until 8 * 8) {
        pushPixel(dut, index, last = index == 8 * 8 - 1)
      }
      dut.io.input.valid.poke(false.B)

      val bytes = collectFrame(dut, JpegHeaderBytes.HeaderLength + 128)

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
