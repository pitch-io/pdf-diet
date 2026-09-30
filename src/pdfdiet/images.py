# Copyright 2026 Pitch Software GmbH
# SPDX-License-Identifier: Apache-2.0
"""Decoding, quality assessment and re-encoding of image XObjects.

Two hazards dominate this module, both of which caused visible rendering bugs
before the guards below existed. Read `CLAUDE.md` before changing the codec
selection logic.
"""

import contextlib
import io
import math
import zlib

import pikepdf
from PIL import Image, ImageChops, ImageFile, ImageFilter, ImageStat

from .profiles import Profile

__all__ = [
    "ENCODABLE_MODES",
    "PSNR_FLOOR_CEILING",
    "detail",
    "encode_candidates",
    "flate",
    "load_pil",
    "normalize_mode",
    "psnr",
    "psnr_floor",
    "reduce_complexity",
    "set_image",
]

# Pillow refuses very large images by default as a decompression-bomb guard.
# We are processing documents the user already has, so lift it.
Image.MAX_IMAGE_PIXELS = None

# Pillow writes JPEG through a fixed-size buffer, and `optimize=True` needs the
# whole scan to fit in it. On noisy images at high quality it does not, and the
# save raises OSError("broken data stream when writing image file"). Raising
# the block size fixes the common case; _encode_jpeg retries without optimize
# for the rest.
ImageFile.MAXBLOCK = max(getattr(ImageFile, "MAXBLOCK", 0), 4 * 1024 * 1024)

#: PIL modes that map onto a PDF colour space directly.
ENCODABLE_MODES = {"1", "L", "P", "RGB"}

#: How much smaller zlib level 9 output can be than level 1, with margin.
#: Measured at 1.5x median and 4.3x worst over 60 corpus images. When level 1
#: is this many times larger than the best lossy candidate, lossless cannot
#: win and the (6x slower) level-9 pass is skipped.
_FLATE_LEVEL_GAIN = 8

#: Where the JPEG 2000 search starts, relative to ``ceil(floor)``.
#: OpenJPEG's dB target is measured on its own transformed components and
#: overshoots our RGB PSNR, so the cheapest passing setting is usually 1-3 dB
#: below the floor. Over 883 searches in five decks, starting 2 below took
#: 2.5 encodes on average against 3.5 starting at the floor. Fixed rather than
#: learned per document: a learned guess would make the output depend on
#: which worker saw which image first.
_JP2_GUESS_OFFSET = -2

#: JPEG quality search bounds.
_JPEG_RANGE = (40, 95)
#: JPEG 2000 PSNR-target search bounds, in dB.
_JP2_RANGE = (30, 64)


def flate(data: bytes) -> bytes:
    """Deflate at maximum effort."""
    return zlib.compress(data, 9)


# --------------------------------------------------------------------------
# Quality measurement
# --------------------------------------------------------------------------


def psnr(ref: Image.Image, got: Image.Image) -> float:
    """Peak signal-to-noise ratio in dB. 99.0 means identical."""
    a, b = ref.convert("RGB"), got.convert("RGB")
    if a.size != b.size:
        return 0.0
    hist = ImageChops.difference(a, b).histogram()
    total = count = 0
    for ch in range(3):
        base = ch * 256
        for v in range(256):
            c = hist[base + v]
            total += c * v * v
            count += c
    mse = total / count if count else 0.0
    return 99.0 if mse == 0 else 10.0 * math.log10(255.0 * 255.0 / mse)


def detail(im: Image.Image) -> float:
    """Mean local high-frequency energy.

    Around 0.06 for a smooth gradient, around 10-12 for a photograph. Used to
    decide how much fidelity an image needs.
    """
    g = im.convert("L")
    return ImageStat.Stat(ImageChops.difference(g, g.filter(ImageFilter.GaussianBlur(2)))).mean[0]


