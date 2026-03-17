"""PatchVerificationEngine for validating CRS-generated patches.

This module provides the main orchestrator for patch verification:
1. Pull inc-build image (if available)
2. Clone source and apply patch locally
3. Build via build_fuzzers with inc-build image (ASAN-instrumented)
4. Run POV test via reproduce (should NOT crash)

Inc-build flow: rsyncs patched source to /src/ and uses replay-build
semantics to preserve unchanged object files where possible.
"""

from __future__ import annotations

import re
import shutil
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Optional

from crsbench.builder import OSSFuzzInfrastructure
from crsbench.builder.types import BuildConfig, VariantType
from crsbench.evaluation.verification.models import (
    CpvStats,
    PatchBenchmarkOutput,
    PatchInfo,
    PatchVerificationResult,
    PatchVerificationStatus,
    UnitTestMode,
)
from crsbench.utils.docker import docker_rmtree, fix_docker_ownership
from crsbench.utils.logger import get_logger
from crsbench.utils.repo_manager import clone_or_copy_cached_repo
from crsbench.utils.workers import resolve_build_workers, resolve_verify_workers

if TYPE_CHECKING:
    from crsbench.validation.meta_adapter import MetaYamlAdapter

logger = get_logger(__name__)


def _summarize_build_failure(stdout: str, stderr: str) -> str:
    """Extract a short human-meaningful build failure summary.

    Prefer stderr lines that contain error/fail markers, then fall back to
    the first non-empty line from stderr/stdout.
    """
    for text in (stderr, stdout):
        if not text:
            continue
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            continue
        for line in lines:
            lower = line.lower()
            if any(token in lower for token in ("error", "failed", "exception")):
                return line[:240]
        return lines[0][:240]
    return ""


@dataclass
class PatchVerifyTask:
    """Task for parallel patch verification."""

    patch: PatchInfo
    harness: str
    pov_path: Path
    project_name: str
    main_repo: str
    commit: str
    repo_name: Optional[str]


