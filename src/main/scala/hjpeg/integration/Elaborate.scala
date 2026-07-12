// See README.md for license details.

package hjpeg

import chisel3.RawModule
import _root_.circt.stage.ChiselStage

private[hjpeg] object HjpegElaboration {
  val FirtoolOptions: Array[String] = Array(
    "-disable-all-randomization",
    "-strip-debug-info",
    "-default-layer-specialization=enable"
  )

  def emitSystemVerilogFile(gen: => RawModule, targetDir: String): Unit = {
    ChiselStage.emitSystemVerilogFile(
      gen,
      args = Array("--target-dir", targetDir),
      firtoolOpts = FirtoolOptions
    )
  }
}

object Elaborate extends App {
  HjpegElaboration.emitSystemVerilogFile(new HjpegCore(), "generated")
}

object ElaborateAxiStream extends App {
  HjpegElaboration.emitSystemVerilogFile(new HjpegAxiStreamCore(), "generated-axi-stream")
}

object ElaborateKv260Top extends App {
  HjpegElaboration.emitSystemVerilogFile(new HjpegKv260Top(), "generated-kv260-top")
}

object ElaborateKv260AxiLiteTop extends App {
  HjpegElaboration.emitSystemVerilogFile(
    new HjpegKv260AxiLiteTop(HjpegTargetConfigs.Kv260FullHd),
    "generated-kv260-axi-lite-top")
}

object ElaborateKv2604k60AxiLiteTop extends App {
  HjpegElaboration.emitSystemVerilogFile(
    new HjpegKv260AxiLiteTop(HjpegTargetConfigs.Kv260Uhd4k, inputPixelsPerBeat = 4),
    "generated-kv260-4k60-axi-lite-top")
}
