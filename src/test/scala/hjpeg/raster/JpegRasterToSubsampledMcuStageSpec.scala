// See README.md for license details.

package hjpeg

import chisel3._
import chisel3.simulator.scalatest.ChiselSim
import org.scalatest.freespec.AnyFreeSpec
import org.scalatest.matchers.must.Matchers

class JpegRasterToSubsampledMcuStageSpec extends AnyFreeSpec with Matchers with ChiselSim {
  private val testConfig = HjpegConfig(maxFrameWidth = 20, maxFrameHeight = 20)

  private def pokeConfig(dut: JpegRasterToSubsampledMcuStage, width: Int, height: Int): Unit = {
    dut.io.config.xsize.poke(width.U)
    dut.io.config.ysize.poke(height.U)
    dut.io.config.quality.poke(50.U)
    dut.io.config.restartInterval.poke(0.U)
    dut.io.config.enableChromaSubsample.poke(true.B)
    dut.io.config.emitJfif.poke(true.B)
  }

  private def pushPixel(dut: JpegRasterToSubsampledMcuStage, index: Int, width: Int, gray: Int = 128): Unit = {
    dut.io.input.valid.poke(true.B)
    dut.io.input.bits.x.poke((index % width).U)
    dut.io.input.bits.y.poke((index / width).U)
    dut.io.input.bits.r.poke(gray.U)
    dut.io.input.bits.g.poke(gray.U)
    dut.io.input.bits.b.poke(gray.U)
    dut.io.input.ready.expect(true.B)
    dut.clock.step()
  }

  private def expectFlatMcu(dut: JpegRasterToSubsampledMcuStage, last: Boolean, yDc: Int = 0): Unit = {
    dut.io.output.valid.expect(true.B)
    dut.io.output.bits.last.expect(last.B)
    dut.io.output.bits.mcu.yBlockCount.expect(4.U)
    for (index <- 0 until HjpegConstants.BlockSize) {
      val expectedY = if (index == 0) yDc else 0
      dut.io.output.bits.mcu.y.coefficients(index).expect(expectedY.S)
      dut.io.output.bits.mcu.y1.coefficients(index).expect(expectedY.S)
      dut.io.output.bits.mcu.y2.coefficients(index).expect(expectedY.S)
      dut.io.output.bits.mcu.y3.coefficients(index).expect(expectedY.S)
      dut.io.output.bits.mcu.cb.coefficients(index).expect(0.S)
      dut.io.output.bits.mcu.cr.coefficients(index).expect(0.S)
    }
  }

  private def waitForOutput(dut: JpegRasterToSubsampledMcuStage, maxCycles: Int = 18000): Int = {
    var cycles = 0
    while (!dut.io.output.valid.peek().litToBoolean) {
      assert(cycles < maxCycles, "timeout waiting for subsampled raster-to-MCU output")
      dut.clock.step()
      cycles += 1
    }
    cycles
  }

  "JpegRasterToSubsampledMcuStage should emit padded 4:2:0 MCUs" in {
    simulate(new JpegRasterToSubsampledMcuStage(testConfig)) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)

      pokeConfig(dut, width = 17, height = 13)
      dut.io.output.ready.poke(true.B)

      for (index <- 0 until 17 * 13) {
        pushPixel(dut, index, width = 17)
      }
      dut.io.input.valid.poke(false.B)

      val firstMcuCycles = waitForOutput(dut)
      info(s"4:2:0 first-MCU processing latency after band collection: $firstMcuCycles cycles")
      firstMcuCycles must be <= 270
      expectFlatMcu(dut, last = false)
      dut.clock.step()
      val secondMcuCycles = waitForOutput(dut)
      secondMcuCycles must be <= 270
      expectFlatMcu(dut, last = true)
    }
  }

  "JpegRasterToSubsampledMcuStage should preserve distinct horizontal MCUs" in {
    simulate(new JpegRasterToSubsampledMcuStage(testConfig)) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)

      pokeConfig(dut, width = 20, height = 16)
      dut.io.output.ready.poke(true.B)

      for (index <- 0 until 20 * 16) {
        val gray = if (index % 20 < 16) 128 else 160
        pushPixel(dut, index, width = 20, gray = gray)
      }
      dut.io.input.valid.poke(false.B)

      waitForOutput(dut)
      expectFlatMcu(dut, last = false)
      dut.clock.step()
      waitForOutput(dut)
      expectFlatMcu(dut, last = true, yDc = 16)
    }
  }

  "JpegRasterToSubsampledMcuStage should collect the next band while processing" in {
    val overlapConfig = HjpegConfig(maxFrameWidth = 16, maxFrameHeight = 32)
    simulate(new JpegRasterToSubsampledMcuStage(overlapConfig)) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)

      pokeConfig(dut, width = 16, height = 32)
      dut.io.output.ready.poke(false.B)

      for (index <- 0 until 16 * 16) {
        pushPixel(dut, index, width = 16, gray = 128)
      }
      for (index <- 16 * 16 until 16 * 32) {
        pushPixel(dut, index, width = 16, gray = 160)
      }
      dut.io.input.valid.poke(false.B)

      dut.io.output.ready.poke(true.B)
      waitForOutput(dut)
      expectFlatMcu(dut, last = false)
      dut.clock.step()
      waitForOutput(dut)
      expectFlatMcu(dut, last = true, yDc = 16)
    }
  }
}
