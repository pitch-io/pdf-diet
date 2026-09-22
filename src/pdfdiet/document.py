# Copyright 2026 Pitch Software GmbH
# SPDX-License-Identifier: Apache-2.0
"""Document-level surgery: placement rewriting, pruning, deduplication."""

from __future__ import annotations

import contextlib
import hashlib

import pikepdf

from .profiles import RemovalOptions

__all__ = ["adjust_matrix", "rewrite_placements", "prune", "dedupe"]


# --------------------------------------------------------------------------
# Placement rewriting (for cropped images)
# --------------------------------------------------------------------------


def adjust_matrix(uv: tuple[float, float, float, float]) -> tuple:
    """Matrix mapping the cropped image back onto the original footprint.

    After cropping, the new image's unit square must cover only the sub-rect
    ``uv`` of the old one, so the page renders exactly as before.
    """
    u0, v0, u1, v1 = uv
    return (u1 - u0, 0.0, 0.0, v1 - v0, u0, v0)


def _rewrite_stream(pdf, owner, resources, adjust: dict[tuple, tuple], done: set) -> bool:
    """Wrap `Do` of cropped images with a corrective `cm`."""
    if resources is None:
        return False
    xobjs = resources.get("/XObject")
    if xobjs is None:
        return False

    names: dict[str, tuple] = {}
    forms: list = []
    for name, xo in xobjs.items():
        try:
            st = xo.get("/Subtype")
            if st == "/Image" and xo.objgen in adjust:
                names[str(name)] = adjust[xo.objgen]
            elif st == "/Form":
                forms.append(xo)
        except Exception:
            pass

    for fx in forms:
        if fx.objgen in done:
            continue
        done.add(fx.objgen)
        _rewrite_stream(pdf, fx, fx.get("/Resources") or resources, adjust, done)

    if not names:
        return False

    try:
        ops = pikepdf.parse_content_stream(owner)
    except Exception:
        return False

    new_ops = []
    changed = False
    for operands, op in ops:
        if str(op) == "Do" and operands and str(operands[0]) in names:
            m = names[str(operands[0])]
            new_ops.append(([], pikepdf.Operator("q")))
            new_ops.append(([round(float(v), 6) for v in m], pikepdf.Operator("cm")))
            new_ops.append((operands, op))
            new_ops.append(([], pikepdf.Operator("Q")))
            changed = True
        else:
            new_ops.append((operands, op))

    if changed:
        data = pikepdf.unparse_content_stream(new_ops)
        if isinstance(owner, pikepdf.Page):
            owner.Contents = pdf.make_stream(data)
        elif "/Contents" in owner:
            owner["/Contents"] = pdf.make_stream(data)
        else:
            owner.write(data)
    return changed


def rewrite_placements(pdf: pikepdf.Pdf, adjust: dict[tuple, tuple]) -> None:
    """Apply corrective matrices for every cropped image across the document."""
    if not adjust:
        return
    done: set = set()
    for page in pdf.pages:
        _rewrite_stream(pdf, page, page.get("/Resources"), adjust, done)


# --------------------------------------------------------------------------
# Object graph pruning
# --------------------------------------------------------------------------


def prune(pdf: pikepdf.Pdf, r: RemovalOptions) -> None:
    """Discard the parts of the object graph the profile does not keep."""
    root = pdf.Root
    if r.remove_metadata:
        for key in ("/Metadata", "/PieceInfo"):
            if key in root:
                del root[key]
        with contextlib.suppress(Exception):
            del pdf.docinfo
    if r.remove_structure_tree:
        for key in ("/StructTreeRoot", "/MarkInfo"):
            if key in root:
                del root[key]
    if r.remove_article_threads and "/Threads" in root:
        del root["/Threads"]
    if r.remove_output_intents and "/OutputIntents" in root:
        del root["/OutputIntents"]
    if "/SpiderInfo" in root:
        del root["/SpiderInfo"]

    for page in pdf.pages:
        if r.remove_thumbnails and "/Thumb" in page:
            del page["/Thumb"]
        if r.remove_piece_info and "/PieceInfo" in page:
            del page["/PieceInfo"]
        if r.remove_metadata and "/Metadata" in page:
            del page["/Metadata"]
        if r.remove_structure_tree and "/StructParents" in page:
            del page["/StructParents"]


