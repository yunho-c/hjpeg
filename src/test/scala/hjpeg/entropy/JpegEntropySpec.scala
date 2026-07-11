// See README.md for license details.

package hjpeg

import chisel3._
import chisel3.simulator.scalatest.ChiselSim
import org.scalatest.freespec.AnyFreeSpec
import org.scalatest.matchers.must.Matchers

class JpegEntropySpec extends AnyFreeSpec with Matchers with ChiselSim {
  "JpegMagnitudeValue should compute JPEG category and amplitude bits" in {
    simulate(new JpegMagnitudeValue(17)) { dut =>
      val cases = Seq(
        (0, 0, 0),
        (1, 1, 1),
        (-1, 1, 0),
        (5, 3, 5),
        (-5, 3, 2),
        (127, 7, 127),
        (-128, 8, 127),
        (511, 9, 511),
        (-511, 9, 0)
      )

      for ((value, category, amplitude) <- cases) {
        dut.io.value.poke(value.S)
        dut.io.category.expect(category.U)
        dut.io.amplitude.expect(amplitude.U)
      }
    }
  }

  "JpegDcHuffmanCode should return default baseline DC table entries" in {
    simulate(new JpegDcHuffmanCode()) { dut =>
      dut.io.isLuminance.poke(true.B)
      dut.io.category.poke(0.U)
      dut.io.code.expect(0.U)
      dut.io.length.expect(2.U)
      dut.io.valid.expect(true.B)

      dut.io.category.poke(3.U)
      dut.io.code.expect("b100".U)
      dut.io.length.expect(3.U)

      dut.io.category.poke(11.U)
      dut.io.code.expect("b111111110".U)
      dut.io.length.expect(9.U)

      dut.io.isLuminance.poke(false.B)
      dut.io.category.poke(0.U)
      dut.io.code.expect("b00".U)
      dut.io.length.expect(2.U)

      dut.io.category.poke(2.U)
      dut.io.code.expect("b10".U)
      dut.io.length.expect(2.U)

      dut.io.category.poke(3.U)
      dut.io.code.expect("b110".U)
      dut.io.length.expect(3.U)

      dut.io.category.poke(12.U)
      dut.io.valid.expect(false.B)
    }
  }

  "JpegAcHuffmanCode should return default baseline AC table entries" in {
    simulate(new JpegAcHuffmanCode()) { dut =>
      dut.io.isLuminance.poke(true.B)
      dut.io.symbol.poke(0x01.U)
      dut.io.code.expect("b00".U)
      dut.io.length.expect(2.U)
      dut.io.valid.expect(true.B)

      dut.io.symbol.poke(0x03.U)
      dut.io.code.expect("b100".U)
      dut.io.length.expect(3.U)

      dut.io.symbol.poke(0x00.U)
      dut.io.code.expect("b1010".U)
      dut.io.length.expect(4.U)

      dut.io.symbol.poke(0xf0.U)
      dut.io.code.expect("b11111111001".U)
      dut.io.length.expect(11.U)

      dut.io.isLuminance.poke(false.B)
      dut.io.symbol.poke(0x00.U)
      dut.io.code.expect("b00".U)
      dut.io.length.expect(2.U)

      dut.io.symbol.poke(0x02.U)
      dut.io.code.expect("b100".U)
      dut.io.length.expect(3.U)

      dut.io.symbol.poke(0xff.U)
      dut.io.valid.expect(false.B)
    }
  }

  "JpegDcEncodeStage should difference DC coefficients and emit token fields" in {
    simulate(new JpegDcEncodeStage()) { dut =>
      dut.io.current.poke(10.S)
      dut.io.previous.poke(3.S)
      dut.io.isLuminance.poke(true.B)
      dut.io.difference.expect(7.S)
      dut.io.token.huffmanCode.expect("b100".U)
      dut.io.token.huffmanLength.expect(3.U)
      dut.io.token.amplitude.expect(7.U)
      dut.io.token.amplitudeLength.expect(3.U)
      dut.io.valid.expect(true.B)

      dut.io.current.poke((-2).S)
      dut.io.previous.poke(3.S)
      dut.io.isLuminance.poke(false.B)
      dut.io.difference.expect((-5).S)
      dut.io.token.huffmanCode.expect("b110".U)
      dut.io.token.huffmanLength.expect(3.U)
      dut.io.token.amplitude.expect(2.U)
      dut.io.token.amplitudeLength.expect(3.U)
      dut.io.valid.expect(true.B)
    }
  }

  "JpegAcEncodeStage should emit EOB, ZRL, and coefficient tokens" in {
    simulate(new JpegAcEncodeStage()) { dut =>
      dut.io.isLuminance.poke(true.B)
      dut.io.runLength.poke(0.U)
      dut.io.coefficient.poke(0.S)
      dut.io.emitEndOfBlock.poke(true.B)
      dut.io.emitZeroRunLength.poke(false.B)
      dut.io.symbol.expect(0x00.U)
      dut.io.token.huffmanCode.expect("b1010".U)
      dut.io.token.huffmanLength.expect(4.U)
      dut.io.token.amplitude.expect(0.U)
      dut.io.token.amplitudeLength.expect(0.U)
      dut.io.valid.expect(true.B)

      dut.io.emitEndOfBlock.poke(false.B)
      dut.io.emitZeroRunLength.poke(true.B)
      dut.io.symbol.expect(0xf0.U)
      dut.io.token.huffmanCode.expect("b11111111001".U)
      dut.io.token.huffmanLength.expect(11.U)
      dut.io.token.amplitudeLength.expect(0.U)
      dut.io.valid.expect(true.B)

      dut.io.emitZeroRunLength.poke(false.B)
      dut.io.runLength.poke(0.U)
      dut.io.coefficient.poke(5.S)
      dut.io.symbol.expect(0x03.U)
      dut.io.token.huffmanCode.expect("b100".U)
      dut.io.token.huffmanLength.expect(3.U)
      dut.io.token.amplitude.expect(5.U)
      dut.io.token.amplitudeLength.expect(3.U)
      dut.io.valid.expect(true.B)

      dut.io.runLength.poke(2.U)
      dut.io.coefficient.poke((-5).S)
      dut.io.symbol.expect(0x23.U)
      dut.io.token.amplitude.expect(2.U)
      dut.io.token.amplitudeLength.expect(3.U)
      dut.io.valid.expect(true.B)

      dut.io.coefficient.poke(0.S)
      dut.io.valid.expect(false.B)
    }
  }
}
