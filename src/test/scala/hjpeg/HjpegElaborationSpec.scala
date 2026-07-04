// See README.md for license details.

package hjpeg

import _root_.circt.stage.ChiselStage
import org.scalatest.freespec.AnyFreeSpec
import org.scalatest.matchers.must.Matchers

class HjpegElaborationSpec extends AnyFreeSpec with Matchers {
  "Hjpeg top levels should elaborate" in {
    ChiselStage.emitSystemVerilog(new HjpegCore()) must include("module HjpegCore")
    ChiselStage.emitSystemVerilog(new HjpegAxiStreamCore()) must include("module HjpegAxiStreamCore")
    ChiselStage.emitSystemVerilog(new HjpegKv260Top()) must include("module HjpegKv260Top")
  }
}
