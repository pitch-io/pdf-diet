# Copyright 2026 Pitch Software GmbH
# SPDX-License-Identifier: Apache-2.0
"""The top-level optimization pass."""

import os
from dataclasses import dataclass, field

import pikepdf
from PIL import Image

from .document import adjust_matrix, dedupe, prune, rewrite_placements
from .geometry import plan_image, scan_placements
from .images import (
    encode_candidates,
    flate,
    load_pil,
    normalize_mode,
    reduce_complexity,
    set_image,
)
from .profiles import Profile, Web
from .srgb import tag_srgb

__all__ = ["ImageResult", "Optimizer", "Result", "optimize_document"]


@dataclass
class ImageResult:
    """What happened to one image XObject."""

    objgen: tuple
    source_px: tuple[int, int]
    result_px: tuple[int, int]
    before_bytes: int
    after_bytes: int
    filter: str
    cropped: bool


@dataclass
class Result:
    """Outcome of one optimization run."""

    input_path: str
    output_path: str
    before_bytes: int
    after_bytes: int
    images: list[ImageResult] = field(default_factory=list)
    merged_objects: int = 0
    srgb_tagged: bool = False
    #: Why ``declare_srgb`` was requested but not applied, else None.
    srgb_skipped: str | None = None

    @property
    def ratio(self) -> float:
        return self.before_bytes / self.after_bytes if self.after_bytes else 0.0

    @property
    def saved_fraction(self) -> float:
        if not self.before_bytes:
            return 0.0
        return 1.0 - self.after_bytes / self.before_bytes

    def __str__(self) -> str:
        return (
            f"{self.input_path}: {self.before_bytes / 1e6:.2f} MB -> "
            f"{self.output_path}: {self.after_bytes / 1e6:.2f} MB "
            f"({self.ratio:.1f}x smaller, {100 * self.saved_fraction:.1f}% saved)"
        )


