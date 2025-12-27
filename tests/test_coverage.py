"""Tests for coverage module."""

import json
import tempfile
from pathlib import Path

import pytest

from crsbench.evaluation.coverage.models import (
    CoverageConfig,
    CoverageReport,
    CoverageSnapshot,
    CoverageSummary,
    CorpusCoverage,
    FunctionCoverage,
)
from crsbench.evaluation.coverage.store import CoverageStore


class TestCoverageModels:
    """Tests for coverage data models."""

    def test_coverage_config_defaults(self):
        """Test CoverageConfig with default values."""
        config = CoverageConfig()
        assert config.enabled is False
        assert config.language == "c"
        assert config.metric == "line"
        assert config.saturation_time == 21600  # 6 hours

    def test_coverage_config_custom(self):
        """Test CoverageConfig with custom values."""
        config = CoverageConfig(
            enabled=True,
            language="java",
            metric="function",
            saturation_time=3600,
        )
        assert config.enabled is True
        assert config.language == "java"
        assert config.metric == "function"
        assert config.saturation_time == 3600

    def test_function_coverage(self):
        """Test FunctionCoverage model."""
        func_cov = FunctionCoverage(src="test.c", lines=[1, 2, 3, 5, 8])
        assert func_cov.src == "test.c"
        assert func_cov.lines == [1, 2, 3, 5, 8]

    def test_corpus_coverage(self):
        """Test CorpusCoverage model."""
        corpus = CorpusCoverage(
            hash="abc123def456",
            first_seen_ts=1000.0,
            file_size=256,
            contributes_unique=True,
            coverage={
                "main": FunctionCoverage(src="main.c", lines=[1, 2, 3]),
            },
        )
        assert corpus.hash == "abc123def456"
        assert corpus.first_seen_ts == 1000.0
        assert corpus.file_size == 256
        assert corpus.contributes_unique is True
        assert "main" in corpus.coverage

    def test_coverage_summary_defaults(self):
        """Test CoverageSummary with defaults."""
        summary = CoverageSummary()
        assert summary.metric == "line"
        assert summary.corpus_total == 0
        assert summary.lines_covered == 0
        assert summary.lines_percent == 0.0

    def test_coverage_summary_with_values(self):
        """Test CoverageSummary with values."""
        summary = CoverageSummary(
            metric="line",
            corpus_total=10,
            corpus_contributing=5,
            lines_covered=100,
            lines_total=200,
            lines_percent=50.0,
            functions_covered=10,
            functions_total=20,
        )
        assert summary.corpus_total == 10
        assert summary.lines_percent == 50.0

    def test_coverage_snapshot(self):
        """Test CoverageSnapshot model."""
        summary = CoverageSummary(lines_covered=50, lines_total=100)
        snapshot = CoverageSnapshot(
            cycle=1,
            timestamp=1000.0,
            elapsed_time=60.0,
            harness_name="fuzz_parse",
            summary=summary,
            new_corpus_count=5,
            new_lines_count=10,
        )
        assert snapshot.cycle == 1
        assert snapshot.harness_name == "fuzz_parse"
        assert snapshot.summary.lines_covered == 50
        assert snapshot.new_corpus_count == 5

    def test_coverage_report(self):
        """Test CoverageReport model."""
        summary = CoverageSummary(lines_covered=100, lines_total=200)
        snapshot = CoverageSnapshot(
            cycle=1,
            timestamp=1000.0,
            elapsed_time=60.0,
            harness_name="fuzz_parse",
            summary=summary,
        )
        report = CoverageReport(
            harness_name="fuzz_parse",
            snapshots=[snapshot],
            final_summary=summary,
            saturation_cycle=None,
        )
        assert report.harness_name == "fuzz_parse"
        assert len(report.snapshots) == 1
        assert report.final_summary.lines_covered == 100


