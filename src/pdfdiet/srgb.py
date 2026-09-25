# Copyright 2026 Pitch Software GmbH
# SPDX-License-Identifier: Apache-2.0
"""Declare that a PDF's DeviceRGB values are sRGB.

Chrome's print-to-PDF writes colours as bare DeviceRGB and embeds no colour
profile. The numbers are sRGB, but nothing in the file says so. A viewer that
does not assume sRGB sends them straight to the display, and on a wide-gamut
display the result looks oversaturated.

The declaration goes in two places:

- ``/OutputIntents`` on the catalog, for a viewer that looks there.
- ``/DefaultRGB`` in the colour-space resources, which the PDF specification
  defines as the substitute for DeviceRGB. It covers vector fills and
  DeviceRGB image data alike.

No pixel changes and no stream is re-encoded. A viewer that already assumed
sRGB renders exactly what it did before.

This is the Python twin of Pitch's ``backend.integration.pdf-srgb`` and
``pitch.headless.pdf.srgb``, with two differences, both deliberate:

- ``/DefaultRGB`` is also set on form XObjects and tiling patterns. The
  specification looks it up in the *current* resource dictionary, which
  inside a form is the form's own. Skia draws much of a Chrome export inside
  transparency-group forms, so page-level substitution alone misses them.

  Soft-mask groups (an ExtGState's ``/SMask /G``) are deliberately not
  followed. Their colours are never shown: the mask's luminosity becomes
  opacity, which Chrome computed from the raw RGB numbers. Tagging them
  could shift transparency without correcting any colour. Real decks have
  them (Chrome writes one per gradient-opacity fill), so a count of tagged
  forms below the total is expected.
- An existing output intent that is not sRGB is never overwritten. A CMYK
  PDF/X intent describes the file's print condition; replacing it would make
  the file lie.

The profile is Graeme Gill's public-domain sRGB IEC61966-2.1 (ICC v2.2) from
ArgyllCMS. v2 rather than v4, because PDF/A-1-era tooling rejects v4 output
intents. The JDK's profile, which the backend embeds, is GPL-licensed data
and cannot be shipped here.
"""

from __future__ import annotations

import contextlib
from importlib import resources

import pikepdf
from pikepdf import Array, Dictionary, Name

from .images import flate

__all__ = ["PROFILE_NAME", "ForeignOutputIntent", "profile_bytes", "tag_srgb"]

PROFILE_NAME = "sRGB IEC61966-2.1"

# Page trees are shallow; a longer /Parent chain is a cycle in a broken file.
_MAX_TREE_DEPTH = 64


class ForeignOutputIntent(ValueError):
    """The document already declares an output intent that is not sRGB."""


def profile_bytes() -> bytes:
    """The ICC profile this module embeds."""
    return resources.files(__package__).joinpath("data/sRGB.icc").read_bytes()


def _existing_profile(pdf: pikepdf.Pdf) -> pikepdf.Object | None:
    """The ICC stream of an sRGB output intent already in the file, if any.

    Reusing it keeps a second run from embedding a second copy. Raises
    :class:`ForeignOutputIntent` when intents exist and none is sRGB.
    """
    intents = pdf.Root.get("/OutputIntents")
    if not isinstance(intents, Array) or len(intents) == 0:
        return None
    for intent in intents:
        try:
            if str(intent.get("/OutputConditionIdentifier")) != PROFILE_NAME:
                continue
            profile = intent.get("/DestOutputProfile")
            if isinstance(profile, pikepdf.Stream):
                return profile
        except Exception:
            continue
    raise ForeignOutputIntent(
        "the document already declares a non-sRGB output intent; leaving it alone"
    )


def _embedded_copy(pdf: pikepdf.Pdf) -> pikepdf.Object | None:
    """Our profile, already referenced from some page's /DefaultRGB.

    ``remove_output_intents`` prunes the intent of a file tagged on an earlier
    run but leaves its pages pointing at the old stream. Embedding afresh
    would store the profile twice: ``dedupe`` compares raw bytes, and qpdf has
    recompressed the old copy since, so it cannot tell they are the same.
    """
    want = profile_bytes()
    for page in pdf.pages:
        try:
            cs = page.obj.Resources.ColorSpace.DefaultRGB
            if isinstance(cs, Array) and cs[0] == Name.ICCBased and cs[1].read_bytes() == want:
                return cs[1]
        except Exception:
            continue
    return None


