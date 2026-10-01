# Copyright 2026 Pitch Software GmbH
# SPDX-License-Identifier: Apache-2.0
"""The top-level optimization pass."""

import hashlib
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, field

import pikepdf
from PIL import Image

from .document import adjust_matrix, dedupe, prune, rewrite_placements
from .geometry import Plan, plan_image, scan_placements
from .images import (
    encode_candidates,
    flate,
    load_pil,
    normalize_mode,
    reduce_complexity,
    set_image,
)
from .parallel import Pool, available_cpus
from .profiles import Profile, Web
from .srgb import tag_srgb

__all__ = ["ImageResult", "Optimizer", "Result", "optimize_document"]


@dataclass
class ImageResult:
    """What happened to one image XObject."""

    objgen: tuple
    source_px: tuple[int, int]
    result_px: tuple[int, int]
    before_bytes: int
    after_bytes: int
    filter: str
    cropped: bool
    #: Seconds spent on this image, decode to write-back, in whichever
    #: process did the work.
    seconds: float = 0.0


def _add(timings: dict[str, float], phase: str, seconds: float) -> None:
    timings[phase] = timings.get(phase, 0.0) + seconds


@dataclass
class Result:
    """Outcome of one optimization run."""

    input_path: str
    output_path: str
    before_bytes: int
    after_bytes: int
    images: list[ImageResult] = field(default_factory=list)
    merged_objects: int = 0
    srgb_tagged: bool = False
    #: Why ``declare_srgb`` was requested but not applied, else None.
    srgb_skipped: str | None = None
    #: Wall-clock seconds per top-level phase, in the order they ran. These
    #: add up to the run's total.
    timings: dict[str, float] = field(default_factory=dict)
    #: Where the ``images`` phase went, summed over every image. With
    #: several workers these overlap, so they add up to *more* than
    #: ``timings["images"]``: read them as CPU cost, not wall-clock.
    image_timings: dict[str, float] = field(default_factory=dict)
    #: Processes that encoded images; 1 means inline.
    workers: int = 1

    @contextmanager
    def timed(self, phase: str):
        """Add the time spent inside the block to ``timings[phase]``."""
        t0 = time.perf_counter()
        try:
            yield
        finally:
            _add(self.timings, phase, time.perf_counter() - t0)

    def timing_report(self) -> str:
        total = sum(self.timings.values())
        lines = [f"  timings ({self.workers} worker{'s' if self.workers != 1 else ''}):"]
        for phase, secs in self.timings.items():
            share = 100 * secs / total if total else 0.0
            lines.append(f"    {phase:<10} {secs:>8.3f}s {share:>5.1f}%")
            if phase == "images":
                for sub, cpu in self.image_timings.items():
                    lines.append(f"      {sub:<8} {cpu:>8.3f}s cpu")
        lines.append(f"    {'total':<10} {total:>8.3f}s")
        return "\n".join(lines)

    @property
    def ratio(self) -> float:
        return self.before_bytes / self.after_bytes if self.after_bytes else 0.0

    @property
    def saved_fraction(self) -> float:
        if not self.before_bytes:
            return 0.0
        return 1.0 - self.after_bytes / self.before_bytes

    def __str__(self) -> str:
        return (
            f"{self.input_path}: {self.before_bytes / 1e6:.2f} MB -> "
            f"{self.output_path}: {self.after_bytes / 1e6:.2f} MB "
            f"({self.ratio:.1f}x smaller, {100 * self.saved_fraction:.1f}% saved)"
        )


# --------------------------------------------------------------------------
# Per-image work. Runs in a worker process, so it sees only Pillow images and
# plain values: pikepdf objects belong to the parent's Pdf and cannot cross.
# Everything that touches the document stays in Optimizer._apply.
# --------------------------------------------------------------------------


@dataclass
class _Job:
    im: Image.Image
    smask: Image.Image | None
    crop: tuple[int, int, int, int] | None
    size: tuple[int, int]
    source_px: tuple[int, int]
    profile: Profile


@dataclass
class _Outcome:
    #: Smallest eligible (size, spec), or None if there was no candidate.
    best: tuple[int, dict] | None
    #: ("drop",) | ("stencil", bytes) | ("replace", spec) | None
    mask: tuple | None
    timings: dict[str, float]


