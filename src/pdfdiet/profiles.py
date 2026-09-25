# Copyright 2026 Pitch Software GmbH
# SPDX-License-Identifier: Apache-2.0
"""Optimization profiles, mirroring the Pdftools SDK's profile objects.

Defaults are taken from the published Pdftools API reference for the
corresponding profile, so that behaviour is comparable out of the box.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["PROFILES", "MinimalFileSize", "Profile", "RemovalOptions", "Web"]


@dataclass
class RemovalOptions:
    """Which parts of the object graph to discard.

    Mirrors ``pdftools_sdk.optimization.removal_options.RemovalOptions``.
    Defaults below are the Web profile's; MinimalFileSize overrides
    ``remove_output_intents``.
    """

    remove_alternate_images: bool = False
    remove_article_threads: bool = True
    remove_metadata: bool = True
    remove_output_intents: bool = False
    remove_piece_info: bool = True
    remove_structure_tree: bool = True
    remove_thumbnails: bool = True


@dataclass
class Profile:
    """Base optimization profile.

    Attributes:
        resolution_dpi: Target resolution for downsampling. ``None`` disables
            resolution reduction entirely (images are still recompressed).
        threshold_ratio: Images are only downsampled when their effective
            resolution exceeds ``resolution_dpi * threshold_ratio``. The SDK
            uses 1.4, which avoids resampling images that are already close to
            target -- resampling always costs sharpness.
        compression_quality: 0..1. Drives the per-image PSNR floor; see
            ``images.psnr_floor``.
        reduce_color_complexity: Collapse RGB to grey to bitonal where the
            pixel data allows, and turn fully-opaque or binary soft masks into
            cheaper constructs.
        crop_to_visible: Crop images to their clipped region and rewrite the
            placement matrix. Off by default in no profile -- it is a large
            win on slide exports, where images are routinely clipped to
            rounded rectangles.
        progressive_jpeg: ~4% smaller at identical pixels, but progressive
            JPEG sits outside PDF's "baseline JPEG" wording for DCTDecode.
            Opt-in.
        declare_srgb: Declare the document's DeviceRGB values as sRGB, via an
            output intent and /DefaultRGB; see ``srgb``. Fixes oversaturated
            Chrome exports in viewers that do not assume sRGB. Off by default
            because the Pdftools SDK does not do it, and it is applied after
            ``removal``, so it holds even under ``remove_output_intents``.
    """

    resolution_dpi: float | None = 150.0
    threshold_ratio: float = 1.4
    compression_quality: float = 0.8
    reduce_color_complexity: bool = True
    crop_to_visible: bool = True
    progressive_jpeg: bool = False
    declare_srgb: bool = False
    removal: RemovalOptions = field(default_factory=RemovalOptions)

    @property
    def threshold_dpi(self) -> float:
        """Effective resolution above which an axis gets downsampled."""
        return (self.resolution_dpi or 0.0) * self.threshold_ratio


@dataclass
class Web(Profile):
    """Preserve viewing quality on digital devices. Target 150 DPI."""

    resolution_dpi: float | None = 150.0
    compression_quality: float = 0.8


@dataclass
class MinimalFileSize(Profile):
    """Minimal viable file size. Target 130 DPI.

    On the reference deck this lands smaller *and* measurably closer to the
    original than the commercial tool's Web profile; see README.
    """

    resolution_dpi: float | None = 130.0
    compression_quality: float = 0.75
    removal: RemovalOptions = field(
        default_factory=lambda: RemovalOptions(remove_output_intents=True)
    )


#: Name -> factory, for the CLI and for callers dispatching on a string.
PROFILES = {"web": Web, "minimal": MinimalFileSize}
