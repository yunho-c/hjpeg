// See README.md for license details.

package hjpeg

import chisel3._
import chisel3.simulator.scalatest.ChiselSim
import org.scalatest.freespec.AnyFreeSpec
import org.scalatest.matchers.must.Matchers

class JpegRgb8x8ToMcuStageSpec extends AnyFreeSpec with Matchers with ChiselSim {
  private def pushPixel(dut: JpegRgb8x8ToMcuStage, index: Int, r: Int, g: Int, b: Int): Unit = {
    dut.io.input.valid.poke(true.B)
    dut.io.input.bits.x.poke((index % HjpegConstants.BlockDim).U)
    dut.io.input.bits.y.poke((index / HjpegConstants.BlockDim).U)
    dut.io.input.bits.r.poke(r.U)
    dut.io.input.bits.g.poke(g.U)
    dut.io.input.bits.b.poke(b.U)
    dut.io.input.ready.expect(true.B)
    dut.clock.step()
  }

  private def pushFlatBlock(dut: JpegRgb8x8ToMcuStage, r: Int, g: Int, b: Int): Unit = {
    for (index <- 0 until HjpegConstants.BlockSize) {
      pushPixel(dut, index, r, g, b)
    }
    dut.io.input.valid.poke(false.B)
  }

  "JpegRgb8x8ToMcuStage should transform neutral gray into an all-zero MCU" in {
    simulate(new JpegRgb8x8ToMcuStage()) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)

      dut.io.quality.poke(50.U)
      dut.io.output.ready.poke(true.B)
      pushFlatBlock(dut, 128, 128, 128)

      dut.io.output.valid.expect(true.B)
      for (index <- 0 until HjpegConstants.BlockSize) {
        dut.io.output.bits.y.coefficients(index).expect(0.S)
        dut.io.output.bits.cb.coefficients(index).expect(0.S)
        dut.io.output.bits.cr.coefficients(index).expect(0.S)
      }
      dut.clock.step()
      dut.io.input.ready.expect(true.B)
    }
  }

  "JpegRgb8x8ToMcuStage should hold the MCU under output backpressure" in {
    simulate(new JpegRgb8x8ToMcuStage()) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)

      dut.io.quality.poke(50.U)
      dut.io.output.ready.poke(false.B)
      pushFlatBlock(dut, 128, 128, 128)

      dut.io.output.valid.expect(true.B)
      dut.io.input.ready.expect(false.B)
      dut.clock.step()
      dut.io.output.valid.expect(true.B)

      dut.io.output.ready.poke(true.B)
      dut.clock.step()
      dut.io.input.ready.expect(true.B)
    }
  }

  "JpegRgb8x8ToMcuStage should emit only DC coefficients for a flat non-neutral block" in {
    simulate(new JpegRgb8x8ToMcuStage()) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)

      dut.io.quality.poke(50.U)
      dut.io.output.ready.poke(true.B)
      pushFlatBlock(dut, 160, 160, 160)

      dut.io.output.valid.expect(true.B)
      dut.io.output.bits.y.coefficients(0).expect(16.S)
      dut.io.output.bits.cb.coefficients(0).expect(0.S)
      dut.io.output.bits.cr.coefficients(0).expect(0.S)
      for (index <- 1 until HjpegConstants.BlockSize) {
        dut.io.output.bits.y.coefficients(index).expect(0.S)
        dut.io.output.bits.cb.coefficients(index).expect(0.S)
        dut.io.output.bits.cr.coefficients(index).expect(0.S)
      }
    }
  }
}
