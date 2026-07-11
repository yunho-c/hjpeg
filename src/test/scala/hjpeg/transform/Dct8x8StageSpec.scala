// See README.md for license details.

package hjpeg

import chisel3._
import chisel3.simulator.scalatest.ChiselSim
import org.scalatest.freespec.AnyFreeSpec
import org.scalatest.matchers.must.Matchers

class Dct8x8StageSpec extends AnyFreeSpec with Matchers with ChiselSim {
  private def roundShiftSigned(value: Long, shift: Int): Int = {
    val magnitude = math.abs(value)
    val rounded = (magnitude + (1L << (shift - 1))) >> shift
    (if (value < 0) -rounded else rounded).toInt
  }

  private def fixedPointReference(samples: Seq[Int]): Seq[Int] = {
    val cosine = Dct8x8Constants.CosineQ14
    val rowTransformed = for {
      row <- 0 until HjpegConstants.BlockDim
      frequency <- 0 until HjpegConstants.BlockDim
    } yield (0 until HjpegConstants.BlockDim)
      .map(term => cosine(frequency)(term).toLong * samples(row * HjpegConstants.BlockDim + term))
      .sum

    for {
      rowFrequency <- 0 until HjpegConstants.BlockDim
      columnFrequency <- 0 until HjpegConstants.BlockDim
    } yield {
      val accumulated = (0 until HjpegConstants.BlockDim)
        .map(term => cosine(rowFrequency)(term).toLong * rowTransformed(term * HjpegConstants.BlockDim + columnFrequency))
        .sum
      roundShiftSigned(accumulated, Dct8x8Constants.FractionBits * 2)
    }
  }

  private def pokeBlock(dut: Dct8x8Stage, samples: Seq[Int]): Unit = {
    for (index <- 0 until HjpegConstants.BlockSize) {
      dut.io.input.bits.samples(index).poke(samples(index).S)
    }
  }

  private def expectBlock(dut: Dct8x8Stage, expected: Seq[Int]): Unit = {
    for (index <- 0 until HjpegConstants.BlockSize) {
      dut.io.output.bits.coefficients(index).expect(expected(index).S)
    }
  }

  private def pushBlock(dut: Dct8x8Stage, samples: Seq[Int]): Unit = {
    dut.io.input.valid.poke(true.B)
    pokeBlock(dut, samples)
    dut.io.input.ready.expect(true.B)
    dut.clock.step()
    dut.io.input.valid.poke(false.B)
  }

  private def waitForOutput(dut: Dct8x8Stage, maxCycles: Int = 1200): Int = {
    var cycles = 0
    while (!dut.io.output.valid.peek().litToBoolean) {
      assert(cycles < maxCycles, "timeout waiting for DCT output")
      dut.clock.step()
      cycles += 1
    }
    cycles
  }

  "Dct8x8Stage should transform a constant block into DC only" in {
    simulate(new Dct8x8Stage()) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)

      dut.io.output.ready.poke(true.B)
      pushBlock(dut, Seq.fill(HjpegConstants.BlockSize)(5))

      val cycles = waitForOutput(dut)
      info(s"constant-block DCT latency: $cycles cycles")
      cycles must be <= 128
      dut.io.output.valid.expect(true.B)
      expectBlock(dut, Seq(40) ++ Seq.fill(HjpegConstants.BlockSize - 1)(0))
    }
  }

  "Dct8x8Stage should preserve sign for a negative constant block" in {
    simulate(new Dct8x8Stage()) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)

      dut.io.output.ready.poke(true.B)
      pushBlock(dut, Seq.fill(HjpegConstants.BlockSize)(-128))

      waitForOutput(dut)
      dut.io.output.valid.expect(true.B)
      expectBlock(dut, Seq(-1024) ++ Seq.fill(HjpegConstants.BlockSize - 1)(0))
    }
  }

  "Dct8x8Stage should match the fixed-point reference for row and column ramps" in {
    simulate(new Dct8x8Stage()) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)

      dut.io.output.ready.poke(true.B)

      pushBlock(dut, (0 until HjpegConstants.BlockSize).map(index => index / HjpegConstants.BlockDim))
      waitForOutput(dut)
      dut.io.output.valid.expect(true.B)
      expectBlock(
        dut,
        Seq(
          28, 0, 0, 0, 0, 0, 0, 0,
          -18, 0, 0, 0, 0, 0, 0, 0,
          0, 0, 0, 0, 0, 0, 0, 0,
          -2, 0, 0, 0, 0, 0, 0, 0,
          0, 0, 0, 0, 0, 0, 0, 0,
          -1, 0, 0, 0, 0, 0, 0, 0,
          0, 0, 0, 0, 0, 0, 0, 0,
          0, 0, 0, 0, 0, 0, 0, 0
        )
      )

      dut.clock.step()
      pushBlock(dut, (0 until HjpegConstants.BlockSize).map(index => index % HjpegConstants.BlockDim))
      waitForOutput(dut)
      dut.io.output.valid.expect(true.B)
      expectBlock(
        dut,
        Seq(
          28, -18, 0, -2, 0, -1, 0, 0,
          0, 0, 0, 0, 0, 0, 0, 0,
          0, 0, 0, 0, 0, 0, 0, 0,
          0, 0, 0, 0, 0, 0, 0, 0,
          0, 0, 0, 0, 0, 0, 0, 0,
          0, 0, 0, 0, 0, 0, 0, 0,
          0, 0, 0, 0, 0, 0, 0, 0,
          0, 0, 0, 0, 0, 0, 0, 0
        )
      )
    }
  }

  "Dct8x8Stage should match the fixed-point reference for varied blocks" in {
    simulate(new Dct8x8Stage()) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)
      dut.io.output.ready.poke(true.B)

      val blocks = Seq(
        (0 until HjpegConstants.BlockSize).map(index => ((index * 73 + 19) & 0xff) - 128),
        (0 until HjpegConstants.BlockSize).map(index => if (((index / 8) + (index % 8)) % 2 == 0) 127 else -128),
        (0 until HjpegConstants.BlockSize).map(index => if (index == 27) 127 else 0)
      )

      for (samples <- blocks) {
        pushBlock(dut, samples)
        waitForOutput(dut)
        expectBlock(dut, fixedPointReference(samples))
        dut.clock.step()
      }
    }
  }

  "Dct8x8Stage should propagate ready backpressure" in {
    simulate(new Dct8x8Stage()) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)

      pushBlock(dut, Seq.fill(HjpegConstants.BlockSize)(0))
      waitForOutput(dut)

      dut.io.output.ready.poke(false.B)
      dut.io.input.ready.expect(false.B)
      dut.io.output.valid.expect(true.B)

      dut.io.output.ready.poke(true.B)
      dut.clock.step()
      dut.io.input.ready.expect(true.B)
    }
  }
}