def _mask_action(smask: Image.Image, profile: Profile) -> tuple | None:
    """Drop a fully-opaque mask, stencil a binary one, recompress the rest."""
    if smask.mode != "L":
        smask = smask.convert("L")
    lo, _hi = smask.getextrema()

    if lo == 255:  # nothing is transparent
        return ("drop",)

    colors = smask.getcolors(4) or [(0, 1)]
    if profile.reduce_color_complexity and all(v in (0, 255) for _, v in colors):
        # Binary alpha: a 1-bit stencil is far cheaper than an 8-bit mask.
        stencil = smask.point(lambda v: 0 if v else 255, mode="1")
        return ("stencil", flate(stencil.tobytes()))

    cands = encode_candidates(smask, profile)
    if not cands:
        return None
    _size, spec = min(cands, key=lambda c: c[0])
    spec["cs"] = "/DeviceGray"
    return ("replace", spec)


def _process(job: _Job) -> _Outcome:
    """Crop, resize and encode one image and its soft mask."""
    timings: dict[str, float] = {}
    t0 = time.perf_counter()
    im, smask = job.im, job.smask
    if job.crop:
        im = im.crop(job.crop)
        if smask is not None:
            # The soft mask may have its own resolution.
            sx = smask.width / job.source_px[0]
            sy = smask.height / job.source_px[1]
            smask = smask.crop(
                (
                    int(job.crop[0] * sx),
                    int(job.crop[1] * sy),
                    max(1, int(job.crop[2] * sx)),
                    max(1, int(job.crop[3] * sy)),
                )
            )
    if (im.width, im.height) != job.size:
        im = im.resize(job.size, Image.LANCZOS)
    if smask is not None and (smask.width, smask.height) != job.size:
        smask = smask.resize(job.size, Image.LANCZOS)
    im = normalize_mode(im)
    if job.profile.reduce_color_complexity:
        im = reduce_complexity(im)
    t1 = time.perf_counter()
    _add(timings, "prepare", t1 - t0)

    mask = None
    if smask is not None:
        mask = _mask_action(smask, job.profile)
        t2 = time.perf_counter()
        _add(timings, "mask", t2 - t1)
        t1 = t2

    cands = encode_candidates(im, job.profile)
    _add(timings, "encode", time.perf_counter() - t1)
    best = min(cands, key=lambda c: c[0]) if cands else None
    return _Outcome(best=best, mask=mask, timings=timings)


@dataclass
class _Pending:
    """Parent-side state for an image while its job is out."""

    objgen: tuple
    before: int
    plan: Plan
    source_px: tuple[int, int]
    decode_seconds: float
    key: tuple


#: Dictionary entries that change what an image stream decodes to.
_DECODE_KEYS = (
    "/Width",
    "/Height",
    "/Filter",
    "/DecodeParms",
    "/ColorSpace",
    "/BitsPerComponent",
    "/Decode",
    "/ImageMask",
    "/Mask",
)


def _content_key(stream) -> tuple:
    """Identifies a stream by what it decodes to, not by which object it is."""
    return (
        hashlib.blake2b(stream.read_raw_bytes(), digest_size=16).digest(),
        tuple(repr(stream.get(k)) for k in _DECODE_KEYS),
    )


@dataclass
class _Seen:
    """Images already sent out, by content key."""

    #: Outcomes that are back.
    done: dict[tuple, _Outcome] = field(default_factory=dict)
    #: Keys still out, with the copies waiting on each.
    waiting: dict[tuple, list[_Pending]] = field(default_factory=dict)


