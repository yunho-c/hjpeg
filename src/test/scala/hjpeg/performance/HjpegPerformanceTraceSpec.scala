// See README.md for license details.

package hjpeg.performance

import chisel3._
import chisel3.util._
import chisel3.util.experimental.BoringUtils
import chisel3.simulator.scalatest.ChiselSim
import hjpeg._
import java.io.ByteArrayInputStream
import java.io.PrintWriter
import java.nio.file.{Files, Path, Paths}
import javax.imageio.ImageIO
import org.scalatest.freespec.AnyFreeSpec
import org.scalatest.matchers.must.Matchers

import scala.collection.mutable.ArrayBuffer

private class PerformanceBoundaryProbe extends Bundle {
  val valid = Bool()
  val ready = Bool()
}

private class HjpegPerformanceProbe extends Bundle {
  val transformInput = new PerformanceBoundaryProbe
  val dctInput = new PerformanceBoundaryProbe
  val dctOutput = new PerformanceBoundaryProbe
  val quantizeInput = new PerformanceBoundaryProbe
  val quantizeOutput = new PerformanceBoundaryProbe
  val zigZagInput = new PerformanceBoundaryProbe
  val zigZagOutput = new PerformanceBoundaryProbe
  val transformOutput = new PerformanceBoundaryProbe
  val mcuOutput = new PerformanceBoundaryProbe
  val entropyBlockInput = new PerformanceBoundaryProbe
  val entropyRunOutput = new PerformanceBoundaryProbe
  val packerOutput = new PerformanceBoundaryProbe
}

/** Test-only wrapper that bores internal ready/valid boundaries to top-level
  * simulation ports. The production `HjpegCore` RTL and public IO are
  * unchanged.
  */
private class HjpegPerformanceHarness(c: HjpegConfig = HjpegConfig()) extends Module {
  val io = IO(new Bundle {
    val config = Input(new FrameConfig(c))
    val clearProtocolError = Input(Bool())
    val input = Flipped(Decoupled(new RgbPixel(c)))
    val output = Decoupled(new EncodedByte(c))
    val busy = Output(Bool())
    val protocolError = Output(Bool())
    val performance = Output(new HjpegPerformanceProbe)
  })

  val core = Module(new HjpegCore(c))
  core.io.config := io.config
  core.io.clearProtocolError := io.clearProtocolError
  core.io.input <> io.input
  io.output <> core.io.output
  io.busy := core.io.busy
  io.protocolError := core.io.protocolError

  private def boreBoundary(valid: Bool, ready: Bool): PerformanceBoundaryProbe = {
    val result = Wire(new PerformanceBoundaryProbe)
    result.valid := BoringUtils.bore(valid)
    result.ready := BoringUtils.bore(ready)
    result
  }

  private def selectBoundary(
      normalValid: Bool,
      normalReady: Bool,
      subsampledValid: Bool,
      subsampledReady: Bool): PerformanceBoundaryProbe = {
    val normal = boreBoundary(normalValid, normalReady)
    val subsampled = boreBoundary(subsampledValid, subsampledReady)
    Mux(io.config.enableChromaSubsample, subsampled, normal)
  }

  private val normalTransform = core.rasterToMcu.transform
  private val subsampledTransform = core.rasterToSubsampledMcu.transform

