// See README.md for license details.

package hjpeg

import chisel3._
import chisel3.simulator.scalatest.ChiselSim
import java.io.ByteArrayInputStream
import javax.imageio.ImageIO
import org.scalatest.freespec.AnyFreeSpec
import org.scalatest.matchers.must.Matchers

class JpegRgb8x8EncoderStageSpec extends AnyFreeSpec with Matchers with ChiselSim {
  private val OutputTimeoutCycles = JpegHeaderBytes.MaxHeaderLength + 20000

  private def pokeConfig(dut: JpegRgb8x8EncoderStage, width: Int = 8, height: Int = 8): Unit = {
    dut.io.config.xsize.poke(width.U)
    dut.io.config.ysize.poke(height.U)
    dut.io.config.quality.poke(50.U)
    dut.io.config.restartInterval.poke(0.U)
    dut.io.config.enableChromaSubsample.poke(false.B)
    dut.io.config.emitJfif.poke(true.B)
  }

  private def pushPixel(dut: JpegRgb8x8EncoderStage, index: Int, r: Int, g: Int, b: Int): Unit = {
    dut.io.input.valid.poke(true.B)
    dut.io.input.bits.x.poke((index % HjpegConstants.BlockDim).U)
    dut.io.input.bits.y.poke((index / HjpegConstants.BlockDim).U)
    dut.io.input.bits.r.poke(r.U)
    dut.io.input.bits.g.poke(g.U)
    dut.io.input.bits.b.poke(b.U)
    dut.io.input.ready.expect(true.B)
    dut.clock.step()
  }

  private def emitFlatBlock(dut: JpegRgb8x8EncoderStage, r: Int, g: Int, b: Int): Seq[Int] = {
    dut.reset.poke(true.B)
    dut.clock.step()
    dut.reset.poke(false.B)

    pokeConfig(dut)
    dut.io.output.ready.poke(true.B)
    for (index <- 0 until HjpegConstants.BlockSize) {
      pushPixel(dut, index, r, g, b)
    }
    dut.io.input.valid.poke(false.B)

    val bytes = scala.collection.mutable.ArrayBuffer.empty[Int]
    var sawLast = false
    var cycles = 0
    while (!sawLast) {
      assert(cycles < OutputTimeoutCycles, "timeout waiting for RGB 8x8 JPEG output")
      if (dut.io.output.valid.peek().litToBoolean) {
        bytes += dut.io.output.bits.byte.peek().litValue.toInt
        sawLast = dut.io.output.bits.last.peek().litToBoolean
      }
      dut.clock.step()
      cycles += 1
    }

    bytes.toSeq
  }

  "JpegRgb8x8EncoderStage should emit a complete JPEG for a neutral gray block" in {
    simulate(new JpegRgb8x8EncoderStage()) { dut =>
      val bytes = emitFlatBlock(dut, 128, 128, 128)

      bytes.length mustBe JpegHeaderBytes.HeaderLength + 4
      bytes.take(2) mustBe Seq(0xff, 0xd8)
      bytes.slice(JpegHeaderBytes.HeaderLength, JpegHeaderBytes.HeaderLength + 2) mustBe Seq(0x28, 0x03)
      bytes.takeRight(2) mustBe Seq(0xff, 0xd9)

      val image = ImageIO.read(new ByteArrayInputStream(bytes.map(_.toByte).toArray))
      image must not be null
      image.getWidth mustBe 8
      image.getHeight mustBe 8
    }
  }

  "JpegRgb8x8EncoderStage should hold the first output byte under backpressure" in {
    simulate(new JpegRgb8x8EncoderStage()) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)

      pokeConfig(dut)
      dut.io.output.ready.poke(false.B)
      for (index <- 0 until HjpegConstants.BlockSize) {
        pushPixel(dut, index, 128, 128, 128)
      }
      dut.io.input.valid.poke(false.B)

      dut.io.busy.expect(true.B)
      var cycles = 0
      while (!dut.io.output.valid.peek().litToBoolean) {
        assert(cycles < OutputTimeoutCycles, "timeout waiting for first output byte")
        dut.clock.step()
        cycles += 1
      }
      dut.io.output.valid.expect(true.B)
      dut.io.output.bits.byte.expect(0xff.U)
      dut.clock.step()
      dut.io.output.valid.expect(true.B)
      dut.io.output.bits.byte.expect(0xff.U)
    }
  }
}
