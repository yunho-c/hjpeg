# hjpeg KV260 bitstream and hardware-platform build
#
# Usage from the repository root, after creating the block-design project:
#
#   vivado -mode batch -source scripts/vivado/build_kv260_bitstream.tcl \
#     -tclargs build/vivado/hjpeg-kv260-bd build/vivado/hjpeg-kv260-artifacts 8
#
# The first argument is the Vivado project directory created by
# create_kv260_block_design.tcl. The second argument is the artifact output
# directory. The optional third argument is the number of Vivado jobs.

set script_dir [file dirname [file normalize [info script]]]
set repo_root [file normalize [file join $script_dir ../..]]

set project_dir [file normalize [file join $repo_root build/vivado/hjpeg-kv260-bd]]
set artifacts_dir [file normalize [file join $repo_root build/vivado/hjpeg-kv260-artifacts]]
set jobs 4

if {$argc > 3} {
  error "Expected at most 3 arguments: project_dir artifacts_dir jobs"
}
if {$argc >= 1} {
  set project_dir [file normalize [lindex $argv 0]]
}
if {$argc >= 2} {
  set artifacts_dir [file normalize [lindex $argv 1]]
}
if {$argc >= 3} {
  set jobs [lindex $argv 2]
}
if {![regexp {^[1-9][0-9]*$} $jobs]} {
  error "Vivado job count must be a positive integer"
}

set project_file [file join $project_dir hjpeg_kv260_bd.xpr]

if {![file exists $project_file]} {
  error "Missing KV260 block-design project: $project_file. Run: vivado -mode batch -source scripts/vivado/create_kv260_block_design.tcl"
}

file mkdir $artifacts_dir
open_project $project_file
set_property top hjpeg_kv260_wrapper [current_fileset]
update_compile_order -fileset sources_1

reset_run synth_1
launch_runs synth_1 -jobs $jobs
wait_on_run synth_1
if {[get_property PROGRESS [get_runs synth_1]] ne "100%"} {
  error "synth_1 did not complete"
}
if {[get_property STATUS [get_runs synth_1]] ne "synth_design Complete!"} {
  error "synth_1 failed with status: [get_property STATUS [get_runs synth_1]]"
}

open_run synth_1
report_utilization -file [file join $artifacts_dir post_synth_utilization.rpt]
report_timing_summary -file [file join $artifacts_dir post_synth_timing_summary.rpt]
close_design

reset_run impl_1
launch_runs impl_1 -to_step write_bitstream -jobs $jobs
wait_on_run impl_1
if {[get_property PROGRESS [get_runs impl_1]] ne "100%"} {
  error "impl_1 did not complete"
}
if {![string match "*write_bitstream Complete!*" [get_property STATUS [get_runs impl_1]]]} {
  error "impl_1 failed with status: [get_property STATUS [get_runs impl_1]]"
}

open_run impl_1
report_utilization -file [file join $artifacts_dir post_impl_utilization.rpt]
report_timing_summary -file [file join $artifacts_dir post_impl_timing_summary.rpt]
report_drc -file [file join $artifacts_dir post_impl_drc.rpt]
report_route_status -file [file join $artifacts_dir post_impl_route_status.rpt]
report_clock_utilization -file [file join $artifacts_dir post_impl_clock_utilization.rpt]

set bit_candidates [glob -nocomplain -directory [file join $project_dir hjpeg_kv260_bd.runs impl_1] *.bit]
if {[llength $bit_candidates] == 0} {
  error "implementation completed but no bitstream was found"
}
set bit_file [lindex $bit_candidates 0]
file copy -force $bit_file [file join $artifacts_dir hjpeg_kv260.bit]

write_hw_platform -fixed -include_bit -force [file join $artifacts_dir hjpeg_kv260.xsa]
write_checkpoint -force [file join $artifacts_dir post_impl.dcp]

puts "hjpeg KV260 bitstream artifacts written to: $artifacts_dir"
