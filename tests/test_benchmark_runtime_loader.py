"""Tests for benchmark runtime loader."""

from pathlib import Path
from unittest.mock import patch

import pytest
from crsbench.benchmark.runtime.loader import (
    _reassemble_split_tarball,
    get_bundled_tarball_path,
    has_bundled_source,
    load_benchmark_source,
    prepare_source_from_bundle,
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
        - Both modes: 1 squashed commit at vulnerable state
        - Delta mode provides ref.diff as hint (not via git history)

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


class TestHasBundledSourceSplitTarballs:
    """Tests for has_bundled_source() with split tarballs."""

    def test_returns_true_for_split_parts_only(self, tmp_path: Path) -> None:
        """Returns True when pkgs/ has only .part* files (no whole tarball)."""
        pkgs_dir = tmp_path / "pkgs"
        pkgs_dir.mkdir()
        (pkgs_dir / "tika.tar.gz.partaa").touch()
        (pkgs_dir / "tika.tar.gz.partab").touch()

        assert has_bundled_source(tmp_path)

    def test_returns_false_for_non_tarball_parts(self, tmp_path: Path) -> None:
        """Returns False when pkgs/ has .part* but no .partaa (first part)."""
        pkgs_dir = tmp_path / "pkgs"
        pkgs_dir.mkdir()
        # Only partab, no partaa → not a valid split tarball
        (pkgs_dir / "tika.tar.gz.partab").touch()

        assert not has_bundled_source(tmp_path)


class TestGetBundledTarballPathSplit:
    """Tests for get_bundled_tarball_path() with split tarballs."""

    def _make_benchmark(self, tmp_path: Path, source_name: str) -> Path:
        """Create benchmark with Dockerfile WORKDIR and split parts."""
        pkgs_dir = tmp_path / "pkgs"
        pkgs_dir.mkdir()
        (pkgs_dir / f"{source_name}.tar.gz.partaa").write_bytes(b"a" * 100)
        (pkgs_dir / f"{source_name}.tar.gz.partab").write_bytes(b"b" * 100)

        dockerfile = tmp_path / "Dockerfile"
        dockerfile.write_text(f"FROM base\nWORKDIR $SRC/{source_name}\n")
        return tmp_path

    def test_returns_partaa_when_no_whole_tarball(self, tmp_path: Path) -> None:
        """Should return .partaa path when whole tarball doesn't exist."""
        bench = self._make_benchmark(tmp_path, "tika")
        result = get_bundled_tarball_path(bench)

        assert result is not None
        assert result.name == "tika.tar.gz.partaa"

    def test_prefers_whole_tarball_over_parts(self, tmp_path: Path) -> None:
        """Should return whole tarball when both exist."""
        bench = self._make_benchmark(tmp_path, "tika")
        (bench / "pkgs" / "tika.tar.gz").write_bytes(b"whole")
        result = get_bundled_tarball_path(bench)

        assert result is not None
        assert result.name == "tika.tar.gz"


class TestLoadBenchmarkSourceSplitTarball:
    """Tests for load_benchmark_source() with split tarballs (source_name bug fix)."""

    def test_pkgs_mode_split_tarball_extracts_correct_source_name(
        self, tmp_path: Path
    ) -> None:
        """When only .part* files exist, source_name should be 'tika' not 'tika.tar.gz'."""
        pkgs_dir = tmp_path / "pkgs"
        pkgs_dir.mkdir()
        (pkgs_dir / "tika.tar.gz.partaa").write_bytes(b"a" * 100)
        (pkgs_dir / "tika.tar.gz.partab").write_bytes(b"b" * 100)

        dockerfile = tmp_path / "Dockerfile"
        dockerfile.write_text("FROM base\nWORKDIR $SRC/tika\n")

        dest_dir = tmp_path / "dest"

        with patch(
            "crsbench.benchmark.runtime.loader.prepare_source_from_bundle"
        ) as mock_prepare:
            mock_prepare.return_value = dest_dir / "tika"

            source = load_benchmark_source(
                tmp_path, dest_dir=dest_dir, source_mode="pkgs"
            )

            assert source.is_bundled
            # The critical assertion: source_name passed should be "tika", not "tika.tar.gz"
            call_args = mock_prepare.call_args
            assert call_args[0][2] == "tika"

    def test_pkgs_mode_whole_tarball_source_name(self, tmp_path: Path) -> None:
        """When whole tarball exists, source_name should be extracted correctly."""
        pkgs_dir = tmp_path / "pkgs"
        pkgs_dir.mkdir()
        (pkgs_dir / "curl.tar.gz").touch()

        dockerfile = tmp_path / "Dockerfile"
        dockerfile.write_text("FROM base\nWORKDIR $SRC/curl\n")

        dest_dir = tmp_path / "dest"

        with patch(
            "crsbench.benchmark.runtime.loader.prepare_source_from_bundle"
        ) as mock_prepare:
            mock_prepare.return_value = dest_dir / "curl"

            load_benchmark_source(tmp_path, dest_dir=dest_dir, source_mode="pkgs")

            call_args = mock_prepare.call_args
            assert call_args[0][2] == "curl"


class TestReassembleSplitTarball:
    """Tests for _reassemble_split_tarball()."""

    def test_reassembles_parts_in_order(self, tmp_path: Path) -> None:
        """Should concatenate parts alphabetically."""
        (tmp_path / "data.tar.gz.partaa").write_bytes(b"AAAA")
        (tmp_path / "data.tar.gz.partab").write_bytes(b"BBBB")
        (tmp_path / "data.tar.gz.partac").write_bytes(b"CCCC")

        result = _reassemble_split_tarball(tmp_path, "data")

        assert result is not None
        assert result.exists()
        content = result.read_bytes()
        assert content == b"AAAA" + b"BBBB" + b"CCCC"
        # Cleanup temp file
        result.unlink()

    def test_returns_none_when_no_parts(self, tmp_path: Path) -> None:
        """Should return None when no matching parts found."""
        result = _reassemble_split_tarball(tmp_path, "nonexistent")
        assert result is None


class TestPrepareSourceSplitTarball:
    """Tests for prepare_source_from_bundle() with split tarballs."""

    def test_reassembles_and_extracts(self, tmp_path: Path) -> None:
        """Should reassemble split parts and extract source directory."""
        import io
        import tarfile

        # Create a real gzipped tarball split into parts
        source_name = "mylib"
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            # Add a file inside mylib/ directory
            info = tarfile.TarInfo(name=f"{source_name}/main.c")
            data = b"int main() { return 0; }"
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))

        tarball_bytes = buf.getvalue()

        # Split into 2 parts
        pkgs_dir = tmp_path / "benchmark" / "pkgs"
        pkgs_dir.mkdir(parents=True)
        mid = len(tarball_bytes) // 2
        (pkgs_dir / f"{source_name}.tar.gz.partaa").write_bytes(tarball_bytes[:mid])
        (pkgs_dir / f"{source_name}.tar.gz.partab").write_bytes(tarball_bytes[mid:])

        dest_dir = tmp_path / "dest"
        result = prepare_source_from_bundle(
            tmp_path / "benchmark", dest_dir, source_name
        )

        assert result is not None
        assert result.exists()
        assert (result / "main.c").exists()
        assert (result / "main.c").read_bytes() == b"int main() { return 0; }"


class TestLoadBenchmarkSourceInvalidMode:
    """Tests for invalid source_mode."""

    def test_invalid_source_mode_raises(self, tmp_path: Path) -> None:
        """Invalid source_mode should raise ValueError."""
        dest_dir = tmp_path / "dest"

        with pytest.raises(ValueError, match="Invalid source_mode"):
            load_benchmark_source(tmp_path, dest_dir=dest_dir, source_mode="invalid")
