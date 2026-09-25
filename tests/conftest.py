# Copyright 2026 Pitch Software GmbH
# SPDX-License-Identifier: Apache-2.0
"""Synthetic PDF fixtures.

The reference deck these behaviours were derived from is a real customer
document and is not in the repository, so every test builds the smallest PDF
that exercises the behaviour in question.
"""

from __future__ import annotations

import math
import random
import zlib

import pikepdf
import pytest
from pikepdf import Dictionary, Name
from PIL import Image

PAGE_W, PAGE_H = 720.0, 405.0  # points; 10 x 5.625 in


# --------------------------------------------------------------------------
# Image generators
# --------------------------------------------------------------------------


def gradient_image(w: int = 800, h: int = 450) -> Image.Image:
    """A smooth two-axis colour ramp: the classic banding provoker."""
    im = Image.new("RGB", (w, h))
    px = im.load()
    for y in range(h):
        for x in range(w):
            px[x, y] = (
                40 + int(180 * x / w),
                20 + int(60 * y / h),
                200 - int(60 * x / w),
            )
    return im


def noisy_image(w: int = 400, h: int = 300, seed: int = 7) -> Image.Image:
    """Photograph-like: high local detail, tolerant of quantisation."""
    rnd = random.Random(seed)
    im = Image.new("RGB", (w, h))
    px = im.load()
    for y in range(h):
        for x in range(w):
            base = int(128 + 100 * math.sin(x / 23.0) * math.cos(y / 17.0))
            px[x, y] = (
                max(0, min(255, base + rnd.randint(-40, 40))),
                max(0, min(255, base + rnd.randint(-40, 40))),
                max(0, min(255, base + rnd.randint(-40, 40))),
            )
    return im


def flat_image(w: int = 200, h: int = 200, color=(10, 200, 90)) -> Image.Image:
    return Image.new("RGB", (w, h), color)


# --------------------------------------------------------------------------
# PDF construction
# --------------------------------------------------------------------------


def _xobject(pdf: pikepdf.Pdf, im: Image.Image) -> pikepdf.Object:
    xo = pdf.make_stream(zlib.compress(im.tobytes(), 6))
    xo.Type = Name.XObject
    xo.Subtype = Name.Image
    xo.Width, xo.Height = im.size
    xo.ColorSpace = Name.DeviceRGB if im.mode == "RGB" else Name.DeviceGray
    xo.BitsPerComponent = 8
    xo.Filter = Name.FlateDecode
    return xo


def build_pdf(path, items, page_size=(PAGE_W, PAGE_H)) -> str:
    """Build a one-page PDF.

    Each item is ``(pil_image, placement, clip, smask)`` where placement is
    ``(w_pt, h_pt, x, y)`` and clip is ``(x, y, w, h)`` in points or None.
    """
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=page_size)

    xobjects = {}
    parts = []
    for i, item in enumerate(items):
        im, placement, clip, smask = item
        name = f"/Im{i}"
        xo = _xobject(pdf, im)
        if smask is not None:
            ms = pdf.make_stream(zlib.compress(smask.tobytes(), 6))
            ms.Type = Name.XObject
            ms.Subtype = Name.Image
            ms.Width, ms.Height = smask.size
            ms.ColorSpace = Name.DeviceGray
            ms.BitsPerComponent = 8
            ms.Filter = Name.FlateDecode
            xo.SMask = ms
        xobjects[name] = xo

        w_pt, h_pt, x, y = placement
        parts.append("q")
        if clip is not None:
            cx, cy, cw, ch = clip
            parts.append(f"{cx} {cy} {cw} {ch} re W n")
        parts.append(f"{w_pt} 0 0 {h_pt} {x} {y} cm {name} Do")
        parts.append("Q")

    page.Resources = Dictionary(
        XObject=Dictionary(**{k.lstrip("/"): v for k, v in xobjects.items()})
    )
    page.Contents = pdf.make_stream("\n".join(parts).encode())
    pdf.save(str(path))
    return str(path)


