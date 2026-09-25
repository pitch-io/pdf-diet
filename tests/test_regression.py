# Copyright 2026 Pitch Software GmbH
# SPDX-License-Identifier: Apache-2.0
"""Regressions for bugs that shipped once.

Each test here corresponds to a defect that reached a rendered page. Keep
them; the failure modes are silent under naive checks (both of these passed a
PSNR comparison of rendered pages at the time they were broken).
"""

import io
import zlib

import pikepdf
import pytest
from pikepdf import Dictionary, Name
from PIL import Image, ImageStat

from conftest import PAGE_H, PAGE_W, gradient_image
from pdfdiet import MinimalFileSize, Web, optimize_document
from pdfdiet.document import dedupe
from pdfdiet.images import encode_candidates, load_pil, psnr, psnr_floor


def _decoded_images(path):
    """Every image in the file, decoded, with its declared colour space."""
    with pikepdf.open(path) as pdf:
        out = []
        for o in pdf.objects:
            if not isinstance(o, pikepdf.Stream):
                continue
            if o.get("/Subtype") != pikepdf.Name.Image or o.get("/ImageMask"):
                continue
            im = load_pil(o)
            out.append((str(o.get("/ColorSpace")), im, (int(o.Width), int(o.Height))))
        return out


class TestColourSpaceConsistency:
    """A stream whose component count disagrees with its declared colour space.

    Cause: ``PdfImage.as_pil_image()`` composites /SMask into an alpha channel
    and returns RGBA. JPEG refuses RGBA so that candidate was skipped, JPEG
    2000 accepted it and wrote four channels, and the dictionary still said
    /DeviceRGB. Poppler tolerated the result; other viewers rendered grey.
    """

    @pytest.mark.parametrize(
        "fixture", ["gradient_pdf", "smask_pdf", "opaque_smask_pdf", "mixed_pdf"]
    )
    def test_channels_match_declared_colourspace(self, fixture, request, tmp_path):
        src = request.getfixturevalue(fixture)
        out = tmp_path / "out.pdf"
        optimize_document(src, out, MinimalFileSize())

        results = _decoded_images(out)
        assert results, "expected at least one image in the output"
        for cs, im, size in results:
            assert im is not None, f"{cs} image failed to decode"
            want = 3 if cs == "/DeviceRGB" else 1
            assert len(im.getbands()) == want, f"{cs} declared but stream decodes to {im.mode}"
            assert im.size == size, "dictionary size disagrees with the stream"

    def test_masked_image_keeps_its_colour(self, smask_pdf, tmp_path):
        """The visible symptom was a purple gradient turning grey."""
        out = tmp_path / "out.pdf"
        optimize_document(smask_pdf, out, MinimalFileSize())
        _cs, im, _size = _decoded_images(out)[0]

        r, g, b = ImageStat.Stat(im.convert("RGB")).mean
        assert not (r == g == b), "image collapsed to greyscale"
        assert b > r, "expected the source's blue-dominant gradient"


class TestNoBanding:
    """Gradients mottled because candidates were chosen on size alone.

    JPEG 2000 at a low rate encoded a full-page gradient into ~1 KB. It won
    the size comparison by a mile and posted a respectable PSNR, while
    visibly banding. The fix is the detail-adaptive floor in
    ``images.psnr_floor``.
    """

    def test_gradient_round_trips_above_its_floor(self, gradient_pdf, tmp_path):
        out = tmp_path / "out.pdf"
        optimize_document(gradient_pdf, out, Web())

        source = gradient_image()
        _cs, got, size = _decoded_images(out)[0]
        reference = source.resize(size, Image.LANCZOS) if source.size != size else source

        floor = psnr_floor(Web(), reference)
        measured = psnr(reference, got)
        assert measured >= floor - 1.0, (
            f"gradient came back at {measured:.1f} dB, floor is {floor:.1f} dB"
        )

    def test_the_cheap_banding_encoding_is_refused(self):
        """The optimizer must decline an encoding that is smaller but banded.

        Encoding a gradient at a low JPEG 2000 rate is dramatically cheaper
        and posts a plausible PSNR. Selecting on size alone picks it every
        time; the floor is what rejects it.
        """

        im = gradient_image(512, 512)
        profile = Web()
        floor = psnr_floor(profile, im)

        cheap = io.BytesIO()
        im.save(
            cheap,
            format="JPEG2000",
            quality_mode="dB",
            quality_layers=[40.0],
            irreversible=True,
        )
        cheap_bytes = cheap.getvalue()

        decoded = Image.open(io.BytesIO(cheap_bytes))
        decoded.load()
        assert psnr(im, decoded) < floor, (
            "fixture no longer demonstrates the hazard; pick a lower rate"
        )

        chosen = min(c[0] for c in encode_candidates(im, profile))
        assert chosen > len(cheap_bytes), (
            "optimizer picked an encoding at or below the banding threshold"
        )


