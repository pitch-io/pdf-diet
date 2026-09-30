# Copyright 2026 Pitch Software GmbH
# SPDX-License-Identifier: Apache-2.0
"""A minimal process pool that works on AWS Lambda.

Threads do not help here: Pillow holds the GIL through JPEG and JPEG 2000
encode and decode, which is where the time goes, so a thread pool measured
exactly 1.0x. Processes it is.

``multiprocessing.Pool`` and ``concurrent.futures.ProcessPoolExecutor`` both
coordinate through POSIX semaphores, which need ``/dev/shm``. Lambda does not
provide it, and both fail at construction with ``OSError: [Errno 38]
Function not implemented``. Plain ``Process`` plus ``Pipe`` needs neither,
so that is all this uses.
"""

import math
import multiprocessing as mp
import os
import sys
import traceback
from collections.abc import Callable, Iterable, Iterator
from multiprocessing.connection import Connection, wait
from typing import Any

__all__ = ["Pool", "available_cpus"]


def _cgroup_cpu_limit() -> float | None:
    """CPU quota imposed by the container's cgroup, in CPUs, if any.

    Lambda allocates CPU in proportion to memory and enforces it as a quota,
    so the core count the kernel reports can exceed what the function may
    actually use. Workers beyond the quota only add contention.
    """
    try:  # cgroup v2
        with open("/sys/fs/cgroup/cpu.max") as fh:
            quota, period = fh.read().split()[:2]
        if quota != "max":
            return int(quota) / int(period)
        return None
    except (OSError, ValueError):
        pass
    try:  # cgroup v1
        with open("/sys/fs/cgroup/cpu/cpu.cfs_quota_us") as fh:
            quota = int(fh.read())
        with open("/sys/fs/cgroup/cpu/cpu.cfs_period_us") as fh:
            period = int(fh.read())
        if quota > 0 and period > 0:
            return quota / period
    except (OSError, ValueError):
        pass
    return None


def available_cpus() -> int:
    """CPUs this process may use: affinity mask, capped by any cgroup quota."""
    try:
        n = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        n = os.cpu_count() or 1
    limit = _cgroup_cpu_limit()
    if limit is not None:
        n = min(n, max(1, math.ceil(limit)))
    return max(1, n)


def _serve(conn: Connection) -> None:
    """Worker loop: run ``fn(arg)`` for each message until told to stop."""
    while True:
        try:
            msg = conn.recv()
        except EOFError:
            return
        if msg is None:
            return
        fn, arg = msg
        try:
            reply = (True, fn(arg))
        except BaseException:
            reply = (False, traceback.format_exc())
        conn.send(reply)


class WorkerError(RuntimeError):
    """A job raised inside a worker. The message carries its traceback."""


class Pool:
    """``workers`` processes fed over pipes, one job in flight per worker.

    One in flight is deliberate. A second job sent to a busy worker blocks
    once it exceeds the pipe buffer (any real image does), and the worker is
    then blocked sending its result back: deadlock. It also bounds memory,
    which matters on Lambda.

    With ``workers <= 1`` nothing is spawned and jobs run inline.

    Start the pool *before* opening anything large: workers are forked, so
    they inherit whatever the parent holds at that moment.
    """

    def __init__(self, workers: int):
        self.workers = max(1, workers)
        self._conns: list[Connection] = []
        self._procs: list[Any] = []
        if self.workers == 1:
            return
        # fork is cheap and needs nothing re-imported; spawn elsewhere, where
        # fork is unsafe (macOS) or unavailable (Windows).
        ctx = mp.get_context("fork" if sys.platform == "linux" else "spawn")
        try:
            for _ in range(self.workers):
                parent, child = ctx.Pipe(duplex=True)
                proc = ctx.Process(target=_serve, args=(child,), daemon=True)
                proc.start()
                child.close()
                self._conns.append(parent)
                self._procs.append(proc)
        except Exception:
            # Some sandboxes forbid process creation. Inline still works.
            self.close()
            self.workers = 1

    def map_unordered(
        self, fn: Callable[[Any], Any], items: Iterable[tuple[Any, Any]]
    ) -> Iterator[tuple[Any, Any]]:
        """Yield ``(tag, fn(arg))`` for each ``(tag, arg)``, as each completes.

        ``items`` is consumed lazily, only when a worker is free, so a
        generator that decodes images keeps at most ``workers + 1`` of them
        alive: one per worker plus one decoded ahead.
        ``fn`` must be a module-level function so it pickles by reference.
        """
        if not self._conns:
            for tag, arg in items:
                yield tag, fn(arg)
            return

        it = iter(items)
        idle = list(self._conns)
        busy: dict[Connection, Any] = {}
        ahead: list[tuple[Any, Any]] = []
        exhausted = False

        def pull() -> bool:
            nonlocal exhausted
            if not exhausted:
                try:
                    ahead.append(next(it))
                except StopIteration:
                    exhausted = True
            return bool(ahead)

        while True:
            while idle and pull():
                tag, arg = ahead.pop()
                conn = idle.pop()
                conn.send((fn, arg))
                busy[conn] = tag
            if not busy:
                return
            # Every worker is busy: produce the next item now, so producing it
            # (decoding, for the optimizer) overlaps their work instead of
            # delaying it.
            if not ahead:
                pull()
            for conn in wait(list(busy)):
                tag = busy.pop(conn)
                try:
                    ok, value = conn.recv()
                except EOFError:
                    raise WorkerError("worker exited unexpectedly (out of memory?)") from None
                idle.append(conn)
                if not ok:
                    raise WorkerError(value)
                yield tag, value

    def close(self) -> None:
        for conn in self._conns:
            try:
                conn.send(None)
                conn.close()
            except Exception:
                pass
        for proc in self._procs:
            proc.join(timeout=5)
            if proc.is_alive():
                proc.kill()
        self._conns, self._procs = [], []

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()
