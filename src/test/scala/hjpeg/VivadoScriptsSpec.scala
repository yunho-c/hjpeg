// See README.md for license details.

package hjpeg

import org.scalatest.freespec.AnyFreeSpec
import org.scalatest.matchers.must.Matchers

import java.nio.file.{Files, Path}

class VivadoScriptsSpec extends AnyFreeSpec with Matchers {
  private val repoRoot = {
    val start = Path.of(System.getProperty("user.dir")).toAbsolutePath

    Iterator
      .iterate(start)(_.getParent)
      .takeWhile(_ != null)
      .find(path => Files.exists(path.resolve("scripts/vivado/synth_kv260_axi_lite.tcl")))
      .getOrElse(start)
  }

  private def read(relative: String): String =
    Files.readString(repoRoot.resolve(relative))

  "Vivado scripts should target the generated KV260 AXI-Lite top" in {
    val synth = read("scripts/vivado/synth_kv260_axi_lite.tcl")
    val packageIp = read("scripts/vivado/package_kv260_axi_lite_ip.tcl")

    for (script <- Seq(synth, packageIp)) {
      script must include("HjpegKv260AxiLiteTop")
      script must include("generated-kv260-axi-lite-top")
      script must include("filelist.f")
      script must include("xck26-sfvc784-2LV-c")
      script must include("sbt 'runMain hjpeg.ElaborateKv260AxiLiteTop'")
    }

    packageIp must include("hjpeg_kv260_axi_lite")
    packageIp must include("xilinx.com:interface:aximm")
    packageIp must include("xilinx.com:interface:axis")
  }
}
