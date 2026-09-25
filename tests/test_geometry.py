# Copyright 2026 Pitch Software GmbH
# SPDX-License-Identifier: Apache-2.0
"""Matrix maths, clip tracking and the crop/downsample planner."""

import pikepdf
import pytest

from conftest import PAGE_H, PAGE_W
from pdfdiet.geometry import (
    IDENTITY,
    Placement,
    apply,
    bbox_of_unit_square,
    intersect,
    is_axis_aligned,
    mat_mul,
    plan_image,
    scan_placements,
    union,
)
from pdfdiet.profiles import MinimalFileSize, Web


class TestMatrices:
    def test_identity_is_neutral(self):
        m = (2.0, 0.0, 0.0, 3.0, 5.0, 7.0)
        assert mat_mul(m, IDENTITY) == m
        assert mat_mul(IDENTITY, m) == m

    def test_translation_then_scale_order_matters(self):
        scale = (2.0, 0.0, 0.0, 2.0, 0.0, 0.0)
        move = (1.0, 0.0, 0.0, 1.0, 10.0, 0.0)
        # mat_mul(m, n) means "apply m, then n"
        assert apply(mat_mul(move, scale), 0, 0) == (20.0, 0.0)
        assert apply(mat_mul(scale, move), 0, 0) == (10.0, 0.0)

    def test_axis_alignment_detection(self):
        assert is_axis_aligned((1, 0, 0, 1, 0, 0))
        assert is_axis_aligned((-3, 0, 0, 2, 4, 4))  # mirrored is still aligned
        assert not is_axis_aligned((0, 1, -1, 0, 0, 0))  # 90 degree rotation

    def test_unit_square_bbox(self):
        assert bbox_of_unit_square((10, 0, 0, 20, 5, 7)) == (5, 7, 15, 27)


class TestBoxes:
    def test_intersect_treats_none_as_unbounded(self):
        box = (0, 0, 10, 10)
        assert intersect(None, box) == box
        assert intersect(box, None) == box

    def test_disjoint_intersection_is_empty(self):
        assert intersect((0, 0, 1, 1), (5, 5, 6, 6)) == (0.0, 0.0, 0.0, 0.0)

    def test_union_grows(self):
        assert union((0, 0, 1, 1), (5, 5, 6, 6)) == (0, 0, 6, 6)


def _placement(px, w_pt, h_pt, clip=None):
    return Placement(objgen=(1, 0), ctm=(w_pt, 0.0, 0.0, h_pt, 0.0, 0.0), clip=clip, px=px)


class TestPlanner:
    def test_below_threshold_is_left_alone(self):
        # 800px over 720pt = 80 DPI, far below Web's 210 threshold
        plan = plan_image([_placement((800, 450), PAGE_W, PAGE_H)], Web())
        assert plan.size == (800, 450)
        assert plan.crop is None

    def test_above_threshold_downsamples_to_target(self):
        # 800px over 180pt = 320 DPI -> target 150 DPI
        plan = plan_image([_placement((800, 450), 180.0, 101.25)], Web())
        assert plan.size[0] == round(800 * 150 / 320)

    def test_downsampling_is_per_axis(self):
        """A stretched image has different DPI per axis and must be treated so.

        This is what produces 904x252 -> 794x252 rather than a uniform scale.
        """
        # x: 400px/72pt = 400 DPI (over).  y: 100px/200pt = 36 DPI (under).
        plan = plan_image([_placement((400, 100), 72.0, 200.0)], Web())
        assert plan.size[1] == 100, "y axis was under threshold, must not shrink"
        assert plan.size[0] < 400, "x axis was over threshold, must shrink"

    def test_threshold_is_1_4_times_target(self):
        assert Web().threshold_dpi == pytest.approx(210.0)
        assert MinimalFileSize().threshold_dpi == pytest.approx(182.0)

    def test_resolution_none_disables_downsampling(self):
        profile = Web()
        profile.resolution_dpi = None
        plan = plan_image([_placement((800, 450), 72.0, 40.0)], profile)
        assert plan.size == (800, 450)

    def test_worst_case_dpi_across_placements_wins(self):
        """An image drawn twice must keep enough pixels for the larger use."""
        small = _placement((800, 450), 72.0, 40.5)  # 800 DPI
        large = _placement((800, 450), PAGE_W, PAGE_H)  # 80 DPI
        plan = plan_image([small, large], Web())
        by_small_only = plan_image([small], Web())
        assert plan.size == by_small_only.size

    def test_clip_produces_a_crop(self):
        clip = (0.0, 0.0, PAGE_W / 2, PAGE_H)
        plan = plan_image([_placement((800, 450), PAGE_W, PAGE_H, clip)], Web())
        assert plan.crop is not None
        left, top, right, bottom = plan.crop
        assert right - left == pytest.approx(400, abs=2)
        assert bottom - top == 450

    def test_rotated_images_are_not_cropped(self):
        rotated = Placement(
            objgen=(1, 0),
            ctm=(0.0, 300.0, -300.0, 0.0, 0.0, 0.0),
            clip=(0.0, 0.0, 50.0, 50.0),
            px=(800, 450),
        )
        plan = plan_image([rotated], Web())
        assert plan.crop is None

    def test_degenerate_images_are_skipped(self):
        assert plan_image([_placement((1, 1), 10.0, 10.0)], Web()) is None
        assert plan_image([], Web()) is None

    def test_crop_disabled_by_profile(self):
        profile = Web()
        profile.crop_to_visible = False
        clip = (0.0, 0.0, PAGE_W / 2, PAGE_H)
        plan = plan_image([_placement((800, 450), PAGE_W, PAGE_H, clip)], profile)
        assert plan.crop is None


class TestScanning:
    def test_finds_the_placement(self, gradient_pdf):
        pdf = pikepdf.open(gradient_pdf)
        placements = scan_placements(pdf)
        assert len(placements) == 1
        (only,) = next(iter(placements.values()))
        assert only.px == (800, 450)
        assert only.ctm[0] == pytest.approx(PAGE_W)

    def test_records_the_clip(self, clipped_pdf):
        pdf = pikepdf.open(clipped_pdf)
        (places,) = scan_placements(pdf).values()
        assert places[0].clip is not None
        assert places[0].clip[2] == pytest.approx(PAGE_W / 2)

    def test_two_images_two_entries(self, mixed_pdf):
        pdf = pikepdf.open(mixed_pdf)
        assert len(scan_placements(pdf)) == 2
