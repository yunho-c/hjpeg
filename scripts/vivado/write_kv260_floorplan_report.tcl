# hjpeg KV260 floorplan evidence writer
#
# Usage from the repository root, after implementation has completed:
#
#   vivado -mode batch -source scripts/vivado/write_kv260_floorplan_report.tcl \
#     -tclargs build/vivado/hjpeg-kv260-bd build/vivado/hjpeg-kv260-artifacts
#
# The first argument is the Vivado project directory created by
# create_kv260_block_design.tcl. The second argument is the artifact output
# directory. This script reopens the completed implementation run and writes the
# same post_impl_floorplan.rpt evidence produced by build_kv260_bitstream.tcl.

set script_dir [file dirname [file normalize [info script]]]
set repo_root [file normalize [file join $script_dir ../..]]

set project_dir [file normalize [file join $repo_root build/vivado/hjpeg-kv260-bd]]
set artifacts_dir [file normalize [file join $repo_root build/vivado/hjpeg-kv260-artifacts]]

if {$argc > 2} {
  error "Expected at most 2 arguments: project_dir artifacts_dir"
}
if {$argc >= 1} {
  set project_dir [file normalize [lindex $argv 0]]
}
if {$argc >= 2} {
  set artifacts_dir [file normalize [lindex $argv 1]]
}

proc require_nonempty_file {path description} {
  if {![file exists $path]} {
    error "Missing $description: $path"
  }
  if {[file size $path] == 0} {
    error "$description is empty: $path"
  }
}

proc require_complete_impl_run {} {
  set runs [get_runs impl_1 -quiet]
  if {[llength $runs] == 0} {
    error "Missing implementation run impl_1"
  }

  set progress [get_property PROGRESS [get_runs impl_1]]
  set status [get_property STATUS [get_runs impl_1]]
  if {$progress ne "100%"} {
    error "impl_1 is not complete; progress is $progress"
  }
  if {![string match "*write_bitstream Complete!*" $status]} {
    error "impl_1 has not completed bitstream generation; status is: $status"
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
set floorplan_report [file join $artifacts_dir post_impl_floorplan.rpt]

if {![file exists $project_file]} {
  error "Missing KV260 block-design project: $project_file. Run: vivado -mode batch -source scripts/vivado/create_kv260_block_design.tcl"
}

file mkdir $artifacts_dir
open_project $project_file
require_complete_impl_run
open_run impl_1
write_floorplan_report $floorplan_report
require_nonempty_file $floorplan_report "post-implementation floorplan report"

puts "hjpeg KV260 floorplan report written to: $floorplan_report"
