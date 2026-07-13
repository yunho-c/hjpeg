// See README.md for license details.

package hjpeg

import chisel3._
import chisel3.simulator.scalatest.ChiselSim
import org.scalatest.freespec.AnyFreeSpec
import org.scalatest.matchers.must.Matchers

import scala.collection.mutable.ArrayBuffer

class JpegParallelMcuTransformStageSpec extends AnyFreeSpec with Matchers with ChiselSim {
  private def pokeConstantBlock(block: LevelShiftedSampleBlock, value: Int): Unit = {
    for (sample <- 0 until HjpegConstants.BlockSize) {
      block.samples(sample).poke(value.S)
    }
  }

  private def poke444Mcu(dut: JpegParallelMcuTransformStage, index: Int, last: Boolean): Unit = {
    dut.io.input.bits.mcu.yBlockCount.poke(1.U)
    dut.io.input.bits.mcu.quality.poke(50.U)
    pokeConstantBlock(dut.io.input.bits.mcu.y, 32 * (index + 1))
    pokeConstantBlock(dut.io.input.bits.mcu.y1, 0)
    pokeConstantBlock(dut.io.input.bits.mcu.y2, 0)
    pokeConstantBlock(dut.io.input.bits.mcu.y3, 0)
    pokeConstantBlock(dut.io.input.bits.mcu.cb, 34)
    pokeConstantBlock(dut.io.input.bits.mcu.cr, 34)
    dut.io.input.bits.last.poke(last.B)
  }

  private def expectFlatBlock(block: ZigZagCoefficientBlock, dc: Int): Unit = {
    block.coefficients(0).expect(dc.S)
    for (coefficient <- 1 until HjpegConstants.BlockSize) {
      block.coefficients(coefficient).expect(0.S)
    }
  }

  "JpegParallelMcuTransformStage should overlap and retire ordered 4:4:4 MCUs" in {
    simulate(new JpegParallelMcuTransformStage()) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)
      dut.io.input.valid.poke(false.B)
      dut.io.output.ready.poke(true.B)

      val inputCycles = ArrayBuffer.empty[Int]
      val outputCycles = ArrayBuffer.empty[Int]
      var nextInput = 0
      var nextOutput = 0
      var cycle = 0
      while (nextOutput < 4) {
        assert(cycle < 400, "timeout waiting for overlapped MCU transforms")

        if (dut.io.output.valid.peek().litToBoolean) {
          dut.io.output.bits.mcu.yBlockCount.expect(1.U)
          dut.io.output.bits.last.expect((nextOutput == 3).B)
          expectFlatBlock(dut.io.output.bits.mcu.y, 16 * (nextOutput + 1))
          expectFlatBlock(dut.io.output.bits.mcu.cb, 16)
          expectFlatBlock(dut.io.output.bits.mcu.cr, 16)
          outputCycles += cycle
          nextOutput += 1
        }

        if (nextInput < 4 && dut.io.input.ready.peek().litToBoolean) {
          dut.io.input.valid.poke(true.B)
          poke444Mcu(dut, nextInput, last = nextInput == 3)
          inputCycles += cycle
          nextInput += 1
        } else {
          dut.io.input.valid.poke(false.B)
        }

        dut.clock.step()
        cycle += 1
      }

      nextInput mustBe 4
      outputCycles.sliding(2).map(pair => pair(1) - pair(0)).toSeq mustBe Seq(16, 16, 16)
      inputCycles.sliding(2).map(pair => pair(1) - pair(0)).forall(_ <= 16) mustBe true
    }
  }
}
