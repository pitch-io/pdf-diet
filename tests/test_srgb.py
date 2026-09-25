# Copyright 2026 Pitch Software GmbH
# SPDX-License-Identifier: Apache-2.0
"""sRGB declaration: output intent plus /DefaultRGB.

Every assertion parses the PDF. The names end up inside compressed object
streams, so a byte search for ``/DefaultRGB`` finds nothing and a test built on
one passes for the wrong reason.
"""

from __future__ import annotations

import pikepdf
import pytest
from pikepdf import Dictionary, Name

from conftest import build_vector_pdf
from pdfdiet import MinimalFileSize, Web, optimize_document
from pdfdiet.cli import main
from pdfdiet.srgb import PROFILE_NAME, ForeignOutputIntent, profile_bytes, tag_srgb


def _tag(src, dst) -> int:
    with pikepdf.open(src) as pdf:
        added = tag_srgb(pdf)
        pdf.save(dst, object_stream_mode=pikepdf.ObjectStreamMode.generate)
    return added


def _default_rgb(resources):
    spaces = resources.get("/ColorSpace")
    return None if spaces is None else spaces.get("/DefaultRGB")


def _declaration(path) -> dict:
    """Summarise the sRGB declaration, copying values out before the Pdf closes."""
    with pikepdf.open(path) as pdf:
        intents = pdf.Root.get("/OutputIntents")
        pages = []
        for page in pdf.pages:
            cs = _default_rgb(page.obj.get("/Resources", Dictionary()))
            pages.append(None if cs is None else (str(cs[0]), cs[1].objgen))
        icc = [
            o
            for o in pdf.objects
            if isinstance(o, pikepdf.Stream) and o.get("/N") == 3 and "/Subtype" not in o
        ]
        out = {"pages": pages, "icc_streams": len(icc), "intents": []}
        for intent in intents or []:
            profile = intent.get("/DestOutputProfile")
            if profile is None:
                out["intents"].append({"id": str(intent.OutputConditionIdentifier)})
                continue
            out["intents"].append(
                {
                    "type": str(intent.get("/Type")),
                    "s": str(intent.get("/S")),
                    "id": str(intent.get("/OutputConditionIdentifier")),
                    "profile": profile.objgen,
                    "n": int(profile.N),
                    "bytes": bytes(profile.read_bytes()),
                }
            )
        return out


def test_fixture_is_untagged(vector_pdf):
    """Otherwise every test below proves nothing."""
    d = _declaration(vector_pdf)
    assert d["intents"] == []
    assert d["pages"] == [None]


class TestTagging:
    def test_catalog_gains_one_srgb_output_intent(self, vector_pdf, tmp_path):
        out = tmp_path / "out.pdf"
        _tag(vector_pdf, out)
        (intent,) = _declaration(out)["intents"]
        assert intent["type"] == "/OutputIntent"
        assert intent["s"] == "/GTS_PDFA1"
        assert intent["id"] == PROFILE_NAME
        assert intent["n"] == 3, "three components, so viewers read it as RGB"

    def test_embedded_profile_survives_the_flate_round_trip(self, vector_pdf, tmp_path):
        # The ICC header alone does not pin the profile down: every RGB
        # profile carries the same signature, so a swapped source would leave
        # the label lying and a header check green.
        out = tmp_path / "out.pdf"
        _tag(vector_pdf, out)
        assert _declaration(out)["intents"][0]["bytes"] == profile_bytes()

    def test_page_substitutes_the_intent_profile_for_device_rgb(self, vector_pdf, tmp_path):
        out = tmp_path / "out.pdf"
        _tag(vector_pdf, out)
        d = _declaration(out)
        family, ref = d["pages"][0]
        assert family == "/ICCBased"
        assert ref == d["intents"][0]["profile"], "one stream, stored once"

    def test_every_page_shares_one_profile(self, tmp_path):
        src = build_vector_pdf(tmp_path / "in.pdf", pages=3)
        out = tmp_path / "out.pdf"
        _tag(src, out)
        d = _declaration(out)
        assert all(p is not None for p in d["pages"])
        assert len({p[1] for p in d["pages"]}) == 1
        assert d["icc_streams"] == 1

    def test_existing_colour_spaces_survive(self, tmp_path):
        src = build_vector_pdf(tmp_path / "in.pdf", colour_spaces={"/CS0": Name.DeviceRGB})
        out = tmp_path / "out.pdf"
        _tag(src, out)
        with pikepdf.open(out) as pdf:
            spaces = pdf.pages[0].Resources.ColorSpace
            assert str(spaces.CS0) == "/DeviceRGB"
            assert "/DefaultRGB" in spaces

    def test_existing_default_rgb_is_not_replaced(self, tmp_path):
        src = build_vector_pdf(tmp_path / "in.pdf", colour_spaces={"/DefaultRGB": Name.DeviceRGB})
        out = tmp_path / "out.pdf"
        _tag(src, out)
        with pikepdf.open(out) as pdf:
            assert pdf.pages[0].Resources.ColorSpace.DefaultRGB == Name.DeviceRGB

    def test_inherited_resources_are_tagged_on_the_page(self, tmp_path):
        # Built in memory: qpdf pushes inherited /Resources onto each page
        # when it writes, so this shape never survives a save and reopen.
        src = build_vector_pdf(tmp_path / "in.pdf", pages=2)
        with pikepdf.open(src) as pdf:
            pdf.Root.Pages.Resources = pdf.pages[0].Resources
            for page in pdf.pages:
                del page.obj["/Resources"]
            tag_srgb(pdf)
            for page in pdf.pages:
                assert _default_rgb(page.obj.Resources) is not None

    def test_form_xobjects_are_tagged(self, tmp_path):
        """/DefaultRGB is looked up in the current resources, i.e. the form's."""
        src = build_vector_pdf(tmp_path / "in.pdf", form=True)
        out = tmp_path / "out.pdf"
        _tag(src, out)
        with pikepdf.open(out) as pdf:
            page_cs = _default_rgb(pdf.pages[0].Resources)
            form_cs = _default_rgb(pdf.pages[0].Resources.XObject.Fx0.Resources)
            assert form_cs is not None
            assert form_cs[1].objgen == page_cs[1].objgen

    def test_soft_mask_groups_are_left_alone(self, tmp_path):
        """A soft mask's colours become opacity, never pixels; see srgb.py."""
        src = build_vector_pdf(tmp_path / "in.pdf", soft_mask=True)
        out = tmp_path / "out.pdf"
        _tag(src, out)
        with pikepdf.open(out) as pdf:
            res = pdf.pages[0].Resources
            assert _default_rgb(res) is not None, "the page itself is still tagged"
            group = res.ExtGState.GS0.SMask.G
            assert _default_rgb(group.Resources) is None

    def test_tagging_twice_embeds_the_profile_once(self, vector_pdf, tmp_path):
        once, twice = tmp_path / "1.pdf", tmp_path / "2.pdf"
        assert _tag(vector_pdf, once) > 0
        assert _tag(once, twice) == 0
        d = _declaration(twice)
        assert len(d["intents"]) == 1
        assert d["icc_streams"] == 1


