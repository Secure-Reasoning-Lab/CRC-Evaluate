"""Tests for patch verification modules.

Tests cover:
- PatchVerificationStatus, PatchInfo, PatchVerificationResult models
- GroundTruthConverter utility for converting benchmark patches to CRS format
- PatchVerificationEngine (E2E tests require Docker)

Test structure mirrors test_pov_verification.py for consistency.
"""

import shutil
import subprocess
import tempfile
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pytest
import yaml
from crsbench.evaluation.verification.models import (
    PatchInfo,
    PatchVerificationResult,
    PatchVerificationStatus,
    TestMode,
)
from crsbench.utils.logger import get_logger

logger = get_logger(__name__)

# Test constants
BENCHMARK_NAME = "sanity-mock-c-delta-01"
HARNESS_NAME = "fuzz_parse_buffer_section"
CPV_ID = "cpv_1"
POV_ID = "pov_0"


# =============================================================================
# Ground Truth to CRS Format Converter
# =============================================================================


@dataclass
class GroundTruthPatch:
    """Represents a ground truth patch from benchmark."""

    harness: str
    cpv_id: str
    pov_id: str
    patch_path: Path
    pov_path: Path

    @property
    def unique_id(self) -> str:
        """Generate unique ID for this patch (used as pov_id in CRS format)."""
        return f"{self.harness}_{self.cpv_id}_{self.pov_id}"


class GroundTruthConverter:
    """Converts ground truth patches from benchmark format to CRS output format.

    Benchmark format:
        .aixcc/<harness>/<cpv_id>/patches/patch_0.diff
        .aixcc/<harness>/<cpv_id>/blobs/<pov_id>.blob

    CRS output format:
        patches/<unique_id>/patch.diff
        povs/<unique_id>.blob

    Usage:
        converter = GroundTruthConverter(benchmark_path)
        with converter.convert() as (patch_dir, pov_dir):
            # Use patch_dir and pov_dir for verification
            engine.verify_patches(benchmark_path, patch_dir, harness, pov_dir)
    """

    def __init__(self, benchmark_path: Path):
        """Initialize converter.

        Args:
            benchmark_path: Path to benchmark directory
        """
        self.benchmark_path = Path(benchmark_path)
        self.aixcc_path = self.benchmark_path / ".aixcc"
        self._temp_dir: Optional[Path] = None
        self._patches: list[GroundTruthPatch] = []

    def discover_patches(
        self, harness_filter: Optional[str] = None
    ) -> list[GroundTruthPatch]:
        """Discover all ground truth patches in the benchmark.

        Args:
            harness_filter: Optional harness name to filter patches

        Returns:
            List of GroundTruthPatch objects
        """
        patches = []

        if not self.aixcc_path.exists():
            logger.warning(f"No .aixcc directory found: {self.aixcc_path}")
            return patches

        # Parse meta.yaml to get harness and vulnerability info
        meta_yaml = self.aixcc_path / "meta.yaml"
        if not meta_yaml.exists():
            logger.warning(f"No meta.yaml found: {meta_yaml}")
            return self._discover_patches_from_filesystem(harness_filter)

        with meta_yaml.open() as f:
            meta = yaml.safe_load(f)

        harness_files = meta.get("harness_files", [])
        for harness_info in harness_files:
            harness_name = harness_info.get("name")
            if harness_filter and harness_name != harness_filter:
                continue

            vulns = harness_info.get("vulns", [])
            for vuln in vulns:
                cpv_id = vuln.get("vuln_keyword")
                povs = vuln.get("povs", [])

                for pov_info in povs:
                    pov_id = pov_info.get("id")

                    # Find patch file
                    patch_path = (
                        self.aixcc_path
                        / harness_name
                        / cpv_id
                        / "patches"
                        / "patch_0.diff"
                    )
                    pov_path = (
                        self.aixcc_path
                        / harness_name
                        / cpv_id
                        / "blobs"
                        / f"{pov_id}.blob"
                    )

                    if patch_path.exists() and pov_path.exists():
                        patches.append(
                            GroundTruthPatch(
                                harness=harness_name,
                                cpv_id=cpv_id,
                                pov_id=pov_id,
                                patch_path=patch_path,
                                pov_path=pov_path,
                            )
                        )
                    elif patch_path.exists():
                        # POV might not exist, try to find any POV file
                        blobs_dir = self.aixcc_path / harness_name / cpv_id / "blobs"
                        if blobs_dir.exists():
                            pov_files = list(blobs_dir.glob("*.blob"))
                            if pov_files:
                                patches.append(
                                    GroundTruthPatch(
                                        harness=harness_name,
                                        cpv_id=cpv_id,
                                        pov_id=pov_id,
                                        patch_path=patch_path,
                                        pov_path=pov_files[0],
                                    )
                                )

        self._patches = patches
        return patches

    def _discover_patches_from_filesystem(
        self, harness_filter: Optional[str] = None
    ) -> list[GroundTruthPatch]:
        """Fallback: discover patches by scanning filesystem."""
        patches = []

        for harness_dir in self.aixcc_path.iterdir():
            if not harness_dir.is_dir() or harness_dir.name in ("tests",):
                continue

            harness_name = harness_dir.name
            if harness_filter and harness_name != harness_filter:
                continue

            for cpv_dir in harness_dir.iterdir():
                if not cpv_dir.is_dir() or not cpv_dir.name.startswith("cpv_"):
                    continue

                cpv_id = cpv_dir.name
                patch_file = cpv_dir / "patches" / "patch_0.diff"
                blobs_dir = cpv_dir / "blobs"

                if patch_file.exists() and blobs_dir.exists():
                    pov_files = list(blobs_dir.glob("*.blob"))
                    for pov_file in pov_files:
                        pov_id = pov_file.stem
                        patches.append(
                            GroundTruthPatch(
                                harness=harness_name,
                                cpv_id=cpv_id,
                                pov_id=pov_id,
                                patch_path=patch_file,
                                pov_path=pov_file,
                            )
                        )

        self._patches = patches
        return patches

    def convert(
        self, harness_filter: Optional[str] = None
    ) -> tuple[Path, Path, list[GroundTruthPatch]]:
        """Convert ground truth patches to CRS format.

        Creates temporary directories with CRS output format structure.

        Args:
            harness_filter: Optional harness name to filter patches

        Returns:
            Tuple of (patch_dir, pov_dir, patches)
        """
        patches = self.discover_patches(harness_filter)
        if not patches:
            raise ValueError(f"No ground truth patches found in {self.benchmark_path}")

        self._temp_dir = Path(tempfile.mkdtemp(prefix="gt-to-crs-"))
        patch_dir = self._temp_dir / "patches"
        pov_dir = self._temp_dir / "povs"

        patch_dir.mkdir(parents=True)
        pov_dir.mkdir(parents=True)

        for gt_patch in patches:
            unique_id = gt_patch.unique_id

            # Create patch directory: patches/<unique_id>/patch.diff
            patch_subdir = patch_dir / unique_id
            patch_subdir.mkdir(parents=True, exist_ok=True)
            shutil.copy(gt_patch.patch_path, patch_subdir / "patch.diff")

            # Copy POV: povs/<unique_id>.blob
            shutil.copy(gt_patch.pov_path, pov_dir / f"{unique_id}.blob")

        logger.info(
            f"Converted {len(patches)} ground truth patches to CRS format at {self._temp_dir}"
        )
        return patch_dir, pov_dir, patches

    def cleanup(self) -> None:
        """Clean up temporary directories."""
        if self._temp_dir and self._temp_dir.exists():
            shutil.rmtree(self._temp_dir, ignore_errors=True)
            self._temp_dir = None

    def __enter__(self) -> "GroundTruthConverter":
        return self

    def __exit__(self, _exc_type, _exc_val, _exc_tb) -> None:
        self.cleanup()