# --------------------------------------------------------------------------
# Redundant object elimination
# --------------------------------------------------------------------------


def _is_ref(v) -> bool:
    """True if ``v`` is an indirect reference.

    pikepdf returns native Python scalars for numbers and strings, so this
    cannot be a bare ``v.is_indirect``.
    """
    return bool(getattr(v, "is_indirect", False))


def _digest(obj: pikepdf.Object) -> bytes | None:
    """Stable digest of a stream object (dictionary plus raw payload).

    Indirect values contribute their object id rather than their resolved
    contents: cheap, cycle-safe, and conservative. Two streams differing only
    by pointing at distinct-but-equal sub-objects will not merge on this pass;
    the next pass catches them once those sub-objects have merged.

    Note that pikepdf hands back plain Python ``int``/``str`` for simple
    values, which have no ``is_indirect``; ``_is_ref`` copes. Getting this
    wrong silently disables deduplication for every image XObject, since
    /Width and /Height are integers.
    """
    try:
        h = hashlib.sha256()
        # .keys() rather than iterating the object: pikepdf containers do not
        # iterate like dicts.
        for k in sorted(str(k) for k in obj.keys() if str(k) != "/Length"):  # noqa: SIM118
            h.update(k.encode())
            v = obj[k]
            h.update(repr(v.objgen).encode() if _is_ref(v) else repr(v).encode())
        h.update(obj.read_raw_bytes())
        return h.digest()
    except Exception:
        return None


def _repoint(obj, replace: dict, seen: set) -> None:
    """Recursively repoint indirect references at their canonical object.

    Must descend through *direct* containers as well: /Resources is very
    often an inline dictionary, so a walk that only visits top-level indirect
    objects silently misses most of the references it needs to fix.
    """
    if isinstance(obj, (pikepdf.Dictionary, pikepdf.Stream)):
        try:
            items = list(obj.items())
        except Exception:
            return
        for k, v in items:
            # Each entry is guarded separately: one awkward value must not
            # abandon the rest of the container half-repointed.
            try:
                if _is_ref(v):
                    if v.objgen in replace:
                        obj[k] = replace[v.objgen]
                        continue
                    if v.objgen in seen:
                        continue
                    seen.add(v.objgen)
                _repoint(v, replace, seen)
            except Exception:
                continue
    elif isinstance(obj, pikepdf.Array):
        try:
            length = len(obj)
        except Exception:
            return
        for i in range(length):
            try:
                v = obj[i]
                if _is_ref(v):
                    if v.objgen in replace:
                        obj[i] = replace[v.objgen]
                        continue
                    if v.objgen in seen:
                        continue
                    seen.add(v.objgen)
                _repoint(v, replace, seen)
            except Exception:
                continue


def dedupe(pdf: pikepdf.Pdf, passes: int = 4) -> int:
    """Merge byte-identical streams and repoint every reference at one copy.

    Iterated, because collapsing a set of form XObjects can make their parents
    identical in turn. Returns the number of objects merged.
    """
    total = 0
    for _ in range(passes):
        canonical: dict[bytes, pikepdf.Object] = {}
        replace: dict[tuple, pikepdf.Object] = {}

        for obj in pdf.objects:
            if not isinstance(obj, pikepdf.Stream):
                continue
            try:
                if obj.get("/Type") == "/Page":
                    continue
            except Exception:
                continue
            d = _digest(obj)
            if d is None:
                continue
            prev = canonical.get(d)
            if prev is None:
                canonical[d] = obj
            elif prev.objgen != obj.objgen:
                replace[obj.objgen] = prev

        if not replace:
            break
        total += len(replace)

        seen: set = set()
        _repoint(pdf.Root, replace, seen)
        _repoint(pdf.trailer, replace, seen)
        for page in pdf.pages:
            _repoint(page.obj, replace, seen)

    return total
