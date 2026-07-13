// See README.md for license details.

package hjpeg

import chisel3._
import chisel3.simulator.scalatest.ChiselSim
import org.scalatest.freespec.AnyFreeSpec
import org.scalatest.matchers.must.Matchers

import scala.collection.mutable.ArrayBuffer

class PipelinedTransformStagesSpec extends AnyFreeSpec with Matchers with ChiselSim {
  private case class QuantizationConfig(quality: Int, isLuminance: Boolean)

  private def roundShiftSigned(value: Long, shift: Int): Int = {
    val magnitude = math.abs(value)
    val rounded = (magnitude + (1L << (shift - 1))) >> shift
    (if (value < 0) -rounded else rounded).toInt
  }

  private def dctReference(samples: Seq[Int]): Seq[Int] = {
    val cosine = Dct8x8Constants.CosineQ14
    val rows = for {
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
        .map(term => cosine(rowFrequency)(term).toLong * rows(term * HjpegConstants.BlockDim + columnFrequency))
        .sum
      roundShiftSigned(accumulated, Dct8x8Constants.FractionBits * 2)
    }
  }

  private def tableValue(index: Int, config: QuantizationConfig): Int = {
    val table = if (config.isLuminance) JpegTables.StandardLuminanceQuant else JpegTables.StandardChrominanceQuant
    val quality = config.quality.max(1).min(100)
    val scale = if (quality < 50) 5000 / quality else 200 - 2 * quality
    ((table(index) * scale + 50) / 100).max(1).min(255)
  }

  private def quantizeReference(coefficients: Seq[Int], config: QuantizationConfig): Seq[Int] = {
    coefficients.indices.map { index =>
      val value = coefficients(index)
      val divisor = tableValue(index, config)
      val rounded = (math.abs(value) + divisor / 2) / divisor
      if (value < 0) -rounded else rounded
    }
  }

  private def zigZagReference(samples: Seq[Int], config: QuantizationConfig): Seq[Int] =
    JpegTables.ZigZagOrder.map(quantizeReference(dctReference(samples), config))

  private val DirectedSampleBlocks = Seq(
    Seq.fill(HjpegConstants.BlockSize)(5),
    Seq.fill(HjpegConstants.BlockSize)(-128),
    (0 until HjpegConstants.BlockSize).map(index => index / HjpegConstants.BlockDim),
    (0 until HjpegConstants.BlockSize).map(index => index % HjpegConstants.BlockDim),
    (0 until HjpegConstants.BlockSize).map(index => ((index * 73 + 19) & 0xff) - 128),
    (0 until HjpegConstants.BlockSize).map(index => if (((index / 8) + (index % 8)) % 2 == 0) 127 else -128),
    (0 until HjpegConstants.BlockSize).map(index => if (index == 27) 127 else 0),
    (0 until HjpegConstants.BlockSize).map(index => ((index * index * 29 + index * 11 + 7) & 0xff) - 128)
  )
  private val SeededSampleBlocks = (0 until 8).map { seed =>
    (0 until HjpegConstants.BlockSize).map { index =>
      var value = index ^ (0x5eed1234 + seed * 0x10203)
      value ^= value << 13
      value ^= value >>> 17
      value ^= value << 5
      (value & 0xff) - 128
    }
  }
  private val SampleBlocks = DirectedSampleBlocks ++ SeededSampleBlocks

  "PipelinedDct8x8Stage should be bit-exact at a 16-cycle sustained input interval" in {
    simulate(new PipelinedDct8x8Stage()) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)
      dut.io.output.ready.poke(true.B)
      dut.io.input.valid.poke(true.B)

      val acceptedAt = ArrayBuffer.empty[Int]
      val emittedAt = ArrayBuffer.empty[Int]
      var offered = 0
      var emitted = 0
      var cycle = 0
      while (emitted < SampleBlocks.length) {
        assert(cycle < 1000, "timeout streaming pipelined DCT blocks")
        if (offered < SampleBlocks.length) {
          for (index <- 0 until HjpegConstants.BlockSize) {
            dut.io.input.bits.samples(index).poke(SampleBlocks(offered)(index).S)
          }
          dut.io.input.valid.poke(true.B)
        } else {
          dut.io.input.valid.poke(false.B)
        }

        if (dut.io.output.valid.peek().litToBoolean) {
          val expected = dctReference(SampleBlocks(emitted))
          for (index <- 0 until HjpegConstants.BlockSize) {
            dut.io.output.bits.coefficients(index).expect(expected(index).S)
          }
          emittedAt += cycle
          emitted += 1
        }
        if (offered < SampleBlocks.length && dut.io.input.ready.peek().litToBoolean) {
          acceptedAt += cycle
          offered += 1
        }

        dut.clock.step()
        cycle += 1
      }

