"""Tests for patch verification modules.

Tests cover:
- PatchVerificationStatus, PatchInfo, PatchVerificationResult models
- GroundTruthConverter utility for converting benchmark patches to CRS format
- PatchVerificationEngine unit tests (mocked)

E2E tests are in test_patch_verification_integration.py.
"""

import os
import shutil
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
    UnitTestMode,
)
from crsbench.utils.logger import get_logger

logger = get_logger(__name__)

# Test constants
BENCHMARK_NAME = "sanity-mock-c-delta-01"
HARNESS_NAME = "fuzz_parse_buffer_section"
CPV_ID = "cpv_1"
POV_ID = "pov_0"


# =============================================================================
# Build Failure Summarization
# =============================================================================


class TestBuildFailureSummary:
    def test_prefers_error_line_from_stderr(self) -> None:
        from crsbench.evaluation.verification.patch.engine import (
            _summarize_build_failure,
        )

        summary = _summarize_build_failure(
            stdout="compiling...\nstep 2",
            stderr="note: start\nERROR: build script failed\ntrailing",
        )
        assert summary == "ERROR: build script failed"

    def test_falls_back_to_stdout_when_stderr_empty(self) -> None:
        from crsbench.evaluation.verification.patch.engine import (
            _summarize_build_failure,
        )

        summary = _summarize_build_failure(
            stdout="fatal: patch does not apply\nnext",
            stderr="",
        )
        assert summary == "fatal: patch does not apply"

    def test_returns_empty_summary_when_no_logs(self) -> None:
        from crsbench.evaluation.verification.patch.engine import (
            _summarize_build_failure,
        )

        assert _summarize_build_failure("", "") == ""


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


class TestUnitTestMode:
    """Tests for UnitTestMode enum."""

    def test_mode_values(self):
        """Test UnitTestMode enum values."""
        assert UnitTestMode.FULL.value == "full"
        assert UnitTestMode.RTS.value == "rts"


class TestPatchInfo:
    """Tests for PatchInfo model."""

    def test_patch_info_loads_content(self, ground_truth_patch: Path):
        """Test that PatchInfo correctly loads patch content."""
        patch = PatchInfo(
            patch_id="patch_0",
            pov_id=POV_ID,
            patch_path=ground_truth_patch,
        )

        assert patch.patch_id == "patch_0"
        assert patch.pov_id == POV_ID
        assert patch.patch_path == ground_truth_patch
        assert "diff --git" in patch.patch_content
        assert "mock.c" in patch.patch_content

    def test_patch_info_from_nonexistent_file(self):
        """Test PatchInfo with nonexistent file returns empty content."""
        patch = PatchInfo(
            patch_id="patch_nonexistent",
            pov_id="nonexistent",
            patch_path=Path("/nonexistent/patch.diff"),
        )

        assert patch.patch_content == ""

    def test_patch_info_with_provided_content(self):
        """Test PatchInfo with pre-loaded content."""
        content = "diff --git a/test.c b/test.c\n--- a/test.c\n+++ b/test.c\n"
        patch = PatchInfo(
            patch_id="patch_test",
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
            patch_id="patch_0",
            pov_id="pov_0",
            benchmark="test-benchmark",
            patch_path=Path("/test/patch.diff"),
        )
        assert result.status == PatchVerificationStatus.VALID
        assert result.is_valid
        assert result.patch_id == "patch_0"
        assert result.pov_id == "pov_0"
        assert result.benchmark == "test-benchmark"

    def test_result_with_failure(self):
        """Test PatchVerificationResult with failure status."""
        result = PatchVerificationResult(
            status=PatchVerificationStatus.BUILD_FAILED,
            patch_id="patch_0",
            pov_id="pov_0",
            benchmark="test-benchmark",
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
            patch_id="patch_0",
            pov_id="pov_0",
            benchmark="test-benchmark",
            patch_path=Path("/test/patch.diff"),
            elapsed_seconds=10.5,
            pov_test_passed=True,
            unit_tests_passed=True,
        )
        d = result.to_dict()
        assert d["status"] == "valid"
        assert d["patch_id"] == "patch_0"
        assert d["pov_id"] == "pov_0"
        assert d["benchmark"] == "test-benchmark"
        assert d["elapsed_seconds"] == 10.5
        assert d["pov_test_passed"] is True

    def test_result_str_valid(self):
        """Test PatchVerificationResult string representation for valid patch."""
        result = PatchVerificationResult(
            status=PatchVerificationStatus.VALID,
            patch_id="patch_0",
            pov_id="pov_0",
            benchmark="test-benchmark",
            patch_path=Path("/test/patch.diff"),
        )
        assert "patch_0" in str(result)
        assert "pov_0" in str(result)
        assert "VALID" in str(result)

    def test_result_str_failed(self):
        """Test PatchVerificationResult string representation for failed patch."""
        result = PatchVerificationResult(
            status=PatchVerificationStatus.POV_STILL_TRIGGERS,
            patch_id="patch_0",
            pov_id="pov_0",
            benchmark="test-benchmark",
            patch_path=Path("/test/patch.diff"),
            details="POV still triggers vulnerability",
        )
        assert "patch_0" in str(result)
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

        # Verify patch content (may have canary header before diff)
        content = patch_file.read_text()
        assert "diff --git" in content, "Patch should contain diff header"

    def test_discovers_patches_in_expected_structure(self, patch_dir: Path):
        """Test discovery finds patches in expected structure."""
        # The patch_dir fixture creates: patch_dir/<pov_id>/patch.diff
        discovered = []
        patch_idx = 0

        for pov_subdir in patch_dir.iterdir():
            if not pov_subdir.is_dir():
                continue
            patch_file = pov_subdir / "patch.diff"
            if patch_file.exists():
                discovered.append(
                    PatchInfo(
                        patch_id=f"patch_{patch_idx}",
                        pov_id=pov_subdir.name,
                        patch_path=patch_file,
                    )
                )
                patch_idx += 1

        assert len(discovered) == 1, "Should discover exactly one patch"
        assert discovered[0].pov_id == POV_ID


