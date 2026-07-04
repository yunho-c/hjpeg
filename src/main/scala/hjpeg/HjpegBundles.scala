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

class YCbCrPixel(c: HjpegConfig) extends Bundle {
  val x = UInt(c.coordBits.W)
  val y = UInt(c.coordBits.W)
  val yComponent = UInt(c.pixelBits.W)
  val cb = UInt(c.pixelBits.W)
  val cr = UInt(c.pixelBits.W)
}

class LevelShiftedYCbCrPixel(c: HjpegConfig) extends Bundle {
  val x = UInt(c.coordBits.W)
  val y = UInt(c.coordBits.W)
  val ySample = SInt((c.pixelBits + 1).W)
  val cbSample = SInt((c.pixelBits + 1).W)
  val crSample = SInt((c.pixelBits + 1).W)
}

class LevelShiftedSampleBlock(val sampleBits: Int = 9) extends Bundle {
  val samples = Vec(HjpegConstants.BlockSize, SInt(sampleBits.W))
}

class DctCoefficientBlock(val coefficientBits: Int = 16) extends Bundle {
  val coefficients = Vec(HjpegConstants.BlockSize, SInt(coefficientBits.W))
}

class QuantizedCoefficientBlock(val coefficientBits: Int = 16) extends Bundle {
  val coefficients = Vec(HjpegConstants.BlockSize, SInt(coefficientBits.W))
}

class ZigZagCoefficientBlock(val coefficientBits: Int = 16) extends Bundle {
  val coefficients = Vec(HjpegConstants.BlockSize, SInt(coefficientBits.W))
}

class ZigZagMinimumCodedUnit(val coefficientBits: Int = 16) extends Bundle {
  val yBlockCount = UInt(3.W)
  val y = new ZigZagCoefficientBlock(coefficientBits)
  val y1 = new ZigZagCoefficientBlock(coefficientBits)
  val y2 = new ZigZagCoefficientBlock(coefficientBits)
  val y3 = new ZigZagCoefficientBlock(coefficientBits)
  val cb = new ZigZagCoefficientBlock(coefficientBits)
  val cr = new ZigZagCoefficientBlock(coefficientBits)
}

class ZigZagMinimumCodedUnitPacket(val coefficientBits: Int = 16) extends Bundle {
  val mcu = new ZigZagMinimumCodedUnit(coefficientBits)
  val last = Bool()
}

class JpegAcRunLengthEvent(val coefficientBits: Int = 16) extends Bundle {
  val runLength = UInt(4.W)
  val coefficient = SInt(coefficientBits.W)
  val emitEndOfBlock = Bool()
  val emitZeroRunLength = Bool()
}

class JpegEntropyToken(val codeBits: Int = 16, val amplitudeBits: Int = 16) extends Bundle {
  val huffmanCode = UInt(codeBits.W)
  val huffmanLength = UInt(5.W)
  val amplitude = UInt(amplitudeBits.W)
  val amplitudeLength = UInt(5.W)
}

class JpegBitRun(val maxBits: Int = 32) extends Bundle {
  val bits = UInt(maxBits.W)
  val length = UInt(6.W)
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

class AxiLiteSlave(addrBits: Int = 12, dataBits: Int = 32) extends Bundle {
  val awaddr = Input(UInt(addrBits.W))
  val awvalid = Input(Bool())
  val awready = Output(Bool())
  val wdata = Input(UInt(dataBits.W))
  val wstrb = Input(UInt((dataBits / 8).W))
  val wvalid = Input(Bool())
  val wready = Output(Bool())
  val bresp = Output(UInt(2.W))
  val bvalid = Output(Bool())
  val bready = Input(Bool())
  val araddr = Input(UInt(addrBits.W))
  val arvalid = Input(Bool())
  val arready = Output(Bool())
  val rdata = Output(UInt(dataBits.W))
  val rresp = Output(UInt(2.W))
  val rvalid = Output(Bool())
  val rready = Input(Bool())
}