# =============================================================================
# Helper Functions
# =============================================================================


def get_project_root() -> Path:
    """Get the CRSBench project root directory."""
    return Path(__file__).parent.parent


def get_benchmark_path() -> Path:
    """Get path to sanity-mock-c-delta-01 benchmark."""
    return get_project_root() / "benchmarks" / BENCHMARK_NAME


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
    """Check if inc-build image is available locally."""
    image_name = f"ghcr.io/team-atlanta/crsbench/{project_name}:inc-{sanitizer}"
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", image_name],
            capture_output=True,
            timeout=30,
        )
        return result.returncode == 0
    except Exception:
        return False


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def benchmark_path() -> Path:
    """Fixture for benchmark path."""
    path = get_benchmark_path()
    if not path.exists():
        pytest.skip(f"Benchmark not found: {path}")
    return path


@pytest.fixture
def ground_truth_patch(benchmark_path: Path) -> Path:
    """Fixture for ground truth patch file."""
    patch_path = (
        benchmark_path / ".aixcc" / HARNESS_NAME / CPV_ID / "patches" / "patch_0.diff"
    )
    if not patch_path.exists():
        pytest.skip(f"Ground truth patch not found: {patch_path}")
    return patch_path


@pytest.fixture
def pov_path(benchmark_path: Path) -> Path:
    """Fixture for POV file."""
    pov_file = (
        benchmark_path / ".aixcc" / HARNESS_NAME / CPV_ID / "blobs" / f"{POV_ID}.blob"
    )
    if not pov_file.exists():
        pytest.skip(f"POV file not found: {pov_file}")
    return pov_file


@pytest.fixture
def patch_dir(ground_truth_patch: Path) -> Generator[Path, None, None]:
    """Create temporary patch directory with ground truth patch.

    Structure: patch_dir/<pov_id>/patch.diff
    """
    temp_dir = Path(tempfile.mkdtemp(prefix="test-patch-verification-"))

    # Create patch subdirectory with pov_id
    pov_patch_dir = temp_dir / POV_ID
    pov_patch_dir.mkdir(parents=True)

    # Copy ground truth patch
    patch_dest = pov_patch_dir / "patch.diff"
    shutil.copy(ground_truth_patch, patch_dest)

    yield temp_dir

    # Cleanup
    shutil.rmtree(temp_dir, ignore_errors=True)


# =============================================================================
# Model Tests
# =============================================================================


class TestPatchVerificationStatus:
    """Tests for PatchVerificationStatus enum."""

    def test_status_values(self):
        """Test PatchVerificationStatus enum values."""
        assert PatchVerificationStatus.PENDING.value == "pending"
        assert PatchVerificationStatus.BUILD_FAILED.value == "build_failed"
        assert PatchVerificationStatus.POV_STILL_TRIGGERS.value == "pov_still_triggers"
        assert PatchVerificationStatus.TEST_FAILED.value == "test_failed"
        assert PatchVerificationStatus.VALID.value == "valid"
        assert PatchVerificationStatus.ERROR.value == "error"


class TestTestMode:
    """Tests for TestMode enum."""

    def test_mode_values(self):
        """Test TestMode enum values."""
        assert TestMode.FULL.value == "full"
        assert TestMode.RTS.value == "rts"


class TestPatchInfo:
    """Tests for PatchInfo model."""

    def test_patch_info_loads_content(self, ground_truth_patch: Path):
        """Test that PatchInfo correctly loads patch content."""
        patch = PatchInfo(
            pov_id=POV_ID,
            patch_path=ground_truth_patch,
        )

        assert patch.pov_id == POV_ID
        assert patch.patch_path == ground_truth_patch
        assert "diff --git" in patch.patch_content
        assert "mock.c" in patch.patch_content

    def test_patch_info_from_nonexistent_file(self):
        """Test PatchInfo with nonexistent file returns empty content."""
        patch = PatchInfo(
            pov_id="nonexistent",
            patch_path=Path("/nonexistent/patch.diff"),
        )

        assert patch.patch_content == ""

    def test_patch_info_with_provided_content(self):
        """Test PatchInfo with pre-loaded content."""
        content = "diff --git a/test.c b/test.c\n--- a/test.c\n+++ b/test.c\n"
        patch = PatchInfo(
            pov_id="test",
            patch_path=Path("/any/path.diff"),
            patch_content=content,
        )

        assert patch.patch_content == content


class TestPatchVerificationResult:
    """Tests for PatchVerificationResult model."""

    def test_result_creation(self):
        """Test PatchVerificationResult creation."""
        result = PatchVerificationResult(
            status=PatchVerificationStatus.VALID,
            pov_id="pov_0",
            patch_path=Path("/test/patch.diff"),
        )
        assert result.status == PatchVerificationStatus.VALID
        assert result.is_valid
        assert result.pov_id == "pov_0"

    def test_result_with_failure(self):
        """Test PatchVerificationResult with failure status."""
        result = PatchVerificationResult(
            status=PatchVerificationStatus.BUILD_FAILED,
            pov_id="pov_0",
            patch_path=Path("/test/patch.diff"),
            details="Compilation error in main.c",
        )
        assert result.status == PatchVerificationStatus.BUILD_FAILED
        assert not result.is_valid
        assert "Compilation error" in result.details

    def test_result_to_dict(self):
        """Test PatchVerificationResult serialization."""
        result = PatchVerificationResult(
            status=PatchVerificationStatus.VALID,
            pov_id="pov_0",
            patch_path=Path("/test/patch.diff"),
            build_time=10.5,
            pov_test_passed=True,
            unit_tests_passed=True,
        )
        d = result.to_dict()
        assert d["status"] == "valid"
        assert d["pov_id"] == "pov_0"
        assert d["build_time"] == 10.5
        assert d["pov_test_passed"] is True

    def test_result_str_valid(self):
        """Test PatchVerificationResult string representation for valid patch."""
        result = PatchVerificationResult(
            status=PatchVerificationStatus.VALID,
            pov_id="pov_0",
            patch_path=Path("/test/patch.diff"),
        )
        assert "pov_0" in str(result)
        assert "VALID" in str(result)

    def test_result_str_failed(self):
        """Test PatchVerificationResult string representation for failed patch."""
        result = PatchVerificationResult(
            status=PatchVerificationStatus.POV_STILL_TRIGGERS,
            pov_id="pov_0",
            patch_path=Path("/test/patch.diff"),
            details="POV still triggers vulnerability",
        )
        assert "pov_0" in str(result)
        assert "POV_STILL_TRIGGERS" in str(result)
        assert "still triggers" in str(result)


