"""VerificationEngine for orchestrating POV validation.

This module provides the main orchestrator for POV verification:
1. Load benchmark configuration
2. Build all required variants (parallel)
3. Run POVs against each variant (parallel)
4. Collect crash results
5. Resolve verdicts
6. Deduplicate results
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from crsbench.builder import BenchmarkMode, BuildResult, OSSFuzzBuilder, VariantType
from crsbench.evaluation.verification.dedup import (
    DeduplicationStrategy,
    PatchBasedDedup,
)
from crsbench.evaluation.verification.models import (
    PovBenchmarkOutput,
    PovVerificationRequest,
    PovVerificationResult,
    PovVerificationStatus,
)
from crsbench.evaluation.verification.pov.verdict import VerdictResolver
from crsbench.evaluation.verification.utils import compute_content_hash
from crsbench.utils import strip_ansi
from crsbench.utils.logger import get_logger
from crsbench.utils.workers import resolve_build_workers, resolve_verify_workers

if TYPE_CHECKING:
    from crsbench.validation.meta_adapter import MetaYamlAdapter

logger = get_logger(__name__)


@dataclass
class ReproduceTask:
    """Task for parallel reproduce execution."""

    pov_id: str
    pov_data: bytes
    harness: str
    variant_name: str
    variant_type: VariantType
    cpv_num: Optional[int]
    sanitizer: str = "address"  # Sanitizer for this variant


@dataclass
class ReproduceResult:
    """Result from a single reproduce call."""

    pov_id: str
    harness: str
    variant_name: str
    variant_type: VariantType
    cpv_num: Optional[int]
    crashed: bool
    crash_log: str = ""  # stdout from reproduce
    stderr: str = ""  # stderr from reproduce


class VerificationEngine:
    """Engine for orchestrating POV verification.

    Coordinates the entire verification workflow:
    1. Load and parse benchmark configuration
    2. Build all required variant projects (parallel with build_workers)
    3. Run POVs against each variant (parallel with verify_workers)
    4. Collect and analyze crash results
    5. Determine final verdicts
    6. Apply deduplication

    Attributes:
        oss_fuzz_path: Path to oss-fuzz directory
        builder: OSSFuzzBuilder instance
        dedup_strategy: Deduplication strategy to use
        timeout: Timeout for reproduce operations
        verify_workers: Number of parallel workers for verification
    """

    def __init__(
        self,
        oss_fuzz_path: Path,
        timeout: int = 180,
        dedup_strategy: DeduplicationStrategy | None = None,
        build_workers: Optional[int] = None,
        verify_workers: Optional[int] = None,
        *,
        source_mode: str = "pkgs",
        max_povs_per_cpv: Optional[int] = None,
    ):
        """Initialize the verification engine.

        Args:
            oss_fuzz_path: Path to oss-fuzz directory
            timeout: Timeout for reproduce operations in seconds
            dedup_strategy: Deduplication strategy instance (defaults to PatchBasedDedup)
            build_workers: Maximum number of parallel workers for building
            verify_workers: Maximum number of parallel workers for verification
            source_mode: Source mode - "pkgs" (bundled, default) or "main_repo" (clone)
            max_povs_per_cpv: Limit POVs verified per CPV (None = no limit).
                When set to 1, only pov_0.blob is used per CPV.
        """
        self.oss_fuzz_path = Path(oss_fuzz_path)
        self.timeout = timeout
        self.verify_workers = resolve_verify_workers(verify_workers)
        self.max_povs_per_cpv = max_povs_per_cpv
        self.builder = OSSFuzzBuilder(
            oss_fuzz_path,
            max_workers=resolve_build_workers(build_workers),
            source_mode=source_mode,
        )
        self.dedup_strategy = dedup_strategy if dedup_strategy else PatchBasedDedup()
        self._built_results: dict[str, dict[str, BuildResult]] = {}

    def _log_crash_summary(
        self, pov_id: str, crash_logs: dict[str, str], max_lines: int = 30
    ) -> None:
        """Log crash summary for debugging UNINTENDED_CRASH.

        Extracts and logs key crash information from crash logs.

        Args:
            pov_id: POV identifier
            crash_logs: Dict mapping variant_name -> crash_log
            max_lines: Maximum lines to show per variant
        """
        for variant_name, crash_log in crash_logs.items():
            if not crash_log:
                continue

            summary_lines = self._extract_crash_summary(crash_log, max_lines)

            if summary_lines:
                summary = "\n".join(summary_lines)
                logger.warning(f"[{pov_id}] Crash on {variant_name}:\n{summary}")

    def _extract_crash_summary(self, crash_log: str, max_lines: int = 30) -> list[str]:
        """Extract crash summary from log.

        Patterns matched (in order):
        1. Java Exception: "== Java Exception:" line and stack trace
        2. ASAN/Sanitizer: "==N==ERROR: AddressSanitizer" and SUMMARY
        3. libFuzzer OOM: "libFuzzer: out-of-memory" or "SUMMARY: libFuzzer"
        4. General crash: Last N lines as fallback

        Args:
            crash_log: Full crash log text
            max_lines: Maximum lines to extract

        Returns:
            List of summary lines
        """
        lines = crash_log.splitlines()
        summary_lines: list[str] = []

        for i, line in enumerate(lines):
            # Java Exception from Jazzer (high priority - specific pattern)
            if "== Java Exception:" in line:
                end = min(len(lines), i + 15)
                summary_lines = lines[i:end]
                break

            # Regular Java exception (e.g., "Exception in thread "main"")
            if line.startswith("Exception in thread "):
                end = min(len(lines), i + 15)
                summary_lines = lines[i:end]
                break

            # ASAN/Sanitizer error (specific pattern with process ID)
            if "==ERROR: AddressSanitizer" in line or "==ERROR: LeakSanitizer" in line:
                start = max(0, i - 1)
                # Find SUMMARY line
                summary_idx = i
                for j in range(i, min(len(lines), i + 30)):
                    if "SUMMARY:" in lines[j]:
                        summary_idx = j
                        break
                end = min(len(lines), summary_idx + 2)
                summary_lines = lines[start:end]
                break

            # libFuzzer OOM (specific pattern)
            if "ERROR: libFuzzer: out-of-memory" in line:
                start = max(0, i - 5)
                end = min(len(lines), i + 3)
                summary_lines = lines[start:end]
                break

            # libFuzzer timeout (specific pattern)
            if "ERROR: libFuzzer: timeout" in line:
                start = max(0, i - 2)
                # Find SUMMARY line
                summary_idx = i
                for j in range(i, min(len(lines), i + 20)):
                    if "SUMMARY:" in lines[j]:
                        summary_idx = j
                        break
                end = min(len(lines), summary_idx + 2)
                summary_lines = lines[start:end]
                break

            # SUMMARY line as fallback for sanitizers
            if line.startswith("SUMMARY:"):
                start = max(0, i - 10)
                end = min(len(lines), i + 2)
                summary_lines = lines[start:end]
                break

        # If no patterns found, use last N lines
        if not summary_lines:
            summary_lines = lines[-max_lines:]

        # Truncate if too long
        if len(summary_lines) > max_lines:
            summary_lines = summary_lines[:max_lines]
            summary_lines.append("... (truncated)")

        return summary_lines

    def _execute_reproduce(self, task: ReproduceTask) -> ReproduceResult:
        """Execute a single reproduce task.

        Args:
            task: ReproduceTask with POV and variant info

        Returns:
            ReproduceResult with crash status and crash log
        """
        output = self.builder.infra.reproduce(
            project_name=task.variant_name,
            harness=task.harness,
            pov_data=task.pov_data,
            timeout=self.timeout,
            pov_id=task.pov_id,
        )
        # Strip ANSI escape codes for cleaner storage
        stdout = strip_ansi(output.stdout) if output.stdout else ""
        stderr = strip_ansi(output.stderr) if output.stderr else ""

        return ReproduceResult(
            pov_id=task.pov_id,
            harness=task.harness,
            variant_name=task.variant_name,
            variant_type=task.variant_type,
            cpv_num=task.cpv_num,
            crashed=output.crashed,
            crash_log=stdout,
            stderr=stderr,
        )

    def verify_pov(
        self,
        request: PovVerificationRequest,
        adapter: MetaYamlAdapter,
        build_results: Optional[dict[str, BuildResult]] = None,
    ) -> PovVerificationResult:
        """Verify a single POV against all variants.

        Runs reproduce() calls in parallel for all variants.

        Args:
            request: Verification request with POV data
            adapter: MetaYamlAdapter for benchmark config
            build_results: Optional pre-built results (for efficiency)

        Returns:
            PovVerificationResult with determined status
        """
        # Get or build variants
        if build_results is None:
            build_results = self.get_or_build_results(adapter)

        if not build_results:
            return PovVerificationResult(
                status=PovVerificationStatus.ERROR,
                benchmark=adapter.benchmark_name,
                pov_id=request.pov_id,
                details="No built versions available",
            )

        # Create tasks for parallel execution
        pov_id = request.pov_id or "unknown"
        tasks = []
        for variant_name, result in build_results.items():
            if not result.success:
                continue
            tasks.append(
                ReproduceTask(
                    pov_id=pov_id,
                    pov_data=request.pov_data,
                    harness=request.harness,
                    variant_name=variant_name,
                    variant_type=result.config.variant_type,
                    cpv_num=result.config.cpv_num,
                )
            )

        # Execute reproduce calls in parallel
        crash_results: dict[VariantType, bool] = {}
        cpv_crash_map: dict[int, bool] = {}
        crash_logs: dict[str, str] = {}  # variant_name -> crash_log

        for task in tasks:
            result = self._execute_reproduce(task)
            if result.variant_type == VariantType.CPV and result.cpv_num is not None:
                cpv_crash_map[result.cpv_num] = result.crashed
            else:
                crash_results[result.variant_type] = result.crashed

            # Collect crash log if crashed
            if result.crashed and result.crash_log:
                crash_logs[result.variant_name] = result.crash_log

            logger.debug(
                f"{result.variant_name}: {'crashed' if result.crashed else 'ok'}"
            )

        # Determine mode for verdict resolution
        mode_str = adapter.get_mode().value
        mode = BenchmarkMode.FULL if mode_str == "full" else BenchmarkMode.DELTA

        # Resolve verdict
        verdict = VerdictResolver.resolve(
            mode=mode,
            crash_results=crash_results,
            cpv_crash_map=cpv_crash_map,
            benchmark_name=adapter.benchmark_name,
            pov_id=request.pov_id,
        )

        # Attach crash logs to result
        if crash_logs:
            verdict.crash_info = {"logs": crash_logs}

        return verdict

    def verify_povs_parallel(
        self,
        pov_harness_pairs: list[tuple[str, bytes, str]],  # (pov_id, pov_data, harness)
        adapter: MetaYamlAdapter,
        build_results: dict[str, BuildResult],
    ) -> list[PovVerificationResult]:
        """Verify multiple POVs in parallel.

        Flattens all (pov, harness, variant) combinations and runs them in parallel,
        then groups results to resolve verdicts.

        Args:
            pov_harness_pairs: List of (pov_id, pov_data, harness) tuples
            adapter: MetaYamlAdapter for benchmark config
            build_results: Pre-built variant results

        Returns:
            List of PovVerificationResult for each (pov, harness) pair
        """
        # Create all tasks (flattened)
        tasks: list[ReproduceTask] = []
        for pov_id, pov_data, harness in pov_harness_pairs:
            for variant_name, result in build_results.items():
                if not result.success:
                    continue
                tasks.append(
                    ReproduceTask(
                        pov_id=pov_id,
                        pov_data=pov_data,
                        harness=harness,
                        variant_name=variant_name,
                        variant_type=result.config.variant_type,
                        cpv_num=result.config.cpv_num,
                        sanitizer=result.config.sanitizer,  # Pass sanitizer from build config
                    )
                )

        if not tasks:
            return []

        logger.debug(
            f"Running {len(tasks)} reproduce tasks in parallel "
            f"({len(pov_harness_pairs)} POVs × {len([r for r in build_results.values() if r.success])} variants)"
        )

        # Execute all reproduce calls in parallel
        # Track crash results, cpv crash map, stdout, and stderr per (pov, harness)
        results_by_pov_harness: dict[
            tuple[str, str],
            tuple[
                dict[VariantType, bool],
                dict[int, bool],
                dict[str, str],
                dict[str, str],
            ],
        ] = defaultdict(lambda: ({}, {}, {}, {}))

        start_time = time.time()
        completed = 0
        last_report_time = start_time
        report_interval = 60  # Report every minute for parallel execution

        for task in tasks:
            result = self._execute_reproduce(task)
            key = (result.pov_id, result.harness)
            crash_results, cpv_crash_map, stdout_logs, stderr_logs = (
                results_by_pov_harness[key]
            )

            if result.variant_type == VariantType.CPV and result.cpv_num is not None:
                cpv_crash_map[result.cpv_num] = result.crashed
            else:
                crash_results[result.variant_type] = result.crashed

            # Collect stdout/stderr logs (always capture for debugging)
            if result.crash_log:
                stdout_logs[result.variant_name] = result.crash_log
            if result.stderr:
                stderr_logs[result.variant_name] = result.stderr

            # Progress reporting
            completed += 1
            current_time = time.time()
            if current_time - last_report_time >= report_interval:
                elapsed = current_time - start_time
                rate = completed / elapsed if elapsed > 0 else 0
                remaining = (len(tasks) - completed) / rate if rate > 0 else 0
                logger.debug(
                    f"Progress: {completed}/{len(tasks)} reproduce calls "
                    f"({elapsed:.0f}s elapsed, ~{remaining:.0f}s remaining)"
                )
                last_report_time = current_time

        # Determine mode for verdict resolution
        mode_str = adapter.get_mode().value
        mode = BenchmarkMode.FULL if mode_str == "full" else BenchmarkMode.DELTA

        # Resolve verdicts for each (pov, harness) pair
        verification_results = []
        for (pov_id, _harness), (
            crash_results,
            cpv_crash_map,
            stdout_logs,
            stderr_logs,
        ) in results_by_pov_harness.items():
            verdict = VerdictResolver.resolve(
                mode=mode,
                crash_results=crash_results,
                cpv_crash_map=cpv_crash_map,
                benchmark_name=adapter.benchmark_name,
                pov_id=pov_id,
            )
            # Attach stdout/stderr logs to result
            crash_info: dict[str, dict[str, str]] = {}
            if stdout_logs:
                crash_info["stdout"] = stdout_logs
            if stderr_logs:
                crash_info["stderr"] = stderr_logs
            if crash_info:
                verdict.crash_info = crash_info

            # Log crash summary for UNINTENDED_CRASH to help debugging
            if verdict.status == PovVerificationStatus.UNINTENDED_CRASH and stdout_logs:
                self._log_crash_summary(pov_id, stdout_logs)

            verification_results.append(verdict)

        elapsed = time.time() - start_time
        logger.debug(
            f"Completed {len(tasks)} reproduce calls in {elapsed:.1f}s "
            f"({len(tasks) / elapsed:.1f} calls/sec)"
        )

        return verification_results

    def verify_all_povs(
        self,
        adapter: MetaYamlAdapter,
        pov_dir: Path,
        harness_filter: Optional[str] = None,
        *,
        deduplicate: bool = True,
        skip_hashes: set[str] | None = None,
    ) -> tuple[list[PovVerificationResult], int]:
        """Verify all POVs in a directory against a benchmark.

        Uses parallel execution for all reproduce() calls.

        Args:
            adapter: MetaYamlAdapter for benchmark config
            pov_dir: Directory containing POV files
            harness_filter: Optional harness name to filter
            deduplicate: Whether to deduplicate results
            skip_hashes: Optional set of content hashes to skip (pre-verification dedup)

        Returns:
            Tuple of (list of verification results, number of POVs skipped due to hash)
        """
        # Build variants once
        build_results = self.get_or_build_results(adapter)
        if not build_results:
            logger.error(f"Failed to build variants for {adapter.benchmark_name}")
            return [], 0

        # Find all POV files (exclude dotfiles)
        pov_files = [
            f
            for f in list(pov_dir.glob("*.bin")) + list(pov_dir.glob("*.pov"))
            if not f.name.startswith(".")
        ]
        if not pov_files:
            logger.warning(f"No POV files found in {pov_dir}")
            return [], 0

        # Get harness names to test
        harness_names = (
            [harness_filter] if harness_filter else adapter.get_harness_names()
        )

        # Filter out POVs with hashes in skip_hashes (pre-verification dedup)
        skipped_count = 0
        if skip_hashes:
            filtered_pov_files = []
            for pov_file in pov_files:
                pov_hash = compute_content_hash(pov_file)
                if pov_hash in skip_hashes:
                    logger.debug(
                        f"Skipping POV {pov_file.name} (hash {pov_hash} already tested)"
                    )
                    skipped_count += 1
                else:
                    filtered_pov_files.append(pov_file)
            pov_files = filtered_pov_files

        if not pov_files:
            if skipped_count > 0:
                logger.debug(f"All {skipped_count} POVs skipped (already tested)")
            else:
                logger.warning(f"No POV files found in {pov_dir}")
            return [], skipped_count

        logger.debug(
            f"Verifying {len(pov_files)} POVs × {len(harness_names)} harnesses "
            f"against {adapter.benchmark_name}"
            + (
                f" (skipped {skipped_count} already tested)"
                if skipped_count > 0
                else ""
            )
        )

        # Create (pov_id, pov_data, harness) tuples
        pov_harness_pairs = []
        for pov_file in pov_files:
            pov_data = pov_file.read_bytes()
            for harness_name in harness_names:
                pov_harness_pairs.append((pov_file.name, pov_data, harness_name))

        # Run parallel verification
        results = self.verify_povs_parallel(pov_harness_pairs, adapter, build_results)

        # Deduplicate if requested
        if deduplicate and results:
            original_count = len(results)
            results = self.dedup_strategy.deduplicate(results)
            if len(results) < original_count:
                logger.debug(
                    f"Deduplicated: {original_count} -> {len(results)} results "
                    f"(using {self.dedup_strategy.name})"
                )

        return results, skipped_count

    def verify_benchmark(
        self,
        benchmark_path: Path,
        pov_dir: Optional[Path] = None,
        harness_filter: Optional[str] = None,
        *,
        force_rebuild: bool = False,
        deduplicate: bool = True,
        skip_hashes: set[str] | None = None,
        use_inc_build: Optional[bool] = None,
    ) -> PovBenchmarkOutput:
        """Verify POVs for a complete benchmark.

        Uses parallel execution for all reproduce() calls.

        Args:
            benchmark_path: Path to benchmark project directory
            pov_dir: Optional POV directory (overrides auto-discovery)
            harness_filter: Optional harness name to filter
            force_rebuild: Force rebuild of variants
            deduplicate: Whether to deduplicate results
            skip_hashes: Optional set of content hashes to skip (pre-verification dedup)
            use_inc_build: Override incremental build setting (None uses project.yaml default)

        Returns:
            PovBenchmarkOutput containing:
            - results: list of verification results
            - skipped_count: number of POVs skipped due to hash
            - fallback_used: True if any build used inc-build fallback
        """
        # Load benchmark configuration
        adapter = self.load_adapter(benchmark_path)
        if not adapter:
            return PovBenchmarkOutput(results=[], skipped_count=0, fallback_used=False)

        # Build variants if needed
        if force_rebuild:
            self._built_results.pop(adapter.benchmark_name, None)

        build_start = time.time()
        build_results = self.get_or_build_results(
            adapter, force_rebuild=force_rebuild, use_inc_build=use_inc_build
        )
        build_elapsed = time.time() - build_start
        if not build_results:
            logger.error(f"Failed to build variants for {adapter.benchmark_name}")
            return PovBenchmarkOutput(results=[], skipped_count=0, fallback_used=False)

        # Check if any inc-build target variant used fallback
        fallback_used = any(
            r.fallback_used
            for r in build_results.values()
            if r.config.variant_type.is_inc_build_target()
        )

        # Collect (pov_id, pov_data, harness) tuples
        pov_harness_pairs: list[tuple[str, bytes, str]] = []
        skipped_count = 0

        if pov_dir:
            # Use explicit POV directory (override)
            harness_names = (
                [harness_filter] if harness_filter else adapter.get_harness_names()
            )

            pov_files = [
                f
                for f in pov_dir.glob("*")
                if f.is_file() and not f.name.startswith(".")
            ]

            # Filter out POVs with hashes in skip_hashes (pre-verification dedup)
            if skip_hashes:
                filtered_pov_files = []
                for pov_file in pov_files:
                    pov_hash = compute_content_hash(pov_file)
                    if pov_hash in skip_hashes:
                        logger.debug(
                            f"Skipping POV {pov_file.name} "
                            f"(hash {pov_hash} already tested)"
                        )
                        skipped_count += 1
                    else:
                        filtered_pov_files.append(pov_file)
                pov_files = filtered_pov_files

            for pov_file in pov_files:
                pov_data = pov_file.read_bytes()
                for harness_name in harness_names:
                    pov_harness_pairs.append((pov_file.name, pov_data, harness_name))
        else:
            # Discover POVs from meta.yaml via adapter
            all_povs = adapter.get_all_pov_paths()

            # Filter by harness if specified
            if harness_filter:
                all_povs = [(h, v, p) for h, v, p in all_povs if h == harness_filter]

            # Limit POVs per CPV (already sorted by cpv_num, pov_num)
            if self.max_povs_per_cpv:
                filtered: list[tuple[str, str, Path]] = []
                cpv_counts: dict[str, int] = {}
                for harness_name, vuln_keyword, pov_path in all_povs:
                    count = cpv_counts.get(vuln_keyword, 0)
                    if count < self.max_povs_per_cpv:
                        filtered.append((harness_name, vuln_keyword, pov_path))
                        cpv_counts[vuln_keyword] = count + 1
                skipped_by_limit = len(all_povs) - len(filtered)
                if skipped_by_limit > 0:
                    logger.info(
                        f"Limiting to {self.max_povs_per_cpv} POV(s) per CPV: "
                        f"using {len(filtered)}, skipped {skipped_by_limit}"
                    )
                all_povs = filtered

            if not all_povs:
                logger.warning("No POVs found in benchmark meta.yaml")
                return PovBenchmarkOutput(
                    results=[], skipped_count=0, fallback_used=False
                )

            # Filter out POVs with hashes in skip_hashes (pre-verification dedup)
            for harness_name, vuln_keyword, pov_path in all_povs:
                if skip_hashes:
                    pov_hash = compute_content_hash(pov_path)
                    if pov_hash in skip_hashes:
                        logger.debug(
                            f"Skipping POV {pov_path.name} "
                            f"(hash {pov_hash} already tested)"
                        )
                        skipped_count += 1
                        continue
                pov_data = pov_path.read_bytes()
                # Include vuln_keyword in pov_id to track POV→CPV correspondence
                # e.g., "cpv_0/pov_0.blob" instead of just "pov_0.blob"
                pov_id = f"{vuln_keyword}/{pov_path.name}"
                pov_harness_pairs.append((pov_id, pov_data, harness_name))

        if not pov_harness_pairs:
            if skipped_count > 0:
                logger.debug(f"All {skipped_count} POVs skipped (already tested)")
            else:
                logger.warning("No POVs to verify")
            return PovBenchmarkOutput(
                results=[], skipped_count=skipped_count, fallback_used=fallback_used
            )

        logger.debug(
            f"Verifying {len(pov_harness_pairs)} POV-harness pairs "
            f"against {adapter.benchmark_name}"
            + (
                f" (skipped {skipped_count} already tested)"
                if skipped_count > 0
                else ""
            )
        )

        # Run parallel verification
        verify_start = time.time()
        results = self.verify_povs_parallel(pov_harness_pairs, adapter, build_results)
        verify_elapsed = time.time() - verify_start

        # Deduplicate if requested
        if deduplicate and results:
            original_count = len(results)
            results = self.dedup_strategy.deduplicate(results)
            if len(results) < original_count:
                logger.debug(
                    f"Deduplicated: {original_count} -> {len(results)} results "
                    f"(using {self.dedup_strategy.name})"
                )

        return PovBenchmarkOutput(
            results=results,
            skipped_count=skipped_count,
            fallback_used=fallback_used,
            build_time=build_elapsed,
            verify_time=verify_elapsed,
        )

    def get_or_build_results(
        self,
        adapter: MetaYamlAdapter,
        *,
        force_rebuild: bool = False,
        use_inc_build: Optional[bool] = None,
    ) -> dict[str, BuildResult]:
        """Get cached build results or build new ones.

        Args:
            adapter: MetaYamlAdapter for benchmark config
            force_rebuild: Force rebuild even if cached
            use_inc_build: Override incremental build setting (None uses adapter default)

        Returns:
            Dict mapping variant names to BuildResult
        """
        if not force_rebuild and adapter.benchmark_name in self._built_results:
            return self._built_results[adapter.benchmark_name]

        # Determine mode
        mode_str = adapter.get_mode().value
        mode = BenchmarkMode.FULL if mode_str == "full" else BenchmarkMode.DELTA

        # Use override if provided, otherwise use adapter's setting
        inc_build = use_inc_build if use_inc_build is not None else adapter.inc_build

        # Get sanitizer(s) from meta.yaml POV definitions
        # If multiple sanitizers, use the first one (CI handles multi-sanitizer properly)
        sanitizers = adapter.get_all_cpv_sanitizers()
        sanitizer = sanitizers[0]
        if len(sanitizers) > 1:
            logger.warning(
                f"{adapter.benchmark_name} uses multiple sanitizers: {sanitizers}. "
                f"POV verification will use {sanitizer}. "
                "Use 'crsbench ci' for full multi-sanitizer support."
            )

        # Create build plan
        plan = self.builder.create_build_plan(
            benchmark_name=adapter.benchmark_name,
            benchmark_path=adapter.benchmark_path or Path(),
            main_repo=adapter.main_repo,
            mode=mode,
            base_commit=adapter.get_base_commit(),
            ref_commit=adapter.get_ref_commit(),
            cpv_numbers=adapter.get_cpv_numbers(),
            language=adapter.lang,
            repo_name=adapter.repo_name,
            include_coverage=False,
            use_inc_build=inc_build,
            sanitizer=sanitizer,
        )

        # Build variants
        results = self.builder.execute_plan(plan, force_rebuild=force_rebuild)
        self._built_results[adapter.benchmark_name] = results
        return results

    def load_adapter(self, benchmark_path: Path) -> Optional[MetaYamlAdapter]:
        """Load MetaYamlAdapter from benchmark path.

        Args:
            benchmark_path: Path to benchmark directory

        Returns:
            MetaYamlAdapter or None if loading fails
        """
        # Lazy import to avoid circular dependencies
        from crsbench.validation.meta_adapter import MetaYamlAdapter

        meta_yaml = benchmark_path / ".aixcc" / "meta.yaml"
        project_yaml = benchmark_path / "project.yaml"

        if not meta_yaml.exists():
            logger.error(f"meta.yaml not found: {meta_yaml}")
            return None

        # Extract benchmark name from path
        benchmark_name = benchmark_path.name

        # Get language, main_repo, and optional repo_name from project.yaml
        lang = "c"  # Default
        main_repo = ""
        repo_name = None
        if project_yaml.exists():
            import yaml

            with project_yaml.open() as f:
                project_data = yaml.safe_load(f)
            lang = project_data.get("language", "c")
            main_repo = project_data.get("main_repo", "")
            # Optional: explicit repo_name to use instead of deriving from URL
            repo_name = project_data.get("repo_name")

        try:
            return MetaYamlAdapter.from_meta_yaml(
                meta_yaml_path=meta_yaml,
                benchmark_name=benchmark_name,
                lang=lang,
                main_repo=main_repo,
                repo_name=repo_name,
            )
        except Exception as e:
            logger.error(f"Failed to load adapter: {e}")
            return None

    def cleanup(self, adapter: MetaYamlAdapter) -> None:
        """Clean up variant projects for a benchmark.

        Args:
            adapter: MetaYamlAdapter for benchmark config
        """
        self.builder.cleanup_variants(adapter.benchmark_name)
        self._built_results.pop(adapter.benchmark_name, None)