  io.performance.transformInput := selectBoundary(
    normalTransform.io.input.valid,
    normalTransform.io.input.ready,
    subsampledTransform.io.input.valid,
    subsampledTransform.io.input.ready)
  io.performance.dctInput := selectBoundary(
    normalTransform.dct.io.input.valid,
    normalTransform.dct.io.input.ready,
    subsampledTransform.dct.io.input.valid,
    subsampledTransform.dct.io.input.ready)
  io.performance.dctOutput := selectBoundary(
    normalTransform.dct.io.output.valid,
    normalTransform.dct.io.output.ready,
    subsampledTransform.dct.io.output.valid,
    subsampledTransform.dct.io.output.ready)
  io.performance.quantizeInput := selectBoundary(
    normalTransform.quantize.io.input.valid,
    normalTransform.quantize.io.input.ready,
    subsampledTransform.quantize.io.input.valid,
    subsampledTransform.quantize.io.input.ready)
  io.performance.quantizeOutput := selectBoundary(
    normalTransform.quantize.io.output.valid,
    normalTransform.quantize.io.output.ready,
    subsampledTransform.quantize.io.output.valid,
    subsampledTransform.quantize.io.output.ready)
  io.performance.zigZagInput := selectBoundary(
    normalTransform.zigZag.io.input.valid,
    normalTransform.zigZag.io.input.ready,
    subsampledTransform.zigZag.io.input.valid,
    subsampledTransform.zigZag.io.input.ready)
  io.performance.zigZagOutput := selectBoundary(
    normalTransform.zigZag.io.output.valid,
    normalTransform.zigZag.io.output.ready,
    subsampledTransform.zigZag.io.output.valid,
    subsampledTransform.zigZag.io.output.ready)
  io.performance.transformOutput := selectBoundary(
    normalTransform.io.output.valid,
    normalTransform.io.output.ready,
    subsampledTransform.io.output.valid,
    subsampledTransform.io.output.ready)
  io.performance.mcuOutput := selectBoundary(
    core.rasterToMcu.io.output.valid,
    core.rasterToMcu.io.output.ready,
    core.rasterToSubsampledMcu.io.output.valid,
    core.rasterToSubsampledMcu.io.output.ready)
  io.performance.entropyBlockInput := boreBoundary(
    core.encoder.blockEncoder.io.input.valid,
    core.encoder.blockEncoder.io.input.ready)
  io.performance.entropyRunOutput := boreBoundary(
    core.encoder.blockEncoder.io.output.valid,
    core.encoder.blockEncoder.io.output.ready)
  io.performance.packerOutput := boreBoundary(
    core.encoder.packer.io.output.valid,
    core.encoder.packer.io.output.ready)
}

class HjpegPerformanceTraceSpec extends AnyFreeSpec with Matchers with ChiselSim {
  private val Width = 32
  private val Height = 16
  private val ClockHz = 100_000_000L
  private val CaptureDirectoryEnvironment = "HJPEG_PERFORMANCE_CAPTURE_DIR"
  private val ScenarioEnvironment = "HJPEG_PERFORMANCE_SCENARIOS"
  private val SupportedScenarios = Seq("444", "420", "444-output-stalls")

  private case class BoundarySample(
      scenario: String,
      cycle: Int,
      boundary: String,
      valid: Boolean,
      ready: Boolean)

  private case class ScenarioResult(
      name: String,
      sampling: String,
      readyPattern: String,
      firstInputCycle: Int,
      lastOutputCycle: Int,
      pixels: Int,
      bytes: Seq[Int],
      mcus: Int,
      blocks: Int,
      samples: Seq[BoundarySample]) {
    val frameCycles: Int = lastOutputCycle - firstInputCycle + 1
  }

  private def pixelAt(index: Int): (Int, Int, Int) = {
    val x = index % Width
    val y = index / Width
    val value = (x * 13 + y * 17) & 0xff
    (value, 255 - value, (value / 2 + 64) & 0xff)
  }

  private def pokeConfig(dut: HjpegPerformanceHarness, subsample: Boolean): Unit = {
    dut.io.config.xsize.poke(Width.U)
    dut.io.config.ysize.poke(Height.U)
    dut.io.config.quality.poke(50.U)
    dut.io.config.restartInterval.poke(0.U)
    dut.io.config.enableChromaSubsample.poke(subsample.B)
    dut.io.config.emitJfif.poke(true.B)
  }

