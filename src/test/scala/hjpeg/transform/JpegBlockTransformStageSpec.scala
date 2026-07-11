// See README.md for license details.

package hjpeg

import chisel3._
import chisel3.simulator.scalatest.ChiselSim
import org.scalatest.freespec.AnyFreeSpec
import org.scalatest.matchers.must.Matchers

class JpegBlockTransformStageSpec extends AnyFreeSpec with Matchers with ChiselSim {
  private def pokeConstantBlock(dut: JpegBlockTransformStage, value: Int): Unit = {
    for (index <- 0 until HjpegConstants.BlockSize) {
      dut.io.input.bits.samples(index).poke(value.S)
    }
  }

  private def pushConstantBlock(dut: JpegBlockTransformStage, value: Int): Unit = {
    dut.io.input.valid.poke(true.B)
    pokeConstantBlock(dut, value)
    dut.io.input.ready.expect(true.B)
    dut.clock.step()
    dut.io.input.valid.poke(false.B)
  }

  private def waitForOutput(dut: JpegBlockTransformStage, maxCycles: Int = 2600): Unit = {
    var cycles = 0
    while (!dut.io.output.valid.peek().litToBoolean) {
      assert(cycles < maxCycles, "timeout waiting for block transform output")
      dut.clock.step()
      cycles += 1
    }
  }

  "JpegBlockTransformStage should transform a flat luminance block to one DC coefficient" in {
    simulate(new JpegBlockTransformStage()) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)

      dut.io.quality.poke(50.U)
      dut.io.isLuminance.poke(true.B)
      dut.io.output.ready.poke(true.B)
      pushConstantBlock(dut, 32)

      waitForOutput(dut)
      dut.io.output.valid.expect(true.B)
      dut.io.output.bits.coefficients(0).expect(16.S)
      for (index <- 1 until HjpegConstants.BlockSize) {
        dut.io.output.bits.coefficients(index).expect(0.S)
      }
    }
  }

  "JpegBlockTransformStage should use chrominance quantization when requested" in {
    simulate(new JpegBlockTransformStage()) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)

      dut.io.quality.poke(50.U)
      dut.io.isLuminance.poke(false.B)
      dut.io.output.ready.poke(true.B)
      pushConstantBlock(dut, 34)

      waitForOutput(dut)
      dut.io.output.valid.expect(true.B)
      dut.io.output.bits.coefficients(0).expect(16.S)
      for (index <- 1 until HjpegConstants.BlockSize) {
        dut.io.output.bits.coefficients(index).expect(0.S)
      }
    }
  }

  "JpegBlockTransformStage should hold output while using available pipeline capacity" in {
    simulate(new JpegBlockTransformStage()) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)

      dut.io.quality.poke(50.U)
      dut.io.isLuminance.poke(true.B)
      dut.io.output.ready.poke(false.B)
      pushConstantBlock(dut, 0)
      waitForOutput(dut)

      dut.io.output.valid.expect(true.B)
      dut.io.output.bits.coefficients(0).expect(0.S)

      // The DCT can accept one more block while the first result is held at
      // the downstream quantize/zig-zag boundary.
      pushConstantBlock(dut, 16)
      dut.io.output.valid.expect(true.B)
      dut.io.output.bits.coefficients(0).expect(0.S)
      dut.clock.step(3)
      dut.io.output.valid.expect(true.B)
      dut.io.output.bits.coefficients(0).expect(0.S)

      dut.io.output.ready.poke(true.B)
      dut.clock.step()
      waitForOutput(dut)
      dut.io.output.bits.coefficients(0).expect(8.S)
    }
  }
}
