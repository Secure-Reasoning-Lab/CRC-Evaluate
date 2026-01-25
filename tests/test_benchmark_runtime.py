"""Tests for crsbench.benchmark.runtime module."""

import subprocess
import tarfile
from pathlib import Path
from unittest.mock import patch

import pytest
from crsbench.benchmark.runtime.loader import (
    has_bundled_source,
    load_benchmark_source,
    prepare_source_from_bundle,
)
from crsbench.benchmark.runtime.models import BenchmarkSource


class TestBenchmarkSource:
    """Tests for BenchmarkSource dataclass."""

    def test_bundled_source_with_path(self, tmp_path: Path) -> None:
        """Test bundled source can have path (extracted location)."""
        source = BenchmarkSource(path=tmp_path, is_bundled=True)
        assert source.path == tmp_path
        assert source.is_bundled is True
        assert source.requires_source_path is True

    def test_cloned_source(self, tmp_path: Path) -> None:
        """Test cloned source has path set."""
        source = BenchmarkSource(path=tmp_path, is_bundled=False)
        assert source.path == tmp_path
        assert source.is_bundled is False
        assert source.requires_source_path is True

    def test_requires_source_path_false_when_no_path(self) -> None:
        """Test requires_source_path is False when path is None."""
        source = BenchmarkSource(path=None, is_bundled=False)
        assert source.requires_source_path is False

    def test_bundled_without_path(self) -> None:
        """Test bundled source can have path=None (not yet extracted)."""
        source = BenchmarkSource(path=None, is_bundled=True)
        assert source.path is None
        assert source.is_bundled is True
        assert source.requires_source_path is False


class TestHasBundledSource:
    """Tests for has_bundled_source function."""

    def test_no_pkgs_dir(self, tmp_path: Path) -> None:
        """Test returns False when pkgs/ doesn't exist."""
        assert has_bundled_source(tmp_path) is False

    def test_empty_pkgs_dir(self, tmp_path: Path) -> None:
        """Test returns False when pkgs/ is empty."""
        (tmp_path / "pkgs").mkdir()
        assert has_bundled_source(tmp_path) is False

    def test_pkgs_with_non_tarball(self, tmp_path: Path) -> None:
        """Test returns False when pkgs/ has no tarballs."""
        pkgs = tmp_path / "pkgs"
        pkgs.mkdir()
        (pkgs / "readme.txt").write_text("info")
        assert has_bundled_source(tmp_path) is False

    def test_pkgs_with_tarball(self, tmp_path: Path) -> None:
        """Test returns True when pkgs/ has tarball."""
        pkgs = tmp_path / "pkgs"
        pkgs.mkdir()
        (pkgs / "source.tar.gz").write_text("dummy tarball")
        assert has_bundled_source(tmp_path) is True

    def test_pkgs_with_multiple_tarballs(self, tmp_path: Path) -> None:
        """Test returns True when pkgs/ has multiple tarballs."""
        pkgs = tmp_path / "pkgs"
        pkgs.mkdir()
        (pkgs / "source1.tar.gz").write_text("dummy")
        (pkgs / "source2.tar.gz").write_text("dummy")
        assert has_bundled_source(tmp_path) is True


class TestLoadBenchmarkSource:
    """Tests for load_benchmark_source function."""

    def test_cloned_source_calls_repo_manager(self, tmp_path: Path) -> None:
        """Test that non-bundled source calls ensure_project_repository."""
        dest_dir = tmp_path / "dest"

        with patch(
            "crsbench.utils.repo_manager.ensure_project_repository"
        ) as mock_ensure:
            mock_ensure.return_value = str(dest_dir)

            source = load_benchmark_source(tmp_path, dest_dir=dest_dir)

            mock_ensure.assert_called_once()
            assert source.is_bundled is False
            assert source.path == dest_dir

    def test_clone_failure_raises(self, tmp_path: Path) -> None:
        """Test that clone failure raises RuntimeError."""
        dest_dir = tmp_path / "dest"

        with patch(
            "crsbench.utils.repo_manager.ensure_project_repository"
        ) as mock_ensure:
            mock_ensure.return_value = None  # Simulate failure

            with pytest.raises(RuntimeError, match="Failed to obtain source code"):
                load_benchmark_source(tmp_path, dest_dir=dest_dir)

    def test_passes_mode_and_verbose(self, tmp_path: Path) -> None:
        """Test that mode and verbose are passed to repo_manager."""
        dest_dir = tmp_path / "dest"

        with patch(
            "crsbench.utils.repo_manager.ensure_project_repository"
        ) as mock_ensure:
            mock_ensure.return_value = str(dest_dir)

            load_benchmark_source(
                tmp_path,
                dest_dir=dest_dir,
                mode="delta",
                verbose=True,
            )

            mock_ensure.assert_called_once_with(
                benchmark_dir=str(tmp_path),
                project_dir=str(dest_dir),
                mode="delta",
                verbose=True,
            )