class Optimizer:
    """Compresses PDFs. Mirrors ``pdftools_sdk.optimization.optimizer.Optimizer``.

    Example:
        >>> from pdfdiet import Optimizer, MinimalFileSize
        >>> result = Optimizer().optimize_document("in.pdf", "out.pdf", MinimalFileSize())
        >>> result.ratio > 1
        True
    """

    def __init__(self, verbose: bool = False):
        self.verbose = verbose

    def _log(self, *a) -> None:
        if self.verbose:
            print(*a)

    def _handle_soft_mask(self, pdf, xobj, smask_obj, smask_im, plan, profile) -> None:
        """Drop a fully-opaque mask, stencil a binary one, recompress the rest."""
        if smask_im.mode != "L":
            smask_im = smask_im.convert("L")
        lo, _hi = smask_im.getextrema()

        if lo == 255:  # nothing is transparent
            del xobj["/SMask"]
            return

        colors = smask_im.getcolors(4) or [(0, 1)]
        if profile.reduce_color_complexity and all(v in (0, 255) for _, v in colors):
            # Binary alpha: a 1-bit stencil is far cheaper than an 8-bit mask.
            stencil = smask_im.point(lambda v: 0 if v else 255, mode="1")
            ms = pdf.make_stream(flate(stencil.tobytes()))
            ms.Filter = pikepdf.Name("/FlateDecode")
            ms.Type = pikepdf.Name("/XObject")
            ms.Subtype = pikepdf.Name("/Image")
            ms.Width, ms.Height = plan.size
            ms.ImageMask = True
            ms.BitsPerComponent = 1
            del xobj["/SMask"]
            xobj.Mask = ms
            return

        cands = encode_candidates(smask_im, profile)
        if cands:
            _size, spec = min(cands, key=lambda c: c[0])
            spec["cs"] = "/DeviceGray"
            set_image(smask_obj, pdf, spec, plan.size)

    def optimize_document(self, in_path, out_path, profile: Profile | None = None) -> Result:
        """Optimize ``in_path`` into ``out_path``. Returns a :class:`Result`."""
        profile = profile or Web()
        before_bytes = os.path.getsize(in_path)
        pdf = pikepdf.open(in_path)
        result = Result(
            input_path=str(in_path),
            output_path=str(out_path),
            before_bytes=before_bytes,
            after_bytes=0,
        )

        placements = scan_placements(pdf)
        adjust: dict[tuple, tuple] = {}

        for objgen, ps in placements.items():
            try:
                xobj = pdf.get_object(objgen)
                before = len(xobj.read_raw_bytes())
            except Exception:
                continue

            plan = plan_image(ps, profile)
            if plan is None:
                continue

            im = load_pil(xobj)
            if im is None:
                continue

            smask_obj = xobj.get("/SMask")
            smask_im = load_pil(smask_obj) if smask_obj is not None else None

            if plan.crop:
                im = im.crop(plan.crop)
                if smask_im is not None:
                    # The soft mask may have its own resolution.
                    sx = smask_im.width / ps[0].px[0]
                    sy = smask_im.height / ps[0].px[1]
                    smask_im = smask_im.crop(
                        (
                            int(plan.crop[0] * sx),
                            int(plan.crop[1] * sy),
                            max(1, int(plan.crop[2] * sx)),
                            max(1, int(plan.crop[3] * sy)),
                        )
                    )
            if (im.width, im.height) != plan.size:
                im = im.resize(plan.size, Image.LANCZOS)
            if smask_im is not None and (smask_im.width, smask_im.height) != plan.size:
                smask_im = smask_im.resize(plan.size, Image.LANCZOS)

            if smask_im is not None:
                self._handle_soft_mask(pdf, xobj, smask_obj, smask_im, plan, profile)

            im = normalize_mode(im)
            if profile.reduce_color_complexity:
                im = reduce_complexity(im)

            cands = encode_candidates(im, profile)
            if not cands:
                continue
            size, spec = min(cands, key=lambda c: c[0])

            if size >= before:
                continue  # recompression would not help

            set_image(xobj, pdf, spec, plan.size)
            if plan.uv:
                adjust[objgen] = adjust_matrix(plan.uv)
            result.images.append(
                ImageResult(
                    objgen=objgen,
                    source_px=ps[0].px,
                    result_px=plan.size,
                    before_bytes=before,
                    after_bytes=size,
                    filter=spec["filter"],
                    cropped=plan.crop is not None,
                )
            )
            self._log(
                f"  {ps[0].px[0]}x{ps[0].px[1]} -> "
                f"{plan.size[0]}x{plan.size[1]} {spec['filter']:<12} "
                f"{before / 1024:>9.1f}K -> {size / 1024:>8.1f}K"
            )

        rewrite_placements(pdf, adjust)
        prune(pdf, profile.removal)
        if profile.declare_srgb:
            # After prune, so remove_output_intents cannot strip the intent
            # just added; before dedupe, so it merges with an identical
            # profile already in the file. An undeclared export beats none.
            try:
                tag_srgb(pdf)
                result.srgb_tagged = True
            except Exception as exc:
                result.srgb_skipped = str(exc) or type(exc).__name__
                self._log(f"  sRGB declaration skipped: {result.srgb_skipped}")
        result.merged_objects = dedupe(pdf)
        if result.merged_objects:
            self._log(f"  deduplicated {result.merged_objects} redundant objects")

        pdf.save(
            out_path,
            compress_streams=True,
            object_stream_mode=pikepdf.ObjectStreamMode.generate,
            linearize=False,
            recompress_flate=True,
            stream_decode_level=pikepdf.StreamDecodeLevel.generalized,
        )
        result.after_bytes = os.path.getsize(out_path)
        return result


def optimize_document(
    in_path, out_path, profile: Profile | None = None, verbose: bool = False
) -> Result:
    """Convenience wrapper around :class:`Optimizer`."""
    return Optimizer(verbose=verbose).optimize_document(in_path, out_path, profile)
