// See README.md for license details.

package hjpeg

import chisel3._
import chisel3.simulator.scalatest.ChiselSim
import org.scalatest.freespec.AnyFreeSpec
import org.scalatest.matchers.must.Matchers

class JpegBitstreamStagesSpec extends AnyFreeSpec with Matchers with ChiselSim {
  private def pushRun(dut: JpegBitRunPacker, bits: BigInt, length: Int): Unit = {
    dut.io.input.valid.poke(true.B)
    dut.io.input.bits.bits.poke(bits.U)
    dut.io.input.bits.length.poke(length.U)
    dut.io.input.ready.expect(true.B)
    dut.clock.step()
    dut.io.input.valid.poke(false.B)
  }

  private def expectByte(dut: JpegBitRunPacker, byte: Int, last: Boolean = false): Unit = {
    dut.io.output.valid.expect(true.B)
    dut.io.output.bits.byte.expect(byte.U)
    dut.io.output.bits.last.expect(last.B)
    dut.clock.step()
  }

  "JpegEntropyTokenBitsStage should concatenate Huffman and amplitude bits" in {
    simulate(new JpegEntropyTokenBitsStage()) { dut =>
      dut.io.token.huffmanCode.poke("b101".U)
      dut.io.token.huffmanLength.poke(3.U)
      dut.io.token.amplitude.poke("b0110".U)
      dut.io.token.amplitudeLength.poke(4.U)

      dut.io.run.bits.expect("b1010110".U)
      dut.io.run.length.expect(7.U)
    }
  }

  "JpegBitRunPacker should pack multiple runs into one byte" in {
    simulate(new JpegBitRunPacker()) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)

      dut.io.flush.poke(false.B)
      dut.io.output.ready.poke(true.B)
      pushRun(dut, BigInt("101", 2), 3)
      pushRun(dut, BigInt("01011", 2), 5)
      expectByte(dut, 0xab)
      dut.io.idle.expect(true.B)
    }
  }

  "JpegBitRunPacker should flush a partial byte with one bits" in {
    simulate(new JpegBitRunPacker()) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)

      dut.io.flush.poke(false.B)
      dut.io.output.ready.poke(true.B)
      pushRun(dut, BigInt("101", 2), 3)

      dut.io.flush.poke(true.B)
      expectByte(dut, 0xbf, last = true)
      dut.io.flush.poke(false.B)
      dut.io.idle.expect(true.B)
    }
  }

  "JpegBitRunPacker should stuff zero after emitted 0xff bytes" in {
    simulate(new JpegBitRunPacker()) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)

      dut.io.flush.poke(false.B)
      dut.io.output.ready.poke(true.B)
      pushRun(dut, 0xff, 8)
      expectByte(dut, 0xff)
      expectByte(dut, 0x00)
      dut.io.idle.expect(true.B)
    }
  }

  "JpegBitRunPacker should hold output under byte backpressure" in {
    simulate(new JpegBitRunPacker()) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)

      dut.io.flush.poke(false.B)
      dut.io.output.ready.poke(false.B)
      pushRun(dut, 0xaa, 8)

      dut.io.output.valid.expect(true.B)
      dut.io.output.bits.byte.expect(0xaa.U)
      dut.io.input.ready.expect(false.B)
      dut.clock.step()
      dut.io.output.valid.expect(true.B)
      dut.io.output.bits.byte.expect(0xaa.U)

      dut.io.output.ready.poke(true.B)
      expectByte(dut, 0xaa)
      dut.io.idle.expect(true.B)
    }
  }
}
