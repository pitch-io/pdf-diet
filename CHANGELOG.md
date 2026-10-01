# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/1.1.0/);
versioning follows [Semantic Versioning](https://semver.org/).

## [1.2.0] - 2026-10-01

### Added
- Images are encoded in parallel worker processes, one per usable CPU
  (cgroup CPU quota included, so AWS Lambda gets what its memory setting
  allows). `-j/--jobs` and `workers=` override it; `1` runs in one process.
  The pool uses only `Process` and `Pipe`, which work on Lambda. Output does
  not depend on the worker count.
- `-v` ends with a table of time per phase, and logs every image with the
  time spent on it, including images left unchanged.
- `Result.timings`, `Result.image_timings`, `Result.workers` and
  `ImageResult.seconds`.

### Changed
- About 8.5x faster over the 46-deck benchmark corpus on a 16-core machine
  (1,801 s to 212 s); output no larger for any deck.
- The JPEG 2000 quality search starts near the image's quality floor instead
  of bisecting the whole range: about 2.5 encodes per image instead of 6.
  Outputs can differ from 1.1.0 by a few hundred bytes where quality is not
  monotonic in the setting.
- The lossless candidate is skipped when a quick deflate shows it cannot
  beat the best lossy one.
- Identical images (same samples, mask, crop and size) are encoded once.

## [1.1.0] - 2026-09-29

### Added
- `--srgb-only` and `declare_srgb()`: declare a PDF's colours as sRGB without
  compressing it. Lossless; for exports that must keep full resolution.

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
- CLI (`pdf-diet`) and library API.

### Fixed during development
- Images carrying a soft mask decoded as RGBA and were written as four-channel
  streams declared `/DeviceRGB`, rendering as grey in some viewers.
- Selecting encodings purely by output size chose JPEG 2000 at rates that
  visibly banded gradients.
- `_digest` raised on native Python integer dictionary values, silently
  disabling deduplication for every image XObject.
