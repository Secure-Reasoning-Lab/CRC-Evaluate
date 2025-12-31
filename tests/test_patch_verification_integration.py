"""E2E tests for patch verification using ground truth patches.

This module provides E2E tests that validate ground truth patches from benchmarks
using the PatchVerificationEngine.

Default projects tested:
- sanity-mock-c-delta-01
- afc-libxml2-delta-01
- afc-libxml2-full-01

Usage:
    pytest tests/test_e2e_patch_verification.py -v
"""

import os
import subprocess
from pathlib import Path
from typing import Optional

import pytest
from crsbench.evaluation.verification.models import (
    PatchInfo,
    PatchVerificationResult,
    PatchVerificationStatus,
    TestMode,
)
from crsbench.evaluation.verification.patch import PatchVerificationEngine
from crsbench.utils.logger import get_logger

from tests.test_patch_verification import GroundTruthConverter

logger = get_logger(__name__)


# =============================================================================
# Test Projects Configuration
# =============================================================================

E2E_PROJECTS = [
    "sanity-mock-c-delta-01",
    "afc-libxml2-delta-01",
    "afc-libxml2-full-01",
]

DEFAULT_SANITIZER = "address"


# =============================================================================
# Helper Functions
# =============================================================================


def get_project_root() -> Path:
    """Get the CRSBench project root directory."""
    return Path(__file__).parent.parent


