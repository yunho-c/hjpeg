// See README.md for license details.

package hjpeg

import chisel3._
import chisel3.simulator.scalatest.ChiselSim
import org.scalatest.freespec.AnyFreeSpec
import org.scalatest.matchers.must.Matchers

class JpegHeaderStageSpec extends AnyFreeSpec with Matchers with ChiselSim {
  private def emitHeader(
      dut: JpegHeaderStage,
      width: Int,
      height: Int,
      quality: Int,
      subsample: Boolean = false,
      restartInterval: Int = 0,
      emitJfif: Boolean = true): Seq[Int] = {
    dut.reset.poke(true.B)
    dut.clock.step()
    dut.reset.poke(false.B)

    dut.io.config.xsize.poke(width.U)
    dut.io.config.ysize.poke(height.U)
    dut.io.config.quality.poke(quality.U)
    dut.io.config.restartInterval.poke(restartInterval.U)
    dut.io.config.enableChromaSubsample.poke(subsample.B)
    dut.io.config.emitJfif.poke(emitJfif.B)
    dut.io.output.ready.poke(true.B)

    dut.io.start.poke(true.B)
    dut.clock.step()
    dut.io.start.poke(false.B)

    val bytes = scala.collection.mutable.ArrayBuffer.empty[Int]
    var sawLast = false
    var cycles = 0

    while (!sawLast) {
      assert(cycles <= JpegHeaderBytes.MaxHeaderLength + 4, "timeout waiting for JPEG header")
      dut.io.output.valid.expect(true.B)
      bytes += dut.io.output.bits.byte.peek().litValue.toInt
      sawLast = dut.io.output.bits.last.peek().litToBoolean
      dut.clock.step()
      cycles += 1
    }

    bytes.toSeq
  }

  "JpegHeaderStage should emit baseline JPEG markers through SOS" in {
    simulate(new JpegHeaderStage()) { dut =>
      val bytes = emitHeader(dut, width = 320, height = 240, quality = 50)

      bytes.length mustBe JpegHeaderBytes.HeaderLength
      bytes.take(2) mustBe Seq(0xff, 0xd8)
      bytes.slice(2, 20) mustBe JpegHeaderBytes.App0
      bytes.slice(JpegHeaderBytes.Sof0Start, JpegHeaderBytes.Sof0Start + 2) mustBe Seq(0xff, 0xc0)
      bytes.takeRight(JpegHeaderBytes.Sos.length) mustBe JpegHeaderBytes.Sos
    }
  }

  "JpegHeaderStage should insert frame dimensions in SOF0" in {
    simulate(new JpegHeaderStage()) { dut =>
      val bytes = emitHeader(dut, width = 640, height = 480, quality = 75)

      bytes(JpegHeaderBytes.Sof0HeightHigh) mustBe 0x01
      bytes(JpegHeaderBytes.Sof0HeightLow) mustBe 0xe0
      bytes(JpegHeaderBytes.Sof0WidthHigh) mustBe 0x02
      bytes(JpegHeaderBytes.Sof0WidthLow) mustBe 0x80
    }
  }

  "JpegHeaderStage should omit JFIF APP0 when disabled" in {
    simulate(new JpegHeaderStage()) { dut =>
      val bytes = emitHeader(dut, width = 8, height = 8, quality = 50, emitJfif = false)

      bytes.length mustBe JpegHeaderBytes.HeaderLength - JpegHeaderBytes.App0.length
      bytes.take(2) mustBe Seq(0xff, 0xd8)
      bytes.slice(2, 4) mustBe Seq(0xff, 0xdb)
      bytes.sliding(2).exists(_ == Seq(0xff, 0xe0)) mustBe false
    }
  }

  "JpegHeaderStage should emit 4:2:0 luminance sampling when chroma subsampling is enabled" in {
    simulate(new JpegHeaderStage()) { dut =>
      val bytes = emitHeader(dut, width = 17, height = 13, quality = 50, subsample = true)

      bytes(JpegHeaderBytes.Sof0LuminanceSamplingFactor) mustBe 0x22
    }
  }