class TestCoverageStore:
    """Tests for CoverageStore."""

    def test_store_initialization(self):
        """Test CoverageStore initialization."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            store_dir = Path(tmp_dir) / "coverage"
            store = CoverageStore(store_dir)
            assert store_dir.exists()
            assert len(store.corpus) == 0
            assert len(store.covered_lines) == 0

    def test_add_corpus(self):
        """Test adding corpus with coverage data."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            store_dir = Path(tmp_dir) / "coverage"
            store = CoverageStore(store_dir)

            # Create a test corpus file
            corpus_file = Path(tmp_dir) / "corpus1"
            corpus_file.write_bytes(b"test corpus content")

            # Add corpus with coverage data
            cov_data = {
                "main": {"src": "main.c", "lines": [1, 2, 3]},
                "parse": {"src": "parse.c", "lines": [10, 11]},
            }
            corpus_hash, contributes = store.add_corpus(
                corpus_file, cov_data, timestamp=1000.0
            )

            assert len(corpus_hash) == 12
            assert contributes is True  # First corpus always contributes
            assert len(store.corpus) == 1
            assert len(store.covered_lines) == 5

    def test_add_duplicate_corpus(self):
        """Test adding duplicate corpus (same content)."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            store_dir = Path(tmp_dir) / "coverage"
            store = CoverageStore(store_dir)

            # Create test corpus files with same content
            corpus1 = Path(tmp_dir) / "corpus1"
            corpus1.write_bytes(b"same content")
            corpus2 = Path(tmp_dir) / "corpus2"
            corpus2.write_bytes(b"same content")

            cov_data = {"main": {"src": "main.c", "lines": [1, 2]}}

            hash1, contrib1 = store.add_corpus(corpus1, cov_data)
            hash2, contrib2 = store.add_corpus(corpus2, cov_data)

            # Same hash
            assert hash1 == hash2
            # Only one entry in store
            assert len(store.corpus) == 1

    def test_get_contributing_corpus(self):
        """Test getting corpus that contributes unique coverage."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            store_dir = Path(tmp_dir) / "coverage"
            store = CoverageStore(store_dir)

            # Create corpus files
            corpus1 = Path(tmp_dir) / "corpus1"
            corpus1.write_bytes(b"content1")
            corpus2 = Path(tmp_dir) / "corpus2"
            corpus2.write_bytes(b"content2")

            # First corpus has unique coverage
            store.add_corpus(corpus1, {"f1": {"src": "a.c", "lines": [1, 2]}})
            # Second corpus has no new coverage
            store.add_corpus(corpus2, {"f1": {"src": "a.c", "lines": [1]}})

            contributing = store.get_contributing_corpus()
            assert len(contributing) == 1

    def test_get_summary(self):
        """Test getting coverage summary."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            store_dir = Path(tmp_dir) / "coverage"
            store = CoverageStore(store_dir)

            # Add some coverage data
            corpus1 = Path(tmp_dir) / "corpus1"
            corpus1.write_bytes(b"content1")
            store.add_corpus(corpus1, {"f1": {"src": "a.c", "lines": [1, 2, 3]}})
            store.set_totals(lines_total=10, functions_total=5)

            summary = store.get_summary()
            assert summary.corpus_total == 1
            assert summary.lines_covered == 3
            assert summary.lines_total == 10
            assert summary.lines_percent == 30.0

    def test_save_and_load(self):
        """Test saving and loading store state."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            store_dir = Path(tmp_dir) / "coverage"
            store = CoverageStore(store_dir)

            # Add data
            corpus1 = Path(tmp_dir) / "corpus1"
            corpus1.write_bytes(b"content1")
            store.add_corpus(corpus1, {"f1": {"src": "a.c", "lines": [1, 2, 3]}})
            store.set_totals(lines_total=100, functions_total=10)

            # Save
            save_path = Path(tmp_dir) / "store.json"
            store.save(save_path)
            assert save_path.exists()

            # Load into new store
            new_store = CoverageStore(Path(tmp_dir) / "coverage2")
            new_store.load(save_path)

            # Verify loaded data
            assert len(new_store.corpus) == 1
            assert len(new_store.covered_lines) == 3
            summary = new_store.get_summary()
            assert summary.lines_total == 100