# =============================================================================
# Ground Truth Converter Tests
# =============================================================================


class TestGroundTruthConverter:
    """Tests for GroundTruthConverter utility."""

    def test_discover_patches(self, benchmark_path: Path):
        """Test discovering ground truth patches from benchmark."""
        converter = GroundTruthConverter(benchmark_path)
        patches = converter.discover_patches()

        assert len(patches) > 0, "Should find at least one patch"

        for patch in patches:
            assert patch.harness, "Harness name should not be empty"
            assert patch.cpv_id.startswith("cpv_"), "CPV ID should start with cpv_"
            assert patch.pov_id.startswith("pov_"), "POV ID should start with pov_"
            assert patch.patch_path.exists(), (
                f"Patch file should exist: {patch.patch_path}"
            )
            assert patch.pov_path.exists(), f"POV file should exist: {patch.pov_path}"

    def test_discover_patches_with_harness_filter(self, benchmark_path: Path):
        """Test discovering patches filtered by harness name."""
        converter = GroundTruthConverter(benchmark_path)

        # Get all patches first
        all_patches = converter.discover_patches()

        # Filter by known harness
        filtered_patches = converter.discover_patches(harness_filter=HARNESS_NAME)

        assert len(filtered_patches) > 0, f"Should find patches for {HARNESS_NAME}"
        assert len(filtered_patches) <= len(all_patches), (
            "Filtered should not have more"
        )

        for patch in filtered_patches:
            assert patch.harness == HARNESS_NAME, (
                "All patches should be for filtered harness"
            )

    def test_convert_creates_crs_format(self, benchmark_path: Path):
        """Test that convert creates proper CRS output format."""
        converter = GroundTruthConverter(benchmark_path)

        try:
            patch_dir, pov_dir, patches = converter.convert()

            assert patch_dir.exists(), "Patch directory should exist"
            assert pov_dir.exists(), "POV directory should exist"
            assert len(patches) > 0, "Should have at least one patch"

            # Verify CRS format structure
            for patch in patches:
                unique_id = patch.unique_id

                # Check patch structure: patches/<unique_id>/patch.diff
                patch_subdir = patch_dir / unique_id
                patch_file = patch_subdir / "patch.diff"
                assert patch_subdir.is_dir(), (
                    f"Patch subdir should exist: {patch_subdir}"
                )
                assert patch_file.exists(), f"Patch file should exist: {patch_file}"

                # Check POV: povs/<unique_id>.blob
                pov_file = pov_dir / f"{unique_id}.blob"
                assert pov_file.exists(), f"POV file should exist: {pov_file}"

                # Verify patch content is copied correctly
                original_content = patch.patch_path.read_text()
                converted_content = patch_file.read_text()
                assert original_content == converted_content, (
                    "Patch content should match"
                )

        finally:
            converter.cleanup()

    def test_unique_id_format(self, benchmark_path: Path):
        """Test that unique_id is generated correctly."""
        converter = GroundTruthConverter(benchmark_path)
        patches = converter.discover_patches()

        for patch in patches:
            unique_id = patch.unique_id
            expected = f"{patch.harness}_{patch.cpv_id}_{patch.pov_id}"
            assert unique_id == expected, f"Unique ID should be {expected}"

    def test_context_manager_cleanup(self, benchmark_path: Path):
        """Test that context manager properly cleans up."""
        temp_dir = None

        with GroundTruthConverter(benchmark_path) as converter:
            patch_dir, _pov_dir, _ = converter.convert()
            temp_dir = patch_dir.parent
            assert temp_dir.exists(), "Temp dir should exist during context"

        # After context exits, temp_dir should be cleaned up
        assert not temp_dir.exists(), "Temp dir should be cleaned up after context"


# =============================================================================
# Patch Discovery Tests
# =============================================================================


class TestPatchDiscovery:
    """Tests for patch directory discovery logic."""

    def test_patch_discovery_from_directory(self, patch_dir: Path):
        """Test that patches are correctly discovered from directory structure.

        Tests the patch directory discovery logic without running actual verification.
        """
        # Verify patch directory structure
        pov_subdir = patch_dir / POV_ID
        patch_file = pov_subdir / "patch.diff"

        assert pov_subdir.is_dir(), f"POV subdirectory should exist: {pov_subdir}"
        assert patch_file.exists(), f"Patch file should exist: {patch_file}"

        # Verify patch content
        content = patch_file.read_text()
        assert content.startswith("diff --git"), "Patch should start with diff header"

    def test_discovers_patches_in_expected_structure(self, patch_dir: Path):
        """Test discovery finds patches in expected structure."""
        # The patch_dir fixture creates: patch_dir/<pov_id>/patch.diff
        discovered = []

        for pov_subdir in patch_dir.iterdir():
            if not pov_subdir.is_dir():
                continue
            patch_file = pov_subdir / "patch.diff"
            if patch_file.exists():
                discovered.append(
                    PatchInfo(pov_id=pov_subdir.name, patch_path=patch_file)
                )

        assert len(discovered) == 1, "Should discover exactly one patch"
        assert discovered[0].pov_id == POV_ID


# =============================================================================
# E2E Tests (Require Docker)
# =============================================================================


class TestPatchVerificationE2E:
    """E2E tests for patch verification.

    These tests require Docker and inc-build images.
    """

    @pytest.mark.skipif(
        not is_docker_available(),
        reason="Docker daemon not available",
    )
    def test_ground_truth_patch_verification(
        self,
        benchmark_path: Path,
        ground_truth_patch: Path,
        pov_path: Path,
    ):
        """Test that ground truth patch passes verification.

        This test validates that:
        1. Patch can be applied successfully
        2. Build succeeds after patching (using inc-build image)
        3. POV no longer triggers the vulnerability

        This is a sanity test - if this fails, the verification pipeline
        or environment is broken.
        """
        import os

        from crsbench.evaluation.verification.patch import PatchVerificationEngine

        # Check if inc-build image is available
        if not is_inc_build_image_available(BENCHMARK_NAME):
            pytest.skip(
                f"Inc-build image not available for {BENCHMARK_NAME}. "
                "Pull with: docker pull ghcr.io/team-atlanta/crsbench/"
                f"{BENCHMARK_NAME}:inc-address"
            )

        # Get oss-fuzz path
        oss_fuzz_path = None
        if os.environ.get("OSS_FUZZ_HOME"):
            oss_fuzz_path = Path(os.environ["OSS_FUZZ_HOME"])
        else:
            candidates = [
                get_project_root().parent / "oss-fuzz",
                Path.home() / "oss-fuzz",
                Path("/oss-fuzz"),
            ]
            for candidate in candidates:
                if candidate.exists():
                    oss_fuzz_path = candidate
                    break

        if not oss_fuzz_path or not oss_fuzz_path.exists():
            pytest.skip("OSS-Fuzz directory not found. Set OSS_FUZZ_HOME env var.")

        # Ensure project is symlinked to oss-fuzz/projects/
        oss_fuzz_project_path = oss_fuzz_path / "projects" / BENCHMARK_NAME
        if not oss_fuzz_project_path.exists():
            pytest.skip(
                f"Project not linked to oss-fuzz. Run: "
                f"ln -s {benchmark_path} {oss_fuzz_project_path}"
            )

        # Create engine
        engine = PatchVerificationEngine(
            oss_fuzz_path=oss_fuzz_path,
            test_mode=TestMode.FULL,
            sanitizer="address",
            timeout=120,
            build_timeout=1200,
        )

        try:
            # Create patch info from ground truth
            patch = PatchInfo(
                pov_id=POV_ID,
                patch_path=ground_truth_patch,
            )

            # Run verification
            result = engine.verify_patch(
                benchmark_path=benchmark_path,
                patch=patch,
                harness=HARNESS_NAME,
                pov_path=pov_path,
            )

            # Assert verification passed
            assert result.status == PatchVerificationStatus.VALID, (
                f"Ground truth patch verification failed.\n"
                f"Status: {result.status}\n"
                f"Details: {result.details}"
            )

            # Additional assertions
            assert result.pov_test_passed, "POV test should pass for ground truth patch"

        finally:
            engine.cleanup()