def build_vector_pdf(
    path,
    pages: int = 1,
    form: bool = False,
    soft_mask: bool = False,
    colour_spaces: dict | None = None,
    output_intent: pikepdf.Dictionary | None = None,
) -> str:
    """Build a PDF that fills a rectangle with DeviceRGB and declares no profile.

    That is the shape Chrome's print-to-PDF emits. ``form`` draws the fill
    inside a form XObject with its own resources, as Skia does for
    transparency groups; ``soft_mask`` fades the fill through a luminosity
    soft mask whose group paints a DeviceRGB ramp, the shape Chrome writes for
    gradient opacity; ``colour_spaces`` seeds each page's /ColorSpace.
    """
    pdf = pikepdf.new()
    fill = b"0.945 0.525 0 rg 0 0 200 200 re f"
    for _ in range(pages):
        page = pdf.add_blank_page(page_size=(PAGE_W, PAGE_H))
        res = Dictionary()
        if colour_spaces:
            res.ColorSpace = Dictionary(colour_spaces)
        if form:
            fx = pdf.make_stream(fill)
            fx.Type = Name.XObject
            fx.Subtype = Name.Form
            fx.BBox = [0, 0, PAGE_W, PAGE_H]
            fx.Resources = Dictionary()
            res.XObject = Dictionary(Fx0=fx)
            page.Contents = pdf.make_stream(b"/Fx0 Do")
        elif soft_mask:
            group = pdf.make_stream(b"0 0 0 rg 0 0 100 200 re f 1 1 1 rg 100 0 100 200 re f")
            group.Type = Name.XObject
            group.Subtype = Name.Form
            group.BBox = [0, 0, 200, 200]
            group.Group = Dictionary(Type=Name.Group, S=Name.Transparency, CS=Name.DeviceRGB)
            group.Resources = Dictionary()
            res.ExtGState = Dictionary(
                GS0=Dictionary(
                    Type=Name.ExtGState,
                    SMask=Dictionary(Type=Name.Mask, S=Name.Luminosity, G=group),
                )
            )
            page.Contents = pdf.make_stream(b"q /GS0 gs " + fill + b" Q")
        else:
            page.Contents = pdf.make_stream(fill)
        page.Resources = res
    if output_intent is not None:
        pdf.Root.OutputIntents = pikepdf.Array([output_intent])
    pdf.save(str(path))
    return str(path)


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture(scope="session")
def gradient_src():
    return gradient_image()


@pytest.fixture
def gradient_pdf(tmp_path, gradient_src):
    """Full-page gradient at ~80 DPI: under threshold, recompressed only."""
    return build_pdf(
        tmp_path / "gradient.pdf", [(gradient_src, (PAGE_W, PAGE_H, 0, 0), None, None)]
    )


@pytest.fixture
def highdpi_pdf(tmp_path, gradient_src):
    """Same image squeezed into a quarter page: ~320 DPI, must downsample."""
    return build_pdf(
        tmp_path / "highdpi.pdf",
        [(gradient_src, (PAGE_W / 4, PAGE_H / 4, 10, 10), None, None)],
    )


@pytest.fixture
def clipped_pdf(tmp_path, gradient_src):
    """Image whose right half is clipped away: must crop and rewrite the CTM."""
    return build_pdf(
        tmp_path / "clipped.pdf",
        [(gradient_src, (PAGE_W, PAGE_H, 0, 0), (0, 0, PAGE_W / 2, PAGE_H), None)],
    )


@pytest.fixture
def smask_pdf(tmp_path, gradient_src):
    """Image carrying a graded soft mask -- the RGBA decode hazard."""
    w, h = gradient_src.size
    mask = Image.linear_gradient("L").resize((w, h))
    return build_pdf(tmp_path / "smask.pdf", [(gradient_src, (PAGE_W, PAGE_H, 0, 0), None, mask)])


@pytest.fixture
def opaque_smask_pdf(tmp_path, gradient_src):
    """Soft mask that is entirely opaque: should be dropped outright."""
    w, h = gradient_src.size
    mask = Image.new("L", (w, h), 255)
    return build_pdf(tmp_path / "opaque.pdf", [(gradient_src, (PAGE_W, PAGE_H, 0, 0), None, mask)])


@pytest.fixture
def mixed_pdf(tmp_path, gradient_src):
    """A gradient and a noisy photo on one page: different quality needs."""
    return build_pdf(
        tmp_path / "mixed.pdf",
        [
            (gradient_src, (PAGE_W, PAGE_H, 0, 0), None, None),
            (noisy_image(), (PAGE_W / 3, PAGE_H / 3, 20, 20), None, None),
        ],
    )


@pytest.fixture
def vector_pdf(tmp_path):
    """One DeviceRGB fill, no colour profile: an untagged Chrome export."""
    return build_vector_pdf(tmp_path / "vector.pdf")