  private def sample(
      samples: ArrayBuffer[BoundarySample],
      scenario: String,
      cycle: Int,
      boundary: String,
      valid: Bool,
      ready: Bool): Unit = {
    samples += BoundarySample(
      scenario,
      cycle,
      boundary,
      valid.peek().litToBoolean,
      ready.peek().litToBoolean)
  }

  private def runScenario(name: String, retainSamples: Boolean = true): ScenarioResult = {
    val subsample = name == "420"
    val stalls = name == "444-output-stalls"
    var result: ScenarioResult = null

    simulate(new HjpegPerformanceHarness()) { dut =>
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)
      pokeConfig(dut, subsample)
      dut.io.clearProtocolError.poke(false.B)

      val samples = ArrayBuffer.empty[BoundarySample]
      val bytes = ArrayBuffer.empty[Int]
      val pixels = Width * Height
      var nextPixel = 0
      var firstInputCycle = -1
      var lastOutputCycle = -1
      var mcuCount = 0
      var blockCount = 0
      var cycle = 0
      var done = false

      while (!done) {
        assert(cycle < pixels * 4096 + JpegHeaderBytes.MaxHeaderLength + 4096, s"timeout tracing $name")

        val outputReady = !stalls || ((cycle % 4) != 1 && (cycle % 9) != 5)
        dut.io.output.ready.poke(outputReady.B)

        if (nextPixel < pixels) {
          val (r, g, b) = pixelAt(nextPixel)
          dut.io.input.valid.poke(true.B)
          dut.io.input.bits.x.poke((nextPixel % Width).U)
          dut.io.input.bits.y.poke((nextPixel / Width).U)
          dut.io.input.bits.r.poke(r.U)
          dut.io.input.bits.g.poke(g.U)
          dut.io.input.bits.b.poke(b.U)
        } else {
          dut.io.input.valid.poke(false.B)
        }

        sample(samples, name, cycle, "rgb_input", dut.io.input.valid, dut.io.input.ready)
        sample(samples, name, cycle, "transform_input", dut.io.performance.transformInput.valid, dut.io.performance.transformInput.ready)
        sample(samples, name, cycle, "dct_input", dut.io.performance.dctInput.valid, dut.io.performance.dctInput.ready)
        sample(samples, name, cycle, "dct_output", dut.io.performance.dctOutput.valid, dut.io.performance.dctOutput.ready)
        sample(samples, name, cycle, "quantize_input", dut.io.performance.quantizeInput.valid, dut.io.performance.quantizeInput.ready)
        sample(samples, name, cycle, "quantize_output", dut.io.performance.quantizeOutput.valid, dut.io.performance.quantizeOutput.ready)
        sample(samples, name, cycle, "zigzag_input", dut.io.performance.zigZagInput.valid, dut.io.performance.zigZagInput.ready)
        sample(samples, name, cycle, "zigzag_output", dut.io.performance.zigZagOutput.valid, dut.io.performance.zigZagOutput.ready)
        sample(samples, name, cycle, "transform_output", dut.io.performance.transformOutput.valid, dut.io.performance.transformOutput.ready)
        sample(samples, name, cycle, "mcu_output", dut.io.performance.mcuOutput.valid, dut.io.performance.mcuOutput.ready)
        sample(samples, name, cycle, "entropy_block_input", dut.io.performance.entropyBlockInput.valid, dut.io.performance.entropyBlockInput.ready)
        sample(samples, name, cycle, "entropy_run_output", dut.io.performance.entropyRunOutput.valid, dut.io.performance.entropyRunOutput.ready)
        sample(samples, name, cycle, "packer_output", dut.io.performance.packerOutput.valid, dut.io.performance.packerOutput.ready)
        sample(samples, name, cycle, "jpeg_output", dut.io.output.valid, dut.io.output.ready)

        val inputFire = dut.io.input.valid.peek().litToBoolean && dut.io.input.ready.peek().litToBoolean
        if (inputFire) {
          if (firstInputCycle < 0) firstInputCycle = cycle
          nextPixel += 1
        }
        if (dut.io.performance.transformInput.valid.peek().litToBoolean && dut.io.performance.transformInput.ready.peek().litToBoolean) {
          blockCount += 1
        }
        if (dut.io.performance.mcuOutput.valid.peek().litToBoolean && dut.io.performance.mcuOutput.ready.peek().litToBoolean) {
          mcuCount += 1
        }
        if (dut.io.output.valid.peek().litToBoolean && outputReady) {
          bytes += dut.io.output.bits.byte.peek().litValue.toInt
          if (dut.io.output.bits.last.peek().litToBoolean) {
            lastOutputCycle = cycle
            done = true
          }
        }

        dut.clock.step()
        cycle += 1
      }

      dut.io.input.valid.poke(false.B)
      dut.io.output.ready.poke(true.B)
      dut.io.protocolError.expect(false.B)
      nextPixel mustBe pixels

      val image = ImageIO.read(new ByteArrayInputStream(bytes.map(_.toByte).toArray))
      image must not be null
      image.getWidth mustBe Width
      image.getHeight mustBe Height

      result = ScenarioResult(
        name,
        if (subsample) "4:2:0" else "4:4:4",
        if (stalls) "cycle%4!=1 && cycle%9!=5" else "always",
        firstInputCycle,
        lastOutputCycle,
        pixels,
        bytes.toSeq,
        mcuCount,
        blockCount,
        if (retainSamples) samples.toSeq else Seq.empty)
    }

