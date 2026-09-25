# Copyright 2026 Pitch Software GmbH
# SPDX-License-Identifier: Apache-2.0
"""Decoding, quality assessment and codec selection."""

import io
from pathlib import Path

import pikepdf
import pytest
from PIL import Image

from conftest import flat_image, gradient_image, noisy_image
from pdfdiet.images import (
    PSNR_FLOOR_CEILING,
    detail,
    encode_candidates,
    load_pil,
    normalize_mode,
    psnr,
    psnr_floor,
    reduce_complexity,
)
from pdfdiet.profiles import Web


def _best_achievable(im: Image.Image, filt: str) -> float:
    """Highest PSNR the given codec reaches at its maximum setting."""
    buf = io.BytesIO()
    if filt == "/DCTDecode":
        im.save(buf, format="JPEG", quality=95, optimize=True, subsampling=0)
    else:
        im.save(
            buf,
            format="JPEG2000",
            quality_mode="dB",
            quality_layers=[64.0],
            irreversible=True,
        )
    dec = Image.open(io.BytesIO(buf.getvalue()))
    dec.load()
    return psnr(im, dec)


class TestNormalizeMode:
    @pytest.mark.parametrize(
        "mode,expected",
        [
            ("RGB", "RGB"),
            ("L", "L"),
            ("1", "1"),
            ("P", "P"),
            ("RGBA", "RGB"),
            ("LA", "L"),
            ("CMYK", "RGB"),
            ("I", "L"),
            ("F", "L"),
        ],
    )
    def test_every_mode_becomes_encodable(self, mode: str, expected: str) -> None:
        im = Image.new(mode, (8, 8))
        assert normalize_mode(im).mode == expected

    def test_rgba_never_survives(self) -> None:
        """Regression: an RGBA buffer written as /DeviceRGB renders as garbage."""
        assert normalize_mode(Image.new("RGBA", (8, 8))).mode == "RGB"


