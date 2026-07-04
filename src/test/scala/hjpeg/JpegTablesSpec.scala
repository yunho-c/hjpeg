// See README.md for license details.

package hjpeg

import chisel3._
import chisel3.simulator.scalatest.ChiselSim
import org.scalatest.freespec.AnyFreeSpec
import org.scalatest.matchers.must.Matchers

class JpegTablesSpec extends AnyFreeSpec with Matchers with ChiselSim {
  "JpegQuantTableValue should return unscaled standard tables at quality 50" in {
    simulate(new JpegQuantTableValue()) { dut =>
      dut.io.quality.poke(50.U)

      for (index <- JpegTables.StandardLuminanceQuant.indices) {
        dut.io.isLuminance.poke(true.B)
        dut.io.index.poke(index.U)
        dut.io.value.expect(JpegTables.StandardLuminanceQuant(index).U)

        dut.io.isLuminance.poke(false.B)
        dut.io.index.poke(index.U)
        dut.io.value.expect(JpegTables.StandardChrominanceQuant(index).U)
      }
    }
  }

  "JpegQuantTableValue should scale and clamp quality values" in {
    simulate(new JpegQuantTableValue()) { dut =>
      dut.io.isLuminance.poke(true.B)
      dut.io.index.poke(0.U)

      dut.io.quality.poke(95.U)
      dut.io.value.expect(2.U)

      dut.io.quality.poke(90.U)
      dut.io.value.expect(3.U)

      dut.io.quality.poke(100.U)
      dut.io.value.expect(1.U)

      dut.io.quality.poke(0.U)
      dut.io.value.expect(255.U)
    }
  }

  "JpegZigZagIndex should map scan positions to raster indices" in {
    simulate(new JpegZigZagIndex()) { dut =>
      for (scanIndex <- JpegTables.ZigZagOrder.indices) {
        dut.io.scanIndex.poke(scanIndex.U)
        dut.io.rasterIndex.expect(JpegTables.ZigZagOrder(scanIndex).U)
      }
    }
  }
}
