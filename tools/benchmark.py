#!/usr/bin/env python3
# Copyright 2026 Pitch Software GmbH
# SPDX-License-Identifier: Apache-2.0
"""Benchmark pdfoptimize against reference output from another optimizer.

Expects a directory of triples following the naming convention:

    <name>.uncompressed.pdf     the original
    <name>.new.pdf              reference output at quality 0.6
    <name>.new-0.8.pdf          reference output at quality 0.8

For each deck it runs pdfoptimize at the matching quality, then reports size
and — with ``--render`` — visual fidelity measured against the original.

Note that the two tools' quality scales are not necessarily the same number
line: ours sets a distortion budget, theirs is whatever their SDK means by
it. Compare the size/fidelity pairs, not the settings.

``--render`` shells out to ``pdftoppm`` (poppler). That is a GPL tool used
here as a separate process for measurement only; it is not a dependency of
the package and nothing it touches is distributed.

Usage:
    tools/benchmark.py examples/pdf-compare
    tools/benchmark.py examples/pdf-compare --render --limit 5
    tools/benchmark.py examples/pdf-compare --render --csv results.csv
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import math
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import pdfoptimize  # noqa: E402

SUFFIXES = {
    "uncompressed.pdf": "raw",
    "new-0.8.pdf": "0.8",
    "new.pdf": "0.6",
}


@dataclass
class Case:
    name: str
    raw: str
    references: dict[str, str] = field(default_factory=dict)


@dataclass
class Row:
    name: str
    quality: str
    pages: int
    raw_bytes: int
    ref_bytes: int
    our_bytes: int
    ref_psnr: float | None = None
    our_psnr: float | None = None
    seconds: float = 0.0
    error: str | None = None

    @property
    def size_delta(self) -> float:
        """Our size relative to the reference: negative means we are smaller."""
        if not self.ref_bytes:
            return 0.0
        return 100.0 * (self.our_bytes / self.ref_bytes - 1.0)

    @property
    def psnr_delta(self) -> float | None:
        if self.ref_psnr is None or self.our_psnr is None:
            return None
        return self.our_psnr - self.ref_psnr


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------

def discover(directory: str) -> list[Case]:
    cases: dict[str, Case] = {}
    for entry in sorted(os.listdir(directory)):
        if not entry.endswith(".pdf"):
            continue
        for suffix, kind in SUFFIXES.items():
            if entry.endswith("." + suffix):
                name = entry[: -len(suffix) - 1]
                case = cases.setdefault(name, Case(name=name, raw=""))
                path = os.path.join(directory, entry)
                if kind == "raw":
                    case.raw = path
                else:
                    case.references[kind] = path
                break
    return [c for c in cases.values() if c.raw and c.references]


# --------------------------------------------------------------------------
# Rendering and comparison
# --------------------------------------------------------------------------

def page_count(path: str) -> int:
    import pikepdf
    try:
        with pikepdf.open(path) as pdf:
            return len(pdf.pages)
    except Exception:
        return 0


def render(path: str, out_dir: str, pages: list[int], dpi: int) -> dict[int, str]:
    """Render selected pages to PNG. Returns page number -> file path."""
    result = {}
    for page in pages:
        prefix = os.path.join(out_dir, f"p{page}")
        cmd = ["pdftoppm", "-png", "-r", str(dpi),
               "-f", str(page), "-l", str(page), path, prefix]
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=300)
        except Exception:
            continue
        hits = [f for f in os.listdir(out_dir)
                if f.startswith(f"p{page}-") and f.endswith(".png")]
        if hits:
            result[page] = os.path.join(out_dir, hits[0])
    return result


def psnr_files(a: str, b: str) -> float | None:
    from PIL import Image, ImageChops
    try:
        ia = Image.open(a).convert("RGB")
        ib = Image.open(b).convert("RGB")
    except Exception:
        return None
    if ia.size != ib.size:
        ib = ib.resize(ia.size, Image.LANCZOS)
    hist = ImageChops.difference(ia, ib).histogram()
    total = count = 0
    for ch in range(3):
        base = ch * 256
        for v in range(256):
            c = hist[base + v]
            total += c * v * v
            count += c
    if not count:
        return None
    mse = total / count
    return 99.0 if mse == 0 else 10.0 * math.log10(255.0 * 255.0 / mse)


def compare_visually(raw: str, ref: str, ours: str, sample: int,
                     dpi: int) -> tuple[float | None, float | None]:
    """Mean PSNR of reference and our output against the original."""
    n = page_count(raw)
    if n == 0:
        return (None, None)
    if n <= sample:
        pages = list(range(1, n + 1))
    else:
        step = n / (sample + 1)
        pages = sorted({max(1, min(n, round(step * (i + 1)))) for i in range(sample)})

    with tempfile.TemporaryDirectory() as tmp:
        dirs = {}
        for tag, path in (("raw", raw), ("ref", ref), ("our", ours)):
            d = os.path.join(tmp, tag)
            os.makedirs(d)
            dirs[tag] = render(path, d, pages, dpi)

        ref_scores, our_scores = [], []
        for page in pages:
            base = dirs["raw"].get(page)
            if not base:
                continue
            if dirs["ref"].get(page):
                s = psnr_files(base, dirs["ref"][page])
                if s is not None:
                    ref_scores.append(s)
            if dirs["our"].get(page):
                s = psnr_files(base, dirs["our"][page])
                if s is not None:
                    our_scores.append(s)

    return (statistics.mean(ref_scores) if ref_scores else None,
            statistics.mean(our_scores) if our_scores else None)


# --------------------------------------------------------------------------
# Running one case
# --------------------------------------------------------------------------

def run_case(case: Case, quality: str, profile_name: str, out_dir: str,
             do_render: bool, sample: int, dpi: int) -> Row:
    ref = case.references[quality]
    our = os.path.join(out_dir, f"{case.name}.ours-{quality}.pdf")

    row = Row(name=case.name, quality=quality, pages=page_count(case.raw),
              raw_bytes=os.path.getsize(case.raw),
              ref_bytes=os.path.getsize(ref), our_bytes=0)

    profile = pdfoptimize.PROFILES[profile_name]()
    profile.compression_quality = float(quality)

    start = time.perf_counter()
    try:
        pdfoptimize.optimize_document(case.raw, our, profile)
    except Exception as exc:
        row.error = f"{type(exc).__name__}: {exc}"
        return row
    row.seconds = time.perf_counter() - start
    row.our_bytes = os.path.getsize(our)

    if do_render:
        try:
            row.ref_psnr, row.our_psnr = compare_visually(
                case.raw, ref, our, sample, dpi)
        except Exception as exc:
            row.error = f"render: {type(exc).__name__}: {exc}"

    return row


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

def mb(n: int) -> str:
    return f"{n / 1e6:.1f}"


def report(rows: list[Row], do_render: bool) -> None:
    ok = [r for r in rows if not r.error and r.our_bytes]
    bad = [r for r in rows if r.error]

    header = (f"{'deck':<42} {'q':>4} {'pg':>4} {'raw':>8} "
              f"{'theirs':>8} {'ours':>8} {'size':>8}")
    if do_render:
        header += f" {'theirs dB':>10} {'ours dB':>9} {'dB':>7}"
    print("\n" + header)
    print("-" * len(header))

    for r in sorted(rows, key=lambda r: (r.quality, r.name)):
        if r.error:
            print(f"{r.name[:42]:<42} {r.quality:>4} {'':>4} {'':>8} {'':>8} "
                  f"{'FAILED':>8}  {r.error[:40]}")
            continue
        line = (f"{r.name[:42]:<42} {r.quality:>4} {r.pages:>4} "
                f"{mb(r.raw_bytes):>8} {mb(r.ref_bytes):>8} {mb(r.our_bytes):>8} "
                f"{r.size_delta:>+7.1f}%")
        if do_render:
            rp = f"{r.ref_psnr:.2f}" if r.ref_psnr is not None else "-"
            op = f"{r.our_psnr:.2f}" if r.our_psnr is not None else "-"
            dp = f"{r.psnr_delta:+.2f}" if r.psnr_delta is not None else "-"
            line += f" {rp:>10} {op:>9} {dp:>7}"
        print(line)

    print()
    for quality in sorted({r.quality for r in ok}):
        sel = [r for r in ok if r.quality == quality]
        if not sel:
            continue
        raw = sum(r.raw_bytes for r in sel)
        theirs = sum(r.ref_bytes for r in sel)
        ours = sum(r.our_bytes for r in sel)
        print(f"quality {quality}: {len(sel)} decks   "
              f"raw {raw / 1e9:.2f} GB -> theirs {theirs / 1e6:.0f} MB "
              f"({raw / theirs:.1f}x), ours {ours / 1e6:.0f} MB "
              f"({raw / ours:.1f}x)   "
              f"we are {100 * (ours / theirs - 1):+.1f}% on size")
        deltas = [r.size_delta for r in sel]
        smaller = sum(1 for d in deltas if d < 0)
        print(f"            per-deck size delta: "
              f"median {statistics.median(deltas):+.1f}%, "
              f"best {min(deltas):+.1f}%, worst {max(deltas):+.1f}%   "
              f"smaller on {smaller}/{len(deltas)}")
        if do_render:
            pd = [r.psnr_delta for r in sel if r.psnr_delta is not None]
            if pd:
                better = sum(1 for d in pd if d > 0)
                print(f"            per-deck PSNR delta: "
                      f"median {statistics.median(pd):+.2f} dB, "
                      f"better on {better}/{len(pd)}")
        total_time = sum(r.seconds for r in sel)
        print(f"            wall time {total_time:.0f}s total, "
              f"{total_time / len(sel):.1f}s per deck")
    if bad:
        print(f"\n{len(bad)} failures")


def write_csv(rows: list[Row], path: str) -> None:
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["deck", "quality", "pages", "raw_bytes", "their_bytes",
                    "our_bytes", "size_delta_pct", "their_psnr", "our_psnr",
                    "psnr_delta", "seconds", "error"])
        for r in rows:
            w.writerow([r.name, r.quality, r.pages, r.raw_bytes, r.ref_bytes,
                        r.our_bytes, f"{r.size_delta:.2f}",
                        f"{r.ref_psnr:.3f}" if r.ref_psnr is not None else "",
                        f"{r.our_psnr:.3f}" if r.our_psnr is not None else "",
                        f"{r.psnr_delta:.3f}" if r.psnr_delta is not None else "",
                        f"{r.seconds:.2f}", r.error or ""])
    print(f"\nwrote {path}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("directory",
                    help="dir of <name>.{uncompressed,new,new-0.8}.pdf")
    ap.add_argument("-o", "--out-dir", default=None,
                    help="where to write our output (default: a temp dir, discarded)")
    ap.add_argument("-p", "--profile", default="web",
                    choices=sorted(pdfoptimize.PROFILES))
    ap.add_argument("-q", "--quality", action="append", default=None,
                    choices=["0.6", "0.8"],
                    help="which reference qualities to compare (default: both)")
    ap.add_argument("-n", "--limit", type=int, default=None,
                    help="only the first N decks")
    ap.add_argument("-j", "--jobs", type=int, default=max(1, (os.cpu_count() or 4) // 2),
                    help="parallel decks (each is memory hungry)")
    ap.add_argument("--render", action="store_true",
                    help="also measure visual fidelity (needs pdftoppm)")
    ap.add_argument("--sample-pages", type=int, default=3,
                    help="pages per deck to render (default: 3)")
    ap.add_argument("--dpi", type=int, default=50, help="render DPI (default: 50)")
    ap.add_argument("--csv", default=None, help="also write results as CSV")
    args = ap.parse_args(argv)

    if args.render and not shutil.which("pdftoppm"):
        print("benchmark: --render needs pdftoppm (poppler-utils)", file=sys.stderr)
        return 2

    cases = discover(args.directory)
    if not cases:
        print(f"benchmark: no complete triples found in {args.directory}",
              file=sys.stderr)
        return 2
    cases.sort(key=lambda c: os.path.getsize(c.raw))
    if args.limit:
        cases = cases[: args.limit]

    qualities = args.quality or ["0.6", "0.8"]
    jobs = [(c, q) for c in cases for q in qualities if q in c.references]

    print(f"{len(cases)} decks, qualities {', '.join(qualities)}, "
          f"profile {args.profile}, {len(jobs)} runs, {args.jobs} workers"
          + (f", rendering {args.sample_pages} pages at {args.dpi} DPI"
             if args.render else ""))

    tmp = None
    out_dir = args.out_dir
    if out_dir is None:
        tmp = tempfile.TemporaryDirectory()
        out_dir = tmp.name
    else:
        os.makedirs(out_dir, exist_ok=True)

    rows: list[Row] = []
    try:
        with concurrent.futures.ProcessPoolExecutor(max_workers=args.jobs) as pool:
            futures = {
                pool.submit(run_case, c, q, args.profile, out_dir,
                            args.render, args.sample_pages, args.dpi): (c, q)
                for c, q in jobs
            }
            for i, fut in enumerate(concurrent.futures.as_completed(futures), 1):
                case, quality = futures[fut]
                try:
                    row = fut.result()
                except Exception as exc:
                    row = Row(name=case.name, quality=quality, pages=0,
                              raw_bytes=os.path.getsize(case.raw),
                              ref_bytes=os.path.getsize(case.references[quality]),
                              our_bytes=0, error=f"{type(exc).__name__}: {exc}")
                rows.append(row)
                mark = "!" if row.error else "."
                print(f"\r[{i}/{len(futures)}] {mark} {row.name[:50]:<50}",
                      end="", file=sys.stderr, flush=True)
        print(file=sys.stderr)
        report(rows, args.render)
        if args.csv:
            write_csv(rows, args.csv)
    finally:
        if tmp is not None:
            tmp.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