def get_oss_fuzz_path() -> Optional[Path]:
    """Get path to oss-fuzz directory."""
    if os.environ.get("OSS_FUZZ_HOME"):
        return Path(os.environ["OSS_FUZZ_HOME"])

    candidates = [
        get_project_root().parent / "oss-fuzz",
        Path.home() / "oss-fuzz",
        Path("/oss-fuzz"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate

    return None


def is_docker_available() -> bool:
    """Check if Docker daemon is running."""
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=10,
        )
        return result.returncode == 0
    except Exception:
        return False


def is_inc_build_image_available(project_name: str, sanitizer: str = "address") -> bool:
    """Check if inc-build image is available locally or remotely.

    Checks multiple image formats:
    1. OSS-Fuzz format: aixcc-afc/{project}:inc-{sanitizer}
    2. Registry format: ghcr.io/team-atlanta/crsbench/{project}:inc-{sanitizer}
    3. Nested format: aixcc-afc/aixcc/c/{project}:inc-{sanitizer}

    Also attempts to pull from registry if not found locally.
    """
    image_formats = [
        f"aixcc-afc/{project_name}:inc-{sanitizer}",
        f"ghcr.io/team-atlanta/crsbench/{project_name}:inc-{sanitizer}",
        f"aixcc-afc/aixcc/c/{project_name}:inc-{sanitizer}",
    ]

    # Check if any format exists locally
    for image_name in image_formats:
        try:
            result = subprocess.run(
                ["docker", "image", "inspect", image_name],
                capture_output=True,
                timeout=30,
            )
            if result.returncode == 0:
                return True
        except Exception:
            continue

    # Try to pull from registry
    registry_image = f"ghcr.io/team-atlanta/crsbench/{project_name}:inc-{sanitizer}"
    try:
        result = subprocess.run(
            ["docker", "pull", registry_image],
            capture_output=True,
            timeout=300,
        )
        return result.returncode == 0
    except Exception:
        return False


def check_e2e_prerequisites(
    project_name: str,
    benchmark_path: Path,
    sanitizer: str = DEFAULT_SANITIZER,
) -> Optional[str]:
    """Check all prerequisites for E2E tests.

    Returns:
        None if all prerequisites are met, otherwise a skip reason string.
    """
    if not is_docker_available():
        return "Docker daemon not available"

    oss_fuzz = get_oss_fuzz_path()
    if not oss_fuzz or not oss_fuzz.exists():
        return "OSS-Fuzz directory not found. Set OSS_FUZZ_HOME env var."

    if not is_inc_build_image_available(project_name, sanitizer):
        return (
            f"Inc-build image not available for {project_name}. "
            f"Pull with: docker pull ghcr.io/team-atlanta/crsbench/{project_name}:inc-{sanitizer}"
        )

    oss_fuzz_project = oss_fuzz / "projects" / project_name
    if not oss_fuzz_project.exists():
        return f"Project not linked to oss-fuzz. Run: ln -s {benchmark_path} {oss_fuzz_project}"

    return None


def run_e2e_patch_verification(
    benchmark_path: Path,
    oss_fuzz_path: Path,
    harness: Optional[str] = None,
    test_mode: TestMode = TestMode.FULL,
    sanitizer: str = DEFAULT_SANITIZER,
    timeout: int = 120,
    build_timeout: int = 1200,
) -> list[PatchVerificationResult]:
    """Run E2E patch verification for a benchmark using ground truth patches.

    Args:
        benchmark_path: Path to benchmark directory
        oss_fuzz_path: Path to oss-fuzz directory
        harness: Optional harness name filter
        test_mode: Unit test mode (FULL or RTS)
        sanitizer: Sanitizer type
        timeout: POV timeout in seconds
        build_timeout: Build timeout in seconds

    Returns:
        List of PatchVerificationResult for each ground truth patch
    """
    results: list[PatchVerificationResult] = []
    converter = GroundTruthConverter(benchmark_path)

    try:
        patch_dir, pov_dir, gt_patches = converter.convert(harness)
        logger.info(f"Found {len(gt_patches)} ground truth patches")

        # Group patches by harness
        harness_patches: dict[str, list] = {}
        for patch in gt_patches:
            if patch.harness not in harness_patches:
                harness_patches[patch.harness] = []
            harness_patches[patch.harness].append(patch)

        # Create engine
        engine = PatchVerificationEngine(
            oss_fuzz_path=oss_fuzz_path,
            test_mode=test_mode,
            sanitizer=sanitizer,
            timeout=timeout,
            build_timeout=build_timeout,
        )

        try:
            # Verify each patch individually
            for harness_name, patches in harness_patches.items():
                logger.info(
                    f"Verifying {len(patches)} patches for harness: {harness_name}"
                )

                for gt_patch in patches:
                    patch_info = PatchInfo(
                        pov_id=gt_patch.unique_id,
                        patch_path=patch_dir / gt_patch.unique_id / "patch.diff",
                    )
                    pov_path = pov_dir / f"{gt_patch.unique_id}.blob"

                    result = engine.verify_patch(
                        benchmark_path=benchmark_path,
                        patch=patch_info,
                        harness=harness_name,
                        pov_path=pov_path,
                    )
                    results.append(result)
        finally:
            engine.cleanup()

    finally:
        converter.cleanup()

    return results


# =============================================================================
# E2E Tests
# =============================================================================


class TestE2EPatchVerification:
    """E2E tests for patch verification across multiple projects."""

    @pytest.mark.parametrize("project", E2E_PROJECTS)
    def test_ground_truth_patches(self, project: str):
        """Test that all ground truth patches pass verification.

        This test:
        1. Discovers ground truth patches from the benchmark
        2. Converts them to CRS output format
        3. Runs patch verification for each patch
        4. Asserts all patches are valid
        """
        benchmark_path = get_project_root() / "benchmarks" / project
        if not benchmark_path.exists():
            pytest.skip(f"Benchmark not found: {benchmark_path}")

        # Check prerequisites
        skip_reason = check_e2e_prerequisites(project, benchmark_path)
        if skip_reason:
            pytest.skip(skip_reason)

        oss_fuzz = get_oss_fuzz_path()
        assert oss_fuzz is not None  # Already checked in prerequisites

        logger.info(f"Running E2E patch verification for: {project}")

        results = run_e2e_patch_verification(
            benchmark_path=benchmark_path,
            oss_fuzz_path=oss_fuzz,
            harness=None,
            test_mode=TestMode.FULL,
            sanitizer=DEFAULT_SANITIZER,
        )

        assert len(results) > 0, (
            f"Should have at least one patch to verify for {project}"
        )

        # Print summary
        valid_count = sum(
            1 for r in results if r.status == PatchVerificationStatus.VALID
        )
        logger.info(f"Results: {valid_count}/{len(results)} valid")

        for r in results:
            status_icon = "V" if r.status == PatchVerificationStatus.VALID else "X"
            logger.info(f"  {status_icon} {r.pov_id}: {r.status.value}")

        # All ground truth patches should be valid
        failed_patches = [
            r for r in results if r.status != PatchVerificationStatus.VALID
        ]
        if failed_patches:
            failure_details = "\n".join(
                f"  - {r.pov_id}: {r.status.value}\n    {r.details}"
                for r in failed_patches
            )
            pytest.fail(f"Ground truth patch verification failed:\n{failure_details}")

    @pytest.mark.parametrize("project", E2E_PROJECTS)
    def test_converter_discovery(self, project: str):
        """Test that ground truth patches are correctly discovered."""
        benchmark_path = get_project_root() / "benchmarks" / project
        if not benchmark_path.exists():
            pytest.skip(f"Benchmark not found: {benchmark_path}")

        converter = GroundTruthConverter(benchmark_path)
        patches = converter.discover_patches()

        assert len(patches) > 0, f"Should find patches in {project}"

        logger.info(f"Found {len(patches)} ground truth patches in {project}:")
        for patch in patches:
            logger.info(f"  - {patch.unique_id}")
            assert patch.patch_path.exists(), f"Patch should exist: {patch.patch_path}"
            assert patch.pov_path.exists(), f"POV should exist: {patch.pov_path}"
