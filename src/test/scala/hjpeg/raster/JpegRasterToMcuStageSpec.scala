// See README.md for license details.

package hjpeg

import chisel3._
import chisel3.simulator.scalatest.ChiselSim
import org.scalatest.freespec.AnyFreeSpec
import org.scalatest.matchers.must.Matchers

class JpegRasterToMcuStageSpec extends AnyFreeSpec with Matchers with ChiselSim {
  private val testConfig = HjpegConfig(maxFrameWidth = 16, maxFrameHeight = 8)

  private def pokeConfig(dut: JpegRasterToMcuStage): Unit = {
    dut.io.config.xsize.poke(16.U)
    dut.io.config.ysize.poke(8.U)
    dut.io.config.quality.poke(50.U)
    dut.io.config.restartInterval.poke(0.U)
    dut.io.config.enableChromaSubsample.poke(false.B)
    dut.io.config.emitJfif.poke(true.B)
  }

  private def pokeCustomConfig(dut: JpegRasterToMcuStage, width: Int, height: Int): Unit = {
    dut.io.config.xsize.poke(width.U)
    dut.io.config.ysize.poke(height.U)
    dut.io.config.quality.poke(50.U)
    dut.io.config.restartInterval.poke(0.U)
    dut.io.config.enableChromaSubsample.poke(false.B)
    dut.io.config.emitJfif.poke(true.B)
  }

  private def pushPixel(dut: JpegRasterToMcuStage, index: Int, width: Int = 16, grayForX: Int => Int = x => if (x < 8) 128 else 160): Unit = {
    val x = index % width
    val y = index / width
    val gray = grayForX(x)
    dut.io.input.valid.poke(true.B)
    dut.io.input.bits.x.poke(x.U)
    dut.io.input.bits.y.poke(y.U)
    dut.io.input.bits.r.poke(gray.U)
    dut.io.input.bits.g.poke(gray.U)
    dut.io.input.bits.b.poke(gray.U)
    dut.io.input.ready.expect(true.B)
    dut.clock.step()
  }

  private def expectFlatMcu(dut: JpegRasterToMcuStage, yDc: Int, last: Boolean): Unit = {
    dut.io.output.valid.expect(true.B)
    dut.io.output.bits.last.expect(last.B)
    dut.io.output.bits.mcu.y.coefficients(0).expect(yDc.S)
    dut.io.output.bits.mcu.cb.coefficients(0).expect(0.S)
    dut.io.output.bits.mcu.cr.coefficients(0).expect(0.S)
    for (index <- 1 until HjpegConstants.BlockSize) {
      dut.io.output.bits.mcu.y.coefficients(index).expect(0.S)
      dut.io.output.bits.mcu.cb.coefficients(index).expect(0.S)
      dut.io.output.bits.mcu.cr.coefficients(index).expect(0.S)
    }
  }

  private def waitForOutput(dut: JpegRasterToMcuStage, maxCycles: Int = 10000): Int = {
    var cycles = 0
    while (!dut.io.output.valid.peek().litToBoolean) {
      assert(cycles < maxCycles, "timeout waiting for raster-to-MCU output")
      dut.clock.step()
      cycles += 1
    }
    cycles
  }

  "JpegRasterToMcuStage should emit left-to-right MCUs from one raster stripe" in {
    simulate(new JpegRasterToMcuStage(testConfig)) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)

      pokeConfig(dut)
      dut.io.output.ready.poke(true.B)
      for (index <- 0 until 16 * 8) {
        pushPixel(dut, index)
      }
      dut.io.input.valid.poke(false.B)

      val firstMcuCycles = waitForOutput(dut)
      info(s"4:4:4 first-MCU processing latency after stripe collection: $firstMcuCycles cycles")
      firstMcuCycles must be <= 5000
      dut.io.output.valid.expect(true.B)
      dut.io.output.bits.last.expect(false.B)
      for (index <- 0 until HjpegConstants.BlockSize) {
        dut.io.output.bits.mcu.y.coefficients(index).expect(0.S)
        dut.io.output.bits.mcu.cb.coefficients(index).expect(0.S)
        dut.io.output.bits.mcu.cr.coefficients(index).expect(0.S)
      }
      dut.clock.step()

      val secondMcuCycles = waitForOutput(dut)
      secondMcuCycles must be <= 5000
      dut.io.output.valid.expect(true.B)
      dut.io.output.bits.last.expect(true.B)
      dut.io.output.bits.mcu.y.coefficients(0).expect(16.S)
      dut.io.output.bits.mcu.cb.coefficients(0).expect(0.S)
      dut.io.output.bits.mcu.cr.coefficients(0).expect(0.S)
      for (index <- 1 until HjpegConstants.BlockSize) {
        dut.io.output.bits.mcu.y.coefficients(index).expect(0.S)
        dut.io.output.bits.mcu.cb.coefficients(index).expect(0.S)
        dut.io.output.bits.mcu.cr.coefficients(index).expect(0.S)
      }
    }
  }

  "JpegRasterToMcuStage should pad partial right and bottom edges" in {
    simulate(new JpegRasterToMcuStage(testConfig)) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)

      pokeCustomConfig(dut, width = 10, height = 10)
      dut.io.output.ready.poke(true.B)

      for (index <- 0 until 10 * 8) {
        pushPixel(dut, index, width = 10, grayForX = _ => 128)
      }
      dut.io.input.valid.poke(false.B)

      waitForOutput(dut)
      expectFlatMcu(dut, yDc = 0, last = false)
      dut.clock.step()
      waitForOutput(dut)
      expectFlatMcu(dut, yDc = 0, last = false)
      dut.clock.step()

      for (index <- 10 * 8 until 10 * 10) {
        pushPixel(dut, index, width = 10, grayForX = _ => 128)
      }
      dut.io.input.valid.poke(false.B)

      waitForOutput(dut)
      expectFlatMcu(dut, yDc = 0, last = false)
      dut.clock.step()
      waitForOutput(dut)
      expectFlatMcu(dut, yDc = 0, last = true)
    }
  }

  "JpegRasterToMcuStage should hold an emitted MCU under backpressure" in {
    simulate(new JpegRasterToMcuStage(testConfig)) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)

      pokeConfig(dut)
      dut.io.output.ready.poke(false.B)
      for (index <- 0 until 16 * 8) {
        pushPixel(dut, index)
      }
      dut.io.input.valid.poke(false.B)

      waitForOutput(dut)
      dut.io.output.valid.expect(true.B)
      dut.io.output.bits.last.expect(false.B)
      dut.io.input.ready.expect(false.B)
      dut.clock.step()
      dut.io.output.valid.expect(true.B)
      dut.io.output.bits.last.expect(false.B)
    }
  }
}
