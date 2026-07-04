// See README.md for license details.

package hjpeg

import chisel3._
import chisel3.simulator.scalatest.ChiselSim
import org.scalatest.freespec.AnyFreeSpec
import org.scalatest.matchers.must.Matchers

class Dct8x8StageSpec extends AnyFreeSpec with Matchers with ChiselSim {
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

  "Dct8x8Stage should transform a constant block into DC only" in {
    simulate(new Dct8x8Stage()) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)

      dut.io.input.valid.poke(true.B)
      dut.io.output.ready.poke(true.B)
      pokeBlock(dut, Seq.fill(HjpegConstants.BlockSize)(5))

      dut.io.output.valid.expect(true.B)
      expectBlock(dut, Seq(40) ++ Seq.fill(HjpegConstants.BlockSize - 1)(0))
    }
  }

  "Dct8x8Stage should preserve sign for a negative constant block" in {
    simulate(new Dct8x8Stage()) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)

      dut.io.input.valid.poke(true.B)
      dut.io.output.ready.poke(true.B)
      pokeBlock(dut, Seq.fill(HjpegConstants.BlockSize)(-128))

      dut.io.output.valid.expect(true.B)
      expectBlock(dut, Seq(-1024) ++ Seq.fill(HjpegConstants.BlockSize - 1)(0))
    }
  }

  "Dct8x8Stage should match the fixed-point reference for row and column ramps" in {
    simulate(new Dct8x8Stage()) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)

      dut.io.input.valid.poke(true.B)
      dut.io.output.ready.poke(true.B)

      pokeBlock(dut, (0 until HjpegConstants.BlockSize).map(index => index / HjpegConstants.BlockDim))
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

      pokeBlock(dut, (0 until HjpegConstants.BlockSize).map(index => index % HjpegConstants.BlockDim))
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

  "Dct8x8Stage should propagate ready backpressure" in {
    simulate(new Dct8x8Stage()) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)

      dut.io.input.valid.poke(true.B)
      pokeBlock(dut, Seq.fill(HjpegConstants.BlockSize)(0))

      dut.io.output.ready.poke(false.B)
      dut.io.input.ready.expect(false.B)
      dut.io.output.valid.expect(true.B)

      dut.io.output.ready.poke(true.B)
      dut.io.input.ready.expect(true.B)
    }
  }
}
