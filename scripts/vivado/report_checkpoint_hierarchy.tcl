# Hierarchical utilization report for an existing Vivado checkpoint.
#
# Usage from the repository root:
#
#   vivado -mode batch -source scripts/vivado/report_checkpoint_hierarchy.tcl \
#     -tclargs build/vivado/hjpeg-kv260-artifacts/post_impl.dcp \
#       build/vivado/hjpeg-kv260-artifacts/post_impl_hierarchical_utilization.rpt 8

set script_dir [file dirname [file normalize [info script]]]
set repo_root [file normalize [file join $script_dir ../..]]

set checkpoint [file normalize [file join $repo_root build/vivado/hjpeg-kv260-artifacts/post_impl.dcp]]
set output_report [file normalize [file join $repo_root build/vivado/hjpeg-kv260-artifacts/post_impl_hierarchical_utilization.rpt]]
set depth 8

if {$argc > 3} {
  error "Expected at most 3 arguments: checkpoint output_report depth"
}
if {$argc >= 1} {
  set checkpoint [file normalize [lindex $argv 0]]
}
if {$argc >= 2} {
  set output_report [file normalize [lindex $argv 1]]
}
if {$argc >= 3} {
  set depth [lindex $argv 2]
}
if {![regexp {^[1-9][0-9]*$} $depth]} {
  error "Hierarchy depth must be a positive integer"
}
if {![file exists $checkpoint]} {
  error "Missing checkpoint: $checkpoint"
}
if {[file size $checkpoint] == 0} {
  error "Checkpoint is empty: $checkpoint"
}

file mkdir [file dirname $output_report]
open_checkpoint $checkpoint
report_utilization -hierarchical -hierarchical_depth $depth -file $output_report

if {![file exists $output_report] || [file size $output_report] == 0} {
  error "Hierarchical utilization report was not written: $output_report"
}

puts "hjpeg hierarchical utilization report written to: $output_report"
