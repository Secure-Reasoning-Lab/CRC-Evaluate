"""Coverage engine for orchestrating coverage collection.

This module provides the CoverageEngine for collecting code coverage
from corpus files against benchmark projects, following the same
patterns as VerificationEngine (POV) and PatchVerificationEngine.

Architecture:
- Uses Atlantis/given_fuzzer `oss-crs build-target` for coverage builds
- Processes corpus files sequentially (parallelism handled by warm runners)
- Uses MetaYamlAdapter for consistent config loading
- Provides cleanup() method for resource management

Usage:
    engine = CoverageEngine(build_workers=4)
    try:
        report = engine.collect_coverage(
            benchmark_path=Path("benchmarks/my-project"),
            corpus_dir=Path("/path/to/corpus"),
        )
    finally:
        engine.cleanup()
"""

import fcntl
import inspect
import json
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager, nullcontext
from datetime import datetime
from pathlib import Path
from typing import Optional

from crsbench.builder.types import BuildConfig, VariantType
from crsbench.evaluation.coverage.backend import CoverageRunResult, CoverageSession
from crsbench.evaluation.coverage.models import (
    CoverageReport,
    CoverageSummary,
    TimedCoverageInput,
)
from crsbench.evaluation.coverage.strategy import (
    CoverageStrategy,
    CoverageStrategyError,
    create_coverage_strategy,
)
from crsbench.evaluation.coverage.uniafl_runtime import (
    build_atlantis_coverage_artifacts,
)
from crsbench.prepare.uniafl_backend import (
    current_prepare_image_ids,
    current_uniafl_checkout_fingerprint,
)
from crsbench.utils.docker import fix_docker_ownership
from crsbench.utils.logger import get_logger
from crsbench.utils.workers import resolve_build_workers
from crsbench.validation.meta_adapter import MetaYamlAdapter

logger = get_logger(__name__)
UNIAFL_BUILD_SENTINEL = ".crsbench-uniafl-build.json"
BUILD_METADATA_FILE = ".build-meta.json"


class _CoverageBuildWorkspace:
    """Coverage-local build/cache workspace for Atlantis artifacts."""

    def __init__(
        self,
        *,
        legacy_root: Optional[Path],
        work_dir: Optional[Path],
    ) -> None:
        self.legacy_root = Path(legacy_root).resolve() if legacy_root else None
        self.work_dir = Path(work_dir).resolve() if work_dir else None
        self.default_root = (
            Path(__file__).resolve().parents[3] / ".crsbench-coverage"
        ).resolve()

    def get_build_output_path(self, variant_name: str) -> Path:
        if self.work_dir:
            return self.work_dir / variant_name / "build" / "out"
        if self.legacy_root:
            return self.legacy_root / "build" / "out" / variant_name
        return self.default_root / variant_name / "build" / "out"

    def get_control_root(self, variant_name: str) -> Path:
        if self.work_dir:
            return self.work_dir / variant_name / "oss-crs"
        if self.legacy_root:
            return (
                self.get_build_output_path(variant_name).parent
                / f".{variant_name}-oss-crs"
            )
        return self.default_root / variant_name / "oss-crs"

    def has_harness(self, variant_name: str, harness_name: str) -> bool:
        harness_path = self.get_build_output_path(variant_name) / harness_name
        return harness_path.exists() and harness_path.is_file()

    def cleanup_build_outputs(self, variant_name: str) -> None:
        targets = [
            self.get_build_output_path(variant_name),
            self.get_control_root(variant_name),
        ]
        for target in targets:
            if not target.exists() and not target.is_symlink():
                continue
            try:
                if target.exists():
                    fix_docker_ownership(target)
            except Exception:
                pass
            if target.is_symlink() or target.is_file():
                target.unlink(missing_ok=True)
                continue
            shutil.rmtree(target, ignore_errors=True)

    def write_build_metadata(
        self,
        variant_name: str,
        *,
        inc_build: bool = False,
        sanitizer: str = "address",
        fallback_used: bool = False,
    ) -> None:
        build_path = self.get_build_output_path(variant_name)
        build_path.mkdir(parents=True, exist_ok=True)
        metadata = {
            "inc_build": inc_build,
            "sanitizer": sanitizer,
            "timestamp": datetime.now().isoformat(),
            "fallback_used": fallback_used,
        }
        metadata_path = build_path / BUILD_METADATA_FILE
        temp_path = metadata_path.with_suffix(".tmp")
        temp_path.write_text(json.dumps(metadata, indent=2))
        temp_path.replace(metadata_path)


