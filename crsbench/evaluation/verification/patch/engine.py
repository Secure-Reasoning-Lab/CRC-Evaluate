"""PatchVerificationEngine for validating CRS-generated patches.

This module provides the main orchestrator for patch verification:
1. Pull inc-build image (if available)
2. Clone source and apply patch
3. Build with inc-build image (for faster incremental builds)
4. Run POV test (should NOT crash)
5. Run unit tests (optional)
"""

from __future__ import annotations

import shutil
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from crsbench.builder import OSSFuzzInfrastructure
from crsbench.builder.types import BuildConfig, VariantType
from crsbench.evaluation.verification.models import (
    CpvStats,
    PatchInfo,
    PatchVerificationResult,
    PatchVerificationStatus,
    UnitTestMode,
)
from crsbench.utils.docker import fix_docker_ownership
from crsbench.utils.logger import get_logger
from crsbench.utils.repo_manager import clone_or_copy_cached_repo
from crsbench.utils.workers import resolve_build_workers, resolve_verify_workers

if TYPE_CHECKING:
    from crsbench.validation.meta_adapter import MetaYamlAdapter

logger = get_logger(__name__)


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
    3. Clone source repository
    4. Apply patch
    5. Build with inc-build image
    6. Run POV test (patch should prevent crash)
    7. Run unit tests (optional, to ensure no regressions)

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
        timeout: int = 120,
        build_timeout: int = 1200,
        test_timeout: int = 1800,
        build_workers: Optional[int] = None,
        verify_workers: Optional[int] = None,
        verify_variants: bool = True,
        work_dir: Optional[Path] = None,
        force_rebuild: bool = False,
        use_inc_build: bool = True,
        source_mode: str = "pkgs",
    ):
        """Initialize the patch verification engine.

        Args:
            oss_fuzz_path: Path to oss-fuzz directory
            test_mode: Unit test execution mode
            sanitizer: Sanitizer type (address, undefined, memory)
            timeout: Timeout for reproduce operations in seconds
            build_timeout: Timeout for build operations in seconds
            test_timeout: Timeout for unit test execution in seconds
            build_workers: Number of parallel workers for patch builds (None = use
                default). Controls how many patches can build simultaneously in
                verify_patches() and verify_benchmark().
            verify_workers: Number of parallel workers for POV variant testing
                (None = use default). Controls parallelism within a single patch
                verification when testing against multiple POV variants.
            verify_variants: If True, verify patch against all POV variants
            work_dir: Working directory for isolated builds. If provided, builds
                are isolated to this directory with symlinks to oss-fuzz/build/out/
                for helper.py compatibility. Use this for experiment-specific builds.
            force_rebuild: If True, clean and rebuild even if build exists.
            use_inc_build: If True, use incremental builds when available (faster).
                If False, always use full OSS-Fuzz build.
            source_mode: Source mode - "pkgs" (bundled, default) or "main_repo" (clone)
        """
        self.oss_fuzz_path = Path(oss_fuzz_path)
        self.work_dir = Path(work_dir) if work_dir else None
        self.infra = OSSFuzzInfrastructure(oss_fuzz_path, work_dir=work_dir)
        self.test_mode = test_mode
        self.sanitizer = sanitizer
        self.timeout = timeout
        self.build_timeout = build_timeout
        self.test_timeout = test_timeout
        self.build_workers = resolve_build_workers(build_workers)
        self.verify_workers = resolve_verify_workers(verify_workers)
        self.verify_variants = verify_variants
        self.force_rebuild = force_rebuild
        self.use_inc_build = use_inc_build
        self.source_mode = source_mode
        self._inc_images_pulled: set[str] = set()
        self._inc_images_unavailable: set[str] = set()  # Cache failed pulls
        self._temp_dirs: list[Path] = []
        self._built_variants: list[str] = []  # Track variants for cleanup

    def verify_patch(
        self,
        benchmark_path: Path,
        patch: PatchInfo,
        harness: str,
        pov_path: Path,
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

        # Check build cache
        if self.force_rebuild:
            logger.info(f"Force rebuild: cleaning {variant_name}")
            self.infra.cleanup_build_outputs(variant_name)
            self.infra.cleanup_source(variant_name)
        elif self.infra.is_variant_built(variant_name):
            logger.info(f"Using cached build for {variant_name}")
            # Track variant (for _built_variants list)
            self._built_variants.append(variant_name)
            # Skip to verification (build_time stays 0 for cached builds)
            result.harness = harness
            verified_result = self._run_verification(
                result, variant_name, harness, benchmark_path, project_name
            )
            verified_result.elapsed_seconds = time.time() - start_time
            return verified_result

        # Step 1: Ensure inc-build image is available (if enabled)
        if self.use_inc_build:
            inc_available = self._ensure_inc_build_image(project_name)
        else:
            inc_available = False
            logger.info(f"Inc-build disabled, using full build for {project_name}")

        # Step 2: Prepare source and apply patch
        # Use isolated source path if work_dir is set, otherwise use temp directory
        if self.work_dir:
            # Use isolated source path
            src_dir = self.infra.get_isolated_src_path(variant_name)
            src_dir.mkdir(parents=True, exist_ok=True)
            repo_path = src_dir / "repo"
        else:
            # Use temp directory (legacy behavior)
            temp_dir = Path(tempfile.mkdtemp(prefix=f"patch-verify-{patch.pov_id}-"))
            self._temp_dirs.append(temp_dir)
            repo_path = temp_dir / "repo"

        try:
            source_path = self._prepare_source(
                benchmark_path=benchmark_path,
                dest_dir=repo_path,
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

        # Use the actual source path (may differ from repo_path for bundled source)
        repo_path = source_path

        # Apply the patch
        if not self._apply_patch(repo_path, patch):
            result.status = PatchVerificationStatus.BUILD_FAILED
            result.details = f"Failed to apply patch: {patch.patch_path}"
            return result

        # Step 3: Build with inc-build image or regular build
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

        # Build using helper.py build_fuzzers (with inc-build image if available)
        build_success = self.infra.build_fuzzers(
            build_config, repo_path, use_inc_image=inc_available
        )
        used_inc_build = inc_available

        # Record build time
        result.build_time = time.time() - start_time

        # Track variant for cleanup (even if build failed, we may have partial artifacts)
        self._built_variants.append(variant_name)

        if not build_success:
            result.status = PatchVerificationStatus.BUILD_FAILED
            if not result.details:
                result.details = "Build failed"
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
            # Check if inc-build image exists for this variant
            inc_image = f"aixcc-afc/{variant_name}:inc-{self.sanitizer}"
            used_inc_build = self.infra._docker_image_exists(inc_image)
            logger.debug(f"Detected inc_build={used_inc_build} for {variant_name}")

        if repo_path is None:
            # Find source path from infra
            src_path = self.infra.get_isolated_src_path(variant_name) / "repo"
            if src_path.exists():
                repo_path = src_path
            else:
                logger.debug(
                    f"No source path found for {variant_name}, skip unit tests"
                )

        # Step 4: Run POV test
        pov_start_time = time.time()
        if self.verify_variants:
            # Test all POV variants for this specific CPV
            cpv_id = result.pov_id
            pov_variants = self._discover_pov_variants(benchmark_path, harness, cpv_id)

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

            with ThreadPoolExecutor(max_workers=self.verify_workers) as executor:
                futures = {
                    executor.submit(
                        self._verify_single_pov, variant_name, harness, pov_path
                    ): pov_path
                    for pov_path in pov_variants
                }

                for future in as_completed(futures):
                    pov_path = futures[future]
                    pov_name, passed = future.result()
                    variant_id = pov_path.stem
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

            pov_passed = self._run_pov_test(variant_name, harness, pov_path)
            result.pov_test_passed = pov_passed
            result.pov_test_time = time.time() - pov_start_time

            if not pov_passed:
                result.status = PatchVerificationStatus.POV_STILL_TRIGGERS
                result.details = "POV still triggers vulnerability after patch"
                result.security_verdict = "FAIL"
                return result

        # Step 5: Run unit tests (if source available)
        unit_test_start_time = time.time()
        if repo_path is None:
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
                test_passed, test_details = self._run_unit_tests(
                    variant_name,
                    repo_path,
                    use_inc_image=True,
                )
                result.unit_tests_passed = test_passed
                result.unit_test_time = time.time() - unit_test_start_time

                if not test_passed:
                    result.status = PatchVerificationStatus.TEST_FAILED
                    result.details = test_details or "Unit tests failed"
                    result.security_verdict = "FAIL"
                    return result
        else:
            # Standard build: use variant's Docker image (already built)
            test_passed, test_details = self._run_unit_tests(
                variant_name,
                repo_path,
                use_inc_image=False,
            )
            result.unit_tests_passed = test_passed
            result.unit_test_time = time.time() - unit_test_start_time

            if not test_passed:
                result.status = PatchVerificationStatus.TEST_FAILED
                result.details = test_details or "Unit tests failed"
                result.security_verdict = "FAIL"
                return result

        # All checks passed
        result.status = PatchVerificationStatus.VALID
        result.details = "Patch is valid"
        # security_verdict = PASS requires: at least 1 CPV complete + tests pass
        result.security_verdict = "PASS"
        return result

    def verify_patches(
        self,
        benchmark_path: Path,
        patch_dir: Path,
        harness: str,
        pov_dir: Path,
        *,
        parallel: bool = True,
    ) -> list[PatchVerificationResult]:
        """Verify multiple patches.

        Discovers patches from patch_dir and verifies each one.
        Structure expected: patch_dir/<pov_id>/patch.diff

        Args:
            benchmark_path: Path to benchmark directory
            patch_dir: Directory containing patches
            harness: Harness name to test
            pov_dir: Directory containing POV files
            parallel: If True, run verifications in parallel

        Returns:
            List of PatchVerificationResult
        """
        # Discover patches
        patches = self._discover_patches(patch_dir)
        if not patches:
            logger.warning(f"No patches found in {patch_dir}")
            return []

        logger.info(f"Found {len(patches)} patches to verify")
        results: list[PatchVerificationResult] = []

        # Get benchmark name for error results
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
            project_name = benchmark_path.name

            # Pre-cache repository to avoid parallel clones
            adapter = self._load_adapter(benchmark_path)
            if adapter:
                main_repo = adapter.main_repo
                commit = adapter.get_ref_commit() or adapter.get_base_commit()
                self._ensure_repo_cached(main_repo, commit, adapter.repo_name)

            # Pre-pull inc-build image (if enabled)
            if self.use_inc_build:
                self._ensure_inc_build_image(project_name)

        if not parallel or len(tasks) <= 1:
            # Sequential verification
            for patch, pov_path in tasks:
                result = self.verify_patch(benchmark_path, patch, harness, pov_path)
                results.append(result)
        else:
            # Parallel patch builds and verification
            # Use build_workers since builds dominate execution time
            with ThreadPoolExecutor(max_workers=self.build_workers) as executor:
                futures = {}
                for patch, pov_path in tasks:
                    future = executor.submit(
                        self.verify_patch,
                        benchmark_path,
                        patch,
                        harness,
                        pov_path,
                    )
                    futures[future] = patch

                for future in as_completed(futures):
                    result = future.result()
                    results.append(result)

        return results

    def _ensure_inc_build_image(self, project_name: str) -> bool:
        """Ensure inc-build image is available.

        Args:
            project_name: OSS-Fuzz project name

        Returns:
            True if inc-build image is available
        """
        cache_key = f"{project_name}:{self.sanitizer}"

        # Check if already known to be available
        if cache_key in self._inc_images_pulled:
            return True

        # Check if already known to be unavailable (avoid retrying failed pulls)
        if cache_key in self._inc_images_unavailable:
            return False

        if self.infra.is_inc_image_available(project_name, self.sanitizer):
            self._inc_images_pulled.add(cache_key)
            return True

        if self.infra.pull_inc_build_image(project_name, self.sanitizer):
            self._inc_images_pulled.add(cache_key)
            return True

        # Cache the failure to avoid retrying
        self._inc_images_unavailable.add(cache_key)
        logger.warning(
            f"Inc-build image not available for {project_name}, "
            "will use standard build (slower)"
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
        _, passed = self._verify_single_pov(variant_name, harness, pov_path)
        return passed

    def _verify_single_pov(
        self,
        variant_name: str,
        harness: str,
        pov_path: Path,
    ) -> tuple[str, bool]:
        """Verify a single POV against patched code.

        Core POV verification logic used by both single POV test and variant tests.

        Args:
            variant_name: Variant name (fuzzers are at build/out/{variant_name}/)
            harness: Harness name
            pov_path: Path to POV blob file

        Returns:
            Tuple of (pov_name, passed) where passed=True if POV does NOT crash
        """
        pov_name = pov_path.name

        if not pov_path.exists():
            logger.error(f"POV file not found: {pov_path}")
            return pov_name, False

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

        return pov_name, passed

    def _run_unit_tests(
        self,
        variant_name: str,
        src_path: Path,
        *,
        use_inc_image: bool = False,
    ) -> tuple[bool, str]:
        """Run unit tests on patched code.

        Uses variant_name for both Docker image and test.sh lookup, enabling
        proper parallel execution with path isolation.

        Args:
            variant_name: Variant name (for Docker image and test.sh lookup)
            src_path: Path to patched source code
            use_inc_image: If True, use inc-{sanitizer} tag; else use latest

        Returns:
            Tuple of (passed, details)
        """
        # Check if tests are available (test.sh exists in variant project directory)
        if not self.infra.is_tests_available(variant_name):
            logger.warning(f"No test.sh in {variant_name}, skipping unit tests")
            return True, ""

        # For inc-build: use inc-{sanitizer} tag (image was retagged to variant)
        # For standard build: use latest tag (variant image was built)
        docker_tag = f"inc-{self.sanitizer}" if use_inc_image else "latest"
        passed, stdout, stderr = self.infra.run_tests(
            variant_name,
            src_path,
            sanitizer=self.sanitizer,
            timeout=self.test_timeout,
            rts_mode=(self.test_mode == UnitTestMode.RTS),
            docker_image_tag=docker_tag,
        )

        if passed:
            return True, ""
        return False, stderr or "Unit tests failed"

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

        # Run POV tests in parallel
        with ThreadPoolExecutor(max_workers=self.verify_workers) as executor:
            futures = {
                executor.submit(
                    self._verify_single_pov, variant_name, harness, pov_path
                ): pov_path
                for pov_path in pov_paths
            }

            for future in as_completed(futures):
                pov_name, passed = future.result()
                if not passed:
                    failed_povs.append(pov_name)

        all_passed = len(failed_povs) == 0
        if all_passed:
            logger.info(f"All {len(pov_paths)} POV variants passed")
        else:
            logger.info(f"{len(failed_povs)}/{len(pov_paths)} POV variants failed")

        return all_passed, failed_povs

    def _discover_patches(self, patch_dir: Path) -> list[PatchInfo]:
        """Discover patches in directory.

        Expected structure: patch_dir/<pov_id>/patch.diff

        Args:
            patch_dir: Directory containing patches

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

        return patches

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
        parallel: bool = True,
    ) -> list[PatchVerificationResult]:
        """Verify all patches in a benchmark using auto-discovery.

        Discovers patches from .aixcc/<harness>/<cpv>/patches/ and verifies each.

        Args:
            benchmark_path: Path to benchmark directory
            harness: Optional harness filter (None = all harnesses)
            parallel: Run in parallel

        Returns:
            List of PatchVerificationResult
        """
        # Discover all patches
        discovered = self._discover_patches_from_benchmark(benchmark_path, harness)

        if not discovered:
            logger.warning(f"No patches found in {benchmark_path}")
            return []

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
            project_name = benchmark_path.name

            # Pre-cache repository to avoid parallel clones
            adapter = self._load_adapter(benchmark_path)
            if adapter:
                main_repo = adapter.main_repo
                commit = adapter.get_ref_commit() or adapter.get_base_commit()
                self._ensure_repo_cached(main_repo, commit, adapter.repo_name)

            # Pre-pull inc-build image (if enabled)
            if self.use_inc_build:
                self._ensure_inc_build_image(project_name)

        if not parallel or len(tasks) <= 1:
            # Sequential verification
            for h, patch, pov_path in tasks:
                result = self.verify_patch(benchmark_path, patch, h, pov_path)
                results.append(result)
        else:
            # Parallel patch builds and verification across all harnesses
            # Use build_workers since builds dominate execution time
            with ThreadPoolExecutor(max_workers=self.build_workers) as executor:
                futures = {}
                for h, patch, pov_path in tasks:
                    future = executor.submit(
                        self.verify_patch,
                        benchmark_path,
                        patch,
                        h,
                        pov_path,
                    )
                    futures[future] = (h, patch)

                for future in as_completed(futures):
                    result = future.result()
                    results.append(result)

        return results

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
        - "pkgs": Use bundled tarballs from pkgs/ (default, requires pkgs/)
        - "main_repo": Clone from main_repo in project.yaml

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
            logger.info("Using main_repo source (cloning from git)")
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

        # For patch verification, always apply ref.diff
        # Patches are designed to apply on top of ref_commit state
        logger.info(f"Using bundled source: {source_name}.tar.gz (apply_ref_diff=True)")
        return prepare_source_from_bundle(
            benchmark_path,
            dest_dir.parent,  # prepare_source_from_bundle creates subdirectory
            source_name,
            apply_ref_diff=True,
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
                    shutil.rmtree(temp_dir)
                except Exception as e:
                    logger.warning(f"Failed to clean up {temp_dir}: {e}")
        self._temp_dirs.clear()

        # Build outputs, project symlinks, and build symlinks are preserved
        # for caching and debugging. Use --force-rebuild to clean and rebuild.

        self._built_variants.clear()
