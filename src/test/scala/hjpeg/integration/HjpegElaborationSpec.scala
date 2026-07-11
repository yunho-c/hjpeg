// See README.md for license details.

package hjpeg

import _root_.circt.stage.ChiselStage
import org.scalatest.freespec.AnyFreeSpec
import org.scalatest.matchers.must.Matchers

import java.nio.file.Files

class HjpegElaborationSpec extends AnyFreeSpec with Matchers {
  "Hjpeg top levels should elaborate" in {
    ChiselStage.emitSystemVerilog(new RgbToYCbCrStage()) must include("module RgbToYCbCrStage")
    ChiselStage.emitSystemVerilog(new YCbCrLevelShiftStage()) must include("module YCbCrLevelShiftStage")
    ChiselStage.emitSystemVerilog(new Dct8x8Stage()) must include("module Dct8x8Stage")
    ChiselStage.emitSystemVerilog(new JpegQuantTableValue()) must include("module JpegQuantTableValue")
    ChiselStage.emitSystemVerilog(new JpegZigZagIndex()) must include("module JpegZigZagIndex")
    ChiselStage.emitSystemVerilog(new JpegMagnitudeValue()) must include("module JpegMagnitudeValue")
    ChiselStage.emitSystemVerilog(new JpegDcHuffmanCode()) must include("module JpegDcHuffmanCode")
    ChiselStage.emitSystemVerilog(new JpegAcHuffmanCode()) must include("module JpegAcHuffmanCode")
    ChiselStage.emitSystemVerilog(new JpegDcEncodeStage()) must include("module JpegDcEncodeStage")
    ChiselStage.emitSystemVerilog(new JpegAcEncodeStage()) must include("module JpegAcEncodeStage")
    ChiselStage.emitSystemVerilog(new JpegAcBlockRunLengthStage()) must include("module JpegAcBlockRunLengthStage")
    ChiselStage.emitSystemVerilog(new JpegBlockEntropyStage()) must include("module JpegBlockEntropyStage")
    ChiselStage.emitSystemVerilog(new JpegBlockTransformStage()) must include("module JpegBlockTransformStage")
    ChiselStage.emitSystemVerilog(new JpegEntropyTokenBitsStage()) must include("module JpegEntropyTokenBitsStage")
    ChiselStage.emitSystemVerilog(new JpegBitRunPacker()) must include("module JpegBitRunPacker")
    ChiselStage.emitSystemVerilog(new JpegHeaderStage()) must include("module JpegHeaderStage")
    ChiselStage.emitSystemVerilog(new JpegRasterToMcuStage()) must include("module JpegRasterToMcuStage")
    ChiselStage.emitSystemVerilog(new JpegRasterToSubsampledMcuStage()) must include("module JpegRasterToSubsampledMcuStage")
    ChiselStage.emitSystemVerilog(new JpegRgb8x8ToMcuStage()) must include("module JpegRgb8x8ToMcuStage")
    ChiselStage.emitSystemVerilog(new JpegSingleMcuEncoderStage()) must include("module JpegSingleMcuEncoderStage")
    ChiselStage.emitSystemVerilog(new JpegMcuStreamEncoderStage()) must include("module JpegMcuStreamEncoderStage")
    ChiselStage.emitSystemVerilog(new JpegRgb8x8EncoderStage()) must include("module JpegRgb8x8EncoderStage")
    ChiselStage.emitSystemVerilog(new QuantizeBlockStage()) must include("module QuantizeBlockStage")
    ChiselStage.emitSystemVerilog(new ZigZagBlockStage()) must include("module ZigZagBlockStage")
    ChiselStage.emitSystemVerilog(new HjpegCore()) must include("module HjpegCore")
    ChiselStage.emitSystemVerilog(new HjpegAxiStreamCore()) must include("module HjpegAxiStreamCore")
    ChiselStage.emitSystemVerilog(new HjpegKv260Top()) must include("module HjpegKv260Top")
    ChiselStage.emitSystemVerilog(new HjpegKv260AxiLiteTop()) must include("module HjpegKv260AxiLiteTop")
  }

  "KV260 AXI-Lite elaboration should emit a Vivado filelist" in {
    val targetDir = Files.createTempDirectory("hjpeg-kv260-axi-lite-elab-")
    HjpegElaboration.emitSystemVerilogFile(new HjpegKv260AxiLiteTop(), targetDir.toString)

    val filelist = targetDir.resolve("filelist.f")
    Files.exists(filelist) mustBe true

    val listedFiles = Files
      .readString(filelist)
      .linesIterator
      .map(_.trim)
      .filter(_.nonEmpty)
      .toSeq

    listedFiles must contain("HjpegKv260AxiLiteTop.sv")
    listedFiles must contain("HjpegAxiStreamCore.sv")
    listedFiles must contain("HjpegCore.sv")
    listedFiles must contain("mem_15360x9.sv")
    listedFiles must contain("mem_30720x9.sv")
    listedFiles.last mustBe "HjpegKv260AxiLiteTop.sv"

    for (rtl <- listedFiles) {
      rtl must endWith(".sv")
      targetDir.resolve(rtl).normalize().startsWith(targetDir) mustBe true
      Files.isRegularFile(targetDir.resolve(rtl)) mustBe true
    }
  }
}
