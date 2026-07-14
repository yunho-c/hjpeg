# Program and exercise the KV260 hjpeg block design through XSDB/JTAG.
#
# The board must already have booted far enough to initialize PS clocks and DDR.
# This script stops Cortex-A53 #0 and programs the PL, so it is intentionally an
# intrusive lab test rather than a coexistence path for a running Linux image.
# The packed frame is downloaded through the aggregate APU target: XSDB treats
# Cortex-A53 core addresses as virtual when its MMU is active, while APU accesses
# the physical DDR address expected by AXI DMA.
#
# Usage:
#   xsdb scripts/host/run_kv260_xsdb_dma.tcl \
#     BITSTREAM INPUT_RGB OUTPUT_JPEG WIDTH HEIGHT QUALITY RESTART \
#     CHROMA_SUBSAMPLE EMIT_JFIF \
#     ?INPUT_ADDR? ?OUTPUT_ADDR? ?OUTPUT_CAPACITY? ?HW_SERVER_URL? ?TRANSCRIPT? \
#     ?PL_CLOCK_HZ? ?MAX_FRAME_CYCLES?
#
# Set the environment variable `HJPEG_XSDB_PREFLIGHT_ONLY=1` to validate all
# arguments, files, frame sizes, DMA limits, and DDR buffer ranges without
# connecting to hw_server or modifying a board.

proc parse_unsigned {name text maximum} {
  if {![regexp {^(0[xX][0-9a-fA-F]+|[0-9]+)$} $text]} {
    error "$name must be an unsigned decimal or hexadecimal integer"
  }
  set value [expr {$text}]
  if {$value < 0 || $value > $maximum} {
    error "$name must be in 0..$maximum"
  }
  return $value
}

proc read32 {address} {
  return [lindex [mrd -value $address] 0]
}

if {$argc < 9 || $argc > 16} {
  error "Expected 9 to 16 arguments: bitstream input_rgb output_jpeg width height quality restart chroma_subsample emit_jfif ?input_addr? ?output_addr? ?output_capacity? ?hw_server_url? ?transcript? ?pl_clock_hz? ?max_frame_cycles?"
}

set bitstream [file normalize [lindex $argv 0]]
set input_rgb [file normalize [lindex $argv 1]]
set output_jpeg [file normalize [lindex $argv 2]]
set width [parse_unsigned width [lindex $argv 3] 3840]
set height [parse_unsigned height [lindex $argv 4] 2160]
set quality [parse_unsigned quality [lindex $argv 5] 100]
set restart_interval [parse_unsigned restart_interval [lindex $argv 6] 65535]
set chroma_subsample [parse_unsigned chroma_subsample [lindex $argv 7] 1]
set emit_jfif [parse_unsigned emit_jfif [lindex $argv 8] 1]

if {$width == 0 || $height == 0} { error "width and height must be nonzero" }
if {$quality == 0} { error "quality must be in 1..100" }
if {![file exists $bitstream] || [file size $bitstream] == 0} {
  error "bitstream is missing or empty: $bitstream"
}
if {![file exists $input_rgb] || [file size $input_rgb] == 0} {
  error "packed RGB input is missing or empty: $input_rgb"
}

set input_addr [expr {$argc >= 10 ? [parse_unsigned input_addr [lindex $argv 9] 0xFFFFFFFF] : 0x60000000}]
set output_addr [expr {$argc >= 11 ? [parse_unsigned output_addr [lindex $argv 10] 0xFFFFFFFF] : 0x64000000}]
set output_capacity [expr {$argc >= 12 ? [parse_unsigned output_capacity [lindex $argv 11] 0x03FFFFFF] : 0x03FFFFFF}]
set hw_server_url [expr {$argc >= 13 ? [lindex $argv 12] : "tcp:localhost:3121"}]
set transcript [file normalize [expr {$argc >= 14 ? [lindex $argv 13] : "${output_jpeg}.xsdb.txt"}]]
set pl_clock_hz [expr {$argc >= 15 ? [parse_unsigned pl_clock_hz [lindex $argv 14] 1000000000] : 100000000}]
set max_frame_cycles [expr {$argc >= 16 ? [parse_unsigned max_frame_cycles [lindex $argv 15] 0x7FFFFFFFFFFFFFFF] : 0}]
if {$pl_clock_hz == 0} { error "pl_clock_hz must be nonzero" }

