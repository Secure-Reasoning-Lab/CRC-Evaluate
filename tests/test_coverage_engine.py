"""Tests for CoverageEngine.

This module tests the CoverageEngine class which provides a unified
interface for coverage collection, following the same patterns as
VerificationEngine (POV) and PatchVerificationEngine.
"""

from pathlib import Path
from typing import Generator
from unittest.mock import MagicMock, patch

import pytest
import yaml
from crsbench.evaluation.coverage.engine import CoverageEngine

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_oss_fuzz(tmp_path: Path) -> Path:
    """Create a mock OSS-Fuzz directory structure."""
    oss_fuzz = tmp_path / "oss-fuzz"
    (oss_fuzz / "infra").mkdir(parents=True)
    (oss_fuzz / "infra" / "helper.py").touch()
    (oss_fuzz / "projects").mkdir()
    (oss_fuzz / "build" / "out").mkdir(parents=True)
    return oss_fuzz


@pytest.fixture
def mock_benchmark(tmp_path: Path) -> Path:
    """Create a mock benchmark with valid meta.yaml and project.yaml."""
    benchmark = tmp_path / "test-benchmark"
    benchmark.mkdir()
    aixcc_dir = benchmark / ".aixcc"
    aixcc_dir.mkdir()

    meta = {
        "delta_mode": {
            "ref_commit": "a" * 40,
            "base_commit": "b" * 40,
        },
        "harness_files": [
            {
                "name": "fuzz_target",
                "path": "/src/fuzz/fuzz_target.c",
                "vulns": [
                    {
                        "vuln_keyword": "cpv_0",
                        "povs": [
                            {
                                "id": "pov_0",
                                "name": "pov_0",
                                "path": "cpv_0/pov_0",
                                "sanitizer": "address",
                            }
                        ],
                    }
                ],
            }
        ],
    }
    (aixcc_dir / "meta.yaml").write_text(yaml.dump(meta))

    project = {"language": "c", "main_repo": "https://example.com/repo"}
    (benchmark / "project.yaml").write_text(yaml.dump(project))

    return benchmark


@pytest.fixture
def mock_corpus(tmp_path: Path) -> Path:
    """Create a mock corpus directory with sample files."""
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    for i in range(3):
        (corpus / f"input_{i}").write_bytes(f"content{i}".encode())
    return corpus


@pytest.fixture
def coverage_engine(mock_oss_fuzz: Path) -> Generator[CoverageEngine, None, None]:
    """Create a CoverageEngine instance with cleanup."""
    engine = CoverageEngine(mock_oss_fuzz)
    yield engine
    engine.cleanup()


# =============================================================================
# Tests
# =============================================================================


class TestCoverageEngineInit:
    """Tests for CoverageEngine initialization."""

    def test_init_with_defaults(self, mock_oss_fuzz: Path):
        """Test CoverageEngine with default worker counts."""
        engine = CoverageEngine(mock_oss_fuzz)

        assert engine.oss_fuzz_path == mock_oss_fuzz
        assert engine.build_workers >= 1
        assert engine.verify_workers >= 1
        assert engine.work_dir is None
        assert len(engine._strategies) == 0

    def test_init_with_custom_workers(self, mock_oss_fuzz: Path):
        """Test CoverageEngine with custom worker counts."""
        engine = CoverageEngine(
            mock_oss_fuzz,
            build_workers=8,
            verify_workers=16,
        )

        assert engine.build_workers == 8
        assert engine.verify_workers == 16

    def test_init_with_work_dir(self, mock_oss_fuzz: Path, tmp_path: Path):
        """Test CoverageEngine with custom work directory."""
        work_dir = tmp_path / "work"
        engine = CoverageEngine(mock_oss_fuzz, work_dir=work_dir)

        assert engine.work_dir == work_dir


class TestCoverageEngineLoadAdapter:
    """Tests for CoverageEngine._load_adapter()."""

    def test_load_adapter_success(
        self, mock_benchmark: Path, coverage_engine: CoverageEngine
    ):
        """Test loading adapter from valid benchmark."""
        adapter = coverage_engine._load_adapter(mock_benchmark)

        assert adapter is not None
        assert adapter.benchmark_name == "test-benchmark"
        assert adapter.lang == "c"

    def test_load_adapter_missing_meta(
        self, mock_oss_fuzz: Path, tmp_path: Path, coverage_engine: CoverageEngine
    ):
        """Test loading adapter with missing meta.yaml."""
        benchmark = tmp_path / "missing-meta"
        benchmark.mkdir()
        (benchmark / ".aixcc").mkdir()
        # No meta.yaml

        adapter = coverage_engine._load_adapter(benchmark)
        assert adapter is None


