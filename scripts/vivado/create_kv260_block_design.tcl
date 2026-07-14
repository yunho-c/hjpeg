# hjpeg KV260 block-design skeleton
#
# Usage from the repository root, after RTL generation and IP packaging:
#
#   vivado -mode batch -source scripts/vivado/create_kv260_block_design.tcl \
#     -tclargs build/vivado/ip_repo build/vivado/hjpeg-kv260-bd 32 100
#
# The first argument is the Vivado IP repository containing
# hjpeg_kv260_axi_lite. The second argument is the Vivado project directory to
# create. The optional third argument selects the AXI DMA MM2S stream width in
# bits; use 32 for the scalar top and 128 for the four-pixel 4K60 top. The
# optional fourth argument selects the requested PS pl_clk0 frequency in MHz;
# use 150 for the 4K60 implementation target. This script builds a reusable
# block design with Zynq UltraScale+ PS, AXI DMA, and the hjpeg encoder IP.
# Board constraints, image packaging, and on-board validation remain separate
# platform steps.

set script_dir [file dirname [file normalize [info script]]]
set repo_root [file normalize [file join $script_dir ../..]]

set ip_repo_dir [file normalize [file join $repo_root build/vivado/ip_repo]]
set project_dir [file normalize [file join $repo_root build/vivado/hjpeg-kv260-bd]]
set mm2s_data_width 32
set pl_clock_mhz 100

if {$argc > 4} {
  error "Expected at most 4 arguments: ip_repo_dir project_dir mm2s_data_width pl_clock_mhz"
}
if {$argc >= 1} {
  set ip_repo_dir [file normalize [lindex $argv 0]]
}
if {$argc >= 2} {
  set project_dir [file normalize [lindex $argv 1]]
}
if {$argc >= 3} {
  set mm2s_data_width [lindex $argv 2]
}
if {$argc >= 4} {
  set pl_clock_mhz [lindex $argv 3]
}
if {$mm2s_data_width ni {32 128}} {
  error "AXI DMA MM2S stream width must be 32 or 128 bits, got: $mm2s_data_width"
}
if {![regexp {^[1-9][0-9]*$} $pl_clock_mhz]} {
  error "PS pl_clk0 frequency must be a positive integer in MHz, got: $pl_clock_mhz"
}

set part_name xck26-sfvc784-2LV-c
set design_name hjpeg_kv260
set ip_dir [file join $ip_repo_dir hjpeg_kv260_axi_lite_1_0]
set component_xml [file join $ip_dir component.xml]

if {![file exists $component_xml]} {
  error "Missing packaged hjpeg IP: $component_xml. Run: vivado -mode batch -source scripts/vivado/package_kv260_axi_lite_ip.tcl"
}

proc write_address_map_report {path} {
  set ps_data_space [get_bd_addr_spaces /ps/Data -quiet]
  if {[llength $ps_data_space] == 0} {
    error "Could not find /ps/Data address space"
  }

  set required_segments {
    {hjpeg_0/s_axi_lite hjpeg_0 control}
    {axi_dma_0/S_AXI_LITE axi_dma_0 Reg}
  }
  set rows {}
  foreach required_segment $required_segments {
    lassign $required_segment interface_name segment_cell segment_leaf
    set matched_segment ""
    foreach segment [get_bd_addr_segs -of_objects $ps_data_space] {
      set segment_name [get_property NAME $segment]
      if {[string first $segment_cell $segment_name] >= 0 && [string first $segment_leaf $segment_name] >= 0} {
        set matched_segment $segment
        break
      }
    }
    if {$matched_segment eq ""} {
      error "Could not find assigned address segment for $interface_name"
    }

    set base [expr {[get_property OFFSET $matched_segment]}]
    set range [expr {[get_property RANGE $matched_segment]}]
    set high [expr {$base + $range - 1}]
    lappend rows [list $interface_name $base $high $range]
  }

  set fp [open $path w]
  puts $fp "Address Map"
  puts $fp "| Interface | Base Address | High Address | Range |"
  foreach row $rows {
    lassign $row interface_name base high range
    puts $fp [format "| %s | 0x%08X | 0x%08X | 0x%X |" $interface_name $base $high $range]
  }
  close $fp
}

file mkdir $project_dir
create_project hjpeg_kv260_bd $project_dir -part $part_name -force
set_property target_language Verilog [current_project]
set_property ip_repo_paths $ip_repo_dir [current_project]
update_ip_catalog

create_bd_design $design_name

create_bd_cell -type ip -vlnv xilinx.com:ip:zynq_ultra_ps_e:* ps
set_property -dict [list \
  CONFIG.PSU__USE__M_AXI_GP0 {1} \
  CONFIG.PSU__USE__S_AXI_GP2 {1} \
  CONFIG.PSU__USE__IRQ0 {1} \
  CONFIG.PSU__CRL_APB__PL0_REF_CTRL__FREQMHZ $pl_clock_mhz \
] [get_bd_cells ps]

create_bd_cell -type ip -vlnv xilinx.com:ip:proc_sys_reset:* reset_pl0
set_property -dict [list CONFIG.C_EXT_RESET_HIGH {0}] [get_bd_cells reset_pl0]

