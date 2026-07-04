// See README.md for license details.

package hjpeg

import chisel3._
import chisel3.simulator.scalatest.ChiselSim
import org.scalatest.freespec.AnyFreeSpec
import org.scalatest.matchers.must.Matchers

class HjpegCoreSpec extends AnyFreeSpec with Matchers with ChiselSim {
  "HjpegCore should emit a deterministic luma byte for each accepted RGB pixel" in {
    simulate(new HjpegCore()) { dut =>
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
      dut.io.input.bits.x.poke(0.U)
      dut.io.input.bits.y.poke(0.U)
      dut.io.input.bits.r.poke(255.U)
      dut.io.input.bits.g.poke(0.U)
      dut.io.input.bits.b.poke(0.U)
      dut.io.output.valid.expect(true.B)
      dut.io.output.bits.byte.expect(76.U)
      dut.io.output.bits.last.expect(false.B)
      dut.clock.step()

      dut.io.input.bits.x.poke(1.U)
      dut.io.input.bits.r.poke(0.U)
      dut.io.input.bits.g.poke(255.U)
      dut.io.input.bits.b.poke(0.U)
      dut.io.output.bits.byte.expect(149.U)
      dut.io.output.bits.last.expect(true.B)
      dut.clock.step()

      dut.io.input.valid.poke(false.B)
      dut.io.protocolError.expect(false.B)
    }
  }

  "HjpegCore should report out-of-frame coordinates" in {
    simulate(new HjpegCore()) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)

      dut.io.config.xsize.poke(1.U)
      dut.io.config.ysize.poke(1.U)
      dut.io.config.quality.poke(90.U)
      dut.io.config.restartInterval.poke(0.U)
      dut.io.config.enableChromaSubsample.poke(true.B)
      dut.io.config.emitJfif.poke(true.B)
      dut.io.clearProtocolError.poke(false.B)
      dut.io.output.ready.poke(true.B)

      dut.io.input.valid.poke(true.B)
      dut.io.input.bits.x.poke(1.U)
      dut.io.input.bits.y.poke(0.U)
      dut.io.input.bits.r.poke(0.U)
      dut.io.input.bits.g.poke(0.U)
      dut.io.input.bits.b.poke(0.U)
      dut.clock.step()

      dut.io.protocolError.expect(true.B)
      dut.io.input.valid.poke(false.B)
      dut.io.clearProtocolError.poke(true.B)
      dut.clock.step()
      dut.io.protocolError.expect(false.B)
    }
  }
}
