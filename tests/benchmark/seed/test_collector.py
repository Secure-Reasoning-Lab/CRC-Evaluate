"""Tests for seed corpus collector."""

import json
from pathlib import Path

import pytest
from crsbench.benchmark.seed.collector import (
    CollectionResult,
    CorpusCollector,
    CorpusFile,
)


class TestCorpusFile:
    """Tests for CorpusFile dataclass."""

    def test_corpus_file_creation(self):
        """Test creating a CorpusFile instance."""
        cf = CorpusFile(
            path=Path("/tmp/test"),
            content_hash="abc123",
            mtime=1000.0,
            relative_time=100.0,
            size=256,
        )
        assert cf.path == Path("/tmp/test")
        assert cf.content_hash == "abc123"
        assert cf.mtime == 1000.0
        assert cf.relative_time == 100.0
        assert cf.size == 256


class TestCollectionResult:
    """Tests for CollectionResult dataclass."""

    def test_collection_result_creation(self):
        """Test creating a CollectionResult instance."""
        result = CollectionResult(
            benchmark_name="test-bench",
            harness_name="test-harness",
            total_files=10,
            output_dir=Path("/tmp/output"),
        )
        assert result.benchmark_name == "test-bench"
        assert result.harness_name == "test-harness"
        assert result.total_files == 10
        assert result.output_dir == Path("/tmp/output")
        assert result.warnings == []

    def test_collection_result_with_warnings(self):
        """Test CollectionResult with warnings."""
        result = CollectionResult(
            benchmark_name="test-bench",
            harness_name="test-harness",
            total_files=5,
            output_dir=Path("/tmp/output"),
            warnings=["Warning 1", "Warning 2"],
        )
        assert len(result.warnings) == 2


