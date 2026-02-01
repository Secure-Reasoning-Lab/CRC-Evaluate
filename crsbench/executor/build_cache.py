"""Thread-safe cache for build results.

Replaces module-level globals (set_build_cache()) in evaluator_jobs.py
with an explicit cache object that can be passed to job functions.
"""

import threading
from typing import Optional

from crsbench.builder.types import BuildResult
from crsbench.utils.logger import get_logger

logger = get_logger(__name__)


class BuildResultCache:
    """Thread-safe cache for build results keyed by (benchmark, variant).

    Stores BuildResult objects and provides lookup by benchmark name
    and variant name. Safe for concurrent access from multiple threads.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cache: dict[str, dict[str, BuildResult]] = {}

    def get(self, benchmark: str, variant: str) -> Optional[BuildResult]:
        """Get a build result by benchmark and variant name.

        Args:
            benchmark: Benchmark name
            variant: Variant name

        Returns:
            BuildResult if found, None otherwise
        """
        with self._lock:
            benchmark_results = self._cache.get(benchmark)
            if benchmark_results is None:
                return None
            return benchmark_results.get(variant)

    def put(self, benchmark: str, variant: str, result: BuildResult) -> None:
        """Store a build result.

        Args:
            benchmark: Benchmark name
            variant: Variant name
            result: Build result to cache
        """
        with self._lock:
            if benchmark not in self._cache:
                self._cache[benchmark] = {}
            self._cache[benchmark][variant] = result

    def get_benchmark(self, benchmark: str) -> dict[str, BuildResult]:
        """Get all build results for a benchmark.

        Args:
            benchmark: Benchmark name

        Returns:
            Dict of variant_name -> BuildResult (empty dict if not found)
        """
        with self._lock:
            return dict(self._cache.get(benchmark, {}))

    def has_benchmark(self, benchmark: str) -> bool:
        """Check if any build results exist for a benchmark.

        Args:
            benchmark: Benchmark name

        Returns:
            True if at least one result exists for this benchmark
        """
        with self._lock:
            return benchmark in self._cache and len(self._cache[benchmark]) > 0

    def put_benchmark(self, benchmark: str, results: dict[str, BuildResult]) -> None:
        """Store all build results for a benchmark at once.

        Args:
            benchmark: Benchmark name
            results: Dict of variant_name -> BuildResult
        """
        with self._lock:
            self._cache[benchmark] = dict(results)

    def clear(self) -> None:
        """Remove all cached results."""
        with self._lock:
            self._cache.clear()

    @property
    def benchmark_count(self) -> int:
        """Number of benchmarks with cached results."""
        with self._lock:
            return len(self._cache)

    @property
    def total_count(self) -> int:
        """Total number of cached build results across all benchmarks."""
        with self._lock:
            return sum(len(v) for v in self._cache.values())
