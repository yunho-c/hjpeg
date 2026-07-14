// See README.md for license details.

package hjpeg.performance

import chisel3._
import chisel3.util._
import chisel3.util.experimental.BoringUtils
import chisel3.simulator.scalatest.ChiselSim
import hjpeg._
import java.io.ByteArrayInputStream
import javax.imageio.ImageIO
import org.scalatest.freespec.AnyFreeSpec
import org.scalatest.matchers.must.Matchers

import scala.collection.mutable.ArrayBuffer

/** Test-only wrapper around the exact four-pixel KV260 stream path. */
private class HjpegUhdPerformanceHarness extends Module {
  private val c = HjpegTargetConfigs.Kv260Uhd4k

  val io = IO(new Bundle {
    val config = Input(new FrameConfig(c))
    val clearProtocolError = Input(Bool())
    val input = Flipped(Decoupled(new AxiStreamWord(128)))
    val output = Decoupled(new AxiStreamWord(c.outputDataBits))
    val busy = Output(Bool())
    val protocolError = Output(Bool())
    val mcuOutputFire = Output(Bool())
  })

  val top = Module(new HjpegKv260Top(c, inputPixelsPerBeat = 4))
  top.io.config := io.config
  top.io.clearProtocolError := io.clearProtocolError
  top.io.sAxisRgb.valid := io.input.valid
  top.io.sAxisRgb.bits := io.input.bits
  io.input.ready := top.io.sAxisRgb.ready
  io.output.valid := top.io.mAxisJpeg.valid
  io.output.bits := top.io.mAxisJpeg.bits
  top.io.mAxisJpeg.ready := io.output.ready
  io.busy := top.io.busy
  io.protocolError := top.io.protocolError
  private val mcuOutputValid = BoringUtils.bore(top.core.core.rasterToMcu.io.output.valid)
  private val mcuOutputReady = BoringUtils.bore(top.core.core.rasterToMcu.io.output.ready)
  io.mcuOutputFire := mcuOutputValid && mcuOutputReady
}

class HjpegUhdPerformanceSpec extends AnyFreeSpec with Matchers with ChiselSim {
  private val Width = 512
  private val Height = 128
  private val Pixels = Width * Height
  private val ClockHz = 150_000_000.0
  private val TargetFramesPerSecond = 60.0
  private val UhdPixels = 3840 * 2160
  private val UhdFrameCycleBudget = ClockHz / TargetFramesPerSecond
  private val NormalizedCyclesPerPixelBudget = UhdFrameCycleBudget / UhdPixels

  private case class Result(
      sampling: String,
      frameCycles: Int,
      inputSpanCycles: Int,
      mcuSpanCycles: Int,
      mcuCount: Int,
      jpegBytes: Int) {
    val frameCyclesPerPixel: Double = frameCycles.toDouble / Pixels
    val inputCyclesPerPixel: Double = inputSpanCycles.toDouble / Pixels
    val mcuCyclesPerMcu: Double = mcuSpanCycles.toDouble / mcuCount
    val nonMcuOverheadCycles: Int = frameCycles - mcuSpanCycles
    def projectedUhdCycles(uhdMcuCount: Int): Double =
      mcuCyclesPerMcu * uhdMcuCount + nonMcuOverheadCycles
  }

  private def gradientCheckerPixel(index: Int): (Int, Int, Int) = {
    val x = index % Width
    val y = index / Width
    val checker = if ((((x / 8) ^ (y / 8)) & 1) != 0) 48 else 0
    val r = x * 255 / (Width - 1)
    val g = y * 255 / (Height - 1)
    val b = math.min(((x + y) * 127 / (Width + Height - 2)) + checker, 255)
    (r, g, b)
  }

  private def packedBeat(firstPixel: Int): BigInt =
    (0 until 4).foldLeft(BigInt(0)) { (packed, lane) =>
      val (r, g, b) = gradientCheckerPixel(firstPixel + lane)
      val word = BigInt(r) | (BigInt(g) << 8) | (BigInt(b) << 16)
      packed | (word << (lane * 32))
    }

