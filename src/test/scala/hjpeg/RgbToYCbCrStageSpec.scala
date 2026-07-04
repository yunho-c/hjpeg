// See README.md for license details.

package hjpeg

import chisel3._
import chisel3.simulator.scalatest.ChiselSim
import org.scalatest.freespec.AnyFreeSpec
import org.scalatest.matchers.must.Matchers

class RgbToYCbCrStageSpec extends AnyFreeSpec with Matchers with ChiselSim {
  "RgbToYCbCrStage should convert primary colors with Q8 JPEG coefficients" in {
    simulate(new RgbToYCbCrStage()) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)

      dut.io.output.ready.poke(true.B)
      dut.io.input.valid.poke(true.B)
      dut.io.input.bits.x.poke(3.U)
      dut.io.input.bits.y.poke(4.U)

      val cases = Seq(
        (0, 0, 0, 0, 128, 128),
        (255, 255, 255, 255, 128, 128),
        (255, 0, 0, 76, 85, 255),
        (0, 255, 0, 149, 43, 21),
        (0, 0, 255, 28, 255, 107),
        (128, 128, 128, 128, 128, 128)
      )

      for ((r, g, b, expectedY, expectedCb, expectedCr) <- cases) {
        dut.io.input.bits.r.poke(r.U)
        dut.io.input.bits.g.poke(g.U)
        dut.io.input.bits.b.poke(b.U)

        dut.io.output.valid.expect(true.B)
        dut.io.output.bits.x.expect(3.U)
        dut.io.output.bits.y.expect(4.U)
        dut.io.output.bits.yComponent.expect(expectedY.U)
        dut.io.output.bits.cb.expect(expectedCb.U)
        dut.io.output.bits.cr.expect(expectedCr.U)
        dut.clock.step()
      }
    }
  }

  "RgbToYCbCrStage should propagate ready backpressure" in {
    simulate(new RgbToYCbCrStage()) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)

      dut.io.input.valid.poke(true.B)
      dut.io.input.bits.x.poke(0.U)
      dut.io.input.bits.y.poke(0.U)
      dut.io.input.bits.r.poke(255.U)
      dut.io.input.bits.g.poke(255.U)
      dut.io.input.bits.b.poke(255.U)

      dut.io.output.ready.poke(false.B)
      dut.io.input.ready.expect(false.B)
      dut.io.output.valid.expect(true.B)

      dut.io.output.ready.poke(true.B)
      dut.io.input.ready.expect(true.B)
      dut.io.output.bits.yComponent.expect(255.U)
    }
  }
}