class TestCorpusCollector:
    """Tests for CorpusCollector class."""

    def test_compute_hash(self, tmp_path: Path):
        """Test hash computation for a file."""
        # Create a test file
        test_file = tmp_path / "test_file"
        test_file.write_bytes(b"test content")

        collector = CorpusCollector(tmp_path, tmp_path)
        hash_value = collector._compute_hash(test_file)

        # Hash should be 16 characters (truncated SHA256)
        assert len(hash_value) == 16
        assert hash_value.isalnum()

    def test_compute_hash_deterministic(self, tmp_path: Path):
        """Test that hash computation is deterministic."""
        test_file = tmp_path / "test_file"
        test_file.write_bytes(b"same content")

        collector = CorpusCollector(tmp_path, tmp_path)
        hash1 = collector._compute_hash(test_file)
        hash2 = collector._compute_hash(test_file)

        assert hash1 == hash2

    def test_compute_hash_different_content(self, tmp_path: Path):
        """Test that different content produces different hashes."""
        file1 = tmp_path / "file1"
        file2 = tmp_path / "file2"
        file1.write_bytes(b"content A")
        file2.write_bytes(b"content B")

        collector = CorpusCollector(tmp_path, tmp_path)
        hash1 = collector._compute_hash(file1)
        hash2 = collector._compute_hash(file2)

        assert hash1 != hash2

    def test_collect_corpus_files_skips_hidden(self, tmp_path: Path):
        """Test that hidden files are skipped."""
        corpus_dir = tmp_path / "corpus"
        corpus_dir.mkdir()

        # Create regular files
        (corpus_dir / "file1").write_bytes(b"content1")
        (corpus_dir / "file2").write_bytes(b"content2")

        # Create hidden files (should be skipped)
        (corpus_dir / ".hidden").write_bytes(b"hidden")
        (corpus_dir / ".file1.metadata").write_bytes(b"metadata")

        collector = CorpusCollector(tmp_path, tmp_path)
        crs_start_time = 1000.0
        files = collector._collect_corpus_files(corpus_dir, crs_start_time)

        assert len(files) == 2
        file_names = [f.path.name for f in files]
        assert "file1" in file_names
        assert "file2" in file_names
        assert ".hidden" not in file_names
        assert ".file1.metadata" not in file_names

    def test_collect_corpus_files_skips_directories(self, tmp_path: Path):
        """Test that directories are skipped."""
        corpus_dir = tmp_path / "corpus"
        corpus_dir.mkdir()

        # Create regular file
        (corpus_dir / "file1").write_bytes(b"content1")

        # Create subdirectory (should be skipped)
        subdir = corpus_dir / "subdir"
        subdir.mkdir()
        (subdir / "nested_file").write_bytes(b"nested")

        collector = CorpusCollector(tmp_path, tmp_path)
        files = collector._collect_corpus_files(corpus_dir, 1000.0)

        assert len(files) == 1
        assert files[0].path.name == "file1"

    def test_write_corpus(self, tmp_path: Path):
        """Test writing corpus files and manifest."""
        output_dir = tmp_path / "output"

        # Create test corpus files
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        file1 = source_dir / "file1"
        file2 = source_dir / "file2"
        file1.write_bytes(b"content1")
        file2.write_bytes(b"content2")

        collector = CorpusCollector(tmp_path, tmp_path)

        corpus_files = [
            CorpusFile(
                path=file1,
                content_hash="hash1",
                mtime=1100.0,
                relative_time=100.0,
                size=8,
            ),
            CorpusFile(
                path=file2,
                content_hash="hash2",
                mtime=1200.0,
                relative_time=200.0,
                size=8,
            ),
        ]

        crs_start_time = 1000.0
        collector._write_corpus(corpus_files, output_dir, crs_start_time)

        # Check output directory structure
        assert output_dir.exists()
        assert (output_dir / "hash1").exists()
        assert (output_dir / "hash2").exists()
        assert (output_dir / "manifest.json").exists()

        # Check manifest content
        with (output_dir / "manifest.json").open() as f:
            manifest = json.load(f)

        assert manifest["crs_run_start_time"] == 1000.0
        assert "hash1" in manifest["files"]
        assert "hash2" in manifest["files"]
        assert manifest["files"]["hash1"]["relative_time"] == 100.0
        assert manifest["files"]["hash2"]["relative_time"] == 200.0

    def test_write_corpus_deduplicates(self, tmp_path: Path):
        """Test that duplicate content hashes are deduplicated."""
        output_dir = tmp_path / "output"
        source_dir = tmp_path / "source"
        source_dir.mkdir()

        # Two files with same content hash
        file1 = source_dir / "file1"
        file2 = source_dir / "file2"
        file1.write_bytes(b"same content")
        file2.write_bytes(b"same content")

        collector = CorpusCollector(tmp_path, tmp_path)

        corpus_files = [
            CorpusFile(
                path=file1,
                content_hash="same_hash",
                mtime=1100.0,
                relative_time=100.0,
                size=12,
            ),
            CorpusFile(
                path=file2,
                content_hash="same_hash",
                mtime=1150.0,
                relative_time=150.0,
                size=12,
            ),
        ]

        collector._write_corpus(corpus_files, output_dir, 1000.0)

        # Should have only one file (deduplicated)
        files = [f for f in output_dir.iterdir() if f.name != "manifest.json"]
        assert len(files) == 1
        assert files[0].name == "same_hash"

        # Manifest should have first occurrence
        with (output_dir / "manifest.json").open() as f:
            manifest = json.load(f)
        assert manifest["files"]["same_hash"]["relative_time"] == 100.0

    def test_find_trial_dir_direct(self, tmp_path: Path):
        """Test finding trial directory when directly under experiment dir."""
        experiment_dir = tmp_path / "experiment"
        trial_dir = experiment_dir / "trial-1"
        trial_dir.mkdir(parents=True)

        collector = CorpusCollector(experiment_dir, tmp_path)
        found = collector._find_trial_dir()

        assert found == trial_dir

    def test_find_trial_dir_nested(self, tmp_path: Path):
        """Test finding trial directory in nested structure."""
        experiment_dir = tmp_path / "experiment"
        nested_trial = experiment_dir / "bench" / "harness" / "mode" / "san" / "trial-1"
        nested_trial.mkdir(parents=True)

        collector = CorpusCollector(experiment_dir, tmp_path)
        found = collector._find_trial_dir()

        assert found == nested_trial

    def test_find_trial_dir_not_found(self, tmp_path: Path):
        """Test error when no trial directory found."""
        experiment_dir = tmp_path / "experiment"
        experiment_dir.mkdir()

        collector = CorpusCollector(experiment_dir, tmp_path)

        with pytest.raises(FileNotFoundError, match="No trial directory found"):
            collector._find_trial_dir()

    def test_find_corpus_dir_direct(self, tmp_path: Path):
        """Test finding corpus in output/corpus/."""
        trial_dir = tmp_path / "trial-1"
        corpus_dir = trial_dir / "output" / "corpus"
        corpus_dir.mkdir(parents=True)
        (corpus_dir / "file1").write_bytes(b"test")

        collector = CorpusCollector(tmp_path, tmp_path)
        found = collector._find_corpus_dir(trial_dir)

        assert found == corpus_dir

    def test_find_corpus_dir_crs_build(self, tmp_path: Path):
        """Test finding corpus in crs-build/run/ structure."""
        trial_dir = tmp_path / "trial-1"
        corpus_dir = trial_dir / "crs-build" / "run" / "some" / "path" / "corpus"
        corpus_dir.mkdir(parents=True)
        (corpus_dir / "file1").write_bytes(b"test")

        collector = CorpusCollector(tmp_path, tmp_path)
        found = collector._find_corpus_dir(trial_dir)

        assert found == corpus_dir

    def test_find_corpus_dir_not_found(self, tmp_path: Path):
        """Test error when no corpus directory found."""
        trial_dir = tmp_path / "trial-1"
        trial_dir.mkdir()

        collector = CorpusCollector(tmp_path, tmp_path)

        with pytest.raises(FileNotFoundError, match="No corpus directory found"):
            collector._find_corpus_dir(trial_dir)

    def test_load_crs_run_start_time(self, tmp_path: Path):
        """Test loading CRS run start time from pov_store.json."""
        trial_dir = tmp_path / "trial-1"
        povs_dir = trial_dir / "povs"
        povs_dir.mkdir(parents=True)

        pov_store = povs_dir / "pov_store.json"
        pov_store.write_text(json.dumps({"crs_run_start_time": 1234567890.123}))

        collector = CorpusCollector(tmp_path, tmp_path)
        start_time = collector._load_crs_run_start_time(trial_dir)

        assert start_time == 1234567890.123

    def test_load_crs_run_start_time_missing(self, tmp_path: Path):
        """Test None returned when pov_store.json missing."""
        trial_dir = tmp_path / "trial-1"
        trial_dir.mkdir()

        collector = CorpusCollector(tmp_path, tmp_path)
        start_time = collector._load_crs_run_start_time(trial_dir)

        assert start_time is None