class TestForeignOutputIntent:
    @pytest.fixture
    def cmyk_pdf(self, tmp_path):
        intent = Dictionary(
            Type=Name.OutputIntent,
            S=Name.GTS_PDFX,
            OutputConditionIdentifier=pikepdf.String("FOGRA39"),
        )
        return build_vector_pdf(tmp_path / "cmyk.pdf", output_intent=intent)

    def test_is_refused_before_anything_changes(self, cmyk_pdf):
        with pikepdf.open(cmyk_pdf) as pdf:
            with pytest.raises(ForeignOutputIntent):
                tag_srgb(pdf)
            assert str(pdf.Root.OutputIntents[0].OutputConditionIdentifier) == "FOGRA39"
            assert _default_rgb(pdf.pages[0].Resources) is None

    def test_optimizer_reports_the_skip_and_still_delivers(self, cmyk_pdf, tmp_path):
        out = tmp_path / "out.pdf"
        result = optimize_document(cmyk_pdf, out, Web(declare_srgb=True))
        assert not result.srgb_tagged
        assert "output intent" in result.srgb_skipped
        assert _declaration(out)["intents"][0]["id"] == "FOGRA39"


class TestOptimizer:
    def test_off_by_default(self, vector_pdf, tmp_path):
        out = tmp_path / "out.pdf"
        result = optimize_document(vector_pdf, out, Web())
        assert not result.srgb_tagged
        assert _declaration(out)["intents"] == []

    def test_declares_srgb_when_asked(self, gradient_pdf, tmp_path):
        """Also covers a recompressed image: it is written as DeviceRGB."""
        out = tmp_path / "out.pdf"
        result = optimize_document(gradient_pdf, out, Web(declare_srgb=True))
        assert result.srgb_tagged and result.srgb_skipped is None
        assert result.images, "the image must have been recompressed"
        d = _declaration(out)
        assert len(d["intents"]) == 1
        assert d["pages"][0] is not None

    def test_survives_remove_output_intents(self, vector_pdf, tmp_path):
        """MinimalFileSize prunes intents; the explicit opt-in must win."""
        profile = MinimalFileSize(declare_srgb=True)
        assert profile.removal.remove_output_intents
        out = tmp_path / "out.pdf"
        optimize_document(vector_pdf, out, profile)
        assert len(_declaration(out)["intents"]) == 1

    def test_repeated_runs_keep_one_profile(self, vector_pdf, tmp_path):
        once, twice = tmp_path / "1.pdf", tmp_path / "2.pdf"
        optimize_document(vector_pdf, once, MinimalFileSize(declare_srgb=True))
        optimize_document(once, twice, MinimalFileSize(declare_srgb=True))
        assert _declaration(twice)["icc_streams"] == 1

    def test_cli_flag(self, vector_pdf, tmp_path):
        out = str(tmp_path / "out.pdf")
        assert main([vector_pdf, out, "--srgb"]) == 0
        assert len(_declaration(out)["intents"]) == 1
