# Copyright 2026 Pitch Software GmbH
# SPDX-License-Identifier: Apache-2.0
"""End-to-end behaviour of the optimizer and CLI."""

import os
from pathlib import Path

import pikepdf
import pytest
from pytest import CaptureFixture

from pdfdiet import MinimalFileSize, Web, __version__, optimize_document
from pdfdiet.cli import main


def _images(path):
    """Summarise the image XObjects in a PDF.

    Values are copied out while the Pdf is still open: pikepdf objects are
    destroyed along with the Pdf that owns them, so returning them directly
    hands back dangling handles.
    """
    with pikepdf.open(path) as pdf:
        out = []
        for o in pdf.objects:
            if not isinstance(o, pikepdf.Stream):
                continue
            if o.get("/Subtype") != pikepdf.Name.Image or o.get("/ImageMask"):
                continue
            out.append(
                {
                    "width": int(o.Width),
                    "height": int(o.Height),
                    "filter": str(o.get("/Filter")),
                    "colorspace": str(o.get("/ColorSpace")),
                    "smask": "/SMask" in o,
                    "mask": "/Mask" in o,
                }
            )
        return out


class TestRoundTrip:
    def test_output_is_a_valid_readable_pdf(self, gradient_pdf: Path, tmp_path: Path) -> None:
        out = tmp_path / "out.pdf"
        optimize_document(gradient_pdf, out, Web())
        with pikepdf.open(out) as pdf:
            assert len(pdf.pages) == 1

    def test_page_count_is_preserved(self, mixed_pdf: Path, tmp_path: Path) -> None:
        before = len(pikepdf.open(mixed_pdf).pages)
        out = tmp_path / "out.pdf"
        optimize_document(mixed_pdf, out, MinimalFileSize())
        assert len(pikepdf.open(out).pages) == before

    def test_it_actually_shrinks(self, gradient_pdf, tmp_path) -> None:
        out = tmp_path / "out.pdf"
        result = optimize_document(gradient_pdf, out, MinimalFileSize())
        assert result.after_bytes < result.before_bytes
        assert result.ratio > 1.0
        assert 0.0 < result.saved_fraction < 1.0

    def test_result_reports_per_image_detail(self, gradient_pdf, tmp_path) -> None:
        result = optimize_document(gradient_pdf, tmp_path / "o.pdf", Web())
        assert len(result.images) == 1
        img = result.images[0]
        assert img.source_px == (800, 450)
        assert img.after_bytes < img.before_bytes
        assert img.filter.startswith("/")

    def test_repeated_runs_are_stable(self, gradient_pdf, tmp_path) -> None:
        """Optimizing twice must not keep degrading the document."""
        once = tmp_path / "1.pdf"
        twice = tmp_path / "2.pdf"
        optimize_document(gradient_pdf, once, MinimalFileSize())
        optimize_document(once, twice, MinimalFileSize())
        assert os.path.getsize(twice) <= os.path.getsize(once) * 1.10


class TestDownsampling:
    def test_high_dpi_image_loses_pixels(self, highdpi_pdf, tmp_path) -> None:
        out = tmp_path / "out.pdf"
        optimize_document(highdpi_pdf, out, Web())
        after = _images(out)[0]
        assert after["width"] < 800

    def test_low_dpi_image_keeps_its_pixels(self, gradient_pdf, tmp_path) -> None:
        out = tmp_path / "out.pdf"
        optimize_document(gradient_pdf, out, Web())
        after = _images(out)[0]
        assert (after["width"], after["height"]) == (800, 450)

    def test_minimal_profile_is_more_aggressive_than_web(self, highdpi_pdf, tmp_path) -> None:
        web = optimize_document(highdpi_pdf, tmp_path / "w.pdf", Web())
        mini = optimize_document(highdpi_pdf, tmp_path / "m.pdf", MinimalFileSize())
        assert mini.after_bytes <= web.after_bytes


