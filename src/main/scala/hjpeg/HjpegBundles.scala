// See README.md for license details.

package hjpeg

import chisel3._

class FrameConfig(c: HjpegConfig) extends Bundle {
  val xsize = UInt(c.coordBits.W)
  val ysize = UInt(c.coordBits.W)
  val quality = UInt(7.W)
  val restartInterval = UInt(16.W)
  val enableChromaSubsample = Bool()
  val emitJfif = Bool()
}

class RgbPixel(c: HjpegConfig) extends Bundle {
  val x = UInt(c.coordBits.W)
  val y = UInt(c.coordBits.W)
  val r = UInt(c.pixelBits.W)
  val g = UInt(c.pixelBits.W)
  val b = UInt(c.pixelBits.W)
}

class EncodedByte(c: HjpegConfig) extends Bundle {
  val byte = UInt(c.outputDataBits.W)
  val last = Bool()
}

class AxiStreamWord(dataBits: Int) extends Bundle {
  val data = UInt(dataBits.W)
  val keep = UInt((dataBits / 8).W)
  val last = Bool()
}