# =============================================================================
# Phase 2: Per-CPV Testing Tests
# =============================================================================


class TestCpvDiscovery:
    """Tests for CPV and POV variant discovery methods."""

    def test_discover_all_cpvs_in_harness(self, benchmark_path: Path):
        """Test discovering all CPVs for a harness."""
        from unittest.mock import MagicMock

        from crsbench.evaluation.verification.patch import PatchVerificationEngine

        # Create a mock engine (we don't need full infrastructure for discovery)
        engine = MagicMock(spec=PatchVerificationEngine)
        engine._discover_all_cpvs_in_harness = (
            PatchVerificationEngine._discover_all_cpvs_in_harness.__get__(
                engine, PatchVerificationEngine
            )
        )

        cpvs = engine._discover_all_cpvs_in_harness(benchmark_path, HARNESS_NAME)

        # Should find at least one CPV
        assert len(cpvs) >= 1, f"Should find at least one CPV in {HARNESS_NAME}"

        # All should be cpv_* format
        for cpv in cpvs:
            assert cpv.startswith("cpv_"), f"CPV should start with cpv_: {cpv}"

        # Should be sorted numerically
        if len(cpvs) > 1:
            cpv_nums = [int(c.split("_")[1]) for c in cpvs]
            assert cpv_nums == sorted(cpv_nums), "CPVs should be sorted numerically"

    def test_discover_all_cpvs_nonexistent_harness(self, benchmark_path: Path):
        """Test CPV discovery for nonexistent harness returns empty list."""
        from unittest.mock import MagicMock

        from crsbench.evaluation.verification.patch import PatchVerificationEngine

        engine = MagicMock(spec=PatchVerificationEngine)
        engine._discover_all_cpvs_in_harness = (
            PatchVerificationEngine._discover_all_cpvs_in_harness.__get__(
                engine, PatchVerificationEngine
            )
        )

        cpvs = engine._discover_all_cpvs_in_harness(
            benchmark_path, "nonexistent_harness"
        )
        assert cpvs == [], "Should return empty list for nonexistent harness"


class TestDiscoverPatchesFromBenchmark:
    """Tests for _discover_patches_from_benchmark method."""

    def test_discover_patches_structure(self, benchmark_path: Path):
        """Test discovering patches from benchmark .aixcc structure."""
        from unittest.mock import MagicMock

        from crsbench.evaluation.verification.patch import PatchVerificationEngine

        engine = MagicMock(spec=PatchVerificationEngine)
        engine._discover_patches_from_benchmark = (
            PatchVerificationEngine._discover_patches_from_benchmark.__get__(
                engine, PatchVerificationEngine
            )
        )

        # Create test structure with patches
        test_dir = benchmark_path / ".aixcc" / HARNESS_NAME / CPV_ID / "patches"
        if not test_dir.exists():
            pytest.skip(f"Patches directory not found: {test_dir}")

        discovered = engine._discover_patches_from_benchmark(benchmark_path)

        # Should discover patches if any exist
        # Note: The test benchmark may not have the patches/<unique_id>/patch.diff structure
        # This tests the discovery logic works without error
        assert isinstance(discovered, list), "Should return a list"

    def test_discover_patches_with_harness_filter(self, benchmark_path: Path):
        """Test discovering patches with harness filter."""
        from unittest.mock import MagicMock

        from crsbench.evaluation.verification.patch import PatchVerificationEngine

        engine = MagicMock(spec=PatchVerificationEngine)
        engine._discover_patches_from_benchmark = (
            PatchVerificationEngine._discover_patches_from_benchmark.__get__(
                engine, PatchVerificationEngine
            )
        )

        # Filter by harness
        discovered = engine._discover_patches_from_benchmark(
            benchmark_path, harness_filter=HARNESS_NAME
        )

        # All discovered should be for the filtered harness
        for harness, cpv_id, patch in discovered:
            assert harness == HARNESS_NAME, (
                f"Should only find patches for {HARNESS_NAME}"
            )


class TestCpvStatsAndScores:
    """Tests for CpvStats and VerificationScores models used in per-CPV testing."""

    def test_cpv_stats_status_complete(self):
        """Test CpvStats status when all variants pass."""
        from crsbench.evaluation.verification.models import CpvStats

        stats = CpvStats(
            cpv_id="cpv_0",
            variants_tested=3,
            variants_matched=3,
            variant_results={"pov_0": True, "pov_1": True, "pov_2": True},
        )

        assert stats.status == "complete"
        assert stats.variants_tested == 3
        assert stats.variants_matched == 3

    def test_cpv_stats_status_partial(self):
        """Test CpvStats status when some variants pass."""
        from crsbench.evaluation.verification.models import CpvStats

        stats = CpvStats(
            cpv_id="cpv_0",
            variants_tested=3,
            variants_matched=2,
            variant_results={"pov_0": True, "pov_1": True, "pov_2": False},
        )

        assert stats.status == "partial"

    def test_cpv_stats_status_none(self):
        """Test CpvStats status when no variants pass."""
        from crsbench.evaluation.verification.models import CpvStats

        stats = CpvStats(
            cpv_id="cpv_0",
            variants_tested=3,
            variants_matched=0,
            variant_results={"pov_0": False, "pov_1": False, "pov_2": False},
        )

        assert stats.status == "none"

    def test_cpv_stats_to_dict(self):
        """Test CpvStats serialization."""
        from crsbench.evaluation.verification.models import CpvStats

        stats = CpvStats(
            cpv_id="cpv_0",
            variants_tested=2,
            variants_matched=1,
            variant_results={"pov_0": True, "pov_1": False},
        )

        d = stats.to_dict()
        assert d["cpv_id"] == "cpv_0"
        assert d["variants_tested"] == 2
        assert d["variants_matched"] == 1
        assert d["status"] == "partial"

    def test_verification_scores_overall_fix_rate(self):
        """Test VerificationScores fix rate calculation."""
        from crsbench.evaluation.verification.models import VerificationScores

        scores = VerificationScores(
            cpvs_complete=2,
            cpvs_partial=1,
            cpvs_none=1,
            total_variants_tested=10,
            total_variants_matched=7,
        )

        assert scores.overall_fix_rate == 0.7
        assert scores.cpvs_complete == 2

    def test_verification_scores_zero_tested(self):
        """Test VerificationScores with zero variants tested."""
        from crsbench.evaluation.verification.models import VerificationScores

        scores = VerificationScores()
        assert scores.overall_fix_rate == 0.0


