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

proc require_nonempty_file {path description} {
  if {![file exists $path]} {
    error "Missing $description: $path"
  }
  if {[file size $path] == 0} {
    error "$description is empty: $path"
  }
}

proc write_floorplan_report {path} {
  set pblocks [get_pblocks -quiet]
  set placed_cells [get_cells -hierarchical -filter {IS_PRIMITIVE && LOC != ""} -quiet]

  set fp [open $path w]
  puts $fp "Floorplan Summary"
  puts $fp [format "Part: %s" [get_property PART [current_project]]]
  puts $fp [format "Pblock Count: %d" [llength $pblocks]]
  puts $fp [format "Placed Cell Count: %d" [llength $placed_cells]]
  puts $fp "Pblocks:"
  foreach pblock $pblocks {
    set ranges ""
    catch {set ranges [get_property GRID_RANGES $pblock]}
    set cell_count 0
    catch {set cell_count [llength [get_cells -quiet -of_objects $pblock]]}
    puts $fp [format "| %s | Cells: %d | Ranges: %s |" $pblock $cell_count $ranges]
  }
  close $fp
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
set post_synth_utilization [file join $artifacts_dir post_synth_utilization.rpt]
set post_synth_timing [file join $artifacts_dir post_synth_timing_summary.rpt]
report_utilization -file $post_synth_utilization
report_timing_summary -file $post_synth_timing
require_nonempty_file $post_synth_utilization "post-synthesis utilization report"
require_nonempty_file $post_synth_timing "post-synthesis timing report"
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
set post_impl_utilization [file join $artifacts_dir post_impl_utilization.rpt]
set post_impl_hierarchical_utilization [file join $artifacts_dir post_impl_hierarchical_utilization.rpt]
set post_impl_timing [file join $artifacts_dir post_impl_timing_summary.rpt]
set post_impl_drc [file join $artifacts_dir post_impl_drc.rpt]
set post_impl_route_status [file join $artifacts_dir post_impl_route_status.rpt]
set post_impl_clock_utilization [file join $artifacts_dir post_impl_clock_utilization.rpt]
set post_impl_floorplan [file join $artifacts_dir post_impl_floorplan.rpt]
report_utilization -file $post_impl_utilization
report_utilization -hierarchical -hierarchical_depth 10 -file $post_impl_hierarchical_utilization
report_timing_summary -file $post_impl_timing
report_drc -file $post_impl_drc
report_route_status -file $post_impl_route_status
report_clock_utilization -file $post_impl_clock_utilization
write_floorplan_report $post_impl_floorplan
require_nonempty_file $post_impl_utilization "post-implementation utilization report"
require_nonempty_file $post_impl_hierarchical_utilization "post-implementation hierarchical utilization report"
require_nonempty_file $post_impl_timing "post-implementation timing report"
require_nonempty_file $post_impl_drc "post-implementation DRC report"
require_nonempty_file $post_impl_route_status "post-implementation route-status report"
require_nonempty_file $post_impl_clock_utilization "post-implementation clock-utilization report"
require_nonempty_file $post_impl_floorplan "post-implementation floorplan report"

set bit_candidates [glob -nocomplain -directory [file join $project_dir hjpeg_kv260_bd.runs impl_1] *.bit]
if {[llength $bit_candidates] == 0} {
  error "implementation completed but no bitstream was found"
}
if {[llength $bit_candidates] > 1} {
  error "implementation completed but multiple bitstreams were found: $bit_candidates"
}
set bit_file [lindex $bit_candidates 0]
set output_bit [file join $artifacts_dir hjpeg_kv260.bit]
set output_xsa [file join $artifacts_dir hjpeg_kv260.xsa]
set output_dcp [file join $artifacts_dir post_impl.dcp]
file copy -force $bit_file $output_bit
require_nonempty_file $output_bit "copied bitstream"

write_hw_platform -fixed -include_bit -force $output_xsa
require_nonempty_file $output_xsa "hardware platform XSA"
write_checkpoint -force $output_dcp
require_nonempty_file $output_dcp "post-implementation checkpoint"

puts "hjpeg KV260 bitstream artifacts written to: $artifacts_dir"
