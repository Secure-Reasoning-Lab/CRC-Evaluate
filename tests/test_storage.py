"""Tests for storage metrics collection and formatting."""

import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from crsbench.benchmark_ci.storage import (
    StorageMetrics,
    collect_benchmark_storage,
    format_storage_size,
    measure_build_artifacts_size,
    measure_directory_size,
    measure_docker_image_size,
    measure_git_size,
)


class TestFormatStorageSize:
    """Tests for format_storage_size function."""

    def test_format_zero(self):
        """Test formatting zero bytes."""
        assert format_storage_size(0) == "0 B"

    def test_format_negative(self):
        """Test formatting negative bytes returns 0 B."""
        assert format_storage_size(-100) == "0 B"

    def test_format_bytes(self):
        """Test formatting small byte values."""
        result = format_storage_size(500)
        assert "500" in result or "bytes" in result.lower()

    def test_format_kilobytes(self):
        """Test formatting kilobyte values."""
        result = format_storage_size(1024)
        assert "KiB" in result or "KB" in result

    def test_format_megabytes(self):
        """Test formatting megabyte values."""
        result = format_storage_size(1024 * 1024)
        assert "MiB" in result or "MB" in result

    def test_format_gigabytes(self):
        """Test formatting gigabyte values."""
        result = format_storage_size(1024 * 1024 * 1024)
        assert "GiB" in result or "GB" in result

    def test_format_uses_binary_units(self):
        """Test that binary units (powers of 1024) are used."""
        # 1.5 GiB in bytes
        size = int(1.5 * 1024 * 1024 * 1024)
        result = format_storage_size(size)
        assert "GiB" in result


class TestStorageMetrics:
    """Tests for StorageMetrics dataclass."""

    def test_total_bytes_calculation(self):
        """Test total_bytes property calculation."""
        metrics = StorageMetrics(
            build_artifacts_bytes=1000,
            docker_image_bytes=2000,
            git_bytes=500,
        )
        assert metrics.total_bytes == 3500

    def test_total_bytes_all_zero(self):
        """Test total_bytes with all zeros."""
        metrics = StorageMetrics()
        assert metrics.total_bytes == 0

    def test_format_total(self):
        """Test format_total method."""
        metrics = StorageMetrics(
            build_artifacts_bytes=1024 * 1024 * 100,  # 100 MiB
            docker_image_bytes=1024 * 1024 * 200,  # 200 MiB
            git_bytes=1024 * 1024 * 50,  # 50 MiB
        )
        result = metrics.format_total()
        assert "MiB" in result


class TestMeasureDirectorySize:
    """Tests for measure_directory_size function."""

    def test_nonexistent_directory(self):
        """Test measuring non-existent directory returns 0."""
        result = measure_directory_size(Path("/nonexistent/path/12345"))
        assert result == 0

    def test_existing_directory(self):
        """Test measuring an existing directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create some files
            test_file = Path(tmpdir) / "test.txt"
            test_file.write_text("Hello World" * 100)

            result = measure_directory_size(Path(tmpdir))
            assert result > 0

    def test_empty_directory(self):
        """Test measuring an empty directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = measure_directory_size(Path(tmpdir))
            # Empty dir still has some metadata size (typically 4KB block)
            assert result >= 0


class TestMeasureBuildArtifactsSize:
    """Tests for measure_build_artifacts_size function."""

    def test_nonexistent_path(self):
        """Test with non-existent oss-fuzz path."""
        result = measure_build_artifacts_size(
            Path("/nonexistent/oss-fuzz"),
            "test-variant",
        )
        assert result == 0

    def test_existing_path(self):
        """Test with existing build artifacts."""
        with tempfile.TemporaryDirectory() as tmpdir:
            oss_fuzz = Path(tmpdir)
            build_out = oss_fuzz / "build" / "out" / "test-address"
            build_out.mkdir(parents=True)
            (build_out / "artifact.bin").write_bytes(b"x" * 1000)

            result = measure_build_artifacts_size(oss_fuzz, "test-address")
            assert result > 0


