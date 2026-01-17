"""Tests for crsbench.benchmark.runtime module."""

from pathlib import Path
from unittest.mock import patch

import pytest
from crsbench.benchmark.runtime.loader import has_bundled_source, load_benchmark_source
from crsbench.benchmark.runtime.models import BenchmarkSource


class TestBenchmarkSource:
    """Tests for BenchmarkSource dataclass."""

    def test_bundled_source(self) -> None:
        """Test bundled source has path=None."""
        source = BenchmarkSource(path=None, is_bundled=True)
        assert source.path is None
        assert source.is_bundled is True
        assert source.requires_source_path is False

    def test_cloned_source(self, tmp_path: Path) -> None:
        """Test cloned source has path set."""
        source = BenchmarkSource(path=tmp_path, is_bundled=False)
        assert source.path == tmp_path
        assert source.is_bundled is False
        assert source.requires_source_path is True

    def test_invalid_bundled_with_path_raises(self, tmp_path: Path) -> None:
        """Test that bundled=True with path raises error."""
        with pytest.raises(ValueError, match="Bundled source should have path=None"):
            BenchmarkSource(path=tmp_path, is_bundled=True)

    def test_requires_source_path_false_when_no_path(self) -> None:
        """Test requires_source_path is False when path is None."""
        source = BenchmarkSource(path=None, is_bundled=False)
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

    def test_bundled_source_returns_none_path(self, tmp_path: Path) -> None:
        """Test that bundled source returns BenchmarkSource with path=None."""
        # Create pkgs/ with tarball
        pkgs = tmp_path / "pkgs"
        pkgs.mkdir()
        (pkgs / "source.tar.gz").write_text("dummy")

        source = load_benchmark_source(tmp_path)

        assert source.is_bundled is True
        assert source.path is None
        assert source.requires_source_path is False

    def test_no_bundled_no_dest_raises(self, tmp_path: Path) -> None:
        """Test that missing pkgs/ and no dest_dir raises error."""
        with pytest.raises(RuntimeError, match="no dest_dir provided"):
            load_benchmark_source(tmp_path)

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
