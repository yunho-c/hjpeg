// See README.md for license details.

package hjpeg

import chisel3._
import chisel3.simulator.scalatest.ChiselSim
import org.scalatest.freespec.AnyFreeSpec
import org.scalatest.matchers.must.Matchers

import scala.collection.mutable.ArrayBuffer
import scala.util.Random

class JpegAcBlockRunLengthStageSpec extends AnyFreeSpec with Matchers with ChiselSim {
  private case class ExpectedEvent(run: Int, coefficient: Int, eob: Boolean, zrl: Boolean)

  private def referenceEvents(coefficients: Seq[Int]): Seq[ExpectedEvent] = {
    val events = ArrayBuffer.empty[ExpectedEvent]
    val lastNonzero = (1 until HjpegConstants.BlockSize)
      .filter(index => coefficients(index) != 0)
      .lastOption

    lastNonzero match {
      case None => events += ExpectedEvent(0, 0, eob = true, zrl = false)
      case Some(lastIndex) =>
        var zeroRun = 0
        for (index <- 1 to lastIndex) {
          if (coefficients(index) == 0) {
            zeroRun += 1
            if (zeroRun == 16) {
              events += ExpectedEvent(0, 0, eob = false, zrl = true)
              zeroRun = 0
            }
          } else {
            events += ExpectedEvent(zeroRun, coefficients(index), eob = false, zrl = false)
            zeroRun = 0
          }
        }
        if (lastIndex < HjpegConstants.BlockSize - 1) {
          events += ExpectedEvent(0, 0, eob = true, zrl = false)
        }
    }

    events.toSeq
  }

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

  private def expectEvents(dut: JpegAcBlockRunLengthStage, events: Seq[ExpectedEvent]): Int = {
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

    cycles
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

  "JpegAcBlockRunLengthStage should skip four zero coefficients per cycle" in {
    simulate(new JpegAcBlockRunLengthStage()) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)

      val coefficients = Array.fill(HjpegConstants.BlockSize)(0)
      coefficients(13) = -9

      dut.io.output.ready.poke(true.B)
      loadBlock(dut, coefficients.toSeq)

      for (_ <- 0 until 3) {
        dut.io.output.valid.expect(false.B)
        dut.clock.step()
      }

      dut.io.output.valid.expect(true.B)
      dut.io.output.bits.runLength.expect(12.U)
      dut.io.output.bits.coefficient.expect((-9).S)
      dut.io.output.bits.emitEndOfBlock.expect(false.B)
      dut.io.output.bits.emitZeroRunLength.expect(false.B)
      dut.clock.step()

      dut.io.output.valid.expect(true.B)
      dut.io.output.bits.emitEndOfBlock.expect(true.B)
      dut.clock.step()
      dut.io.busy.expect(false.B)
    }
  }

  "JpegAcBlockRunLengthStage should sustain one ordered event per cycle" in {
    simulate(new JpegAcBlockRunLengthStage()) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)

      val coefficients = Array.fill(HjpegConstants.BlockSize)(0)
      coefficients(1) = 1
      coefficients(2) = -2
      coefficients(3) = 3
      coefficients(4) = -4

      dut.io.output.ready.poke(true.B)
      loadBlock(dut, coefficients.toSeq)
      val cycles = expectEvents(
        dut,
        Seq(
          ExpectedEvent(0, 1, eob = false, zrl = false),
          ExpectedEvent(0, -2, eob = false, zrl = false),
          ExpectedEvent(0, 3, eob = false, zrl = false),
          ExpectedEvent(0, -4, eob = false, zrl = false),
          ExpectedEvent(0, 0, eob = true, zrl = false)
        )
      )

      cycles mustBe 5
      dut.io.busy.expect(false.B)
    }
  }

  "JpegAcBlockRunLengthStage should match a scalar reference across sparse and dense blocks" in {
    simulate(new JpegAcBlockRunLengthStage()) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)
      dut.io.output.ready.poke(true.B)

      val random = new Random(0x4ac5eedL)
      val randomBlocks = Seq.fill(24) {
        Seq.tabulate(HjpegConstants.BlockSize) { index =>
          if (index == 0 || random.nextInt(5) != 0) 0 else random.nextInt(31) - 15
        }
      }
      val lastCoefficientBlock = Seq.tabulate(HjpegConstants.BlockSize) { index =>
        if (index == HjpegConstants.BlockSize - 1) -11 else 0
      }

      (randomBlocks :+ lastCoefficientBlock).foreach { coefficients =>
        loadBlock(dut, coefficients)
        expectEvents(dut, referenceEvents(coefficients))
        dut.io.busy.expect(false.B)
      }
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
