"""Tests for CoverageEngine.

Essential tests for:
- Worker configuration
- Thread-safe coverage merging
- Summary computation
- Parallel coverage collection
- Variant building
"""

from pathlib import Path
from typing import Generator
from unittest.mock import MagicMock, patch

import pytest
import yaml
from crsbench.evaluation.coverage.engine import CoverageEngine


@pytest.fixture
def mock_oss_fuzz(tmp_path: Path) -> Path:
    """Create mock OSS-Fuzz directory."""
    oss_fuzz = tmp_path / "oss-fuzz"
    (oss_fuzz / "infra").mkdir(parents=True)
    (oss_fuzz / "infra" / "helper.py").touch()
    (oss_fuzz / "projects").mkdir()
    (oss_fuzz / "build" / "out").mkdir(parents=True)
    return oss_fuzz


@pytest.fixture
def mock_benchmark(tmp_path: Path) -> Path:
    """Create mock benchmark with meta.yaml and project.yaml."""
    benchmark = tmp_path / "test-benchmark"
    benchmark.mkdir()
    aixcc = benchmark / ".aixcc"
    aixcc.mkdir()

    meta = {
        "delta_mode": {"ref_commit": "a" * 40, "base_commit": "b" * 40},
        "harness_files": [
            {
                "name": "fuzz_target",
                "path": "/src/fuzz.c",
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
    (aixcc / "meta.yaml").write_text(yaml.dump(meta))
    (benchmark / "project.yaml").write_text(
        yaml.dump({"language": "c", "main_repo": "https://example.com/repo"})
    )
    return benchmark


@pytest.fixture
def mock_corpus(tmp_path: Path) -> Path:
    """Create mock corpus directory with 3 files."""
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    for i in range(3):
        (corpus / f"input_{i}").write_bytes(f"content{i}".encode())
    return corpus


@pytest.fixture
def engine(mock_oss_fuzz: Path) -> Generator[CoverageEngine, None, None]:
    """Create CoverageEngine with cleanup."""
    eng = CoverageEngine(mock_oss_fuzz, build_workers=2, verify_workers=4)
    yield eng
    eng.cleanup()


class TestCoverageEngine:
    """Essential tests for CoverageEngine."""

    def test_worker_configuration(self, mock_oss_fuzz: Path):
        """Test worker counts are set correctly."""
        eng = CoverageEngine(mock_oss_fuzz, build_workers=8, verify_workers=16)
        assert eng.build_workers == 8
        assert eng.verify_workers == 16

    def test_merge_overlapping_coverage(self, engine: CoverageEngine):
        """Test merging coverage deduplicates overlapping lines."""
        merged = {"main": {"src": "main.c", "lines": {1, 2, 3}}}
        engine._merge_coverage_safe(merged, {"main": {"src": "main.c", "lines": [3, 4, 5]}})
        assert merged["main"]["lines"] == {1, 2, 3, 4, 5}

    def test_merge_multiple_functions(self, engine: CoverageEngine):
        """Test merging coverage across multiple functions."""
        merged: dict = {}
        engine._merge_coverage_safe(merged, {"func1": {"src": "a.c", "lines": [1, 2]}})
        engine._merge_coverage_safe(merged, {"func2": {"src": "b.c", "lines": [10]}})
        assert "func1" in merged
        assert "func2" in merged

    def test_compute_summary(self, engine: CoverageEngine):
        """Test summary computation uses totals from batch coverage."""
        merged = {
            "func1": {"src": "a.c", "lines": {1, 2, 3}},
            "func2": {"src": "b.c", "lines": {10, 11}},
        }
        totals = {
            "lines_covered": 50,
            "lines_total": 100,
            "lines_percent": 50.0,
            "functions_covered": 10,
            "functions_total": 20,
        }
        summary = engine._compute_summary(merged, 10, 8, totals)

        # Values come from totals dict, not merged_coverage
        assert summary.lines_covered == 50
        assert summary.lines_total == 100
        assert summary.lines_percent == 50.0
        assert summary.functions_covered == 10
        assert summary.functions_total == 20
        assert summary.corpus_total == 10
        assert summary.corpus_contributing == 8

    @patch("crsbench.evaluation.coverage.engine.parse_llvm_cov_summary")
    @patch("crsbench.evaluation.coverage.engine.create_coverage_strategy")
    def test_parallel_corpus_processing(
        self,
        mock_create_strategy,
        mock_parse_summary,
        mock_oss_fuzz: Path,
        mock_benchmark: Path,
        mock_corpus: Path,
    ):
        """Test corpus files are processed in parallel."""
        mock_strategy = MagicMock()
        mock_strategy.collect_single_coverage.return_value = {
            "main": {"src": "main.c", "lines": [1, 2, 3]}
        }
        mock_strategy.collect_batch_coverage.return_value = Path("/tmp/summary.json")
        mock_create_strategy.return_value = mock_strategy

        # Mock totals from batch coverage
        mock_parse_summary.return_value = {
            "lines_covered": 10,
            "lines_total": 100,
            "lines_percent": 10.0,
            "functions_covered": 5,
            "functions_total": 20,
        }

        eng = CoverageEngine(mock_oss_fuzz, verify_workers=2)

        with patch.object(eng.builder, "build_single") as mock_build:
            mock_result = MagicMock()
            mock_result.success = True
            mock_result.variant_name = "test-benchmark-delta-coverage"
            mock_build.return_value = mock_result

            with patch.object(eng.infra, "has_harness", return_value=True):
                report = eng.collect_coverage(mock_benchmark, mock_corpus)

        # All 3 corpus files should be processed
        assert mock_strategy.collect_single_coverage.call_count == 3
        assert report.final_summary.corpus_total == 3
        assert report.final_summary.lines_total == 100
        assert report.harness_name == "fuzz_target"

    def test_build_variant_success(self, mock_benchmark: Path, engine: CoverageEngine):
        """Test successful coverage variant build."""
        adapter = engine._load_adapter(mock_benchmark)

        with patch.object(engine.builder, "build_single") as mock_build:
            mock_result = MagicMock()
            mock_result.success = True
            mock_result.variant_name = "test-benchmark-delta-coverage"
            mock_build.return_value = mock_result

            result = engine._build_coverage_variant(adapter)

        assert result == "test-benchmark-delta-coverage"

    def test_build_variant_failure(self, mock_benchmark: Path, engine: CoverageEngine):
        """Test failed build returns None."""
        adapter = engine._load_adapter(mock_benchmark)

        with patch.object(engine.builder, "build_single") as mock_build:
            mock_result = MagicMock()
            mock_result.success = False
            mock_build.return_value = mock_result

            result = engine._build_coverage_variant(adapter)

        assert result is None
