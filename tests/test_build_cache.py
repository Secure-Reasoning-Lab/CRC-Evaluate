"""Tests for BuildResultCache thread-safe build result storage."""

import threading
from pathlib import Path
from unittest.mock import MagicMock

from crsbench.builder.types import BuildConfig, BuildResult
from crsbench.executor.build_cache import BuildResultCache


def _make_result(benchmark: str, variant: str) -> BuildResult:
    """Create a minimal BuildResult for testing."""
    config = MagicMock(spec=BuildConfig)
    config.variant_name = variant
    return BuildResult(
        config=config,
        success=True,
        variant_name=variant,
        build_path=Path(f"/build/{benchmark}/{variant}"),
    )


class TestBuildResultCacheBasic:
    """Basic put/get operations."""

    def test_empty_cache(self) -> None:
        cache = BuildResultCache()
        assert cache.get("bench", "variant") is None
        assert cache.get_benchmark("bench") == {}
        assert not cache.has_benchmark("bench")
        assert cache.benchmark_count == 0
        assert cache.total_count == 0

    def test_put_and_get(self) -> None:
        cache = BuildResultCache()
        result = _make_result("bench", "bench-asan-deltaref")

        cache.put("bench", "bench-asan-deltaref", result)

        retrieved = cache.get("bench", "bench-asan-deltaref")
        assert retrieved is result
        assert cache.has_benchmark("bench")
        assert cache.benchmark_count == 1
        assert cache.total_count == 1

    def test_get_missing_variant(self) -> None:
        cache = BuildResultCache()
        result = _make_result("bench", "bench-asan-deltaref")
        cache.put("bench", "bench-asan-deltaref", result)

        assert cache.get("bench", "bench-asan-allpatched") is None

    def test_get_missing_benchmark(self) -> None:
        cache = BuildResultCache()
        result = _make_result("bench", "bench-asan-deltaref")
        cache.put("bench", "bench-asan-deltaref", result)

        assert cache.get("other", "bench-asan-deltaref") is None

    def test_get_benchmark(self) -> None:
        cache = BuildResultCache()
        r1 = _make_result("bench", "bench-asan-deltaref")
        r2 = _make_result("bench", "bench-asan-allpatched")

        cache.put("bench", "bench-asan-deltaref", r1)
        cache.put("bench", "bench-asan-allpatched", r2)

        results = cache.get_benchmark("bench")
        assert len(results) == 2
        assert results["bench-asan-deltaref"] is r1
        assert results["bench-asan-allpatched"] is r2

    def test_get_benchmark_returns_copy(self) -> None:
        """Returned dict is a copy, not the internal store."""
        cache = BuildResultCache()
        r1 = _make_result("bench", "bench-asan-deltaref")
        cache.put("bench", "bench-asan-deltaref", r1)

        results = cache.get_benchmark("bench")
        results["injected"] = _make_result("bench", "injected")

        assert cache.get("bench", "injected") is None

    def test_put_benchmark(self) -> None:
        cache = BuildResultCache()
        r1 = _make_result("bench", "bench-asan-deltaref")
        r2 = _make_result("bench", "bench-asan-allpatched")

        cache.put_benchmark(
            "bench",
            {
                "bench-asan-deltaref": r1,
                "bench-asan-allpatched": r2,
            },
        )

        assert cache.has_benchmark("bench")
        assert cache.get("bench", "bench-asan-deltaref") is r1
        assert cache.total_count == 2

    def test_clear(self) -> None:
        cache = BuildResultCache()
        cache.put("bench", "bench-asan-deltaref", _make_result("bench", "v"))
        cache.put("other", "other-asan-deltaref", _make_result("other", "v"))

        cache.clear()

        assert cache.benchmark_count == 0
        assert cache.total_count == 0
        assert not cache.has_benchmark("bench")

    def test_overwrite(self) -> None:
        cache = BuildResultCache()
        r1 = _make_result("bench", "v1")
        r2 = _make_result("bench", "v1")

        cache.put("bench", "v1", r1)
        cache.put("bench", "v1", r2)

        assert cache.get("bench", "v1") is r2
        assert cache.total_count == 1


class TestBuildResultCacheMultiBenchmark:
    """Multiple benchmarks in cache."""

    def test_multiple_benchmarks(self) -> None:
        cache = BuildResultCache()
        cache.put("a", "a-asan-deltaref", _make_result("a", "a-asan-deltaref"))
        cache.put("b", "b-asan-deltaref", _make_result("b", "b-asan-deltaref"))
        cache.put("b", "b-asan-allpatched", _make_result("b", "b-asan-allpatched"))

        assert cache.benchmark_count == 2
        assert cache.total_count == 3
        assert len(cache.get_benchmark("a")) == 1
        assert len(cache.get_benchmark("b")) == 2


class TestBuildResultCacheThreadSafety:
    """Concurrent access safety."""

    def test_concurrent_writes(self) -> None:
        cache = BuildResultCache()
        errors: list[str] = []

        def writer(bench_id: int) -> None:
            try:
                for i in range(50):
                    cache.put(
                        f"bench-{bench_id}",
                        f"variant-{i}",
                        _make_result(f"bench-{bench_id}", f"variant-{i}"),
                    )
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert cache.benchmark_count == 8
        assert cache.total_count == 400

    def test_concurrent_reads_and_writes(self) -> None:
        cache = BuildResultCache()
        cache.put("bench", "v0", _make_result("bench", "v0"))
        errors: list[str] = []

        def reader() -> None:
            try:
                for _ in range(100):
                    cache.get("bench", "v0")
                    cache.get_benchmark("bench")
                    cache.has_benchmark("bench")
            except Exception as e:
                errors.append(str(e))

        def writer() -> None:
            try:
                for i in range(100):
                    cache.put("bench", f"v{i}", _make_result("bench", f"v{i}"))
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=reader) for _ in range(4)]
        threads.extend(threading.Thread(target=writer) for _ in range(2))
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
