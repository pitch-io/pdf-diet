# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/1.1.0/);
versioning follows [Semantic Versioning](https://semver.org/).

## [1.0.0] - 2026-09-22

First release. Implements the Web and MinimalFileSize profiles of the
Pdftools SDK `Optimizer.optimizeDocument` call.

### Added
- Content-stream walker tracking CTM and clipping path, reproducing
  pdf-tools' output dimensions exactly on the reference document.
- Per-axis DPI downsampling with the SDK's 1.4x threshold.
- Cropping of images to their visible region, with placement-matrix rewriting.
- Detail-adaptive quality floor with binary-searched codec selection.
- Soft-mask handling: opaque masks dropped, binary masks converted to 1-bit
  stencils.
- Iterative deduplication of byte-identical streams.
- CLI (`pdfoptimize`) and library API.

### Fixed during development
- Images carrying a soft mask decoded as RGBA and were written as four-channel
  streams declared `/DeviceRGB`, rendering as grey in some viewers.
- Selecting encodings purely by output size chose JPEG 2000 at rates that
  visibly banded gradients.
- `_digest` raised on native Python integer dictionary values, silently
  disabling deduplication for every image XObject.
