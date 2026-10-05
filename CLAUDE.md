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
uv run pdf-diet in.pdf out.pdf -p minimal -v   # -v ends with a timing table
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
  parallel.py    Lambda-safe process pool (Process + Pipe only).
  srgb.py        Opt-in sRGB declaration: output intent + /DefaultRGB.
                 Python twin of pitch-app's backend.integration.pdf-srgb.
  optimizer.py   Orchestration.  -> Optimizer.optimize_document()
                 Parent decodes and writes back; _process() runs in workers.
  cli.py         Argument parsing.
```

Data flow for one image:

```
scan_placements()  -> every (ctm, clip) an image is drawn under
plan_image()       -> crop rect + final pixel size
load_pil()         -> base samples, masks detached          [parent]
crop / resize                                               [worker]
encode_candidates()-> all encodings meeting the quality floor  [worker]
min(by size)       -> chosen                                [worker]
set_image()        -> written back in place                 [parent]
rewrite_placements-> corrective `cm` if it was cropped
```

pikepdf objects belong to their `Pdf` and cannot cross a process boundary,
so workers only ever see Pillow images and plain values.

Images identical in samples, mask, crop and size are encoded once
(`_content_key`); copies get the first one's outcome. Decks repeat images as
separate XObjects that `dedupe` only merges at the end — 19% of all encoded
pixels in the benchmark corpus.

## Invariants

These are load-bearing. Tests enforce all of them.

1. **A stream's component count must match its declared `/ColorSpace`.**
   3 for `/DeviceRGB`, 1 for `/DeviceGray`. `_shape_ok` re-decodes every
   candidate to check.
2. **Every lossy candidate clears `psnr_floor()`, or is the best that codec
   can manage** — over the whole image *and* over `content_mask()`. Never
   select on size alone. The second case exists because the floor is capped
   (see `PSNR_FLOOR_CEILING`) and some images cannot reach it at any
   setting; returning nothing there left them untouched.
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
8. **`declare_srgb` (`--srgb-only`) is lossless.** It opens, tags and saves;
   no image, prune or dedupe step. Anything that re-encodes belongs in
   `optimize_document`.
9. **Output does not depend on the worker count.** Results arrive in
   completion order; `result.images` is re-sorted into document order, and
   any search heuristic must be fixed, not learned during a run.

## Hazards

Four bugs reached rendered pages. Each one passed a naive check first.

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

### Flat backgrounds hide damage from PSNR

Whole-image PSNR averages error over every pixel, and an exactly flat region
comes back from any codec almost perfect. On a pale circle on white, 8 grey
levels off its background, JPEG 2000 produced 1.1 KB that scored 48.4 dB
against a 48 dB floor while edge pixels were off by up to 12. The circle
rendered visibly blurred and blotchy next to the reference tool's.

`content_mask()` marks pixels within 8 px of any change in the source;
candidates must clear the floor over it too (40 dB for that encoding), and
`detail()` is measured over the same pixels so a photograph on white is not
mistaken for a flat graphic. The mask comes from the *source*, never the
error: deciding by where the output is wrong lets a codec that smears error
everywhere dilute itself again. Worst-tile PSNR was tried and does not
separate good from bad — it ignores how much contrast the tile had.

### Pillow's JPEG optimiser silently caps quality

`im.save(..., format="JPEG", optimize=True)` buffers the whole scan and
raises `OSError("broken data stream when writing image file")` when it does
not fit — which happens on noisy images above roughly quality 90. The
exception surfaced inside `_search_codec`'s guard and looked exactly like
"this codec is a poor fit", so the binary search never saw the top of its own
range. `_encode_jpeg` now raises `ImageFile.MAXBLOCK` and retries without
`optimize`. If you touch JPEG encoding, keep the retry.

### Threads do not parallelise encoding; `multiprocessing.Pool` fails on Lambda

Pillow holds the GIL through JPEG and JPEG 2000 encode and decode. A thread
pool measured exactly 1.0x, so encoding runs in processes. But
`multiprocessing.Pool` and `ProcessPoolExecutor` need POSIX semaphores in
`/dev/shm`, which AWS Lambda lacks: they fail at construction with
`OSError: [Errno 38]`. `parallel.Pool` is built from `Process` and `Pipe`
alone. Do not "simplify" it to the standard pools.

It keeps one job per worker on purpose: a second job sent to a busy worker
fills the pipe buffer and blocks, while the worker blocks sending its result
back. It is started before the PDF is opened so forked workers inherit
nothing large.

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
- Compare outputs by parsing them, not by bytes: qpdf's trailer `/ID`
  includes the time, so two identical runs differ. It is repeated inside the
  `/XRef` stream, so skip that stream too.
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
- Pixels under a fully transparent `/SMask` still count towards an image's
  quality score. Harmless, but slightly stricter than it needs to be.
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
