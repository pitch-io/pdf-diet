# pdf-diet

`pdf-diet` makes PDFs smaller by cropping and resampling images, choosing a
suitable image codec, and removing objects that are no longer needed. It is an
open-source implementation of the Web and MinimalFileSize profiles from the
Pdftools SDK (formerly 3-Heights).

The project uses [pikepdf](https://github.com/pikepdf/pikepdf),
[Pillow](https://github.com/python-pillow/Pillow), and
[qpdf](https://github.com/qpdf/qpdf). It does not depend on Ghostscript or
MuPDF, so the full stack is available under permissive or weak-copyleft
licences. See [Licensing](#licensing) for the details.

On the slide deck used during development, the `minimal` profile reduced a
26.4 MB file to 0.88 MB. That was a little smaller than the result from the
commercial tool's Web profile, while retaining slightly more image detail.
Your results will depend on what is in the PDF; documents dominated by large,
lossless images tend to benefit most.

## Installation

`pdf-diet` requires Python 3.10 or later. Once the package is published, it
will be installable with pip:

```bash
pip install pdf-diet
```

Or with uv, without installing it permanently:

```bash
uvx pdf-diet deck.pdf
```

The command-line program is called `pdf-diet`; the Python package is
`pdfdiet`.

## Command-line usage

The simplest invocation uses the Web profile and writes
`deck.optimized.pdf`:

```bash
pdf-diet deck.pdf
```

To choose the smaller profile or provide an explicit output path:

```bash
pdf-diet deck.pdf small.pdf --profile minimal
```

You can also override the profile's quality and resolution settings:

```bash
pdf-diet deck.pdf small.pdf --quality 0.6 --dpi 96 --verbose
```

The complete command is:

```text
pdf-diet [-p PROFILE] [-q 0..1] [-d DPI] [--no-crop] [--progressive] [--srgb]
         [-v] input [output]
```

| Option | Default | Description |
|---|---|---|
| `input` | required | PDF to compress. The input file is not modified. |
| `output` | `<input>.optimized.pdf` | Output path. An existing file at this path is overwritten. |
| `-p`, `--profile` | `web` | Either `web` or `minimal`. |
| `-q`, `--quality` | profile setting | Quality from 0 to 1. Lower values allow more image distortion in exchange for smaller files. |
| `-d`, `--dpi` | profile setting | Target resolution for downsampling. |
| `--no-crop` | off | Do not crop images to their visible area. |
| `--progressive` | off | Write progressive JPEGs. This usually saves a few percent, but is not covered by PDF's baseline-JPEG wording. |
| `--srgb` | off | Declare the document's colours as sRGB. See [Colour](#colour). |
| `-v`, `--verbose` | off | Report what happened to each image. |
| `--version` | — | Print the installed version. |

The process exits with status 0 on success, 1 if optimization fails, and 2
for invalid arguments or a missing input file.

### Quality and resolution

`--quality` is not passed straight through as a JPEG quality value. It sets a
fidelity target, and `pdf-diet` searches for the smallest encoding that meets
that target for each image. The default of 0.8 is deliberately conservative.
Values around 0.5–0.6 are visibly lossy, though they may be acceptable for a
file intended only for quick sharing.

`--dpi` controls image dimensions rather than compression artifacts. Images
are downsampled only when their effective resolution is more than 1.4 times
the target, so a 150 DPI profile starts resampling above 210 DPI. For PDFs
that will only be viewed on screen, 96 DPI is often a useful starting point.

If a PDF does not shrink as much as expected, try `--verbose`. The report
shows the original and output dimensions, codec, and number of bytes saved
for each image.

### Profiles

| Setting | `web` | `minimal` |
|---|---:|---:|
| Target resolution | 150 DPI | 130 DPI |
| Downsample above | 210 DPI | 182 DPI |
| Quality | 0.80 | 0.75 |
| Remove output intents | no | yes |

Both profiles crop images to their visible area and reduce colour complexity
where possible. The `minimal` profile is intended for cases where file size
matters more than preserving every bit of image fidelity.

### Colour

Chrome's print-to-PDF writes every colour as bare `DeviceRGB` and embeds no
colour profile. Viewers that do not assume sRGB then show the export
oversaturated on wide-gamut displays. `--srgb` (or `declare_srgb=True` on a
profile, or `pdfdiet.tag_srgb(pdf)` on an open `pikepdf.Pdf`) embeds one sRGB
IEC61966-2.1 profile and points an `/OutputIntents` entry and a `/DefaultRGB`
colour space on every page, form XObject and tiling pattern at it. No pixels
change.

It is off by default because the Pdftools SDK does not do it. It is applied
after the profile's removals, so it holds under `minimal` too. A document
that already declares a different output intent, such as a CMYK PDF/X
condition, is left undeclared rather than overwritten; the result's
`srgb_skipped` says why.

## Python API

```python
import pdfdiet

result = pdfdiet.optimize_document(
    "deck.pdf",
    "small.pdf",
    pdfdiet.MinimalFileSize(),
)

print(result)
print(result.ratio)

for image in result.images:
    print(image.source_px, "->", image.result_px, image.filter)
```

The returned `Result` contains `before_bytes`, `after_bytes`, `ratio`,
`saved_fraction`, `merged_objects`, and a list of `ImageResult` values. Each
image result records its source and output dimensions, byte counts, chosen
filter, and whether it was cropped.

Profiles are dataclasses, so settings can be changed directly:

```python
profile = pdfdiet.Web()
profile.resolution_dpi = 96
profile.threshold_ratio = 1.0
profile.compression_quality = 0.6
profile.crop_to_visible = False
profile.reduce_color_complexity = False
profile.progressive_jpeg = True

# Keep the document's accessibility structure.
profile.removal.remove_structure_tree = False
```

Set `resolution_dpi` to `None` to disable downsampling. Images may still be
recompressed. `RemovalOptions` also controls removal of alternate images,
article threads, metadata, output intents, piece information, structure trees,
and thumbnails. The defaults match the corresponding Pdftools profiles.

Both stock profiles remove the structure tree to save space. If accessible
tagging matters for your document, set `remove_structure_tree` to `False` as
shown above.

## How it works

An image inside a PDF does not have a resolution on its own; its effective DPI
depends on how large it is drawn on the page. It may also be partly hidden by
a clipping path. `pdf-diet` therefore does a little more than simply extract
and recompress every image:

1. It walks the page content streams, tracking transformations and clipping
   paths to find where each image is drawn.
2. It crops images to the portion that is actually visible and adjusts their
   placement matrices accordingly.
3. It calculates effective horizontal and vertical DPI and downsamples axes
   that exceed the profile threshold.
4. It tries suitable encodings and keeps the smallest one that clears the
   image's quality target.
5. It removes redundant soft masks, prunes unused objects, and deduplicates
   identical streams.

The quality target adapts to the image. Photographs can hide compression
noise reasonably well, while gradients and flat artwork need a higher signal
quality to avoid banding. A single fixed PSNR threshold worked poorly for
both, so the encoder takes local image detail into account.

## Benchmark notes

The main reference file is an 11-page presentation export containing eight
large, lossless RGB images. It is a customer document and cannot be included
in the repository. These are the measurements from the development run:

| Output | Size | Compression ratio | Image pages | Text pages |
|---|---:|---:|---:|---:|
| Pdftools Web | 973,186 bytes | 27.1× | 39.46 dB | 56.43 dB |
| pdf-diet Web | 1,369,277 bytes | 19.3× | 41.84 dB | 99.00 dB |
| pdf-diet MinimalFileSize | 879,517 bytes | 30.0× | 40.90 dB | 99.00 dB |

Fidelity was measured as PSNR against a 50 DPI render of the original. A
reported value of 99 dB means the render was identical. Pages without images
are left unchanged.

The crop and per-axis DPI calculations produced the same image dimensions as
Pdftools for all eight images in this file. This is useful regression data,
but it should not be read as a general promise that the two implementations
will make identical choices for every PDF.

## Limitations

- Rotated and skewed images are downsampled but are not cropped.
- Clip paths are treated as axis-aligned bounding boxes. This is conservative:
  it may keep a little extra data, but should not remove visible pixels.
- Inline images (`BI`/`ID`/`EI`) are not changed.
- There is no MRC profile or font subsetting.
- JPEG 2000 encoding is provided by OpenJPEG. It does not compress photographs
  as efficiently as the commercial encoder used by Pdftools in our tests.
- Progressive JPEGs work in modern PDF viewers, but are opt-in because the PDF
  specification describes `DCTDecode` in terms of baseline JPEG.

Cropping involves rewriting image placement matrices. It is covered by the
test suite, but PDFs are varied and occasionally surprising. If you encounter
a bad crop, `--no-crop` provides a quick workaround; please also open an issue
with a reproducible example if you can share one.

## Licensing

`pdf-diet` is licensed under the [Apache License 2.0](LICENSE). The main
runtime components use the following licences:

| Component | Licence |
|---|---|
| pikepdf | MPL-2.0 |
| qpdf | Apache-2.0 |
| Pillow | MIT-CMU |
| OpenJPEG (through Pillow) | BSD-2-Clause |
| libjpeg-turbo (through Pillow) | BSD-3-Clause / IJG |
| sRGB ICC profile (vendored, ArgyllCMS) | Public domain |

Ghostscript, MuPDF, PyMuPDF, and pdfsizeopt are deliberately not used because
their AGPL licensing is not a good fit for this project, particularly for
server-side use. This is an engineering constraint, not legal advice; check
the licence terms against your own requirements.

See [NOTICE](NOTICE) for the full attribution notice.

## Development

The repository uses [uv](https://docs.astral.sh/uv/) and includes a lockfile.

```bash
uv sync
uv run pytest
uv run ruff check src tests tools
uv run ruff format src tests tools
uv build
```

Without uv, pip 25.1 or later can install the PEP 735 development dependency
group:

```bash
pip install -e . --group dev
```

The benchmark helper compares optimizer output across a directory of test
files:

```bash
tools/benchmark.py examples/pdf-compare --render --csv results.csv
```

It expects triples named `<name>.uncompressed.pdf`, `<name>.new.pdf`, and
`<name>.new-0.8.pdf`. The optional render comparison requires `pdftoppm` from
Poppler; it is used only for measurement and is not a package dependency.

Before changing codec selection or stream deduplication, read
[CLAUDE.md](CLAUDE.md). It describes the architecture, important invariants,
and a few non-obvious failure modes found during development.
