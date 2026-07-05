# hjpeg KV260 block-design skeleton
#
# Usage from the repository root, after RTL generation and IP packaging:
#
#   vivado -mode batch -source scripts/vivado/create_kv260_block_design.tcl \
#     -tclargs build/vivado/ip_repo build/vivado/hjpeg-kv260-bd
#
# The first argument is the Vivado IP repository containing
# hjpeg_kv260_axi_lite. The second argument is the Vivado project directory to
# create. This script builds a reusable block design with Zynq UltraScale+ PS,
# AXI DMA, and the hjpeg encoder IP. Board constraints, image packaging, and
# on-board validation remain separate platform steps.

set script_dir [file dirname [file normalize [info script]]]
set repo_root [file normalize [file join $script_dir ../..]]

set ip_repo_dir [file normalize [file join $repo_root build/vivado/ip_repo]]
set project_dir [file normalize [file join $repo_root build/vivado/hjpeg-kv260-bd]]

if {$argc >= 1} {
  set ip_repo_dir [file normalize [lindex $argv 0]]
}
if {$argc >= 2} {
  set project_dir [file normalize [lindex $argv 1]]
}

set part_name xck26-sfvc784-2LV-c
set design_name hjpeg_kv260
set ip_dir [file join $ip_repo_dir hjpeg_kv260_axi_lite_1_0]
set component_xml [file join $ip_dir component.xml]

if {![file exists $component_xml]} {
  error "Missing packaged hjpeg IP: $component_xml. Run: vivado -mode batch -source scripts/vivado/package_kv260_axi_lite_ip.tcl"
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
] [get_bd_cells ps]

create_bd_cell -type ip -vlnv xilinx.com:ip:proc_sys_reset:* reset_pl0
set_property -dict [list CONFIG.C_EXT_RESET_HIGH {0}] [get_bd_cells reset_pl0]

create_bd_cell -type ip -vlnv xilinx.com:ip:axi_dma:* axi_dma_0
set_property -dict [list \
  CONFIG.c_include_sg {0} \
  CONFIG.c_include_mm2s {1} \
  CONFIG.c_include_s2mm {1} \
  CONFIG.c_m_axis_mm2s_tdata_width {32} \
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
validate_bd_design
save_bd_design
make_wrapper -files [get_files [file join $project_dir hjpeg_kv260_bd.srcs sources_1 bd $design_name ${design_name}.bd]] -top
add_files -norecurse [file join $project_dir hjpeg_kv260_bd.gen sources_1 bd $design_name hdl ${design_name}_wrapper.v]
set_property top ${design_name}_wrapper [current_fileset]
update_compile_order -fileset sources_1

puts "hjpeg KV260 block design created: $project_dir"
