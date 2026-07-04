// See README.md for license details.

package hjpeg

import _root_.circt.stage.ChiselStage

object Elaborate extends App {
  ChiselStage.emitSystemVerilogFile(
    new HjpegCore(),
    args = Array("--target-dir", "generated"),
    firtoolOpts = Array(
      "-disable-all-randomization",
      "-strip-debug-info",
      "-default-layer-specialization=enable"
    )
  )
}

object ElaborateAxiStream extends App {
  ChiselStage.emitSystemVerilogFile(
    new HjpegAxiStreamCore(),
    args = Array("--target-dir", "generated-axi-stream"),
    firtoolOpts = Array(
      "-disable-all-randomization",
      "-strip-debug-info",
      "-default-layer-specialization=enable"
    )
  )
}

object ElaborateKv260Top extends App {
  ChiselStage.emitSystemVerilogFile(
    new HjpegKv260Top(),
    args = Array("--target-dir", "generated-kv260-top"),
    firtoolOpts = Array(
      "-disable-all-randomization",
      "-strip-debug-info",
      "-default-layer-specialization=enable"
    )
  )
}

object ElaborateKv260AxiLiteTop extends App {
  ChiselStage.emitSystemVerilogFile(
    new HjpegKv260AxiLiteTop(),
    args = Array("--target-dir", "generated-kv260-axi-lite-top"),
    firtoolOpts = Array(
      "-disable-all-randomization",
      "-strip-debug-info",
      "-default-layer-specialization=enable"
    )
  )
}