class TestMetrics:
    def test_psnr_of_identical_images_is_max(self) -> None:
        im = gradient_image(64, 64)
        assert psnr(im, im) == 99.0

    def test_psnr_decreases_with_damage(self) -> None:
        im = gradient_image(64, 64)
        worse = im.point(lambda v: (v // 32) * 32)
        assert psnr(im, worse) < 99.0

    def test_psnr_of_mismatched_sizes_is_zero(self) -> None:
        assert psnr(gradient_image(64, 64), gradient_image(32, 32)) == 0.0

    def test_detail_separates_gradients_from_photographs(self) -> None:
        """The signal the adaptive quality floor is built on."""
        assert detail(gradient_image(256, 256)) < 1.0
        assert detail(noisy_image(256, 256)) > 5.0

    def test_smooth_images_demand_a_higher_floor(self) -> None:
        """Regression: a single fixed floor mottles gradients.

        A gradient must be held to a far stricter standard than a photograph,
        or JPEG 2000 will happily encode it into 1 KB of visible banding.
        """
        profile = Web()
        smooth = psnr_floor(profile, gradient_image(256, 256))
        busy = psnr_floor(profile, noisy_image(256, 256))
        assert smooth > busy + 10.0
        # A flat gradient saturates the adaptive term and lands on the cap.
        assert smooth == pytest.approx(PSNR_FLOOR_CEILING)
        assert busy < 42.0

    def test_floor_rises_with_quality_setting(self) -> None:
        low, high = Web(), Web()
        low.compression_quality = 0.3
        high.compression_quality = 0.95
        im = noisy_image(128, 128)
        assert psnr_floor(high, im) > psnr_floor(low, im)


class TestReduceComplexity:
    def test_grey_rgb_collapses_to_luminance(self) -> None:
        im = Image.merge("RGB", [Image.linear_gradient("L")] * 3)
        assert reduce_complexity(im).mode == "L"

    def test_colour_is_preserved(self) -> None:
        assert reduce_complexity(gradient_image(32, 32)).mode == "RGB"

    def test_black_and_white_collapses_to_bitonal(self) -> None:
        im = Image.new("L", (16, 16), 0)
        im.paste(255, (0, 0, 8, 16))
        assert reduce_complexity(im).mode == "1"


class TestEncodeCandidates:
    def test_candidates_clear_the_floor_or_are_best_effort(self) -> None:
        """The core invariant. Without it, gradients band.

        Every lossy candidate either clears the image's floor, or is the best
        that codec can manage — a candidate is never chosen merely because it
        was small. The second case exists because the floor is capped, so
        some images cannot reach it at any setting; returning nothing there
        meant the image was left completely untouched.
        """
        profile = Web()
        for im in (gradient_image(256, 256), noisy_image(256, 256)):
            floor = psnr_floor(profile, im)
            for _size, spec in encode_candidates(im, profile):
                if spec["filter"] == "/FlateDecode":
                    continue  # lossless, exempt
                dec = Image.open(io.BytesIO(spec["data"]))
                dec.load()
                score = psnr(im, dec)
                if score >= floor - 0.01:
                    continue
                ceiling = _best_achievable(im, spec["filter"])
                assert score >= ceiling - 0.5, (
                    f"{spec['filter']} is below the floor at {score:.2f} dB "
                    f"but the codec could reach {ceiling:.2f} dB"
                )

    def test_floor_never_exceeds_the_ceiling(self) -> None:
        """Uncapped, the adaptive term demanded ~53.6 dB at quality 0.8.

        Nothing could satisfy that cheaply, so whole decks came out larger
        than their original encoding.
        """
        profile = Web()
        profile.compression_quality = 1.0
        for im in (gradient_image(128, 128), flat_image(128, 128), noisy_image(128, 128)):
            assert psnr_floor(profile, im) <= PSNR_FLOOR_CEILING

    def test_unreachable_floor_still_yields_a_lossy_candidate(self) -> None:
        """Regression: returning nothing left the image entirely untouched."""
        profile = Web()
        profile.compression_quality = 1.0
        im = noisy_image(128, 128)
        filters = {spec["filter"] for _n, spec in encode_candidates(im, profile)}
        assert filters - {"/FlateDecode"}, (
            "no lossy candidate offered; the image would be left alone"
        )

    def test_declared_colourspace_matches_channel_count(self) -> None:
        """Regression: /DeviceRGB declared over a 4-channel stream."""
        profile = Web()
        for im in (gradient_image(128, 128), Image.new("L", (128, 128), 90)):
            for _size, spec in encode_candidates(im, profile):
                if spec["filter"] == "/FlateDecode":
                    continue
                dec = Image.open(io.BytesIO(spec["data"]))
                dec.load()
                want = 3 if spec["cs"] == "/DeviceRGB" else 1
                assert len(dec.getbands()) == want
                assert dec.size == im.size

    def test_lossless_is_always_offered(self) -> None:
        cands = encode_candidates(gradient_image(64, 64), Web())
        assert any(s["filter"] == "/FlateDecode" for _n, s in cands)

    def test_bitonal_stays_lossless(self) -> None:
        im = Image.new("1", (64, 64))
        cands = encode_candidates(im, Web())
        assert [s["filter"] for _n, s in cands] == ["/FlateDecode"]
        assert cands[0][1]["bpc"] == 1

    def test_flat_image_compresses_hard(self) -> None:
        cands = encode_candidates(flat_image(256, 256), Web())
        best = min(c[0] for c in cands)
        assert best < 256 * 256 * 3 / 50


class TestLoadPil:
    def test_soft_mask_is_not_composited_into_alpha(self, smask_pdf: Path) -> None:
        """Regression: as_pil_image() merges /SMask and returns RGBA.

        Transparency is handled separately, so the base decode must come back
        in the image's own colour space.
        """
        pdf = pikepdf.open(smask_pdf)
        images = [
            o
            for o in pdf.objects
            if isinstance(o, pikepdf.Stream)
            and o.get("/Subtype") == pikepdf.Name.Image
            and "/SMask" in o
        ]
        assert images, "fixture should contain a masked image"
        im = load_pil(images[0])
        assert im is not None
        assert im.mode == "RGB", f"expected base samples, got {im.mode}"

    def test_mask_entries_are_restored_after_decoding(self, smask_pdf: Path) -> None:
        pdf = pikepdf.open(smask_pdf)
        img = next(
            o
            for o in pdf.objects
            if isinstance(o, pikepdf.Stream)
            and o.get("/Subtype") == pikepdf.Name.Image
            and "/SMask" in o
        )
        load_pil(img)
        assert "/SMask" in img, "load_pil must put the mask back"
