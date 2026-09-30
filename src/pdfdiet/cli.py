# Copyright 2026 Pitch Software GmbH
# SPDX-License-Identifier: Apache-2.0
"""Command line interface."""

import argparse
import os
import sys

from . import __version__
from .optimizer import declare_srgb, optimize_document
from .profiles import PROFILES

__all__ = [
    "build_parser",
    "main",
]


def _positive_int(text: str) -> int:
    n = int(text)
    if n < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return n


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="pdf-diet",
        description="Compress a PDF. Open-source take on pdf-tools "
        "optimizeDocument, on a permissively licensed stack.",
    )
    ap.add_argument("input", help="PDF to compress")
    ap.add_argument("output", nargs="?", help="output path (default: <input>.optimized.pdf)")
    ap.add_argument(
        "-p",
        "--profile",
        choices=sorted(PROFILES),
        default=None,
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
        "--srgb-only",
        action="store_true",
        help="declare the colours as sRGB and compress nothing: no image is "
        "re-encoded, so the output is lossless",
    )
    ap.add_argument(
        "-j",
        "--jobs",
        type=_positive_int,
        default=None,
        metavar="N",
        help="processes encoding images (default: one per usable CPU; 1 = no subprocesses)",
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

    out = args.output or (os.path.splitext(args.input)[0] + ".optimized.pdf")

    if args.srgb_only:
        return _declare_srgb_only(args, out)

    profile = PROFILES[args.profile or "web"]()
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

    try:
        result = optimize_document(
            args.input, out, profile, verbose=args.verbose, workers=args.jobs
        )
    except Exception as exc:  # pragma: no cover
        print(f"pdf-diet: failed to optimize {args.input}: {exc}", file=sys.stderr)
        return 1

    _report(result)
    return 0


#: Options that only mean something when compressing.
_COMPRESSION_OPTIONS = {
    "profile": "--profile",
    "quality": "--quality",
    "dpi": "--dpi",
    "no_crop": "--no-crop",
    "progressive": "--progressive",
    "jobs": "--jobs",
}


def _declare_srgb_only(args: argparse.Namespace, out: str) -> int:
    # Refused rather than ignored, so a caller cannot believe it compressed.
    given = [
        flag
        for dest, flag in _COMPRESSION_OPTIONS.items()
        if getattr(args, dest) not in (None, False)
    ]
    if given:
        print(f"pdf-diet: --srgb-only does not compress; drop {', '.join(given)}", file=sys.stderr)
        return 2

    try:
        result = declare_srgb(args.input, out, verbose=args.verbose)
    except Exception as exc:  # pragma: no cover
        print(f"pdf-diet: failed to declare sRGB for {args.input}: {exc}", file=sys.stderr)
        return 1

    _report(result)
    return 0


def _report(result) -> None:
    print(result)
    if result.srgb_skipped:
        print(f"pdf-diet: sRGB not declared: {result.srgb_skipped}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
