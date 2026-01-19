"""Coverage engine for orchestrating coverage collection.

This module provides the CoverageEngine for collecting code coverage
from corpus files against benchmark projects, following the same
patterns as VerificationEngine (POV) and PatchVerificationEngine.

Architecture:
- Uses OSSFuzzBuilder for building coverage variants (with build_workers)
- Uses ThreadPoolExecutor for parallel corpus processing (with verify_workers)
- Uses MetaYamlAdapter for consistent config loading
- Provides cleanup() method for resource management

Usage:
    engine = CoverageEngine(
        oss_fuzz_path=Path("./oss-fuzz"),
        build_workers=4,
        verify_workers=8,
    )
    try:
        report = engine.collect_coverage(
            benchmark_path=Path("benchmarks/my-project"),
            corpus_dir=Path("/path/to/corpus"),
        )
    finally:
        engine.cleanup()
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

from crsbench.builder import OSSFuzzBuilder
from crsbench.builder.infrastructure import OSSFuzzInfrastructure
from crsbench.builder.types import BuildConfig, VariantType
from crsbench.evaluation.coverage.models import CoverageReport, CoverageSummary
from crsbench.evaluation.coverage.strategy import (
    CoverageStrategy,
    CoverageStrategyError,
    create_coverage_strategy,
    parse_llvm_cov_summary,
)
from crsbench.utils.logger import get_logger
from crsbench.utils.workers import resolve_build_workers, resolve_verify_workers
from crsbench.validation.meta_adapter import MetaYamlAdapter

logger = get_logger(__name__)


class CoverageEngine:
    """Engine for orchestrating coverage collection.

    Coordinates the entire coverage workflow:
    1. Load benchmark configuration via MetaYamlAdapter
    2. Build coverage variant (with build_workers)
    3. Collect coverage from corpus files (parallel with verify_workers)
    4. Merge and deduplicate coverage results
    5. Generate coverage reports

    This follows the same architectural pattern as VerificationEngine (POV)
    and PatchVerificationEngine, enabling consistent parallelization and
    configuration across all verification/analysis workflows.

    Attributes:
        oss_fuzz_path: Path to oss-fuzz directory.
        builder: OSSFuzzBuilder instance for building variants.
        infra: OSSFuzzInfrastructure for low-level operations.
        build_workers: Number of parallel workers for building.
        verify_workers: Number of parallel workers for coverage collection.
    """

    def __init__(
        self,
        oss_fuzz_path: Path,
        *,
        build_workers: Optional[int] = None,
        verify_workers: Optional[int] = None,
        work_dir: Optional[Path] = None,
        source_mode: str = "main_repo",
    ):
        """Initialize the coverage engine.

        Args:
            oss_fuzz_path: Path to oss-fuzz directory.
            build_workers: Number of parallel workers for building (default: 4).
                Priority: CLI > CRSBENCH_BUILD_WORKERS env > config > default.
            verify_workers: Number of parallel workers for coverage (default: 4).
                Priority: CLI > CRSBENCH_VERIFY_WORKERS env > config > default.
            work_dir: Working directory for isolated builds. If None, uses
                default oss-fuzz/build/out/ location.
            source_mode: Source mode - "main_repo" (clone, default) or "pkgs" (bundled)
        """
        self.oss_fuzz_path = Path(oss_fuzz_path)
        self.work_dir = Path(work_dir) if work_dir else None
        self.build_workers = resolve_build_workers(build_workers)
        self.verify_workers = resolve_verify_workers(verify_workers)
        self.builder = OSSFuzzBuilder(
            oss_fuzz_path, max_workers=self.build_workers, source_mode=source_mode
        )
        self.infra = OSSFuzzInfrastructure(oss_fuzz_path, work_dir=work_dir)

        # Cache for strategies (keyed by variant_name)
        # Note: Strategy creation happens before parallel execution, so no lock needed
        self._strategies: dict[str, CoverageStrategy] = {}

        # Thread safety for coverage merging and line set tracking
        self._merge_lock = threading.Lock()

        # Track distinct line sets for corpus_unique calculation
        self._seen_line_sets: set[frozenset[tuple[str, int]]] = set()
        self._covered_lines: set[tuple[str, int]] = set()

    def collect_coverage(
        self,
        benchmark_path: Path,
        corpus_dir: Path,
        harness_filter: Optional[str] = None,
        *,
        force_rebuild: bool = False,
    ) -> CoverageReport:
        """Collect coverage for a benchmark.

        Main entry point for coverage collection. Builds the coverage variant,
        discovers corpus files, and collects coverage in parallel.

        Args:
            benchmark_path: Path to benchmark project directory.
            corpus_dir: Directory containing corpus files.
            harness_filter: Optional harness name to filter. If None, uses
                the first harness from meta.yaml.
            force_rebuild: If True, clean and rebuild even if build exists.

        Returns:
            CoverageReport with coverage statistics and optional snapshots.
        """
        benchmark_path = Path(benchmark_path)
        corpus_dir = Path(corpus_dir)

        # Load adapter
        adapter = self._load_adapter(benchmark_path)
        if not adapter:
            logger.error(f"Failed to load adapter for {benchmark_path}")
            return CoverageReport(
                harness_name="",
                final_summary=CoverageSummary(),
            )

        # Determine harness
        harness_names = (
            [harness_filter] if harness_filter else adapter.get_harness_names()
        )

        if not harness_names:
            logger.error("No harnesses found in benchmark")
            return CoverageReport(harness_name="", final_summary=CoverageSummary())

        harness_name = harness_names[0]  # Use first harness
        logger.info(f"Using harness: {harness_name}")

        # Build coverage variant
        variant_name = self._build_coverage_variant(
            adapter, force_rebuild=force_rebuild
        )
        if not variant_name:
            logger.error("Failed to build coverage variant")
            return CoverageReport(
                harness_name=harness_name, final_summary=CoverageSummary()
            )

        # Verify harness exists in build
        if not self.infra.has_harness(variant_name, harness_name):
            logger.error(
                f"Harness '{harness_name}' not found in build output for {variant_name}"
            )
            return CoverageReport(
                harness_name=harness_name, final_summary=CoverageSummary()
            )

        # Create or get strategy
        strategy = self._get_or_create_strategy(adapter, variant_name)

        # Validate corpus directory
        if not corpus_dir.exists():
            logger.error(f"Corpus directory not found: {corpus_dir}")
            return CoverageReport(
                harness_name=harness_name, final_summary=CoverageSummary()
            )

        # Find corpus files
        corpus_files = [
            f
            for f in corpus_dir.iterdir()
            if f.is_file() and not f.name.startswith(".")
        ]

        if not corpus_files:
            logger.warning(f"No corpus files found in {corpus_dir}")
            return CoverageReport(
                harness_name=harness_name, final_summary=CoverageSummary()
            )

        logger.info(
            f"Collecting coverage for {len(corpus_files)} corpus files "
            f"with {self.verify_workers} workers"
        )

        # Collect coverage in parallel (for per-file contribution tracking)
        merged_coverage, success_count, contributing_count, unique_count = (
            self._collect_coverage_parallel(corpus_files, strategy, harness_name)
        )

        # Run batch coverage to get totals from summary.json
        totals = self._get_coverage_totals(strategy, harness_name, corpus_dir)

        # Compute summary with totals
        summary = self._compute_summary(
            merged_coverage,
            len(corpus_files),
            contributing_count,
            unique_count,
            totals,
        )

        logger.info(
            f"Coverage collection complete: {summary.lines_covered}/{summary.lines_total} lines, "
            f"{summary.functions_covered}/{summary.functions_total} functions, "
            f"{success_count}/{len(corpus_files)} corpus processed, "
            f"{contributing_count} contributing, {unique_count} unique"
        )

        return CoverageReport(
            harness_name=harness_name,
            final_summary=summary,
        )

    def _collect_coverage_parallel(
        self,
        corpus_files: list[Path],
        strategy: CoverageStrategy,
        harness_name: str,
    ) -> tuple[dict, int, int, int]:
        """Collect coverage for multiple corpus files in parallel.

        Uses ThreadPoolExecutor with verify_workers to process corpus files
        concurrently. Each corpus file runs in an isolated Docker container
        via the CoverageStrategy.

        Args:
            corpus_files: List of corpus file paths.
            strategy: CoverageStrategy instance for collection.
            harness_name: Name of the harness.

        Returns:
            Tuple of (merged_coverage_dict, success_count, contributing_count, unique_count).
        """
        merged: dict[str, dict] = {}
        success_count = 0
        contributing_count = 0
        unique_count = 0

        # Reset tracking for this collection
        self._seen_line_sets.clear()
        self._covered_lines.clear()

        with ThreadPoolExecutor(max_workers=self.verify_workers) as executor:
            futures = {
                executor.submit(
                    self._collect_single_safe,
                    strategy,
                    harness_name,
                    corpus_file,
                ): corpus_file
                for corpus_file in corpus_files
            }

            completed = 0
            for future in as_completed(futures):
                corpus_file = futures[future]
                try:
                    cov_data = future.result()
                    if cov_data:
                        # Track contributing and unique corpus
                        is_contributing, is_unique = self._track_corpus_coverage(
                            cov_data
                        )
                        if is_contributing:
                            contributing_count += 1
                        if is_unique:
                            unique_count += 1

                        self._merge_coverage_safe(merged, cov_data)
                        success_count += 1
                    completed += 1
                    if completed % 100 == 0:
                        logger.info(
                            f"Processed {completed}/{len(corpus_files)} corpus files"
                        )
                except Exception as e:
                    logger.warning(f"Failed to collect coverage for {corpus_file}: {e}")
                    completed += 1

        return merged, success_count, contributing_count, unique_count

    def _track_corpus_coverage(self, cov_data: dict) -> tuple[bool, bool]:
        """Track coverage for a corpus file and determine if it's contributing/unique.

        Thread-safe method to track line sets and determine:
        - is_contributing: Does this corpus add new lines?
        - is_unique: Does this corpus have a distinct coverage profile?

        Args:
            cov_data: Coverage data for the corpus file.

        Returns:
            Tuple of (is_contributing, is_unique).
        """
        # Extract line set from coverage data
        line_set: set[tuple[str, int]] = set()
        for func_data in cov_data.values():
            if isinstance(func_data, dict):
                src = func_data.get("src", "")
                for line in func_data.get("lines", []):
                    line_set.add((src, line))

        if not line_set:
            return False, False

        line_set_frozen = frozenset(line_set)

        with self._merge_lock:
            # Check if contributing (adds new lines)
            new_lines = line_set - self._covered_lines
            is_contributing = len(new_lines) > 0

            # Check if unique (distinct profile)
            is_unique = line_set_frozen not in self._seen_line_sets

            # Update tracking
            if is_contributing:
                self._covered_lines.update(new_lines)
            if is_unique:
                self._seen_line_sets.add(line_set_frozen)

        return is_contributing, is_unique

    def _collect_single_safe(
        self,
        strategy: CoverageStrategy,
        harness_name: str,
        corpus_file: Path,
    ) -> dict:
        """Safely collect coverage for a single corpus file.

        Wrapper around strategy.collect_single_coverage() that handles
        exceptions gracefully.

        Args:
            strategy: CoverageStrategy instance.
            harness_name: Name of the harness.
            corpus_file: Path to the corpus file.

        Returns:
            Coverage data dict, or empty dict on failure.
        """
        try:
            return strategy.collect_single_coverage(
                harness_name, corpus_file, output_dir=self.work_dir
            )
        except CoverageStrategyError as e:
            logger.debug(f"Coverage strategy error for {corpus_file.name}: {e}")
            return {}
        except Exception as e:
            logger.debug(f"Unexpected error for {corpus_file.name}: {e}")
            return {}

    def _merge_coverage_safe(self, merged: dict, new_data: dict) -> None:
        """Merge new coverage data into merged dict (thread-safe).

        Uses a lock to protect shared state during parallel merging.
        Coverage lines are stored as sets during merging for efficient
        deduplication, then converted to sorted lists at the end.

        Args:
            merged: Shared merged coverage dict to update.
            new_data: New coverage data to merge in.
        """
        with self._merge_lock:
            for func_name, func_data in new_data.items():
                if not isinstance(func_data, dict):
                    continue
                src = func_data.get("src", "")
                lines = func_data.get("lines", [])

                if func_name not in merged:
                    merged[func_name] = {"src": src, "lines": set(lines)}
                else:
                    merged[func_name]["lines"].update(lines)

    def _get_coverage_totals(
        self,
        strategy: CoverageStrategy,
        harness_name: str,
        corpus_dir: Path,
    ) -> dict:
        """Get coverage totals by running batch coverage.

        Runs batch coverage to generate summary.json which contains
        accurate totals (lines_total, functions_total).

        Args:
            strategy: CoverageStrategy instance.
            harness_name: Name of the harness.
            corpus_dir: Directory containing corpus files.

        Returns:
            Dict with totals: {lines_total, functions_total, lines_percent, ...}
        """
        try:
            summary_path = strategy.collect_batch_coverage(
                Path(harness_name), corpus_dir
            )
            return parse_llvm_cov_summary(summary_path)
        except CoverageStrategyError as e:
            logger.warning(f"Failed to get coverage totals: {e}")
            return {}

    def _build_coverage_variant(
        self,
        adapter: MetaYamlAdapter,
        *,
        force_rebuild: bool = False,
    ) -> Optional[str]:
        """Build coverage variant for benchmark.

        Uses OSSFuzzBuilder to build the coverage-instrumented variant.

        Args:
            adapter: MetaYamlAdapter with benchmark configuration.
            force_rebuild: If True, clean and rebuild.

        Returns:
            Variant name if build succeeded, None otherwise.
        """
        commit = adapter.get_ref_commit() or adapter.get_base_commit()

        config = BuildConfig(
            benchmark_name=adapter.benchmark_name,
            benchmark_path=adapter.benchmark_path or Path(),
            variant_type=VariantType.COVERAGE,
            mode=adapter.get_mode(),
            sanitizer="coverage",
            language=adapter.lang,
            commit=commit,
            main_repo=adapter.main_repo,
            repo_name=adapter.repo_name,
        )

        logger.info(f"Building coverage variant: {config.variant_name}")

        result = self.builder.build_single(config, force_rebuild=force_rebuild)

        if result and result.success:
            logger.info(f"Coverage build succeeded: {result.variant_name}")
            return result.variant_name

        logger.error(f"Coverage build failed for {adapter.benchmark_name}")
        return None

    def _get_or_create_strategy(
        self,
        adapter: MetaYamlAdapter,
        variant_name: str,
    ) -> CoverageStrategy:
        """Get or create coverage strategy for a variant.

        Strategies are cached by variant name to avoid recreating them
        for multiple corpus collections on the same variant.

        Args:
            adapter: MetaYamlAdapter with benchmark configuration.
            variant_name: Name of the built variant.

        Returns:
            CoverageStrategy instance.
        """
        if variant_name not in self._strategies:
            self._strategies[variant_name] = create_coverage_strategy(
                oss_fuzz_path=self.oss_fuzz_path,
                project_name=variant_name,
                language=adapter.lang,
                work_dir=self.work_dir,
            )
        return self._strategies[variant_name]

    def _load_adapter(self, benchmark_path: Path) -> Optional[MetaYamlAdapter]:
        """Load MetaYamlAdapter from benchmark path.

        Uses MetaYamlAdapter.from_benchmark_path() for consistency
        across all engines (POV, Patch, Coverage).

        Args:
            benchmark_path: Path to benchmark directory.

        Returns:
            MetaYamlAdapter or None if loading fails.
        """
        return MetaYamlAdapter.from_benchmark_path(benchmark_path)

    def _compute_summary(
        self,
        merged_coverage: dict,
        corpus_count: int,
        contributing_count: int,
        unique_count: int,
        totals: dict,
    ) -> CoverageSummary:
        """Compute coverage summary from merged data and totals.

        Converts line sets to sorted lists and computes aggregate statistics.
        Uses totals from batch coverage for accurate lines_total/functions_total.

        Args:
            merged_coverage: Merged coverage dict with line sets.
            corpus_count: Total number of corpus files.
            contributing_count: Number of corpus files that add new lines.
            unique_count: Number of corpus files with distinct coverage profiles.
            totals: Dict with totals from parse_llvm_cov_summary().

        Returns:
            CoverageSummary with aggregate statistics.
        """
        # Convert sets back to sorted lists
        for func_data in merged_coverage.values():
            if isinstance(func_data.get("lines"), set):
                func_data["lines"] = sorted(func_data["lines"])

        # Use totals from batch coverage (accurate)
        lines_covered = int(totals.get("lines_covered", 0))
        lines_total = int(totals.get("lines_total", 0))
        lines_percent = float(totals.get("lines_percent", 0.0))
        functions_covered = int(totals.get("functions_covered", 0))
        functions_total = int(totals.get("functions_total", 0))

        return CoverageSummary(
            metric="line",
            corpus_total=corpus_count,
            corpus_contributing=contributing_count,
            corpus_unique=unique_count,
            lines_covered=lines_covered,
            lines_total=lines_total,
            lines_percent=lines_percent,
            functions_covered=functions_covered,
            functions_total=functions_total,
        )

    def cleanup(self) -> None:
        """Clean up temporary resources.

        Clears cached strategies. Strategies manage their own temp directories
        internally via tempfile.TemporaryDirectory.
        Same pattern as POV/Patch engines.
        """
        self._strategies.clear()
        logger.debug("CoverageEngine cleanup complete")
