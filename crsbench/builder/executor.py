"""Parallel execution utilities for the builder module.

This module provides ParallelExecutor for running build tasks concurrently
using ThreadPoolExecutor.
"""

from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from typing import Callable, TypeVar

from crsbench.builder.types import BuildConfig, BuildResult
from crsbench.utils.logger import get_logger

logger = get_logger(__name__)

T = TypeVar("T")


class ParallelExecutor:
    """Executor for running build tasks in parallel.

    Uses ThreadPoolExecutor to run multiple builds concurrently.
    Docker daemon contention limits practical parallelism to 4-8 workers.

    Attributes:
        max_workers: Maximum number of parallel workers
    """

    def __init__(self, max_workers: int = 4):
        """Initialize the parallel executor.

        Args:
            max_workers: Maximum number of parallel workers (default: 4)
        """
        if max_workers < 1:
            raise ValueError(f"max_workers must be >= 1, got {max_workers}")
        self.max_workers = max_workers

    def execute_builds(
        self,
        configs: list[BuildConfig],
        build_fn: Callable[[BuildConfig], BuildResult],
    ) -> dict[str, BuildResult]:
        """Execute builds in parallel.

        Args:
            configs: List of build configurations
            build_fn: Function that builds a single variant

        Returns:
            Dict mapping variant names to their build results
        """
        if not configs:
            return {}

        results: dict[str, BuildResult] = {}
        total = len(configs)

        logger.info(f"Building {total} variants with {self.max_workers} workers...")

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            # Submit all builds
            future_to_config: dict[Future[BuildResult], BuildConfig] = {
                executor.submit(build_fn, config): config for config in configs
            }

            # Collect results as they complete
            completed = 0
            for future in as_completed(future_to_config):
                config = future_to_config[future]
                completed += 1

                try:
                    result = future.result()
                    results[result.variant_name] = result

                    status = "OK" if result.success else "FAILED"
                    logger.info(
                        f"[{completed}/{total}] {config.variant_name}: {status}"
                    )

                except Exception as e:
                    # Handle unexpected exceptions from build_fn
                    error_msg = f"Unexpected error: {e}"
                    logger.error(
                        f"[{completed}/{total}] {config.variant_name}: {error_msg}"
                    )

                    results[config.variant_name] = BuildResult.from_error(
                        config=config,
                        error=error_msg,
                    )

        # Summary
        success_count = sum(1 for r in results.values() if r.success)
        cached_count = sum(1 for r in results.values() if r.cached)
        logger.info(
            f"Build complete: {success_count}/{total} succeeded "
            f"({cached_count} from cache)"
        )

        return results

    def execute_single(
        self,
        config: BuildConfig,
        build_fn: Callable[[BuildConfig], BuildResult],
    ) -> BuildResult:
        """Execute a single build (no parallelism).

        Args:
            config: Build configuration
            build_fn: Function that builds the variant

        Returns:
            Build result
        """
        logger.info(f"Building {config.variant_name}...")

        try:
            result = build_fn(config)
            status = "OK" if result.success else "FAILED"
            logger.info(f"{config.variant_name}: {status}")
            return result
        except Exception as e:
            error_msg = f"Unexpected error: {e}"
            logger.error(f"{config.variant_name}: {error_msg}")
            return BuildResult.from_error(config=config, error=error_msg)
