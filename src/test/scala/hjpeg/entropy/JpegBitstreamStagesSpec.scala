// See README.md for license details.

package hjpeg

import chisel3._
import chisel3.simulator.scalatest.ChiselSim
import org.scalatest.freespec.AnyFreeSpec
import org.scalatest.matchers.must.Matchers

import scala.collection.mutable.ArrayBuffer
import scala.util.Random

class JpegBitstreamStagesSpec extends AnyFreeSpec with Matchers with ChiselSim {
  private def referenceBytes(runs: Seq[(BigInt, Int)]): Seq[Int] = {
    val bits = ArrayBuffer.empty[Int]
    runs.foreach { case (value, length) =>
      for (index <- (length - 1) to 0 by -1) {
        bits += ((value >> index) & 1).toInt
      }
    }
    while (bits.length % 8 != 0) bits += 1

    bits.grouped(8).flatMap { byteBits =>
      val byte = byteBits.foldLeft(0)((value, bit) => (value << 1) | bit)
      if (byte == 0xff) Seq(0xff, 0x00) else Seq(byte)
    }.toSeq
  }

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

  "JpegBitRunPacker should accept a run while emitting a data byte" in {
    simulate(new JpegBitRunPacker()) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)

      dut.io.flush.poke(false.B)
      dut.io.output.ready.poke(true.B)
      pushRun(dut, 0xaa, 8)

      dut.io.input.valid.poke(true.B)
      dut.io.input.bits.bits.poke(0x55.U)
      dut.io.input.bits.length.poke(8.U)
      dut.io.input.ready.expect(true.B)
      dut.io.output.valid.expect(true.B)
      dut.io.output.bits.byte.expect(0xaa.U)
      dut.clock.step()
      dut.io.input.valid.poke(false.B)

      expectByte(dut, 0x55)
      dut.io.idle.expect(true.B)
    }
  }

  "JpegBitRunPacker should preserve stuffing when accepting with a 0xff byte" in {
    simulate(new JpegBitRunPacker()) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)

      dut.io.flush.poke(false.B)
      dut.io.output.ready.poke(true.B)
      pushRun(dut, 0xff, 8)

      dut.io.input.valid.poke(true.B)
      dut.io.input.bits.bits.poke(0xa5.U)
      dut.io.input.bits.length.poke(8.U)
      dut.io.input.ready.expect(true.B)
      dut.io.output.bits.byte.expect(0xff.U)
      dut.clock.step()
      dut.io.input.valid.poke(false.B)

      expectByte(dut, 0x00)
      expectByte(dut, 0xa5)
      dut.io.idle.expect(true.B)
    }
  }

  "JpegBitRunPacker should match a software packer under sustained runs and stalls" in {
    simulate(new JpegBitRunPacker()) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)

      val random = new Random(0xb17ca11L)
      val runs = Seq((BigInt(0xff), 8), (BigInt(0), 3), (BigInt(0x1f), 5)) ++ Seq.fill(48) {
        val length = random.nextInt(16) + 1
        (BigInt(length, random), length)
      }
      val received = ArrayBuffer.empty[Int]
      var nextRun = 0
      var cycle = 0
      var done = false

      while (!done) {
        assert(cycle < 1000, "timeout draining sustained bit runs")
        val outputReady = cycle % 5 != 2
        dut.io.output.ready.poke(outputReady.B)
        dut.io.flush.poke((nextRun == runs.length).B)

        if (nextRun < runs.length) {
          val (bits, length) = runs(nextRun)
          dut.io.input.valid.poke(true.B)
          dut.io.input.bits.bits.poke(bits.U)
          dut.io.input.bits.length.poke(length.U)
        } else {
          dut.io.input.valid.poke(false.B)
        }

        val inputFire =
          nextRun < runs.length && dut.io.input.ready.peek().litToBoolean
        val outputFire =
          outputReady && dut.io.output.valid.peek().litToBoolean
        if (outputFire) received += dut.io.output.bits.byte.peek().litValue.toInt

        dut.clock.step()
        if (inputFire) nextRun += 1
        done = nextRun == runs.length && dut.io.idle.peek().litToBoolean
        cycle += 1
      }

      received.toSeq mustBe referenceBytes(runs)
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
