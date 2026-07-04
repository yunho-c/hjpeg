// See README.md for license details.

package hjpeg

import chisel3._
import chisel3.simulator.scalatest.ChiselSim
import org.scalatest.freespec.AnyFreeSpec
import org.scalatest.matchers.must.Matchers

class HjpegAxiStreamCoreSpec extends AnyFreeSpec with Matchers with ChiselSim {
  "HjpegAxiStreamCore should generate raster coordinates and output frame last" in {
    simulate(new HjpegAxiStreamCore()) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)

      dut.io.config.xsize.poke(2.U)
      dut.io.config.ysize.poke(1.U)
      dut.io.config.quality.poke(90.U)
      dut.io.config.restartInterval.poke(0.U)
      dut.io.config.enableChromaSubsample.poke(true.B)
      dut.io.config.emitJfif.poke(true.B)
      dut.io.clearProtocolError.poke(false.B)
      dut.io.output.ready.poke(true.B)
      dut.io.input.valid.poke(true.B)
      dut.io.input.bits.keep.poke(7.U)

      dut.io.input.bits.data.poke(255.U)
      dut.io.input.bits.last.poke(false.B)
      dut.io.output.valid.expect(true.B)
      dut.io.output.bits.data.expect(76.U)
      dut.io.output.bits.keep.expect(1.U)
      dut.io.output.bits.last.expect(false.B)
      dut.clock.step()

      dut.io.input.bits.data.poke((BigInt(255) << 8).U)
      dut.io.input.bits.last.poke(true.B)
      dut.io.output.bits.data.expect(149.U)
      dut.io.output.bits.last.expect(true.B)
      dut.clock.step()

      dut.io.protocolError.expect(false.B)
    }
  }

  "HjpegAxiStreamCore should report mismatched input last" in {
    simulate(new HjpegAxiStreamCore()) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)

      dut.io.config.xsize.poke(2.U)
      dut.io.config.ysize.poke(1.U)
      dut.io.config.quality.poke(90.U)
      dut.io.config.restartInterval.poke(0.U)
      dut.io.config.enableChromaSubsample.poke(true.B)
      dut.io.config.emitJfif.poke(true.B)
      dut.io.clearProtocolError.poke(false.B)
      dut.io.output.ready.poke(true.B)
      dut.io.input.valid.poke(true.B)
      dut.io.input.bits.keep.poke(7.U)
      dut.io.input.bits.data.poke(0.U)
      dut.io.input.bits.last.poke(true.B)
      dut.clock.step()

      dut.io.protocolError.expect(true.B)
    }
  }
}
