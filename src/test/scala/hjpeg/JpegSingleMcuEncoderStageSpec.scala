// See README.md for license details.

package hjpeg

import chisel3._
import chisel3.simulator.scalatest.ChiselSim
import org.scalatest.freespec.AnyFreeSpec
import org.scalatest.matchers.must.Matchers

class JpegSingleMcuEncoderStageSpec extends AnyFreeSpec with Matchers with ChiselSim {
  private def pokeConfig(dut: JpegSingleMcuEncoderStage, width: Int = 8, height: Int = 8): Unit = {
    dut.io.config.xsize.poke(width.U)
    dut.io.config.ysize.poke(height.U)
    dut.io.config.quality.poke(50.U)
    dut.io.config.restartInterval.poke(0.U)
    dut.io.config.enableChromaSubsample.poke(false.B)
    dut.io.config.emitJfif.poke(true.B)
  }

  private def pokeZeroMcu(dut: JpegSingleMcuEncoderStage): Unit = {
    dut.io.input.bits.yBlockCount.poke(1.U)
    for (index <- 0 until HjpegConstants.BlockSize) {
      dut.io.input.bits.y.coefficients(index).poke(0.S)
      dut.io.input.bits.y1.coefficients(index).poke(0.S)
      dut.io.input.bits.y2.coefficients(index).poke(0.S)
      dut.io.input.bits.y3.coefficients(index).poke(0.S)
      dut.io.input.bits.cb.coefficients(index).poke(0.S)
      dut.io.input.bits.cr.coefficients(index).poke(0.S)
    }
  }

  private def emitZeroMcu(dut: JpegSingleMcuEncoderStage): Seq[Int] = {
    dut.reset.poke(true.B)
    dut.clock.step()
    dut.reset.poke(false.B)

    pokeConfig(dut)
    pokeZeroMcu(dut)
    dut.io.output.ready.poke(true.B)
    dut.io.input.valid.poke(true.B)
    dut.io.input.ready.expect(true.B)
    dut.clock.step()
    dut.io.input.valid.poke(false.B)

    val bytes = scala.collection.mutable.ArrayBuffer.empty[Int]
    var sawLast = false
    var cycles = 0
    while (!sawLast) {
      assert(cycles < JpegHeaderBytes.HeaderLength + 8192, "timeout waiting for single-MCU JPEG output")
      if (dut.io.output.valid.peek().litToBoolean) {
        bytes += dut.io.output.bits.byte.peek().litValue.toInt
        sawLast = dut.io.output.bits.last.peek().litToBoolean
      }
      dut.clock.step()
      cycles += 1
    }

    bytes.toSeq
  }

  "JpegSingleMcuEncoderStage should emit a complete JPEG byte stream for one zero MCU" in {
    simulate(new JpegSingleMcuEncoderStage()) { dut =>
      val bytes = emitZeroMcu(dut)

      bytes.length mustBe JpegHeaderBytes.HeaderLength + 4
      bytes.take(2) mustBe Seq(0xff, 0xd8)
      bytes.slice(JpegHeaderBytes.HeaderLength - JpegHeaderBytes.Sos.length, JpegHeaderBytes.HeaderLength) mustBe
        JpegHeaderBytes.Sos
      bytes.slice(JpegHeaderBytes.HeaderLength, JpegHeaderBytes.HeaderLength + 2) mustBe Seq(0x28, 0x03)
      bytes.takeRight(2) mustBe Seq(0xff, 0xd9)
    }
  }

  "JpegSingleMcuEncoderStage should hold the first header byte under backpressure" in {
    simulate(new JpegSingleMcuEncoderStage()) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)

      pokeConfig(dut)
      pokeZeroMcu(dut)
      dut.io.output.ready.poke(false.B)
      dut.io.input.valid.poke(true.B)
      dut.clock.step()
      dut.io.input.valid.poke(false.B)

      dut.io.output.valid.expect(true.B)
      dut.io.output.bits.byte.expect(0xff.U)
      dut.clock.step()
      dut.io.output.valid.expect(true.B)
      dut.io.output.bits.byte.expect(0xff.U)

      dut.io.output.ready.poke(true.B)
      dut.clock.step()
      dut.io.output.valid.expect(true.B)
      dut.io.output.bits.byte.expect(0xd8.U)
    }
  }
}
