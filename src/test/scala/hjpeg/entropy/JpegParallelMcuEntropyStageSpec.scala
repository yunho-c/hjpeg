// See README.md for license details.

package hjpeg

import chisel3._
import chisel3.simulator.scalatest.ChiselSim
import org.scalatest.freespec.AnyFreeSpec
import org.scalatest.matchers.must.Matchers

class JpegParallelMcuEntropyStageSpec extends AnyFreeSpec with Matchers with ChiselSim {
  private case class ExpectedRun(bits: BigInt, length: Int)

  private def magnitude(value: Int): (Int, Int) = {
    val absolute = math.abs(value)
    if (absolute == 0) (0, 0)
    else {
      val category = 32 - Integer.numberOfLeadingZeros(absolute)
      val mask = (1 << category) - 1
      val amplitude = if (value < 0) mask - absolute else absolute
      (category, amplitude)
    }
  }

  private def appendToken(
      runs: scala.collection.mutable.ArrayBuffer[ExpectedRun],
      table: Seq[(Int, Int)],
      symbol: Int,
      amplitude: Int = 0,
      amplitudeLength: Int = 0): Unit = {
    val (code, codeLength) = table(symbol)
    require(codeLength > 0, f"missing Huffman symbol 0x$symbol%02x")
    runs += ExpectedRun((BigInt(code) << amplitudeLength) | amplitude, codeLength + amplitudeLength)
  }

  private def encodeBlock(coefficients: Seq[Int], previousDc: Int, luminance: Boolean): Seq[ExpectedRun] = {
    val runs = scala.collection.mutable.ArrayBuffer.empty[ExpectedRun]
    val dcTable = if (luminance) JpegTables.StandardDcLuminanceCodes else JpegTables.StandardDcChrominanceCodes
    val acTable =
      if (luminance) JpegTables.StandardAcLuminanceCodesBySymbol
      else JpegTables.StandardAcChrominanceCodesBySymbol

    val (dcCategory, dcAmplitude) = magnitude(coefficients.head - previousDc)
    appendToken(runs, dcTable, dcCategory, dcAmplitude, dcCategory)

    val lastNonzero = (1 until HjpegConstants.BlockSize).filter(index => coefficients(index) != 0).lastOption
    lastNonzero.foreach { lastIndex =>
      var zeroRun = 0
      for (index <- 1 to lastIndex) {
        val coefficient = coefficients(index)
        if (coefficient == 0) {
          zeroRun += 1
        } else {
          while (zeroRun >= 16) {
            appendToken(runs, acTable, 0xf0)
            zeroRun -= 16
          }
          val (category, amplitude) = magnitude(coefficient)
          appendToken(runs, acTable, (zeroRun << 4) | category, amplitude, category)
          zeroRun = 0
        }
      }
    }
    if (lastNonzero.forall(_ < HjpegConstants.BlockSize - 1)) {
      appendToken(runs, acTable, 0x00)
    }
    runs.toSeq
  }

  private def block(dc: Int, firstAc: Int, lateAc: Int, lateIndex: Int): Seq[Int] = {
    val coefficients = Array.fill(HjpegConstants.BlockSize)(0)
    coefficients(0) = dc
    coefficients(1) = firstAc
    coefficients(lateIndex) = lateAc
    coefficients.toSeq
  }

  private def pokeMcu(
      dut: JpegParallelMcuEntropyStage,
      blocks: Seq[Seq[Int]],
      subsampled: Boolean): Unit = {
    val targets = Seq(
      dut.io.input.bits.y,
      dut.io.input.bits.y1,
      dut.io.input.bits.y2,
      dut.io.input.bits.y3,
      dut.io.input.bits.cb,
      dut.io.input.bits.cr)
    for ((target, coefficients) <- targets.zip(blocks); index <- 0 until HjpegConstants.BlockSize) {
      target.coefficients(index).poke(coefficients(index).S)
    }
    dut.io.input.bits.yBlockCount.poke((if (subsampled) 4 else 1).U)
  }

