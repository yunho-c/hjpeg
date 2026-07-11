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
  val rasterPhase = UInt(2.W)
  val encoderPhase = UInt(4.W)
  val snapshot = UInt(34.W)
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

  io.performance.rasterPhase := Mux(
    io.config.enableChromaSubsample,
    BoringUtils.bore(core.rasterToSubsampledMcu.state),
    BoringUtils.bore(core.rasterToMcu.state))
  io.performance.encoderPhase := BoringUtils.bore(core.encoder.state)

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

  // A single packed peek keeps large matrix captures practical on simulators
  // where each individual signal peek crosses a process/API boundary.
  io.performance.snapshot := Cat(
    Seq(
      Cat(io.input.valid, io.input.ready),
      Cat(io.performance.transformInput.valid, io.performance.transformInput.ready),
      Cat(io.performance.dctInput.valid, io.performance.dctInput.ready),
      Cat(io.performance.dctOutput.valid, io.performance.dctOutput.ready),
      Cat(io.performance.quantizeInput.valid, io.performance.quantizeInput.ready),
      Cat(io.performance.quantizeOutput.valid, io.performance.quantizeOutput.ready),
      Cat(io.performance.zigZagInput.valid, io.performance.zigZagInput.ready),
      Cat(io.performance.zigZagOutput.valid, io.performance.zigZagOutput.ready),
      Cat(io.performance.transformOutput.valid, io.performance.transformOutput.ready),
      Cat(io.performance.mcuOutput.valid, io.performance.mcuOutput.ready),
      Cat(io.performance.entropyBlockInput.valid, io.performance.entropyBlockInput.ready),
      Cat(io.performance.entropyRunOutput.valid, io.performance.entropyRunOutput.ready),
      Cat(io.performance.packerOutput.valid, io.performance.packerOutput.ready),
      Cat(io.output.valid, io.output.ready),
      io.performance.rasterPhase,
      io.performance.encoderPhase))
}

class HjpegPerformanceTraceSpec extends AnyFreeSpec with Matchers with ChiselSim {
  private val ClockHz = 100_000_000L
  private val CaptureDirectoryEnvironment = "HJPEG_PERFORMANCE_CAPTURE_DIR"
  private val ScenarioEnvironment = "HJPEG_PERFORMANCE_SCENARIOS"

  private case class ScenarioDefinition(
      name: String,
      profile: String,
      width: Int,
      height: Int,
      sampling: String,
      content: String,
      quality: Int,
      stalls: Boolean = false) {
    val subsample: Boolean = sampling == "4:2:0"
  }

  private val QuickScenarios = Seq(
    ScenarioDefinition("444", "quick", 32, 16, "4:4:4", "deterministic-gradient", 50),
    ScenarioDefinition("420", "quick", 32, 16, "4:2:0", "deterministic-gradient", 50),
    ScenarioDefinition("444-output-stalls", "quick", 32, 16, "4:4:4", "deterministic-gradient", 50, stalls = true))
  private val SteadyStateScenarios = for {
    sampling <- Seq("4:4:4", "4:2:0")
    content <- Seq("flat", "smooth-gradient", "checkerboard", "seeded-random")
    quality <- Seq(10, 50, 90)
  } yield {
    val samplingName = if (sampling == "4:4:4") "444" else "420"
    ScenarioDefinition(s"steady-$samplingName-$content-q$quality", "steady-state", 64, 64, sampling, content, quality)
  }
  private val SupportedScenarios = (QuickScenarios ++ SteadyStateScenarios).map(item => item.name -> item).toMap
  private val BoundaryNames = Seq(
    "rgb_input",
    "transform_input",
    "dct_input",
    "dct_output",
    "quantize_input",
    "quantize_output",
    "zigzag_input",
    "zigzag_output",
    "transform_output",
    "mcu_output",
    "entropy_block_input",
    "entropy_run_output",
    "packer_output",
    "jpeg_output")

  private case class BoundarySample(
      scenario: String,
      cycle: Int,
      boundary: String,
      valid: Boolean,
      ready: Boolean)

