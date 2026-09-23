# Copyright 2026 Pitch Software GmbH
# SPDX-License-Identifier: Apache-2.0
"""Command line interface."""

from __future__ import annotations

import argparse
import os
import sys

from . import __version__
from .optimizer import optimize_document
from .profiles import PROFILES

__all__ = ["main", "build_parser"]


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="pdf-diet",
        description="Compress a PDF. Open-source take on pdf-tools "
        "optimizeDocument, on a permissively licensed stack.",
    )
    ap.add_argument("input", help="PDF to compress")
    ap.add_argument(
        "output", nargs="?", help="output path (default: <input>.optimized.pdf)"
    )
    ap.add_argument(
        "-p",
        "--profile",
        choices=sorted(PROFILES),
        default="web",
        help="optimization profile (default: web)",
    )
    ap.add_argument(
        "-q",
        "--quality",
        type=float,
        default=None,
        metavar="0..1",
        help="compression quality, overrides the profile's",
    )
    ap.add_argument(
        "-d",
        "--dpi",
        type=float,
        default=None,
        help="target resolution, overrides the profile's",
    )
    ap.add_argument(
        "--no-crop",
        action="store_true",
        help="do not crop images to their visible region",
    )
    ap.add_argument(
        "--progressive",
        action="store_true",
        help="progressive JPEG: ~4%% smaller, but outside PDF's "
        "baseline-JPEG wording for DCTDecode",
    )
    ap.add_argument(
        "--srgb",
        action="store_true",
        help="declare the colours as sRGB (output intent + /DefaultRGB), "
        "for exports that look oversaturated in some viewers",
    )
    ap.add_argument(
        "-v", "--verbose", action="store_true", help="report what happens to each image"
    )
    ap.add_argument("--version", action="version", version=f"pdf-diet {__version__}")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if not os.path.isfile(args.input):
        print(f"pdf-diet: no such file: {args.input}", file=sys.stderr)
        return 2

    profile = PROFILES[args.profile]()
    if args.quality is not None:
        if not 0.0 <= args.quality <= 1.0:
            print("pdf-diet: --quality must be between 0 and 1", file=sys.stderr)
            return 2
        profile.compression_quality = args.quality
    if args.dpi is not None:
        if args.dpi <= 0:
            print("pdf-diet: --dpi must be positive", file=sys.stderr)
            return 2
        profile.resolution_dpi = args.dpi
    if args.no_crop:
        profile.crop_to_visible = False
    if args.progressive:
        profile.progressive_jpeg = True
    if args.srgb:
        profile.declare_srgb = True

    out = args.output or (os.path.splitext(args.input)[0] + ".optimized.pdf")

    try:
        result = optimize_document(args.input, out, profile, verbose=args.verbose)
    except Exception as exc:  # pragma: no cover
        print(f"pdf-diet: failed to optimize {args.input}: {exc}", file=sys.stderr)
        return 1

    print(result)
    if result.srgb_skipped:
        print(f"pdf-diet: sRGB not declared: {result.srgb_skipped}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