class TestCorpusCollectorIntegration:
    """Integration tests for full collection workflow."""

    def test_full_collection(self, tmp_path: Path):
        """Test complete collection workflow."""
        # Set up experiment structure
        experiment_dir = tmp_path / "experiment"
        trial_dir = experiment_dir / "trial-1"
        povs_dir = trial_dir / "povs"
        corpus_dir = trial_dir / "output" / "corpus"
        corpus_dir.mkdir(parents=True)
        povs_dir.mkdir(parents=True)

        # Create pov_store.json
        crs_start = 1000.0
        pov_store = povs_dir / "pov_store.json"
        pov_store.write_text(json.dumps({"crs_run_start_time": crs_start}))

        # Create metadata.json
        metadata = trial_dir / "metadata.json"
        metadata.write_text(
            json.dumps({"benchmark_name": "test-bench", "harness_name": "test-harness"})
        )

        # Create corpus files with mtime > crs_start
        import os
        import time

        file1 = corpus_dir / "file1"
        file2 = corpus_dir / "file2"
        file1.write_bytes(b"content1")
        file2.write_bytes(b"content2")

        # Set mtime to be after crs_start
        future_time = time.time() + 100
        os.utime(file1, (future_time, future_time))
        os.utime(file2, (future_time, future_time + 100))

        # Set up benchmark directory
        benchmarks_dir = tmp_path / "benchmarks"
        benchmark_dir = benchmarks_dir / "test-bench"
        benchmark_dir.mkdir(parents=True)

        # Run collection
        collector = CorpusCollector(experiment_dir, benchmarks_dir)
        result = collector.collect()

        # Verify result
        assert result.benchmark_name == "test-bench"
        assert result.harness_name == "test-harness"
        assert result.total_files == 2
        assert result.output_dir == benchmark_dir / ".aixcc" / "test-harness" / "corpus"
        assert result.output_dir.exists()
        assert (result.output_dir / "manifest.json").exists()

    def test_collection_force_overwrite(self, tmp_path: Path):
        """Test that --force overwrites existing corpus."""
        # Set up experiment structure
        experiment_dir = tmp_path / "experiment"
        trial_dir = experiment_dir / "trial-1"
        povs_dir = trial_dir / "povs"
        corpus_dir = trial_dir / "output" / "corpus"
        corpus_dir.mkdir(parents=True)
        povs_dir.mkdir(parents=True)

        pov_store = povs_dir / "pov_store.json"
        pov_store.write_text(json.dumps({"crs_run_start_time": 1000.0}))

        metadata = trial_dir / "metadata.json"
        metadata.write_text(
            json.dumps({"benchmark_name": "test-bench", "harness_name": "test-harness"})
        )

        # Set up benchmark with existing corpus
        benchmarks_dir = tmp_path / "benchmarks"
        benchmark_dir = benchmarks_dir / "test-bench"
        existing_corpus = benchmark_dir / ".aixcc" / "test-harness" / "corpus"
        existing_corpus.mkdir(parents=True)
        (existing_corpus / "old_file").write_bytes(b"old")

        # Should fail without force
        collector = CorpusCollector(experiment_dir, benchmarks_dir)
        with pytest.raises(FileExistsError):
            collector.collect()

        # Should succeed with force
        result = collector.collect(force=True)
        assert result.total_files == 0  # No corpus files in this test
        assert not (existing_corpus / "old_file").exists()

    def test_collection_negative_relative_time_warning(self, tmp_path: Path):
        """Test that files with negative relative time are skipped with warning."""
        # Set up experiment structure
        experiment_dir = tmp_path / "experiment"
        trial_dir = experiment_dir / "trial-1"
        povs_dir = trial_dir / "povs"
        corpus_dir = trial_dir / "output" / "corpus"
        corpus_dir.mkdir(parents=True)
        povs_dir.mkdir(parents=True)

        # Set crs_start_time to far future so file mtime is before it
        pov_store = povs_dir / "pov_store.json"
        pov_store.write_text(json.dumps({"crs_run_start_time": 9999999999.0}))

        metadata = trial_dir / "metadata.json"
        metadata.write_text(
            json.dumps({"benchmark_name": "test-bench", "harness_name": "test-harness"})
        )

        # Create corpus file (mtime will be "now", which is before crs_start)
        file1 = corpus_dir / "file1"
        file1.write_bytes(b"content")

        benchmarks_dir = tmp_path / "benchmarks"
        benchmark_dir = benchmarks_dir / "test-bench"
        benchmark_dir.mkdir(parents=True)

        collector = CorpusCollector(experiment_dir, benchmarks_dir)
        result = collector.collect()

        # File should be skipped due to negative relative time
        assert result.total_files == 0
        assert any("negative relative time" in w for w in result.warnings)
