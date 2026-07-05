# hjpeg KV260 AXI-Lite IP packaging
#
# Usage from the repository root, after RTL generation:
#
#   vivado -mode batch -source scripts/vivado/package_kv260_axi_lite_ip.tcl \
#     -tclargs generated-kv260-axi-lite-top build/vivado/ip_repo
#
# The script packages HjpegKv260AxiLiteTop as reusable RTL IP. It does not create
# a complete KV260 block design or bitstream.

set script_dir [file dirname [file normalize [info script]]]
set repo_root [file normalize [file join $script_dir ../..]]

set rtl_dir [file normalize [file join $repo_root generated-kv260-axi-lite-top]]
set ip_repo_dir [file normalize [file join $repo_root build/vivado/ip_repo]]

if {$argc >= 1} {
  set rtl_dir [file normalize [lindex $argv 0]]
}
if {$argc >= 2} {
  set ip_repo_dir [file normalize [lindex $argv 1]]
}

set top_name HjpegKv260AxiLiteTop
set part_name xck26-sfvc784-2LV-c
set ip_dir [file join $ip_repo_dir hjpeg_kv260_axi_lite_1_0]
set filelist [file join $rtl_dir filelist.f]

if {![file exists $filelist]} {
  error "Missing generated RTL filelist: $filelist. Run: sbt 'runMain hjpeg.ElaborateKv260AxiLiteTop'"
}

file mkdir $ip_repo_dir
create_project hjpeg_ip_packaging [file join $ip_repo_dir .package_project] -part $part_name -force
set_property target_language Verilog [current_project]

set fp [open $filelist r]
set rtl_files {}
while {[gets $fp line] >= 0} {
  set trimmed [string trim $line]
  if {$trimmed eq "" || [string match "#*" $trimmed]} {
    continue
  }
  if {[file pathtype $trimmed] eq "absolute"} {
    lappend rtl_files $trimmed
  } else {
    lappend rtl_files [file normalize [file join $rtl_dir $trimmed]]
  }
}
close $fp

add_files -norecurse -fileset sources_1 $rtl_files
foreach rtl_file $rtl_files {
  if {[string match *.sv $rtl_file]} {
    set_property file_type SystemVerilog [get_files $rtl_file]
  }
}
set_property top $top_name [current_fileset]
update_compile_order -fileset sources_1

ipx::package_project -root_dir $ip_dir -vendor user.org -library user -taxonomy /UserIP -import_files -force
set core [ipx::current_core]
set_property name hjpeg_kv260_axi_lite $core
set_property display_name {hjpeg KV260 AXI-Lite JPEG Encoder} $core
set_property description {Baseline JPEG encoder with AXI-Lite control and AXI-stream RGB/JPEG data ports.} $core
set_property version 1.0 $core
set_property supported_families {zynquplus Production} $core

foreach inferred_bus [ipx::get_bus_interfaces io_sAxiLite -of_objects $core -quiet] {
  ipx::remove_bus_interface $inferred_bus $core
}
foreach inferred_map [ipx::get_memory_maps io_sAxiLite -of_objects $core -quiet] {
  ipx::remove_memory_map $inferred_map $core
}

proc map_bus_port {bus logical physical} {
  ipx::add_port_map $logical $bus
  set_property physical_name $physical [ipx::get_port_maps $logical -of_objects $bus]
}

ipx::add_bus_interface clock $core
set clock_bus [ipx::get_bus_interfaces clock -of_objects $core]
set_property abstraction_type_vlnv xilinx.com:signal:clock_rtl:1.0 $clock_bus
set_property bus_type_vlnv xilinx.com:signal:clock:1.0 $clock_bus
set_property interface_mode slave $clock_bus
map_bus_port $clock_bus CLK clock
ipx::add_bus_parameter ASSOCIATED_BUSIF $clock_bus
set_property value {io_sAxiLite:s_axi_lite:s_axis_rgb:m_axis_jpeg} [ipx::get_bus_parameters ASSOCIATED_BUSIF -of_objects $clock_bus]
ipx::add_bus_parameter ASSOCIATED_RESET $clock_bus
set_property value reset [ipx::get_bus_parameters ASSOCIATED_RESET -of_objects $clock_bus]

ipx::add_bus_interface reset $core
set reset_bus [ipx::get_bus_interfaces reset -of_objects $core]
set_property abstraction_type_vlnv xilinx.com:signal:reset_rtl:1.0 $reset_bus
set_property bus_type_vlnv xilinx.com:signal:reset:1.0 $reset_bus
set_property interface_mode slave $reset_bus
map_bus_port $reset_bus RST reset
ipx::add_bus_parameter POLARITY $reset_bus
set_property value ACTIVE_HIGH [ipx::get_bus_parameters POLARITY -of_objects $reset_bus]