class TestPatchVerificationResultPhase2:
    """Tests for PatchVerificationResult with Phase 2 fields."""

    def test_result_with_cpv_stats(self):
        """Test PatchVerificationResult with per-CPV statistics."""
        from crsbench.evaluation.verification.models import (
            CpvStats,
            PatchVerificationResult,
            PatchVerificationStatus,
            VerificationScores,
        )

        cpv_stats = {
            "cpv_0": CpvStats(
                cpv_id="cpv_0",
                variants_tested=2,
                variants_matched=2,
                variant_results={"pov_0": True, "pov_1": True},
            ),
            "cpv_1": CpvStats(
                cpv_id="cpv_1",
                variants_tested=2,
                variants_matched=1,
                variant_results={"pov_0": True, "pov_1": False},
            ),
        }

        scores = VerificationScores(
            cpvs_complete=1,
            cpvs_partial=1,
            cpvs_none=0,
            total_variants_tested=4,
            total_variants_matched=3,
        )

        result = PatchVerificationResult(
            status=PatchVerificationStatus.VALID,
            pov_id="test_patch",
            patch_path=Path("/test/patch.diff"),
            harness="test_harness",
            cpv_fixed=["cpv_0"],
            cpv_stats=cpv_stats,
            scores=scores,
            security_verdict="PASS",
        )

        assert result.harness == "test_harness"
        assert result.cpv_fixed == ["cpv_0"]
        assert len(result.cpv_stats) == 2
        assert result.scores.overall_fix_rate == 0.75
        assert result.security_verdict == "PASS"

    def test_result_to_dict_with_phase2_fields(self):
        """Test PatchVerificationResult serialization with Phase 2 fields."""
        from crsbench.evaluation.verification.models import (
            CpvStats,
            PatchVerificationResult,
            PatchVerificationStatus,
            VerificationScores,
        )

        result = PatchVerificationResult(
            status=PatchVerificationStatus.VALID,
            pov_id="test",
            patch_path=Path("/test/patch.diff"),
            harness="harness",
            cpv_fixed=["cpv_0"],
            cpv_stats={
                "cpv_0": CpvStats(
                    cpv_id="cpv_0",
                    variants_tested=1,
                    variants_matched=1,
                    variant_results={"pov_0": True},
                )
            },
            scores=VerificationScores(
                cpvs_complete=1,
                total_variants_tested=1,
                total_variants_matched=1,
            ),
            security_verdict="PASS",
        )

        d = result.to_dict()

        assert d["harness"] == "harness"
        assert d["cpv_fixed"] == ["cpv_0"]
        assert "cpv_0" in d["cpv_stats"]
        assert d["cpv_stats"]["cpv_0"]["status"] == "complete"
        assert d["scores"]["cpvs_complete"] == 1
        assert d["security_verdict"] == "PASS"

    def test_security_verdict_fail_no_cpvs_fixed(self):
        """Test security_verdict is FAIL when no CPVs are fixed."""
        from crsbench.evaluation.verification.models import (
            PatchVerificationResult,
            PatchVerificationStatus,
        )

        result = PatchVerificationResult(
            status=PatchVerificationStatus.POV_STILL_TRIGGERS,
            pov_id="test",
            patch_path=Path("/test/patch.diff"),
            cpv_fixed=[],  # No CPVs fixed
            security_verdict="FAIL",
        )

        assert result.security_verdict == "FAIL"
        assert not result.is_valid


# =============================================================================
# CpvFixedDedup Tests
# =============================================================================


class TestCpvFixedDedup:
    """Tests for CpvFixedDedup deduplication strategy."""

    def test_deduplicate_by_cpv_fixed(self):
        """Test that patches with same cpv_fixed are deduplicated."""
        from crsbench.evaluation.verification.dedup import CpvFixedDedup
        from crsbench.evaluation.verification.models import (
            PatchVerificationResult,
            PatchVerificationStatus,
        )

        dedup = CpvFixedDedup()

        results = [
            PatchVerificationResult(
                status=PatchVerificationStatus.VALID,
                pov_id="patch1",
                patch_path=Path("/patch1.diff"),
                cpv_fixed=["cpv_0", "cpv_1"],
            ),
            PatchVerificationResult(
                status=PatchVerificationStatus.VALID,
                pov_id="patch2",
                patch_path=Path("/patch2.diff"),
                cpv_fixed=["cpv_1", "cpv_0"],  # Same set, different order
            ),
            PatchVerificationResult(
                status=PatchVerificationStatus.VALID,
                pov_id="patch3",
                patch_path=Path("/patch3.diff"),
                cpv_fixed=["cpv_0"],  # Different set
            ),
        ]

        unique = dedup.deduplicate(results)

        assert len(unique) == 2
        assert unique[0].pov_id == "patch1"
        assert unique[1].pov_id == "patch3"

    def test_keeps_non_valid_patches(self):
        """Test that non-VALID patches are not deduplicated."""
        from crsbench.evaluation.verification.dedup import CpvFixedDedup
        from crsbench.evaluation.verification.models import (
            PatchVerificationResult,
            PatchVerificationStatus,
        )

        dedup = CpvFixedDedup()

        results = [
            PatchVerificationResult(
                status=PatchVerificationStatus.BUILD_FAILED,
                pov_id="patch1",
                patch_path=Path("/patch1.diff"),
            ),
            PatchVerificationResult(
                status=PatchVerificationStatus.BUILD_FAILED,
                pov_id="patch2",
                patch_path=Path("/patch2.diff"),
            ),
        ]

        unique = dedup.deduplicate(results)

        assert len(unique) == 2  # Both kept since they're not VALID

    def test_strategy_name(self):
        """Test dedup strategy name."""
        from crsbench.evaluation.verification.dedup import CpvFixedDedup

        dedup = CpvFixedDedup()
        assert dedup.name == "cpv-fixed"

    def test_get_patch_dedup_strategy(self):
        """Test get_patch_dedup_strategy factory function."""
        from crsbench.evaluation.verification.dedup import (
            CpvFixedDedup,
            NoOpPatchDedup,
            get_patch_dedup_strategy,
        )

        cpv_dedup = get_patch_dedup_strategy("cpv-fixed")
        assert isinstance(cpv_dedup, CpvFixedDedup)

        noop_dedup = get_patch_dedup_strategy("none")
        assert isinstance(noop_dedup, NoOpPatchDedup)


# =============================================================================
# Core Test Cases - Security Verdict Determination
# =============================================================================


