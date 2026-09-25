# Copyright 2026 Pitch Software GmbH
# SPDX-License-Identifier: Apache-2.0
"""Where images actually land on the page, and what to do about it.

This is the part that makes the difference between "recompress every image"
and matching a commercial optimizer. An image XObject carries no resolution
of its own: its effective DPI depends entirely on the matrix it is drawn
under, and the region of it you can actually see depends on the clipping
path in force at that moment. Both have to come out of the content stream.
"""

from __future__ import annotations

import contextlib
import math
from dataclasses import dataclass

import pikepdf

from .profiles import Profile

__all__ = [
    "IDENTITY",
    "Placement",
    "Plan",
    "apply",
    "bbox_of_unit_square",
    "intersect",
    "is_axis_aligned",
    "mat_mul",
    "plan_image",
    "scan_placements",
    "union",
]

#: Identity transform, in PDF's six-number matrix form.
IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)

# Maximum nesting depth when recursing into form XObjects. Guards against
# pathological or maliciously deep documents.
_MAX_DEPTH = 12


def mat_mul(m: tuple, n: tuple) -> tuple:
    """Concatenate ``m`` then ``n`` (PDF convention: result = m x n)."""
    a, b, c, d, e, f = m
    A, B, C, D, E, F = n
    return (
        a * A + b * C,
        a * B + b * D,
        c * A + d * C,
        c * B + d * D,
        e * A + f * C + E,
        e * B + f * D + F,
    )


def apply(m: tuple, x: float, y: float) -> tuple[float, float]:
    """Transform a point by a matrix."""
    a, b, c, d, e, f = m
    return (a * x + c * y + e, b * x + d * y + f)


def is_axis_aligned(m: tuple, tol: float = 1e-6) -> bool:
    """True if the matrix has no rotation or skew."""
    return abs(m[1]) < tol and abs(m[2]) < tol


def bbox_of_unit_square(m: tuple) -> tuple[float, float, float, float]:
    """Device-space bounding box of the unit square under ``m``.

    PDF draws every image into the unit square, so this is the image's
    footprint on the page.
    """
    pts = [apply(m, x, y) for x, y in ((0, 0), (1, 0), (0, 1), (1, 1))]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def intersect(a, b):
    """Intersect two bboxes; ``None`` acts as 'unbounded'."""
    if a is None:
        return b
    if b is None:
        return a
    r = (max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3]))
    return r if r[0] < r[2] and r[1] < r[3] else (0.0, 0.0, 0.0, 0.0)


def union(a, b):
    """Union of two bboxes; ``None`` acts as 'empty'."""
    if a is None:
        return b
    if b is None:
        return a
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


@dataclass
class Placement:
    """One occurrence of an image XObject on a page."""

    objgen: tuple  # pikepdf object id, stable within one Pdf
    ctm: tuple  # matrix in force at the `Do`
    clip: tuple | None  # device-space clip bbox, or None for unclipped
    px: tuple[int, int]  # the XObject's own pixel dimensions


_PATH_OPS = {"m", "l", "c", "v", "y", "re", "h"}
_PAINT_OPS = {"S", "s", "f", "F", "f*", "B", "B*", "b", "b*", "n"}