class CoverageEngine:
    """Engine for orchestrating coverage collection.

    Coordinates the entire coverage workflow:
    1. Load benchmark configuration via MetaYamlAdapter
    2. Build coverage variant (with build_workers)
    3. Collect coverage from corpus files (sequentially)
    4. Merge and deduplicate coverage results
    5. Generate coverage reports

    This follows the same architectural pattern as VerificationEngine (POV)
    and PatchVerificationEngine. Parallelism is handled by Redis job queue at
    the benchmark level, not within this engine.

    Attributes:
        oss_fuzz_path: Optional legacy compatibility root for coverage build cache.
        workspace: Coverage-local build workspace for Atlantis artifacts.
        build_workers: Number of parallel workers for building.
    """

    def __init__(
        self,
        oss_fuzz_path: Optional[Path] = None,
        *,
        build_workers: Optional[int] = None,
        runtime_workers: Optional[int] = None,
        runtime_cpus: Optional[list[int]] = None,
        work_dir: Optional[Path] = None,
        source_mode: str = "pkgs",
    ):
        """Initialize the coverage engine.

        Args:
            oss_fuzz_path: Optional legacy cache root. Coverage analysis no longer
                requires a real OSS-Fuzz checkout.
            build_workers: Number of parallel workers for building (default: 4).
            work_dir: Working directory for isolated builds. If None, uses
                default oss-fuzz/build/out/ location.
            source_mode: Source mode - "pkgs" (bundled, default) or "main_repo" (clone)
        """
        self.oss_fuzz_path = Path(oss_fuzz_path).resolve() if oss_fuzz_path else None
        self.work_dir = Path(work_dir) if work_dir else None
        self.build_workers = resolve_build_workers(build_workers)
        self.runtime_workers = max(1, int(runtime_workers or 1))
        self.runtime_cpus = list(runtime_cpus) if runtime_cpus else None
        self.source_mode = source_mode
        self.workspace = _CoverageBuildWorkspace(
            legacy_root=self.oss_fuzz_path,
            work_dir=self.work_dir,
        )
        self.infra = self.workspace

        # Cache for strategies (keyed by variant_name)
        self._strategies: dict[str, CoverageStrategy] = {}

        # Thread safety for coverage merging and line set tracking
        # (kept for external thread safety if engine is used from multiple threads)
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
        use_inc_build: bool = False,
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
                success=False,
            )

        # Determine harness
        harness_names = (
            [harness_filter] if harness_filter else adapter.get_harness_names()
        )

        if not harness_names:
            logger.error("No harnesses found in benchmark")
            return CoverageReport(
                harness_name="", final_summary=CoverageSummary(), success=False
            )

        harness_name = harness_names[0]  # Use first harness
        logger.info(f"Using harness: {harness_name}")

        # Build coverage variant
        build_start = time.time()
        variant_name = self._build_coverage_variant(
            adapter, force_rebuild=force_rebuild, use_inc_build=use_inc_build
        )
        build_elapsed = time.time() - build_start
        if not variant_name:
            logger.error("Failed to build coverage variant")
            return CoverageReport(
                harness_name=harness_name,
                final_summary=CoverageSummary(),
                success=False,
            )

        # Verify harness exists in build
        if not self.workspace.has_harness(variant_name, harness_name):
            logger.error(
                f"Harness '{harness_name}' not found in build output for {variant_name}"
            )
            return CoverageReport(
                harness_name=harness_name,
                final_summary=CoverageSummary(),
                success=False,
            )

        # Create or get strategy
        strategy = self._get_or_create_strategy(adapter, variant_name)

        # Validate corpus directory
        if not corpus_dir.exists():
            logger.error(f"Corpus directory not found: {corpus_dir}")
            return CoverageReport(
                harness_name=harness_name,
                final_summary=CoverageSummary(),
                success=False,
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
                harness_name=harness_name,
                final_summary=CoverageSummary(),
                success=False,
            )

        logger.info(f"Collecting coverage for {len(corpus_files)} corpus files")

        verify_start = time.time()
        session_cm = self._maybe_open_session(strategy, harness_name)
        with session_cm as session:
            if session is None:
                msg = "Coverage analysis requires an Atlantis-backed coverage session"
                raise CoverageStrategyError(msg)
            merged_coverage, success_count, contributing_count, unique_count = (
                self._collect_coverage_sequential(
                    corpus_files,
                    session=session,
                )
            )
        verify_elapsed = time.time() - verify_start

        # Compute summary directly from merged per-seed coverage.
        summary = self._compute_summary(
            merged_coverage,
            len(corpus_files),
            contributing_count,
            unique_count,
        )

        logger.info(
            f"Coverage collection complete: {summary.format_lines()} lines, "
            f"{summary.format_functions()} functions, "
            f"{success_count}/{len(corpus_files)} corpus processed, "
            f"{contributing_count} contributing, {unique_count} unique"
        )

        return CoverageReport(
            harness_name=harness_name,
            final_summary=summary,
            build_time=build_elapsed,
            verify_time=verify_elapsed,
        )

    def collect_timed_line_coverage(
        self,
        benchmark_path: Path,
        timed_inputs: list[TimedCoverageInput],
        *,
        harness_filter: Optional[str] = None,
        force_rebuild: bool = False,
        use_inc_build: bool = False,
        output_dir: Optional[Path] = None,
    ) -> tuple[list[TimedCoverageInput], CoverageSummary]:
        """Collect line coverage for a timed set of normalized inputs.

        Builds the coverage variant once, measures coverage for each unique input,
        and annotates each input with cumulative line coverage at its timestamp.
        """
        benchmark_path = Path(benchmark_path)
        adapter = self._load_adapter(benchmark_path)
        if not adapter:
            msg = f"Failed to load benchmark adapter for {benchmark_path}"
            raise CoverageStrategyError(msg)

        harness_names = (
            [harness_filter] if harness_filter else adapter.get_harness_names()
        )
        if not harness_names:
            msg = f"No harnesses found in benchmark: {benchmark_path}"
            raise CoverageStrategyError(msg)
        harness_name = harness_names[0]

        variant_name = self._build_coverage_variant(
            adapter, force_rebuild=force_rebuild, use_inc_build=use_inc_build
        )
        if not variant_name:
            msg = f"Failed to build coverage variant for {benchmark_path}"
            raise CoverageStrategyError(msg)

        if not self.workspace.has_harness(variant_name, harness_name):
            msg = (
                f"Harness '{harness_name}' not found in build output for {variant_name}"
            )
            raise CoverageStrategyError(msg)

        strategy = self._get_or_create_strategy(adapter, variant_name)
        sorted_inputs = sorted(timed_inputs, key=lambda item: item.relative_time)

        merged: dict[str, dict] = {}
        cumulative_lines: set[tuple[str, int]] = set()
        contributing_count = 0
        unique_count = 0
        processed_inputs: list[TimedCoverageInput] = []

        self._seen_line_sets.clear()
        self._covered_lines.clear()

        session_cm = self._maybe_open_session(
            strategy, harness_name, output_dir=output_dir
        )
        with session_cm as session:
            if session is None:
                msg = "Coverage analysis requires an Atlantis-backed coverage session"
                raise CoverageStrategyError(msg)
            batch_results: dict[Path, CoverageRunResult] = {}
            try:
                batch_results = session.collect_many(
                    [timed_input.path for timed_input in sorted_inputs]
                )
            except Exception as e:
                logger.debug(f"Batch timed coverage collection failed: {e}")
                batch_results = {}
            for timed_input in sorted_inputs:
                result = batch_results.get(
                    timed_input.path
                ) or self._collect_single_result_safe(
                    timed_input.path,
                    session=session,
                )
                cov_data = result.coverage_data
                if not cov_data and not (
                    result.crashed
                    or result.raw_cov_path is not None
                    or result.crash_log_path is not None
                ):
                    continue
                if cov_data:
                    is_contributing, is_unique = self._track_corpus_coverage(cov_data)
                    if is_contributing:
                        contributing_count += 1
                    if is_unique:
                        unique_count += 1
                    self._merge_coverage_safe(merged, cov_data)
                    cumulative_lines.update(self._extract_line_locations(cov_data))
                processed_inputs.append(
                    timed_input.model_copy(
                        update={
                            "lines_covered": len(cumulative_lines),
                            "crashed": result.crashed,
                            "raw_cov_path": result.raw_cov_path,
                            "crash_log_path": result.crash_log_path,
                        }
                    )
                )

        if not processed_inputs:
            msg = (
                "Coverage collection failed for all inputs "
                f"for {benchmark_path.name}/{harness_name}"
            )
            raise CoverageStrategyError(msg)
        summary = self._compute_summary(
            merged_coverage=merged,
            corpus_count=len(processed_inputs),
            contributing_count=contributing_count,
            unique_count=unique_count,
        )
        summary = summary.model_copy(
            update={
                "corpus_total": len(processed_inputs),
                "corpus_contributing": contributing_count,
                "corpus_unique": unique_count,
            }
        )
        logger.info(
            f"Timed coverage complete for {benchmark_path.name}/{harness_name}: "
            f"{len(processed_inputs)}/{len(sorted_inputs)} inputs, "
            f"{summary.format_lines()} lines"
        )
        return (
            processed_inputs,
            summary.model_copy(),
        )

    def _maybe_open_session(
        self,
        strategy: CoverageStrategy,
        harness_name: str,
        *,
        output_dir: Optional[Path] = None,
    ):
        open_session = getattr(type(strategy), "open_session", None)
        if open_session is None or not callable(open_session):
            return nullcontext(None)
        if self.runtime_workers <= 1:
            cpu_set = (
                str(self.runtime_cpus[0])
                if self.runtime_cpus and len(self.runtime_cpus) >= 1
                else None
            )
            return self._open_strategy_session(
                strategy,
                harness_name,
                output_dir=output_dir,
                cpu_set=cpu_set,
            )

        if self.runtime_cpus and len(self.runtime_cpus) < self.runtime_workers:
            msg = (
                f"runtime_cpus has {len(self.runtime_cpus)} CPUs but "
                f"runtime_workers={self.runtime_workers}"
            )
            raise ValueError(msg)

        sessions: list[CoverageSession] = []
        opened_sessions: list[CoverageSession | None] = [
            None for _ in range(self.runtime_workers)
        ]
        try:
            with ThreadPoolExecutor(max_workers=self.runtime_workers) as executor:
                futures = {
                    executor.submit(
                        self._open_strategy_session,
                        strategy,
                        harness_name,
                        output_dir=output_dir,
                        cpu_set=(
                            str(self.runtime_cpus[index]) if self.runtime_cpus else None
                        ),
                        session_label=f"worker-{index}",
                    ): index
                    for index in range(self.runtime_workers)
                }
                for future in as_completed(futures):
                    index = futures[future]
                    opened_sessions[index] = future.result()
                sessions = [
                    session for session in opened_sessions if session is not None
                ]
        except Exception:
            for session in reversed(
                [session for session in opened_sessions if session is not None]
            ):
                session.close()
            raise

        from crsbench.evaluation.coverage.backend import ShardedCoverageSession

        return ShardedCoverageSession(sessions)

    def _open_strategy_session(
        self,
        strategy: CoverageStrategy,
        harness_name: str,
        **optional_kwargs,
    ):
        open_session = strategy.open_session
        try:
            signature = inspect.signature(open_session)
        except (TypeError, ValueError):
            signature = None

        supports_var_kwargs = False
        accepted_names: set[str] = set()
        if signature is not None:
            for parameter in signature.parameters.values():
                if parameter.kind is inspect.Parameter.VAR_KEYWORD:
                    supports_var_kwargs = True
                else:
                    accepted_names.add(parameter.name)

        kwargs = {}
        for key, value in optional_kwargs.items():
            if value is None:
                continue
            if signature is None or supports_var_kwargs or key in accepted_names:
                kwargs[key] = value
        return open_session(harness_name, **kwargs)

    def _collect_single_result_safe(
        self,
        corpus_file: Path,
        *,
        session: CoverageSession,
    ) -> CoverageRunResult:
        try:
            return session.collect_single(corpus_file)
        except CoverageStrategyError as e:
            logger.debug(f"Coverage session error for {corpus_file.name}: {e}")
            return CoverageRunResult(coverage_data={})
        except Exception as e:
            logger.debug(f"Unexpected error for {corpus_file.name}: {e}")
            return CoverageRunResult(coverage_data={})

    def _collect_coverage_sequential(
        self,
        corpus_files: list[Path],
        *,
        session: CoverageSession,
    ) -> tuple[dict, int, int, int]:
        """Collect coverage for multiple corpus files sequentially.

        Parallelism is handled by Redis job queue at the benchmark level,
        not within this method.

        Args:
            corpus_files: List of corpus file paths.
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

        batch_results: dict[Path, CoverageRunResult] = {}
        try:
            batch_results = session.collect_many(corpus_files)
        except Exception as e:
            logger.debug(f"Batch coverage collection failed: {e}")
            batch_results = {}

        completed = 0
        for corpus_file in corpus_files:
            if batch_results:
                cov_data = batch_results.get(
                    corpus_file, CoverageRunResult(coverage_data={})
                ).coverage_data
            else:
                cov_data = self._collect_single_result_safe(
                    corpus_file,
                    session=session,
                ).coverage_data
            if cov_data:
                is_contributing, is_unique = self._track_corpus_coverage(cov_data)
                if is_contributing:
                    contributing_count += 1
                if is_unique:
                    unique_count += 1
                self._merge_coverage_safe(merged, cov_data)
                success_count += 1
            completed += 1
            if completed % 100 == 0:
                logger.info(f"Processed {completed}/{len(corpus_files)} corpus files")

        return merged, success_count, contributing_count, unique_count

    @contextmanager
    def _acquire_build_lock(self, variant_name: str):
        lock_path = Path("/tmp") / f"crsbench-coverage-build-{variant_name}.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("w") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

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

    def _build_coverage_variant(
        self,
        adapter: MetaYamlAdapter,
        *,
        force_rebuild: bool = False,
        use_inc_build: bool = False,
    ) -> Optional[str]:
        """Build coverage variant for benchmark.

        Uses OSSFuzzBuilder to build the coverage-instrumented variant.

        Note: Coverage variants do not support inc-build (requires different
        instrumentation). The use_inc_build parameter is accepted for
        interface consistency but has no effect on coverage builds.

        Args:
            adapter: MetaYamlAdapter with benchmark configuration.
            force_rebuild: If True, clean and rebuild.
            use_inc_build: Accepted for consistency but not used for coverage.

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
            use_inc_build=use_inc_build,
        )

        logger.info(f"Building coverage variant: {config.variant_name}")

        variant_name = config.variant_name
        with self._acquire_build_lock(variant_name):
            if force_rebuild:
                self.workspace.cleanup_build_outputs(variant_name)

            build_output_dir = self.workspace.get_build_output_path(variant_name)
            build_output_dir.mkdir(parents=True, exist_ok=True)
            staged_repo_dir = build_output_dir / ".crsbench-repo"
            coverage_build_dir = build_output_dir / "coverage-out"
            build_sentinel = build_output_dir / UNIAFL_BUILD_SENTINEL
            has_existing_build = any(build_output_dir.iterdir())
            sentinel_data: dict[str, object] = {}
            if build_sentinel.exists():
                try:
                    sentinel_data = json.loads(build_sentinel.read_text())
                except json.JSONDecodeError:
                    sentinel_data = {}
            checkout_fingerprint = current_uniafl_checkout_fingerprint()
            prepare_image_ids = current_prepare_image_ids()
            if (
                not force_rebuild
                and staged_repo_dir.exists()
                and build_sentinel.exists()
                and has_existing_build
                and (adapter.lang == "jvm" or coverage_build_dir.exists())
                and sentinel_data.get("checkout_fingerprint") == checkout_fingerprint
                and sentinel_data.get("prepare_image_ids") == prepare_image_ids
            ):
                logger.info(f"Reusing existing UniAFL coverage build: {variant_name}")
                return variant_name

            control_root = self.workspace.get_control_root(variant_name)
            try:
                build = build_atlantis_coverage_artifacts(
                    benchmark_path=config.benchmark_path,
                    normalized_build_output_dir=build_output_dir,
                    control_root=control_root,
                )
            except Exception as exc:
                logger.error(
                    f"Atlantis coverage build failed for {variant_name}: {exc}"
                )
                return None

            build_sentinel.write_text(
                json.dumps(
                    {
                        "variant_name": variant_name,
                        "language": adapter.lang,
                        "sanitizer": "coverage",
                        "build_id": build.build_id,
                        "compose_file": str(build.compose_file),
                        "control_root": str(build.control_root),
                        "atlantis_build_output_dir": str(
                            build.atlantis_build_output_dir
                        ),
                        "checkout_fingerprint": checkout_fingerprint,
                        "prepare_image_ids": prepare_image_ids,
                    },
                    indent=2,
                )
            )

            self.workspace.write_build_metadata(
                variant_name,
                inc_build=False,
                sanitizer="coverage",
                fallback_used=False,
            )
            fix_docker_ownership(build_output_dir)
            logger.info(f"Coverage build succeeded: {variant_name}")
            return variant_name

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
                build_output_dir=self.workspace.get_build_output_path(variant_name),
                benchmark_path=adapter.benchmark_path,
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
    ) -> CoverageSummary:
        """Compute coverage summary directly from merged per-seed coverage.

        Converts line sets to sorted lists and computes aggregate statistics.
        The Atlantis per-seed replay path does not run a separate whole-corpus
        summary pass, so total coverable lines/functions are not known here.
        The summary therefore reports covered lines/functions only and leaves
        total/percent fields unset (zero) rather than inventing a narrower
        denominator from only the files touched by the replay.

        Args:
            merged_coverage: Merged coverage dict with line sets.
            corpus_count: Total number of corpus files.
            contributing_count: Number of corpus files that add new lines.
            unique_count: Number of corpus files with distinct coverage profiles.

        Returns:
            CoverageSummary with aggregate statistics.
        """
        merged_lines: set[tuple[str, int]] = set()
        functions_covered = 0

        for func_data in merged_coverage.values():
            if not isinstance(func_data, dict):
                continue
            src = str(func_data.get("src", ""))
            raw_lines = func_data.get("lines", [])
            normalized_lines = sorted({int(line) for line in raw_lines})
            func_data["lines"] = normalized_lines

            if normalized_lines:
                functions_covered += 1
            for line in normalized_lines:
                merged_lines.add((src, line))

        lines_covered = len(merged_lines)
        lines_total = 0
        lines_percent = 0.0
        functions_total = 0

        return CoverageSummary(
            metric="line",
            corpus_total=corpus_count,
            corpus_contributing=contributing_count,
            corpus_unique=unique_count,
            lines_covered=lines_covered,
            lines_total=lines_total,
            lines_percent=lines_percent,
            totals_available=False,
            functions_covered=functions_covered,
            functions_total=functions_total,
        )

    def _extract_line_locations(self, cov_data: dict) -> set[tuple[str, int]]:
        """Extract covered line locations from coverage data."""
        line_locations: set[tuple[str, int]] = set()
        for func_data in cov_data.values():
            if not isinstance(func_data, dict):
                continue
            src = func_data.get("src", "")
            for line in func_data.get("lines", []):
                line_locations.add((src, line))
        return line_locations

    def cleanup(self) -> None:
        """Clean up temporary resources.

        Clears cached strategies. Strategies manage their own temp directories
        internally via tempfile.TemporaryDirectory.
        Same pattern as POV/Patch engines.
        """
        self._strategies.clear()
        logger.debug("CoverageEngine cleanup complete")