class PatchVerificationEngine:
    """Engine for validating CRS-generated patches.

    Coordinates the entire patch verification workflow:
    1. Load benchmark configuration
    2. Pull inc-build image (for faster incremental builds)
    3. Clone source repository and apply patch locally
    4. Build fuzzers with snapshot build fallback or full build
    5. Run POV test (patch should prevent crash)
    6. Run unit tests (optional, to ensure no regressions)

    Attributes:
        oss_fuzz_path: Path to oss-fuzz directory
        infra: OSSFuzzInfrastructure instance
        test_mode: Unit test execution mode
        sanitizer: Sanitizer type
        timeout: Timeout for reproduce operations
        build_timeout: Timeout for build operations
        test_timeout: Timeout for unit test execution
        build_workers: Number of parallel workers for patch builds
        verify_workers: Number of parallel workers for POV variant testing
        work_dir: Working directory for isolated builds
    """

    def __init__(
        self,
        oss_fuzz_path: Path,
        *,
        test_mode: UnitTestMode = UnitTestMode.FULL,
        sanitizer: str = "address",
        timeout: int = 180,
        build_timeout: int = 1200,
        test_timeout: int = 1800,
        jobs: Optional[int] = None,
        cores_per_job: Optional[int] = None,
        verify_variants: bool = True,
        work_dir: Optional[Path] = None,
        log_dir: Optional[Path] = None,
        force_rebuild: bool = False,
        use_inc_build: bool = True,
        source_mode: str = "pkgs",
        build_only: bool = False,
        max_povs_per_cpv: Optional[int] = None,
        skip_pov: bool = False,
        skip_unittest: bool = False,
        inc_image_policy: Optional[str] = None,
        inc_image_registry: Optional[str] = None,
        inc_image_max_pull_bytes: Optional[int] = None,
        inc_image_pull_timeout: Optional[int] = None,
        local_image_prefix: Optional[str] = None,
    ):
        """Initialize the patch verification engine.

        Args:
            oss_fuzz_path: Path to oss-fuzz directory
            test_mode: Unit test execution mode
            sanitizer: Sanitizer type (address, undefined, memory)
            timeout: Timeout for reproduce operations in seconds
            build_timeout: Timeout for build operations in seconds
            test_timeout: Timeout for unit test execution in seconds
            jobs: Parallel verification jobs (controls build parallelism).
            cores_per_job: CPUs per verification job (controls per-job
                parallelism for POV variant testing).
            verify_variants: If True, verify patch against all POV variants
            work_dir: Working directory for isolated builds. If provided, builds
                are isolated to this directory with symlinks to oss-fuzz/build/out/
                for helper.py compatibility. Use this for experiment-specific builds.
            log_dir: Directory for patch verification stdout/stderr log artifacts.
                Logs are written under <work_dir>/logs in structured files.
                If work_dir is unset, a temporary runtime work_dir is created
                and cleaned up (including logs).
            force_rebuild: If True, clean and rebuild even if build exists.
            use_inc_build: If True, use incremental builds when available (faster).
                If False, always use full OSS-Fuzz build.
            source_mode: Source mode - "pkgs" (bundled, default) or "main_repo" (clone)
            build_only: If True, build variants but skip verification (POV tests
                and unit tests). Used to pre-build variants for subsequent
                verify-only calls with force_rebuild=False.
            max_povs_per_cpv: Limit POV variants tested per CPV (None = no limit).
                When set to 1, only pov_0.blob is used per CPV.
            skip_pov: If True, skip POV tests (run unit tests only).
            skip_unittest: If True, skip unit tests (run POV tests only).
        """
        self.oss_fuzz_path = Path(oss_fuzz_path)
        effective_work_dir = Path(work_dir) if work_dir else None
        effective_log_dir = Path(log_dir) if log_dir else None
        temp_runtime_dir: Optional[Path] = None
        if effective_work_dir is None:
            # Use isolated temporary work_dir by default (including CI paths
            # that provide explicit log_dir) to avoid cwd pollution and
            # cross-job workspace interference.
            temp_runtime_dir = Path(tempfile.mkdtemp(prefix="crsbench-patch-verify-"))
            effective_work_dir = temp_runtime_dir

        self.work_dir = effective_work_dir
        self.log_dir = effective_log_dir
        self.infra = OSSFuzzInfrastructure(
            oss_fuzz_path,
            work_dir=effective_work_dir,
            inc_image_policy=inc_image_policy,
            inc_image_registry=inc_image_registry,
            inc_image_max_pull_bytes=inc_image_max_pull_bytes,
            inc_image_pull_timeout=inc_image_pull_timeout,
            local_image_prefix=local_image_prefix,
        )
        self.test_mode = test_mode
        self.sanitizer = sanitizer
        self.timeout = timeout
        self.build_timeout = build_timeout
        self.test_timeout = test_timeout
        self.build_workers = resolve_build_workers(jobs)
        self.verify_workers = resolve_verify_workers(cores_per_job)
        self.verify_variants = verify_variants
        self.force_rebuild = force_rebuild
        self.use_inc_build = use_inc_build
        self.source_mode = source_mode
        self.build_only = build_only
        self.max_povs_per_cpv = max_povs_per_cpv
        self.skip_pov = skip_pov
        self.skip_unittest = skip_unittest
        self._inc_images_pulled: set[str] = set()
        self._temp_dirs: list[Path] = []
        if temp_runtime_dir is not None:
            self._temp_dirs.append(temp_runtime_dir)
        self._built_variants: list[str] = []  # Track variants for cleanup

    def verify_patch(
        self,
        benchmark_path: Path,
        patch: PatchInfo,
        harness: str,
        pov_path: Path,
        *,
        allow_build: bool = True,
    ) -> PatchVerificationResult:
        """Verify a single patch.

        Args:
            benchmark_path: Path to benchmark directory
            patch: Patch to verify
            harness: Harness name to test
            pov_path: Path to the POV file

        Returns:
            PatchVerificationResult with verification status
        """
        start_time = time.time()

        if self.build_only and not allow_build:
            return PatchVerificationResult(
                status=PatchVerificationStatus.ERROR,
                patch_id=patch.patch_id,
                pov_id=patch.pov_id,
                benchmark=benchmark_path.name,
                patch_path=patch.patch_path,
                details="build_only requires allow_build=True",
            )

        # Load benchmark adapter first to get benchmark_name
        adapter = self._load_adapter(benchmark_path)
        if not adapter:
            return PatchVerificationResult(
                status=PatchVerificationStatus.ERROR,
                patch_id=patch.patch_id,
                pov_id=patch.pov_id,
                benchmark=str(benchmark_path.name),
                patch_path=patch.patch_path,
                details=f"Failed to load benchmark: {benchmark_path}",
            )

        project_name = adapter.benchmark_name

        result = PatchVerificationResult(
            status=PatchVerificationStatus.PENDING,
            patch_id=patch.patch_id,
            pov_id=patch.pov_id,
            benchmark=project_name,
            patch_path=patch.patch_path,
        )
        main_repo = adapter.main_repo
        # For patch verification, use ref_commit (vulnerable code) in delta mode
        commit = adapter.get_ref_commit() or adapter.get_base_commit()
        repo_name = adapter.repo_name

        # Build variant name first to check cache
        build_config = BuildConfig(
            benchmark_name=project_name,
            benchmark_path=benchmark_path,
            variant_type=VariantType.PATCHED,
            mode=adapter.get_mode(),
            sanitizer=self.sanitizer,
            language=adapter.lang,
            commit=commit,
            main_repo=main_repo,
            patch_id=patch.patch_id,
            pov_id=patch.pov_id,
        )
        variant_name = build_config.variant_name

        # Check build cache (respect inc_build mode)
        build_cached = False
        if self.force_rebuild and not allow_build:
            result.status = PatchVerificationStatus.BUILD_FAILED
            result.details = (
                f"Missing prebuilt artifact for {variant_name} "
                "(allow_build=False, force_rebuild=True)"
            )
            return result

        if self.force_rebuild:
            logger.info(f"Force rebuild: cleaning {variant_name}")
            self.infra.cleanup_docker_images(variant_name)
            self.infra.cleanup_build_outputs(variant_name)
            self.infra.cleanup_source(variant_name)
        elif self.infra.is_variant_built(
            variant_name,
            require_inc_build=self.use_inc_build,
            # Patch verification requires a runnable source image for unit-test
            # execution (and for build-only split jobs that feed unittest jobs).
            require_source_image=(self.build_only or not self.skip_unittest),
        ):
            logger.debug(f"Using cached build for {variant_name}")
            # Track variant (for _built_variants list)
            self._built_variants.append(variant_name)
            build_cached = True
            # Cache hit preserves the requested inc-build mode. This must be
            # propagated so downstream split patch jobs use the same mode.
            result.inc_build_available = self.use_inc_build

            # Build-only mode: build already exists, nothing to do
            if self.build_only:
                result.status = PatchVerificationStatus.VALID
                result.elapsed_seconds = time.time() - start_time
                return result

            # For verification mode, continue to prepare source for unit tests

        if (not build_cached) and (not allow_build):
            result.status = PatchVerificationStatus.BUILD_FAILED
            result.details = (
                f"Missing prebuilt artifact for {variant_name} (allow_build=False)"
            )
            return result

        # Step 1: Ensure inc-build image is available (if enabled and not cached)
        inc_available = False
        if not build_cached and self.use_inc_build:
            inc_available = self._ensure_inc_build_image(
                project_name, benchmark_path=benchmark_path
            )
        elif not build_cached:
            logger.debug(f"Inc-build disabled, using full build for {project_name}")

        # Source and patch-apply are only needed when we might run unit tests.
        # POV-only verification against a cached patched build can skip this.
        needs_source_for_tests = not self.skip_unittest
        repo_path: Optional[Path] = None
        if (not build_cached) or needs_source_for_tests:
            # Step 2: Prepare source and apply patch
            # Use isolated source path if work_dir is set, otherwise temp directory
            if self.work_dir:
                src_dir = self.infra.get_isolated_src_path(variant_name)
                src_dir.mkdir(parents=True, exist_ok=True)
                candidate_repo_path = src_dir / "repo"
            else:
                temp_dir = Path(
                    tempfile.mkdtemp(prefix=f"patch-verify-{patch.pov_id}-")
                )
                self._temp_dirs.append(temp_dir)
                candidate_repo_path = temp_dir / "repo"

            try:
                source_path = self._prepare_source(
                    benchmark_path=benchmark_path,
                    dest_dir=candidate_repo_path,
                    main_repo=main_repo,
                    commit=commit,
                    repo_name=repo_name,
                )
            except RuntimeError as e:
                result.status = PatchVerificationStatus.BUILD_FAILED
                result.details = str(e)
                return result

            if not source_path:
                result.status = PatchVerificationStatus.BUILD_FAILED
                result.details = f"Failed to prepare source for {benchmark_path.name}"
                return result

            # Use the actual source path (may differ for bundled source).
            repo_path = source_path

            if not self._apply_patch(repo_path, patch):
                result.status = PatchVerificationStatus.BUILD_FAILED
                result.details = f"Failed to apply patch: {patch.patch_path}"
                return result
        else:
            logger.debug(
                f"Skipping source preparation and patch apply for cached POV-only "
                f"verification: {variant_name}"
            )

        # Step 3: Build fuzzers (snapshot path or full path)
        # - With snapshot_build: compile from replayable snapshot context
        # - With full_build: full rebuild from patched source
        # Both output fuzzers to /out for subsequent POV testing
        used_inc_build = None
        if build_cached:
            logger.debug(f"Skipping build for cached variant: {variant_name}")
        else:
            # Create variant project directory (symlink to benchmark)
            # All operations (build, reproduce, tests) use variant_name
            variant_project = self.infra.create_variant_project(
                benchmark_path=benchmark_path,
                variant_name=variant_name,
            )
            if not variant_project:
                result.status = PatchVerificationStatus.BUILD_FAILED
                result.details = f"Failed to create variant project: {variant_name}"
                return result

            # Prepare variant inc-build image if using inc-build
            if inc_available:
                if not self.infra.prepare_inc_image_for_variant(
                    project_name, variant_name, self.sanitizer
                ):
                    result.status = PatchVerificationStatus.BUILD_FAILED
                    result.details = "Failed to prepare inc-build image for variant"
                    return result

            # Use build_fuzzers for proper ASAN-instrumented binary
            # Inc-build: rsync patched source to /src/ and use replay-build semantics.
            # Fallback: Full build without inc-build (automatic on inc-build failure)
            used_inc_build = inc_available
            fallback_to_full = self.use_inc_build and not inc_available

            logger.info(
                f"Building {variant_name} via build_fuzzers (inc_build={inc_available})"
            )

            build_result = self.infra.build_fuzzers(
                build_config,
                repo_path,
                use_inc_image=inc_available,
            )

            # If inc-build failed, fallback to clean build from /src/
            # using the same inc-build image (deps already installed)
            if inc_available and (not build_result or not build_result.success):
                logger.warning(
                    f"Inc-build failed for {variant_name}, "
                    "falling back to clean build from /src/"
                )
                fallback_to_full = True
                used_inc_build = False

                self.infra.cleanup_build_outputs(variant_name)

                build_result = self.infra.build_fuzzers(
                    build_config,
                    repo_path,
                    use_inc_image=True,
                    snapshot_fallback=True,
                )

            # Record build time and output
            result.build_time = time.time() - start_time
            result.build_stdout = build_result.stdout if build_result else ""
            result.build_stderr = build_result.stderr if build_result else ""

            if not build_result or not build_result.success:
                result.status = PatchVerificationStatus.BUILD_FAILED
                base_details = (
                    "Build failed (inc-build and full build both failed)"
                    if fallback_to_full
                    else "Build failed"
                )
                failure_summary = _summarize_build_failure(
                    result.build_stdout, result.build_stderr
                )
                result.details = (
                    f"{base_details}: {failure_summary}"
                    if failure_summary
                    else base_details
                )
                return result

            # Verify harness binary exists
            build_out = self.infra.get_build_output_path(variant_name)
            harness_binary = build_out / harness
            if not harness_binary.exists():
                # Inc-build may return success while producing no harness output
                # (e.g., replay/snapshot mismatch). Treat this as an inc-build
                # failure and retry once via clean fallback build.
                if inc_available and not fallback_to_full:
                    logger.warning(
                        "Inc-build produced no harness output for %s/%s; "
                        "retrying clean fallback build",
                        variant_name,
                        harness,
                    )
                    fallback_to_full = True
                    used_inc_build = False

                    self.infra.cleanup_build_outputs(variant_name)
                    build_result = self.infra.build_fuzzers(
                        build_config,
                        repo_path,
                        use_inc_image=True,
                        snapshot_fallback=True,
                    )
                    result.build_stdout = build_result.stdout if build_result else ""
                    result.build_stderr = build_result.stderr if build_result else ""

                    if not build_result or not build_result.success:
                        result.status = PatchVerificationStatus.BUILD_FAILED
                        result.details = (
                            "Build failed (inc-build harness-miss fallback failed)"
                        )
                        result.elapsed_seconds = time.time() - start_time
                        self._built_variants.append(variant_name)
                        return result

                    build_out = self.infra.get_build_output_path(variant_name)
                    harness_binary = build_out / harness

                if not harness_binary.exists():
                    result.status = PatchVerificationStatus.BUILD_FAILED
                    result.details = (
                        f"Build failed - {harness} not found in {build_out}"
                    )
                    result.elapsed_seconds = time.time() - start_time
                    self._built_variants.append(variant_name)
                    return result

            # Track variant for cleanup
            self._built_variants.append(variant_name)

            # Write build metadata for cache validation
            # fallback_used: True if inc-build failed and we fell back to full build
            self.infra.write_build_metadata(
                variant_name,
                inc_build=used_inc_build,
                sanitizer=self.sanitizer,
                fallback_used=fallback_to_full,
            )

            # Track fallback status in result
            result.fallback_used = fallback_to_full
            result.inc_build_available = inc_available

            # Build-only mode: return after successful build
            if self.build_only:
                result.status = PatchVerificationStatus.VALID
                result.elapsed_seconds = time.time() - start_time
                return result

        # Run verification (POV tests + unit tests)
        result.harness = harness
        verified_result = self._run_verification(
            result,
            variant_name,
            harness,
            benchmark_path,
            project_name,
            used_inc_build=used_inc_build,
            repo_path=repo_path,
            pov_path=pov_path,
        )
        # Fix ownership of build output (including files created by reproduce)
        # Must run AFTER verification because reproduce() creates root-owned files
        build_out = self.infra.get_build_output_path(variant_name)
        fix_docker_ownership(build_out)

        verified_result.elapsed_seconds = time.time() - start_time
        return verified_result

    def _run_verification(
        self,
        result: PatchVerificationResult,
        variant_name: str,
        harness: str,
        benchmark_path: Path,
        project_name: str,
        *,
        used_inc_build: Optional[bool] = None,
        repo_path: Optional[Path] = None,
        pov_path: Optional[Path] = None,
    ) -> PatchVerificationResult:
        """Run POV and unit test verification for a built variant.

        Used by both fresh builds and cached builds.

        Args:
            result: Partial result to update
            variant_name: Variant name with built fuzzers
            harness: Harness name
            benchmark_path: Path to benchmark directory
            project_name: Project name
            used_inc_build: True if inc-build was used (None = detect)
            repo_path: Path to source repo (None = find from infra)
            pov_path: Path to single POV file for legacy mode (None = discover)

        Returns:
            Updated PatchVerificationResult
        """
        # For cached builds, detect inc_build and find repo_path
        if used_inc_build is None:
            # Check if inc-build image is available for the BASE project
            # Try to ensure it's available (pull if needed) when use_inc_build is enabled
            if self.use_inc_build:
                used_inc_build = self._ensure_inc_build_image(
                    project_name, benchmark_path=benchmark_path
                )
                logger.debug(
                    f"Ensured inc_build={used_inc_build} for cached build {project_name}"
                )
            else:
                used_inc_build = False
                logger.debug(f"Inc-build disabled for {project_name}")

        if repo_path is None:
            # Find source path from infra
            src_path = self.infra.get_isolated_src_path(variant_name) / "repo"
            if src_path.exists():
                repo_path = src_path
            else:
                logger.debug(
                    f"No source path found for {variant_name}, skip unit tests"
                )

        # Step 4: Run POV test (skip if skip_pov is True)
        pov_start_time = time.time()
        if self.skip_pov:
            logger.debug(f"Skipping POV tests for {variant_name} (skip_pov=True)")
            result.pov_test_passed = None  # Indicate POV was skipped
            result.pov_test_time = 0.0
        elif self.verify_variants:
            # Test all POV variants for this specific CPV
            cpv_id = result.pov_id
            pov_variants = self._discover_pov_variants(benchmark_path, harness, cpv_id)

            if self.max_povs_per_cpv and len(pov_variants) > self.max_povs_per_cpv:
                pov_variants = pov_variants[: self.max_povs_per_cpv]

            if not pov_variants:
                logger.warning(f"No POV variants found for {harness}/{cpv_id}")
                result.status = PatchVerificationStatus.ERROR
                result.details = f"No POV variants found for {cpv_id}"
                result.security_verdict = "FAIL"
                result.pov_test_time = time.time() - pov_start_time
                return result

            logger.info(
                f"Testing patch against {len(pov_variants)} POV variants for {cpv_id}"
            )

            # Test each variant
            variant_results: dict[str, bool] = {}
            variants_matched = 0

            for pov_path, (
                _pov_name,
                passed,
                pov_stdout,
                pov_stderr,
            ) in zip(
                pov_variants,
                self._run_pov_variant_checks(variant_name, harness, pov_variants),
                strict=False,
            ):
                variant_id = pov_path.stem
                self._write_verify_streams(
                    patch_id=result.patch_id,
                    cpv_id=cpv_id,
                    stage="pov",
                    run_id=variant_id,
                    stdout=pov_stdout,
                    stderr=pov_stderr,
                )
                variant_results[variant_id] = passed
                if passed:
                    variants_matched += 1

            # Build CpvStats for this CPV
            stats = CpvStats(
                cpv_id=cpv_id,
                variants_tested=len(pov_variants),
                variants_matched=variants_matched,
                variant_results=variant_results,
            )

            result.cpv_stats = {cpv_id: stats}
            result.pov_test_passed = stats.status == "complete"
            result.pov_test_time = time.time() - pov_start_time

            if stats.status == "complete":
                result.cpv_fixed = [cpv_id]
                logger.info(
                    f"  {cpv_id}: COMPLETE ({variants_matched}/{len(pov_variants)} variants)"
                )
            elif stats.status == "partial":
                logger.info(
                    f"  {cpv_id}: PARTIAL ({variants_matched}/{len(pov_variants)} variants)"
                )
                result.status = PatchVerificationStatus.POV_STILL_TRIGGERS
                result.details = f"Partial fix: {variants_matched}/{len(pov_variants)} variants passed"
                result.security_verdict = "FAIL"
                return result
            else:
                logger.info(
                    f"  {cpv_id}: NONE ({variants_matched}/{len(pov_variants)} variants)"
                )
                result.status = PatchVerificationStatus.POV_STILL_TRIGGERS
                result.details = "No POV variants passed"
                result.security_verdict = "FAIL"
                return result
        else:
            # Single POV test only (legacy behavior)
            # Use provided pov_path or discover it
            if pov_path is None or not pov_path.exists():
                pov_variants = self._discover_pov_variants(
                    benchmark_path, harness, result.pov_id
                )
                pov_path = pov_variants[0] if pov_variants else None

            if pov_path is None or not pov_path.exists():
                # Cannot verify patch without POV - this is an error
                logger.error(f"No POV found for {result.pov_id}, cannot verify patch")
                result.status = PatchVerificationStatus.ERROR
                result.details = f"No POV found for {result.pov_id}"
                result.security_verdict = "FAIL"
                result.pov_test_time = time.time() - pov_start_time
                return result

            _, pov_passed, pov_stdout, pov_stderr = self._verify_single_pov(
                variant_name, harness, pov_path
            )
            self._write_verify_streams(
                patch_id=result.patch_id,
                cpv_id=result.pov_id,
                stage="pov",
                run_id=pov_path.stem,
                stdout=pov_stdout,
                stderr=pov_stderr,
            )
            result.pov_test_passed = pov_passed
            result.pov_test_time = time.time() - pov_start_time

            if not pov_passed:
                result.status = PatchVerificationStatus.POV_STILL_TRIGGERS
                result.details = "POV still triggers vulnerability after patch"
                result.security_verdict = "FAIL"
                return result

        # Step 5: Run unit tests (skip if skip_unittest is True)
        # Some callers may treat unit tests as already handled during build-only flows.
        unit_test_start_time = time.time()
        if self.skip_unittest:
            logger.debug(f"Skipping unit tests for {variant_name} (skip_unittest=True)")
            if result.unit_tests_passed is None:
                result.unit_tests_passed = None  # Indicate unit tests were skipped
            result.unit_test_time = result.unit_test_time or 0.0
        elif result.unit_tests_passed is not None:
            # Unit tests already handled by build-only flow.
            logger.debug(
                f"Unit tests already ran during build for {variant_name}: "
                f"passed={result.unit_tests_passed}"
            )
            # Check the cached result
            if not result.unit_tests_passed:
                result.status = PatchVerificationStatus.TEST_FAILED
                result.details = "Unit tests failed"
                result.security_verdict = "FAIL"
                return result
        elif repo_path is None:
            # Skip unit tests for cached builds without source
            logger.info(
                f"Skipping unit tests for {variant_name} (no source, cached build)"
            )
            result.unit_tests_passed = None
        elif used_inc_build:
            # Retag inc-build image for variant (enables parallel test execution)
            if not self.infra.prepare_inc_image_for_variant(
                project_name, variant_name, self.sanitizer
            ):
                logger.warning(
                    f"Failed to prepare inc-build image for {variant_name}, "
                    "skipping unit tests"
                )
                result.unit_tests_passed = None
            else:
                test_passed, test_details, test_stdout, test_stderr = (
                    self._run_unit_tests(
                        variant_name,
                        repo_path,
                        benchmark_path,
                        use_inc_image=True,
                    )
                )
                self._write_verify_streams(
                    patch_id=result.patch_id,
                    cpv_id=result.pov_id,
                    stage="unittest",
                    run_id=self.test_mode.value,
                    stdout=test_stdout,
                    stderr=test_stderr,
                )
                result.unit_tests_passed = test_passed
                result.unit_test_time = time.time() - unit_test_start_time

                # None = skipped (e.g., RTS without inc-build); only fail on False
                if test_passed is False:
                    result.status = PatchVerificationStatus.TEST_FAILED
                    result.details = test_details or "Unit tests failed"
                    result.security_verdict = "FAIL"
                    return result
        else:
            # Standard build: use variant's Docker image (already built)
            test_passed, test_details, test_stdout, test_stderr = self._run_unit_tests(
                variant_name,
                repo_path,
                benchmark_path,
                use_inc_image=False,
            )
            self._write_verify_streams(
                patch_id=result.patch_id,
                cpv_id=result.pov_id,
                stage="unittest",
                run_id=self.test_mode.value,
                stdout=test_stdout,
                stderr=test_stderr,
            )
            result.unit_tests_passed = test_passed
            result.unit_test_time = time.time() - unit_test_start_time

            # None = skipped (e.g., RTS without inc-build); only fail on False
            if test_passed is False:
                result.status = PatchVerificationStatus.TEST_FAILED
                result.details = test_details or "Unit tests failed"
                result.security_verdict = "FAIL"
                return result

        # All checks passed (or skipped)
        result.status = PatchVerificationStatus.VALID
        # Determine details based on what was run
        if self.skip_pov and self.skip_unittest:
            result.details = "Build only (no verification)"
        elif self.skip_pov:
            result.details = "Unit tests passed (POV skipped)"
        elif self.skip_unittest:
            result.details = "POV tests passed (unit tests skipped)"
        else:
            result.details = "Patch is valid"
        # security_verdict = PASS requires: at least 1 CPV complete + tests pass
        # When skip_pov is used, security cannot be verified, so leave as default "FAIL"
        if not self.skip_pov:
            result.security_verdict = "PASS"
        return result

    def verify_patches(
        self,
        benchmark_path: Path,
        patch_dir: Path,
        harness: str,
        pov_dir: Path,
        *,
        allow_build: bool = True,
    ) -> list[PatchVerificationResult]:
        """Verify multiple patches.

        Discovers patches from patch_dir and verifies each one.
        Supported structures:
        - Structured: patch_dir/<pov_id>/patch.diff
        - Flat: patch_dir/<patch_id>.diff (mapped to trial CPV when inferable)

        Args:
            benchmark_path: Path to benchmark directory
            patch_dir: Directory containing patches
            harness: Harness name to test
            pov_dir: Directory containing POV files

        Returns:
            List of PatchVerificationResult
        """
        target_pov_id = self._infer_single_pov_id(pov_dir)
        # Discover patches
        patches = self._discover_patches(patch_dir, target_pov_id=target_pov_id)
        if not patches:
            logger.warning(f"No patches found in {patch_dir}")
            return []

        logger.info(f"Found {len(patches)} patches to verify")
        results: list[PatchVerificationResult] = []

        # Get benchmark name for error results and pre-cache resource keys
        adapter = self._load_adapter(benchmark_path)
        benchmark_name = adapter.benchmark_name if adapter else str(benchmark_path.name)

        # Create tasks
        tasks: list[tuple[PatchInfo, Path]] = []
        for patch in patches:
            pov_path = self._find_pov_for_patch(pov_dir, patch.pov_id)
            if not pov_path:
                results.append(
                    PatchVerificationResult(
                        status=PatchVerificationStatus.ERROR,
                        patch_id=patch.patch_id,
                        pov_id=patch.pov_id,
                        benchmark=benchmark_name,
                        patch_path=patch.patch_path,
                        details=f"POV not found for {patch.pov_id}",
                    )
                )
                continue
            tasks.append((patch, pov_path))

        # Pre-cache resources before parallel execution
        if tasks and len(tasks) > 1:
            project_name = benchmark_name

            # Pre-cache repository to avoid parallel clones
            if adapter:
                main_repo = adapter.main_repo
                commit = adapter.get_ref_commit() or adapter.get_base_commit()
                self._ensure_repo_cached(main_repo, commit, adapter.repo_name)

            # Pre-pull inc-build image (if enabled)
            if self.use_inc_build:
                self._ensure_inc_build_image(
                    project_name, benchmark_path=benchmark_path
                )

        for patch, pov_path in tasks:
            result = self.verify_patch(
                benchmark_path,
                patch,
                harness,
                pov_path,
                allow_build=allow_build,
            )
            results.append(result)

        return results

    def _ensure_inc_build_image(
        self,
        project_name: str,
        benchmark_path: Optional[Path] = None,
    ) -> bool:
        """Ensure inc-build image is available.

        Args:
            project_name: OSS-Fuzz project name
            benchmark_path: Optional benchmark path for creating the
                `third_party/oss-fuzz/projects/<project_name>` alias.

        Returns:
            True if inc-build image is available
        """
        cache_key = f"{project_name}:{self.sanitizer}"

        # Check if already known to be available
        if cache_key in self._inc_images_pulled:
            return True

        if benchmark_path is not None:
            self.infra.create_variant_project(
                benchmark_path=benchmark_path,
                variant_name=project_name,
            )

        if self.infra.ensure_inc_image(
            project_name,
            self.sanitizer,
            benchmark_path=benchmark_path,
        ):
            self._inc_images_pulled.add(cache_key)
            return True

        logger.debug(
            f"Inc-build image not available for {project_name}, will use standard build"
        )
        return False

    def _ensure_repo_cached(
        self, main_repo: str, commit: str, repo_name: Optional[str] = None
    ) -> None:
        """Ensure repository is cached before parallel execution.

        This prevents multiple workers from cloning the same repo concurrently.
        The first clone populates the cache, subsequent clones copy from cache.

        Only applies to main_repo mode - bundled source (pkgs) doesn't need caching.

        Args:
            main_repo: Repository URL
            commit: Commit hash
            repo_name: Optional repository name for cache key
        """
        # Skip caching in pkgs mode - bundled source doesn't need git repos
        if self.source_mode == "pkgs":
            logger.debug("Using pkgs source mode, skipping repo cache")
            return

        # Clone to a temp directory just to populate the cache
        with tempfile.TemporaryDirectory(prefix="repo-cache-") as temp_dir:
            target = str(Path(temp_dir) / "repo")
            clone_or_copy_cached_repo(
                repo_url=main_repo,
                commit=commit,
                target_dir=target,
                repo_name=repo_name,
                verbose=True,
            )
        logger.debug(f"Repository cache ensured for {main_repo}@{commit[:8]}")

    def _apply_patch(self, repo_path: Path, patch: PatchInfo) -> bool:
        """Apply a patch to the repository.

        Args:
            repo_path: Path to repository
            patch: Patch to apply

        Returns:
            True if successful
        """
        if not patch.patch_content.strip():
            logger.warning(f"Empty patch for {patch.pov_id}")
            return False

        return self.infra.apply_patch(repo_path, patch.patch_path)

    def _run_pov_test(
        self,
        variant_name: str,
        harness: str,
        pov_path: Path,
    ) -> bool:
        """Run POV test against patched code.

        A valid patch should NOT crash.

        Args:
            variant_name: Variant name (fuzzers are at build/out/{variant_name}/)
            harness: Harness name
            pov_path: Path to POV file

        Returns:
            True if POV does NOT crash (patch is valid)
        """
        _, passed, _, _ = self._verify_single_pov(variant_name, harness, pov_path)
        return passed

    def _verify_single_pov(
        self,
        variant_name: str,
        harness: str,
        pov_path: Path,
    ) -> tuple[str, bool, str, str]:
        """Verify a single POV against patched code.

        Core POV verification logic used by both single POV test and variant tests.

        Args:
            variant_name: Variant name (fuzzers are at build/out/{variant_name}/)
            harness: Harness name
            pov_path: Path to POV blob file

        Returns:
            Tuple of (pov_name, passed, stdout, stderr) where passed=True if POV
            does NOT crash
        """
        pov_name = pov_path.name

        if not pov_path.exists():
            logger.error(f"POV file not found: {pov_path}")
            return pov_name, False, "", ""

        pov_data = pov_path.read_bytes()
        logger.debug(f"Running POV: {pov_name} against {variant_name}/{harness}")

        output = self.infra.reproduce(
            project_name=variant_name,
            harness=harness,
            pov_data=pov_data,
            timeout=self.timeout,
            pov_id=pov_name,
        )

        passed = not output.crashed
        if not passed:
            logger.debug(f"  ✗ {pov_name}: POV still triggers crash")
        else:
            logger.debug(f"  ✓ {pov_name}: passed")

        return pov_name, passed, output.stdout or "", output.stderr or ""

    @staticmethod
    def _sanitize_log_token(token: str) -> str:
        """Sanitize token for log file names."""
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", token).strip("_")
        return safe or "unknown"

    def _get_verify_log_dir(self) -> Path:
        """Return base log directory for patch verification stream files."""
        base_dir = self.work_dir or self.log_dir or Path.cwd()
        log_dir = base_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        return log_dir

    def _write_verify_streams(
        self,
        *,
        patch_id: str,
        cpv_id: str,
        stage: str,
        run_id: str,
        stdout: str,
        stderr: str,
    ) -> None:
        """Write stream-separated logs for a single verification run."""
        log_dir = self._get_verify_log_dir()
        file_base = (
            f"patch-{self._sanitize_log_token(patch_id)}"
            f"__cpv-{self._sanitize_log_token(cpv_id)}"
            f"__{self._sanitize_log_token(stage)}"
            f"__{self._sanitize_log_token(run_id)}"
        )
        (log_dir / f"{file_base}.stdout").write_text(stdout or "")
        (log_dir / f"{file_base}.stderr").write_text(stderr or "")

    def _run_unit_tests(
        self,
        variant_name: str,
        src_path: Path,
        benchmark_path: Path,
        *,
        use_inc_image: bool = False,
    ) -> tuple[bool | None, str, str, str]:
        """Run unit tests on patched code.

        Uses a separate test variant (variant_name-unittest or variant_name-rts)
        to avoid overwriting ASAN binary from build_fuzzers.

        Args:
            variant_name: Base variant name (for Docker image lookup)
            src_path: Path to patched source code
            benchmark_path: Path to benchmark directory
            use_inc_image: If True, use inc tag; else use latest

        Returns:
            Tuple of (passed, details)
        """
        # Check if tests are available (test.sh exists in variant project directory)
        if not self.infra.is_tests_available(variant_name):
            logger.warning(f"No test.sh in {variant_name}, skipping unit tests")
            return True, "", "", ""

        # RTS tests require /rts_config_jvm.py which is only in inc-build images.
        # Skip RTS tests when inc-build is not available to avoid false failures.
        # Return None (not True) to indicate skipped, not passed. The caller at
        # flat.py PatchUnitTestJob.execute() uses `unit_tests_passed is True` which
        # correctly treats None as not-success, propagating skip status.
        if self.test_mode == UnitTestMode.RTS and not use_inc_image:
            logger.warning(
                f"RTS tests require inc-build image, but inc-build not available. "
                f"Skipping RTS tests for {variant_name}"
            )
            return None, "RTS skipped: inc-build not available", "", ""

        # Use separate variant for test output to avoid overwriting ASAN binary
        # test.sh rebuilds with fuzz-shim which produces non-ASAN binaries
        test_suffix = "rts" if self.test_mode == UnitTestMode.RTS else "unittest"
        test_variant_name = f"{variant_name}-{test_suffix}"

        # Create test variant project directory
        self.infra.create_variant_project(
            benchmark_path=benchmark_path,
            variant_name=test_variant_name,
        )

        # Copy build artifacts from base variant to test variant
        # The test variant needs the binaries from the build to execute tests
        if not self.infra.copy_build_output(variant_name, test_variant_name):
            logger.warning(
                f"Failed to copy build output from {variant_name} to "
                f"{test_variant_name}, tests may fail"
            )

        # Prepare Docker image for test variant
        # Test variant uses a different name (-unittest or -rts suffix) to avoid
        # overwriting ASAN binary from build_fuzzers.
        if use_inc_image:
            # For inc-build: retag :inc image from base project
            # Extract base project name by stripping sanitizer and mode suffixes
            # Variant format: {benchmark}-{san_short}-{mode}-patched-{pov_id}-{patch_id}
            base_project = variant_name.rsplit("-patched-", 1)[0]
            if "-patched-" in variant_name:
                # Strip sanitizer suffix (asan, ubsan, msan, tsan, cov)
                sanitizer_suffixes = [
                    "-asan-",
                    "-ubsan-",
                    "-msan-",
                    "-tsan-",
                    "-cov-",
                ]
                for suffix in sanitizer_suffixes:
                    if suffix in base_project:
                        base_project = base_project.rsplit(suffix, 1)[0]
                        break
            if not self.infra.prepare_inc_image_for_variant(
                base_project, test_variant_name, self.sanitizer
            ):
                logger.warning(
                    f"Failed to prepare inc-build image for {test_variant_name}, "
                    "tests may fail"
                )
        else:
            # For standard build: retag :latest image from source variant
            # Without this, test container run fails with image-not-found.
            if not self.infra.prepare_image_for_variant(
                variant_name, test_variant_name, docker_tag="latest"
            ):
                logger.warning(
                    f"Failed to prepare test image for {test_variant_name}, "
                    "tests may fail"
                )

        # For inc-build: use :inc tag
        # For standard build: use latest tag
        docker_tag = "inc" if use_inc_image else "latest"
        passed, stdout, stderr = self.infra.run_tests(
            test_variant_name,
            src_path,
            sanitizer=self.sanitizer,
            timeout=self.test_timeout,
            rts_mode=(self.test_mode == UnitTestMode.RTS),
            docker_tag=docker_tag,
        )

        if passed:
            return True, "", stdout or "", stderr or ""
        # Preserve both streams so CI reports include actionable failure details.
        details_parts = []
        if stderr and stderr.strip():
            details_parts.append(stderr.strip())
        if stdout and stdout.strip():
            details_parts.append(stdout.strip())
        return (
            False,
            "\n\n".join(details_parts) or "Unit tests failed",
            stdout or "",
            stderr or "",
        )

    def _discover_pov_variants(
        self,
        benchmark_path: Path,
        harness: str,
        cpv_id: str,
    ) -> list[Path]:
        """Discover all POV variants for a CPV from the blobs directory.

        POV variants are stored in: .aixcc/<harness>/<cpv_id>/blobs/pov_*.blob

        Args:
            benchmark_path: Path to benchmark directory
            harness: Harness name
            cpv_id: CPV identifier (e.g., cpv_0)

        Returns:
            List of paths to POV blob files, sorted by POV number
        """
        blobs_dir = benchmark_path / ".aixcc" / harness / cpv_id / "blobs"

        if not blobs_dir.exists():
            logger.debug(f"Blobs directory not found: {blobs_dir}")
            return []

        pov_files = list(blobs_dir.glob("pov_*.blob"))

        # Sort by POV number (pov_0, pov_1, pov_2, ...)
        def extract_pov_num(path: Path) -> int:
            try:
                # pov_0.blob -> 0
                return int(path.stem.split("_")[1])
            except (IndexError, ValueError):
                return 999

        pov_files.sort(key=extract_pov_num)

        logger.debug(f"Found {len(pov_files)} POV variants for {harness}/{cpv_id}")
        return pov_files

    def _parse_unique_id(self, unique_id: str) -> tuple[str, str, str]:
        """Parse unique_id into harness, cpv_id, pov_id.

        Format: {harness}_{cpv_id}_{pov_id}
        Example: html_cpv_0_pov_0 -> (html, cpv_0, pov_0)

        Args:
            unique_id: Unique identifier string

        Returns:
            Tuple of (harness, cpv_id, pov_id)
        """
        parts = unique_id.rsplit("_", 3)

        if len(parts) >= 4 and parts[-3] == "cpv" and parts[-1].startswith("pov"):
            # Expected format: harness_cpv_N_pov_M (e.g., html_cpv_0_pov_0)
            harness = "_".join(parts[:-4]) if len(parts) > 4 else parts[0]
            cpv_id = f"cpv_{parts[-2]}"
            pov_id = (
                f"pov_{parts[-1].split('_')[-1]}" if "_" in parts[-1] else parts[-1]
            )
            return harness, cpv_id, pov_id

        # Fallback: try to find cpv_ and pov_ markers
        if "_cpv_" in unique_id and "_pov_" in unique_id:
            cpv_idx = unique_id.index("_cpv_")
            pov_idx = unique_id.index("_pov_")
            harness = unique_id[:cpv_idx]
            cpv_id = unique_id[cpv_idx + 1 : pov_idx]
            pov_id = unique_id[pov_idx + 1 :]
            return harness, cpv_id, pov_id

        raise ValueError(
            f"Invalid unique_id format: '{unique_id}'. "
            "Expected format: {{harness}}_cpv_{{N}}_pov_{{M}} (e.g., html_cpv_0_pov_0)"
        )

    def _run_pov_variants_test(
        self,
        variant_name: str,
        harness: str,
        pov_paths: list[Path],
    ) -> tuple[bool, list[str]]:
        """Test patch against multiple POV variants in parallel.

        Args:
            variant_name: Variant name (fuzzers are at build/out/{variant_name}/)
            harness: Harness name
            pov_paths: List of POV file paths to test

        Returns:
            Tuple of (all_passed, list of failed POV names)
        """
        if not pov_paths:
            return True, []

        logger.info(
            f"Testing {len(pov_paths)} POV variants against {variant_name}/{harness}"
        )

        failed_povs: list[str] = []

        for pov_name, passed, _, _ in self._run_pov_variant_checks(
            variant_name,
            harness,
            pov_paths,
        ):
            if not passed:
                failed_povs.append(pov_name)

        all_passed = len(failed_povs) == 0
        if all_passed:
            logger.info(f"All {len(pov_paths)} POV variants passed")
        else:
            logger.info(f"{len(failed_povs)}/{len(pov_paths)} POV variants failed")

        return all_passed, failed_povs

    def _run_pov_variant_checks(
        self,
        variant_name: str,
        harness: str,
        pov_paths: list[Path],
    ) -> Iterable[tuple[str, bool, str, str]]:
        """Run POV variant checks using configured per-patch verification parallelism."""
        if len(pov_paths) <= 1 or self.verify_workers <= 1:
            for pov_path in pov_paths:
                yield self._verify_single_pov(variant_name, harness, pov_path)
            return

        max_workers = min(self.verify_workers, len(pov_paths))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            yield from executor.map(
                lambda pov_path: self._verify_single_pov(
                    variant_name, harness, pov_path
                ),
                pov_paths,
            )

    def _discover_patches(
        self, patch_dir: Path, target_pov_id: Optional[str] = None
    ) -> list[PatchInfo]:
        """Discover patches in directory.

        Supported structures:
        - patch_dir/<pov_id>/patch.diff
        - patch_dir/*.diff (flat; target_pov_id required for CPV mapping)

        Args:
            patch_dir: Directory containing patches
            target_pov_id: CPV/POV ID that flat patches should target

        Returns:
            List of PatchInfo
        """
        patches = []

        if not patch_dir.exists():
            return patches

        for pov_subdir in patch_dir.iterdir():
            if not pov_subdir.is_dir():
                continue

            patch_file = pov_subdir / "patch.diff"
            if patch_file.exists():
                patches.append(
                    PatchInfo(
                        patch_id=f"patch_{len(patches)}",  # Auto-generate patch_id
                        pov_id=pov_subdir.name,
                        patch_path=patch_file,
                    )
                )

        # Flat layout from CRS output collector:
        # output/patches/<patch_id>.diff
        flat_patches = sorted(
            p
            for p in patch_dir.glob("*.diff")
            if p.is_file() and not p.name.startswith(".")
        )
        if flat_patches:
            if not target_pov_id:
                logger.warning(
                    f"Found {len(flat_patches)} flat patch files in {patch_dir} "
                    "but no target CPV/POV could be inferred; skipping flat patches"
                )
            else:
                for patch_file in flat_patches:
                    patches.append(
                        PatchInfo(
                            patch_id=patch_file.stem,
                            pov_id=target_pov_id,
                            patch_path=patch_file,
                        )
                    )

        return patches

    def _infer_single_pov_id(self, pov_dir: Path) -> Optional[str]:
        """Infer a single target POV/CPV identifier from pov_dir.

        In patch-generation trials, crs-input/povs usually contains a single
        CPV input (e.g., ``cpv_0``). This lets flat patch files map back to the
        correct target during verification.
        """
        if not pov_dir.exists():
            return None

        candidates = sorted(
            p.name
            for p in pov_dir.iterdir()
            if not p.name.startswith(".") and (p.is_file() or p.is_dir())
        )
        return candidates[0] if len(candidates) == 1 else None

    def _discover_patches_from_benchmark(
        self, benchmark_path: Path, harness_filter: Optional[str] = None
    ) -> list[tuple[str, str, PatchInfo]]:
        """Discover patches from .aixcc/<harness>/<cpv>/patches/

        Structure: .aixcc/<harness>/<cpv>/patches/patch_*.diff

        Args:
            benchmark_path: Path to benchmark directory
            harness_filter: Optional harness to filter (None = all harnesses)

        Returns:
            List of (harness, cpv_id, PatchInfo) tuples
        """
        aixcc_dir = benchmark_path / ".aixcc"
        if not aixcc_dir.exists():
            logger.warning(f".aixcc directory not found: {aixcc_dir}")
            return []

        discovered: list[tuple[str, str, PatchInfo]] = []

        # Iterate through harness directories
        for harness_dir in aixcc_dir.iterdir():
            if not harness_dir.is_dir():
                continue

            harness = harness_dir.name

            # Skip meta.yaml and other non-harness entries
            if harness.endswith(".yaml") or harness.startswith("."):
                continue

            # Apply harness filter if specified
            if harness_filter and harness != harness_filter:
                continue

            # Iterate through CPV directories
            for cpv_dir in harness_dir.iterdir():
                if not cpv_dir.is_dir() or not cpv_dir.name.startswith("cpv_"):
                    continue

                cpv_id = cpv_dir.name
                patches_dir = cpv_dir / "patches"

                if not patches_dir.exists():
                    continue

                # Iterate through patch files (patch_*.diff)
                for patch_file in patches_dir.glob("patch_*.diff"):
                    if not patch_file.is_file():
                        continue

                    # Extract patch ID from filename (e.g., patch_0.diff -> patch_0)
                    patch_id = patch_file.stem
                    patch_info = PatchInfo(
                        patch_id=patch_id,
                        pov_id=cpv_id,  # The CPV this patch targets
                        patch_path=patch_file,
                    )
                    discovered.append((harness, cpv_id, patch_info))

        logger.info(f"Discovered {len(discovered)} patches in {benchmark_path}")
        return discovered

    def verify_benchmark(
        self,
        benchmark_path: Path,
        harness: Optional[str] = None,
        *,
        allow_build: bool = True,
    ) -> PatchBenchmarkOutput:
        """Verify all patches in a benchmark using auto-discovery.

        Discovers patches from .aixcc/<harness>/<cpv>/patches/ and verifies each.

        Args:
            benchmark_path: Path to benchmark directory
            harness: Optional harness filter (None = all harnesses)

        Returns:
            PatchBenchmarkOutput containing:
            - results: List of PatchVerificationResult
            - fallback_used: True if any patch build used fallback to standard build
        """
        # Discover all patches
        discovered = self._discover_patches_from_benchmark(benchmark_path, harness)

        if not discovered:
            logger.warning(f"No patches found in {benchmark_path}")
            return PatchBenchmarkOutput(results=[], fallback_used=False)

        # Collect unique harnesses for logging
        harnesses = sorted({h for h, _, _ in discovered})
        logger.info(
            f"Verifying {len(discovered)} patches across {len(harnesses)} harnesses: "
            f"{harnesses}"
        )

        # Prepare all tasks: (harness, patch, pov_path)
        # Skip patches without POVs
        tasks: list[tuple[str, PatchInfo, Path]] = []
        results: list[PatchVerificationResult] = []
        for h, cpv_id, patch in discovered:
            pov_variants = self._discover_pov_variants(benchmark_path, h, cpv_id)
            if not pov_variants:
                logger.warning(f"No POV found for {cpv_id} in {h}, skipping patch")
                # Add error result for this patch
                error_result = PatchVerificationResult(
                    patch_id=patch.patch_id,
                    pov_id=patch.pov_id,
                    benchmark=benchmark_path.name,
                    harness=h,
                    patch_path=patch.patch_path,
                    status=PatchVerificationStatus.ERROR,
                    details=f"No POV found for {cpv_id}",
                    security_verdict="FAIL",
                )
                results.append(error_result)
                continue
            pov_path = pov_variants[0]
            tasks.append((h, patch, pov_path))

        # Pre-cache resources before parallel execution
        if tasks and len(tasks) > 1:
            adapter = self._load_adapter(benchmark_path)
            project_name = (
                adapter.benchmark_name if adapter else str(benchmark_path.name)
            )

            # Pre-cache repository to avoid parallel clones
            if adapter:
                main_repo = adapter.main_repo
                commit = adapter.get_ref_commit() or adapter.get_base_commit()
                self._ensure_repo_cached(main_repo, commit, adapter.repo_name)

            # Pre-pull inc-build image (if enabled)
            if self.use_inc_build:
                self._ensure_inc_build_image(
                    project_name, benchmark_path=benchmark_path
                )

        # Deduplicate tasks by variant key (cpv_id, patch_id).
        # Different harnesses with the same CPV produce the same variant_name,
        # causing race conditions if built in parallel. This is a benchmark
        # data quality issue (see #80) — skip duplicates with a warning.
        seen_variants: set[str] = set()
        deduped_tasks: list[tuple[str, PatchInfo, Path]] = []
        for h, patch, pov_path in tasks:
            variant_key = f"{patch.pov_id}-{patch.patch_id}"
            if variant_key in seen_variants:
                logger.warning(
                    f"Skipping duplicate patch {variant_key} under harness "
                    f"{h} (already queued from another harness)"
                )
                continue
            seen_variants.add(variant_key)
            deduped_tasks.append((h, patch, pov_path))

        for h, patch, pov_path in deduped_tasks:
            result = self.verify_patch(
                benchmark_path,
                patch,
                h,
                pov_path,
                allow_build=allow_build,
            )
            results.append(result)

        # Compute overall fallback status
        fallback_used = any(r.fallback_used for r in results)
        return PatchBenchmarkOutput(results=results, fallback_used=fallback_used)

    def _find_pov_for_patch(
        self,
        pov_dir: Path,
        pov_id: str,
    ) -> Optional[Path]:
        """Find POV file for a patch.

        Args:
            pov_dir: Directory containing POVs
            pov_id: POV identifier

        Returns:
            Path to POV file, or None if not found
        """
        # Try exact match first
        for ext in [".bin", ".pov", ".blob", ""]:
            pov_path = pov_dir / f"{pov_id}{ext}"
            if pov_path.exists():
                return pov_path

        # Try in subdirectory
        pov_subdir = pov_dir / pov_id
        if pov_subdir.is_dir():
            for pov_file in pov_subdir.glob("*"):
                if pov_file.is_file():
                    return pov_file

        return None

    def _extract_repo_name(self, project_name: str) -> str:
        """Extract repository name from project name.

        Pattern: {prefix}-{repo}-{mode}-{num} -> {repo}

        Args:
            project_name: OSS-Fuzz project name

        Returns:
            Repository name
        """
        parts = project_name.split("-")
        if len(parts) >= 4:
            return "-".join(parts[1:-2])
        if len(parts) == 3:
            return parts[1]
        return project_name

    def _load_adapter(self, benchmark_path: Path) -> Optional[MetaYamlAdapter]:
        """Load MetaYamlAdapter from benchmark path.

        Args:
            benchmark_path: Path to benchmark directory

        Returns:
            MetaYamlAdapter or None if loading fails
        """
        import yaml

        from crsbench.validation.meta_adapter import MetaYamlAdapter

        meta_yaml = benchmark_path / ".aixcc" / "meta.yaml"
        project_yaml = benchmark_path / "project.yaml"

        if not meta_yaml.exists():
            logger.error(f"meta.yaml not found: {meta_yaml}")
            return None

        benchmark_name = benchmark_path.name
        lang = "c"
        main_repo = ""
        repo_name = None

        if project_yaml.exists():
            with project_yaml.open() as f:
                project_data = yaml.safe_load(f)
            lang = project_data.get("language", "c")
            main_repo = project_data.get("main_repo", "")
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

    def _prepare_source(
        self,
        benchmark_path: Path,
        dest_dir: Path,
        main_repo: str,
        commit: str,
        repo_name: Optional[str] = None,
    ) -> Optional[Path]:
        """Prepare source for patch verification - from pkgs/ or git clone.

        Source mode determines how source is obtained:
        - "main_repo": Clone from main_repo in project.yaml (default)
        - "pkgs": Use bundled tarballs from pkgs/ (requires pkgs/)

        For patch verification, ref.diff is always applied (patches target ref_commit).

        Args:
            benchmark_path: Path to benchmark directory
            dest_dir: Destination directory for source
            main_repo: Repository URL (used for main_repo mode)
            commit: Commit hash (used for main_repo mode)
            repo_name: Optional repository name

        Returns:
            Path to prepared source, or None on failure

        Raises:
            RuntimeError: If source_mode is "pkgs" but no pkgs/ exists
        """
        # Handle main_repo mode - always clone from git
        if self.source_mode == "main_repo":
            logger.debug("Using main_repo source (cloning from git)")
            clone_result = clone_or_copy_cached_repo(
                repo_url=main_repo,
                commit=commit,
                target_dir=str(dest_dir),
                repo_name=repo_name,
                verbose=True,
            )
            return dest_dir if clone_result else None

        # pkgs mode (default) - use bundled source
        from crsbench.benchmark.packaging import get_expected_source_dir
        from crsbench.benchmark.runtime import (
            has_bundled_source,
            prepare_source_from_bundle,
        )

        if not has_bundled_source(benchmark_path):
            raise RuntimeError(
                f"No bundled source (pkgs/) found for {benchmark_path.name}. "
                "Run 'crsbench benchmark bundle' first, or use --source main_repo."
            )

        # Get source name from Dockerfile WORKDIR
        dockerfile = benchmark_path / "Dockerfile"
        source_name = get_expected_source_dir(dockerfile)
        if not source_name:
            # Fall back to repo_name or benchmark name
            source_name = repo_name or benchmark_path.name
            logger.debug(f"No WORKDIR found, using: {source_name}")

        # Bundled tarball already has correct commit structure (done at packaging):
        # - Both modes: 1 squashed commit at vulnerable state
        # - Delta mode provides ref.diff as hint (not via git history)
        # Just extract and use - no post-processing needed.
        logger.debug(f"Using bundled source: {source_name}.tar.gz")
        return prepare_source_from_bundle(
            benchmark_path,
            dest_dir.parent,  # prepare_source_from_bundle creates subdirectory
            source_name,
        )

    def cleanup(self) -> None:
        """Clean up temporary directories only.

        Preserves:
        - Build outputs (for caching and debugging)
        - Project symlinks (lightweight, no harm)
        - Build symlinks (needed for reproduce)

        Only removes:
        - Temporary source directories (created when work_dir not set)

        Use --force-rebuild to clean and rebuild from scratch.
        """
        # Clean up temp directories only
        for temp_dir in self._temp_dirs:
            if temp_dir.exists():
                try:
                    # Build/reproduce containers may leave root-owned files in
                    # patch-verify temp dirs; normalize ownership before removal.
                    fix_docker_ownership(temp_dir)
                    shutil.rmtree(temp_dir)
                except Exception as e:
                    # Last-resort Docker-backed recursive delete for root-owned
                    # leftovers from interrupted/failed container executions.
                    if not docker_rmtree(temp_dir):
                        logger.warning(f"Failed to clean up {temp_dir}: {e}")
        self._temp_dirs.clear()

        # Build outputs, project symlinks, and build symlinks are preserved
        # for caching and debugging. Use --force-rebuild to clean and rebuild.

        self._built_variants.clear()