  private def runMode(dut: HjpegUhdPerformanceHarness, subsample: Boolean): Result = {
    dut.reset.poke(true.B)
    dut.io.input.valid.poke(false.B)
    dut.io.input.bits.data.poke(0.U)
    dut.io.input.bits.keep.poke(0.U)
    dut.io.input.bits.last.poke(false.B)
    dut.io.output.ready.poke(true.B)
    dut.io.clearProtocolError.poke(false.B)
    dut.clock.step(2)
    dut.reset.poke(false.B)

    dut.io.config.xsize.poke(Width.U)
    dut.io.config.ysize.poke(Height.U)
    dut.io.config.quality.poke(85.U)
    dut.io.config.restartInterval.poke(0.U)
    dut.io.config.enableChromaSubsample.poke(subsample.B)
    dut.io.config.emitJfif.poke(true.B)

    val bytes = ArrayBuffer.empty[Int]
    val mcuCycles = ArrayBuffer.empty[Int]
    var nextPixel = 0
    var firstInputCycle = -1
    var lastInputCycle = -1
    var lastOutputCycle = -1
    var cycle = 0
    var done = false

    while (!done) {
      assert(cycle < Pixels * 8, "timeout waiting for UHD-vector JPEG output")

      val inputValid = nextPixel < Pixels
      dut.io.input.valid.poke(inputValid.B)
      if (inputValid) {
        dut.io.input.bits.data.poke(packedBeat(nextPixel).U)
        dut.io.input.bits.keep.poke("hffff".U)
        dut.io.input.bits.last.poke((nextPixel == Pixels - 4).B)
      }

      if (inputValid && dut.io.input.ready.peek().litToBoolean) {
        if (firstInputCycle < 0) firstInputCycle = cycle
        lastInputCycle = cycle
        nextPixel += 4
      }
      if (dut.io.mcuOutputFire.peek().litToBoolean) {
        mcuCycles += cycle
      }
      if (dut.io.output.valid.peek().litToBoolean) {
        dut.io.output.bits.keep.expect(1.U)
        bytes += dut.io.output.bits.data.peek().litValue.toInt
        if (dut.io.output.bits.last.peek().litToBoolean) {
          lastOutputCycle = cycle
          done = true
        }
      }

      dut.clock.step()
      cycle += 1
    }

    dut.io.input.valid.poke(false.B)
    dut.io.protocolError.expect(false.B)
    nextPixel mustBe Pixels
    firstInputCycle must be >= 0
    lastInputCycle must be >= firstInputCycle
    lastOutputCycle must be >= lastInputCycle

    val image = ImageIO.read(new ByteArrayInputStream(bytes.map(_.toByte).toArray))
    image must not be null
    image.getWidth mustBe Width
    image.getHeight mustBe Height

    val expectedMcus =
      if (subsample) (Width / 16) * (Height / 16)
      else (Width / 8) * (Height / 8)
    mcuCycles.size mustBe expectedMcus

    Result(
      if (subsample) "4:2:0" else "4:4:4",
      lastOutputCycle - firstInputCycle + 1,
      lastInputCycle - firstInputCycle + 1,
      mcuCycles.last - mcuCycles.head + 1,
      mcuCycles.size,
      bytes.size)
  }

  "four-pixel q85 gradient/checker should meet normalized UHD simulation budgets" in {
    simulate(new HjpegUhdPerformanceHarness) { dut =>
      val results = Seq(runMode(dut, subsample = false), runMode(dut, subsample = true))

      results.foreach { result =>
        val uhdMcuCount = if (result.sampling == "4:2:0") 32_400 else 129_600
        val projectedCycles = result.projectedUhdCycles(uhdMcuCount)
        info(
          f"${result.sampling}: frame=${result.frameCycles}%d cycles " +
            f"(${result.frameCyclesPerPixel}%.6f cycles/pixel), " +
            f"input=${result.inputCyclesPerPixel}%.6f cycles/pixel, " +
            f"MCU=${result.mcuCyclesPerMcu}%.3f cycles/MCU, " +
            f"fixed=${result.nonMcuOverheadCycles}%d cycles, " +
            f"projected-UHD=$projectedCycles%.1f cycles, " +
            f"JPEG=${result.jpegBytes}%d bytes")
      }

      results.foreach { result =>
        val uhdMcuCount = if (result.sampling == "4:2:0") 32_400 else 129_600
        val mcuBudget = UhdFrameCycleBudget / uhdMcuCount

        withClue(s"${result.sampling} input acceptance: ") {
          result.inputCyclesPerPixel must be <= NormalizedCyclesPerPixelBudget
        }
        withClue(s"${result.sampling} MCU cadence: ") {
          result.mcuCyclesPerMcu must be <= mcuBudget
        }
        withClue(s"${result.sampling} projected UHD frame: ") {
          result.projectedUhdCycles(uhdMcuCount) must be <= UhdFrameCycleBudget
        }
      }
    }
  }
}