class TestCropping:
    def test_clipped_image_is_cropped(self, clipped_pdf, tmp_path) -> None:
        out = tmp_path / "out.pdf"
        optimize_document(clipped_pdf, out, Web())
        after = _images(out)[0]
        assert after["width"] < 800, "invisible right half should be gone"

    def test_crop_can_be_disabled(self, clipped_pdf, tmp_path) -> None:
        profile = Web()
        profile.crop_to_visible = False
        out = tmp_path / "out.pdf"
        optimize_document(clipped_pdf, out, profile)
        assert _images(out)[0]["width"] == 800

    def test_cropping_adds_a_corrective_matrix(self, clipped_pdf, tmp_path) -> None:
        """The page must still render identically, so the CTM is rewritten."""
        out = tmp_path / "out.pdf"
        optimize_document(clipped_pdf, out, Web())
        pdf = pikepdf.open(out)
        content = pdf.pages[0].Contents.read_bytes()
        assert content.count(b"cm") >= 2, "expected an extra cm for the crop"


class TestSoftMasks:
    def test_opaque_mask_is_dropped(self, opaque_smask_pdf, tmp_path) -> None:
        out = tmp_path / "out.pdf"
        optimize_document(opaque_smask_pdf, out, Web())
        for img in _images(out):
            assert not img["smask"], "a fully opaque mask carries no information"

    def test_graded_mask_is_preserved(self, smask_pdf, tmp_path) -> None:
        out = tmp_path / "out.pdf"
        optimize_document(smask_pdf, out, Web())
        pdf = pikepdf.open(out)
        has_transparency = any(
            "/SMask" in o or "/Mask" in o
            for o in pdf.objects
            if isinstance(o, pikepdf.Stream) and o.get("/Subtype") == pikepdf.Name.Image
        )
        assert has_transparency, "a graded mask must survive in some form"


class TestProfiles:
    def test_minimal_removes_output_intents(self) -> None:
        assert MinimalFileSize().removal.remove_output_intents is True
        assert Web().removal.remove_output_intents is False

    def test_documented_sdk_defaults(self) -> None:
        assert Web().resolution_dpi == 150.0
        assert Web().compression_quality == 0.8
        assert MinimalFileSize().resolution_dpi == 130.0
        assert MinimalFileSize().compression_quality == 0.75

    def test_metadata_is_stripped(self, gradient_pdf: Path, tmp_path: Path) -> None:
        out = tmp_path / "out.pdf"
        optimize_document(gradient_pdf, out, Web())
        assert "/Metadata" not in pikepdf.open(out).Root


class TestCli:
    def test_default_output_path(self, gradient_pdf: Path, capsys: CaptureFixture[str]) -> None:
        assert main([str(gradient_pdf)]) == 0
        expected = os.path.splitext(gradient_pdf)[0] + ".optimized.pdf"
        assert os.path.exists(expected)
        assert "smaller" in capsys.readouterr().out

    def test_explicit_profile_and_quality(self, gradient_pdf: Path, tmp_path: Path) -> None:
        out = str(tmp_path / "o.pdf")
        assert main([str(gradient_pdf), out, "-p", "minimal", "-q", "0.6"]) == 0
        assert os.path.exists(out)

    def test_missing_input_is_reported(self, capsys: CaptureFixture[str]) -> None:
        assert main(["/nonexistent/nope.pdf"]) == 2
        assert "no such file" in capsys.readouterr().err

    def test_quality_out_of_range_is_rejected(
        self, gradient_pdf: Path, capsys: CaptureFixture[str]
    ) -> None:
        assert main([str(gradient_pdf), "-q", "5"]) == 2
        assert "between 0 and 1" in capsys.readouterr().err

    def test_negative_dpi_is_rejected(
        self, gradient_pdf: Path, capsys: CaptureFixture[str]
    ) -> None:
        assert main([str(gradient_pdf), "-d", "-10"]) == 2
        assert "must be positive" in capsys.readouterr().err

    def test_version_flag(self, capsys: CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exc:
            main(["--version"])
        assert exc.value.code == 0
        assert __version__ in capsys.readouterr().out