class TestPrepareSourceFromBundle:
    """Tests for prepare_source_from_bundle function.

    Note: Commit structure tests (both modes use 1 commit) are in test_tarball.py
    since that logic is now at packaging time, not runtime.
    """

    @pytest.fixture
    def mock_benchmark(self, tmp_path: Path) -> Path:
        """Create a mock benchmark with pkgs/ tarball.

        The tarball already has the correct commit structure (done at packaging time):
        - Both modes: 1 squashed commit at vulnerable state
        - Delta mode provides ref.diff as hint (not via git history)
        """
        benchmark_path = tmp_path / "test-benchmark"
        benchmark_path.mkdir()

        # Create pkgs/ directory
        pkgs_dir = benchmark_path / "pkgs"
        pkgs_dir.mkdir()

        # Create source directory to be tarred
        source_name = "testsrc"
        source_dir = tmp_path / "source_staging" / source_name
        source_dir.mkdir(parents=True)

        # Add some source files
        (source_dir / "main.c").write_text("int main() { return 0; }\n")
        (source_dir / "README.md").write_text("Test project\n")

        # Initialize git with a single commit (simulating pre-packaged tarball)
        subprocess.run(["git", "init"], cwd=source_dir, capture_output=True, check=True)
        subprocess.run(
            ["git", "add", "."], cwd=source_dir, capture_output=True, check=True
        )
        subprocess.run(
            ["git", "commit", "-m", "Initial source"],
            cwd=source_dir,
            capture_output=True,
            check=True,
            env={
                "GIT_AUTHOR_NAME": "CRSBench",
                "GIT_AUTHOR_EMAIL": "crsbench@example.com",
                "GIT_COMMITTER_NAME": "CRSBench",
                "GIT_COMMITTER_EMAIL": "crsbench@example.com",
            },
        )

        # Create tarball
        tarball_path = pkgs_dir / f"{source_name}.tar.gz"
        with tarfile.open(tarball_path, "w:gz") as tar:
            tar.add(source_dir, arcname=source_name)

        return benchmark_path

    def test_extracts_tarball_and_returns_path(
        self, mock_benchmark: Path, tmp_path: Path
    ) -> None:
        """Test that prepare_source_from_bundle extracts tarball correctly."""
        dest_dir = tmp_path / "extracted"

        source_path = prepare_source_from_bundle(
            mock_benchmark,
            dest_dir,
            "testsrc",
        )

        assert source_path is not None
        assert source_path.exists()
        assert (source_path / "main.c").exists()
        assert (source_path / "README.md").exists()

    def test_git_repo_exists_after_extract(
        self, mock_benchmark: Path, tmp_path: Path
    ) -> None:
        """Test that extracted source has git repository."""
        dest_dir = tmp_path / "extracted"

        source_path = prepare_source_from_bundle(
            mock_benchmark,
            dest_dir,
            "testsrc",
        )

        assert source_path is not None
        assert (source_path / ".git").exists()

    def test_returns_none_for_missing_tarball(self, tmp_path: Path) -> None:
        """Test that missing tarball returns None."""
        benchmark_path = tmp_path / "benchmark"
        benchmark_path.mkdir()
        (benchmark_path / "pkgs").mkdir()

        source_path = prepare_source_from_bundle(
            benchmark_path,
            tmp_path / "dest",
            "nonexistent",
        )

        assert source_path is None

    def test_crsbench_author_in_commits(
        self, mock_benchmark: Path, tmp_path: Path
    ) -> None:
        """Test that commits show CRSBench as author (from packaging)."""
        dest_dir = tmp_path / "extracted"

        source_path = prepare_source_from_bundle(
            mock_benchmark,
            dest_dir,
            "testsrc",
        )

        assert source_path is not None

        # Get author info
        result = subprocess.run(
            ["git", "log", "--format=%an <%ae>"],
            cwd=source_path,
            capture_output=True,
            text=True,
            check=True,
        )
        authors = [
            line.strip() for line in result.stdout.strip().split("\n") if line.strip()
        ]

        # All authors should be CRSBench
        for author in authors:
            assert "CRSBench" in author or "crsbench" in author.lower(), (
                f"Expected CRSBench author, got: {author}"
            )