ipx::add_bus_interface s_axi_lite $core
set s_axi_lite_bus [ipx::get_bus_interfaces s_axi_lite -of_objects $core]
set_property abstraction_type_vlnv xilinx.com:interface:aximm_rtl:1.0 $s_axi_lite_bus
set_property bus_type_vlnv xilinx.com:interface:aximm:1.0 $s_axi_lite_bus
set_property interface_mode slave $s_axi_lite_bus
map_bus_port $s_axi_lite_bus AWADDR io_sAxiLite_awaddr
map_bus_port $s_axi_lite_bus AWVALID io_sAxiLite_awvalid
map_bus_port $s_axi_lite_bus AWREADY io_sAxiLite_awready
map_bus_port $s_axi_lite_bus WDATA io_sAxiLite_wdata
map_bus_port $s_axi_lite_bus WSTRB io_sAxiLite_wstrb
map_bus_port $s_axi_lite_bus WVALID io_sAxiLite_wvalid
map_bus_port $s_axi_lite_bus WREADY io_sAxiLite_wready
map_bus_port $s_axi_lite_bus BRESP io_sAxiLite_bresp
map_bus_port $s_axi_lite_bus BVALID io_sAxiLite_bvalid
map_bus_port $s_axi_lite_bus BREADY io_sAxiLite_bready
map_bus_port $s_axi_lite_bus ARADDR io_sAxiLite_araddr
map_bus_port $s_axi_lite_bus ARVALID io_sAxiLite_arvalid
map_bus_port $s_axi_lite_bus ARREADY io_sAxiLite_arready
map_bus_port $s_axi_lite_bus RDATA io_sAxiLite_rdata
map_bus_port $s_axi_lite_bus RRESP io_sAxiLite_rresp
map_bus_port $s_axi_lite_bus RVALID io_sAxiLite_rvalid
map_bus_port $s_axi_lite_bus RREADY io_sAxiLite_rready

ipx::add_memory_map s_axi_lite $core
set memory_map [ipx::get_memory_maps s_axi_lite -of_objects $core]
set_property slave_memory_map_ref s_axi_lite $s_axi_lite_bus
ipx::add_address_block control $memory_map
set address_block [ipx::get_address_blocks control -of_objects $memory_map]
set_property base_address 0x00000000 $address_block
set_property range 0x00001000 $address_block
set_property width 32 $address_block

ipx::add_bus_interface s_axis_rgb $core
set s_axis_rgb_bus [ipx::get_bus_interfaces s_axis_rgb -of_objects $core]
set_property abstraction_type_vlnv xilinx.com:interface:axis_rtl:1.0 $s_axis_rgb_bus
set_property bus_type_vlnv xilinx.com:interface:axis:1.0 $s_axis_rgb_bus
set_property interface_mode slave $s_axis_rgb_bus
map_bus_port $s_axis_rgb_bus TREADY io_sAxisRgb_ready
map_bus_port $s_axis_rgb_bus TVALID io_sAxisRgb_valid
map_bus_port $s_axis_rgb_bus TDATA io_sAxisRgb_bits_data
map_bus_port $s_axis_rgb_bus TKEEP io_sAxisRgb_bits_keep
map_bus_port $s_axis_rgb_bus TLAST io_sAxisRgb_bits_last

ipx::add_bus_interface m_axis_jpeg $core
set m_axis_jpeg_bus [ipx::get_bus_interfaces m_axis_jpeg -of_objects $core]
set_property abstraction_type_vlnv xilinx.com:interface:axis_rtl:1.0 $m_axis_jpeg_bus
set_property bus_type_vlnv xilinx.com:interface:axis:1.0 $m_axis_jpeg_bus
set_property interface_mode master $m_axis_jpeg_bus
map_bus_port $m_axis_jpeg_bus TREADY io_mAxisJpeg_ready
map_bus_port $m_axis_jpeg_bus TVALID io_mAxisJpeg_valid
map_bus_port $m_axis_jpeg_bus TDATA io_mAxisJpeg_bits_data
map_bus_port $m_axis_jpeg_bus TKEEP io_mAxisJpeg_bits_keep
map_bus_port $m_axis_jpeg_bus TLAST io_mAxisJpeg_bits_last

ipx::update_checksums $core
ipx::save_core $core

puts "hjpeg KV260 AXI-Lite IP packaged at: $ip_dir"
