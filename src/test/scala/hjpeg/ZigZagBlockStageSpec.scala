// See README.md for license details.

package hjpeg

import chisel3._
import chisel3.simulator.scalatest.ChiselSim
import org.scalatest.freespec.AnyFreeSpec
import org.scalatest.matchers.must.Matchers

class ZigZagBlockStageSpec extends AnyFreeSpec with Matchers with ChiselSim {
  "ZigZagBlockStage should reorder natural coefficients into scan order" in {
    simulate(new ZigZagBlockStage()) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)

      dut.io.input.valid.poke(true.B)
      dut.io.output.ready.poke(true.B)
      for (index <- 0 until HjpegConstants.BlockSize) {
        dut.io.input.bits.coefficients(index).poke(index.S)
      }

      dut.io.output.valid.expect(true.B)
      for (scanIndex <- 0 until HjpegConstants.BlockSize) {
        dut.io.output.bits.coefficients(scanIndex).expect(JpegTables.ZigZagOrder(scanIndex).S)
      }
    }
  }

  "ZigZagBlockStage should propagate ready backpressure" in {
    simulate(new ZigZagBlockStage()) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)

      dut.io.input.valid.poke(true.B)
      for (index <- 0 until HjpegConstants.BlockSize) {
        dut.io.input.bits.coefficients(index).poke(0.S)
      }

      dut.io.output.ready.poke(false.B)
      dut.io.input.ready.expect(false.B)
      dut.io.output.valid.expect(true.B)

      dut.io.output.ready.poke(true.B)
      dut.io.input.ready.expect(true.B)
    }
  }
}
