"""Unit tests for VerificationEngine skip_hashes filtering.

Tests for pre-verification hash filtering in VerificationEngine.
These tests verify that POVs with hashes in skip_hashes are properly skipped.
"""

import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from crsbench.evaluation.verification.utils import compute_content_hash


class TestSkipHashesFiltering:
    """Tests for skip_hashes parameter in VerificationEngine."""

    @pytest.fixture
    def pov_dir(self, tmp_path: Path) -> Path:
        """Create a directory with test POV files."""
        pov_dir = tmp_path / "povs"
        pov_dir.mkdir()

        # Create POV files with different content
        (pov_dir / "pov1.bin").write_bytes(b"content_a")
        (pov_dir / "pov2.bin").write_bytes(b"content_b")
        (pov_dir / "pov3.bin").write_bytes(b"content_c")

        return pov_dir

    def test_compute_hash_for_pov_files(self, pov_dir: Path) -> None:
        """Verify we can compute hashes for POV files correctly."""
        pov1 = pov_dir / "pov1.bin"
        pov2 = pov_dir / "pov2.bin"

        hash1 = compute_content_hash(pov1)
        hash2 = compute_content_hash(pov2)

        assert len(hash1) == 16
        assert len(hash2) == 16
        assert hash1 != hash2

    def test_duplicate_content_same_hash(self, pov_dir: Path) -> None:
        """Verify duplicate content produces same hash."""
        # Create two files with identical content
        (pov_dir / "dup1.bin").write_bytes(b"same_content")
        (pov_dir / "dup2.bin").write_bytes(b"same_content")

        hash1 = compute_content_hash(pov_dir / "dup1.bin")
        hash2 = compute_content_hash(pov_dir / "dup2.bin")

        assert hash1 == hash2

    def test_skip_hashes_filters_known_hashes(self, pov_dir: Path) -> None:
        """Test that POVs with hashes in skip_hashes are filtered out."""
        pov1_hash = compute_content_hash(pov_dir / "pov1.bin")
        skip_hashes = {pov1_hash}

        # Simulate the filtering logic from verify_all_povs
        pov_files = list(pov_dir.glob("*.bin"))
        filtered = []
        skipped = 0

        for pov_file in pov_files:
            pov_hash = compute_content_hash(pov_file)
            if pov_hash in skip_hashes:
                skipped += 1
            else:
                filtered.append(pov_file)

        # pov1.bin should be skipped, pov2.bin and pov3.bin should remain
        assert len(filtered) == 2
        assert skipped == 1
        assert pov_dir / "pov1.bin" not in filtered

    def test_skip_hashes_empty_skips_nothing(self, pov_dir: Path) -> None:
        """Test that empty skip_hashes doesn't filter anything."""
        skip_hashes: set[str] = set()

        pov_files = list(pov_dir.glob("*.bin"))
        filtered = []
        skipped = 0

        for pov_file in pov_files:
            pov_hash = compute_content_hash(pov_file)
            if pov_hash in skip_hashes:
                skipped += 1
            else:
                filtered.append(pov_file)

        assert len(filtered) == len(pov_files)
        assert skipped == 0

    def test_skip_hashes_none_skips_nothing(self, pov_dir: Path) -> None:
        """Test that None skip_hashes doesn't filter anything."""
        skip_hashes = None

        pov_files = list(pov_dir.glob("*.bin"))
        filtered = []
        skipped = 0

        for pov_file in pov_files:
            if skip_hashes:  # Same logic as in engine.py
                pov_hash = compute_content_hash(pov_file)
                if pov_hash in skip_hashes:
                    skipped += 1
                    continue
            filtered.append(pov_file)

        assert len(filtered) == len(pov_files)
        assert skipped == 0

    def test_skip_all_hashes_returns_empty(self, pov_dir: Path) -> None:
        """Test that skipping all hashes returns empty list."""
        # Compute all hashes
        pov_files = list(pov_dir.glob("*.bin"))
        skip_hashes = {compute_content_hash(f) for f in pov_files}

        filtered = []
        skipped = 0

        for pov_file in pov_files:
            pov_hash = compute_content_hash(pov_file)
            if pov_hash in skip_hashes:
                skipped += 1
            else:
                filtered.append(pov_file)

        assert len(filtered) == 0
        assert skipped == len(pov_files)

    def test_skip_hashes_with_nonmatching_hashes(self, pov_dir: Path) -> None:
        """Test that non-matching hashes don't affect filtering."""
        # Create skip_hashes with hashes that don't exist
        skip_hashes = {"000000000000", "111111111111", "aaaaaaaaaaaa"}

        pov_files = list(pov_dir.glob("*.bin"))
        filtered = []
        skipped = 0

        for pov_file in pov_files:
            pov_hash = compute_content_hash(pov_file)
            if pov_hash in skip_hashes:
                skipped += 1
            else:
                filtered.append(pov_file)

        # No files should be skipped
        assert len(filtered) == len(pov_files)
        assert skipped == 0