#: Absolute ceiling on the per-image quality floor, in dB.
#:
#: Without it the adaptive term below runs away on documents made mostly of
#: flat graphics: at quality 0.8 a smooth image demanded ~53.6 dB, which
#: nothing could satisfy cheaply, so whole decks came out larger than the
#: original encoding or were skipped outright. Measured across a 46-deck
#: corpus, 48 dB is the point where the size regression disappears without
#: banding returning. Lower it and gradients start to mottle.
PSNR_FLOOR_CEILING = 48.0


def psnr_floor(profile: Profile, im: Image.Image) -> float:
    """How much fidelity this particular image needs, in dB.

    **Do not replace this with a constant.** How much distortion an image can
    absorb depends on the image. A photograph hides quantisation error in its
    texture and looks fine at ~35 dB. A flat gradient shows every wavelet
    ripple as banding and needs north of 50 dB. A single threshold cannot
    serve both: tuned for photographs it mottles gradients, tuned for
    gradients it triples the size of every photograph.

    The result is capped at :data:`PSNR_FLOOR_CEILING`; above that the extra
    fidelity is not visible and is very expensive.
    """
    q = max(0.0, min(1.0, profile.compression_quality))
    adaptive = 18.0 + 22.0 * q + min(18.0, 12.0 / (0.3 + detail(im)))
    return min(adaptive, PSNR_FLOOR_CEILING)


# --------------------------------------------------------------------------
# Decoding
# --------------------------------------------------------------------------


def normalize_mode(im: Image.Image) -> Image.Image:
    """Coerce a decoded image into a mode we can faithfully write to PDF.

    Encoding an RGBA buffer while declaring /DeviceRGB writes a stream whose
    component count disagrees with its colour space. Some viewers tolerate
    it, others render grey mush.
    """
    if im.mode in ENCODABLE_MODES:
        return im
    if im.mode in ("LA", "I", "F", "I;16"):
        return im.convert("L")
    return im.convert("RGB")


def load_pil(xobj: pikepdf.Object) -> Image.Image | None:
    """Decode an image XObject's *own* samples, with no mask composited in.

    ``PdfImage.as_pil_image()`` merges /SMask and /Mask into an alpha channel
    and hands back RGBA. Transparency is handled separately here, so detach
    those entries for the duration of the decode and restore them after.
    """
    detached = {}
    try:
        for key in ("/SMask", "/Mask"):
            if key in xobj:
                detached[key] = xobj[key]
                del xobj[key]
    except Exception:
        pass
    try:
        return pikepdf.PdfImage(xobj).as_pil_image()
    except Exception:
        return None
    finally:
        for key, val in detached.items():
            with contextlib.suppress(Exception):
                xobj[key] = val


def reduce_complexity(im: Image.Image) -> Image.Image:
    """Drop redundant colour channels: RGB -> L -> 1 where the data allows."""
    if im.mode == "RGB":
        bands = im.split()
        if len(bands) == 3:
            d1 = ImageChops.difference(bands[0], bands[1]).getextrema()[1]
            d2 = ImageChops.difference(bands[1], bands[2]).getextrema()[1]
            if d1 == 0 and d2 == 0:
                im = bands[0]
    if im.mode == "L":
        lo, hi = im.getextrema()
        if lo != hi:
            colors = im.getcolors(maxcolors=4)
            if colors and all(v in (0, 255) for _, v in colors):
                im = im.convert("1")
    return im


# --------------------------------------------------------------------------
# Encoding
# --------------------------------------------------------------------------


def _encode_jpeg(im: Image.Image, q: int, profile: Profile) -> bytes:
    """Encode as JPEG, falling back if Pillow's optimiser cannot fit the scan.

    `optimize=True` buys a few percent by rebuilding the Huffman tables, but
    it needs the entire scan buffered and raises OSError when it does not fit.
    Letting that propagate silently capped achievable JPEG quality at around
    89 on noisy images, so the search never saw the high end of the range.
    """
    last: Exception | None = None
    for optimize in (True, False):
        buf = io.BytesIO()
        try:
            im.save(
                buf,
                format="JPEG",
                quality=q,
                optimize=optimize,
                progressive=profile.progressive_jpeg,
                subsampling=(0 if q >= 90 else 2),
            )
            return buf.getvalue()
        except OSError as exc:
            last = exc
    raise last if last else RuntimeError("JPEG encoding failed")


