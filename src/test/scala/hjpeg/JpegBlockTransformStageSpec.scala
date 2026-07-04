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

  "JpegBlockTransformStage should transform a flat luminance block to one DC coefficient" in {
    simulate(new JpegBlockTransformStage()) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)

      dut.io.quality.poke(50.U)
      dut.io.isLuminance.poke(true.B)
      dut.io.input.valid.poke(true.B)
      dut.io.output.ready.poke(true.B)
      pokeConstantBlock(dut, 32)

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
      dut.io.input.valid.poke(true.B)
      dut.io.output.ready.poke(true.B)
      pokeConstantBlock(dut, 34)

      dut.io.output.valid.expect(true.B)
      dut.io.output.bits.coefficients(0).expect(16.S)
      for (index <- 1 until HjpegConstants.BlockSize) {
        dut.io.output.bits.coefficients(index).expect(0.S)
      }
    }
  }

  "JpegBlockTransformStage should propagate ready backpressure" in {
    simulate(new JpegBlockTransformStage()) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)

      dut.io.quality.poke(50.U)
      dut.io.isLuminance.poke(true.B)
      dut.io.input.valid.poke(true.B)
      pokeConstantBlock(dut, 0)

      dut.io.output.ready.poke(false.B)
      dut.io.input.ready.expect(false.B)
      dut.io.output.valid.expect(true.B)

      dut.io.output.ready.poke(true.B)
      dut.io.input.ready.expect(true.B)
    }
  }
}