class Optimizer:
    """Compresses PDFs. Mirrors ``pdftools_sdk.optimization.optimizer.Optimizer``.

    ``workers`` is how many processes encode images: ``None`` (the default)
    means one per CPU this process may use, cgroup quota included, which is
    what matters on AWS Lambda; ``1`` encodes inline with no subprocesses.

    Example:
        >>> from pdfdiet import Optimizer, MinimalFileSize
        >>> result = Optimizer().optimize_document("in.pdf", "out.pdf", MinimalFileSize())
        >>> result.ratio > 1
        True
    """

    def __init__(self, verbose: bool = False, workers: int | None = None):
        self.verbose = verbose
        self.workers = workers

    def _log(self, *a) -> None:
        if self.verbose:
            print(*a)

    def _declare_srgb(self, pdf, result: Result) -> None:
        """Tag ``pdf`` as sRGB, recording a failure on ``result`` rather than
        raising: an undeclared file beats none."""
        try:
            tag_srgb(pdf)
            result.srgb_tagged = True
        except Exception as exc:
            result.srgb_skipped = str(exc) or type(exc).__name__
            self._log(f"  sRGB declaration skipped: {result.srgb_skipped}")

    def declare_srgb(self, in_path, out_path) -> Result:
        """Write ``in_path`` to ``out_path`` with only the sRGB declaration added.

        Lossless: no image is touched, nothing is pruned or deduplicated, and
        existing streams keep their encoding. For callers that want the colour
        fix without compression.
        """
        result = Result(
            input_path=str(in_path),
            output_path=str(out_path),
            before_bytes=os.path.getsize(in_path),
            after_bytes=0,
        )
        with result.timed("open"):
            pdf = pikepdf.open(in_path)
        with pdf:
            with result.timed("srgb"):
                self._declare_srgb(pdf, result)
            with result.timed("save"):
                pdf.save(out_path)
        result.after_bytes = os.path.getsize(out_path)
        self._log(result.timing_report())
        return result

    def optimize_document(self, in_path, out_path, profile: Profile | None = None) -> Result:
        """Optimize ``in_path`` into ``out_path``. Returns a :class:`Result`."""
        profile = profile or Web()
        result = Result(
            input_path=str(in_path),
            output_path=str(out_path),
            before_bytes=os.path.getsize(in_path),
            after_bytes=0,
        )
        n = self.workers if self.workers is not None else available_cpus()
        # Before opening the PDF: workers are forked and should not inherit it.
        with result.timed("spawn"):
            pool = Pool(n)
        with pool:
            result.workers = pool.workers
            with result.timed("open"):
                pdf = pikepdf.open(in_path)
            try:
                return self._optimize(pdf, pool, out_path, profile, result)
            finally:
                pdf.close()

    def _jobs(self, pdf, placements, profile, result: Result, seen: _Seen, adjust):
        """Decode each image worth touching; yields ``(_Pending, _Job)``.

        A generator so the pool can pull lazily: only the images currently
        being worked on are held decoded in memory.

        An image identical to one already sent out -- same samples, mask,
        crop and size -- is not sent again. Decks repeat images as separate
        XObjects that ``dedupe`` only merges at the end; across the benchmark
        corpus that was 19% of all pixels encoded. It gets the first copy's
        outcome instead, now if that is back, or when it arrives.
        """
        # Largest first: encode time grows with pixel count, and one big image
        # handed out last leaves every other worker idle while it finishes.
        by_size = sorted(placements.items(), key=lambda kv: -kv[1][0].px[0] * kv[1][0].px[1])
        for objgen, ps in by_size:
            try:
                xobj = pdf.get_object(objgen)
                before = len(xobj.read_raw_bytes())
            except Exception:
                continue

            plan = plan_image(ps, profile)
            if plan is None:
                continue

            try:
                smask_obj = xobj.get("/SMask")
                key = (
                    _content_key(xobj),
                    _content_key(smask_obj) if smask_obj is not None else None,
                    plan.crop,
                    plan.size,
                )
            except Exception:
                key = (objgen,)  # unhashable content: never shared
            pending = _Pending(objgen, before, plan, ps[0].px, 0.0, key)
            if key in seen.done:
                self._apply(pdf, pending, seen.done[key], result, adjust, reused=True)
                continue
            if key in seen.waiting:
                seen.waiting[key].append(pending)
                continue
            seen.waiting[key] = []

            t0 = time.perf_counter()
            im = load_pil(xobj)
            smask_obj = xobj.get("/SMask")
            smask_im = load_pil(smask_obj) if smask_obj is not None else None
            pending.decode_seconds = time.perf_counter() - t0
            _add(result.image_timings, "decode", pending.decode_seconds)
            if im is None:
                # Copies of an undecodable image are left alone with it.
                del seen.waiting[key]
                continue

            yield pending, _Job(im, smask_im, plan.crop, plan.size, ps[0].px, profile)

    def _apply(
        self,
        pdf,
        pending: _Pending,
        outcome: _Outcome,
        result: Result,
        adjust,
        reused: bool = False,
    ) -> None:
        """Write one image's outcome back into the document.

        ``reused`` marks a copy given another image's outcome: its encode
        time was not spent again, so it is neither counted nor reported.
        """
        t0 = time.perf_counter()
        spent = 0.0 if reused else pending.decode_seconds + sum(outcome.timings.values())
        if not reused:
            for phase, secs in outcome.timings.items():
                _add(result.image_timings, phase, secs)
        xobj = pdf.get_object(pending.objgen)
        plan = pending.plan
        w, h = pending.source_px

        if outcome.mask is not None:
            kind = outcome.mask[0]
            if kind == "drop":
                del xobj["/SMask"]
            elif kind == "stencil":
                ms = pdf.make_stream(outcome.mask[1])
                ms.Filter = pikepdf.Name("/FlateDecode")
                ms.Type = pikepdf.Name("/XObject")
                ms.Subtype = pikepdf.Name("/Image")
                ms.Width, ms.Height = plan.size
                ms.ImageMask = True
                ms.BitsPerComponent = 1
                del xobj["/SMask"]
                xobj.Mask = ms
            elif kind == "replace":
                set_image(xobj.SMask, pdf, outcome.mask[1], plan.size)

        def seconds() -> float:
            return spent + time.perf_counter() - t0

        tag = " (copy)" if reused else ""

        if outcome.best is None:
            self._log(f"  {w}x{h} kept, no candidate {seconds():.2f}s{tag}")
            return
        size, spec = outcome.best
        before = pending.before

        if size >= before:
            # Recompression would not help. Logged anyway: the encode time
            # was spent all the same.
            self._log(
                f"  {w}x{h} kept, best "
                f"{spec['filter']:<12} {before / 1024:>9.1f}K <= {size / 1024:>8.1f}K "
                f"{seconds():>7.2f}s{tag}"
            )
            return

        set_image(xobj, pdf, spec, plan.size)
        _add(result.image_timings, "write", time.perf_counter() - t0)
        if plan.uv:
            adjust[pending.objgen] = adjust_matrix(plan.uv)
        result.images.append(
            ImageResult(
                objgen=pending.objgen,
                source_px=pending.source_px,
                result_px=plan.size,
                before_bytes=before,
                after_bytes=size,
                filter=spec["filter"],
                cropped=plan.crop is not None,
                seconds=seconds(),
            )
        )
        self._log(
            f"  {w}x{h} -> "
            f"{plan.size[0]}x{plan.size[1]} {spec['filter']:<12} "
            f"{before / 1024:>9.1f}K -> {size / 1024:>8.1f}K "
            f"{result.images[-1].seconds:>7.2f}s{tag}"
        )

    def _optimize(self, pdf, pool: Pool, out_path, profile: Profile, result: Result) -> Result:
        with result.timed("scan"):
            placements = scan_placements(pdf)
        adjust: dict[tuple, tuple] = {}
        self._log(f"  {len(placements)} image XObjects placed")

        order = {objgen: i for i, objgen in enumerate(placements)}
        seen = _Seen()
        with result.timed("images"):
            jobs = self._jobs(pdf, placements, profile, result, seen, adjust)
            for pending, outcome in pool.map_unordered(_process, jobs):
                self._apply(pdf, pending, outcome, result, adjust)
                for copy in seen.waiting.pop(pending.key, []):
                    self._apply(pdf, copy, outcome, result, adjust, reused=True)
                seen.done[pending.key] = outcome
        # Results arrive in completion order; report them in document order.
        result.images.sort(key=lambda r: order[r.objgen])

        with result.timed("rewrite"):
            rewrite_placements(pdf, adjust)
        with result.timed("prune"):
            prune(pdf, profile.removal)
        if profile.declare_srgb:
            # After prune, so remove_output_intents cannot strip the intent
            # just added; before dedupe, so it merges with an identical
            # profile already in the file.
            with result.timed("srgb"):
                self._declare_srgb(pdf, result)
        with result.timed("dedupe"):
            result.merged_objects = dedupe(pdf)
        if result.merged_objects:
            self._log(f"  deduplicated {result.merged_objects} redundant objects")

        with result.timed("save"):
            pdf.save(
                out_path,
                compress_streams=True,
                object_stream_mode=pikepdf.ObjectStreamMode.generate,
                linearize=False,
                recompress_flate=True,
                stream_decode_level=pikepdf.StreamDecodeLevel.generalized,
            )
        result.after_bytes = os.path.getsize(out_path)
        self._log(result.timing_report())
        return result


def optimize_document(
    in_path,
    out_path,
    profile: Profile | None = None,
    verbose: bool = False,
    workers: int | None = None,
) -> Result:
    """Convenience wrapper around :class:`Optimizer`."""
    return Optimizer(verbose=verbose, workers=workers).optimize_document(in_path, out_path, profile)


def declare_srgb(in_path, out_path, verbose: bool = False) -> Result:
    """Convenience wrapper around :meth:`Optimizer.declare_srgb`."""
    return Optimizer(verbose=verbose).declare_srgb(in_path, out_path)