class _Walker:
    """Collects image placements, tracking CTM and clipping path.

    The clip is tracked as an axis-aligned bounding box. That is an
    approximation: a real clip may be any path. It is deliberately a
    *conservative* one -- the bbox always contains the true clip, so cropping
    to it can never discard a pixel that was visible.
    """

    def __init__(self, page_box):
        self.page_box = page_box
        self.placements: list[Placement] = []

    def run(self, owner, resources, ctm, clip, depth: int = 0, seen=None) -> None:
        if depth > _MAX_DEPTH:
            return
        seen = seen or set()
        try:
            ops = pikepdf.parse_content_stream(owner)
        except Exception:
            return

        stack: list[tuple] = []
        cur_ctm, cur_clip = ctm, clip
        path_bbox = None
        pending_clip = False

        for operands, op in ops:
            o = str(op)

            if o == "q":
                stack.append((cur_ctm, cur_clip))
            elif o == "Q":
                if stack:
                    cur_ctm, cur_clip = stack.pop()
            elif o == "cm":
                with contextlib.suppress(Exception):
                    cur_ctm = mat_mul(tuple(float(x) for x in operands), cur_ctm)

            elif o in _PATH_OPS:
                try:
                    nums = [float(x) for x in operands]
                    if o == "re" and len(nums) == 4:
                        x, y, w, h = nums
                        pts = [(x, y), (x + w, y), (x, y + h), (x + w, y + h)]
                    else:
                        pts = [(nums[i], nums[i + 1]) for i in range(0, len(nums) - 1, 2)]
                    for px, py in pts:
                        dx, dy = apply(cur_ctm, px, py)
                        path_bbox = union(path_bbox, (dx, dy, dx, dy))
                except Exception:
                    pass

            elif o in ("W", "W*"):
                # Clip takes effect at the next path-painting operator.
                pending_clip = True

            elif o in _PAINT_OPS:
                if pending_clip and path_bbox is not None:
                    cur_clip = intersect(cur_clip, path_bbox)
                pending_clip = False
                path_bbox = None

            elif o == "Do":
                xobjs = resources.get("/XObject") if resources is not None else None
                if xobjs is None:
                    continue
                try:
                    xo = xobjs.get(str(operands[0]))
                except Exception:
                    continue
                if xo is None:
                    continue
                subtype = xo.get("/Subtype")
                if subtype == "/Image":
                    with contextlib.suppress(Exception):
                        self.placements.append(
                            Placement(
                                objgen=xo.objgen,
                                ctm=cur_ctm,
                                clip=intersect(cur_clip, self.page_box),
                                px=(int(xo.Width), int(xo.Height)),
                            )
                        )
                elif subtype == "/Form":
                    key = xo.objgen
                    if key in seen:  # cycle guard
                        continue
                    fctm = cur_ctm
                    fm = xo.get("/Matrix")
                    if fm is not None:
                        with contextlib.suppress(Exception):
                            fctm = mat_mul(tuple(float(x) for x in fm), cur_ctm)
                    fclip = cur_clip
                    bb = xo.get("/BBox")
                    if bb is not None:
                        try:
                            v = [float(x) for x in bb]
                            corners = [
                                apply(fctm, v[0], v[1]),
                                apply(fctm, v[2], v[1]),
                                apply(fctm, v[0], v[3]),
                                apply(fctm, v[2], v[3]),
                            ]
                            xs = [c[0] for c in corners]
                            ys = [c[1] for c in corners]
                            fclip = intersect(fclip, (min(xs), min(ys), max(xs), max(ys)))
                        except Exception:
                            pass
                    self.run(
                        xo,
                        xo.get("/Resources") or resources,
                        fctm,
                        fclip,
                        depth + 1,
                        seen | {key},
                    )

            # Inline images (BI/ID/EI) are bounded by the content stream itself
            # and are left alone.


def scan_placements(pdf: pikepdf.Pdf) -> dict[tuple, list[Placement]]:
    """Map each image XObject's objgen to every placement of it in the document."""
    by_obj: dict[tuple, list[Placement]] = {}
    for page in pdf.pages:
        box = page.get("/CropBox") or page.get("/MediaBox")
        try:
            v = [float(x) for x in box]
            page_box = (
                min(v[0], v[2]),
                min(v[1], v[3]),
                max(v[0], v[2]),
                max(v[1], v[3]),
            )
        except Exception:
            page_box = None
        walker = _Walker(page_box)
        walker.run(page, page.get("/Resources") or pikepdf.Dictionary(), IDENTITY, None)
        for p in walker.placements:
            by_obj.setdefault(p.objgen, []).append(p)
    return by_obj


@dataclass
class Plan:
    """What to do with one image XObject."""

    crop: tuple[int, int, int, int] | None  # left, top, right, bottom (px)
    size: tuple[int, int]  # final pixel size
    uv: tuple[float, float, float, float] | None  # crop in image space, u0 v0 u1 v1

    @property
    def is_noop(self) -> bool:
        return self.crop is None and self.uv is None


