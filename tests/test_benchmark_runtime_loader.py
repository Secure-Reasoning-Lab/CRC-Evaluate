"""Tests for benchmark runtime loader."""

from pathlib import Path
from unittest.mock import patch

import pytest
from crsbench.benchmark.runtime.loader import (
    has_bundled_source,
    load_benchmark_source,
)


class TestHasBundledSource:
    """Tests for has_bundled_source()."""

    def test_returns_true_when_pkgs_with_tarball(self, tmp_path: Path) -> None:
        """Returns True when pkgs/ contains tarballs."""
        pkgs_dir = tmp_path / "pkgs"
        pkgs_dir.mkdir()
        (pkgs_dir / "mock-c.tar.gz").touch()

        assert has_bundled_source(tmp_path)

    def test_returns_false_when_no_pkgs(self, tmp_path: Path) -> None:
        """Returns False when pkgs/ doesn't exist."""
        assert not has_bundled_source(tmp_path)

    def test_returns_false_when_pkgs_empty(self, tmp_path: Path) -> None:
        """Returns False when pkgs/ is empty."""
        pkgs_dir = tmp_path / "pkgs"
        pkgs_dir.mkdir()

        assert not has_bundled_source(tmp_path)


class TestLoadBenchmarkSourceMainRepo:
    """Tests for load_benchmark_source() with source_mode='main_repo'."""

    def test_main_repo_clones_from_git(self, tmp_path: Path) -> None:
        """source_mode='main_repo' should clone from main_repo."""
        (tmp_path / "project.yaml").write_text(
            "main_repo: https://github.com/example/repo.git\n"
        )
        aixcc = tmp_path / ".aixcc"
        aixcc.mkdir()
        (aixcc / "meta.yaml").write_text("full_mode:\n  base_commit: abc123\n")

        dest_dir = tmp_path / "dest"

        with patch(
            "crsbench.utils.repo_manager.ensure_project_repository"
        ) as mock_ensure:
            mock_ensure.return_value = str(dest_dir / "repo")

            source = load_benchmark_source(
                tmp_path, dest_dir=dest_dir, source_mode="main_repo"
            )

            assert not source.is_bundled
            assert source.path is not None
            mock_ensure.assert_called_once()

    def test_main_repo_ignores_pkgs(self, tmp_path: Path) -> None:
        """source_mode='main_repo' should ignore pkgs/ even if present."""
        # Create pkgs/ with tarball (should be ignored)
        pkgs_dir = tmp_path / "pkgs"
        pkgs_dir.mkdir()
        (pkgs_dir / "mock-c.tar.gz").touch()

        # Create minimal benchmark structure
        (tmp_path / "project.yaml").write_text(
            "main_repo: https://github.com/example/repo.git\n"
        )
        aixcc = tmp_path / ".aixcc"
        aixcc.mkdir()
        (aixcc / "meta.yaml").write_text("full_mode:\n  base_commit: abc123\n")

        dest_dir = tmp_path / "dest"

        with patch(
            "crsbench.utils.repo_manager.ensure_project_repository"
        ) as mock_ensure:
            mock_ensure.return_value = str(dest_dir / "repo")

            source = load_benchmark_source(
                tmp_path, dest_dir=dest_dir, source_mode="main_repo"
            )

            # Should clone, not use bundled
            assert not source.is_bundled
            mock_ensure.assert_called_once()

    def test_delta_mode_passes_mode_to_repo_manager(self, tmp_path: Path) -> None:
        """Delta mode should pass mode='delta' to repo_manager."""
        (tmp_path / "project.yaml").write_text(
            "main_repo: https://github.com/example/repo.git\n"
        )
        aixcc = tmp_path / ".aixcc"
        aixcc.mkdir()
        (aixcc / "meta.yaml").write_text(
            "delta_mode:\n  base_commit: abc123\n  ref_commit: def456\n"
        )

        dest_dir = tmp_path / "dest"

        with patch(
            "crsbench.utils.repo_manager.ensure_project_repository"
        ) as mock_ensure:
            mock_ensure.return_value = str(dest_dir / "repo")

            source = load_benchmark_source(
                tmp_path, dest_dir=dest_dir, source_mode="main_repo", mode="delta"
            )

            assert not source.is_bundled
            call_kwargs = mock_ensure.call_args[1]
            assert call_kwargs.get("mode") == "delta"


class TestLoadBenchmarkSourcePkgs:
    """Tests for load_benchmark_source() with source_mode='pkgs'."""

    def test_pkgs_mode_requires_pkgs_dir(self, tmp_path: Path) -> None:
        """source_mode='pkgs' should error if no pkgs/ exists."""
        dest_dir = tmp_path / "dest"

        with pytest.raises(RuntimeError, match="No bundled source"):
            load_benchmark_source(tmp_path, dest_dir=dest_dir, source_mode="pkgs")

    def test_pkgs_mode_extracts_tarball(self, tmp_path: Path) -> None:
        """source_mode='pkgs' should extract tarball and return path."""
        # Create pkgs/ with tarball
        pkgs_dir = tmp_path / "pkgs"
        pkgs_dir.mkdir()
        (pkgs_dir / "mock-c.tar.gz").touch()

        dest_dir = tmp_path / "dest"

        with patch(
            "crsbench.benchmark.runtime.loader.prepare_source_from_bundle"
        ) as mock_prepare:
            mock_prepare.return_value = dest_dir / "mock-c"

            source = load_benchmark_source(
                tmp_path, dest_dir=dest_dir, source_mode="pkgs"
            )

            assert source.is_bundled
            assert source.path == dest_dir / "mock-c"
            mock_prepare.assert_called_once()

    def test_pkgs_mode_ignores_mode_parameter(self, tmp_path: Path) -> None:
        """source_mode='pkgs' ignores mode - commit structure is from packaging.

        The tarball already has the correct commit structure:
        - Delta mode: 2 commits (base → ref) at ref_commit state
        - Full mode: 1 squashed commit at vulnerable state

        No runtime post-processing needed.
        """
        pkgs_dir = tmp_path / "pkgs"
        pkgs_dir.mkdir()
        (pkgs_dir / "mock-c.tar.gz").touch()

        dest_dir = tmp_path / "dest"

        with patch(
            "crsbench.benchmark.runtime.loader.prepare_source_from_bundle"
        ) as mock_prepare:
            mock_prepare.return_value = dest_dir / "mock-c"

            # Call with mode="delta" - should be ignored
            load_benchmark_source(
                tmp_path, dest_dir=dest_dir, source_mode="pkgs", mode="delta"
            )

            # Verify no apply_ref_diff or squash_history params
            call_args, call_kwargs = mock_prepare.call_args
            assert "apply_ref_diff" not in call_kwargs
            assert "squash_history" not in call_kwargs

            mock_prepare.reset_mock()

            # Call with mode="full" - should also be ignored
            load_benchmark_source(
                tmp_path, dest_dir=dest_dir, source_mode="pkgs", mode="full"
            )

            call_args, call_kwargs = mock_prepare.call_args
            assert "apply_ref_diff" not in call_kwargs
            assert "squash_history" not in call_kwargs


class TestLoadBenchmarkSourceInvalidMode:
    """Tests for invalid source_mode."""

    def test_invalid_source_mode_raises(self, tmp_path: Path) -> None:
        """Invalid source_mode should raise ValueError."""
        dest_dir = tmp_path / "dest"

        with pytest.raises(ValueError, match="Invalid source_mode"):
            load_benchmark_source(tmp_path, dest_dir=dest_dir, source_mode="invalid")