class TestMeasureDockerImageSize:
    """Tests for measure_docker_image_size function."""

    def test_nonexistent_image(self):
        """Test with non-existent Docker image."""
        result = measure_docker_image_size("nonexistent-image-12345:latest")
        assert result == 0

    @patch("subprocess.run")
    def test_docker_error_returns_zero(self, mock_run):
        """Test that Docker errors return 0."""
        mock_run.side_effect = subprocess.CalledProcessError(1, "docker")
        result = measure_docker_image_size("test-image:latest")
        assert result == 0

    @patch("subprocess.run")
    def test_docker_timeout_returns_zero(self, mock_run):
        """Test that Docker timeout returns 0."""
        mock_run.side_effect = subprocess.TimeoutExpired("docker", 10)
        result = measure_docker_image_size("test-image:latest")
        assert result == 0

    @patch("subprocess.run")
    def test_docker_success(self, mock_run):
        """Test successful Docker image size query."""
        mock_run.return_value = MagicMock(stdout="123456789\n")
        result = measure_docker_image_size("test-image:latest")
        assert result == 123456789


class TestMeasureGitSize:
    """Tests for measure_git_size function."""

    def test_nonexistent_git_dir(self):
        """Test with non-existent .git directory."""
        result = measure_git_size(Path("/nonexistent/path"))
        assert result == 0

    def test_existing_git_dir(self):
        """Test with existing .git directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            git_dir = Path(tmpdir) / ".git"
            git_dir.mkdir()
            (git_dir / "config").write_text("[core]")

            result = measure_git_size(Path(tmpdir))
            assert result > 0


class TestCollectBenchmarkStorage:
    """Tests for collect_benchmark_storage function."""

    def test_returns_storage_metrics(self):
        """Test that function returns StorageMetrics."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = collect_benchmark_storage(
                benchmark_name="test",
                benchmark_path=Path(tmpdir),
                oss_fuzz_path=Path(tmpdir),
            )
            assert isinstance(result, StorageMetrics)

    def test_collects_all_components(self):
        """Test that all storage components are collected."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)

            # Create build artifacts
            build_out = tmpdir_path / "build" / "out" / "test-address"
            build_out.mkdir(parents=True)
            (build_out / "artifact").write_bytes(b"x" * 1000)

            # Create git dir
            git_dir = tmpdir_path / ".git"
            git_dir.mkdir()
            (git_dir / "config").write_text("[core]")

            result = collect_benchmark_storage(
                benchmark_name="test",
                benchmark_path=tmpdir_path,
                oss_fuzz_path=tmpdir_path,
            )

            assert result.build_artifacts_bytes > 0
            assert result.git_bytes > 0
            # Docker will be 0 since no real image exists
            assert result.docker_image_bytes == 0


class TestOutputTableStorageColumn:
    """Tests for storage column in output table."""

    def test_storage_in_benchmark_validation_result(self):
        """Test that BenchmarkValidationResult has storage_bytes field."""
        from crsbench.benchmark_ci.models import BenchmarkValidationResult

        result = BenchmarkValidationResult(
            benchmark="test",
            benchmark_path=Path("/tmp/test"),
            storage_bytes=1024 * 1024 * 500,  # 500 MiB
        )
        assert result.storage_bytes == 1024 * 1024 * 500

    def test_storage_in_to_dict(self):
        """Test that storage_bytes is included in to_dict()."""
        from crsbench.benchmark_ci.models import BenchmarkValidationResult

        result = BenchmarkValidationResult(
            benchmark="test",
            benchmark_path=Path("/tmp/test"),
            storage_bytes=12345,
        )
        as_dict = result.to_dict()
        assert "storage_bytes" in as_dict
        assert as_dict["storage_bytes"] == 12345

    def test_format_storage_helper(self):
        """Test _format_storage helper in output module."""
        from crsbench.benchmark_ci.cli.output import _format_storage
        from crsbench.benchmark_ci.models import BenchmarkValidationResult

        result = BenchmarkValidationResult(
            benchmark="test",
            benchmark_path=Path("/tmp/test"),
            storage_bytes=1024 * 1024 * 100,  # 100 MiB
        )
        formatted = _format_storage(result)
        assert "MiB" in formatted or "100" in formatted

    def test_format_storage_zero(self):
        """Test _format_storage with zero storage."""
        from crsbench.benchmark_ci.cli.output import _format_storage
        from crsbench.benchmark_ci.models import BenchmarkValidationResult

        result = BenchmarkValidationResult(
            benchmark="test",
            benchmark_path=Path("/tmp/test"),
            storage_bytes=0,
        )
        formatted = _format_storage(result)
        assert "-" in formatted  # Should show "-" for no storage