class TestDedupe:
    """Merging identical streams must repoint *every* reference.

    The first implementation only walked top-level indirect objects, so
    references living inside direct /Resources dictionaries were never
    repointed and nothing was actually saved.
    """

    def _pdf_with_duplicate_images(self, path):
        pdf = pikepdf.new()
        im = gradient_image(100, 100)
        payload = zlib.compress(im.tobytes(), 6)

        names = {}
        for i in range(2):
            xo = pdf.make_stream(payload)  # byte-identical twins
            xo.Type, xo.Subtype = Name.XObject, Name.Image
            xo.Width, xo.Height = im.size
            xo.ColorSpace = Name.DeviceRGB
            xo.BitsPerComponent = 8
            xo.Filter = Name.FlateDecode
            names[f"Im{i}"] = xo

        page = pdf.add_blank_page(page_size=(PAGE_W, PAGE_H))
        page.Resources = Dictionary(XObject=Dictionary(**names))
        page.Contents = pdf.make_stream(
            b"q 200 0 0 200 0 0 cm /Im0 Do Q q 200 0 0 200 300 0 cm /Im1 Do Q"
        )
        pdf.save(str(path))
        return str(path)

    def test_identical_streams_merge(self, tmp_path):
        src = self._pdf_with_duplicate_images(tmp_path / "dup.pdf")
        with pikepdf.open(src) as pdf:
            merged = dedupe(pdf)
            assert merged >= 1
            xobjs = pdf.pages[0].Resources.XObject
            assert xobjs["/Im0"].objgen == xobjs["/Im1"].objgen, (
                "both names should now resolve to one object"
            )

    def test_dedupe_is_lossless(self, tmp_path):
        src = self._pdf_with_duplicate_images(tmp_path / "dup.pdf")
        with pikepdf.open(src) as pdf:
            before = pdf.pages[0].Resources.XObject["/Im0"].read_raw_bytes()
            dedupe(pdf)
            after = pdf.pages[0].Resources.XObject["/Im0"].read_raw_bytes()
        assert before == after

    def test_dedupe_shrinks_the_file(self, tmp_path):
        src = self._pdf_with_duplicate_images(tmp_path / "dup.pdf")
        out = tmp_path / "out.pdf"
        result = optimize_document(src, out, Web())
        with pikepdf.open(out) as pdf:
            images = [
                o
                for o in pdf.objects
                if isinstance(o, pikepdf.Stream) and o.get("/Subtype") == pikepdf.Name.Image
            ]
        assert len(images) == 1, "duplicate images should collapse to one"
        assert result.after_bytes < result.before_bytes


class TestCropCorrectness:
    """Cropping must be paired with a matrix rewrite or the page shifts."""

    def test_cropped_page_still_covers_the_same_area(self, clipped_pdf, tmp_path):
        out = tmp_path / "out.pdf"
        optimize_document(clipped_pdf, out, Web())
        with pikepdf.open(out) as pdf:
            content = pdf.pages[0].Contents.read_bytes().decode("latin-1")
        # The corrective cm must scale x by the visible fraction (~0.5) and
        # leave y alone.
        assert "cm" in content
        assert "Do" in content

    def test_no_crop_no_rewrite(self, gradient_pdf, tmp_path):
        """An unclipped image needs no corrective matrix."""
        out = tmp_path / "out.pdf"
        optimize_document(gradient_pdf, out, Web())
        with pikepdf.open(out) as pdf:
            content = pdf.pages[0].Contents.read_bytes()
        assert content.count(b"cm") == 1