  private case class PhaseSample(
      scenario: String,
      cycle: Int,
      rasterPhase: Int,
      encoderPhase: Int)

  private case class ScenarioResult(
      definition: ScenarioDefinition,
      readyPattern: String,
      firstInputCycle: Int,
      lastOutputCycle: Int,
      pixels: Int,
      bytes: Seq[Int],
      mcus: Int,
      blocks: Int,
      samples: Seq[BoundarySample],
      phases: Seq[PhaseSample]) {
    val name: String = definition.name
    val frameCycles: Int = lastOutputCycle - firstInputCycle + 1
  }

  private def pixelAt(definition: ScenarioDefinition, index: Int): (Int, Int, Int) = {
    val x = index % definition.width
    val y = index / definition.width
    definition.content match {
      case "flat" => (96, 144, 192)
      case "smooth-gradient" =>
        val r = x * 255 / (definition.width - 1)
        val g = y * 255 / (definition.height - 1)
        val b = (x + y) * 255 / (definition.width + definition.height - 2)
        (r, g, b)
      case "checkerboard" =>
        if (((x / 4) + (y / 4)) % 2 == 0) (240, 32, 208) else (16, 224, 48)
      case "seeded-random" =>
        var value = index ^ 0x5eed1234
        value ^= value << 13
        value ^= value >>> 17
        value ^= value << 5
        (value & 0xff, (value >>> 8) & 0xff, (value >>> 16) & 0xff)
      case "deterministic-gradient" =>
        val value = (x * 13 + y * 17) & 0xff
        (value, 255 - value, (value / 2 + 64) & 0xff)
    }
  }

  private def pokeConfig(dut: HjpegPerformanceHarness, definition: ScenarioDefinition): Unit = {
    dut.io.config.xsize.poke(definition.width.U)
    dut.io.config.ysize.poke(definition.height.U)
    dut.io.config.quality.poke(definition.quality.U)
    dut.io.config.restartInterval.poke(0.U)
    dut.io.config.enableChromaSubsample.poke(definition.subsample.B)
    dut.io.config.emitJfif.poke(true.B)
  }

  private def sample(
      samples: ArrayBuffer[BoundarySample],
      scenario: String,
      cycle: Int,
      boundary: String,
      state: Int): Unit = {
    samples += BoundarySample(scenario, cycle, boundary, (state & 2) != 0, (state & 1) != 0)
  }

