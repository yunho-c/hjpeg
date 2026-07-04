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
    val blockDesign = read("scripts/vivado/create_kv260_block_design.tcl")

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
    packageIp must include("xilinx.com:signal:clock")
    packageIp must include("xilinx.com:signal:reset")
    packageIp must include("ASSOCIATED_BUSIF")
    packageIp must include("ASSOCIATED_RESET")
    packageIp must include("ipx::add_memory_map s_axi_lite")
    packageIp must include("set_property slave_memory_map_ref s_axi_lite")
    packageIp must include("set_property base_address 0x00000000")
    packageIp must include("set_property range 0x00001000")
    packageIp must include("set_property width 32")

    val requiredPortMaps = Seq(
      ("clock_bus", "CLK", "clock"),
      ("reset_bus", "RST", "reset"),
      ("s_axi_lite_bus", "AWADDR", "io_sAxiLite_awaddr"),
      ("s_axi_lite_bus", "AWVALID", "io_sAxiLite_awvalid"),
      ("s_axi_lite_bus", "AWREADY", "io_sAxiLite_awready"),
      ("s_axi_lite_bus", "WDATA", "io_sAxiLite_wdata"),
      ("s_axi_lite_bus", "WSTRB", "io_sAxiLite_wstrb"),
      ("s_axi_lite_bus", "WVALID", "io_sAxiLite_wvalid"),
      ("s_axi_lite_bus", "WREADY", "io_sAxiLite_wready"),
      ("s_axi_lite_bus", "BRESP", "io_sAxiLite_bresp"),
      ("s_axi_lite_bus", "BVALID", "io_sAxiLite_bvalid"),
      ("s_axi_lite_bus", "BREADY", "io_sAxiLite_bready"),
      ("s_axi_lite_bus", "ARADDR", "io_sAxiLite_araddr"),
      ("s_axi_lite_bus", "ARVALID", "io_sAxiLite_arvalid"),
      ("s_axi_lite_bus", "ARREADY", "io_sAxiLite_arready"),
      ("s_axi_lite_bus", "RDATA", "io_sAxiLite_rdata"),
      ("s_axi_lite_bus", "RRESP", "io_sAxiLite_rresp"),
      ("s_axi_lite_bus", "RVALID", "io_sAxiLite_rvalid"),
      ("s_axi_lite_bus", "RREADY", "io_sAxiLite_rready"),
      ("s_axis_rgb_bus", "TREADY", "io_sAxisRgb_ready"),
      ("s_axis_rgb_bus", "TVALID", "io_sAxisRgb_valid"),
      ("s_axis_rgb_bus", "TDATA", "io_sAxisRgb_bits_data"),
      ("s_axis_rgb_bus", "TKEEP", "io_sAxisRgb_bits_keep"),
      ("s_axis_rgb_bus", "TLAST", "io_sAxisRgb_bits_last"),
      ("m_axis_jpeg_bus", "TREADY", "io_mAxisJpeg_ready"),
      ("m_axis_jpeg_bus", "TVALID", "io_mAxisJpeg_valid"),
      ("m_axis_jpeg_bus", "TDATA", "io_mAxisJpeg_bits_data"),
      ("m_axis_jpeg_bus", "TKEEP", "io_mAxisJpeg_bits_keep"),
      ("m_axis_jpeg_bus", "TLAST", "io_mAxisJpeg_bits_last")
    )
    for ((bus, logical, physical) <- requiredPortMaps) {
      packageIp must include(s"map_bus_port $$$bus $logical $physical")
    }

    blockDesign must include("hjpeg_kv260_axi_lite_1_0")
    blockDesign must include("component.xml")
    blockDesign must include("xilinx.com:ip:zynq_ultra_ps_e")
    blockDesign must include("xilinx.com:ip:axi_dma")
    blockDesign must include("xilinx.com:ip:smartconnect")
    blockDesign must include("xilinx.com:ip:proc_sys_reset")
    blockDesign must include("CONFIG.C_EXT_RESET_HIGH {0}")
    blockDesign must include("xilinx.com:ip:xlconcat")
    blockDesign must include("user.org:user:hjpeg_kv260_axi_lite:1.0")
    blockDesign must include("CONFIG.c_m_axis_mm2s_tdata_width {24}")
    blockDesign must include("CONFIG.c_s_axis_s2mm_tdata_width {8}")
    blockDesign must include("hjpeg_0/s_axis_rgb")
    blockDesign must include("hjpeg_0/m_axis_jpeg")
    blockDesign must include("hjpeg_0/s_axi_lite")
    blockDesign must include("ps/M_AXI_HPM0_FPD")
    blockDesign must include("ps/S_AXI_HP0_FPD")
    blockDesign must include("dma_irq_concat")
    blockDesign must include("validate_bd_design")
  }
}