create_bd_cell -type ip -vlnv xilinx.com:ip:axi_dma:* axi_dma_0
# 26 bits covers one packed 3840x2160 RGB frame (33,177,600 bytes) in a single
# MM2S transaction, so DMA emits TLAST only at the frame boundary. The packed
# byte layout remains four bytes per pixel at either stream width.
# Long DMA-side bursts sustain UHD ingress; MM2S store-and-forward exceeds the
# project's BRAM ceiling and is unnecessary with SmartConnect buffering.
set_property -dict [list \
  CONFIG.c_include_sg {0} \
  CONFIG.c_include_mm2s {1} \
  CONFIG.c_include_s2mm {1} \
  CONFIG.c_sg_length_width {26} \
  CONFIG.c_include_mm2s_sf {0} \
  CONFIG.c_mm2s_burst_size {256} \
  CONFIG.c_m_axis_mm2s_tdata_width $mm2s_data_width \
  CONFIG.c_s_axis_s2mm_tdata_width {8} \
] [get_bd_cells axi_dma_0]

create_bd_cell -type ip -vlnv xilinx.com:ip:smartconnect:* axi_lite_xbar
set_property -dict [list CONFIG.NUM_SI {1} CONFIG.NUM_MI {2}] [get_bd_cells axi_lite_xbar]

create_bd_cell -type ip -vlnv xilinx.com:ip:smartconnect:* dma_mem_xbar
set_property -dict [list CONFIG.NUM_SI {2} CONFIG.NUM_MI {1}] [get_bd_cells dma_mem_xbar]

create_bd_cell -type ip -vlnv xilinx.com:ip:xlconcat:* dma_irq_concat
set_property -dict [list CONFIG.NUM_PORTS {2}] [get_bd_cells dma_irq_concat]

create_bd_cell -type ip -vlnv user.org:user:hjpeg_kv260_axi_lite:1.0 hjpeg_0

connect_bd_net [get_bd_pins ps/pl_clk0] \
  [get_bd_pins ps/maxihpm0_fpd_aclk] \
  [get_bd_pins ps/maxihpm0_lpd_aclk] \
  [get_bd_pins ps/saxihp0_fpd_aclk] \
  [get_bd_pins reset_pl0/slowest_sync_clk] \
  [get_bd_pins axi_dma_0/s_axi_lite_aclk] \
  [get_bd_pins axi_dma_0/m_axi_mm2s_aclk] \
  [get_bd_pins axi_dma_0/m_axi_s2mm_aclk] \
  [get_bd_pins axi_lite_xbar/aclk] \
  [get_bd_pins dma_mem_xbar/aclk] \
  [get_bd_pins hjpeg_0/clock]

connect_bd_net [get_bd_pins ps/pl_resetn0] [get_bd_pins reset_pl0/ext_reset_in]
connect_bd_net [get_bd_pins reset_pl0/peripheral_reset] [get_bd_pins hjpeg_0/reset]
connect_bd_net [get_bd_pins reset_pl0/peripheral_aresetn] \
  [get_bd_pins axi_dma_0/axi_resetn] \
  [get_bd_pins axi_lite_xbar/aresetn] \
  [get_bd_pins dma_mem_xbar/aresetn]

connect_bd_intf_net [get_bd_intf_pins ps/M_AXI_HPM0_FPD] [get_bd_intf_pins axi_lite_xbar/S00_AXI]
connect_bd_intf_net [get_bd_intf_pins axi_lite_xbar/M00_AXI] [get_bd_intf_pins hjpeg_0/s_axi_lite]
connect_bd_intf_net [get_bd_intf_pins axi_lite_xbar/M01_AXI] [get_bd_intf_pins axi_dma_0/S_AXI_LITE]

connect_bd_intf_net [get_bd_intf_pins axi_dma_0/M_AXIS_MM2S] [get_bd_intf_pins hjpeg_0/s_axis_rgb]
connect_bd_intf_net [get_bd_intf_pins hjpeg_0/m_axis_jpeg] [get_bd_intf_pins axi_dma_0/S_AXIS_S2MM]

connect_bd_intf_net [get_bd_intf_pins axi_dma_0/M_AXI_MM2S] [get_bd_intf_pins dma_mem_xbar/S00_AXI]
connect_bd_intf_net [get_bd_intf_pins axi_dma_0/M_AXI_S2MM] [get_bd_intf_pins dma_mem_xbar/S01_AXI]
connect_bd_intf_net [get_bd_intf_pins dma_mem_xbar/M00_AXI] [get_bd_intf_pins ps/S_AXI_HP0_FPD]

connect_bd_net [get_bd_pins axi_dma_0/mm2s_introut] [get_bd_pins dma_irq_concat/In0]
connect_bd_net [get_bd_pins axi_dma_0/s2mm_introut] [get_bd_pins dma_irq_concat/In1]
connect_bd_net [get_bd_pins dma_irq_concat/dout] [get_bd_pins ps/pl_ps_irq0]

assign_bd_address
write_address_map_report [file join $project_dir hjpeg_kv260_address_map.rpt]
validate_bd_design
save_bd_design
make_wrapper -files [get_files [file join $project_dir hjpeg_kv260_bd.srcs sources_1 bd $design_name ${design_name}.bd]] -top
add_files -norecurse [file join $project_dir hjpeg_kv260_bd.gen sources_1 bd $design_name hdl ${design_name}_wrapper.v]
set_property top ${design_name}_wrapper [current_fileset]
update_compile_order -fileset sources_1

puts "hjpeg KV260 block design created: $project_dir"
