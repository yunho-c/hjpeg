// See README.md for license details.

package hjpeg

/** Static configuration for the initial JPEG encoder RTL path. */
case class HjpegConfig(
    pixelBits: Int = 8,
    coordBits: Int = 16,
    outputDataBits: Int = 8,
    maxFrameWidth: Int = 1920,
    maxFrameHeight: Int = 1080
) {
  require(pixelBits == 8, "baseline JPEG scaffold expects 8-bit input components")
  require(coordBits > 0, "coordBits must be positive")
  require(outputDataBits == 8, "initial output stream is byte-oriented")
  require(maxFrameWidth > 0, "maxFrameWidth must be positive")
  require(maxFrameHeight > 0, "maxFrameHeight must be positive")
}

object HjpegConstants {
  val BlockDim = 8
  val BlockSize = BlockDim * BlockDim
  val Components = 3
  val MaxBaselineDcCategory = 11
  val MaxHuffmanCodeBits = 16
}

object JpegMarker {
  val Soi = 0xffd8
  val Eoi = 0xffd9
  val App0 = 0xffe0
  val Dqt = 0xffdb
  val Sof0 = 0xffc0
  val Dht = 0xffc4
  val Sos = 0xffda
}