    result
  }

  private def writeCapture(directory: Path, results: Seq[ScenarioResult]): Unit = {
    Files.createDirectories(directory)

    val scenarioWriter = new PrintWriter(Files.newBufferedWriter(directory.resolve("scenarios.csv")))
    try {
      scenarioWriter.println(
        "scenario,width,height,sampling,ready_pattern,clock_hz,first_input_cycle,last_output_cycle,frame_cycles,pixels,bytes,mcus,blocks")
      results.foreach { result =>
        scenarioWriter.println(
          Seq(
            result.name,
            Width,
            Height,
            result.sampling,
            result.readyPattern,
            ClockHz,
            result.firstInputCycle,
            result.lastOutputCycle,
            result.frameCycles,
            result.pixels,
            result.bytes.length,
            result.mcus,
            result.blocks).mkString(","))
      }
    } finally scenarioWriter.close()

    val sampleWriter = new PrintWriter(Files.newBufferedWriter(directory.resolve("samples.csv")))
    try {
      sampleWriter.println("scenario,cycle,boundary,valid,ready")
      results.iterator.flatMap(_.samples).foreach { boundarySample =>
        sampleWriter.println(
          Seq(
            boundarySample.scenario,
            boundarySample.cycle,
            boundarySample.boundary,
            if (boundarySample.valid) 1 else 0,
            if (boundarySample.ready) 1 else 0).mkString(","))
      }
    } finally sampleWriter.close()
  }

  sys.env.get(CaptureDirectoryEnvironment).foreach { captureDirectory =>
    "Hjpeg performance capture should emit valid scenario samples" in {
      val selected = sys.env
        .get(ScenarioEnvironment)
        .map(_.split(",").iterator.map(_.trim).filter(_.nonEmpty).toSeq)
        .getOrElse(SupportedScenarios)
      selected must not be empty
      selected.foreach { scenario =>
        withClue(s"unsupported performance scenario $scenario") {
          SupportedScenarios must contain(scenario)
        }
      }

      val results = selected.distinct.map(runScenario(_))
      if (selected.contains("444-output-stalls")) {
        val baseline = results.find(_.name == "444").getOrElse(runScenario("444", retainSamples = false))
        results.find(_.name == "444-output-stalls").get.bytes mustBe baseline.bytes
      }
      writeCapture(Paths.get(captureDirectory), results)
    }
  }
}