class TestCoverageBuilder:
    """Tests for CoverageBuilder."""

    def test_get_coverage_variant_name(self):
        """Test coverage variant name generation."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            # Create minimal oss-fuzz structure
            oss_fuzz = Path(tmp_dir)
            (oss_fuzz / "infra").mkdir()
            (oss_fuzz / "infra" / "helper.py").touch()
            (oss_fuzz / "projects").mkdir()

            from crsbench.evaluation.coverage.builder import CoverageBuilder

            builder = CoverageBuilder(oss_fuzz)
            assert builder.get_coverage_variant_name("mock-c") == "mock-c-coverage"
            assert (
                builder.get_coverage_variant_name("sanity-mock-c-delta-01")
                == "sanity-mock-c-delta-01-coverage"
            )

    def test_is_built_returns_false_when_not_built(self):
        """Test is_built returns False when no build exists."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            oss_fuzz = Path(tmp_dir)
            (oss_fuzz / "infra").mkdir()
            (oss_fuzz / "infra" / "helper.py").touch()
            (oss_fuzz / "projects").mkdir()

            from crsbench.evaluation.coverage.builder import CoverageBuilder

            builder = CoverageBuilder(oss_fuzz)
            assert builder.is_built("nonexistent-project") is False


class TestCoverageStrategy:
    """Tests for coverage strategies."""

    def test_create_strategy_c(self):
        """Test creating LLVM strategy for C."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            oss_fuzz = Path(tmp_dir)
            (oss_fuzz / "infra").mkdir()
            (oss_fuzz / "infra" / "helper.py").touch()

            from crsbench.evaluation.coverage.strategy import create_coverage_strategy

            strategy = create_coverage_strategy(oss_fuzz, "test-project", "c")
            assert strategy.project_name == "test-project"
            assert strategy.language == "c"

    def test_create_strategy_java(self):
        """Test creating JaCoCo strategy for Java."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            oss_fuzz = Path(tmp_dir)
            (oss_fuzz / "infra").mkdir()
            (oss_fuzz / "infra" / "helper.py").touch()

            from crsbench.evaluation.coverage.strategy import create_coverage_strategy

            strategy = create_coverage_strategy(oss_fuzz, "test-project", "jvm")
            assert strategy.project_name == "test-project"
            assert strategy.language == "jvm"

    def test_create_strategy_unsupported(self):
        """Test creating strategy for unsupported language."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            oss_fuzz = Path(tmp_dir)
            (oss_fuzz / "infra").mkdir()
            (oss_fuzz / "infra" / "helper.py").touch()

            from crsbench.evaluation.coverage.strategy import (
                CoverageStrategyError,
                create_coverage_strategy,
            )

            with pytest.raises(CoverageStrategyError, match="Unsupported language"):
                create_coverage_strategy(oss_fuzz, "test-project", "go")

    def test_parse_llvm_cov_summary(self):
        """Test parsing LLVM cov summary.json."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            summary_path = Path(tmp_dir) / "summary.json"
            summary_data = {
                "data": [
                    {
                        "totals": {
                            "lines": {"covered": 100, "count": 200, "percent": 50.0},
                            "functions": {"covered": 10, "count": 20, "percent": 50.0},
                            "regions": {"covered": 50, "count": 100, "percent": 50.0},
                        }
                    }
                ]
            }
            summary_path.write_text(json.dumps(summary_data))

            from crsbench.evaluation.coverage.strategy import parse_llvm_cov_summary

            result = parse_llvm_cov_summary(summary_path)
            assert result["lines_covered"] == 100
            assert result["lines_total"] == 200
            assert result["lines_percent"] == 50.0
            assert result["functions_covered"] == 10
