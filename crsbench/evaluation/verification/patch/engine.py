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
from crsbench.evaluation.verification.models import (
    PatchInfo,
    PatchVerificationResult,
    PatchVerificationStatus,
    TestMode,
)
from crsbench.utils.logger import get_logger
from crsbench.utils.repo_manager import clone_or_copy_cached_repo
from crsbench.utils.workers import resolve_verify_workers

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
        verify_workers: Number of parallel workers
    """

    def __init__(
        self,
        oss_fuzz_path: Path,
        *,
        test_mode: TestMode = TestMode.FULL,
        sanitizer: str = "address",
        timeout: int = 120,
        build_timeout: int = 1200,
        test_timeout: int = 1800,
        verify_workers: Optional[int] = None,
    ):
        """Initialize the patch verification engine.

        Args:
            oss_fuzz_path: Path to oss-fuzz directory
            test_mode: Unit test execution mode
            sanitizer: Sanitizer type (address, undefined, memory)
            timeout: Timeout for reproduce operations in seconds
            build_timeout: Timeout for build operations in seconds
            test_timeout: Timeout for unit test execution in seconds
            verify_workers: Number of parallel workers (None = use default)
        """
        self.oss_fuzz_path = Path(oss_fuzz_path)
        self.infra = OSSFuzzInfrastructure(oss_fuzz_path)
        self.test_mode = test_mode
        self.sanitizer = sanitizer
        self.timeout = timeout
        self.build_timeout = build_timeout
        self.test_timeout = test_timeout
        self.verify_workers = resolve_verify_workers(verify_workers)
        self._inc_images_pulled: set[str] = set()
        self._temp_dirs: list[Path] = []

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
        result = PatchVerificationResult(
            status=PatchVerificationStatus.PENDING,
            pov_id=patch.pov_id,
            patch_path=patch.patch_path,
        )

        # Load benchmark adapter
        adapter = self._load_adapter(benchmark_path)
        if not adapter:
            result.status = PatchVerificationStatus.ERROR
            result.details = f"Failed to load benchmark: {benchmark_path}"
            return result

        project_name = adapter.benchmark_name
        main_repo = adapter.main_repo
        # For patch verification, use ref_commit (vulnerable code) in delta mode
        commit = adapter.get_ref_commit() or adapter.get_base_commit()
        repo_name = adapter.repo_name

        # Step 1: Ensure inc-build image is available
        inc_available = self._ensure_inc_build_image(project_name)

        # Step 2: Clone source and apply patch
        temp_dir = Path(tempfile.mkdtemp(prefix=f"patch-verify-{patch.pov_id}-"))
        self._temp_dirs.append(temp_dir)
        repo_path = temp_dir / "repo"

        clone_result = clone_or_copy_cached_repo(
            repo_url=main_repo,
            commit=commit,
            target_dir=str(repo_path),
            repo_name=repo_name,
            verbose=True,
        )
        if not clone_result:
            result.status = PatchVerificationStatus.BUILD_FAILED
            result.details = f"Failed to clone repository: {main_repo}"
            return result

        # Apply the patch
        if not self._apply_patch(repo_path, patch):
            result.status = PatchVerificationStatus.BUILD_FAILED
            result.details = f"Failed to apply patch: {patch.patch_path}"
            return result

        # Step 3: Build with inc-build image or regular build
        if inc_available:
            build_success = self.infra.build_with_inc_image(
                project_name,
                repo_path,
                repo_name=repo_name or self._extract_repo_name(project_name),
                sanitizer=self.sanitizer,
                timeout=self.build_timeout,
            )
        else:
            # Fallback: need to create a variant project for regular build
            # This is a simplified approach - full implementation would use OSSFuzzBuilder
            build_success = False
            result.details = (
                "Inc-build image not available, regular build not implemented"
            )

        result.build_time = time.time() - start_time

        if not build_success:
            result.status = PatchVerificationStatus.BUILD_FAILED
            if not result.details:
                result.details = "Build failed"
            return result

        # Step 4: Run POV test - patch should prevent crash
        pov_passed = self._run_pov_test(project_name, harness, pov_path)
        result.pov_test_passed = pov_passed

        if not pov_passed:
            result.status = PatchVerificationStatus.POV_STILL_TRIGGERS
            result.details = "POV still triggers vulnerability after patch"
            return result

        # Step 5: Run unit tests
        test_passed, test_details = self._run_unit_tests(project_name, repo_path)
        result.unit_tests_passed = test_passed

        if not test_passed:
            result.status = PatchVerificationStatus.TEST_FAILED
            result.details = test_details or "Unit tests failed"
            return result

        # All checks passed
        result.status = PatchVerificationStatus.VALID
        result.details = "Patch is valid"
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

        # Create tasks
        tasks: list[tuple[PatchInfo, Path]] = []
        for patch in patches:
            pov_path = self._find_pov_for_patch(pov_dir, patch.pov_id)
            if not pov_path:
                results.append(
                    PatchVerificationResult(
                        status=PatchVerificationStatus.ERROR,
                        pov_id=patch.pov_id,
                        patch_path=patch.patch_path,
                        details=f"POV not found for {patch.pov_id}",
                    )
                )
                continue
            tasks.append((patch, pov_path))

        if not parallel or len(tasks) <= 1:
            # Sequential verification
            for patch, pov_path in tasks:
                result = self.verify_patch(benchmark_path, patch, harness, pov_path)
                results.append(result)
        else:
            # Parallel verification
            with ThreadPoolExecutor(max_workers=self.verify_workers) as executor:
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
        if cache_key in self._inc_images_pulled:
            return True

        if self.infra.is_inc_image_available(project_name, self.sanitizer):
            self._inc_images_pulled.add(cache_key)
            return True

        if self.infra.pull_inc_build_image(project_name, self.sanitizer):
            self._inc_images_pulled.add(cache_key)
            return True

        logger.warning(f"Inc-build image not available for {project_name}")
        return False

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
        project_name: str,
        harness: str,
        pov_path: Path,
    ) -> bool:
        """Run POV test against patched code.

        A valid patch should NOT crash.

        Args:
            project_name: OSS-Fuzz project name
            harness: Harness name
            pov_path: Path to POV file

        Returns:
            True if POV does NOT crash (patch is valid)
        """
        if not pov_path.exists():
            logger.error(f"POV file not found: {pov_path}")
            return False

        pov_data = pov_path.read_bytes()
        logger.info(
            f"Running POV test: {pov_path.name} against {project_name}/{harness}"
        )

        crashed = self.infra.reproduce(
            project_name=project_name,
            harness=harness,
            pov_data=pov_data,
            timeout=self.timeout,
        )

        if crashed:
            logger.info("POV still triggers crash - patch did not fix the bug")
            return False
        logger.info("POV did not crash - patch may be valid")
        return True

    def _run_unit_tests(
        self,
        project_name: str,
        src_path: Path,
    ) -> tuple[bool, str]:
        """Run unit tests on patched code.

        Args:
            project_name: OSS-Fuzz project name
            src_path: Path to patched source code

        Returns:
            Tuple of (passed, details)
        """
        passed, stdout, stderr = self.infra.run_tests(
            project_name,
            src_path,
            sanitizer=self.sanitizer,
            timeout=self.test_timeout,
            rts_mode=(self.test_mode == TestMode.RTS),
            docker_image_tag=f"inc-{self.sanitizer}",
        )

        if passed:
            return True, ""
        return False, stderr or "Unit tests failed"

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
                        pov_id=pov_subdir.name,
                        patch_path=patch_file,
                    )
                )

        return patches

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

    def cleanup(self) -> None:
        """Clean up temporary directories."""
        for temp_dir in self._temp_dirs:
            if temp_dir.exists():
                try:
                    shutil.rmtree(temp_dir)
                except Exception as e:
                    logger.warning(f"Failed to clean up {temp_dir}: {e}")
        self._temp_dirs.clear()
