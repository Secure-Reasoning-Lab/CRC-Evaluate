"""Simplified benchmark validator using existing verification engines.

This module provides a thin orchestration layer over existing engines:
- VerificationEngine: POV verification (builds all variants, runs POVs)
- PatchVerificationEngine: Patch verification
- CoverageEngine: Coverage collection

No custom build logic - delegates everything to existing, tested code.
"""

import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

from crsbench.benchmark_ci.models import (
    BenchmarkValidationResult,
    CheckMode,
    CheckResult,
    CheckStatus,
    ValidationSummary,
)
from crsbench.evaluation.coverage import CoverageEngine
from crsbench.evaluation.verification.models import (
    PatchVerificationStatus,
    PovVerificationStatus,
    UnitTestMode,
)
from crsbench.evaluation.verification.patch import PatchVerificationEngine
from crsbench.evaluation.verification.pov import VerificationEngine
from crsbench.utils.logger import add_file_handler, get_logger, remove_file_handler
from crsbench.utils.run_helper import get_oss_fuzz_root
from crsbench.validation import validate_benchmark as format_validate

logger = get_logger(__name__)


def _load_project_capabilities(benchmark_path: Path) -> tuple[bool, Optional[str]]:
    """Load inc_build and rts_mode from project.yaml.

    Args:
        benchmark_path: Path to benchmark directory

    Returns:
        Tuple of (supports_inc_build, rts_mode)
        - supports_inc_build: True if benchmark supports incremental builds
        - rts_mode: RTS mode string or None if not supported
    """
    import yaml

    project_yaml = benchmark_path / "project.yaml"
    supports_inc_build = True  # Default
    rts_mode = None

    if project_yaml.exists():
        try:
            with project_yaml.open() as f:
                data = yaml.safe_load(f)
                if data:
                    supports_inc_build = data.get("inc_build", True)
                    rts_mode_value = data.get("rts_mode")
                    # Skip if explicitly "none" or None
                    if rts_mode_value and rts_mode_value != "none":
                        rts_mode = rts_mode_value
        except Exception as e:
            logger.warning(f"Failed to load project.yaml: {e}")

    return supports_inc_build, rts_mode


@contextmanager
def _with_file_logging(log_file: Optional[Path]):
    """Context manager to add/remove file logging for a phase.

    Uses thread ID filtering to ensure logs from concurrent benchmark validations
    don't get interleaved across log files.

    Args:
        log_file: Path to log file, or None to skip logging

    Yields:
        None
    """
    if log_file is None:
        yield
        return

    # Use thread ID to filter logs - ensures parallel benchmark validations
    # don't write to each other's log files
    current_thread_id = threading.get_ident()

    def thread_filter(record):
        # Only capture logs from the current thread
        return record["thread"].id == current_thread_id

    handler_id = add_file_handler(log_file, level="DEBUG", filter_func=thread_filter)
    try:
        yield
    finally:
        remove_file_handler(handler_id)


