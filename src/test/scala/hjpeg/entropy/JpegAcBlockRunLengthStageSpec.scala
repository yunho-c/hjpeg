// See README.md for license details.

package hjpeg

import chisel3._
import chisel3.simulator.scalatest.ChiselSim
import org.scalatest.freespec.AnyFreeSpec
import org.scalatest.matchers.must.Matchers

class JpegAcBlockRunLengthStageSpec extends AnyFreeSpec with Matchers with ChiselSim {
  private case class ExpectedEvent(run: Int, coefficient: Int, eob: Boolean, zrl: Boolean)

  private def pokeBlock(dut: JpegAcBlockRunLengthStage, coefficients: Seq[Int]): Unit = {
    for (index <- 0 until HjpegConstants.BlockSize) {
      dut.io.input.bits.coefficients(index).poke(coefficients(index).S)
    }
  }

  private def loadBlock(dut: JpegAcBlockRunLengthStage, coefficients: Seq[Int]): Unit = {
    dut.io.input.valid.poke(true.B)
    pokeBlock(dut, coefficients)
    dut.io.input.ready.expect(true.B)
    dut.clock.step()
    dut.io.input.valid.poke(false.B)
  }

  private def expectEvents(dut: JpegAcBlockRunLengthStage, events: Seq[ExpectedEvent]): Unit = {
    var received = 0
    var cycles = 0

    while (received < events.length) {
      assert(cycles < 100, "timeout waiting for AC run-length events")

      if (dut.io.output.valid.peek().litToBoolean) {
        val expected = events(received)
        dut.io.output.bits.runLength.expect(expected.run.U)
        dut.io.output.bits.coefficient.expect(expected.coefficient.S)
        dut.io.output.bits.emitEndOfBlock.expect(expected.eob.B)
        dut.io.output.bits.emitZeroRunLength.expect(expected.zrl.B)
        received += 1
      }

      dut.clock.step()
      cycles += 1
    }
  }

  "JpegAcBlockRunLengthStage should emit EOB for an all-zero AC block" in {
    simulate(new JpegAcBlockRunLengthStage()) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)

      dut.io.output.ready.poke(true.B)
      loadBlock(dut, Seq.fill(HjpegConstants.BlockSize)(0))
      expectEvents(dut, Seq(ExpectedEvent(0, 0, eob = true, zrl = false)))
      dut.io.busy.expect(false.B)
    }
  }

  "JpegAcBlockRunLengthStage should emit coefficient runs followed by EOB" in {
    simulate(new JpegAcBlockRunLengthStage()) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)

      val coefficients = Array.fill(HjpegConstants.BlockSize)(0)
      coefficients(0) = 12
      coefficients(1) = 5
      coefficients(4) = -2

      dut.io.output.ready.poke(true.B)
      loadBlock(dut, coefficients.toSeq)
      expectEvents(
        dut,
        Seq(
          ExpectedEvent(0, 5, eob = false, zrl = false),
          ExpectedEvent(2, -2, eob = false, zrl = false),
          ExpectedEvent(0, 0, eob = true, zrl = false)
        )
      )
      dut.io.busy.expect(false.B)
    }
  }

  "JpegAcBlockRunLengthStage should emit ZRL before a later nonzero after sixteen zeros" in {
    simulate(new JpegAcBlockRunLengthStage()) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)

      val coefficients = Array.fill(HjpegConstants.BlockSize)(0)
      coefficients(17) = 7

      dut.io.output.ready.poke(true.B)
      loadBlock(dut, coefficients.toSeq)
      expectEvents(
        dut,
        Seq(
          ExpectedEvent(0, 0, eob = false, zrl = true),
          ExpectedEvent(0, 7, eob = false, zrl = false),
          ExpectedEvent(0, 0, eob = true, zrl = false)
        )
      )
      dut.io.busy.expect(false.B)
    }
  }

  "JpegAcBlockRunLengthStage should hold an event while output is backpressured" in {
    simulate(new JpegAcBlockRunLengthStage()) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)

      val coefficients = Array.fill(HjpegConstants.BlockSize)(0)
      coefficients(1) = 3

      dut.io.output.ready.poke(false.B)
      loadBlock(dut, coefficients.toSeq)

      dut.io.output.valid.expect(true.B)
      dut.io.output.bits.runLength.expect(0.U)
      dut.io.output.bits.coefficient.expect(3.S)
      dut.io.input.ready.expect(false.B)
      dut.clock.step()

      dut.io.output.valid.expect(true.B)
      dut.io.output.bits.coefficient.expect(3.S)

      dut.io.output.ready.poke(true.B)
      expectEvents(
        dut,
        Seq(
          ExpectedEvent(0, 3, eob = false, zrl = false),
          ExpectedEvent(0, 0, eob = true, zrl = false)
        )
      )
      dut.io.busy.expect(false.B)
    }
  }
}
