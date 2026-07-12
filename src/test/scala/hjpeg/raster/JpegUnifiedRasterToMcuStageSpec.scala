// See README.md for license details.

package hjpeg

import chisel3._
import chisel3.simulator.scalatest.ChiselSim
import org.scalatest.freespec.AnyFreeSpec
import org.scalatest.matchers.must.Matchers

class JpegUnifiedRasterToMcuStageSpec extends AnyFreeSpec with Matchers with ChiselSim {
  private val testConfig = HjpegConfig(maxFrameWidth = 20, maxFrameHeight = 20)

  private def pokeConfig(
      dut: JpegUnifiedRasterToMcuStage,
      width: Int,
      height: Int,
      subsampled: Boolean): Unit = {
    dut.io.config.xsize.poke(width.U)
    dut.io.config.ysize.poke(height.U)
    dut.io.config.quality.poke(50.U)
    dut.io.config.restartInterval.poke(0.U)
    dut.io.config.enableChromaSubsample.poke(subsampled.B)
    dut.io.config.emitJfif.poke(true.B)
  }

  private def pushPixel(
      dut: JpegUnifiedRasterToMcuStage,
      index: Int,
      width: Int,
      gray: Int): Unit = {
    dut.io.input.valid.poke(true.B)
    dut.io.input.bits.x.poke((index % width).U)
    dut.io.input.bits.y.poke((index / width).U)
    dut.io.input.bits.r.poke(gray.U)
    dut.io.input.bits.g.poke(gray.U)
    dut.io.input.bits.b.poke(gray.U)
    dut.io.input.ready.expect(true.B)
    dut.clock.step()
  }

  private def waitForOutput(dut: JpegUnifiedRasterToMcuStage, maxCycles: Int = 10000): Int = {
    var cycles = 0
    while (!dut.io.output.valid.peek().litToBoolean) {
      assert(cycles < maxCycles, "timeout waiting for unified raster-to-MCU output")
      dut.clock.step()
      cycles += 1
    }
    cycles
  }

  private def expectFlatBlock(block: ZigZagCoefficientBlock, dc: Int): Unit = {
    block.coefficients(0).expect(dc.S)
    for (index <- 1 until HjpegConstants.BlockSize) {
      block.coefficients(index).expect(0.S)
    }
  }

  "JpegUnifiedRasterToMcuStage should emit two ordered 4:4:4 stripes from one shared band" in {
    simulate(new JpegUnifiedRasterToMcuStage(testConfig)) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)
      pokeConfig(dut, width = 16, height = 16, subsampled = false)
      dut.io.output.ready.poke(true.B)

      for (index <- 0 until 16 * 16) {
        pushPixel(dut, index, width = 16, gray = if (index / 16 < 8) 128 else 160)
      }
      dut.io.input.valid.poke(false.B)

      val expectedDc = Seq(0, 0, 16, 16)
      for ((dc, mcu) <- expectedDc.zipWithIndex) {
        val cycles = waitForOutput(dut)
        cycles must be <= 145
        dut.io.output.bits.mcu.yBlockCount.expect(1.U)
        dut.io.output.bits.last.expect((mcu == expectedDc.size - 1).B)
        expectFlatBlock(dut.io.output.bits.mcu.y, dc)
        expectFlatBlock(dut.io.output.bits.mcu.cb, 0)
        expectFlatBlock(dut.io.output.bits.mcu.cr, 0)
        dut.clock.step()
      }
    }
  }

  "JpegUnifiedRasterToMcuStage should emit padded 4:2:0 MCUs from the same storage" in {
    simulate(new JpegUnifiedRasterToMcuStage(testConfig)) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)
      pokeConfig(dut, width = 17, height = 13, subsampled = true)
      dut.io.output.ready.poke(true.B)

      for (index <- 0 until 17 * 13) {
        pushPixel(dut, index, width = 17, gray = 128)
      }
      dut.io.input.valid.poke(false.B)

      for (mcu <- 0 until 2) {
        val cycles = waitForOutput(dut)
        cycles must be <= 270
        dut.io.output.bits.mcu.yBlockCount.expect(4.U)
        dut.io.output.bits.last.expect((mcu == 1).B)
        expectFlatBlock(dut.io.output.bits.mcu.y, 0)
        expectFlatBlock(dut.io.output.bits.mcu.y1, 0)
        expectFlatBlock(dut.io.output.bits.mcu.y2, 0)
        expectFlatBlock(dut.io.output.bits.mcu.y3, 0)
        expectFlatBlock(dut.io.output.bits.mcu.cb, 0)
        expectFlatBlock(dut.io.output.bits.mcu.cr, 0)
        dut.clock.step()
      }
    }
  }
}
