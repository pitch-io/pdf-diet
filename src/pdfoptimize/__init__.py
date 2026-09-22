# Copyright 2026 Pitch Software GmbH
# SPDX-License-Identifier: Apache-2.0
"""pdfoptimize -- PDF compression on a permissively licensed stack.

An open-source implementation of the Pdftools SDK (3-Heights)
``Optimizer.optimizeDocument`` call, covering the Web and MinimalFileSize
profiles. Built on pikepdf (MPL-2.0), Pillow (MIT-CMU) and qpdf (Apache-2.0):
no Ghostscript, no MuPDF, nothing AGPL.

    >>> import pdfoptimize
    >>> result = pdfoptimize.optimize_document("in.pdf", "out.pdf",
    ...                                        pdfoptimize.MinimalFileSize())
    >>> print(result)                                    # doctest: +SKIP
    in.pdf: 26.41 MB -> out.pdf: 0.89 MB (29.7x smaller, 96.6% saved)
"""

from __future__ import annotations

from .optimizer import ImageResult, Optimizer, Result, optimize_document
from .profiles import PROFILES, MinimalFileSize, Profile, RemovalOptions, Web

__version__ = "1.0.0"

__all__ = [
    "Optimizer",
    "optimize_document",
    "Result",
    "ImageResult",
    "Profile",
    "Web",
    "MinimalFileSize",
    "RemovalOptions",
    "PROFILES",
    "__version__",
]