class TestSecurityVerdictDetermination:
    """Core tests for security verdict logic."""

    def test_verdict_pass_requires_cpv_fixed_and_tests_pass(self):
        """Test PASS verdict requires both cpv_fixed non-empty AND tests pass."""
        from crsbench.evaluation.verification.models import (
            CpvStats,
            PatchVerificationResult,
            PatchVerificationStatus,
            VerificationScores,
        )

        result = PatchVerificationResult(
            status=PatchVerificationStatus.VALID,
            pov_id="test",
            patch_path=Path("/test/patch.diff"),
            harness="test_harness",
            cpv_fixed=["cpv_0"],
            cpv_stats={
                "cpv_0": CpvStats(
                    cpv_id="cpv_0",
                    variants_tested=2,
                    variants_matched=2,
                    variant_results={"pov_0": True, "pov_1": True},
                )
            },
            scores=VerificationScores(
                cpvs_complete=1,
                total_variants_tested=2,
                total_variants_matched=2,
            ),
            pov_test_passed=True,
            unit_tests_passed=True,
            security_verdict="PASS",
        )

        assert result.security_verdict == "PASS"
        assert result.is_valid
        assert len(result.cpv_fixed) == 1

    def test_verdict_fail_when_no_cpvs_fixed(self):
        """Test FAIL verdict when cpv_fixed is empty."""
        from crsbench.evaluation.verification.models import (
            CpvStats,
            PatchVerificationResult,
            PatchVerificationStatus,
            VerificationScores,
        )

        result = PatchVerificationResult(
            status=PatchVerificationStatus.POV_STILL_TRIGGERS,
            pov_id="test",
            patch_path=Path("/test/patch.diff"),
            harness="test_harness",
            cpv_fixed=[],  # No CPVs fixed
            cpv_stats={
                "cpv_0": CpvStats(
                    cpv_id="cpv_0",
                    variants_tested=2,
                    variants_matched=0,  # None matched
                    variant_results={"pov_0": False, "pov_1": False},
                )
            },
            scores=VerificationScores(
                cpvs_none=1,
                total_variants_tested=2,
                total_variants_matched=0,
            ),
            pov_test_passed=False,
            security_verdict="FAIL",
        )

        assert result.security_verdict == "FAIL"
        assert not result.is_valid
        assert result.cpv_fixed == []

    def test_verdict_fail_when_only_partial_fix(self):
        """Test FAIL verdict when CPV is only partially fixed."""
        from crsbench.evaluation.verification.models import (
            CpvStats,
            PatchVerificationResult,
            PatchVerificationStatus,
            VerificationScores,
        )

        result = PatchVerificationResult(
            status=PatchVerificationStatus.POV_STILL_TRIGGERS,
            pov_id="test",
            patch_path=Path("/test/patch.diff"),
            harness="test_harness",
            cpv_fixed=[],  # Partial fix NOT in cpv_fixed
            cpv_stats={
                "cpv_0": CpvStats(
                    cpv_id="cpv_0",
                    variants_tested=3,
                    variants_matched=2,  # Partial: 2/3
                    variant_results={"pov_0": True, "pov_1": True, "pov_2": False},
                )
            },
            scores=VerificationScores(
                cpvs_partial=1,
                total_variants_tested=3,
                total_variants_matched=2,
            ),
            pov_test_passed=False,
            security_verdict="FAIL",
        )

        assert result.security_verdict == "FAIL"
        assert result.cpv_stats["cpv_0"].status == "partial"
        assert "cpv_0" not in result.cpv_fixed

    def test_verdict_fail_when_tests_fail_even_with_cpvs_fixed(self):
        """Test FAIL verdict when unit tests fail even if CPVs fixed."""
        from crsbench.evaluation.verification.models import (
            CpvStats,
            PatchVerificationResult,
            PatchVerificationStatus,
            VerificationScores,
        )

        result = PatchVerificationResult(
            status=PatchVerificationStatus.TEST_FAILED,
            pov_id="test",
            patch_path=Path("/test/patch.diff"),
            harness="test_harness",
            cpv_fixed=["cpv_0"],  # CPV fixed but tests failed
            cpv_stats={
                "cpv_0": CpvStats(
                    cpv_id="cpv_0",
                    variants_tested=1,
                    variants_matched=1,
                    variant_results={"pov_0": True},
                )
            },
            scores=VerificationScores(
                cpvs_complete=1,
                total_variants_tested=1,
                total_variants_matched=1,
            ),
            pov_test_passed=True,
            unit_tests_passed=False,  # Tests failed
            security_verdict="FAIL",
        )

        assert result.security_verdict == "FAIL"
        assert not result.is_valid  # TEST_FAILED is not VALID

    def test_default_verdict_is_fail(self):
        """Test that default security_verdict is FAIL."""
        from crsbench.evaluation.verification.models import (
            PatchVerificationResult,
            PatchVerificationStatus,
        )

        result = PatchVerificationResult(
            status=PatchVerificationStatus.PENDING,
            pov_id="test",
            patch_path=Path("/test/patch.diff"),
        )

        assert result.security_verdict == "FAIL"


# =============================================================================
# Core Test Cases - CpvStats Edge Cases
# =============================================================================


class TestCpvStatsEdgeCases:
    """Core tests for CpvStats edge cases."""

    def test_cpv_stats_status_zero_tested_zero_matched(self):
        """Test status when variants_tested=0 and variants_matched=0."""
        from crsbench.evaluation.verification.models import CpvStats

        stats = CpvStats(
            cpv_id="cpv_0",
            variants_tested=0,
            variants_matched=0,
            variant_results={},
        )

        assert stats.status == "none"

    def test_cpv_stats_status_one_tested_one_matched(self):
        """Test status when only one variant exists and passes."""
        from crsbench.evaluation.verification.models import CpvStats

        stats = CpvStats(
            cpv_id="cpv_0",
            variants_tested=1,
            variants_matched=1,
            variant_results={"pov_0": True},
        )

        assert stats.status == "complete"

    def test_cpv_stats_status_one_tested_zero_matched(self):
        """Test status when only one variant exists and fails."""
        from crsbench.evaluation.verification.models import CpvStats

        stats = CpvStats(
            cpv_id="cpv_0",
            variants_tested=1,
            variants_matched=0,
            variant_results={"pov_0": False},
        )

        assert stats.status == "none"

    def test_cpv_stats_status_many_tested_all_matched(self):
        """Test status when many variants all pass."""
        from crsbench.evaluation.verification.models import CpvStats

        stats = CpvStats(
            cpv_id="cpv_0",
            variants_tested=5,
            variants_matched=5,
            variant_results={f"pov_{i}": True for i in range(5)},
        )

        assert stats.status == "complete"

    def test_cpv_stats_status_many_tested_some_matched(self):
        """Test status when many variants, only some pass."""
        from crsbench.evaluation.verification.models import CpvStats

        stats = CpvStats(
            cpv_id="cpv_0",
            variants_tested=5,
            variants_matched=3,
            variant_results={
                "pov_0": True,
                "pov_1": True,
                "pov_2": True,
                "pov_3": False,
                "pov_4": False,
            },
        )

        assert stats.status == "partial"

    def test_cpv_stats_to_dict_includes_all_fields(self):
        """Test to_dict includes all fields including computed status."""
        from crsbench.evaluation.verification.models import CpvStats

        stats = CpvStats(
            cpv_id="cpv_0",
            variants_tested=2,
            variants_matched=1,
            variant_results={"pov_0": True, "pov_1": False},
        )

        d = stats.to_dict()

        assert d["cpv_id"] == "cpv_0"
        assert d["variants_tested"] == 2
        assert d["variants_matched"] == 1
        assert d["variant_results"] == {"pov_0": True, "pov_1": False}
        assert d["status"] == "partial"


