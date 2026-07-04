// See README.md for license details.

package hjpeg

import chisel3._
import chisel3.simulator.scalatest.ChiselSim
import java.io.ByteArrayInputStream
import javax.imageio.ImageIO
import org.scalatest.freespec.AnyFreeSpec
import org.scalatest.matchers.must.Matchers

class HjpegAxiStreamCoreSpec extends AnyFreeSpec with Matchers with ChiselSim {
  private def pokeConfig(dut: HjpegAxiStreamCore, width: Int = 8, height: Int = 8): Unit = {
    dut.io.config.xsize.poke(width.U)
    dut.io.config.ysize.poke(height.U)
    dut.io.config.quality.poke(50.U)
    dut.io.config.restartInterval.poke(0.U)
    dut.io.config.enableChromaSubsample.poke(false.B)
    dut.io.config.emitJfif.poke(true.B)
  }

  private def pushPixel(dut: HjpegAxiStreamCore, index: Int, last: Boolean): Unit = {
    val gray = BigInt(128) | (BigInt(128) << 8) | (BigInt(128) << 16)
    dut.io.input.valid.poke(true.B)
    dut.io.input.bits.keep.poke(7.U)
    dut.io.input.bits.data.poke(gray.U)
    dut.io.input.bits.last.poke(last.B)
    dut.io.input.ready.expect(true.B)
    dut.clock.step()
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

      val bytes = scala.collection.mutable.ArrayBuffer.empty[Int]
      var sawLast = false
      var cycles = 0
      while (!sawLast) {
        assert(cycles < JpegHeaderBytes.HeaderLength + 128, "timeout waiting for AXI JPEG output")
        if (dut.io.output.valid.peek().litToBoolean) {
          dut.io.output.bits.keep.expect(1.U)
          bytes += dut.io.output.bits.data.peek().litValue.toInt
          sawLast = dut.io.output.bits.last.peek().litToBoolean
        }
        dut.clock.step()
        cycles += 1
      }

      bytes.take(2) mustBe Seq(0xff, 0xd8)
      bytes.takeRight(2) mustBe Seq(0xff, 0xd9)
      val image = ImageIO.read(new ByteArrayInputStream(bytes.map(_.toByte).toArray))
      image must not be null
      image.getWidth mustBe 16
      image.getHeight mustBe 8
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
}
