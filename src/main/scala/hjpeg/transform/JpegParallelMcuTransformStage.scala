// See README.md for license details.

package hjpeg

import chisel3._
import chisel3.util._

class ParallelMcuTransformBatchMetadata extends Bundle {
  val subsampled = Bool()
  val finalBatch = Bool()
  val lastMcu = Bool()
}

/** Ordered three-lane transform pipeline for complete JPEG MCUs.
  *
  * A 4:4:4 MCU issues Y/Cb/Cr in one batch. A 4:2:0 MCU issues Y0/Y1/Y2 and
  * Y3/Cb/Cr in two batches. Batch metadata follows the transform pipelines so
  * several MCUs may be in flight while the single ordered output is held under
  * backpressure.
  */
class JpegParallelMcuTransformStage(sampleBits: Int = 9, coefficientBits: Int = 16) extends Module {
  val io = IO(new Bundle {
    val input = Flipped(Decoupled(new LevelShiftedMinimumCodedUnitPacket(sampleBits)))
    val output = Decoupled(new ZigZagMinimumCodedUnitPacket(coefficientBits))
  })

  val rawMcu = Reg(new LevelShiftedMinimumCodedUnitPacket(sampleBits))
  val rawValid = RegInit(false.B)
  val issueBatch = RegInit(0.U(1.W))

  val transform = Module(new JpegBlockTransformStage(sampleBits, coefficientBits))
  val transform1 = Module(new JpegBlockTransformStage(sampleBits, coefficientBits))
  val transform2 = Module(new JpegBlockTransformStage(sampleBits, coefficientBits))
  val transforms = Seq(transform, transform1, transform2)
  val metadata = Module(new Queue(new ParallelMcuTransformBatchMetadata, entries = 8, pipe = true))

  val subsampled = rawMcu.mcu.yBlockCount === 4.U
  val batchCount = Mux(subsampled, 2.U, 1.U)
  val batchPending = rawValid && issueBatch < batchCount
  val inputReadies = VecInit(transforms.map(_.io.input.ready))

  for ((laneTransform, lane) <- transforms.zipWithIndex) {
    laneTransform.io.quality := rawMcu.mcu.quality
    laneTransform.io.isLuminance := Mux(
      subsampled,
      if (lane == 0) true.B else issueBatch === 0.U,
      (lane == 0).B)
    val otherLanesReady =
      (0 until transforms.length).filter(_ != lane).map(inputReadies(_)).reduce(_ && _)
    laneTransform.io.input.valid := batchPending && metadata.io.enq.ready && otherLanesReady

    for (sample <- 0 until HjpegConstants.BlockSize) {
      val sample420 = lane match {
        case 0 => Mux(issueBatch === 0.U, rawMcu.mcu.y.samples(sample), rawMcu.mcu.y3.samples(sample))
        case 1 => Mux(issueBatch === 0.U, rawMcu.mcu.y1.samples(sample), rawMcu.mcu.cb.samples(sample))
        case _ => Mux(issueBatch === 0.U, rawMcu.mcu.y2.samples(sample), rawMcu.mcu.cr.samples(sample))
      }
      val sample444 = lane match {
        case 0 => rawMcu.mcu.y.samples(sample)
        case 1 => rawMcu.mcu.cb.samples(sample)
        case _ => rawMcu.mcu.cr.samples(sample)
      }
      laneTransform.io.input.bits.samples(sample) := Mux(subsampled, sample420, sample444)
    }
  }

  val batchInputFire = batchPending && metadata.io.enq.ready && inputReadies.asUInt.andR
  val finalBatchInputFire = batchInputFire && issueBatch === batchCount - 1.U
  metadata.io.enq.valid := batchPending && inputReadies.asUInt.andR
  metadata.io.enq.bits.subsampled := subsampled
  metadata.io.enq.bits.finalBatch := issueBatch === batchCount - 1.U
  metadata.io.enq.bits.lastMcu := rawMcu.last

  // The next raw MCU can replace the current register in the same cycle that
  // the current MCU's final batch enters all three pipelines.
  io.input.ready := !rawValid || finalBatchInputFire
  when(io.input.fire) {
    rawMcu := io.input.bits
    rawValid := true.B
    issueBatch := 0.U
  }.elsewhen(batchInputFire) {
    when(finalBatchInputFire) {
      rawValid := false.B
      issueBatch := 0.U
    }.otherwise {
      issueBatch := issueBatch + 1.U
    }
  }

  val outputMcu = Reg(new ZigZagMinimumCodedUnitPacket(coefficientBits))
  val outputValid = RegInit(false.B)
  val firstBatchY0 = Reg(new ZigZagCoefficientBlock(coefficientBits))
  val firstBatchY1 = Reg(new ZigZagCoefficientBlock(coefficientBits))
  val firstBatchY2 = Reg(new ZigZagCoefficientBlock(coefficientBits))
  val firstBatchValid = RegInit(false.B)

  io.output.valid := outputValid
  io.output.bits := outputMcu
  val outputSlotReady = !outputValid || io.output.ready
  val outputMetadata = metadata.io.deq.bits
  val completesMcu = !outputMetadata.subsampled || outputMetadata.finalBatch
  val batchCanRetire = metadata.io.deq.valid && Mux(completesMcu, outputSlotReady, !firstBatchValid)
  val outputValids = VecInit(transforms.map(_.io.output.valid))

  for ((laneTransform, lane) <- transforms.zipWithIndex) {
    val otherLanesValid =
      (0 until transforms.length).filter(_ != lane).map(outputValids(_)).reduce(_ && _)
    laneTransform.io.output.ready := batchCanRetire && otherLanesValid
  }

  val batchOutputFire = batchCanRetire && outputValids.asUInt.andR
  metadata.io.deq.ready := batchOutputFire
  val completedMcuFire = batchOutputFire && completesMcu

  when(io.output.fire && !completedMcuFire) {
    outputValid := false.B
  }

  when(batchOutputFire) {
    when(outputMetadata.subsampled && !outputMetadata.finalBatch) {
      assert(!firstBatchValid, "4:2:0 transform assembly already contains a first batch")
      firstBatchY0 := transform.io.output.bits
      firstBatchY1 := transform1.io.output.bits
      firstBatchY2 := transform2.io.output.bits
      firstBatchValid := true.B
    }.otherwise {
      when(outputMetadata.subsampled) {
        assert(firstBatchValid, "4:2:0 transform final batch is missing its first batch")
      }.otherwise {
        assert(!firstBatchValid, "4:4:4 transform output cannot follow a partial 4:2:0 MCU")
      }

      outputMcu.mcu.yBlockCount := Mux(outputMetadata.subsampled, 4.U, 1.U)
      outputMcu.mcu.y := Mux(outputMetadata.subsampled, firstBatchY0, transform.io.output.bits)
      outputMcu.mcu.y1 := Mux(outputMetadata.subsampled, firstBatchY1, transform.io.output.bits)
      outputMcu.mcu.y2 := Mux(outputMetadata.subsampled, firstBatchY2, transform.io.output.bits)
      outputMcu.mcu.y3 := transform.io.output.bits
      outputMcu.mcu.cb := transform1.io.output.bits
      outputMcu.mcu.cr := transform2.io.output.bits
      outputMcu.last := outputMetadata.lastMcu
      outputValid := true.B
      firstBatchValid := false.B
    }
  }
}