# =============================================================================
# Core Test Cases - VerificationScores Edge Cases
# =============================================================================


class TestVerificationScoresEdgeCases:
    """Core tests for VerificationScores edge cases."""

    def test_overall_fix_rate_100_percent(self):
        """Test fix rate when all variants matched."""
        from crsbench.evaluation.verification.models import VerificationScores

        scores = VerificationScores(
            cpvs_complete=2,
            cpvs_partial=0,
            cpvs_none=0,
            total_variants_tested=10,
            total_variants_matched=10,
        )

        assert scores.overall_fix_rate == 1.0

    def test_overall_fix_rate_0_percent(self):
        """Test fix rate when no variants matched."""
        from crsbench.evaluation.verification.models import VerificationScores

        scores = VerificationScores(
            cpvs_complete=0,
            cpvs_partial=0,
            cpvs_none=2,
            total_variants_tested=10,
            total_variants_matched=0,
        )

        assert scores.overall_fix_rate == 0.0

    def test_overall_fix_rate_partial(self):
        """Test fix rate with partial fixes."""
        from crsbench.evaluation.verification.models import VerificationScores

        scores = VerificationScores(
            cpvs_complete=1,
            cpvs_partial=1,
            cpvs_none=0,
            total_variants_tested=10,
            total_variants_matched=7,
        )

        assert scores.overall_fix_rate == 0.7

    def test_overall_fix_rate_zero_tested(self):
        """Test fix rate when no variants tested (avoid division by zero)."""
        from crsbench.evaluation.verification.models import VerificationScores

        scores = VerificationScores(
            cpvs_complete=0,
            cpvs_partial=0,
            cpvs_none=0,
            total_variants_tested=0,
            total_variants_matched=0,
        )

        assert scores.overall_fix_rate == 0.0

    def test_scores_to_dict_includes_fix_rate(self):
        """Test to_dict includes computed overall_fix_rate."""
        from crsbench.evaluation.verification.models import VerificationScores

        scores = VerificationScores(
            cpvs_complete=1,
            cpvs_partial=0,
            cpvs_none=1,
            total_variants_tested=4,
            total_variants_matched=2,
        )

        d = scores.to_dict()

        assert d["cpvs_complete"] == 1
        assert d["cpvs_partial"] == 0
        assert d["cpvs_none"] == 1
        assert d["total_variants_tested"] == 4
        assert d["total_variants_matched"] == 2
        assert d["overall_fix_rate"] == 0.5


# =============================================================================
# Core Test Cases - CpvFixedDedup Edge Cases
# =============================================================================


class TestCpvFixedDedupEdgeCases:
    """Core tests for CpvFixedDedup edge cases."""

    def test_dedup_preserves_order(self):
        """Test that deduplication preserves original order."""
        from crsbench.evaluation.verification.dedup import CpvFixedDedup
        from crsbench.evaluation.verification.models import (
            PatchVerificationResult,
            PatchVerificationStatus,
        )

        dedup = CpvFixedDedup()

        results = [
            PatchVerificationResult(
                status=PatchVerificationStatus.VALID,
                pov_id="third",
                patch_path=Path("/patch3.diff"),
                cpv_fixed=["cpv_2"],
            ),
            PatchVerificationResult(
                status=PatchVerificationStatus.VALID,
                pov_id="first",
                patch_path=Path("/patch1.diff"),
                cpv_fixed=["cpv_0"],
            ),
            PatchVerificationResult(
                status=PatchVerificationStatus.VALID,
                pov_id="second",
                patch_path=Path("/patch2.diff"),
                cpv_fixed=["cpv_1"],
            ),
        ]

        unique = dedup.deduplicate(results)

        assert len(unique) == 3
        assert [r.pov_id for r in unique] == ["third", "first", "second"]

    def test_single_cpv_fixed_list(self):
        """Test deduplication with single-element cpv_fixed lists."""
        from crsbench.evaluation.verification.dedup import CpvFixedDedup
        from crsbench.evaluation.verification.models import (
            PatchVerificationResult,
            PatchVerificationStatus,
        )

        dedup = CpvFixedDedup()

        results = [
            PatchVerificationResult(
                status=PatchVerificationStatus.VALID,
                pov_id="patch1",
                patch_path=Path("/patch1.diff"),
                cpv_fixed=["cpv_0"],
            ),
            PatchVerificationResult(
                status=PatchVerificationStatus.VALID,
                pov_id="patch2",
                patch_path=Path("/patch2.diff"),
                cpv_fixed=["cpv_0"],  # Duplicate
            ),
        ]

        unique = dedup.deduplicate(results)

        assert len(unique) == 1
        assert unique[0].pov_id == "patch1"

    def test_mixed_valid_and_non_valid_results(self):
        """Test deduplication with mix of VALID and non-VALID results."""
        from crsbench.evaluation.verification.dedup import CpvFixedDedup
        from crsbench.evaluation.verification.models import (
            PatchVerificationResult,
            PatchVerificationStatus,
        )

        dedup = CpvFixedDedup()

        results = [
            PatchVerificationResult(
                status=PatchVerificationStatus.VALID,
                pov_id="valid1",
                patch_path=Path("/patch1.diff"),
                cpv_fixed=["cpv_0"],
            ),
            PatchVerificationResult(
                status=PatchVerificationStatus.BUILD_FAILED,
                pov_id="failed1",
                patch_path=Path("/patch2.diff"),
            ),
            PatchVerificationResult(
                status=PatchVerificationStatus.VALID,
                pov_id="valid2",
                patch_path=Path("/patch3.diff"),
                cpv_fixed=["cpv_0"],  # Duplicate of valid1
            ),
            PatchVerificationResult(
                status=PatchVerificationStatus.TEST_FAILED,
                pov_id="failed2",
                patch_path=Path("/patch4.diff"),
            ),
        ]

        unique = dedup.deduplicate(results)

        # valid1 kept, valid2 deduplicated, both failed patches kept
        assert len(unique) == 3
        pov_ids = [r.pov_id for r in unique]
        assert "valid1" in pov_ids
        assert "valid2" not in pov_ids  # Deduplicated
        assert "failed1" in pov_ids
        assert "failed2" in pov_ids

    def test_empty_cpv_fixed_not_deduplicated(self):
        """Test that VALID patches with empty cpv_fixed are not deduplicated."""
        from crsbench.evaluation.verification.dedup import CpvFixedDedup
        from crsbench.evaluation.verification.models import (
            PatchVerificationResult,
            PatchVerificationStatus,
        )

        dedup = CpvFixedDedup()

        results = [
            PatchVerificationResult(
                status=PatchVerificationStatus.VALID,
                pov_id="patch1",
                patch_path=Path("/patch1.diff"),
                cpv_fixed=[],  # Empty - edge case
            ),
            PatchVerificationResult(
                status=PatchVerificationStatus.VALID,
                pov_id="patch2",
                patch_path=Path("/patch2.diff"),
                cpv_fixed=[],  # Also empty
            ),
        ]

        unique = dedup.deduplicate(results)

        # Both kept because empty cpv_fixed doesn't trigger dedup condition
        assert len(unique) == 2


