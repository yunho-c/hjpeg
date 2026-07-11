// See README.md for license details.

package hjpeg

import chisel3._
import chisel3.simulator.scalatest.ChiselSim
import org.scalatest.freespec.AnyFreeSpec
import org.scalatest.matchers.must.Matchers

class JpegMcuStreamEncoderStageSpec extends AnyFreeSpec with Matchers with ChiselSim {
  private def pokeConfig(
      dut: JpegMcuStreamEncoderStage,
      width: Int = 16,
      height: Int = 8,
      restartInterval: Int = 0): Unit = {
    dut.io.config.xsize.poke(width.U)
    dut.io.config.ysize.poke(height.U)
    dut.io.config.quality.poke(50.U)
    dut.io.config.restartInterval.poke(restartInterval.U)
    dut.io.config.enableChromaSubsample.poke(false.B)
    dut.io.config.emitJfif.poke(true.B)
  }

  private def pokeMcu(dut: JpegMcuStreamEncoderStage, yDc: Int, last: Boolean): Unit = {
    dut.io.input.bits.last.poke(last.B)
    dut.io.input.bits.mcu.yBlockCount.poke(1.U)
    for (index <- 0 until HjpegConstants.BlockSize) {
      dut.io.input.bits.mcu.y.coefficients(index).poke(0.S)
      dut.io.input.bits.mcu.y1.coefficients(index).poke(0.S)
      dut.io.input.bits.mcu.y2.coefficients(index).poke(0.S)
      dut.io.input.bits.mcu.y3.coefficients(index).poke(0.S)
      dut.io.input.bits.mcu.cb.coefficients(index).poke(0.S)
      dut.io.input.bits.mcu.cr.coefficients(index).poke(0.S)
    }
    dut.io.input.bits.mcu.y.coefficients(0).poke(yDc.S)
  }

  private def emitMcus(dut: JpegMcuStreamEncoderStage, yDcs: Seq[Int], restartInterval: Int = 0): Seq[Int] = {
    dut.reset.poke(true.B)
    dut.clock.step()
    dut.reset.poke(false.B)

    pokeConfig(dut, restartInterval = restartInterval)
    dut.io.output.ready.poke(true.B)
    dut.io.input.valid.poke(false.B)

    val bytes = scala.collection.mutable.ArrayBuffer.empty[Int]
    var nextMcu = 0
    var sawLast = false
    var cycles = 0

    while (!sawLast) {
      assert(cycles < JpegHeaderBytes.MaxHeaderLength + 384, "timeout waiting for MCU stream JPEG output")

      if (dut.io.output.valid.peek().litToBoolean) {
        bytes += dut.io.output.bits.byte.peek().litValue.toInt
        sawLast = dut.io.output.bits.last.peek().litToBoolean
      }

      if (nextMcu < yDcs.length && dut.io.input.ready.peek().litToBoolean) {
        pokeMcu(dut, yDcs(nextMcu), last = nextMcu == yDcs.length - 1)
        dut.io.input.valid.poke(true.B)
        nextMcu += 1
      } else {
        dut.io.input.valid.poke(false.B)
      }

      dut.clock.step()
      cycles += 1
    }

    bytes.toSeq
  }

  "JpegMcuStreamEncoderStage should emit one JPEG stream for two zero MCUs" in {
    simulate(new JpegMcuStreamEncoderStage()) { dut =>
      val bytes = emitMcus(dut, Seq(0, 0))

      bytes.length mustBe JpegHeaderBytes.HeaderLength + 6
      bytes.take(2) mustBe Seq(0xff, 0xd8)
      bytes.slice(JpegHeaderBytes.HeaderLength, JpegHeaderBytes.HeaderLength + 4) mustBe
        Seq(0x28, 0x00, 0xa0, 0x0f)
      bytes.takeRight(2) mustBe Seq(0xff, 0xd9)
      bytes.sliding(2).count(_ == Seq(0xff, 0xd8)) mustBe 1
      bytes.sliding(2).count(_ == Seq(0xff, 0xd9)) mustBe 1
    }
  }

  "JpegMcuStreamEncoderStage should carry DC predictors across MCUs" in {
    simulate(new JpegMcuStreamEncoderStage()) { dut =>
      val repeatedDcBytes = emitMcus(dut, Seq(4, 4))

      repeatedDcBytes.slice(JpegHeaderBytes.HeaderLength, JpegHeaderBytes.HeaderLength + 4) mustBe
        Seq(0x92, 0x80, 0x0a, 0x00)
      repeatedDcBytes.takeRight(2) mustBe Seq(0xff, 0xd9)
    }
  }

  "JpegMcuStreamEncoderStage should emit restart markers and reset DC predictors" in {
    simulate(new JpegMcuStreamEncoderStage()) { dut =>
      val bytes = emitMcus(dut, Seq(4, 4), restartInterval = 1)

      bytes.slice(JpegHeaderBytes.DriStart, JpegHeaderBytes.DriStart + JpegHeaderBytes.Dri.length) mustBe
        Seq(0xff, 0xdd, 0x00, 0x04, 0x00, 0x01)
      bytes.sliding(2).count(_ == Seq(0xff, 0xd0)) mustBe 1
      bytes.takeRight(2) mustBe Seq(0xff, 0xd9)

      val entropyStart = JpegHeaderBytes.MaxHeaderLength
      val restartIndex = bytes.sliding(2).indexWhere(_ == Seq(0xff, 0xd0))
      restartIndex must be > entropyStart

      val firstEntropyChunk = bytes.slice(entropyStart, restartIndex)
      val secondEntropyChunk = bytes.slice(restartIndex + 2, bytes.length - 2)
      firstEntropyChunk mustBe secondEntropyChunk
    }
  }

  "JpegMcuStreamEncoderStage should cycle restart marker numbers" in {
    simulate(new JpegMcuStreamEncoderStage()) { dut =>
      val bytes = emitMcus(dut, Seq.fill(10)(0), restartInterval = 1)

      val restartMarkers = bytes.sliding(2).collect {
        case Seq(0xff, marker) if marker >= 0xd0 && marker <= 0xd7 => marker
      }.toSeq

      restartMarkers mustBe Seq(
        0xd0, 0xd1, 0xd2, 0xd3, 0xd4, 0xd5, 0xd6, 0xd7, 0xd0)
      bytes.takeRight(2) mustBe Seq(0xff, 0xd9)
    }
  }

  "JpegMcuStreamEncoderStage should hold the first header byte under backpressure" in {
    simulate(new JpegMcuStreamEncoderStage()) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)

      pokeConfig(dut)
      pokeMcu(dut, yDc = 0, last = true)
      dut.io.output.ready.poke(false.B)
      dut.io.input.valid.poke(true.B)
      dut.clock.step()
      dut.io.input.valid.poke(false.B)

      dut.io.output.valid.expect(true.B)
      dut.io.output.bits.byte.expect(0xff.U)
      dut.clock.step()
      dut.io.output.valid.expect(true.B)
      dut.io.output.bits.byte.expect(0xff.U)
    }
  }
}
