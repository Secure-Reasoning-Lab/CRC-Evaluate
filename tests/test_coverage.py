"""Tests for coverage module."""

import json
import tempfile
from pathlib import Path

import pytest
from crsbench.evaluation.coverage.models import (
    CorpusCoverage,
    CoverageConfig,
    CoverageReport,
    CoverageSnapshot,
    CoverageSummary,
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

    def test_is_built_returns_true_when_built(self):
        """Test is_built returns True when variant project and build output exist."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            oss_fuzz = Path(tmp_dir)
            (oss_fuzz / "infra").mkdir()
            (oss_fuzz / "infra" / "helper.py").touch()
            (oss_fuzz / "projects").mkdir()
            (oss_fuzz / "build" / "out").mkdir(parents=True)

            from crsbench.evaluation.coverage.builder import CoverageBuilder

            builder = CoverageBuilder(oss_fuzz)

            # Create variant project directory
            variant_name = builder.get_coverage_variant_name("mock-c")
            (oss_fuzz / "projects" / variant_name).mkdir()

            # Create build output with a file
            build_path = builder.get_build_output_path(variant_name)
            build_path.mkdir(parents=True)
            (build_path / "fuzz_target").touch()

            assert builder.is_built("mock-c") is True

    def test_build_returns_cached_when_already_built(self):
        """Test build() returns cached CoverageBuild without rebuilding."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            oss_fuzz = Path(tmp_dir)
            (oss_fuzz / "infra").mkdir()
            (oss_fuzz / "infra" / "helper.py").touch()
            (oss_fuzz / "projects").mkdir()
            (oss_fuzz / "build" / "out").mkdir(parents=True)

            from crsbench.evaluation.coverage.builder import CoverageBuilder

            builder = CoverageBuilder(oss_fuzz)

            # Setup: create variant project and build output (simulating previous build)
            variant_name = builder.get_coverage_variant_name("mock-c")
            (oss_fuzz / "projects" / variant_name).mkdir()
            build_path = builder.get_build_output_path(variant_name)
            build_path.mkdir(parents=True)
            (build_path / "fuzz_target").touch()

            # Call build - should return cached without subprocess
            result = builder.build(
                project_name="mock-c",
                benchmark_path=Path("/nonexistent"),  # Not used when cached
                main_repo="https://example.com/repo",
                commit="abc123",
                language="c",
            )

            assert result is not None
            assert result.project_name == "mock-c"
            assert result.variant_name == "mock-c-coverage"
            assert result.language == "c"
            assert result.commit == "abc123"
            assert result.build_path == build_path


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


class TestExportUniqueCorpus:
    """Tests for export_unique_corpus functionality."""

    def test_export_unique_corpus_creates_files(self):
        """Test that export_unique_corpus creates corpus and .cov files."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)

            # Create store and add corpus
            store_dir = tmp_path / "store"
            store = CoverageStore(store_dir)

            corpus1 = tmp_path / "corpus1"
            corpus1.write_bytes(b"content1")
            corpus2 = tmp_path / "corpus2"
            corpus2.write_bytes(b"content2")

            # First corpus covers lines 1-3, second covers same lines (no new)
            cov_data1 = {"func1": {"src": "test.c", "lines": [1, 2, 3]}}
            cov_data2 = {"func1": {"src": "test.c", "lines": [1, 2]}}

            hash1, _ = store.add_corpus(corpus1, cov_data1, timestamp=1000.0)
            hash2, _ = store.add_corpus(corpus2, cov_data2, timestamp=2000.0)

            # Create collector with store
            from unittest.mock import MagicMock

            from crsbench.evaluation.coverage.collector import CoverageCollector

            mock_strategy = MagicMock()
            collector = CoverageCollector(mock_strategy, store, "test_harness")

            # Map hashes to paths
            collector._corpus_hash_to_path[hash1] = corpus1
            collector._corpus_hash_to_path[hash2] = corpus2

            # Export unique corpus
            output_dir = tmp_path / "corpus_unique"
            collector.export_unique_corpus(output_dir)

            # Only first corpus should be exported (contributes unique)
            assert (output_dir / hash1).exists()
            assert (output_dir / f".{hash1}.cov").exists()
            # Second corpus doesn't contribute unique, shouldn't be exported
            assert not (output_dir / hash2).exists()

    def test_export_unique_corpus_cov_file_format(self):
        """Test that .cov file contains correct fields including elapsed_time."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)

            # Create store and add corpus
            store_dir = tmp_path / "store"
            store = CoverageStore(store_dir)

            corpus1 = tmp_path / "corpus1"
            corpus1.write_bytes(b"unique content")

            cov_data = {"main": {"src": "main.c", "lines": [10, 20, 30]}}
            trial_start_time = 1704067200.0  # 2024-01-01 00:00:00 UTC
            corpus_timestamp = trial_start_time + 30.5  # 30.5 seconds after start

            corpus_hash, _ = store.add_corpus(
                corpus1, cov_data, timestamp=corpus_timestamp
            )

            # Create collector and set run_start_time
            from unittest.mock import MagicMock

            from crsbench.evaluation.coverage.collector import CoverageCollector

            mock_strategy = MagicMock()
            collector = CoverageCollector(mock_strategy, store, "test_harness")
            # Set run_start_time after creation (simulating on_run_start callback)
            collector.set_run_start_time(trial_start_time)
            collector._corpus_hash_to_path[corpus_hash] = corpus1

            # Export unique corpus
            output_dir = tmp_path / "corpus_unique"
            collector.export_unique_corpus(output_dir)

            # Read and verify .cov file
            cov_path = output_dir / f".{corpus_hash}.cov"
            cov_data_loaded = json.loads(cov_path.read_text())

            assert cov_data_loaded["hash"] == corpus_hash
            assert cov_data_loaded["seen_ts"] == corpus_timestamp
            assert cov_data_loaded["elapsed_time"] == 30.5  # seconds since trial start
            assert cov_data_loaded["file_size"] == len(b"unique content")
            assert cov_data_loaded["contributes_unique"] is True
            assert "main" in cov_data_loaded["coverage"]
            assert cov_data_loaded["coverage"]["main"]["lines"] == [10, 20, 30]

    def test_export_unique_corpus_elapsed_time_none_without_start(self):
        """Test that elapsed_time is None when trial_start_time not provided."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)

            store_dir = tmp_path / "store"
            store = CoverageStore(store_dir)

            corpus1 = tmp_path / "corpus1"
            corpus1.write_bytes(b"content")

            cov_data = {"func": {"src": "test.c", "lines": [1]}}
            corpus_hash, _ = store.add_corpus(corpus1, cov_data, timestamp=1000.0)

            from unittest.mock import MagicMock

            from crsbench.evaluation.coverage.collector import CoverageCollector

            mock_strategy = MagicMock()
            # No trial_start_time provided
            collector = CoverageCollector(mock_strategy, store, "test_harness")
            collector._corpus_hash_to_path[corpus_hash] = corpus1

            output_dir = tmp_path / "corpus_unique"
            collector.export_unique_corpus(output_dir)

            cov_path = output_dir / f".{corpus_hash}.cov"
            cov_data_loaded = json.loads(cov_path.read_text())

            # elapsed_time should be None when trial_start_time not set
            assert cov_data_loaded["elapsed_time"] is None

    def test_export_unique_corpus_preserves_first_seen(self):
        """Test that duplicate corpus keeps earliest timestamp."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)

            store_dir = tmp_path / "store"
            store = CoverageStore(store_dir)

            # Create two files with same content (duplicate)
            corpus1 = tmp_path / "corpus1"
            corpus1.write_bytes(b"same content")
            corpus2 = tmp_path / "corpus2"
            corpus2.write_bytes(b"same content")

            cov_data = {"func": {"src": "test.c", "lines": [1]}}

            # Add second corpus first (later timestamp)
            hash1, _ = store.add_corpus(corpus1, cov_data, timestamp=2000.0)
            # Add first corpus second (earlier timestamp)
            hash2, _ = store.add_corpus(corpus2, cov_data, timestamp=1000.0)

            # Should be same hash
            assert hash1 == hash2

            # Check that store keeps earliest timestamp
            stored_corpus = store.corpus[hash1]
            assert stored_corpus.first_seen_ts == 1000.0

    def test_export_unique_corpus_hidden_files(self):
        """Test that .cov files are hidden (start with dot)."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)

            store_dir = tmp_path / "store"
            store = CoverageStore(store_dir)

            corpus1 = tmp_path / "corpus1"
            corpus1.write_bytes(b"test content")

            cov_data = {"func": {"src": "test.c", "lines": [1, 2]}}
            corpus_hash, _ = store.add_corpus(corpus1, cov_data, timestamp=1000.0)

            from unittest.mock import MagicMock

            from crsbench.evaluation.coverage.collector import CoverageCollector

            mock_strategy = MagicMock()
            collector = CoverageCollector(mock_strategy, store, "test_harness")
            collector._corpus_hash_to_path[corpus_hash] = corpus1

            output_dir = tmp_path / "corpus_unique"
            collector.export_unique_corpus(output_dir)

            # List all files
            all_files = list(output_dir.iterdir())
            corpus_files = [f for f in all_files if not f.name.startswith(".")]
            cov_files = [f for f in all_files if f.name.startswith(".")]

            assert len(corpus_files) == 1
            assert len(cov_files) == 1
            assert cov_files[0].name == f".{corpus_hash}.cov"
