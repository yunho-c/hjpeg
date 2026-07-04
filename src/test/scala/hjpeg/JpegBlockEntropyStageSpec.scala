// See README.md for license details.

package hjpeg

import chisel3._
import chisel3.simulator.scalatest.ChiselSim
import org.scalatest.freespec.AnyFreeSpec
import org.scalatest.matchers.must.Matchers

class JpegBlockEntropyStageSpec extends AnyFreeSpec with Matchers with ChiselSim {
  private case class ExpectedRun(bits: BigInt, length: Int)

  private def pokeBlock(dut: JpegBlockEntropyStage, coefficients: Seq[Int]): Unit = {
    for (index <- 0 until HjpegConstants.BlockSize) {
      dut.io.input.bits.coefficients(index).poke(coefficients(index).S)
    }
  }

  private def loadBlock(
      dut: JpegBlockEntropyStage,
      coefficients: Seq[Int],
      previousDc: Int,
      isLuminance: Boolean
  ): Unit = {
    dut.io.previousDc.poke(previousDc.S)
    dut.io.isLuminance.poke(isLuminance.B)
    pokeBlock(dut, coefficients)
    dut.io.input.valid.poke(true.B)
    dut.io.input.ready.expect(true.B)
    dut.clock.step()
    dut.io.input.valid.poke(false.B)
  }

  private def expectRuns(dut: JpegBlockEntropyStage, runs: Seq[ExpectedRun]): Unit = {
    var received = 0
    var cycles = 0

    while (received < runs.length) {
      assert(cycles < 100, "timeout waiting for block entropy bit runs")
      if (dut.io.output.valid.peek().litToBoolean) {
        val expected = runs(received)
        dut.io.output.bits.bits.expect(expected.bits.U)
        dut.io.output.bits.length.expect(expected.length.U)
        received += 1
      }
      dut.clock.step()
      cycles += 1
    }
  }

  private def waitIdle(dut: JpegBlockEntropyStage): Unit = {
    var cycles = 0
    while (dut.io.busy.peek().litToBoolean) {
      assert(cycles < 20, "timeout waiting for block entropy stage to become idle")
      dut.clock.step()
      cycles += 1
    }
  }

  "JpegBlockEntropyStage should emit DC then EOB for a DC-only luminance block" in {
    simulate(new JpegBlockEntropyStage()) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)

      dut.io.output.ready.poke(true.B)
      val coefficients = Array.fill(HjpegConstants.BlockSize)(0)
      coefficients(0) = 10

      loadBlock(dut, coefficients.toSeq, previousDc = 3, isLuminance = true)
      expectRuns(
        dut,
        Seq(
          ExpectedRun(BigInt("100111", 2), 6),
          ExpectedRun(BigInt("1010", 2), 4)
        )
      )
      waitIdle(dut)
      dut.io.currentDc.expect(10.S)
    }
  }

  "JpegBlockEntropyStage should emit DC, AC coefficient, and EOB runs" in {
    simulate(new JpegBlockEntropyStage()) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)

      dut.io.output.ready.poke(true.B)
      val coefficients = Array.fill(HjpegConstants.BlockSize)(0)
      coefficients(0) = 0
      coefficients(1) = 5

      loadBlock(dut, coefficients.toSeq, previousDc = 0, isLuminance = true)
      expectRuns(
        dut,
        Seq(
          ExpectedRun(BigInt("00", 2), 2),
          ExpectedRun(BigInt("100101", 2), 6),
          ExpectedRun(BigInt("1010", 2), 4)
        )
      )
      waitIdle(dut)
    }
  }

  "JpegBlockEntropyStage should hold the DC run under output backpressure" in {
    simulate(new JpegBlockEntropyStage()) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)

      dut.io.output.ready.poke(false.B)
      val coefficients = Array.fill(HjpegConstants.BlockSize)(0)
      coefficients(0) = 10
      loadBlock(dut, coefficients.toSeq, previousDc = 3, isLuminance = true)

      dut.io.output.valid.expect(true.B)
      dut.io.output.bits.bits.expect(BigInt("100111", 2).U)
      dut.io.output.bits.length.expect(6.U)
      dut.io.input.ready.expect(false.B)
      dut.clock.step()
      dut.io.output.valid.expect(true.B)
      dut.io.output.bits.bits.expect(BigInt("100111", 2).U)

      dut.io.output.ready.poke(true.B)
      expectRuns(
        dut,
        Seq(
          ExpectedRun(BigInt("100111", 2), 6),
          ExpectedRun(BigInt("1010", 2), 4)
        )
      )
      waitIdle(dut)
    }
  }
}