  private def runMcu(
      dut: JpegParallelMcuEntropyStage,
      blocks: Seq[Seq[Int]],
      previousDc: Seq[Int],
      subsampled: Boolean,
      stallOutput: Boolean): (Seq[ExpectedRun], Int) = {
    dut.reset.poke(true.B)
    dut.clock.step()
    dut.reset.poke(false.B)
    dut.io.input.valid.poke(false.B)
    dut.io.output.ready.poke(false.B)
    for (component <- previousDc.indices) {
      dut.io.previousDc(component).poke(previousDc(component).S)
    }
    pokeMcu(dut, blocks, subsampled)

    dut.io.input.ready.expect(true.B)
    dut.io.input.valid.poke(true.B)
    dut.clock.step()
    dut.io.input.valid.poke(false.B)

    val received = scala.collection.mutable.ArrayBuffer.empty[ExpectedRun]
    var heldRun = Option.empty[ExpectedRun]
    var cycles = 0
    var done = false
    while (!done) {
      assert(cycles < 400, "timeout waiting for ordered MCU entropy runs")
      val ready = !stallOutput || cycles % 5 != 1
      dut.io.output.ready.poke(ready.B)
      val valid = dut.io.output.valid.peek().litToBoolean
      val currentRun =
        if (valid)
          Some(ExpectedRun(
            dut.io.output.bits.bits.peek().litValue,
            dut.io.output.bits.length.peek().litValue.toInt))
        else None

      heldRun.foreach { expected =>
        currentRun mustBe Some(expected)
      }
      if (valid && ready) {
        received += currentRun.get
        heldRun = None
      } else if (valid) {
        heldRun = currentRun
      }

      done = dut.io.done.peek().litToBoolean
      dut.clock.step()
      cycles += 1
    }

    dut.io.busy.expect(false.B)
    val expectedNextDc = Seq(
      if (subsampled) blocks(3).head else blocks.head.head,
      blocks(4).head,
      blocks(5).head)
    for (component <- expectedNextDc.indices) {
      dut.io.nextDc(component).expect(expectedNextDc(component).S)
    }
    (received.toSeq, cycles)
  }

  "JpegParallelMcuEntropyStage should preserve 4:4:4 block order under backpressure" in {
    simulate(new JpegParallelMcuEntropyStage()) { dut =>
      val blocks = Seq(
        block(7, 1, -2, 19),
        Seq.fill(HjpegConstants.BlockSize)(0),
        Seq.fill(HjpegConstants.BlockSize)(0),
        Seq.fill(HjpegConstants.BlockSize)(0),
        block(-5, 3, -1, 34),
        block(11, -4, 2, 63))
      val previousDc = Seq(2, -9, 4)
      val expected =
        encodeBlock(blocks(0), previousDc(0), luminance = true) ++
          encodeBlock(blocks(4), previousDc(1), luminance = false) ++
          encodeBlock(blocks(5), previousDc(2), luminance = false)

      val (received, _) = runMcu(dut, blocks, previousDc, subsampled = false, stallOutput = true)
      received mustBe expected
    }
  }

  "JpegParallelMcuEntropyStage should reuse three slots for ordered 4:2:0 runs" in {
    simulate(new JpegParallelMcuEntropyStage()) { dut =>
      val blocks = Seq(
        block(7, 1, -2, 18),
        block(9, -1, 3, 25),
        block(12, 2, -3, 33),
        block(16, -2, 1, 41),
        block(-5, 3, -1, 34),
        block(11, -4, 2, 63))
      val previousDc = Seq(2, -9, 4)
      val expected =
        encodeBlock(blocks(0), previousDc(0), luminance = true) ++
          encodeBlock(blocks(1), blocks(0).head, luminance = true) ++
          encodeBlock(blocks(2), blocks(1).head, luminance = true) ++
          encodeBlock(blocks(3), blocks(2).head, luminance = true) ++
          encodeBlock(blocks(4), previousDc(1), luminance = false) ++
          encodeBlock(blocks(5), previousDc(2), luminance = false)

      val (received, cycles) = runMcu(dut, blocks, previousDc, subsampled = true, stallOutput = false)
      received mustBe expected
      cycles must be <= 100
    }
  }
}