def _encode_jp2(im: Image.Image, db: int) -> bytes:
    buf = io.BytesIO()
    im.save(
        buf,
        format="JPEG2000",
        quality_mode="dB",
        quality_layers=[float(db)],
        irreversible=True,
    )
    return buf.getvalue()


def _shape_ok(data: bytes, im: Image.Image, cs: str) -> Image.Image | None:
    """Decode a candidate and confirm it matches the dictionary we will write.

    Cheap insurance: a codec that silently produced a different channel count
    or size would otherwise reach the file and render as garbage.
    """
    try:
        dec = Image.open(io.BytesIO(data))
        dec.load()
    except Exception:
        return None
    if dec.size != im.size:
        return None
    want = 3 if cs == "/DeviceRGB" else 1
    return dec if len(dec.getbands()) == want else None


def _search_codec(
    encode,
    lo: int,
    hi: int,
    im: Image.Image,
    cs: str,
    floor: float,
    guess: int | None = None,
) -> tuple[int, bytes] | None:
    """Cheapest quality setting in [lo, hi] whose decode clears ``floor`` dB.

    Both codecs are monotonic in their quality knob, so a binary search finds
    the cheapest acceptable setting in ~6 encodes. A fixed ladder was tried
    first and overshot badly: the gap between two rungs was the difference
    between 17 KB and 189 KB on a full-page gradient.

    If nothing in the range clears the floor, the highest-quality encoding
    tried is returned rather than nothing. Returning nothing meant the caller
    fell back to lossless, which is normally larger than the source encoding,
    so the image was left completely untouched -- losing the downsampling
    saving as well. Best effort at the top of the range is strictly better,
    and the caller still only uses it if it actually beats the original.

    With ``guess``, the search gallops outward from it before bisecting: the
    same answer as a plain binary search, in far fewer encodes when the guess
    is close. JPEG 2000 targets PSNR directly, so the answer sits within a
    few dB of the floor, and each of its encodes costs ~0.5 s on a 2000 px
    image.
    """
    best: tuple[int, bytes] | None = None
    fallback: tuple[int, bytes] | None = None
    best_psnr = -1.0
    failed = False

    def probe(q: int) -> bool | None:
        """True if ``q`` clears the floor, False if not, None if unusable."""
        nonlocal best, fallback, best_psnr, failed
        try:
            data = encode(q)
        except Exception:
            failed = True
            return None
        dec = _shape_ok(data, im, cs)
        if dec is None:
            failed = True
            return None
        score = psnr(im, dec)
        if score > best_psnr:
            best_psnr = score
            fallback = (len(data), data)
        if score >= floor:
            best = (len(data), data)
            return True
        return False

    if guess is not None and lo <= guess <= hi:
        # Narrow [lo, hi] to a bracket around the boundary, doubling the step.
        ok = probe(guess)
        if ok is None:
            return best or fallback
        step, q = 1, guess
        if ok:
            hi = guess - 1
            while q > lo:
                q = max(lo, guess - step)
                ok = probe(q)
                if ok is None:
                    return best or fallback
                if not ok:
                    lo = q + 1
                    break
                hi = q - 1
                step *= 2
        else:
            lo = guess + 1
            while q < hi:
                q = min(hi, guess + step)
                ok = probe(q)
                if ok is None:
                    return best or fallback
                if ok:
                    hi = q - 1
                    break
                lo = q + 1
                step *= 2
            else:
                return best or fallback

    while lo <= hi and not failed:
        mid = (lo + hi) // 2
        ok = probe(mid)
        if ok is None:
            break
        if ok:
            hi = mid - 1
        else:
            lo = mid + 1
    return best or fallback


