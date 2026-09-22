# Copyright 2026 Pitch Software GmbH
# SPDX-License-Identifier: Apache-2.0
"""Entry point for ``python -m pdfdiet``."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