class TestVerifyAllPovsSkipHashes:
    """Integration tests for verify_all_povs skip_hashes parameter.

    These tests use mocking to avoid needing actual oss-fuzz infrastructure.
    """

    @pytest.fixture
    def mock_oss_fuzz(self, tmp_path: Path) -> Path:
        """Create mock oss-fuzz directory structure with mock helper.py."""
        oss_fuzz = tmp_path / "oss-fuzz"
        oss_fuzz.mkdir()
        infra_dir = oss_fuzz / "infra"
        infra_dir.mkdir()
        (oss_fuzz / "projects").mkdir()
        (oss_fuzz / "build").mkdir()

        # Copy mock helper.py
        mock_helper_src = Path(__file__).parent / "fixtures" / "mock_helper.py"
        shutil.copy(mock_helper_src, infra_dir / "helper.py")

        return oss_fuzz

    @pytest.fixture
    def pov_dir(self, tmp_path: Path) -> Path:
        """Create a directory with test POV files."""
        pov_dir = tmp_path / "povs"
        pov_dir.mkdir()

        (pov_dir / "pov1.bin").write_bytes(b"content_a")
        (pov_dir / "pov2.bin").write_bytes(b"content_b")

        return pov_dir

    @pytest.fixture
    def mock_adapter(self) -> MagicMock:
        """Create mock MetaYamlAdapter."""
        adapter = MagicMock()
        adapter.benchmark_name = "test-benchmark"
        adapter.get_harness_names.return_value = ["test_harness"]
        adapter.get_mode.return_value = MagicMock(value="full")
        return adapter

    def test_verify_all_povs_returns_tuple_with_skipped_count(
        self, mock_oss_fuzz: Path, pov_dir: Path, mock_adapter: MagicMock
    ) -> None:
        """Test that verify_all_povs returns tuple including skipped count."""
        from crsbench.evaluation.verification.pov.engine import VerificationEngine

        with (
            patch.object(VerificationEngine, "get_or_build_results") as mock_build,
            patch.object(VerificationEngine, "verify_povs_parallel") as mock_verify,
        ):
            # Setup mocks
            mock_build.return_value = {"variant1": MagicMock(success=True)}
            mock_verify.return_value = []

            engine = VerificationEngine(mock_oss_fuzz)

            # Skip pov1's hash
            pov1_hash = compute_content_hash(pov_dir / "pov1.bin")
            skip_hashes = {pov1_hash}

            results, skipped = engine.verify_all_povs(
                mock_adapter, pov_dir, skip_hashes=skip_hashes
            )

            # Should return tuple
            assert isinstance(results, list)
            assert isinstance(skipped, int)
            assert skipped == 1  # pov1.bin was skipped

    def test_verify_all_povs_no_skip_hashes_returns_zero_skipped(
        self, mock_oss_fuzz: Path, pov_dir: Path, mock_adapter: MagicMock
    ) -> None:
        """Test that verify_all_povs with no skip_hashes returns 0 skipped."""
        from crsbench.evaluation.verification.pov.engine import VerificationEngine

        with (
            patch.object(VerificationEngine, "get_or_build_results") as mock_build,
            patch.object(VerificationEngine, "verify_povs_parallel") as mock_verify,
        ):
            # Setup mocks
            mock_build.return_value = {"variant1": MagicMock(success=True)}
            mock_verify.return_value = []

            engine = VerificationEngine(mock_oss_fuzz)

            results, skipped = engine.verify_all_povs(mock_adapter, pov_dir)

            assert skipped == 0


