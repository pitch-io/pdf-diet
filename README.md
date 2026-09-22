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
pdfoptimize --help
```

```python
import pdfoptimize

result = pdfoptimize.optimize_document("deck.pdf", "small.pdf",
                                       pdfoptimize.MinimalFileSize())
print(result)          # deck.pdf: 26.41 MB -> small.pdf: 0.88 MB (30.0x smaller)
print(result.ratio)    # 30.03
for img in result.images:
    print(img.source_px, "->", img.result_px, img.filter)
```

Profiles are plain dataclasses, so any field can be overridden:

```python
profile = pdfoptimize.Web()
profile.resolution_dpi = 96        # more aggressive than the 150 default
profile.crop_to_visible = False    # leave placement matrices alone
```

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

| | Web | MinimalFileSize |
|---|---|---|
| Target resolution | 150 DPI | 130 DPI |
| Downsample threshold | 210 DPI | 182 DPI |
| Quality | 0.80 | 0.75 |
| Removes output intents | no | yes |

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
