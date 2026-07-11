// See README.md for license details.

package hjpeg

import chisel3._
import chisel3.simulator.scalatest.ChiselSim
import org.scalatest.freespec.AnyFreeSpec
import org.scalatest.matchers.must.Matchers

class QuantizeBlockStageSpec extends AnyFreeSpec with Matchers with ChiselSim {
  private def clearBlock(dut: QuantizeBlockStage): Unit = {
    for (index <- 0 until HjpegConstants.BlockSize) {
      dut.io.input.bits.coefficients(index).poke(0.S)
    }
  }

  private def pushBlock(dut: QuantizeBlockStage): Unit = {
    dut.io.input.valid.poke(true.B)
    dut.io.input.ready.expect(true.B)
    dut.clock.step()
    dut.io.input.valid.poke(false.B)
  }

  private def waitForOutput(dut: QuantizeBlockStage, maxCycles: Int = 1400): Int = {
    var cycles = 0
    while (!dut.io.output.valid.peek().litToBoolean) {
      assert(cycles < maxCycles, "timeout waiting for quantize output")
      dut.clock.step()
      cycles += 1
    }
    cycles
  }

  "QuantizeBlockStage should quantize signed coefficients with luminance tables" in {
    simulate(new QuantizeBlockStage()) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)

      dut.io.quality.poke(50.U)
      dut.io.isLuminance.poke(true.B)
      dut.io.output.ready.poke(true.B)
      clearBlock(dut)

      dut.io.input.bits.coefficients(0).poke(16.S)
      dut.io.input.bits.coefficients(1).poke((-11).S)
      dut.io.input.bits.coefficients(2).poke(5.S)
      dut.io.input.bits.coefficients(3).poke(7.S)
      dut.io.input.bits.coefficients(4).poke((-36).S)

      pushBlock(dut)
      val cycles = waitForOutput(dut)
      info(s"64-coefficient quantizer latency: $cycles cycles")
      cycles must be <= 1280
      dut.io.output.valid.expect(true.B)
      dut.io.output.bits.coefficients(0).expect(1.S)
      dut.io.output.bits.coefficients(1).expect((-1).S)
      dut.io.output.bits.coefficients(2).expect(1.S)
      dut.io.output.bits.coefficients(3).expect(0.S)
      dut.io.output.bits.coefficients(4).expect((-2).S)
    }
  }

  "QuantizeBlockStage should use chrominance tables and quality scaling" in {
    simulate(new QuantizeBlockStage()) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)

      dut.io.output.ready.poke(true.B)

      dut.io.quality.poke(50.U)
      dut.io.isLuminance.poke(false.B)
      clearBlock(dut)
      dut.io.input.bits.coefficients(0).poke(34.S)
      pushBlock(dut)
      waitForOutput(dut)
      dut.io.output.bits.coefficients(0).expect(2.S)
      dut.clock.step()

      dut.io.quality.poke(100.U)
      dut.io.isLuminance.poke(true.B)
      clearBlock(dut)
      dut.io.input.bits.coefficients(0).poke((-7).S)
      pushBlock(dut)
      waitForOutput(dut)
      dut.io.output.bits.coefficients(0).expect((-7).S)
      dut.clock.step()

      dut.io.quality.poke(0.U)
      clearBlock(dut)
      dut.io.input.bits.coefficients(0).poke(510.S)
      pushBlock(dut)
      waitForOutput(dut)
      dut.io.output.bits.coefficients(0).expect(2.S)
    }
  }

  "QuantizeBlockStage should propagate ready backpressure" in {
    simulate(new QuantizeBlockStage()) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)

      dut.io.quality.poke(50.U)
      dut.io.isLuminance.poke(true.B)
      clearBlock(dut)
      pushBlock(dut)
      waitForOutput(dut)

      dut.io.output.ready.poke(false.B)
      dut.io.input.ready.expect(false.B)
      dut.io.output.valid.expect(true.B)

      dut.io.output.ready.poke(true.B)
      dut.clock.step()
      dut.io.input.ready.expect(true.B)
    }
  }
}