def encode_candidates(im: Image.Image, profile: Profile) -> list[tuple[int, dict]]:
    """Encode this image every way we can and return the eligible results.

    Returns ``[(size_in_bytes, spec)]``; the caller takes the smallest.
    A candidate that cannot plausibly be the smallest may be left out.

    **Every candidate must clear the image's quality floor to be eligible.**
    Picking the smallest output across codecs without that gate -- which is
    what the SDK's "smallest output file" wording suggests -- selects JPEG
    2000 at a rate that wins on bytes by a mile and mottles every gradient in
    the document. See ``psnr_floor``.
    """
    im = normalize_mode(im)

    if im.mode == "1":
        raw = flate(im.tobytes())
        return [
            (
                len(raw),
                {"data": raw, "filter": "/FlateDecode", "cs": "/DeviceGray", "bpc": 1},
            )
        ]

    if im.mode == "P":
        raw = flate(im.tobytes())
        palette = im.getpalette() or []
        ncol = max(1, len(palette) // 3)
        return [
            (
                len(raw),
                {
                    "data": raw,
                    "filter": "/FlateDecode",
                    "cs": ("indexed", bytes(palette[: ncol * 3]), ncol - 1),
                    "bpc": 8,
                },
            )
        ]

    cs = "/DeviceGray" if im.mode == "L" else "/DeviceRGB"
    floor = psnr_floor(profile, im)

    out: list[tuple[int, dict]] = []

    hit = _search_codec(lambda q: _encode_jpeg(im, q, profile), *_JPEG_RANGE, im, cs, floor)
    if hit:
        out.append((hit[0], {"data": hit[1], "filter": "/DCTDecode", "cs": cs, "bpc": 8}))

    hit = _search_codec(
        lambda db: _encode_jp2(im, db),
        *_JP2_RANGE,
        im,
        cs,
        floor,
        guess=math.ceil(floor) + _JP2_GUESS_OFFSET,
    )
    if hit:
        out.append((hit[0], {"data": hit[1], "filter": "/JPXDecode", "cs": cs, "bpc": 8}))

    raw = im.tobytes()
    if out and len(zlib.compress(raw, 1)) > _FLATE_LEVEL_GAIN * min(c[0] for c in out):
        # Lossless cannot win, so skip the level-9 deflate: on a photograph
        # it is over half the time spent on the whole image.
        return out
    data = flate(raw)
    out.insert(0, (len(data), {"data": data, "filter": "/FlateDecode", "cs": cs, "bpc": 8}))
    return out


def set_image(xobj: pikepdf.Object, pdf: pikepdf.Pdf, spec: dict, size: tuple[int, int]) -> None:
    """Overwrite an image XObject in place with new pixel data.

    Done in place so every existing reference to the object stays valid.
    Stale entries that described the *old* samples are removed first.
    """
    for key in (
        "/DecodeParms",
        "/Decode",
        "/Interpolate",
        "/Intent",
        "/Alternates",
        "/StructParent",
        "/OPI",
        "/Metadata",
    ):
        if key in xobj:
            del xobj[key]

    xobj.write(spec["data"], filter=pikepdf.Name(spec["filter"]))
    xobj.Width = size[0]
    xobj.Height = size[1]
    xobj.BitsPerComponent = spec["bpc"]

    cs = spec["cs"]
    if isinstance(cs, tuple) and cs[0] == "indexed":
        _, palette, hival = cs
        lut = pdf.make_stream(flate(palette))
        lut.Filter = pikepdf.Name("/FlateDecode")
        xobj.ColorSpace = pikepdf.Array(
            [pikepdf.Name("/Indexed"), pikepdf.Name("/DeviceRGB"), hival, lut]
        )
    else:
        xobj.ColorSpace = pikepdf.Name(cs)