def _embed_profile(pdf: pikepdf.Pdf) -> pikepdf.Object:
    """Write the profile into ``pdf`` once and return the indirect stream.

    The output intent and every /DefaultRGB reference this one stream, so the
    file carries the profile once.
    """
    stream = pdf.make_stream(flate(profile_bytes()))
    stream.Filter = Name.FlateDecode
    stream.N = 3
    stream.Alternate = Name.DeviceRGB
    return pdf.make_indirect(stream)


def _set_output_intent(pdf: pikepdf.Pdf, profile: pikepdf.Object) -> None:
    intent = Dictionary(
        Type=Name.OutputIntent,
        # GTS_PDFA1 is the registered subtype for an RGB output intent. It
        # records the working space and claims no PDF/A conformance, which
        # needs XMP metadata this does not add.
        S=Name.GTS_PDFA1,
        OutputConditionIdentifier=pikepdf.String(PROFILE_NAME),
        Info=pikepdf.String(PROFILE_NAME),
        DestOutputProfile=profile,
    )
    pdf.Root.OutputIntents = Array([intent])


def _page_resources(page: pikepdf.Page) -> pikepdf.Object:
    """The page's /Resources, pulled down onto the page if inherited.

    A page can inherit /Resources from its /Pages node. Attaching the
    dictionary to the page keeps the substitution on the page that was
    changed, and matches what both twins do.
    """
    obj = page.obj
    res = obj.get("/Resources")
    if isinstance(res, Dictionary):
        return res
    node = obj.get("/Parent")
    for _ in range(_MAX_TREE_DEPTH):
        if node is None:
            break
        res = node.get("/Resources")
        if isinstance(res, Dictionary):
            break
        node = node.get("/Parent")
    obj.Resources = res if isinstance(res, Dictionary) else Dictionary()
    # Assigning a direct dictionary copies it; hand back the page's copy.
    return obj.Resources


def _set_default_rgb(res: pikepdf.Object, cs: pikepdf.Object) -> bool:
    """Add /DefaultRGB to one resource dictionary. True if it was added.

    An existing /DefaultRGB is the document's own statement about its colours
    and is left as it is.
    """
    spaces = res.get("/ColorSpace")
    if spaces is None:
        res.ColorSpace = Dictionary()
        spaces = res.ColorSpace
    if not isinstance(spaces, Dictionary) or "/DefaultRGB" in spaces:
        return False
    spaces.DefaultRGB = cs
    return True


def _tag_resources(res: pikepdf.Object, cs: pikepdf.Object, seen: set) -> int:
    """Tag ``res`` and every form XObject and tiling pattern it reaches.

    /ExtGState is not walked, so soft-mask groups stay untagged; see the
    module docstring.
    """
    added = 0
    with contextlib.suppress(Exception):
        added += _set_default_rgb(res, cs)
    for category in ("/XObject", "/Pattern"):
        try:
            entries = res.get(category)
            items = list(entries.items()) if isinstance(entries, Dictionary) else []
        except Exception:
            continue
        # Guard each entry, so one awkward value does not abandon the rest.
        for _, obj in items:
            try:
                if not isinstance(obj, pikepdf.Stream) or obj.objgen in seen:
                    continue
                is_form = obj.get("/Subtype") == Name.Form
                is_tiling = obj.get("/PatternType") == 1
                if not (is_form or is_tiling):
                    continue
                seen.add(obj.objgen)
                # A form without /Resources draws with the page's, which is
                # already tagged.
                sub = obj.get("/Resources")
                if isinstance(sub, Dictionary):
                    added += _tag_resources(sub, cs, seen)
            except Exception:
                continue
    return added


def tag_srgb(pdf: pikepdf.Pdf) -> int:
    """Declare ``pdf``'s DeviceRGB values as sRGB, in place.

    Returns how many resource dictionaries gained a /DefaultRGB; 0 means the
    document already carried the full declaration. Running it twice embeds
    the profile once.

    Raises :class:`ForeignOutputIntent`, before changing anything, when the
    document declares a different output intent.
    """
    profile = _existing_profile(pdf)
    if profile is None:
        profile = _embedded_copy(pdf) or _embed_profile(pdf)
        _set_output_intent(pdf, profile)
    cs = pdf.make_indirect(Array([Name.ICCBased, profile]))

    added = 0
    seen: set = set()
    for page in pdf.pages:
        try:
            res = _page_resources(page)
        except Exception:
            continue
        added += _tag_resources(res, cs, seen)
    return added