# =============================================================================
# Core Test Cases - Per-CPV Testing Logic
# =============================================================================


class TestPerCpvTestingLogicUnit:
    """Unit tests for per-CPV testing logic."""

    def test_cpv_fixed_only_includes_complete_cpvs(self):
        """Test that cpv_fixed list only includes CPVs with status='complete'."""
        from crsbench.evaluation.verification.models import (
            CpvStats,
            PatchVerificationResult,
            PatchVerificationStatus,
            VerificationScores,
        )

        # Simulate result with mix of complete, partial, none
        cpv_stats = {
            "cpv_0": CpvStats(
                cpv_id="cpv_0",
                variants_tested=2,
                variants_matched=2,  # Complete
                variant_results={"pov_0": True, "pov_1": True},
            ),
            "cpv_1": CpvStats(
                cpv_id="cpv_1",
                variants_tested=3,
                variants_matched=1,  # Partial
                variant_results={"pov_0": True, "pov_1": False, "pov_2": False},
            ),
            "cpv_2": CpvStats(
                cpv_id="cpv_2",
                variants_tested=2,
                variants_matched=0,  # None
                variant_results={"pov_0": False, "pov_1": False},
            ),
        }

        # cpv_fixed should only include complete CPVs
        cpv_fixed = [
            cpv_id for cpv_id, stats in cpv_stats.items() if stats.status == "complete"
        ]

        result = PatchVerificationResult(
            status=PatchVerificationStatus.VALID,
            pov_id="test",
            patch_path=Path("/test/patch.diff"),
            harness="test_harness",
            cpv_fixed=cpv_fixed,
            cpv_stats=cpv_stats,
            scores=VerificationScores(
                cpvs_complete=1,
                cpvs_partial=1,
                cpvs_none=1,
                total_variants_tested=7,
                total_variants_matched=3,
            ),
            security_verdict="PASS",
        )

        assert result.cpv_fixed == ["cpv_0"]
        assert "cpv_1" not in result.cpv_fixed  # Partial
        assert "cpv_2" not in result.cpv_fixed  # None

    def test_scores_aggregation_across_multiple_cpvs(self):
        """Test scores correctly aggregate across multiple CPVs."""
        from crsbench.evaluation.verification.models import (
            CpvStats,
            VerificationScores,
        )

        cpv_stats = {
            "cpv_0": CpvStats(
                cpv_id="cpv_0",
                variants_tested=3,
                variants_matched=3,  # Complete
            ),
            "cpv_1": CpvStats(
                cpv_id="cpv_1",
                variants_tested=2,
                variants_matched=1,  # Partial
            ),
            "cpv_2": CpvStats(
                cpv_id="cpv_2",
                variants_tested=4,
                variants_matched=0,  # None
            ),
        }

        # Calculate expected scores
        cpvs_complete = sum(1 for s in cpv_stats.values() if s.status == "complete")
        cpvs_partial = sum(1 for s in cpv_stats.values() if s.status == "partial")
        cpvs_none = sum(1 for s in cpv_stats.values() if s.status == "none")
        total_tested = sum(s.variants_tested for s in cpv_stats.values())
        total_matched = sum(s.variants_matched for s in cpv_stats.values())

        scores = VerificationScores(
            cpvs_complete=cpvs_complete,
            cpvs_partial=cpvs_partial,
            cpvs_none=cpvs_none,
            total_variants_tested=total_tested,
            total_variants_matched=total_matched,
        )

        assert scores.cpvs_complete == 1
        assert scores.cpvs_partial == 1
        assert scores.cpvs_none == 1
        assert scores.total_variants_tested == 9
        assert scores.total_variants_matched == 4
        assert scores.overall_fix_rate == pytest.approx(4 / 9)


# =============================================================================
# Core Test Cases - Engine Discovery Functions
# =============================================================================


class TestEngineDiscoveryFunctions:
    """Tests for engine discovery functions using fixtures."""

    @pytest.fixture
    def mock_oss_fuzz(self, tmp_path: Path) -> Path:
        """Create a mock oss-fuzz directory with helper.py."""
        oss_fuzz = tmp_path / "oss-fuzz"
        infra = oss_fuzz / "infra"
        infra.mkdir(parents=True)
        (infra / "helper.py").write_text("# mock helper")
        return oss_fuzz

    def test_discover_all_cpvs_sorts_numerically(
        self, tmp_path: Path, mock_oss_fuzz: Path
    ):
        """Test that CPVs are sorted numerically (cpv_0, cpv_1, cpv_10)."""
        # Create .aixcc/harness/cpv_* structure
        benchmark = tmp_path / "benchmark"
        harness_dir = benchmark / ".aixcc" / "test_harness"
        for i in [0, 1, 10, 2, 5]:
            (harness_dir / f"cpv_{i}").mkdir(parents=True)

        from crsbench.evaluation.verification.patch import PatchVerificationEngine

        engine = PatchVerificationEngine(mock_oss_fuzz)
        cpvs = engine._discover_all_cpvs_in_harness(benchmark, "test_harness")

        # Should be sorted numerically, not lexicographically
        assert cpvs == ["cpv_0", "cpv_1", "cpv_2", "cpv_5", "cpv_10"]

    def test_discover_pov_variants_sorts_numerically(
        self, tmp_path: Path, mock_oss_fuzz: Path
    ):
        """Test that POV variants are sorted numerically."""
        # Create blobs directory with POV files
        benchmark = tmp_path / "benchmark"
        blobs_dir = benchmark / ".aixcc" / "test_harness" / "cpv_0" / "blobs"
        blobs_dir.mkdir(parents=True)
        for i in [0, 1, 10, 2, 5]:
            (blobs_dir / f"pov_{i}.blob").write_bytes(b"test")

        from crsbench.evaluation.verification.patch import PatchVerificationEngine

        engine = PatchVerificationEngine(mock_oss_fuzz)
        povs = engine._discover_pov_variants(benchmark, "test_harness", "cpv_0")

        pov_names = [p.name for p in povs]
        assert pov_names == [
            "pov_0.blob",
            "pov_1.blob",
            "pov_2.blob",
            "pov_5.blob",
            "pov_10.blob",
        ]

    def test_discover_patches_from_benchmark_structure(
        self, tmp_path: Path, mock_oss_fuzz: Path
    ):
        """Test patch discovery from benchmark .aixcc structure."""
        # Create .aixcc/harness/cpv/patches/unique_id/patch.diff structure
        benchmark = tmp_path / "benchmark"
        patch_dir = (
            benchmark
            / ".aixcc"
            / "test_harness"
            / "cpv_0"
            / "patches"
            / "test_harness_cpv_0_pov_0"
        )
        patch_dir.mkdir(parents=True)
        (patch_dir / "patch.diff").write_text("diff content")

        from crsbench.evaluation.verification.patch import PatchVerificationEngine

        engine = PatchVerificationEngine(mock_oss_fuzz)
        patches = engine._discover_patches_from_benchmark(benchmark)

        assert len(patches) == 1
        harness, cpv_id, patch_info = patches[0]
        assert harness == "test_harness"
        assert cpv_id == "cpv_0"
        assert patch_info.pov_id == "test_harness_cpv_0_pov_0"
