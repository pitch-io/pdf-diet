# Working on pdf-diet

Orientation for agents and humans. Read the **Hazards** section before
touching `images.py` or `document.py` — both contain non-obvious code that
looks wrong until you know what it is defending against.

## What this is

A reimplementation of the Pdftools SDK (3-Heights) `Optimizer.optimizeDocument`
call, covering the **Web** and **MinimalFileSize** profiles. The constraint
that shaped every dependency choice: it must be usable in a commercial SaaS,
so nothing AGPL. That rules out Ghostscript, MuPDF, PyMuPDF and pdfsizeopt,
which is why this exists at all rather than shelling out to `gs`.

## Commands

Managed with [uv](https://docs.astral.sh/uv/); `uv.lock` is committed.

```bash
uv sync                              # create .venv from the lockfile
uv run pytest                        # full suite, ~30s, no external fixtures
uv run pytest tests/test_regression.py   # the bugs that shipped once
uv run ruff check src tests tools     # lint
uv run ruff format src tests tools    # format (CI checks this)
uv run pdf-diet in.pdf out.pdf -p minimal -v
```

Dev dependencies are a PEP 735 `[dependency-groups]` entry, not an extra, so
with plain pip it is `pip install -e . --group dev` (pip 25.1+), never
`.[dev]`.

There is no network access needed and no test fixture larger than a few
hundred KB; every test builds the PDF it needs in `tests/conftest.py`.

### Benchmarking against another optimizer

```bash
tools/benchmark.py examples/pdf-compare --render -n 5    # quick look
tools/benchmark.py examples/pdf-compare --render --csv results.csv
```

Expects triples named `<name>.uncompressed.pdf`, `<name>.new.pdf` (reference
at quality 0.6) and `<name>.new-0.8.pdf` (at 0.8). Reports size and, with
`--render`, mean PSNR of each output against the original.

Two things to keep in mind when reading its output:

- **The quality scales are not the same number line.** Ours sets a
  distortion budget; the reference tool's 0.6 means whatever its SDK means.
  Compare size/fidelity *pairs*, not settings.
- **PSNR is an estimate from sampled pages; size is exact.** A 38-page deck
  measured 99.00 dB over 3 sampled pages and 73.55 dB over 30, because the
  small sample missed every page whose images had been touched. Check
  `Result.images` or raise `--sample-pages` before concluding anything about
  an individual deck.
- **A PSNR near 99 dB over a full sample means we declined to compress.**
  That is a real state — `encode_candidates` can return only a lossless
  option, which the caller rejects as no improvement — but confirm the
  sample is large enough first.

`--render` shells out to `pdftoppm` (poppler, GPL). It is a measurement
tool run as a separate process, never a dependency of the package, and
nothing it touches is distributed — the licensing story in README stands.

## Architecture

```
src/pdfdiet/
  profiles.py    Profile dataclasses. Defaults come from the published
                 Pdftools API reference; do not "tidy" the numbers.
  geometry.py    Content-stream walking. Finds where each image lands
                 (CTM) and how much of it is visible (clip), then decides
                 crop + per-axis target size.  -> plan_image()
  images.py      Decode, measure, re-encode.  -> encode_candidates()
  document.py    Placement rewriting, object pruning, deduplication.
  srgb.py        Opt-in sRGB declaration: output intent + /DefaultRGB.
                 Python twin of pitch-app's backend.integration.pdf-srgb.
  optimizer.py   Orchestration.  -> Optimizer.optimize_document()
  cli.py         Argument parsing.
```

Data flow for one image:

```
scan_placements()  -> every (ctm, clip) an image is drawn under
plan_image()       -> crop rect + final pixel size
load_pil()         -> base samples, masks detached
crop / resize
encode_candidates()-> all encodings meeting the quality floor
min(by size)       -> chosen
set_image()        -> written back in place
rewrite_placements-> corrective `cm` if it was cropped
```

## Invariants

These are load-bearing. Tests enforce all of them.

1. **A stream's component count must match its declared `/ColorSpace`.**
   3 for `/DeviceRGB`, 1 for `/DeviceGray`. `_shape_ok` re-decodes every
   candidate to check.
2. **Every lossy candidate clears `psnr_floor()`, or is the best that codec
   can manage.** Never select on size alone. The second case exists because
   the floor is capped (see `PSNR_FLOOR_CEILING`) and some images cannot
   reach it at any setting; returning nothing there left them untouched.
3. **Cropping an image requires rewriting its placement matrix.** Crop and
   `adjust_matrix` must be applied together or the page shifts.
4. **Downsampling is decided per axis.** A stretched image has different
   effective DPI horizontally and vertically.
5. **An image drawn more than once gets the worst-case (highest) DPI** and
   the union of visible regions.
6. **Image XObjects are modified in place**, so existing references stay
   valid. Never replace the object.
7. **The sRGB declaration runs after `prune` and before `dedupe`.** After, so
   `remove_output_intents` cannot strip the intent just added; before, so the
   profile merges with an identical copy already in the file. It never
   overwrites a non-sRGB output intent, and never fails the run.

## Hazards

Three bugs reached rendered pages. Each one passed a naive check first.

### `as_pil_image()` composites masks into alpha

`pikepdf.PdfImage.as_pil_image()` merges `/SMask` and `/Mask` into an alpha
channel and returns **RGBA**. Transparency is handled separately here, so
always decode via `images.load_pil()`, which detaches those entries first and
restores them after.

What went wrong: RGBA reached the encoder. JPEG refuses RGBA so that
candidate was silently skipped; JPEG 2000 accepted it and wrote four
channels; the dictionary still said `/DeviceRGB`. Poppler tolerated it, so
render-based PSNR checks passed at 44 dB. Other viewers showed grey.

### "Keep the smallest output" bands gradients

The SDK documents its BALANCED strategy as keeping the smallest output.
Implemented literally, that picks JPEG 2000 at a rate which wins on bytes by
a mile and mottles every gradient. A full-page gradient went to 1.3 KB at
43 dB PSNR — a number that looks fine and is not.

**How much distortion an image can absorb depends on the image.** A
photograph hides quantisation error in texture and looks fine at ~35 dB; a
flat gradient shows every wavelet ripple and needs north of 50 dB. Hence
`psnr_floor()` scales with measured local `detail()`. Do not replace it with
a constant; both a photo-tuned and a gradient-tuned constant were tried and
each is badly wrong for the other case.

A low-frequency (post-blur) error metric was also tried as a banding
detector and rejected: it separates good from bad *within* one image but not
*across* images — a mottled gradient scored 1.58 and an acceptable
photograph 1.75.

**The floor is capped at `PSNR_FLOOR_CEILING` (48 dB).** Uncapped, quality
0.8 demanded ~53.6 dB on smooth content; nothing satisfied that cheaply, so
decks made mostly of flat graphics came out *larger* than the reference
optimizer's output or were skipped entirely. Swept over the corpus at
48/50/52/54/uncapped, 48 is where the size regression disappears — a
gradient-heavy deck went from +49% to −31% against the reference with no
banding on visual inspection, and photo-heavy decks are entirely insensitive
to the cap because their floors sit far below it.

### Pillow's JPEG optimiser silently caps quality

`im.save(..., format="JPEG", optimize=True)` buffers the whole scan and
raises `OSError("broken data stream when writing image file")` when it does
not fit — which happens on noisy images above roughly quality 90. The
exception surfaced inside `_search_codec`'s guard and looked exactly like
"this codec is a poor fit", so the binary search never saw the top of its own
range. `_encode_jpeg` now raises `ImageFile.MAXBLOCK` and retries without
`optimize`. If you touch JPEG encoding, keep the retry.

### pikepdf returns native Python scalars

`obj["/Width"]` is a plain `int`, not a `pikepdf.Object`, so it has no
`.is_indirect`. Use `document._is_ref()`. Getting this wrong threw inside a
broad `except` and silently disabled deduplication for every image XObject
(which all carry integer `/Width` and `/Height`) for months without any test
noticing.

Related: `_repoint` guards **each entry** separately rather than wrapping
the whole loop, so one awkward value cannot abandon a container half-done.

## Testing notes

- pikepdf objects die with the `Pdf` that owns them. Copy values out inside
  the `with` block; returning objects gives you `object of type destroyed`.
- Do not assert on absolute byte sizes. A synthetic perfect gradient
  legitimately compresses to ~1 KB while clearing its floor; a real one does
  not. Assert on the invariant (quality floor, channel count), not a
  threshold someone picked.
- Parse the output, never search its bytes. pdf-diet writes object streams,
  so names like `/DefaultRGB` sit inside compressed data and a byte search
  finds nothing, passing for the wrong reason.
- qpdf pushes inherited page `/Resources` onto each page when it writes, so a
  fixture that relies on inheritance must be built in memory, not saved.
- Rendered-page PSNR is a weak check. It passed for both rendering bugs
  above. Prefer per-image structural assertions.

## Known limitations

- Rotated/skewed images are downsampled but not cropped.
- Clip paths are tracked as axis-aligned bounding boxes — conservative, so
  visible pixels are never cropped away, but slack remains on non-rectangular
  clips.
- Inline images (`BI`/`ID`/`EI`) are left alone.
- No MRC profile, no font subsetting.
- `set_image` writes recompressed images as `/DeviceRGB` even when they were
  `/ICCBased`, dropping the image's own profile. With `declare_srgb` those
  images are then read as sRGB. Chrome exports carry no such images.
- The sRGB declaration does not reach annotation appearance streams or a
  page's transparency-group `/CS`. Soft-mask groups (`/ExtGState` →
  `/SMask /G`) are skipped **on purpose**: they produce opacity, not colour.
  A real deck showing "2/4 forms tagged" is this, not a bug —
  `test_soft_mask_groups_are_left_alone` pins it.
- JPEG 2000 quality comes from OpenJPEG, which is weaker than the
  Kakadu-class encoder the commercial tool uses. **mozjpeg** (BSD-3-Clause)
  is the obvious next lever and is not wired up.

## Conventions

- Apache-2.0. Every source file carries the SPDX header.
- Broad `except Exception` around PDF object access is deliberate: the
  underlying C++ throws a wide and poorly documented variety. Keep the scope
  tight — guard the individual access, not a whole loop.
- Comments explain *why*, especially where code defends against something.
  Do not delete a comment that names a bug without checking the test that
  pins it.