set input_bytes [file size $input_rgb]
set expected_input_bytes [expr {$width * $height * 4}]
if {$input_bytes != $expected_input_bytes} {
  error "packed RGB byte length $input_bytes does not match width*height*4 ($expected_input_bytes)"
}
if {$input_bytes > 0x03FFFFFF} {
  error "packed RGB input exceeds the AXI DMA 26-bit length field"
}
if {$output_capacity == 0} { error "output_capacity must be nonzero" }

# The block design maps only the low 2 GiB of DDR through S_AXI_HP0_FPD and the
# top 1 MiB is reserved by PMU firmware.
set ddr_limit 0x7FF00000
set input_end [expr {$input_addr + $input_bytes}]
set output_end [expr {$output_addr + $output_capacity}]
if {$input_end > $ddr_limit || $output_end > $ddr_limit} {
  error "DMA buffers must end below 0x7FF00000"
}
if {$input_addr < $output_end && $output_addr < $input_end} {
  error "input and output DMA buffers overlap"
}

if {[info exists ::env(HJPEG_XSDB_PREFLIGHT_ONLY)] && $::env(HJPEG_XSDB_PREFLIGHT_ONLY) eq "1"} {
  puts [format "PREFLIGHT_OK width=%d height=%d input_bytes=%d input_addr=0x%08X input_end=0x%08X output_addr=0x%08X output_capacity=%d output_end=0x%08X pl_clock_hz=%d max_frame_cycles=%d" \
    $width $height $input_bytes $input_addr $input_end $output_addr \
    $output_capacity $output_end $pl_clock_hz $max_frame_cycles]
  exit
}

file mkdir [file dirname $output_jpeg]
file mkdir [file dirname $transcript]

