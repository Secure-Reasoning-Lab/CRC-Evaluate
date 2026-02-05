"""Tests for benchmark utility functions."""

import tempfile
from pathlib import Path

import pytest
import yaml
from crsbench.utils.benchmark_utils import (
    filter_benchmarks_by_mode,
    get_available_modes_for_benchmark,
)


@pytest.fixture
def temp_benchmarks_dir():
    """Create a temporary directory with mock benchmarks."""
    with tempfile.TemporaryDirectory() as tmpdir:
        benchmarks_root = Path(tmpdir)

        # Create delta-only benchmark
        delta_only = benchmarks_root / "project-delta-01"
        (delta_only / ".aixcc").mkdir(parents=True)
        with (delta_only / ".aixcc" / "meta.yaml").open("w") as f:
            yaml.dump({"delta_mode": True, "full_mode": False}, f)

        # Create full-only benchmark
        full_only = benchmarks_root / "project-full-01"
        (full_only / ".aixcc").mkdir(parents=True)
        with (full_only / ".aixcc" / "meta.yaml").open("w") as f:
            yaml.dump({"delta_mode": False, "full_mode": True}, f)

        # Create benchmark with both modes
        both_modes = benchmarks_root / "project-both-01"
        (both_modes / ".aixcc").mkdir(parents=True)
        with (both_modes / ".aixcc" / "meta.yaml").open("w") as f:
            yaml.dump({"delta_mode": True, "full_mode": True}, f)

        # Create benchmark with no modes (edge case)
        no_modes = benchmarks_root / "project-empty-01"
        (no_modes / ".aixcc").mkdir(parents=True)
        with (no_modes / ".aixcc" / "meta.yaml").open("w") as f:
            yaml.dump({"delta_mode": False, "full_mode": False}, f)

        yield benchmarks_root


class TestGetAvailableModesForBenchmark:
    """Tests for get_available_modes_for_benchmark function."""

    def test_delta_only(self, temp_benchmarks_dir):
        """Test benchmark with only delta mode."""
        benchmark_path = temp_benchmarks_dir / "project-delta-01"
        modes = get_available_modes_for_benchmark(benchmark_path)
        assert modes == ["delta"]

    def test_full_only(self, temp_benchmarks_dir):
        """Test benchmark with only full mode."""
        benchmark_path = temp_benchmarks_dir / "project-full-01"
        modes = get_available_modes_for_benchmark(benchmark_path)
        assert modes == ["full"]

    def test_both_modes(self, temp_benchmarks_dir):
        """Test benchmark with both modes."""
        benchmark_path = temp_benchmarks_dir / "project-both-01"
        modes = get_available_modes_for_benchmark(benchmark_path)
        assert "delta" in modes
        assert "full" in modes

    def test_no_modes(self, temp_benchmarks_dir):
        """Test benchmark with no modes enabled."""
        benchmark_path = temp_benchmarks_dir / "project-empty-01"
        modes = get_available_modes_for_benchmark(benchmark_path)
        assert modes == []

    def test_missing_meta_yaml(self, temp_benchmarks_dir):
        """Test benchmark with missing meta.yaml."""
        benchmark_path = temp_benchmarks_dir / "nonexistent-project"
        modes = get_available_modes_for_benchmark(benchmark_path)
        assert modes == []


class TestFilterBenchmarksByMode:
    """Tests for filter_benchmarks_by_mode function."""

    def test_filter_delta_mode(self, temp_benchmarks_dir):
        """Test filtering to delta-only benchmarks."""
        benchmark_names = [
            "project-delta-01",
            "project-full-01",
            "project-both-01",
            "project-empty-01",
        ]
        filtered = filter_benchmarks_by_mode(
            benchmark_names, "delta", temp_benchmarks_dir
        )
        assert "project-delta-01" in filtered
        assert "project-both-01" in filtered
        assert "project-full-01" not in filtered
        assert "project-empty-01" not in filtered
        assert len(filtered) == 2

    def test_filter_full_mode(self, temp_benchmarks_dir):
        """Test filtering to full-only benchmarks."""
        benchmark_names = [
            "project-delta-01",
            "project-full-01",
            "project-both-01",
            "project-empty-01",
        ]
        filtered = filter_benchmarks_by_mode(
            benchmark_names, "full", temp_benchmarks_dir
        )
        assert "project-full-01" in filtered
        assert "project-both-01" in filtered
        assert "project-delta-01" not in filtered
        assert "project-empty-01" not in filtered
        assert len(filtered) == 2

    def test_filter_all_mode(self, temp_benchmarks_dir):
        """Test that 'all' mode returns all benchmarks unchanged."""
        benchmark_names = [
            "project-delta-01",
            "project-full-01",
            "project-both-01",
            "project-empty-01",
        ]
        filtered = filter_benchmarks_by_mode(
            benchmark_names, "all", temp_benchmarks_dir
        )
        assert filtered == benchmark_names

    def test_empty_list(self, temp_benchmarks_dir):
        """Test filtering an empty list."""
        filtered = filter_benchmarks_by_mode([], "delta", temp_benchmarks_dir)
        assert filtered == []

    def test_nonexistent_benchmarks(self, temp_benchmarks_dir):
        """Test filtering with nonexistent benchmark names."""
        benchmark_names = ["nonexistent-1", "nonexistent-2"]
        filtered = filter_benchmarks_by_mode(
            benchmark_names, "delta", temp_benchmarks_dir
        )
        assert filtered == []