class TestVerifyBenchmarkSkipHashes:
    """Integration tests for verify_benchmark skip_hashes parameter."""

    @pytest.fixture
    def mock_oss_fuzz(self, tmp_path: Path) -> Path:
        """Create mock oss-fuzz directory structure with mock helper.py."""
        oss_fuzz = tmp_path / "oss-fuzz"
        oss_fuzz.mkdir()
        infra_dir = oss_fuzz / "infra"
        infra_dir.mkdir()
        (oss_fuzz / "projects").mkdir()
        (oss_fuzz / "build").mkdir()

        # Copy mock helper.py
        mock_helper_src = Path(__file__).parent / "fixtures" / "mock_helper.py"
        shutil.copy(mock_helper_src, infra_dir / "helper.py")

        return oss_fuzz

    @pytest.fixture
    def benchmark_path(self, tmp_path: Path) -> Path:
        """Create mock benchmark structure."""
        benchmark_dir = tmp_path / "test-benchmark"
        benchmark_dir.mkdir()

        aixcc_dir = benchmark_dir / ".aixcc"
        aixcc_dir.mkdir()

        # Create minimal meta.yaml
        (aixcc_dir / "meta.yaml").write_text("mode: full\n")
        (benchmark_dir / "project.yaml").write_text("language: c\nmain_repo: ''\n")

        return benchmark_dir

    @pytest.fixture
    def pov_dir(self, tmp_path: Path) -> Path:
        """Create a directory with test POV files."""
        pov_dir = tmp_path / "output_povs"
        pov_dir.mkdir()

        (pov_dir / "pov1.bin").write_bytes(b"content_a")
        (pov_dir / "pov2.bin").write_bytes(b"content_b")

        return pov_dir

    def test_verify_benchmark_returns_output_with_skipped_count(
        self, mock_oss_fuzz: Path, benchmark_path: Path, pov_dir: Path
    ) -> None:
        """Test that verify_benchmark returns PovBenchmarkOutput with skipped count."""
        from crsbench.evaluation.verification.pov.engine import VerificationEngine

        with (
            patch.object(VerificationEngine, "load_adapter") as mock_adapter,
            patch.object(VerificationEngine, "get_or_build_results") as mock_build,
            patch.object(VerificationEngine, "verify_povs_parallel") as mock_verify,
        ):
            # Setup mocks
            adapter = MagicMock()
            adapter.benchmark_name = "test-benchmark"
            adapter.get_harness_names.return_value = ["test_harness"]
            adapter.get_mode.return_value = MagicMock(value="full")
            mock_adapter.return_value = adapter
            mock_build.return_value = {"variant1": MagicMock(success=True)}
            mock_verify.return_value = []

            engine = VerificationEngine(mock_oss_fuzz)

            # Skip pov1's hash
            pov1_hash = compute_content_hash(pov_dir / "pov1.bin")
            skip_hashes = {pov1_hash}

            output = engine.verify_benchmark(
                benchmark_path, pov_dir=pov_dir, skip_hashes=skip_hashes
            )

            assert isinstance(output.results, list)
            assert output.skipped_count == 1  # pov1.bin was skipped