set hjpeg_base 0xA0000000
set dma_base 0xA0010000
set f [open $transcript w]
set connected 0
if {[catch {
  connect -url $hw_server_url
  set connected 1

  targets -set -filter {name == "Cortex-A53 #0"}
  catch {stop}
  targets -set -filter {name == "APU"}
  dow -data $input_rgb $input_addr

  targets -set -filter {name == "PL"}
  fpga -file $bitstream
  after 1000

  targets -set -filter {name == "PSU"}
  memmap -addr $hjpeg_base -size 0x20000 -flags 3

  mwr [expr {$dma_base + 0x00}] 0x00000004
  mwr [expr {$dma_base + 0x30}] 0x00000004
  after 100

  set control [expr {($chroma_subsample ? 2 : 0) | ($emit_jfif ? 4 : 0)}]
  mwr [expr {$hjpeg_base + 0x08}] $width
  mwr [expr {$hjpeg_base + 0x0C}] $height
  mwr [expr {$hjpeg_base + 0x10}] $quality
  mwr [expr {$hjpeg_base + 0x14}] $restart_interval
  mwr [expr {$hjpeg_base + 0x00}] $control

  set status_before [read32 [expr {$hjpeg_base + 0x04}]]
  set completed_before [read32 [expr {$hjpeg_base + 0x20}]]
  puts $f [format "HJPEG_CONFIG control=0x%08X status=0x%08X width=%d height=%d quality=%d restart=%d chroma_subsample=%d emit_jfif=%d input_bytes=%d output_capacity=%d" \
    [read32 [expr {$hjpeg_base + 0x00}]] $status_before $width $height \
    $quality $restart_interval $chroma_subsample $emit_jfif $input_bytes $output_capacity]
  if {$status_before != 0} { error "hjpeg was not idle before transfer" }
  if {$completed_before != 0} { error "completed-frame counter was not reset by PL programming" }

  # Arm S2MM first so the encoder never sees output backpressure at startup.
  mwr [expr {$dma_base + 0x30}] 0x00000001
  mwr [expr {$dma_base + 0x48}] $output_addr
  mwr [expr {$dma_base + 0x58}] $output_capacity
  mwr [expr {$dma_base + 0x00}] 0x00000001
  mwr [expr {$dma_base + 0x18}] $input_addr
  set start_us [clock microseconds]
  mwr [expr {$dma_base + 0x28}] $input_bytes

  set complete 0
  for {set attempt 0} {$attempt < 6000} {incr attempt} {
    after 10
    set mm2s_status [read32 [expr {$dma_base + 0x04}]]
    set s2mm_status [read32 [expr {$dma_base + 0x34}]]
    if {(($mm2s_status & 0x2) != 0) && (($s2mm_status & 0x2) != 0)} {
      set complete 1
      break
    }
  }
  set elapsed_ms [expr {double([clock microseconds] - $start_us) / 1000.0}]
  set mm2s_status [read32 [expr {$dma_base + 0x04}]]
  set s2mm_status [read32 [expr {$dma_base + 0x34}]]
  set hjpeg_status [read32 [expr {$hjpeg_base + 0x04}]]
  for {set attempt 0} {$attempt < 100 && (($hjpeg_status & 0x1) != 0)} {incr attempt} {
    after 1
    set hjpeg_status [read32 [expr {$hjpeg_base + 0x04}]]
  }
  set mm2s_length [read32 [expr {$dma_base + 0x28}]]
  set s2mm_length [read32 [expr {$dma_base + 0x58}]]
  set frame_cycles_low [read32 [expr {$hjpeg_base + 0x18}]]
  set frame_cycles_high [read32 [expr {$hjpeg_base + 0x1C}]]
  set completed_after [read32 [expr {$hjpeg_base + 0x20}]]
  set frame_cycles [expr {wide($frame_cycles_low) | (wide($frame_cycles_high) << 32)}]
  if {$completed_after != 1 || $frame_cycles <= 0} {
    error "hjpeg frame-cycle evidence is missing or inconsistent"
  }
  set frame_ms_100mhz [expr {double($frame_cycles) / 100000.0}]
  set frame_fps_100mhz [expr {100000000.0 / double($frame_cycles)}]
  set frame_ms [expr {double($frame_cycles) * 1000.0 / double($pl_clock_hz)}]
  set frame_fps [expr {double($pl_clock_hz) / double($frame_cycles)}]
  set frame_target_required [expr {$max_frame_cycles > 0}]
  set frame_target_met [expr {!$frame_target_required || $frame_cycles <= $max_frame_cycles}]
  puts $f [format "DMA_COMPLETE complete=%d elapsed_ms=%.3f mm2s_sr=0x%08X s2mm_sr=0x%08X hjpeg_status=0x%08X mm2s_length=%d s2mm_length=%d" \
    $complete $elapsed_ms $mm2s_status $s2mm_status $hjpeg_status \
    $mm2s_length $s2mm_length]
  puts $f [format "FRAME_TIMING cycles=%d milliseconds_at_100mhz=%.6f fps_at_100mhz=%.6f clock_hz=%d milliseconds=%.6f fps=%.6f max_frame_cycles=%d target_required=%d target_met=%d completed_frames=%d" \
    $frame_cycles $frame_ms_100mhz $frame_fps_100mhz $pl_clock_hz \
    $frame_ms $frame_fps $max_frame_cycles $frame_target_required \
    $frame_target_met $completed_after]

  if {!$complete} { error "DMA transfer timed out" }
  if {(($mm2s_status | $s2mm_status) & 0x00000770) != 0} {
    error "DMA reported an error status"
  }
  if {$hjpeg_status != 0} {
    error "hjpeg did not return to idle without protocol error"
  }
  if {$mm2s_length != $input_bytes} {
    error "MM2S length did not retain the full input byte count"
  }
  if {$s2mm_length <= 0 || $s2mm_length >= $output_capacity} {
    error "S2MM returned an invalid JPEG byte count"
  }

  mrd -size b -bin -file $output_jpeg $output_addr $s2mm_length
  if {!$frame_target_met} {
    error "frame latency $frame_cycles cycles exceeds target $max_frame_cycles cycles"
  }
} result options]} {
  puts $f "RUN_ERROR: $result"
  puts $f "RUN_OPTIONS: $options"
  close $f
  if {$connected} { catch {disconnect} }
  exit 1
}

puts $f "RUN_OK"
close $f
if {$connected} { disconnect }
exit