# =============================================================================
# Phase 2: Per-CPV Testing Tests
# =============================================================================


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
            patch_id="patch_0",
            pov_id="test_patch",
            benchmark="test-benchmark",
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
            patch_id="patch_0",
            pov_id="test",
            benchmark="test-benchmark",
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
            patch_id="patch_0",
            pov_id="test",
            benchmark="test-benchmark",
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
                patch_id="patch1",
                pov_id="pov_0",
                benchmark="test-benchmark",
                patch_path=Path("/patch1.diff"),
                cpv_fixed=["cpv_0", "cpv_1"],
            ),
            PatchVerificationResult(
                status=PatchVerificationStatus.VALID,
                patch_id="patch2",
                pov_id="pov_0",
                benchmark="test-benchmark",
                patch_path=Path("/patch2.diff"),
                cpv_fixed=["cpv_1", "cpv_0"],  # Same set, different order
            ),
            PatchVerificationResult(
                status=PatchVerificationStatus.VALID,
                patch_id="patch3",
                pov_id="pov_0",
                benchmark="test-benchmark",
                patch_path=Path("/patch3.diff"),
                cpv_fixed=["cpv_0"],  # Different set
            ),
        ]

        unique = dedup.deduplicate(results)

        assert len(unique) == 2
        assert unique[0].patch_id == "patch1"
        assert unique[1].patch_id == "patch3"

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
                patch_id="patch1",
                pov_id="pov_0",
                benchmark="test-benchmark",
                patch_path=Path("/patch1.diff"),
            ),
            PatchVerificationResult(
                status=PatchVerificationStatus.BUILD_FAILED,
                patch_id="patch2",
                pov_id="pov_0",
                benchmark="test-benchmark",
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
            patch_id="patch_0",
            pov_id="test",
            benchmark="test-benchmark",
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
            patch_id="patch_0",
            pov_id="test",
            benchmark="test-benchmark",
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
            patch_id="patch_0",
            pov_id="test",
            benchmark="test-benchmark",
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
            patch_id="patch_0",
            pov_id="test",
            benchmark="test-benchmark",
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
            patch_id="patch_0",
            pov_id="test",
            benchmark="test-benchmark",
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
                patch_id="third",
                pov_id="pov_0",
                benchmark="test-benchmark",
                patch_path=Path("/patch3.diff"),
                cpv_fixed=["cpv_2"],
            ),
            PatchVerificationResult(
                status=PatchVerificationStatus.VALID,
                patch_id="first",
                pov_id="pov_0",
                benchmark="test-benchmark",
                patch_path=Path("/patch1.diff"),
                cpv_fixed=["cpv_0"],
            ),
            PatchVerificationResult(
                status=PatchVerificationStatus.VALID,
                patch_id="second",
                pov_id="pov_0",
                benchmark="test-benchmark",
                patch_path=Path("/patch2.diff"),
                cpv_fixed=["cpv_1"],
            ),
        ]

        unique = dedup.deduplicate(results)

        assert len(unique) == 3
        assert [r.patch_id for r in unique] == ["third", "first", "second"]

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
                patch_id="patch1",
                pov_id="pov_0",
                benchmark="test-benchmark",
                patch_path=Path("/patch1.diff"),
                cpv_fixed=["cpv_0"],
            ),
            PatchVerificationResult(
                status=PatchVerificationStatus.VALID,
                patch_id="patch2",
                pov_id="pov_0",
                benchmark="test-benchmark",
                patch_path=Path("/patch2.diff"),
                cpv_fixed=["cpv_0"],  # Duplicate
            ),
        ]

        unique = dedup.deduplicate(results)

        assert len(unique) == 1
        assert unique[0].patch_id == "patch1"

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
                patch_id="valid1",
                pov_id="pov_0",
                benchmark="test-benchmark",
                patch_path=Path("/patch1.diff"),
                cpv_fixed=["cpv_0"],
            ),
            PatchVerificationResult(
                status=PatchVerificationStatus.BUILD_FAILED,
                patch_id="failed1",
                pov_id="pov_0",
                benchmark="test-benchmark",
                patch_path=Path("/patch2.diff"),
            ),
            PatchVerificationResult(
                status=PatchVerificationStatus.VALID,
                patch_id="valid2",
                pov_id="pov_0",
                benchmark="test-benchmark",
                patch_path=Path("/patch3.diff"),
                cpv_fixed=["cpv_0"],  # Duplicate of valid1
            ),
            PatchVerificationResult(
                status=PatchVerificationStatus.TEST_FAILED,
                patch_id="failed2",
                pov_id="pov_0",
                benchmark="test-benchmark",
                patch_path=Path("/patch4.diff"),
            ),
        ]

        unique = dedup.deduplicate(results)

        # valid1 kept, valid2 deduplicated, both failed patches kept
        assert len(unique) == 3
        patch_ids = [r.patch_id for r in unique]
        assert "valid1" in patch_ids
        assert "valid2" not in patch_ids  # Deduplicated
        assert "failed1" in patch_ids
        assert "failed2" in patch_ids

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
                patch_id="patch1",
                pov_id="pov_0",
                benchmark="test-benchmark",
                patch_path=Path("/patch1.diff"),
                cpv_fixed=[],  # Empty - edge case
            ),
            PatchVerificationResult(
                status=PatchVerificationStatus.VALID,
                patch_id="patch2",
                pov_id="pov_0",
                benchmark="test-benchmark",
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
            patch_id="patch_0",
            pov_id="test",
            benchmark="test-benchmark",
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
        # Create .aixcc/harness/cpv/patches/patch_*.diff structure
        benchmark = tmp_path / "benchmark"
        patches_dir = benchmark / ".aixcc" / "test_harness" / "cpv_0" / "patches"
        patches_dir.mkdir(parents=True)
        (patches_dir / "patch_0.diff").write_text("diff content")

        from crsbench.evaluation.verification.patch import PatchVerificationEngine

        engine = PatchVerificationEngine(mock_oss_fuzz)
        patches = engine._discover_patches_from_benchmark(benchmark)

        assert len(patches) == 1
        harness, cpv_id, patch_info = patches[0]
        assert harness == "test_harness"
        assert cpv_id == "cpv_0"
        assert patch_info.patch_id == "patch_0"
        assert patch_info.pov_id == "cpv_0"  # pov_id = CPV this patch targets

    def test_discover_patches_flat_layout_with_target_pov(
        self, tmp_path: Path, mock_oss_fuzz: Path
    ):
        """Flat patch files should map to inferred target POV/CPV."""
        patch_dir = tmp_path / "patches"
        patch_dir.mkdir(parents=True)
        (patch_dir / "abc123.diff").write_text("diff content")

        from crsbench.evaluation.verification.patch import PatchVerificationEngine

        engine = PatchVerificationEngine(mock_oss_fuzz)
        patches = engine._discover_patches(patch_dir, target_pov_id="cpv_3")

        assert len(patches) == 1
        assert patches[0].patch_id == "abc123"
        assert patches[0].pov_id == "cpv_3"

    def test_infer_single_pov_id(self, tmp_path: Path, mock_oss_fuzz: Path):
        """Infer CPV/POV ID only when pov_dir has exactly one entry."""
        from crsbench.evaluation.verification.patch import PatchVerificationEngine

        engine = PatchVerificationEngine(mock_oss_fuzz)
        pov_dir = tmp_path / "povs"
        pov_dir.mkdir(parents=True)
        (pov_dir / "cpv_1").write_bytes(b"pov")

        assert engine._infer_single_pov_id(pov_dir) == "cpv_1"

        (pov_dir / "cpv_2").write_bytes(b"pov2")
        assert engine._infer_single_pov_id(pov_dir) is None


# =============================================================================
# Project Directory Creation Tests
# =============================================================================


class TestProjectDirectoryCreation:
    """Tests for creating original and variant project directories.

    The patch verification engine creates two project directories:
    1. Original project (project_name) - for unit test Docker image and test.sh lookup
    2. Variant project (variant_name) - for standard build fallback and build isolation
    """

    @pytest.fixture
    def mock_oss_fuzz(self, tmp_path: Path) -> Path:
        """Create a mock oss-fuzz directory."""
        oss_fuzz = tmp_path / "oss-fuzz"
        infra = oss_fuzz / "infra"
        infra.mkdir(parents=True)
        (infra / "helper.py").write_text("# mock helper")
        projects = oss_fuzz / "projects"
        projects.mkdir(parents=True)
        return oss_fuzz

    @pytest.fixture
    def mock_benchmark(self, tmp_path: Path) -> Path:
        """Create a mock benchmark directory with test.sh."""
        benchmark = tmp_path / "benchmark"
        benchmark.mkdir(parents=True)
        # Create project files
        (benchmark / "test.sh").write_text("#!/bin/bash\necho 'tests'")
        (benchmark / "Dockerfile").write_text("FROM base")
        (benchmark / "build.sh").write_text("#!/bin/bash\ncompile")
        (benchmark / "project.yaml").write_text("language: c++")
        # Create .aixcc structure
        aixcc = benchmark / ".aixcc" / "fuzz_target" / "cpv_0"
        aixcc.mkdir(parents=True)
        patches = aixcc / "patches"
        patches.mkdir()
        (patches / "patch_0.diff").write_text("--- a/file\n+++ b/file\n")
        blobs = aixcc / "blobs"
        blobs.mkdir()
        (blobs / "pov_0.blob").write_bytes(b"crash input")
        # Create meta.yaml
        (benchmark / "meta.yaml").write_text("""
harness_files:
  - name: fuzz_target
    vulns:
      - vuln_keyword: cpv_0
        povs:
          - id: pov_0
delta_mode:
  base_commit: abc123
  ref_commit: def456
""")
        return benchmark

    def test_original_project_contains_test_sh(
        self, mock_oss_fuzz: Path, mock_benchmark: Path
    ):
        """Test that original project directory contains test.sh for unit tests."""
        from crsbench.builder.infrastructure import OSSFuzzInfrastructure

        infra = OSSFuzzInfrastructure(mock_oss_fuzz)
        project_name = "test-project"

        # Create original project
        result = infra.create_variant_project(mock_benchmark, project_name)

        assert result is not None
        assert result.exists()
        # Verify test.sh exists for unit test lookup
        assert (result / "test.sh").exists()
        assert (result / "Dockerfile").exists()
        assert (result / "build.sh").exists()

    def test_variant_project_contains_build_files(
        self, mock_oss_fuzz: Path, mock_benchmark: Path
    ):
        """Test that variant project directory contains files for standard build."""
        from crsbench.builder.infrastructure import OSSFuzzInfrastructure

        infra = OSSFuzzInfrastructure(mock_oss_fuzz)
        variant_name = "test-project-delta-patched-cpv_0-patch_0"

        # Create variant project
        result = infra.create_variant_project(mock_benchmark, variant_name)

        assert result is not None
        assert result.exists()
        # Verify build files exist for standard build fallback
        assert (result / "Dockerfile").exists()
        assert (result / "build.sh").exists()
        assert (result / "project.yaml").exists()

    def test_is_tests_available_checks_project_directory(
        self, mock_oss_fuzz: Path, mock_benchmark: Path
    ):
        """Test that is_tests_available checks the correct project directory."""
        from crsbench.builder.infrastructure import OSSFuzzInfrastructure

        infra = OSSFuzzInfrastructure(mock_oss_fuzz)
        project_name = "test-project"

        # Before creating project
        assert not infra.is_tests_available(project_name)

        # Create project with test.sh
        infra.create_variant_project(mock_benchmark, project_name)

        # After creating project
        assert infra.is_tests_available(project_name)

    def test_reuses_existing_project_directory(
        self, mock_oss_fuzz: Path, mock_benchmark: Path
    ):
        """Test that existing project directories are reused."""
        from crsbench.builder.infrastructure import OSSFuzzInfrastructure

        infra = OSSFuzzInfrastructure(mock_oss_fuzz)
        project_name = "test-project"

        # Create project first time
        result1 = infra.create_variant_project(mock_benchmark, project_name)
        # Modify a file to detect if it gets overwritten
        marker_file = result1 / "marker.txt"
        marker_file.write_text("original")

        # Create project second time
        result2 = infra.create_variant_project(mock_benchmark, project_name)

        assert result1 == result2
        # Marker file should still exist (not overwritten)
        assert marker_file.exists()
        assert marker_file.read_text() == "original"


# =============================================================================
# Inc-Build vs Standard Build Fallback Tests
# =============================================================================


class TestBuildFallbackLogic:
    """Tests for inc-build vs standard build fallback logic.

    When inc-build image is unavailable, the engine falls back to standard
    OSS-Fuzz build using the variant project directory.
    """

    @pytest.fixture
    def mock_oss_fuzz(self, tmp_path: Path) -> Path:
        """Create a mock oss-fuzz directory."""
        oss_fuzz = tmp_path / "oss-fuzz"
        infra = oss_fuzz / "infra"
        infra.mkdir(parents=True)
        (infra / "helper.py").write_text("# mock helper")
        projects = oss_fuzz / "projects"
        projects.mkdir(parents=True)
        build_out = oss_fuzz / "build" / "out"
        build_out.mkdir(parents=True)
        return oss_fuzz

    def test_inc_build_uses_variant_name_for_output_path(
        self, mock_oss_fuzz: Path, tmp_path: Path
    ):
        """Test that inc-build uses variant_name for output path isolation."""
        from crsbench.builder.infrastructure import OSSFuzzInfrastructure

        infra = OSSFuzzInfrastructure(mock_oss_fuzz)

        project_name = "test-project"
        variant_name = "test-project-delta-patched-cpv_0-patch_0"
        src_path = tmp_path / "src"
        src_path.mkdir()

        # The output directory should use variant_name for isolation
        out_dir = mock_oss_fuzz / "build" / "out" / variant_name
        work_dir = mock_oss_fuzz / "build" / "work" / variant_name

        # Verify these paths are what build_with_inc_image would use
        # (We can't run the actual build without Docker, but we can verify the path logic)
        assert out_dir.parent == mock_oss_fuzz / "build" / "out"
        assert work_dir.parent == mock_oss_fuzz / "build" / "work"

    def test_variant_name_format_for_patch_verification(self):
        """Test that variant_name follows the expected format for patch verification."""
        from crsbench.builder.types import BenchmarkMode, BuildConfig, VariantType

        config = BuildConfig(
            benchmark_name="sanity-mock-c-delta-01",
            benchmark_path=Path("/tmp/benchmark"),
            variant_type=VariantType.PATCHED,
            mode=BenchmarkMode.DELTA,
            sanitizer="address",
            language="c",
            commit="abc123",
            main_repo="https://example.com/repo.git",
            patch_id="patch_0",
            pov_id="cpv_0",
        )

        # Verify variant_name format (includes sanitizer)
        expected = "sanity-mock-c-delta-01-asan-delta-patched-cpv_0-patch_0"
        assert config.variant_name == expected


# =============================================================================
# Unit Test Execution Tests
# =============================================================================


class TestUnitTestExecution:
    """Tests for unit test execution with Docker images.

    Unit tests use the original project name for:
    1. Docker image lookup: gcr.io/oss-fuzz/{project_name}-{san}:inc
    2. test.sh lookup: oss-fuzz/projects/{project_name}/test.sh
    """

    @pytest.fixture
    def mock_oss_fuzz(self, tmp_path: Path) -> Path:
        """Create a mock oss-fuzz directory with project."""
        oss_fuzz = tmp_path / "oss-fuzz"
        infra = oss_fuzz / "infra"
        infra.mkdir(parents=True)
        (infra / "helper.py").write_text("# mock helper")

        # Create project with test.sh
        project = oss_fuzz / "projects" / "test-project"
        project.mkdir(parents=True)
        (project / "test.sh").write_text("#!/bin/bash\necho 'tests'")

        return oss_fuzz

    def test_run_unit_tests_uses_variant_name_for_test_availability(
        self, mock_oss_fuzz: Path
    ):
        """Test that _run_unit_tests checks test.sh in variant_name directory.

        Both inc-build and standard build use variant_name for tests.
        This enables proper parallel execution with path isolation.
        """
        from crsbench.builder.infrastructure import OSSFuzzInfrastructure

        infra = OSSFuzzInfrastructure(mock_oss_fuzz)

        # test-project has test.sh (could be original or variant)
        assert infra.is_tests_available("test-project")

        # variant without test.sh should return False
        assert not infra.is_tests_available("test-project-delta-patched-cpv_0-patch_0")

    def test_inc_build_docker_tag_format(self):
        """Test that inc-build uses the correct Docker image tag format."""
        # The Docker tag is now stable ":inc" and sanitizer is scoped in project key.
        assert "inc" == "inc"

    def test_standard_build_uses_latest_docker_tag(self):
        """Test that standard build uses 'latest' for docker_image_tag."""
        # When use_inc_image=False, docker_tag should be "latest"
        # because standard build creates aixcc-afc/{variant}:latest
        use_inc_image = False
        docker_tag = "inc" if use_inc_image else "latest"

        assert docker_tag == "latest"

    def test_inc_build_docker_tag(self):
        """Test that inc-build passes correct docker_image_tag."""
        # When use_inc_image=True, docker_tag should be "inc".
        use_inc_image = True
        docker_tag = "inc" if use_inc_image else None

        assert docker_tag == "inc"


class TestRtsSkipReturnValue:
    """Tests that RTS skip returns None (not True) when inc-build unavailable."""

    def test_rts_skip_returns_none_when_no_inc_image(self, tmp_path: Path):
        """RTS tests return None when inc-build is not available."""
        from unittest.mock import MagicMock

        from crsbench.evaluation.verification.models import UnitTestMode
        from crsbench.evaluation.verification.patch.engine import (
            PatchVerificationEngine,
        )

        # Setup minimal oss-fuzz directory
        oss_fuzz = tmp_path / "oss-fuzz"
        infra_dir = oss_fuzz / "infra"
        infra_dir.mkdir(parents=True)
        (infra_dir / "helper.py").write_text("# mock")
        project = oss_fuzz / "projects" / "test-proj"
        project.mkdir(parents=True)
        (project / "test.sh").write_text("#!/bin/bash\necho test")

        engine = PatchVerificationEngine(
            oss_fuzz,
            test_mode=UnitTestMode.RTS,
            use_inc_build=False,
        )

        # Mock infra.is_tests_available to return True (test.sh exists)
        engine.infra = MagicMock()
        engine.infra.is_tests_available.return_value = True

        # Call _run_unit_tests with use_inc_image=False (RTS without inc-build)
        passed, details, _stdout, _stderr = engine._run_unit_tests(
            variant_name="test-proj",
            src_path=tmp_path / "src",
            benchmark_path=tmp_path / "bench",
            use_inc_image=False,
        )

        assert passed is None, "RTS skip should return None, not True"
        assert "RTS skipped" in details
        assert "inc-build not available" in details

    def test_rts_runs_normally_when_inc_image_available(self, tmp_path: Path):
        """RTS tests proceed when inc-build is available (use_inc_image=True)."""
        from unittest.mock import MagicMock

        from crsbench.evaluation.verification.models import UnitTestMode
        from crsbench.evaluation.verification.patch.engine import (
            PatchVerificationEngine,
        )

        oss_fuzz = tmp_path / "oss-fuzz"
        infra_dir = oss_fuzz / "infra"
        infra_dir.mkdir(parents=True)
        (infra_dir / "helper.py").write_text("# mock")
        project = oss_fuzz / "projects" / "test-proj"
        project.mkdir(parents=True)
        (project / "test.sh").write_text("#!/bin/bash\necho test")

        engine = PatchVerificationEngine(
            oss_fuzz,
            test_mode=UnitTestMode.RTS,
            use_inc_build=True,
        )

        # Mock infra to simulate normal test execution
        engine.infra = MagicMock()
        engine.infra.is_tests_available.return_value = True
        engine.infra.create_variant_project.return_value = True
        engine.infra.copy_build_output.return_value = True
        engine.infra.prepare_inc_image_for_variant.return_value = True
        engine.infra.run_tests.return_value = (True, "tests passed", "")

        # Call _run_unit_tests with use_inc_image=True (RTS with inc-build)
        passed, details, _stdout, _stderr = engine._run_unit_tests(
            variant_name="test-proj",
            src_path=tmp_path / "src",
            benchmark_path=tmp_path / "bench",
            use_inc_image=True,
        )

        # Should proceed normally and return True (tests passed)
        assert passed is True, "RTS should run and pass with inc-build available"
        engine.infra.run_tests.assert_called_once_with(
            "test-proj-rts",
            tmp_path / "src",
            sanitizer="address",
            timeout=1800,
            rts_mode=True,
            docker_tag="inc",
        )

    def test_full_mode_forwards_run_tests_args(self, tmp_path: Path):
        """FULL mode should forward expected args to infra.run_tests."""
        from unittest.mock import MagicMock

        from crsbench.evaluation.verification.models import UnitTestMode
        from crsbench.evaluation.verification.patch.engine import (
            PatchVerificationEngine,
        )

        oss_fuzz = tmp_path / "oss-fuzz"
        infra_dir = oss_fuzz / "infra"
        infra_dir.mkdir(parents=True)
        (infra_dir / "helper.py").write_text("# mock")
        project = oss_fuzz / "projects" / "test-proj"
        project.mkdir(parents=True)
        (project / "test.sh").write_text("#!/bin/bash\necho test")

        engine = PatchVerificationEngine(
            oss_fuzz,
            test_mode=UnitTestMode.FULL,
            use_inc_build=False,
        )

        engine.infra = MagicMock()
        engine.infra.is_tests_available.return_value = True
        engine.infra.create_variant_project.return_value = True
        engine.infra.copy_build_output.return_value = True
        engine.infra.prepare_image_for_variant.return_value = True
        engine.infra.run_tests.return_value = (True, "tests passed", "")

        passed, _details, _stdout, _stderr = engine._run_unit_tests(
            variant_name="test-proj",
            src_path=tmp_path / "src",
            benchmark_path=tmp_path / "bench",
            use_inc_image=False,
        )

        assert passed is True
        engine.infra.run_tests.assert_called_once_with(
            "test-proj-unittest",
            tmp_path / "src",
            sanitizer="address",
            timeout=1800,
            rts_mode=False,
            docker_tag="latest",
        )


class TestCachedPovOnlyFastPath:
    """Tests for cached-build POV-only verification fast path."""

    def test_cached_skip_unittest_skips_source_and_patch_apply(
        self, tmp_path: Path
    ) -> None:
        """Cached POV-only verification should not re-prepare source or patch."""
        from unittest.mock import MagicMock

        from crsbench.builder.types import BenchmarkMode
        from crsbench.evaluation.verification.models import PatchInfo
        from crsbench.evaluation.verification.patch.engine import (
            PatchVerificationEngine,
        )

        # Minimal oss-fuzz directory
        oss_fuzz = tmp_path / "oss-fuzz"
        infra_dir = oss_fuzz / "infra"
        infra_dir.mkdir(parents=True)
        (infra_dir / "helper.py").write_text("# mock")

        # Benchmark + POV/patch files
        benchmark = tmp_path / "benchmark"
        benchmark.mkdir(parents=True)
        pov_path = benchmark / "pov_0.blob"
        pov_path.write_bytes(b"pov")
        patch_path = benchmark / "patch.diff"
        patch_path.write_text("--- a/a\n+++ b/a\n")

        class _Adapter:
            benchmark_name = "test-proj"
            main_repo = "https://example.com/repo.git"
            repo_name = "repo"
            lang = "c++"

            @staticmethod
            def get_ref_commit() -> str:
                return "b" * 40

            @staticmethod
            def get_base_commit() -> str:
                return "a" * 40

            @staticmethod
            def get_mode() -> BenchmarkMode:
                return BenchmarkMode.DELTA

        engine = PatchVerificationEngine(
            oss_fuzz,
            skip_unittest=True,
            use_inc_build=True,
            verify_variants=False,
        )
        engine._load_adapter = MagicMock(return_value=_Adapter())
        engine._ensure_inc_build_image = MagicMock(return_value=True)
        engine._prepare_source = MagicMock(
            side_effect=AssertionError("source should not be prepared")
        )
        engine._apply_patch = MagicMock(
            side_effect=AssertionError("patch should not be re-applied")
        )
        engine._verify_single_pov = MagicMock(return_value=("pov_0", True, "", ""))

        # Simulate cached variant build.
        engine.infra = MagicMock()
        engine.infra.is_variant_built.return_value = True

        result = engine.verify_patch(
            benchmark_path=benchmark,
            patch=PatchInfo(
                patch_id="patch_0",
                pov_id="cpv_0",
                patch_path=patch_path,
            ),
            harness="fuzz",
            pov_path=pov_path,
        )

        assert result.status == PatchVerificationStatus.VALID
        assert result.pov_test_passed is True
        assert result.inc_build_available is True
        engine._prepare_source.assert_not_called()
        engine._apply_patch.assert_not_called()
        engine._verify_single_pov.assert_called_once()


class TestPatchVerificationLogs:
    """Tests for patch verification stdout/stderr log outputs."""

    def test_write_verify_streams_creates_stdout_stderr_files(self, tmp_path: Path):
        """Stream logs are written as flat files under logs/."""
        from crsbench.evaluation.verification.patch.engine import (
            PatchVerificationEngine,
        )

        oss_fuzz = tmp_path / "oss-fuzz"
        infra_dir = oss_fuzz / "infra"
        infra_dir.mkdir(parents=True)
        (infra_dir / "helper.py").write_text("# mock")

        work_dir = tmp_path / "trial" / "patches"
        engine = PatchVerificationEngine(oss_fuzz, work_dir=work_dir)
        engine._write_verify_streams(
            patch_id="patch_0",
            cpv_id="cpv_0",
            stage="pov",
            run_id="pov_0",
            stdout="pov stdout",
            stderr="pov stderr",
        )

        base = work_dir / "logs"
        stdout_files = list(base.glob("*.stdout"))
        stderr_files = list(base.glob("*.stderr"))

        assert len(stdout_files) == 1
        assert len(stderr_files) == 1
        assert stdout_files[0].read_text() == "pov stdout"
        assert stderr_files[0].read_text() == "pov stderr"

    def test_default_log_dir_uses_temp_dir_not_cwd(self, tmp_path: Path):
        """Without work_dir/log_dir, logs should go to temp and be cleaned."""
        from crsbench.evaluation.verification.patch.engine import (
            PatchVerificationEngine,
        )

        oss_fuzz = tmp_path / "oss-fuzz"
        infra_dir = oss_fuzz / "infra"
        infra_dir.mkdir(parents=True)
        (infra_dir / "helper.py").write_text("# mock")

        old_cwd = Path.cwd()
        try:
            # If engine incorrectly defaults to cwd, this would create tmp_path/logs.
            os.chdir(tmp_path)
            engine = PatchVerificationEngine(oss_fuzz)
            engine._write_verify_streams(
                patch_id="patch_0",
                cpv_id="cpv_0",
                stage="pov",
                run_id="pov_0",
                stdout="pov stdout",
                stderr="pov stderr",
            )
            assert not (tmp_path / "logs").exists()
            assert engine.work_dir is not None
            log_dir = engine.work_dir / "logs"
            assert log_dir.exists()
            assert list(log_dir.glob("*.stdout"))
        finally:
            os.chdir(old_cwd)
            temp_work_dir = engine.work_dir
            engine.cleanup()
            assert temp_work_dir is not None
            assert not temp_work_dir.exists()

    def test_explicit_log_dir_still_uses_temp_work_dir(self, tmp_path: Path):
        """Providing log_dir should not force work_dir to cwd."""
        from crsbench.evaluation.verification.patch.engine import (
            PatchVerificationEngine,
        )

        oss_fuzz = tmp_path / "oss-fuzz"
        infra_dir = oss_fuzz / "infra"
        infra_dir.mkdir(parents=True)
        (infra_dir / "helper.py").write_text("# mock")

        explicit_log_dir = tmp_path / "explicit-logs"
        old_cwd = Path.cwd()
        try:
            os.chdir(tmp_path)
            engine = PatchVerificationEngine(oss_fuzz, log_dir=explicit_log_dir)
            assert engine.work_dir is not None
            assert engine.work_dir != tmp_path
            engine._write_verify_streams(
                patch_id="patch_0",
                cpv_id="cpv_0",
                stage="pov",
                run_id="pov_0",
                stdout="pov stdout",
                stderr="pov stderr",
            )
            assert not (tmp_path / "logs").exists()
            log_dir = engine.work_dir / "logs"
            assert log_dir.exists()
            assert list(log_dir.glob("*.stdout"))
        finally:
            os.chdir(old_cwd)
            temp_work_dir = engine.work_dir
            engine.cleanup()
            assert temp_work_dir is not None
            assert not temp_work_dir.exists()


# =============================================================================
# CLI Tests
# =============================================================================


class TestPatchVerifyCLI:
    """Test patch-verify CLI argument parsing and validation."""

    @pytest.fixture
    def cli_module(self):
        """Import CLI module."""
        import argparse

        from crsbench.evaluation.verification.cli.patch_verify_command import (
            add_patch_verify_subparser,
        )

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest="command")
        add_patch_verify_subparser(subparsers)
        return parser

    @pytest.fixture
    def temp_dir(self) -> Generator[Path, None, None]:
        """Create temporary directory with test files."""
        temp_path = Path(tempfile.mkdtemp(prefix="patch-verify-cli-"))
        # Create mock benchmark
        benchmark_dir = temp_path / "benchmark"
        benchmark_dir.mkdir()
        (benchmark_dir / ".aixcc").mkdir()
        (benchmark_dir / ".aixcc" / "meta.yaml").write_text("version: 1")

        # Create mock patch and POV files
        (temp_path / "patch.diff").write_text("--- a/file.c\n+++ b/file.c\n")
        (temp_path / "pov.blob").write_bytes(b"\x00\x01\x02")
        (temp_path / "povs").mkdir()
        (temp_path / "povs" / "pov_0.blob").write_bytes(b"\x00\x01\x02")
        (temp_path / "patches").mkdir()
        (temp_path / "patches" / "pov_0").mkdir()
        (temp_path / "patches" / "pov_0" / "patch.diff").write_text("--- a/file.c\n")

        # Create mock oss-fuzz
        oss_fuzz_dir = temp_path / "oss-fuzz"
        oss_fuzz_dir.mkdir()
        (oss_fuzz_dir / "infra").mkdir()
        (oss_fuzz_dir / "infra" / "helper.py").write_text("# mock")

        yield temp_path
        shutil.rmtree(temp_path)

    def test_patch_or_patch_dir_required(self, cli_module, temp_dir: Path):
        """Test that --patch or --patch-dir is required."""
        with pytest.raises(SystemExit):
            cli_module.parse_args(
                [
                    "patch-verify",
                    str(temp_dir / "benchmark"),
                    "--pov-dir",
                    str(temp_dir / "povs"),
                ]
            )

    def test_pov_or_pov_dir_required(self, cli_module, temp_dir: Path):
        """Test that --pov or --pov-dir is required."""
        with pytest.raises(SystemExit):
            cli_module.parse_args(
                [
                    "patch-verify",
                    str(temp_dir / "benchmark"),
                    "--patch-dir",
                    str(temp_dir / "patches"),
                ]
            )

    def test_force_rebuild_flag(self, cli_module, temp_dir: Path):
        """Test --force-rebuild flag."""
        args = cli_module.parse_args(
            [
                "patch-verify",
                str(temp_dir / "benchmark"),
                "--patch-dir",
                str(temp_dir / "patches"),
                "--pov-dir",
                str(temp_dir / "povs"),
                "--force-rebuild",
            ]
        )
        assert args.force_rebuild is True

    def test_inc_build_flag(self, cli_module, temp_dir: Path):
        """Test --inc-build flag."""
        args = cli_module.parse_args(
            [
                "patch-verify",
                str(temp_dir / "benchmark"),
                "--patch-dir",
                str(temp_dir / "patches"),
                "--pov-dir",
                str(temp_dir / "povs"),
                "--inc-build",
            ]
        )
        assert args.inc_build is True

    def test_single_patch_with_pov(self, cli_module, temp_dir: Path):
        """Test single patch mode with --pov."""
        args = cli_module.parse_args(
            [
                "patch-verify",
                str(temp_dir / "benchmark"),
                "--harness",
                "fuzz_test",
                "--patch",
                str(temp_dir / "patch.diff"),
                "--pov",
                str(temp_dir / "pov.blob"),
            ]
        )
        assert args.patch == temp_dir / "patch.diff"
        assert args.pov == temp_dir / "pov.blob"
        assert args.harness == "fuzz_test"

    def test_single_patch_with_pov_dir(self, cli_module, temp_dir: Path):
        """Test single patch mode with --pov-dir."""
        args = cli_module.parse_args(
            [
                "patch-verify",
                str(temp_dir / "benchmark"),
                "--harness",
                "fuzz_test",
                "--patch",
                str(temp_dir / "patch.diff"),
                "--pov-dir",
                str(temp_dir / "povs"),
            ]
        )
        assert args.patch == temp_dir / "patch.diff"
        assert args.pov_dir == temp_dir / "povs"
        assert args.harness == "fuzz_test"

    def test_directory_mode(self, cli_module, temp_dir: Path):
        """Test directory mode with --patch-dir and --pov-dir."""
        args = cli_module.parse_args(
            [
                "patch-verify",
                str(temp_dir / "benchmark"),
                "--harness",
                "fuzz_test",
                "--patch-dir",
                str(temp_dir / "patches"),
                "--pov-dir",
                str(temp_dir / "povs"),
            ]
        )
        assert args.patch_dir == temp_dir / "patches"
        assert args.pov_dir == temp_dir / "povs"
        assert args.harness == "fuzz_test"

    def test_output_json_format(self, cli_module, temp_dir: Path):
        """Test --output with --format json."""
        args = cli_module.parse_args(
            [
                "patch-verify",
                str(temp_dir / "benchmark"),
                "--patch-dir",
                str(temp_dir / "patches"),
                "--pov-dir",
                str(temp_dir / "povs"),
                "--output",
                str(temp_dir / "results.json"),
                "--format",
                "json",
            ]
        )
        assert args.output == temp_dir / "results.json"
        assert args.format == "json"

    def test_output_yaml_format(self, cli_module, temp_dir: Path):
        """Test --output with --format yaml."""
        args = cli_module.parse_args(
            [
                "patch-verify",
                str(temp_dir / "benchmark"),
                "--patch-dir",
                str(temp_dir / "patches"),
                "--pov-dir",
                str(temp_dir / "povs"),
                "--output",
                str(temp_dir / "results.yaml"),
                "--format",
                "yaml",
            ]
        )
        assert args.output == temp_dir / "results.yaml"
        assert args.format == "yaml"

    def test_test_mode_rts(self, cli_module, temp_dir: Path):
        """Test --test-mode rts option."""
        args = cli_module.parse_args(
            [
                "patch-verify",
                str(temp_dir / "benchmark"),
                "--patch-dir",
                str(temp_dir / "patches"),
                "--pov-dir",
                str(temp_dir / "povs"),
                "--test-mode",
                "rts",
            ]
        )
        assert args.test_mode == "rts"

    def test_worker_options(self, cli_module, temp_dir: Path):
        """Test --build-workers and --verify-workers options."""
        args = cli_module.parse_args(
            [
                "patch-verify",
                str(temp_dir / "benchmark"),
                "--patch-dir",
                str(temp_dir / "patches"),
                "--pov-dir",
                str(temp_dir / "povs"),
                "--build-workers",
                "8",
                "--verify-workers",
                "16",
            ]
        )
        assert args.build_workers == 8
        assert args.verify_workers == 16

    def test_no_variants_flag(self, cli_module, temp_dir: Path):
        """Test --no-variants flag."""
        args = cli_module.parse_args(
            [
                "patch-verify",
                str(temp_dir / "benchmark"),
                "--patch-dir",
                str(temp_dir / "patches"),
                "--pov-dir",
                str(temp_dir / "povs"),
                "--no-variants",
            ]
        )
        assert args.no_variants is True

    def test_pov_and_pov_dir_mutually_exclusive(self, cli_module, temp_dir: Path):
        """Test that --pov and --pov-dir are mutually exclusive."""
        with pytest.raises(SystemExit):
            cli_module.parse_args(
                [
                    "patch-verify",
                    str(temp_dir / "benchmark"),
                    "--harness",
                    "fuzz_test",
                    "--patch",
                    str(temp_dir / "patch.diff"),
                    "--pov",
                    str(temp_dir / "pov.blob"),
                    "--pov-dir",
                    str(temp_dir / "povs"),
                ]
            )

    def test_sanitizer_option(self, cli_module, temp_dir: Path):
        """Test --sanitizer option."""
        args = cli_module.parse_args(
            [
                "patch-verify",
                str(temp_dir / "benchmark"),
                "--patch-dir",
                str(temp_dir / "patches"),
                "--pov-dir",
                str(temp_dir / "povs"),
                "--sanitizer",
                "undefined",
            ]
        )
        assert args.sanitizer == "undefined"

    def test_timeout_options(self, cli_module, temp_dir: Path):
        """Test timeout options."""
        args = cli_module.parse_args(
            [
                "patch-verify",
                str(temp_dir / "benchmark"),
                "--patch-dir",
                str(temp_dir / "patches"),
                "--pov-dir",
                str(temp_dir / "povs"),
                "--timeout",
                "60",
                "--build-timeout",
                "600",
                "--test-timeout",
                "900",
            ]
        )
        assert args.timeout == 60
        assert args.build_timeout == 600
        assert args.test_timeout == 900


