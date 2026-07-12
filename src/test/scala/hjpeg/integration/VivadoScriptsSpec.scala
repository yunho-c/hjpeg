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
    val bitstream = read("scripts/vivado/build_kv260_bitstream.tcl")
    val floorplan = read("scripts/vivado/write_kv260_floorplan_report.tcl")
    val xsdbDma = read("scripts/host/run_kv260_xsdb_dma.tcl")

    for (script <- Seq(synth, packageIp)) {
      script must include("HjpegKv260AxiLiteTop")
      script must include("generated-kv260-axi-lite-top")
      script must include("filelist.f")
      script must include("xck26-sfvc784-2LV-c")
      script must include("sbt 'runMain hjpeg.ElaborateKv260AxiLiteTop'")
      script must include("No RTL files listed in $filelist")
      script must include("RTL file listed in $filelist does not exist")
    }
    synth must include("Expected at most 2 arguments: rtl_dir project_dir")
    synth must include("create_clock -period 10.000 -name pl_clk [get_ports clock]")
    synth must include("post_synth_utilization.rpt")
    synth must include("post_synth_timing_summary.rpt")
    synth must include("write_checkpoint -force")
    synth must include("post_synth.dcp")

    packageIp must include("Expected at most 2 arguments: rtl_dir ip_repo_dir")
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
    blockDesign must include("Expected at most 2 arguments: ip_repo_dir project_dir")
    blockDesign must include("component.xml")
    blockDesign must include("xilinx.com:ip:zynq_ultra_ps_e")
    blockDesign must include("xilinx.com:ip:axi_dma")
    blockDesign must include("xilinx.com:ip:smartconnect")
    blockDesign must include("xilinx.com:ip:proc_sys_reset")
    blockDesign must include("CONFIG.C_EXT_RESET_HIGH {0}")
    blockDesign must include("xilinx.com:ip:xlconcat")
    blockDesign must include("user.org:user:hjpeg_kv260_axi_lite:1.0")
    blockDesign must include("CONFIG.c_sg_length_width {26}")
    blockDesign must include("CONFIG.c_m_axis_mm2s_tdata_width {32}")
    blockDesign must include("CONFIG.c_s_axis_s2mm_tdata_width {8}")
    blockDesign must include("hjpeg_0/s_axis_rgb")
    blockDesign must include("hjpeg_0/m_axis_jpeg")
    blockDesign must include("hjpeg_0/s_axi_lite")
    blockDesign must include("ps/maxihpm0_fpd_aclk")
    blockDesign must include("ps/maxihpm0_lpd_aclk")
    blockDesign must include("ps/saxihp0_fpd_aclk")
    blockDesign must include("ps/M_AXI_HPM0_FPD")
    blockDesign must include("ps/S_AXI_HP0_FPD")
    blockDesign must include("dma_irq_concat")
    blockDesign must include("assign_bd_address")
    blockDesign must include("write_address_map_report")
    blockDesign must include("get_bd_addr_segs -of_objects $ps_data_space")
    blockDesign must include("hjpeg_kv260_address_map.rpt")

    xsdbDma must include("packed RGB byte length $input_bytes does not match width*height*4")
    xsdbDma must include("packed RGB input exceeds the AXI DMA 26-bit length field")
    xsdbDma must include("mwr [expr {$dma_base + 0x58}] $output_capacity")
    xsdbDma must include("mwr [expr {$dma_base + 0x28}] $input_bytes")
    xsdbDma must include("if {$mm2s_length != $input_bytes}")
    xsdbDma must include("if {$s2mm_length <= 0 || $s2mm_length >= $output_capacity}")
    blockDesign must include("validate_bd_design")
    blockDesign must include("save_bd_design")
    blockDesign must include("make_wrapper")
    blockDesign must include("${design_name}_wrapper.v")
    blockDesign must include("update_compile_order -fileset sources_1")

    bitstream must include("hjpeg_kv260_bd.xpr")
    bitstream must include("open_project")
    bitstream must include("Expected at most 3 arguments: project_dir artifacts_dir jobs")
    bitstream must include("Vivado job count must be a positive integer")
    bitstream must include("proc require_nonempty_file")
    bitstream must include("set_property top hjpeg_kv260_wrapper")
    bitstream must include("launch_runs synth_1")
    bitstream must include("wait_on_run synth_1")
    bitstream must include("launch_runs impl_1 -to_step write_bitstream")
    bitstream must include("wait_on_run impl_1")
    bitstream must include("post_synth_utilization.rpt")
    bitstream must include("post_synth_timing_summary.rpt")
    bitstream must include("post_impl_utilization.rpt")
    bitstream must include("post_impl_timing_summary.rpt")
    bitstream must include("post_impl_drc.rpt")
    bitstream must include("post_impl_route_status.rpt")
    bitstream must include("post_impl_clock_utilization.rpt")
    bitstream must include("implementation completed but multiple bitstreams were found")
    bitstream must include("require_nonempty_file $output_bit")
    bitstream must include("require_nonempty_file $output_xsa")
    bitstream must include("require_nonempty_file $output_dcp")
    bitstream must include("hjpeg_kv260.bit")
    bitstream must include("write_hw_platform -fixed -include_bit")
    bitstream must include("hjpeg_kv260.xsa")
    bitstream must include("write_checkpoint -force")
    bitstream must include("post_impl.dcp")

    floorplan must include("hjpeg_kv260_bd.xpr")
    floorplan must include("open_project")
    floorplan must include("Expected at most 2 arguments: project_dir artifacts_dir")
    floorplan must include("proc require_nonempty_file")
    floorplan must include("proc require_complete_impl_run")
    floorplan must include("Missing implementation run impl_1")
    floorplan must include("impl_1 is not complete")
    floorplan must include("impl_1 has not completed bitstream generation")
    floorplan must include("open_run impl_1")
    floorplan must include("proc write_floorplan_report")
    floorplan must include("get_pblocks -quiet")
    floorplan must include("get_cells -hierarchical -filter {IS_PRIMITIVE && LOC != \"\"} -quiet")
    floorplan must include("Pblock Count")
    floorplan must include("Placed Cell Count")
    floorplan must include("post_impl_floorplan.rpt")
    floorplan must include("require_nonempty_file $floorplan_report")
  }
}