class BenchmarkValidator:
    """Validates benchmarks using existing verification engines.

    Orchestrates validation checks without reimplementing build/verify logic.
    All heavy lifting is done by existing engines that handle:
    - Parallel variant building (no race conditions)
    - POV verification with verdict resolution
    - Patch verification with security checks
    """

    def __init__(
        self,
        oss_fuzz_path: Optional[Path] = None,
        *,
        pov_timeout: int = 120,
        build_workers: int = 4,
        verify_workers: int = 4,
        source_mode: str = "pkgs",
        max_povs_per_cpv: Optional[int] = None,
    ):
        """Initialize validator.

        Args:
            oss_fuzz_path: Path to OSS-Fuzz directory (auto-detected if None)
            pov_timeout: Timeout for POV verification in seconds
            build_workers: Number of parallel build workers
            verify_workers: Number of parallel verification workers
            source_mode: Source mode - "pkgs" (bundled, default) or "main_repo" (clone)
            max_povs_per_cpv: Limit POVs verified per CPV (None = no limit)
        """
        self.oss_fuzz_path = Path(oss_fuzz_path or get_oss_fuzz_root())
        self.pov_timeout = pov_timeout
        self.build_workers = build_workers
        self.verify_workers = verify_workers
        self.source_mode = source_mode
        self.max_povs_per_cpv = max_povs_per_cpv

    def validate_format(self, benchmark_path: Path) -> CheckResult:
        """Validate benchmark format and structure.

        Args:
            benchmark_path: Path to benchmark directory

        Returns:
            CheckResult with validation status
        """
        start_time = time.time()
        try:
            result = format_validate(benchmark_path)
            elapsed = time.time() - start_time

            if result.is_valid:
                return CheckResult(
                    status=CheckStatus.PASS,
                    time_seconds=elapsed,
                    details={"warnings": result.warning_count},
                )

            error_msgs = "; ".join(e.message for e in result.errors[:3])
            return CheckResult(
                status=CheckStatus.FAIL,
                time_seconds=elapsed,
                error=error_msgs,
                details={
                    "error_count": result.error_count,
                    "warning_count": result.warning_count,
                },
            )

        except Exception as e:
            elapsed = time.time() - start_time
            return CheckResult.make_error(str(e), elapsed)

    def validate_povs(
        self,
        benchmark_path: Path,
        *,
        force_rebuild: bool = False,
        use_inc_build: bool = False,
    ) -> CheckResult:
        """Validate POVs using VerificationEngine.

        The engine handles:
        - Building all variants (DELTA_REF, FULL_BASE, ALL_PATCHED, CPVs)
        - Running POVs against each variant
        - Resolving verdicts based on crash patterns

        For benchmark validation, we expect all ground-truth POVs to have
        status CPV (crashes vulnerable version, fixed by patch).

        Args:
            benchmark_path: Path to benchmark directory
            force_rebuild: Force rebuild of Docker images
            use_inc_build: Use incremental build if available

        Returns:
            CheckResult with POV verification status
        """
        start_time = time.time()
        try:
            engine = VerificationEngine(
                oss_fuzz_path=self.oss_fuzz_path,
                timeout=self.pov_timeout,
                build_workers=self.build_workers,
                verify_workers=self.verify_workers,
                source_mode=self.source_mode,
                max_povs_per_cpv=self.max_povs_per_cpv,
            )

            output = engine.verify_benchmark(
                benchmark_path,
                force_rebuild=force_rebuild,
                use_inc_build=use_inc_build,
            )

            elapsed = time.time() - start_time

            if not output.results:
                return CheckResult(
                    status=CheckStatus.FAIL,
                    time_seconds=elapsed,
                    error="No POV verification results generated",
                    fallback_used=output.fallback_used,
                )

            # Check results - for CI validation, we expect CPV status
            # (POV triggers known vulnerability that is fixed by patch)
            # Additionally, verify that each POV matches its expected CPV
            # pov_id format: "cpv_N/pov_M.blob" -> expected_cpv = "cpv_N"
            failed_povs = []
            passed_povs = []

            for r in output.results:
                # Extract expected CPV from pov_id (e.g., "cpv_0" from "cpv_0/pov_0.blob")
                expected_cpv = None
                if r.pov_id and "/" in r.pov_id:
                    expected_cpv = r.pov_id.split("/")[0]

                if r.status == PovVerificationStatus.CPV:
                    # Verify POV matches its expected CPV
                    if expected_cpv and expected_cpv not in r.cpv_matched:
                        failed_povs.append(
                            f"{r.pov_id}: CPV mismatch - expected {expected_cpv}, "
                            f"got {r.cpv_matched}"
                        )
                    else:
                        passed_povs.append(r.pov_id)
                elif r.status == PovVerificationStatus.NOT_VULNERABLE:
                    # POV didn't crash where expected - this is a failure
                    failed_povs.append(f"{r.pov_id}: NOT_VULNERABLE - {r.details}")
                elif r.status == PovVerificationStatus.UNINTENDED_CRASH:
                    # Crashed even with patches applied
                    failed_povs.append(f"{r.pov_id}: UNINTENDED_CRASH - {r.details}")
                else:
                    failed_povs.append(f"{r.pov_id}: {r.status.value}")

            if failed_povs:
                return CheckResult(
                    status=CheckStatus.FAIL,
                    time_seconds=elapsed,
                    build_time=output.build_time,
                    verify_time=output.verify_time,
                    error=f"{len(failed_povs)} POV(s) failed: {failed_povs[0]}",
                    details={
                        "passed": len(passed_povs),
                        "failed": len(failed_povs),
                        "skipped": output.skipped_count,
                        "failures": failed_povs,
                    },
                    fallback_used=output.fallback_used,
                )

            return CheckResult(
                status=CheckStatus.PASS,
                time_seconds=elapsed,
                build_time=output.build_time,
                verify_time=output.verify_time,
                details={
                    "passed": len(passed_povs),
                    "failed": 0,
                    "skipped": output.skipped_count,
                },
                fallback_used=output.fallback_used,
            )

        except Exception as e:
            elapsed = time.time() - start_time
            logger.exception("POV validation failed")
            return CheckResult.make_error(str(e), elapsed)

    def validate_patches(
        self,
        benchmark_path: Path,
        *,
        force_rebuild: bool = False,
        use_inc_build: bool = False,
        test_mode: UnitTestMode = UnitTestMode.FULL,
        build_only: bool = False,
        work_dir: Optional[Path] = None,
    ) -> CheckResult:
        """Validate patches using PatchVerificationEngine.

        The engine handles:
        - Discovering patches from .aixcc/<harness>/<cpv>/patches/
        - Building patched variants
        - Verifying POVs don't crash after patch
        - Running unit tests

        Args:
            benchmark_path: Path to benchmark directory
            force_rebuild: Force rebuild of Docker images
            use_inc_build: Use incremental build if available
            test_mode: Unit test mode (FULL or RTS)
            build_only: Build variants but skip verification
            work_dir: Shared working directory for source persistence
                across build_only and verify calls

        Returns:
            CheckResult with patch verification status
        """
        start_time = time.time()
        try:
            engine = PatchVerificationEngine(
                oss_fuzz_path=self.oss_fuzz_path,
                timeout=self.pov_timeout,
                force_rebuild=force_rebuild,
                use_inc_build=use_inc_build,
                source_mode=self.source_mode,
                test_mode=test_mode,
                build_only=build_only,
                work_dir=work_dir,
                max_povs_per_cpv=self.max_povs_per_cpv,
            )

            try:
                output = engine.verify_benchmark(benchmark_path)
            finally:
                engine.cleanup()

            elapsed = time.time() - start_time

            if not output.results:
                # No patches found - might be okay for some benchmarks
                return CheckResult(
                    status=CheckStatus.SKIP,
                    time_seconds=elapsed,
                    error="No patches found",
                    fallback_used=output.fallback_used,
                )

            # Check results
            failed_patches = []
            passed_patches = []

            for r in output.results:
                if r.status == PatchVerificationStatus.VALID:
                    passed_patches.append(r.patch_id)
                else:
                    failed_patches.append(
                        f"{r.patch_id}: {r.status.value} - {r.details}"
                    )

            # Extract per-patch timing breakdown
            total_build = sum(r.build_time for r in output.results)
            total_verify = sum(
                r.pov_test_time + r.unit_test_time for r in output.results
            )
            per_patch = [
                {
                    "patch_id": r.patch_id,
                    "build_time": r.build_time,
                    "unit_test_time": r.unit_test_time,
                }
                for r in output.results
            ]

            if failed_patches:
                return CheckResult(
                    status=CheckStatus.FAIL,
                    time_seconds=elapsed,
                    build_time=total_build,
                    verify_time=total_verify,
                    error=f"{len(failed_patches)} patch(es) failed: {failed_patches[0]}",
                    details={
                        "passed": len(passed_patches),
                        "failed": len(failed_patches),
                        "failures": failed_patches,
                        "per_patch": per_patch,
                    },
                    fallback_used=output.fallback_used,
                )

            return CheckResult(
                status=CheckStatus.PASS,
                time_seconds=elapsed,
                build_time=total_build,
                verify_time=total_verify,
                details={
                    "passed": len(passed_patches),
                    "failed": 0,
                    "per_patch": per_patch,
                },
                fallback_used=output.fallback_used,
            )

        except Exception as e:
            elapsed = time.time() - start_time
            logger.exception("Patch validation failed")
            return CheckResult.make_error(str(e), elapsed)

    def validate_coverage(
        self,
        benchmark_path: Path,
        corpus_dir: Optional[Path] = None,
        *,
        force_rebuild: bool = False,
        use_inc_build: bool = False,
    ) -> CheckResult:
        """Validate coverage collection works.

        For benchmark CI validation, this verifies that coverage collection
        infrastructure works correctly. If no corpus is provided, creates a
        minimal artificial corpus with a simple "A" input to test the pipeline.

        Args:
            benchmark_path: Path to benchmark directory
            corpus_dir: Optional directory containing corpus files

        Returns:
            CheckResult with coverage check status
        """
        import tempfile

        start_time = time.time()
        temp_corpus_dir = None
        use_artificial_corpus = False

        # Look for corpus via MetaYamlAdapter if not provided
        if corpus_dir is None:
            from crsbench.validation.meta_adapter import MetaYamlAdapter

            adapter = MetaYamlAdapter.from_benchmark_path(benchmark_path)
            if adapter:
                corpus_dir = adapter.get_corpus_dir()

        # If no corpus found, create minimal artificial corpus for validation
        if corpus_dir is None or not corpus_dir.exists():
            temp_corpus_dir = Path(tempfile.mkdtemp(prefix="benchmark_ci_corpus_"))
            corpus_dir = temp_corpus_dir
            use_artificial_corpus = True

            # Create minimal test input - simple "A" byte
            minimal_input = temp_corpus_dir / "minimal_input"
            minimal_input.write_bytes(b"A")
            logger.info(
                "No corpus found, using artificial corpus for coverage validation"
            )

        try:
            engine = CoverageEngine(
                oss_fuzz_path=self.oss_fuzz_path,
                build_workers=self.build_workers,
                source_mode=self.source_mode,
            )

            # Collect coverage using the corpus
            result = engine.collect_coverage(
                benchmark_path,
                corpus_dir,
                force_rebuild=force_rebuild,
                use_inc_build=use_inc_build,
            )
            elapsed = time.time() - start_time

            if not result or not result.final_summary:
                return CheckResult(
                    status=CheckStatus.FAIL,
                    time_seconds=elapsed,
                    build_time=result.build_time if result else 0.0,
                    verify_time=result.verify_time if result else 0.0,
                    error="No coverage results generated",
                )

            return CheckResult(
                status=CheckStatus.PASS,
                time_seconds=elapsed,
                build_time=result.build_time,
                verify_time=result.verify_time,
                details={
                    "harness": result.harness_name,
                    "lines_covered": result.final_summary.lines_covered,
                    "lines_total": result.final_summary.lines_total,
                    "artificial_corpus": use_artificial_corpus,
                },
            )

        except Exception as e:
            elapsed = time.time() - start_time
            logger.exception("Coverage validation failed")
            return CheckResult.make_error(str(e), elapsed)

        finally:
            # Clean up temporary corpus directory
            if temp_corpus_dir and temp_corpus_dir.exists():
                import shutil

                shutil.rmtree(temp_corpus_dir, ignore_errors=True)

    def validate_benchmark(
        self,
        benchmark_path: Path,
        *,
        check_mode: CheckMode = CheckMode.DEFAULT,
        include_coverage: bool = False,
        use_inc_build: bool = False,
        skip_format: bool = False,
        skip_verify: bool = False,
        skip_patch_verify: bool = False,
        log_dir: Optional[Path] = None,
    ) -> BenchmarkValidationResult:
        """Run all validation checks for a benchmark.

        CI always rebuilds from scratch to ensure reproducibility.
        Each check (POV, patch, coverage) runs independently without
        sharing cached builds.

        Args:
            benchmark_path: Path to benchmark directory
            check_mode: Validation mode (DEFAULT, INC, RTS, INC_RTS, ALL)
            include_coverage: Include coverage check (slower)
            use_inc_build: Use incremental build if available (for DEFAULT mode)
            skip_format: Skip format validation
            skip_verify: Skip POV verification
            skip_patch_verify: Skip patch verification
            log_dir: Optional directory to save per-phase log files

        Returns:
            BenchmarkValidationResult with all check results
        """
        # CI always rebuilds - no caching between commands
        force_rebuild = True

        benchmark_path = Path(benchmark_path).resolve()
        benchmark_name = benchmark_path.name
        started_at = datetime.now()

        # Load benchmark capabilities
        supports_inc_build, rts_mode = _load_project_capabilities(benchmark_path)

        # Setup log directory if specified
        benchmark_log_dir = None
        if log_dir:
            benchmark_log_dir = log_dir / benchmark_name
            benchmark_log_dir.mkdir(parents=True, exist_ok=True)

        logger.debug(f"Validating benchmark: {benchmark_name}")
        if check_mode != CheckMode.DEFAULT:
            logger.debug(f"  Check mode: {check_mode.value}")
            logger.debug(
                f"  Capabilities: inc_build={supports_inc_build}, rts_mode={rts_mode}"
            )

        # 1. Format validation (fast, no Docker) - always run regardless of check_mode
        if skip_format:
            format_result = CheckResult.skip("--skip-format")
        else:
            logger.debug(f"  [{benchmark_name}] Running format validation...")
            format_log = benchmark_log_dir / "format.log" if benchmark_log_dir else None
            with _with_file_logging(format_log):
                format_result = self.validate_format(benchmark_path)
            logger.debug(
                f"  [{benchmark_name}] Format: {format_result.status.value} "
                f"({format_result.time_seconds:.1f}s)"
            )

        # Stop early if format fails (unless skipped)
        if not skip_format and format_result.status == CheckStatus.FAIL:
            return BenchmarkValidationResult(
                benchmark=benchmark_name,
                benchmark_path=benchmark_path,
                format_check=format_result,
                pov_check=CheckResult.skip("Skipped due to format failure"),
                patch_check=CheckResult.skip("Skipped due to format failure"),
                supports_inc_build=supports_inc_build,
                rts_mode=rts_mode,
                started_at=started_at,
                finished_at=datetime.now(),
            )

        # Determine which checks to run based on check_mode
        run_standard = check_mode in (CheckMode.DEFAULT, CheckMode.ALL)
        run_inc = check_mode in (CheckMode.INC, CheckMode.INC_RTS, CheckMode.ALL)
        run_rts = check_mode in (CheckMode.RTS, CheckMode.INC_RTS, CheckMode.ALL)
        run_inc_rts = check_mode in (CheckMode.INC_RTS, CheckMode.ALL)

        # Initialize all variant results
        pov_result = CheckResult.skip("Not in check mode")
        patch_result = CheckResult.skip("Not in check mode")
        coverage_result = None
        pov_inc_result = None
        patch_inc_result = None
        patch_rts_result = None
        patch_inc_rts_result = None
        coverage_inc_result = None

        # 2. Standard POV verification (DEFAULT, ALL)
        if run_standard and not skip_verify:
            logger.debug(f"  [{benchmark_name}] Running POV verification...")
            verify_log = benchmark_log_dir / "verify.log" if benchmark_log_dir else None
            with _with_file_logging(verify_log):
                pov_result = self.validate_povs(
                    benchmark_path,
                    force_rebuild=force_rebuild,
                    use_inc_build=use_inc_build,
                )
            logger.debug(
                f"  [{benchmark_name}] POV: {pov_result.status.value} "
                f"({pov_result.time_seconds:.1f}s)"
            )
        elif skip_verify:
            pov_result = CheckResult.skip("--skip-verify")

        # 3. Standard Patch verification (DEFAULT, ALL)
        if run_standard and not skip_patch_verify:
            logger.debug(f"  [{benchmark_name}] Running patch verification...")
            patch_log = (
                benchmark_log_dir / "patch-verify.log" if benchmark_log_dir else None
            )
            with _with_file_logging(patch_log):
                patch_result = self.validate_patches(
                    benchmark_path,
                    force_rebuild=force_rebuild,
                    use_inc_build=use_inc_build,
                )
            logger.debug(
                f"  [{benchmark_name}] Patch: {patch_result.status.value} "
                f"({patch_result.time_seconds:.1f}s)"
            )
        elif skip_patch_verify:
            patch_result = CheckResult.skip("--skip-patch-verify")

        # 4. Standard Coverage check (optional, DEFAULT or ALL)
        if include_coverage and run_standard:
            logger.debug(f"  [{benchmark_name}] Running coverage check...")
            coverage_log = (
                benchmark_log_dir / "coverage.log" if benchmark_log_dir else None
            )
            with _with_file_logging(coverage_log):
                coverage_result = self.validate_coverage(
                    benchmark_path,
                    force_rebuild=force_rebuild,
                    use_inc_build=use_inc_build,
                )
            logger.debug(
                f"  [{benchmark_name}] Coverage: {coverage_result.status.value} "
                f"({coverage_result.time_seconds:.1f}s)"
            )

        # 5. Inc-build variants (INC, INC_RTS, ALL)
        if run_inc and supports_inc_build:
            # POV with inc-build
            if not skip_verify:
                logger.debug(f"  [{benchmark_name}] Running POV verification (inc)...")
                verify_inc_log = (
                    benchmark_log_dir / "verify-inc.log" if benchmark_log_dir else None
                )
                with _with_file_logging(verify_inc_log):
                    pov_inc_result = self.validate_povs(
                        benchmark_path,
                        force_rebuild=force_rebuild,
                        use_inc_build=True,
                    )
                logger.debug(
                    f"  [{benchmark_name}] POV(inc): {pov_inc_result.status.value} "
                    f"({pov_inc_result.time_seconds:.1f}s)"
                )

            # Patch with inc-build
            if not skip_patch_verify:
                logger.debug(
                    f"  [{benchmark_name}] Running patch verification (inc)..."
                )
                patch_inc_log = (
                    benchmark_log_dir / "patch-verify-inc.log"
                    if benchmark_log_dir
                    else None
                )
                with _with_file_logging(patch_inc_log):
                    patch_inc_result = self.validate_patches(
                        benchmark_path,
                        force_rebuild=force_rebuild,
                        use_inc_build=True,
                    )
                logger.debug(
                    f"  [{benchmark_name}] Patch(inc): {patch_inc_result.status.value} "
                    f"({patch_inc_result.time_seconds:.1f}s)"
                )

            # Coverage with inc-build
            if include_coverage:
                logger.debug(f"  [{benchmark_name}] Running coverage check (inc)...")
                coverage_inc_log = (
                    benchmark_log_dir / "coverage-inc.log"
                    if benchmark_log_dir
                    else None
                )
                with _with_file_logging(coverage_inc_log):
                    coverage_inc_result = self.validate_coverage(
                        benchmark_path,
                        force_rebuild=force_rebuild,
                        use_inc_build=True,
                    )
                logger.debug(
                    f"  [{benchmark_name}] Coverage(inc): {coverage_inc_result.status.value} "
                    f"({coverage_inc_result.time_seconds:.1f}s)"
                )
        elif run_inc and not supports_inc_build:
            logger.debug(f"  [{benchmark_name}] Skipping inc variants (not supported)")
            # Set explicit SKIP results for unsupported inc-build
            if not skip_verify:
                pov_inc_result = CheckResult.skip(
                    "Benchmark does not support inc-build"
                )
            if not skip_patch_verify:
                patch_inc_result = CheckResult.skip(
                    "Benchmark does not support inc-build"
                )
            if include_coverage:
                coverage_inc_result = CheckResult.skip(
                    "Benchmark does not support inc-build"
                )

        # 6. RTS variants (RTS, ALL) - standard build + RTS test mode
        if run_rts and rts_mode and not skip_patch_verify:
            logger.debug(f"  [{benchmark_name}] Running patch verification (rts)...")
            patch_rts_log = (
                benchmark_log_dir / "patch-verify-rts.log"
                if benchmark_log_dir
                else None
            )
            with _with_file_logging(patch_rts_log):
                patch_rts_result = self.validate_patches(
                    benchmark_path,
                    force_rebuild=force_rebuild,
                    use_inc_build=False,
                    test_mode=UnitTestMode.RTS,
                )
            logger.debug(
                f"  [{benchmark_name}] Patch(rts): {patch_rts_result.status.value} "
                f"({patch_rts_result.time_seconds:.1f}s)"
            )
        elif run_rts and not rts_mode:
            logger.debug(f"  [{benchmark_name}] Skipping RTS variants (not supported)")
            # Set explicit SKIP result for unsupported RTS
            if not skip_patch_verify:
                patch_rts_result = CheckResult.skip("Benchmark does not support RTS")

        # 7. Inc + RTS combined (INC_RTS, ALL)
        if run_inc_rts and supports_inc_build and rts_mode and not skip_patch_verify:
            logger.debug(
                f"  [{benchmark_name}] Running patch verification (inc+rts)..."
            )
            patch_inc_rts_log = (
                benchmark_log_dir / "patch-verify-inc-rts.log"
                if benchmark_log_dir
                else None
            )
            with _with_file_logging(patch_inc_rts_log):
                patch_inc_rts_result = self.validate_patches(
                    benchmark_path,
                    force_rebuild=force_rebuild,
                    use_inc_build=True,
                    test_mode=UnitTestMode.RTS,
                )
            logger.debug(
                f"  [{benchmark_name}] Patch(inc+rts): {patch_inc_rts_result.status.value} "
                f"({patch_inc_rts_result.time_seconds:.1f}s)"
            )
        elif run_inc_rts and not skip_patch_verify:
            # Set explicit SKIP for unsupported inc+rts combination
            if not supports_inc_build and not rts_mode:
                patch_inc_rts_result = CheckResult.skip(
                    "Benchmark does not support inc-build or RTS"
                )
            elif not supports_inc_build:
                patch_inc_rts_result = CheckResult.skip(
                    "Benchmark does not support inc-build"
                )
            elif not rts_mode:
                patch_inc_rts_result = CheckResult.skip(
                    "Benchmark does not support RTS"
                )

        return BenchmarkValidationResult(
            benchmark=benchmark_name,
            benchmark_path=benchmark_path,
            format_check=format_result,
            pov_check=pov_result,
            patch_check=patch_result,
            coverage_check=coverage_result,
            pov_inc_check=pov_inc_result,
            patch_inc_check=patch_inc_result,
            patch_rts_check=patch_rts_result,
            patch_inc_rts_check=patch_inc_rts_result,
            coverage_inc_check=coverage_inc_result,
            supports_inc_build=supports_inc_build,
            rts_mode=rts_mode,
            started_at=started_at,
            finished_at=datetime.now(),
        )

    def validate_benchmarks(
        self,
        benchmark_paths: list[Path],
        *,
        include_coverage: bool = False,
        use_inc_build: bool = False,
    ) -> ValidationSummary:
        """Validate multiple benchmarks.

        Note: Benchmarks are validated sequentially to avoid resource contention.
        Each benchmark's internal operations (variant building, POV verification)
        are parallelized by the underlying engines.

        CI always rebuilds from scratch - no caching between commands.

        Args:
            benchmark_paths: List of benchmark directory paths
            include_coverage: Include coverage check (slower)
            use_inc_build: Use incremental build if available

        Returns:
            ValidationSummary with all results
        """
        summary = ValidationSummary(started_at=datetime.now())

        for i, benchmark_path in enumerate(benchmark_paths, 1):
            logger.debug(f"[{i}/{len(benchmark_paths)}] {benchmark_path.name}")
            result = self.validate_benchmark(
                benchmark_path,
                include_coverage=include_coverage,
                use_inc_build=use_inc_build,
            )
            summary.add_result(result)

        summary.finished_at = datetime.now()

        logger.info(
            f"Validation complete: {summary.passed}/{summary.total} passed, "
            f"{summary.failed} failed, {summary.errors} errors"
        )

        return summary