  "JpegHeaderStage should emit quality-scaled DQT payloads in zig-zag order" in {
    simulate(new JpegHeaderStage()) { dut =>
      val bytes = emitHeader(dut, width = 8, height = 8, quality = 50)

      val luminanceDqt = bytes.slice(
        JpegHeaderBytes.DqtLuminanceDataStart,
        JpegHeaderBytes.DqtLuminanceDataStart + HjpegConstants.BlockSize
      )
      val chrominanceDqt = bytes.slice(
        JpegHeaderBytes.DqtChrominanceDataStart,
        JpegHeaderBytes.DqtChrominanceDataStart + HjpegConstants.BlockSize
      )

      luminanceDqt mustBe JpegTables.ZigZagOrder.map(JpegTables.StandardLuminanceQuant)
      chrominanceDqt mustBe JpegTables.ZigZagOrder.map(JpegTables.StandardChrominanceQuant)
    }
  }

  "JpegHeaderStage should emit four standard DHT segments" in {
    simulate(new JpegHeaderStage()) { dut =>
      val bytes = emitHeader(dut, width = 8, height = 8, quality = 50)
      val dhtStart = JpegHeaderBytes.Sof0Start + JpegHeaderBytes.Sof0Prefix.length

      bytes.slice(dhtStart, dhtStart + 5) mustBe Seq(0xff, 0xc4, 0x00, 0x1f, 0x00)
      val dcChrominanceStart = dhtStart + 33
      bytes.slice(dcChrominanceStart, dcChrominanceStart + 5) mustBe Seq(0xff, 0xc4, 0x00, 0x1f, 0x01)
      val acLuminanceStart = dcChrominanceStart + 33
      bytes.slice(acLuminanceStart, acLuminanceStart + 5) mustBe Seq(0xff, 0xc4, 0x00, 0xb5, 0x10)
      val acChrominanceStart = acLuminanceStart + 183
      bytes.slice(acChrominanceStart, acChrominanceStart + 5) mustBe Seq(0xff, 0xc4, 0x00, 0xb5, 0x11)
    }
  }

  "JpegHeaderStage should emit DRI before SOS when restart intervals are enabled" in {
    simulate(new JpegHeaderStage()) { dut =>
      val bytes = emitHeader(dut, width = 16, height = 8, quality = 50, restartInterval = 2)

      bytes.length mustBe JpegHeaderBytes.MaxHeaderLength
      bytes.slice(JpegHeaderBytes.DriStart, JpegHeaderBytes.DriStart + JpegHeaderBytes.Dri.length) mustBe
        Seq(0xff, 0xdd, 0x00, 0x04, 0x00, 0x02)
      bytes.slice(
        JpegHeaderBytes.DriStart + JpegHeaderBytes.Dri.length,
        JpegHeaderBytes.DriStart + JpegHeaderBytes.Dri.length + JpegHeaderBytes.Sos.length
      ) mustBe JpegHeaderBytes.Sos
    }
  }

  "JpegHeaderStage should hold output under backpressure" in {
    simulate(new JpegHeaderStage()) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)

      dut.io.config.xsize.poke(8.U)
      dut.io.config.ysize.poke(8.U)
      dut.io.config.quality.poke(50.U)
      dut.io.config.restartInterval.poke(0.U)
      dut.io.config.enableChromaSubsample.poke(false.B)
      dut.io.config.emitJfif.poke(true.B)
      dut.io.output.ready.poke(false.B)
      dut.io.start.poke(true.B)
      dut.clock.step()
      dut.io.start.poke(false.B)

      dut.io.output.valid.expect(true.B)
      dut.io.output.bits.byte.expect(0xff.U)
      dut.io.busy.expect(true.B)
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
