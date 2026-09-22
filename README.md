# pdfoptimize

PDF compression on a permissively licensed stack — an open-source
implementation of the Pdftools SDK (3-Heights) `Optimizer.optimizeDocument`
call, covering the **Web** and **MinimalFileSize** profiles.

Built on [pikepdf](https://github.com/pikepdf/pikepdf) (MPL-2.0),
[Pillow](https://github.com/python-pillow/Pillow) (MIT-CMU) and
[qpdf](https://github.com/qpdf/qpdf) (Apache-2.0).
**No Ghostscript, no MuPDF, nothing AGPL** — see [Licensing](#licensing).

On the reference deck (an 11-page slide export, 26.4 MB) it produces a file
**9.6% smaller than the commercial tool at measurably higher fidelity**.

## Install

```bash
pip install pdfoptimize            # once published
pip install -e ".[dev]"            # from a checkout, with tests and lint
```

Requires Python 3.10+.

## Use

```bash
pdfoptimize deck.pdf                          # -> deck.optimized.pdf, Web profile
pdfoptimize deck.pdf small.pdf -p minimal     # MinimalFileSize
pdfoptimize deck.pdf small.pdf -q 0.6 -v      # tune quality, report per image
```

### Command line options

```
pdfoptimize [-p PROFILE] [-q 0..1] [-d DPI] [--no-crop] [--progressive] [-v]
            input [output]
```

| Option | Default | What it does |
|---|---|---|
| `input` | — | The PDF to compress. Left untouched. |
| `output` | `<input>.optimized.pdf` | Where to write. Overwritten if it exists. |
| `-p`, `--profile` | `web` | `web` or `minimal`. See the profile table below. |
| `-q`, `--quality` | profile's | Fidelity, 0–1. Raises or lowers the quality floor every image must meet. |
| `-d`, `--dpi` | profile's | Target resolution for downsampling. Images stay untouched until they exceed 1.4× this. |
| `--no-crop` | off | Leave clipped images at full size instead of cropping to the visible region. |
| `--progressive` | off | Progressive JPEG: ~4% smaller, pixel-identical, but outside PDF's baseline-JPEG wording. |
| `-v`, `--verbose` | off | Print a line per image plus the deduplication count. |
| `--version` | — | Print the version and exit. |

Exit codes: `0` success, `1` optimization failed, `2` bad arguments or missing
input.

**`--quality`** is the main dial. It does not map to a JPEG quality number —
it sets how much distortion each image may carry, and the encoder searches
for the cheapest settings that stay within it. Lower values give smaller
files; 0.5–0.6 is noticeably lossy but usually still presentable, and the
default 0.8 is conservative. Because the floor adapts per image, lowering
this affects photographs far more than flat graphics, which is usually what
you want.

**`--dpi`** controls resolution rather than fidelity. Reach for it when the
output will be viewed at a known size — 96 for screen-only sharing, 72 for
thumbnails. It is often a bigger win than `--quality` on documents built from
oversized source images, and it costs nothing on documents that were already
sized correctly, since anything under the threshold is left alone.

**`--no-crop`** exists as an escape hatch. Cropping rewrites placement
matrices, and while that is well covered by tests, this turns the whole
mechanism off if you hit a document it mishandles. It costs size on
documents with clipped images (slide exports especially) and nothing on
documents without them.

**`-v`** is the first thing to try when a file does not shrink as expected.
It shows each image's source and output dimensions, the codec chosen and the
bytes saved, so you can see whether the limit was resolution, quality, or an
image that was already well compressed.

### Profiles

| | `web` | `minimal` |
|---|---|---|
| Target resolution | 150 DPI | 130 DPI |
| Downsample threshold | 210 DPI | 182 DPI |
| Quality | 0.80 | 0.75 |
| Removes output intents | no | yes |

Both crop to the visible region and reduce colour complexity. `minimal` is
the better default for sharing: on the reference deck it is 36% smaller than
`web` with no visible difference.

### Python API

```python
import pdfoptimize

result = pdfoptimize.optimize_document("deck.pdf", "small.pdf",
                                       pdfoptimize.MinimalFileSize())
print(result)          # deck.pdf: 26.41 MB -> small.pdf: 0.88 MB (30.0x smaller)
print(result.ratio)    # 30.03
for img in result.images:
    print(img.source_px, "->", img.result_px, img.filter)
```

`Result` carries `before_bytes`, `after_bytes`, `ratio`, `saved_fraction`,
`merged_objects` and a list of `ImageResult` (`source_px`, `result_px`,
`before_bytes`, `after_bytes`, `filter`, `cropped`).

Profiles are plain dataclasses, so every field is settable — including
several with no command-line equivalent:

```python
profile = pdfoptimize.Web()
profile.resolution_dpi = 96          # or None to disable downsampling entirely
profile.threshold_ratio = 1.0        # downsample as soon as over target
profile.compression_quality = 0.6
profile.crop_to_visible = False
profile.reduce_color_complexity = False   # keep RGB even where greyscale suffices
profile.progressive_jpeg = True
profile.removal.remove_structure_tree = False   # keep tagging for accessibility
```

| Field | Default (Web) | Notes |
|---|---|---|
| `resolution_dpi` | `150.0` | `None` disables downsampling; images are still recompressed. |
| `threshold_ratio` | `1.4` | Multiplier on the target giving the downsample threshold. The SDK's value; lowering it resamples more images, which always costs some sharpness. |
| `compression_quality` | `0.8` | 0–1, drives the per-image quality floor. |
| `reduce_color_complexity` | `True` | Collapse RGB to grey to bitonal where the pixels allow, and simplify soft masks. |
| `crop_to_visible` | `True` | Crop to the clipped region and rewrite placement matrices. |
| `progressive_jpeg` | `False` | See `--progressive`. |
| `removal` | `RemovalOptions()` | Which parts of the object graph to discard. |

`RemovalOptions` fields — `remove_alternate_images`, `remove_article_threads`,
`remove_metadata`, `remove_output_intents`, `remove_piece_info`,
`remove_structure_tree`, `remove_thumbnails` — mirror the SDK's, and the
defaults match its Web profile. Set `remove_structure_tree = False` if the
document's accessibility tagging matters; it is discarded by default because
both stock profiles prioritise size.

## What it does

1. Walks every content stream tracking the **CTM and clipping path**, to find
   where each image XObject actually lands on the page.
2. **Crops** each image to its visible (clipped) region, rewriting the
   placement matrix so the page renders identically.
3. Computes **effective DPI per axis** and downsamples any axis above the
   threshold (1.4× the target) to the target resolution.
4. Re-encodes, choosing the smallest encoding **that meets a per-image
   quality floor**.
5. Drops fully-opaque soft masks; converts binary soft masks to 1-bit stencils.
6. Prunes the object graph and **deduplicates byte-identical streams**,
   iteratively — collapsing forms makes their parents identical in turn.

Steps 1–3 are what separate this from "recompress every image": an image
XObject carries no resolution of its own, so both the DPI it is drawn at and
the portion of it that is visible have to be recovered from the content
stream.

## Measured against pdf-tools

Reference: an 11-page slide export, 26.41 MB, 8 images (all lossless Flate
RGB, 99.3% of the file), compared against the same file run through
pdf-tools' Web profile. The deck is a real customer document and is not in
the repository.

**Geometry is an exact match.** The crop and per-axis DPI logic reproduces
all 8 of pdf-tools' output dimensions exactly, including the non-obvious
cases:

| Source | Effective DPI | Rule applied | pdf-tools | ours |
|---|---|---|---|---|
| 1732×2309 | 208.9 | under 210 threshold — crop only | 921×1470 | **921×1470** |
| 2485×1204 | 249.3 | over threshold — to 150 DPI | 1339×725 | **1339×725** |
| 625×625 | 428.6 | over threshold, non-square clip | 219×153 | **219×153** |
| 2665×1498 | 133.2 | under threshold — untouched | 2665×1498 | **2665×1498** |

**Size and fidelity** (PSNR against the original render at 50 DPI, split by
page type):

| | bytes | ratio | image pages | text pages |
|---|---|---|---|---|
| pdf-tools Web | 973,186 | 27.1× | 39.46 dB | 56.43 dB |
| ours, Web | 1,369,277 | 19.3× | 41.84 dB | **99.00 dB** |
| **ours, MinimalFileSize** | **879,517** | **30.0×** | **40.90 dB** | **99.00 dB** |

99.00 dB means bit-identical. Pages without images are passed through
untouched, which pdf-tools does not do.

## Choosing a codec: why "smallest wins" is wrong

The obvious reading of the SDK's BALANCED strategy — encode several ways,
keep the smallest — is a trap, and it caused a shipped rendering bug.

JPEG 2000 at a low rate wins on bytes by a mile while looking visibly worse:

```
2665×1498 gradient:  JP2 44 dB   1,286 bytes   43.45 dB   <- mottled
                     JP2 56 dB  14,033 bytes   53.13 dB   <- clean
                     (pdf-tools spent 14,970 bytes)
```

A fixed PSNR threshold does not fix it either, because **how much fidelity an
image needs depends on the image**. A photograph hides quantisation error in
its texture and looks fine at ~35 dB; a flat gradient shows every wavelet
ripple as banding and needs north of 50 dB.

So the floor adapts to measured local detail, and each codec is
binary-searched for the cheapest setting clearing it. Measured detail ranges
from ~0.06 for a gradient to ~12 for a photograph, giving floors of ~53 dB
and ~36 dB respectively.

## Licensing

This project is licensed under the **Apache License 2.0** — see
[LICENSE](LICENSE) and [NOTICE](NOTICE).

Dependency licences, which drove the whole design:

| | Licence | |
|---|---|---|
| pikepdf | MPL-2.0 | file-level copyleft; fine for proprietary use |
| qpdf | Apache-2.0 | |
| Pillow | MIT-CMU | |
| OpenJPEG | BSD-2-Clause | JPEG 2000, via Pillow |
| libjpeg-turbo | BSD-3-Clause / IJG | JPEG, via Pillow |

**Deliberately excluded:** Ghostscript, MuPDF, PyMuPDF and pdfsizeopt are all
AGPL. Artifex states that the AGPL "prohibits the deployment of AGPL software
in a SaaS environment unless all of the software on the server is also
released under the AGPL", so using any of them server-side requires a
commercial licence. That constraint is the reason this project exists.

This is not legal advice; confirm against your own obligations.

## Limitations

- Rotated or skewed images are downsampled but not cropped.
- Clip paths are tracked as **axis-aligned bounding boxes**. Conservative —
  visible pixels are never cropped away — but slack remains on
  non-rectangular clips.
- Inline images (`BI`/`ID`/`EI`) are left alone.
- No MRC profile and no font subsetting.
- JPEG 2000 quality comes from OpenJPEG, which is weaker than the
  Kakadu-class encoder pdf-tools uses. **mozjpeg** (BSD-3-Clause) is the
  obvious next lever for closing the remaining gap on photographs; it is not
  wired up.
- `--progressive` gives ~4% smaller output with pixel-identical results, but
  progressive JPEG sits outside PDF's "baseline JPEG" wording for DCTDecode.
  Modern viewers handle it; it is opt-in rather than default.

## Development

```bash
pip install -e ".[dev]"
pytest                    # ~30s, no external fixtures
ruff check src tests
```

[CLAUDE.md](CLAUDE.md) documents the architecture, the load-bearing
invariants, and the three bugs that shipped — read it before changing codec
selection or the deduplication pass.
