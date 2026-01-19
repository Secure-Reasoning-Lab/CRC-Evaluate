"""Simplified benchmark validator using existing verification engines.

This module provides a thin orchestration layer over existing engines:
- VerificationEngine: POV verification (builds all variants, runs POVs)
- PatchVerificationEngine: Patch verification
- CoverageEngine: Coverage collection

No custom build logic - delegates everything to existing, tested code.
"""

import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from crsbench.benchmark_ci.models import (
    BenchmarkValidationResult,
    CheckResult,
    CheckStatus,
    ValidationSummary,
)
from crsbench.evaluation.coverage import CoverageEngine
from crsbench.evaluation.verification.models import (
    PatchVerificationStatus,
    PovVerificationStatus,
)
from crsbench.evaluation.verification.patch import PatchVerificationEngine
from crsbench.evaluation.verification.pov import VerificationEngine
from crsbench.utils.logger import get_logger
from crsbench.utils.run_helper import get_oss_fuzz_root
from crsbench.validation import validate_benchmark as format_validate

logger = get_logger(__name__)


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
    ):
        """Initialize validator.

        Args:
            oss_fuzz_path: Path to OSS-Fuzz directory (auto-detected if None)
            pov_timeout: Timeout for POV verification in seconds
            build_workers: Number of parallel build workers
            verify_workers: Number of parallel verification workers
        """
        self.oss_fuzz_path = Path(oss_fuzz_path or get_oss_fuzz_root())
        self.pov_timeout = pov_timeout
        self.build_workers = build_workers
        self.verify_workers = verify_workers

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
        - Building all variants (DELTA_BASE, DELTA_REF, FULL_BASE, ALL_PATCHED, CPVs)
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
            )

            results, skipped = engine.verify_benchmark(
                benchmark_path,
                force_rebuild=force_rebuild,
                use_inc_build=use_inc_build,
            )

            elapsed = time.time() - start_time

            if not results:
                return CheckResult(
                    status=CheckStatus.FAIL,
                    time_seconds=elapsed,
                    error="No POV verification results generated",
                )

            # Check results - for CI validation, we expect CPV status
            # (POV triggers known vulnerability that is fixed by patch)
            # Additionally, verify that each POV matches its expected CPV
            # pov_id format: "cpv_N/pov_M.blob" -> expected_cpv = "cpv_N"
            failed_povs = []
            passed_povs = []

            for r in results:
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
                elif r.status == PovVerificationStatus.ZERODAY:
                    # In DELTA mode: crashed on base (pre-existing bug)
                    # In FULL mode: crashed but no CPV matched
                    failed_povs.append(f"{r.pov_id}: ZERODAY - {r.details}")
                elif r.status == PovVerificationStatus.UNINTENDED_CRASH:
                    # Crashed even with patches applied
                    failed_povs.append(f"{r.pov_id}: UNINTENDED_CRASH - {r.details}")
                else:
                    failed_povs.append(f"{r.pov_id}: {r.status.value}")

            if failed_povs:
                return CheckResult(
                    status=CheckStatus.FAIL,
                    time_seconds=elapsed,
                    error=f"{len(failed_povs)} POV(s) failed: {failed_povs[0]}",
                    details={
                        "passed": len(passed_povs),
                        "failed": len(failed_povs),
                        "skipped": skipped,
                        "failures": failed_povs,
                    },
                )

            return CheckResult(
                status=CheckStatus.PASS,
                time_seconds=elapsed,
                details={
                    "passed": len(passed_povs),
                    "failed": 0,
                    "skipped": skipped,
                },
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
            )

            try:
                results = engine.verify_benchmark(benchmark_path)
            finally:
                engine.cleanup()

            elapsed = time.time() - start_time

            if not results:
                # No patches found - might be okay for some benchmarks
                return CheckResult(
                    status=CheckStatus.SKIP,
                    time_seconds=elapsed,
                    error="No patches found",
                )

            # Check results
            failed_patches = []
            passed_patches = []

            for r in results:
                if r.status == PatchVerificationStatus.VALID:
                    passed_patches.append(r.patch_id)
                else:
                    failed_patches.append(
                        f"{r.patch_id}: {r.status.value} - {r.details}"
                    )

            if failed_patches:
                return CheckResult(
                    status=CheckStatus.FAIL,
                    time_seconds=elapsed,
                    error=f"{len(failed_patches)} patch(es) failed: {failed_patches[0]}",
                    details={
                        "passed": len(passed_patches),
                        "failed": len(failed_patches),
                        "failures": failed_patches,
                    },
                )

            return CheckResult(
                status=CheckStatus.PASS,
                time_seconds=elapsed,
                details={
                    "passed": len(passed_patches),
                    "failed": 0,
                },
            )

        except Exception as e:
            elapsed = time.time() - start_time
            logger.exception("Patch validation failed")
            return CheckResult.make_error(str(e), elapsed)

    def validate_coverage(
        self,
        benchmark_path: Path,
        corpus_dir: Optional[Path] = None,
    ) -> CheckResult:
        """Validate coverage collection works.

        Coverage validation requires a corpus directory with test inputs.
        If no corpus is provided, looks for corpus in benchmark's .aixcc directory.

        Args:
            benchmark_path: Path to benchmark directory
            corpus_dir: Optional directory containing corpus files

        Returns:
            CheckResult with coverage check status
        """
        start_time = time.time()

        # Look for corpus in benchmark directory if not provided
        if corpus_dir is None:
            aixcc_dir = benchmark_path / ".aixcc"
            # Try common corpus locations
            possible_corpus = [
                aixcc_dir / "corpus",
                aixcc_dir / "seed_corpus",
                benchmark_path / "corpus",
            ]
            for path in possible_corpus:
                if path.exists() and any(path.iterdir()):
                    corpus_dir = path
                    break

        if corpus_dir is None or not corpus_dir.exists():
            return CheckResult(
                status=CheckStatus.SKIP,
                time_seconds=time.time() - start_time,
                error="No corpus directory found for coverage collection",
            )

        try:
            engine = CoverageEngine(
                oss_fuzz_path=self.oss_fuzz_path,
                build_workers=self.build_workers,
                verify_workers=self.verify_workers,
            )

            # Collect coverage using the corpus
            result = engine.collect_coverage(benchmark_path, corpus_dir)
            elapsed = time.time() - start_time

            if not result or not result.final_summary:
                return CheckResult(
                    status=CheckStatus.FAIL,
                    time_seconds=elapsed,
                    error="No coverage results generated",
                )

            return CheckResult(
                status=CheckStatus.PASS,
                time_seconds=elapsed,
                details={
                    "harness": result.harness_name,
                    "lines_covered": result.final_summary.lines_covered,
                    "lines_total": result.final_summary.lines_total,
                },
            )

        except Exception as e:
            elapsed = time.time() - start_time
            logger.exception("Coverage validation failed")
            return CheckResult.make_error(str(e), elapsed)

    def validate_benchmark(
        self,
        benchmark_path: Path,
        *,
        include_coverage: bool = False,
        force_rebuild: bool = False,
        use_inc_build: bool = False,
        skip_format: bool = False,
        skip_verify: bool = False,
        skip_patch_verify: bool = False,
    ) -> BenchmarkValidationResult:
        """Run all validation checks for a benchmark.

        Args:
            benchmark_path: Path to benchmark directory
            include_coverage: Include coverage check (slower)
            force_rebuild: Force rebuild of Docker images
            use_inc_build: Use incremental build if available
            skip_format: Skip format validation
            skip_verify: Skip POV verification
            skip_patch_verify: Skip patch verification

        Returns:
            BenchmarkValidationResult with all check results
        """
        benchmark_path = Path(benchmark_path).resolve()
        benchmark_name = benchmark_path.name
        started_at = datetime.now()

        logger.info(f"Validating benchmark: {benchmark_name}")

        # 1. Format validation (fast, no Docker)
        if skip_format:
            format_result = CheckResult.skip("--skip-format")
        else:
            logger.info(f"  [{benchmark_name}] Running format validation...")
            format_result = self.validate_format(benchmark_path)
            logger.info(
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
                coverage_check=(
                    CheckResult.skip("Skipped due to format failure")
                    if include_coverage
                    else None
                ),
                started_at=started_at,
                finished_at=datetime.now(),
            )

        # 2. POV verification
        if skip_verify:
            pov_result = CheckResult.skip("--skip-verify")
        else:
            logger.info(f"  [{benchmark_name}] Running POV verification...")
            pov_result = self.validate_povs(
                benchmark_path,
                force_rebuild=force_rebuild,
                use_inc_build=use_inc_build,
            )
            logger.info(
                f"  [{benchmark_name}] POV: {pov_result.status.value} "
                f"({pov_result.time_seconds:.1f}s)"
            )

        # 3. Patch verification
        if skip_patch_verify:
            patch_result = CheckResult.skip("--skip-patch-verify")
        else:
            logger.info(f"  [{benchmark_name}] Running patch verification...")
            patch_result = self.validate_patches(
                benchmark_path,
                force_rebuild=force_rebuild,
                use_inc_build=use_inc_build,
            )
            logger.info(
                f"  [{benchmark_name}] Patch: {patch_result.status.value} "
                f"({patch_result.time_seconds:.1f}s)"
            )

        # 4. Coverage check (optional)
        coverage_result = None
        if include_coverage:
            logger.info(f"  [{benchmark_name}] Running coverage check...")
            coverage_result = self.validate_coverage(benchmark_path)
            logger.info(
                f"  [{benchmark_name}] Coverage: {coverage_result.status.value} "
                f"({coverage_result.time_seconds:.1f}s)"
            )

        return BenchmarkValidationResult(
            benchmark=benchmark_name,
            benchmark_path=benchmark_path,
            format_check=format_result,
            pov_check=pov_result,
            patch_check=patch_result,
            coverage_check=coverage_result,
            started_at=started_at,
            finished_at=datetime.now(),
        )

    def validate_benchmarks(
        self,
        benchmark_paths: list[Path],
        *,
        include_coverage: bool = False,
        force_rebuild: bool = False,
        use_inc_build: bool = False,
    ) -> ValidationSummary:
        """Validate multiple benchmarks.

        Note: Benchmarks are validated sequentially to avoid resource contention.
        Each benchmark's internal operations (variant building, POV verification)
        are parallelized by the underlying engines.

        Args:
            benchmark_paths: List of benchmark directory paths
            include_coverage: Include coverage check (slower)
            force_rebuild: Force rebuild of Docker images
            use_inc_build: Use incremental build if available

        Returns:
            ValidationSummary with all results
        """
        summary = ValidationSummary(started_at=datetime.now())

        for i, benchmark_path in enumerate(benchmark_paths, 1):
            logger.info(f"[{i}/{len(benchmark_paths)}] {benchmark_path.name}")
            result = self.validate_benchmark(
                benchmark_path,
                include_coverage=include_coverage,
                force_rebuild=force_rebuild,
                use_inc_build=use_inc_build,
            )
            summary.add_result(result)

        summary.finished_at = datetime.now()

        logger.info(
            f"Validation complete: {summary.passed}/{summary.total} passed, "
            f"{summary.failed} failed, {summary.errors} errors"
        )

        return summary