  private def runScenario(
      dut: HjpegPerformanceHarness,
      definition: ScenarioDefinition,
      retainSamples: Boolean = true): ScenarioResult = {
      val name = definition.name
      dut.reset.poke(true.B)
      dut.clock.step()
      dut.reset.poke(false.B)
      pokeConfig(dut, definition)
      dut.io.clearProtocolError.poke(false.B)

      val samples = ArrayBuffer.empty[BoundarySample]
      val phases = ArrayBuffer.empty[PhaseSample]
      val bytes = ArrayBuffer.empty[Int]
      val pixels = definition.width * definition.height
      var nextPixel = 0
      var firstInputCycle = -1
      var lastOutputCycle = -1
      var mcuCount = 0
      var blockCount = 0
      var cycle = 0
      var done = false

      while (!done) {
        assert(cycle < pixels * 4096 + JpegHeaderBytes.MaxHeaderLength + 4096, s"timeout tracing $name")

        val outputReady = !definition.stalls || ((cycle % 4) != 1 && (cycle % 9) != 5)
        dut.io.output.ready.poke(outputReady.B)

        if (nextPixel < pixels) {
          val (r, g, b) = pixelAt(definition, nextPixel)
          dut.io.input.valid.poke(true.B)
          dut.io.input.bits.x.poke((nextPixel % definition.width).U)
          dut.io.input.bits.y.poke((nextPixel / definition.width).U)
          dut.io.input.bits.r.poke(r.U)
          dut.io.input.bits.g.poke(g.U)
          dut.io.input.bits.b.poke(b.U)
        } else {
          dut.io.input.valid.poke(false.B)
        }

        val snapshot = dut.io.performance.snapshot.peek().litValue
        val boundaryStates = BoundaryNames.indices.map { index =>
          ((snapshot >> (6 + (BoundaryNames.length - index - 1) * 2)) & 3).toInt
        }
        BoundaryNames.zip(boundaryStates).foreach { case (boundary, state) =>
          sample(samples, name, cycle, boundary, state)
        }
        phases += PhaseSample(
          name,
          cycle,
          ((snapshot >> 4) & 3).toInt,
          (snapshot & 15).toInt)

        val inputFire = boundaryStates.head == 3
        if (inputFire) {
          if (firstInputCycle < 0) firstInputCycle = cycle
          nextPixel += 1
        }
        if (boundaryStates(1) == 3) {
          blockCount += 1
        }
        if (boundaryStates(9) == 3) {
          mcuCount += 1
        }
        if (boundaryStates(13) == 3) {
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
      image.getWidth mustBe definition.width
      image.getHeight mustBe definition.height

      ScenarioResult(
        definition,
        if (definition.stalls) "cycle%4!=1 && cycle%9!=5" else "always",
        firstInputCycle,
        lastOutputCycle,
        pixels,
        bytes.toSeq,
        mcuCount,
        blockCount,
        if (retainSamples) samples.toSeq else Seq.empty,
        if (retainSamples) phases.toSeq else Seq.empty)
  }

  private def runScenarios(definitions: Seq[ScenarioDefinition]): Seq[ScenarioResult] = {
    var results = Seq.empty[ScenarioResult]
    simulate(new HjpegPerformanceHarness()) { dut =>
      val needsBaseline = definitions.exists(_.name == "444-output-stalls") && !definitions.exists(_.name == "444")
      val simulationDefinitions = if (needsBaseline) definitions :+ SupportedScenarios("444") else definitions
      val allResults = simulationDefinitions.map(definition =>
        runScenario(dut, definition, retainSamples = definitions.contains(definition)))
      if (definitions.exists(_.name == "444-output-stalls")) {
        allResults.find(_.name == "444-output-stalls").get.bytes mustBe allResults.find(_.name == "444").get.bytes
      }
      results = allResults.filter(result => definitions.contains(result.definition))
    }
    results
  }

  private def writeCapture(directory: Path, results: Seq[ScenarioResult]): Unit = {
    Files.createDirectories(directory)

    val scenarioWriter = new PrintWriter(Files.newBufferedWriter(directory.resolve("scenarios.csv")))
    try {
      scenarioWriter.println(
        "scenario,profile,width,height,sampling,content,quality,ready_pattern,clock_hz,first_input_cycle,last_output_cycle,frame_cycles,pixels,bytes,mcus,blocks")
      results.foreach { result =>
        scenarioWriter.println(
          Seq(
            result.name,
            result.definition.profile,
            result.definition.width,
            result.definition.height,
            result.definition.sampling,
            result.definition.content,
            result.definition.quality,
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

    val phaseWriter = new PrintWriter(Files.newBufferedWriter(directory.resolve("phases.csv")))
    try {
      phaseWriter.println("scenario,cycle,raster_phase,encoder_phase")
      results.iterator.flatMap(_.phases).foreach { phaseSample =>
        phaseWriter.println(
          Seq(phaseSample.scenario, phaseSample.cycle, phaseSample.rasterPhase, phaseSample.encoderPhase).mkString(","))
      }
    } finally phaseWriter.close()
  }

  sys.env.get(CaptureDirectoryEnvironment).foreach { captureDirectory =>
    "Hjpeg performance capture should emit valid scenario samples" in {
      val selected = sys.env
        .get(ScenarioEnvironment)
        .map(_.split(",").iterator.map(_.trim).filter(_.nonEmpty).toSeq)
        .getOrElse(QuickScenarios.map(_.name))
      selected must not be empty
      selected.foreach { scenario =>
        withClue(s"unsupported performance scenario $scenario") {
          SupportedScenarios.keySet must contain(scenario)
        }
      }

      val results = runScenarios(selected.distinct.map(SupportedScenarios))
      writeCapture(Paths.get(captureDirectory), results)
    }
  }
}