      val inputIntervals = acceptedAt.sliding(2).map(pair => pair(1) - pair(0)).toSeq
      val outputIntervals = emittedAt.sliding(2).map(pair => pair(1) - pair(0)).toSeq
      val firstLatency = emittedAt.head - acceptedAt.head
      info(s"four-lane DCT first-block latency: $firstLatency cycles")
      info(s"four-lane DCT input intervals: ${inputIntervals.mkString(", ")}")
      info(s"four-lane DCT output intervals: ${outputIntervals.mkString(", ")}")
      firstLatency must be <= 36
      inputIntervals.toSet mustBe Set(16)
      outputIntervals.toSet mustBe Set(16)

      val held = SampleBlocks(4)
      val heldExpected = dctReference(held)
      dut.io.output.ready.poke(false.B)
      dut.io.input.valid.poke(true.B)
      for (index <- 0 until HjpegConstants.BlockSize) {
        dut.io.input.bits.samples(index).poke(held(index).S)
      }
      while (!dut.io.input.ready.peek().litToBoolean) dut.clock.step()
      dut.clock.step()
      dut.io.input.valid.poke(false.B)
      while (!dut.io.output.valid.peek().litToBoolean) dut.clock.step()
      for (_ <- 0 until 5) {
        for (index <- 0 until HjpegConstants.BlockSize) {
          dut.io.output.bits.coefficients(index).expect(heldExpected(index).S)
        }
        dut.clock.step()
      }
      dut.io.output.ready.poke(true.B)
      dut.clock.step()
    }
  }

  "PipelinedQuantizeBlockStage should be exact and sustain one block per 16 cycles" in {
    simulate(new PipelinedQuantizeBlockStage()) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)
      dut.io.output.ready.poke(true.B)

      val configs = Seq(
        QuantizationConfig(1, isLuminance = true),
        QuantizationConfig(37, isLuminance = false),
        QuantizationConfig(50, isLuminance = true),
        QuantizationConfig(100, isLuminance = false),
        QuantizationConfig(0, isLuminance = false),
        QuantizationConfig(127, isLuminance = true)
      )
      val blocks = configs.indices.map { block =>
        (0 until HjpegConstants.BlockSize).map { index =>
          if (block == 5 && index == 0) -32768
          else if (block == 5 && index == 1) 32767
          else {
            val magnitude = ((index * 521 + block * 997) & 0x7fff)
            if (((index + block) & 1) == 0) magnitude else -magnitude
          }
        }
      }

      val acceptedAt = ArrayBuffer.empty[Int]
      val emittedAt = ArrayBuffer.empty[Int]
      var offered = 0
      var emitted = 0
      var cycle = 0
      while (emitted < blocks.length) {
        assert(cycle < 1000, "timeout streaming four-lane quantizer blocks")
        if (offered < blocks.length) {
          dut.io.input.valid.poke(true.B)
          dut.io.quality.poke(configs(offered).quality.U)
          dut.io.isLuminance.poke(configs(offered).isLuminance.B)
          for (index <- 0 until HjpegConstants.BlockSize) {
            dut.io.input.bits.coefficients(index).poke(blocks(offered)(index).S)
          }
        } else {
          dut.io.input.valid.poke(false.B)
        }

        if (dut.io.output.valid.peek().litToBoolean) {
          val expected = quantizeReference(blocks(emitted), configs(emitted))
          for (index <- 0 until HjpegConstants.BlockSize) {
            dut.io.output.bits.coefficients(index).expect(expected(index).S)
          }
          emittedAt += cycle
          emitted += 1
        }
        if (offered < blocks.length && dut.io.input.ready.peek().litToBoolean) {
          acceptedAt += cycle
          offered += 1
        }
        dut.clock.step()
        cycle += 1
      }

      val outputIntervals = emittedAt.sliding(2).map(pair => pair(1) - pair(0)).toSeq
      val firstLatency = emittedAt.head - acceptedAt.head
      info(s"four-lane quantizer first-block latency: $firstLatency cycles")
      info(s"four-lane quantizer output intervals: ${outputIntervals.mkString(", ")}")
      firstLatency must be <= 23
      outputIntervals.toSet mustBe Set(16)
    }
  }

  "JpegBlockTransformStage should preserve pipelined metadata and exact zig-zag coefficients" in {
    simulate(new JpegBlockTransformStage()) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)
      dut.io.output.ready.poke(true.B)

      val configs = SampleBlocks.indices.map { index =>
        QuantizationConfig(Seq(10, 50, 90, 100)(index % 4), isLuminance = (index & 1) == 0)
      }
      val acceptedAt = ArrayBuffer.empty[Int]
      val emittedAt = ArrayBuffer.empty[Int]
      var offered = 0
      var emitted = 0
      var cycle = 0
      while (emitted < SampleBlocks.length) {
        assert(cycle < 1500, "timeout streaming pipelined block transforms")
        if (offered < SampleBlocks.length) {
          dut.io.input.valid.poke(true.B)
          dut.io.quality.poke(configs(offered).quality.U)
          dut.io.isLuminance.poke(configs(offered).isLuminance.B)
          for (index <- 0 until HjpegConstants.BlockSize) {
            dut.io.input.bits.samples(index).poke(SampleBlocks(offered)(index).S)
          }
        } else {
          dut.io.input.valid.poke(false.B)
        }

        if (dut.io.output.valid.peek().litToBoolean) {
          val expected = zigZagReference(SampleBlocks(emitted), configs(emitted))
          for (index <- 0 until HjpegConstants.BlockSize) {
            dut.io.output.bits.coefficients(index).expect(expected(index).S)
          }
          emittedAt += cycle
          emitted += 1
        }
        if (offered < SampleBlocks.length && dut.io.input.ready.peek().litToBoolean) {
          acceptedAt += cycle
          offered += 1
        }
        dut.clock.step()
        cycle += 1
      }

      val inputIntervals = acceptedAt.sliding(2).map(pair => pair(1) - pair(0)).toSeq
      val outputIntervals = emittedAt.sliding(2).map(pair => pair(1) - pair(0)).toSeq
      val firstLatency = emittedAt.head - acceptedAt.head
      info(s"complete pipelined transform first-block latency: $firstLatency cycles")
      info(s"pipelined transform input intervals: ${inputIntervals.mkString(", ")}")
      info(s"pipelined transform output intervals: ${outputIntervals.mkString(", ")}")
      firstLatency must be <= 59
      inputIntervals.toSet mustBe Set(16)
      outputIntervals.toSet mustBe Set(16)

      // Reuse the same elaborated model for an ordered backpressure pass. Check
      // the presented block every valid cycle so held data is also covered.
      offered = 0
      emitted = 0
      var stalledCycle = 0
      while (emitted < SampleBlocks.length) {
        assert(stalledCycle < 2000, "timeout streaming backpressured pipelined block transforms")
        val ready = (stalledCycle % 5) != 2 && (stalledCycle % 11) != 7
        dut.io.output.ready.poke(ready.B)
        if (offered < SampleBlocks.length) {
          dut.io.input.valid.poke(true.B)
          dut.io.quality.poke(configs(offered).quality.U)
          dut.io.isLuminance.poke(configs(offered).isLuminance.B)
          for (index <- 0 until HjpegConstants.BlockSize) {
            dut.io.input.bits.samples(index).poke(SampleBlocks(offered)(index).S)
          }
        } else {
          dut.io.input.valid.poke(false.B)
        }

        if (dut.io.output.valid.peek().litToBoolean) {
          val expected = zigZagReference(SampleBlocks(emitted), configs(emitted))
          for (index <- 0 until HjpegConstants.BlockSize) {
            dut.io.output.bits.coefficients(index).expect(expected(index).S)
          }
          if (ready) emitted += 1
        }
        if (offered < SampleBlocks.length && dut.io.input.ready.peek().litToBoolean) offered += 1
        dut.clock.step()
        stalledCycle += 1
      }
      dut.io.input.valid.poke(false.B)
      dut.io.output.ready.poke(true.B)
    }
  }
}