class TestCoverageEngineMergeCoverage:
    """Tests for CoverageEngine._merge_coverage_safe()."""

    def test_merge_empty_to_empty(self, coverage_engine: CoverageEngine):
        """Test merging empty coverage data."""
        merged: dict = {}
        coverage_engine._merge_coverage_safe(merged, {})

        assert merged == {}

    def test_merge_new_function(self, coverage_engine: CoverageEngine):
        """Test merging new function into empty merged dict."""
        merged: dict = {}
        new_data = {"main": {"src": "main.c", "lines": [1, 2, 3]}}

        coverage_engine._merge_coverage_safe(merged, new_data)

        assert "main" in merged
        assert merged["main"]["src"] == "main.c"
        assert merged["main"]["lines"] == {1, 2, 3}

    def test_merge_overlapping_lines(self, coverage_engine: CoverageEngine):
        """Test merging overlapping line coverage."""
        merged = {"main": {"src": "main.c", "lines": {1, 2, 3}}}
        new_data = {"main": {"src": "main.c", "lines": [3, 4, 5]}}

        coverage_engine._merge_coverage_safe(merged, new_data)

        assert merged["main"]["lines"] == {1, 2, 3, 4, 5}

    def test_merge_multiple_functions(self, coverage_engine: CoverageEngine):
        """Test merging multiple functions."""
        merged: dict = {}

        coverage_engine._merge_coverage_safe(
            merged, {"func1": {"src": "a.c", "lines": [1, 2]}}
        )
        coverage_engine._merge_coverage_safe(
            merged, {"func2": {"src": "b.c", "lines": [10, 11]}}
        )

        assert "func1" in merged
        assert "func2" in merged
        assert merged["func1"]["lines"] == {1, 2}
        assert merged["func2"]["lines"] == {10, 11}


class TestCoverageEngineComputeSummary:
    """Tests for CoverageEngine._compute_summary()."""

    def test_compute_summary_empty(self, coverage_engine: CoverageEngine):
        """Test computing summary from empty coverage."""
        summary = coverage_engine._compute_summary({}, corpus_count=0, success_count=0)

        assert summary.lines_covered == 0
        assert summary.functions_covered == 0
        assert summary.corpus_total == 0
        assert summary.corpus_contributing == 0

    def test_compute_summary_with_coverage(self, coverage_engine: CoverageEngine):
        """Test computing summary from coverage data."""
        merged = {
            "func1": {"src": "a.c", "lines": {1, 2, 3}},
            "func2": {"src": "b.c", "lines": {10, 11}},
        }
        summary = coverage_engine._compute_summary(
            merged, corpus_count=10, success_count=8
        )

        assert summary.lines_covered == 5
        assert summary.functions_covered == 2
        assert summary.corpus_total == 10
        assert summary.corpus_contributing == 8

    def test_compute_summary_converts_sets_to_lists(
        self, coverage_engine: CoverageEngine
    ):
        """Test that compute_summary converts sets to sorted lists."""
        merged = {"main": {"src": "main.c", "lines": {5, 1, 3, 2, 4}}}

        coverage_engine._compute_summary(merged, corpus_count=1, success_count=1)

        # Sets should be converted to sorted lists
        assert merged["main"]["lines"] == [1, 2, 3, 4, 5]


class TestCoverageEngineCleanup:
    """Tests for CoverageEngine.cleanup()."""

    def test_cleanup_clears_strategies(self, mock_oss_fuzz: Path):
        """Test that cleanup clears strategy cache."""
        engine = CoverageEngine(mock_oss_fuzz)
        engine._strategies["test"] = MagicMock()

        engine.cleanup()

        assert len(engine._strategies) == 0


