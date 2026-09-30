# Copyright 2026 Pitch Software GmbH
# SPDX-License-Identifier: Apache-2.0
"""The Lambda-safe process pool."""

import os

import pytest

from pdfdiet import parallel
from pdfdiet.parallel import Pool, WorkerError, available_cpus


def _square(x: int) -> int:
    return x * x


def _explode(x: int) -> int:
    raise ValueError(f"bad input {x}")


def _pid(_x: int) -> int:
    return os.getpid()


class TestPool:
    @pytest.mark.parametrize("workers", [1, 3])
    def test_every_item_comes_back_once_with_its_tag(self, workers) -> None:
        with Pool(workers) as pool:
            got = dict(pool.map_unordered(_square, ((i, i) for i in range(20))))
        assert got == {i: i * i for i in range(20)}

    def test_work_runs_in_other_processes(self) -> None:
        with Pool(2) as pool:
            pids = {pid for _, pid in pool.map_unordered(_pid, ((i, i) for i in range(8)))}
        assert os.getpid() not in pids

    def test_one_worker_runs_inline(self) -> None:
        with Pool(1) as pool:
            pids = {pid for _, pid in pool.map_unordered(_pid, [(0, 0)])}
        assert pids == {os.getpid()}

    def test_worker_exception_surfaces_with_its_traceback(self) -> None:
        with Pool(2) as pool, pytest.raises(WorkerError, match="bad input 3"):
            list(pool.map_unordered(_explode, [(3, 3)]))

    def test_items_are_pulled_lazily(self) -> None:
        """Only as many items as there are workers are decoded ahead."""
        pulled = []

        def items():
            for i in range(10):
                pulled.append(i)
                yield i, i

        with Pool(2) as pool:
            it = pool.map_unordered(_square, items())
            next(it)
            assert len(pulled) <= 4  # two in flight, one ahead, one just yielded
            list(it)
        assert len(pulled) == 10

    def test_falls_back_inline_when_processes_are_forbidden(self, monkeypatch) -> None:
        class NoProcesses:
            def Pipe(self, duplex):
                raise OSError(38, "Function not implemented")

        monkeypatch.setattr(parallel.mp, "get_context", lambda _method: NoProcesses())
        with Pool(4) as pool:
            assert pool.workers == 1
            assert dict(pool.map_unordered(_square, [(2, 2)])) == {2: 4}


class TestAvailableCpus:
    def test_is_at_least_one(self) -> None:
        assert available_cpus() >= 1

    def test_cgroup_quota_caps_the_count(self, monkeypatch) -> None:
        monkeypatch.setattr(parallel, "_cgroup_cpu_limit", lambda: 1.5)
        monkeypatch.setattr(os, "sched_getaffinity", lambda _pid: set(range(8)))
        assert available_cpus() == 2
