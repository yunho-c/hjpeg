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
set_property target_language SystemVerilog [current_project]

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
set_property top $top_name [current_fileset]
update_compile_order -fileset sources_1

ipx::package_project -root_dir $ip_dir -vendor user.org -library user -taxonomy /UserIP -force
set core [ipx::current_core]
set_property name hjpeg_kv260_axi_lite $core
set_property display_name {hjpeg KV260 AXI-Lite JPEG Encoder} $core
set_property description {Baseline JPEG encoder with AXI-Lite control and AXI-stream RGB/JPEG data ports.} $core
set_property version 1.0 $core
set_property supported_families {zynquplus Production} $core

ipx::add_bus_interface s_axi_lite $core
set_property abstraction_type_vlnv xilinx.com:interface:aximm_rtl:1.0 [ipx::get_bus_interfaces s_axi_lite -of_objects $core]
set_property bus_type_vlnv xilinx.com:interface:aximm:1.0 [ipx::get_bus_interfaces s_axi_lite -of_objects $core]
set_property interface_mode slave [ipx::get_bus_interfaces s_axi_lite -of_objects $core]

ipx::add_bus_interface s_axis_rgb $core
set_property abstraction_type_vlnv xilinx.com:interface:axis_rtl:1.0 [ipx::get_bus_interfaces s_axis_rgb -of_objects $core]
set_property bus_type_vlnv xilinx.com:interface:axis:1.0 [ipx::get_bus_interfaces s_axis_rgb -of_objects $core]
set_property interface_mode slave [ipx::get_bus_interfaces s_axis_rgb -of_objects $core]

ipx::add_bus_interface m_axis_jpeg $core
set_property abstraction_type_vlnv xilinx.com:interface:axis_rtl:1.0 [ipx::get_bus_interfaces m_axis_jpeg -of_objects $core]
set_property bus_type_vlnv xilinx.com:interface:axis:1.0 [ipx::get_bus_interfaces m_axis_jpeg -of_objects $core]
set_property interface_mode master [ipx::get_bus_interfaces m_axis_jpeg -of_objects $core]

ipx::update_checksums $core
ipx::save_core $core

puts "hjpeg KV260 AXI-Lite IP packaged at: $ip_dir"
