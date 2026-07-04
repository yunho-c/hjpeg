// See README.md for license details.

package hjpeg

import chisel3._
import chisel3.util._

object JpegColorConversion {
  def clampToPixel(value: SInt, pixelBits: Int): UInt =
    Mux(value < 0.S, 0.U(pixelBits.W), Mux(value > 255.S, 255.U(pixelBits.W), value.asUInt(pixelBits - 1, 0)))

  def rgbToYCbCr(rIn: UInt, gIn: UInt, bIn: UInt, pixelBits: Int): (UInt, UInt, UInt) = {
    val r = rIn.zext
    val g = gIn.zext
    val b = bIn.zext

    val yScaled = (77.S * r) + (150.S * g) + (29.S * b)
    val cbDeltaScaled = ((-43).S * r) + ((-85).S * g) + (128.S * b)
    val crDeltaScaled = (128.S * r) + ((-107).S * g) + ((-21).S * b)

    (
      clampToPixel(yScaled >> 8, pixelBits),
      clampToPixel((cbDeltaScaled >> 8) + 128.S, pixelBits),
      clampToPixel((crDeltaScaled >> 8) + 128.S, pixelBits)
    )
  }
}

/** Streaming RGB to full-range YCbCr conversion.
  *
  * Coefficients are Q8 fixed-point approximations of the usual JPEG/JFIF color
  * transform:
  *
  *   Y  =  0.299 R + 0.587 G + 0.114 B
  *   Cb = -0.168 R - 0.331 G + 0.500 B + 128
  *   Cr =  0.500 R - 0.418 G - 0.081 B + 128
  *
  * This module is combinational and preserves input coordinates. It is a
  * pipeline boundary so later buffering/DCT stages can consume component
  * samples without knowing about RGB packing.
  */
class RgbToYCbCrStage(c: HjpegConfig = HjpegConfig()) extends Module {
  val io = IO(new Bundle {
    val input = Flipped(Decoupled(new RgbPixel(c)))
    val output = Decoupled(new YCbCrPixel(c))
  })

  val (yComponent, cb, cr) = JpegColorConversion.rgbToYCbCr(io.input.bits.r, io.input.bits.g, io.input.bits.b, c.pixelBits)

  io.input.ready := io.output.ready
  io.output.valid := io.input.valid
  io.output.bits.x := io.input.bits.x
  io.output.bits.y := io.input.bits.y
  io.output.bits.yComponent := yComponent
  io.output.bits.cb := cb
  io.output.bits.cr := cr
}