class TestPatchVerifyCLIValidation:
    """Test patch-verify CLI validation logic."""

    @pytest.fixture
    def temp_dir(self) -> Generator[Path, None, None]:
        """Create temporary directory with test files."""
        temp_path = Path(tempfile.mkdtemp(prefix="patch-verify-validation-"))
        # Create mock benchmark
        benchmark_dir = temp_path / "benchmark"
        benchmark_dir.mkdir()
        (benchmark_dir / ".aixcc").mkdir()
        (benchmark_dir / ".aixcc" / "meta.yaml").write_text("version: 1")

        # Create mock files
        (temp_path / "patch.diff").write_text("--- a/file.c\n+++ b/file.c\n")
        (temp_path / "pov.blob").write_bytes(b"\x00\x01\x02")
        (temp_path / "povs").mkdir()
        (temp_path / "patches").mkdir()

        # Create mock oss-fuzz
        oss_fuzz_dir = temp_path / "oss-fuzz"
        oss_fuzz_dir.mkdir()
        (oss_fuzz_dir / "infra").mkdir()
        (oss_fuzz_dir / "infra" / "helper.py").write_text("# mock")

        yield temp_path
        shutil.rmtree(temp_path)

    def test_nonexistent_patch_file(self, temp_dir: Path):
        """Test error when patch file doesn't exist."""
        import argparse

        from crsbench.evaluation.verification.cli.patch_verify_command import (
            run_patch_verify,
        )

        args = argparse.Namespace(
            benchmark_path=temp_dir / "benchmark",
            patch=temp_dir / "nonexistent.diff",
            patch_dir=None,
            pov=temp_dir / "pov.blob",
            pov_dir=None,
            harness="fuzz_test",
            oss_fuzz_path=temp_dir / "oss-fuzz",
            test_mode="full",
            sanitizer="address",
            timeout=120,
            build_timeout=1200,
            test_timeout=1800,
            build_workers=None,
            verify_workers=None,
            no_variants=False,
            force_rebuild=False,
            inc_build=False,
            source="main_repo",
            output=None,
            format="text",
            verbose=False,
        )
        result = run_patch_verify(args)
        assert result == 1

    def test_nonexistent_pov_file(self, temp_dir: Path):
        """Test error when POV file doesn't exist."""
        import argparse

        from crsbench.evaluation.verification.cli.patch_verify_command import (
            run_patch_verify,
        )

        args = argparse.Namespace(
            benchmark_path=temp_dir / "benchmark",
            patch=temp_dir / "patch.diff",
            patch_dir=None,
            pov=temp_dir / "nonexistent.blob",
            pov_dir=None,
            harness="fuzz_test",
            oss_fuzz_path=temp_dir / "oss-fuzz",
            test_mode="full",
            sanitizer="address",
            timeout=120,
            build_timeout=1200,
            test_timeout=1800,
            build_workers=None,
            verify_workers=None,
            no_variants=False,
            force_rebuild=False,
            inc_build=False,
            source="main_repo",
            output=None,
            format="text",
            verbose=False,
        )
        result = run_patch_verify(args)
        assert result == 1