def plan_image(placements: list[Placement], profile: Profile) -> Plan | None:
    """Decide the crop rectangle and output size for one image XObject.

    Returns ``None`` when the image should be left alone entirely.

    Downsampling is decided **per axis**. An image stretched more horizontally
    than vertically has different effective resolutions on each axis, and
    treating them together either over- or under-samples one of them. The
    reference tool does the same; it is why a 904x252 source comes out
    794x252 rather than uniformly scaled.
    """
    if not placements:
        return None
    px_w, px_h = placements[0].px
    if px_w < 2 or px_h < 2:
        return None

    # --- crop: union of the visible region across all placements ------------
    uv = None
    if profile.crop_to_visible:
        for p in placements:
            if not is_axis_aligned(p.ctm):
                uv = (0.0, 0.0, 1.0, 1.0)  # rotated or skewed: do not crop
                break
            box = bbox_of_unit_square(p.ctm)
            vis = intersect(box, p.clip) if p.clip else box
            if vis is None or vis[2] <= vis[0] or vis[3] <= vis[1]:
                continue
            w = box[2] - box[0]
            h = box[3] - box[1]
            if w <= 0 or h <= 0:
                continue
            u0 = (vis[0] - box[0]) / w
            u1 = (vis[2] - box[0]) / w
            v0 = (vis[1] - box[1]) / h
            v1 = (vis[3] - box[1]) / h
            if p.ctm[0] < 0:  # mirrored horizontally
                u0, u1 = 1 - u1, 1 - u0
            if p.ctm[3] < 0:  # mirrored vertically
                v0, v1 = 1 - v1, 1 - v0
            uv = union(uv, (u0, v0, u1, v1))
        if uv is not None:
            uv = (max(0.0, uv[0]), max(0.0, uv[1]), min(1.0, uv[2]), min(1.0, uv[3]))

    # A crop that saves under 0.1% on both axes is not worth the rewrite.
    if uv is not None and uv[2] - uv[0] >= 0.999 and uv[3] - uv[1] >= 0.999:
        uv = None

    if uv is not None:
        left = max(0, math.floor(uv[0] * px_w))
        right = min(px_w, math.ceil(uv[2] * px_w))
        # PDF image space puts row 0 at the TOP, so v flips.
        top = max(0, math.floor((1.0 - uv[3]) * px_h))
        bottom = min(px_h, math.ceil((1.0 - uv[1]) * px_h))
        if right - left < 2 or bottom - top < 2:
            return None
        crop = (left, top, right, bottom)
        # Snap uv back to the integer pixel grid we will actually cut, so the
        # corrective matrix matches the pixels exactly.
        uv = (left / px_w, 1.0 - bottom / px_h, right / px_w, 1.0 - top / px_h)
        cw, ch = right - left, bottom - top
    else:
        crop = None
        cw, ch = px_w, px_h

    # --- resolution: worst-case effective DPI across placements -------------
    dpi_x = dpi_y = 0.0
    for p in placements:
        w_pt = math.hypot(p.ctm[0], p.ctm[1])
        h_pt = math.hypot(p.ctm[2], p.ctm[3])
        if uv is not None:
            w_pt *= uv[2] - uv[0]
            h_pt *= uv[3] - uv[1]
        if w_pt > 0:
            dpi_x = max(dpi_x, cw / (w_pt / 72.0))
        if h_pt > 0:
            dpi_y = max(dpi_y, ch / (h_pt / 72.0))

    target = profile.resolution_dpi
    nw, nh = cw, ch
    if target:
        thr = profile.threshold_dpi
        if dpi_x > thr:
            nw = max(1, round(cw * target / dpi_x))
        if dpi_y > thr:
            nh = max(1, round(ch * target / dpi_y))

    if crop is None and (nw, nh) == (px_w, px_h):
        return Plan(None, (px_w, px_h), None)
    return Plan(crop, (nw, nh), uv)
