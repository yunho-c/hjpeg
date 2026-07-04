// See README.md for license details.

package hjpeg

import chisel3._
import chisel3.simulator.scalatest.ChiselSim
import org.scalatest.freespec.AnyFreeSpec
import org.scalatest.matchers.must.Matchers

class YCbCrLevelShiftStageSpec extends AnyFreeSpec with Matchers with ChiselSim {
  "YCbCrLevelShiftStage should center unsigned components around zero" in {
    simulate(new YCbCrLevelShiftStage()) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)

      dut.io.output.ready.poke(true.B)
      dut.io.input.valid.poke(true.B)
      dut.io.input.bits.x.poke(5.U)
      dut.io.input.bits.y.poke(6.U)
      dut.io.input.bits.yComponent.poke(0.U)
      dut.io.input.bits.cb.poke(128.U)
      dut.io.input.bits.cr.poke(255.U)

      dut.io.output.valid.expect(true.B)
      dut.io.output.bits.x.expect(5.U)
      dut.io.output.bits.y.expect(6.U)
      dut.io.output.bits.ySample.expect((-128).S)
      dut.io.output.bits.cbSample.expect(0.S)
      dut.io.output.bits.crSample.expect(127.S)
    }
  }

  "YCbCrLevelShiftStage should propagate ready backpressure" in {
    simulate(new YCbCrLevelShiftStage()) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)

      dut.io.input.valid.poke(true.B)
      dut.io.input.bits.x.poke(0.U)
      dut.io.input.bits.y.poke(0.U)
      dut.io.input.bits.yComponent.poke(128.U)
      dut.io.input.bits.cb.poke(128.U)
      dut.io.input.bits.cr.poke(128.U)

      dut.io.output.ready.poke(false.B)
      dut.io.input.ready.expect(false.B)
      dut.io.output.valid.expect(true.B)

      dut.io.output.ready.poke(true.B)
      dut.io.input.ready.expect(true.B)
      dut.io.output.bits.ySample.expect(0.S)
      dut.io.output.bits.cbSample.expect(0.S)
      dut.io.output.bits.crSample.expect(0.S)
    }
  }
}
