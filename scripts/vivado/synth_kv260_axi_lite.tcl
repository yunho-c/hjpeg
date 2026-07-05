# hjpeg KV260 AXI-Lite synthesis project
#
# Usage from the repository root, after RTL generation:
#
#   vivado -mode batch -source scripts/vivado/synth_kv260_axi_lite.tcl \
#     -tclargs generated-kv260-axi-lite-top build/vivado/hjpeg-kv260-axi-lite
#
# The first argument is the generated RTL directory containing filelist.f. The
# second argument is the Vivado project directory to create.

set script_dir [file dirname [file normalize [info script]]]
set repo_root [file normalize [file join $script_dir ../..]]

set rtl_dir [file normalize [file join $repo_root generated-kv260-axi-lite-top]]
set project_dir [file normalize [file join $repo_root build/vivado/hjpeg-kv260-axi-lite]]

if {$argc >= 1} {
  set rtl_dir [file normalize [lindex $argv 0]]
}
if {$argc >= 2} {
  set project_dir [file normalize [lindex $argv 1]]
}

set top_name HjpegKv260AxiLiteTop
set part_name xck26-sfvc784-2LV-c
set filelist [file join $rtl_dir filelist.f]

if {![file exists $filelist]} {
  error "Missing generated RTL filelist: $filelist. Run: sbt 'runMain hjpeg.ElaborateKv260AxiLiteTop'"
}

file mkdir $project_dir
create_project hjpeg_kv260_axi_lite $project_dir -part $part_name -force
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

if {[llength $rtl_files] == 0} {
  error "No RTL files listed in $filelist"
}

add_files -norecurse -fileset sources_1 $rtl_files
foreach rtl_file $rtl_files {
  if {[string match *.sv $rtl_file]} {
    set_property file_type SystemVerilog [get_files $rtl_file]
  }
}
set_property top $top_name [current_fileset]
update_compile_order -fileset sources_1

synth_design -top $top_name -part $part_name
report_utilization -file [file join $project_dir post_synth_utilization.rpt]
report_timing_summary -file [file join $project_dir post_synth_timing_summary.rpt]
write_checkpoint -force [file join $project_dir post_synth.dcp]

puts "hjpeg KV260 AXI-Lite synthesis complete: $project_dir"