class TestCoverageEngineCollectCoverage:
    """Tests for CoverageEngine.collect_coverage()."""

    def test_collect_coverage_missing_benchmark(self, coverage_engine: CoverageEngine):
        """Test collect_coverage with missing benchmark path."""
        report = coverage_engine.collect_coverage(
            benchmark_path=Path("/nonexistent"),
            corpus_dir=Path("/tmp"),
        )

        # Should return empty report on failure
        assert report.harness_name == ""

    def test_collect_coverage_empty_corpus(
        self,
        mock_oss_fuzz: Path,
        mock_benchmark: Path,
        tmp_path: Path,
    ):
        """Test collect_coverage with empty corpus directory."""
        # Empty corpus
        corpus = tmp_path / "empty_corpus"
        corpus.mkdir()

        engine = CoverageEngine(mock_oss_fuzz)

        # Mock the build to succeed
        with patch.object(engine.builder, "build_single") as mock_build:
            mock_result = MagicMock()
            mock_result.success = True
            mock_result.variant_name = "test-benchmark-delta-coverage"
            mock_build.return_value = mock_result

            with patch.object(engine.infra, "has_harness", return_value=True):
                report = engine.collect_coverage(
                    benchmark_path=mock_benchmark,
                    corpus_dir=corpus,
                )

        # Should return report with 0 corpus
        assert report.final_summary.corpus_total == 0

    @patch("crsbench.evaluation.coverage.engine.create_coverage_strategy")
    def test_collect_coverage_parallel_processing(
        self,
        mock_create_strategy,
        mock_oss_fuzz: Path,
        mock_benchmark: Path,
        mock_corpus: Path,
    ):
        """Test that collect_coverage processes corpus files in parallel."""
        # Setup mock strategy
        mock_strategy = MagicMock()
        mock_strategy.collect_single_coverage.return_value = {
            "main": {"src": "main.c", "lines": [1, 2, 3]}
        }
        mock_create_strategy.return_value = mock_strategy

        engine = CoverageEngine(mock_oss_fuzz, verify_workers=2)

        # Mock the build to succeed
        with patch.object(engine.builder, "build_single") as mock_build:
            mock_result = MagicMock()
            mock_result.success = True
            mock_result.variant_name = "test-benchmark-delta-coverage"
            mock_build.return_value = mock_result

            with patch.object(engine.infra, "has_harness", return_value=True):
                report = engine.collect_coverage(
                    benchmark_path=mock_benchmark,
                    corpus_dir=mock_corpus,
                )

        # Should process all 3 corpus files (from mock_corpus fixture)
        assert mock_strategy.collect_single_coverage.call_count == 3
        assert report.final_summary.corpus_total == 3
        assert report.harness_name == "fuzz_target"


class TestCoverageEngineIntegration:
    """Integration tests for CoverageEngine with mocked OSSFuzz infrastructure."""

    @patch("crsbench.evaluation.coverage.engine.create_coverage_strategy")
    def test_full_workflow(
        self,
        mock_create_strategy,
        mock_oss_fuzz: Path,
        mock_benchmark: Path,
        mock_corpus: Path,
    ):
        """Test complete coverage workflow with mocks."""
        # Setup mock strategy to return different coverage per input
        mock_strategy = MagicMock()
        call_count = [0]

        def mock_collect(_harness, _corpus, **_kwargs):
            call_count[0] += 1
            return {"func": {"src": "test.c", "lines": [call_count[0] * 10]}}

        mock_strategy.collect_single_coverage.side_effect = mock_collect
        mock_create_strategy.return_value = mock_strategy

        engine = CoverageEngine(mock_oss_fuzz, build_workers=1, verify_workers=2)

        try:
            with patch.object(engine.builder, "build_single") as mock_build:
                mock_result = MagicMock()
                mock_result.success = True
                mock_result.variant_name = "test-benchmark-delta-coverage"
                mock_build.return_value = mock_result

                with patch.object(engine.infra, "has_harness", return_value=True):
                    report = engine.collect_coverage(
                        benchmark_path=mock_benchmark,
                        corpus_dir=mock_corpus,
                        force_rebuild=True,
                    )

            # Verify report
            assert report.harness_name == "fuzz_target"
            assert report.final_summary.corpus_total == 3
            assert report.final_summary.corpus_contributing == 3
            assert report.final_summary.functions_covered == 1
            # Each corpus covers 1 unique line
            assert report.final_summary.lines_covered == 3

        finally:
            engine.cleanup()


class TestCoverageEngineBuildCoverageVariant:
    """Tests for CoverageEngine._build_coverage_variant()."""

    def test_build_variant_success(
        self,
        mock_benchmark: Path,
        coverage_engine: CoverageEngine,
    ):
        """Test building coverage variant successfully."""
        adapter = coverage_engine._load_adapter(mock_benchmark)

        with patch.object(coverage_engine.builder, "build_single") as mock_build:
            mock_result = MagicMock()
            mock_result.success = True
            mock_result.variant_name = "test-benchmark-delta-coverage"
            mock_build.return_value = mock_result

            result = coverage_engine._build_coverage_variant(adapter)

        assert result == "test-benchmark-delta-coverage"

    def test_build_variant_failure(
        self,
        mock_benchmark: Path,
        coverage_engine: CoverageEngine,
    ):
        """Test building coverage variant when build fails."""
        adapter = coverage_engine._load_adapter(mock_benchmark)

        with patch.object(coverage_engine.builder, "build_single") as mock_build:
            mock_result = MagicMock()
            mock_result.success = False
            mock_build.return_value = mock_result

            result = coverage_engine._build_coverage_variant(adapter)

        assert result is None
