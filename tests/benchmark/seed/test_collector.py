"""Tests for the corpus collector and seed preparer."""

import json
import os
import time
from pathlib import Path

import pytest
from crsbench.benchmark.seed.collector import (
    CollectionResult,
    CorpusCollector,
    CorpusFile,
    TrialSource,
    _compute_hash,
)
from crsbench.benchmark.seed.preparer import SeedCorpusPreparer

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


def _make_trial(
    experiment_dir: Path,
    *,
    project: str,
    harness: str,
    mode: str = "delta",
    sanitizer: str = "address",
    trial_index: int = 1,
    crs_start_time: float = 1000.0,
    seed_files: dict[str, tuple[bytes, float]] | None = None,
) -> Path:
    """Create a trial directory in the canonical layout.

    ``seed_files`` maps file name to ``(content, mtime_absolute)``.
    """
    trial_dir = (
        experiment_dir / project / harness / mode / sanitizer / f"trial-{trial_index}"
    )
    seeds_dir = trial_dir / "output" / "seeds"
    seeds_dir.mkdir(parents=True)
    povs_dir = trial_dir / "povs"
    povs_dir.mkdir(parents=True)

    (povs_dir / "pov_store.json").write_text(
        json.dumps({"crs_run_start_time": crs_start_time})
    )
    (trial_dir / "metadata.json").write_text(
        json.dumps({"benchmark_name": project, "harness_name": harness})
    )

    if seed_files:
        for name, (content, mtime) in seed_files.items():
            path = seeds_dir / name
            path.write_bytes(content)
            os.utime(path, (mtime, mtime))
    return trial_dir


def _make_benchmark(benchmarks_dir: Path, project: str) -> Path:
    benchmark_dir = benchmarks_dir / project
    benchmark_dir.mkdir(parents=True)
    return benchmark_dir


# ---------------------------------------------------------------------------
# Simple unit behavior
# ---------------------------------------------------------------------------


def test_compute_hash_is_deterministic_and_truncated(tmp_path: Path):
    path = tmp_path / "seed"
    path.write_bytes(b"abc")
    first = _compute_hash(path)
    second = _compute_hash(path)
    assert first == second
    assert len(first) == 16


def test_compute_hash_differs_by_content(tmp_path: Path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.write_bytes(b"A")
    b.write_bytes(b"B")
    assert _compute_hash(a) != _compute_hash(b)


# ---------------------------------------------------------------------------
# Single (benchmark, harness) — single trial
# ---------------------------------------------------------------------------


class TestSingleTrial:
    def test_imports_into_corpus_directory(self, tmp_path: Path):
        experiment_dir = tmp_path / "experiment"
        benchmarks_dir = tmp_path / "benchmarks"
        _make_benchmark(benchmarks_dir, "bench")

        mtime = time.time() + 100
        _make_trial(
            experiment_dir,
            project="bench",
            harness="h1",
            crs_start_time=1000.0,
            seed_files={
                "alpha": (b"content-1", mtime),
                "beta": (b"content-2", mtime + 10),
            },
        )

        collector = CorpusCollector(experiment_dir, benchmarks_dir)
        [result] = collector.collect()

        assert result.benchmark_name == "bench"
        assert result.harness_name == "h1"
        assert result.total_files == 2
        assert result.new_files == 2
        assert result.source_trials == 1

        corpus_dir = benchmarks_dir / "bench" / ".aixcc" / "h1" / "corpus"
        assert corpus_dir.is_dir()
        # Old destination must NOT be populated.
        assert not (benchmarks_dir / "bench" / ".aixcc" / "h1" / "seeds").exists()

        files = {p.name for p in corpus_dir.iterdir() if p.name != "manifest.json"}
        assert len(files) == 2

        manifest = json.loads((corpus_dir / "manifest.json").read_text())
        assert manifest["total_files"] == 2
        assert len(manifest["source_trials"]) == 1
        for entry in manifest["files"].values():
            assert "size" in entry
            assert "original_names" in entry

    def test_skips_negative_relative_time_with_warning(self, tmp_path: Path):
        experiment_dir = tmp_path / "experiment"
        benchmarks_dir = tmp_path / "benchmarks"
        _make_benchmark(benchmarks_dir, "bench")

        _make_trial(
            experiment_dir,
            project="bench",
            harness="h1",
            # crs_start_time in the far future so file mtime (= now) is before.
            crs_start_time=9_999_999_999.0,
            seed_files={"only": (b"x", time.time())},
        )

        collector = CorpusCollector(experiment_dir, benchmarks_dir)
        [result] = collector.collect()

        assert result.total_files == 0
        assert any("negative relative time" in w for w in result.warnings)

    def test_hidden_files_skipped(self, tmp_path: Path):
        experiment_dir = tmp_path / "experiment"
        benchmarks_dir = tmp_path / "benchmarks"
        _make_benchmark(benchmarks_dir, "bench")

        mtime = time.time() + 100
        trial_dir = _make_trial(
            experiment_dir,
            project="bench",
            harness="h1",
            crs_start_time=1000.0,
            seed_files={"visible": (b"v", mtime)},
        )
        hidden = trial_dir / "output" / "seeds" / ".hidden"
        hidden.write_bytes(b"h")
        os.utime(hidden, (mtime, mtime))

        collector = CorpusCollector(experiment_dir, benchmarks_dir)
        [result] = collector.collect()
        assert result.total_files == 1


# ---------------------------------------------------------------------------
# Multiple trials for the same (benchmark, harness)
# ---------------------------------------------------------------------------


class TestMultiTrialAggregation:
    def test_aggregates_across_trials_with_dedup(self, tmp_path: Path):
        experiment_dir = tmp_path / "experiment"
        benchmarks_dir = tmp_path / "benchmarks"
        _make_benchmark(benchmarks_dir, "bench")

        mtime = time.time() + 100
        _make_trial(
            experiment_dir,
            project="bench",
            harness="h1",
            trial_index=1,
            crs_start_time=1000.0,
            seed_files={
                "file-a": (b"shared", mtime),
                "file-b": (b"only-in-1", mtime),
            },
        )
        _make_trial(
            experiment_dir,
            project="bench",
            harness="h1",
            trial_index=2,
            crs_start_time=1100.0,
            seed_files={
                "file-a2": (b"shared", mtime + 10),
                "file-c": (b"only-in-2", mtime + 20),
            },
        )

        collector = CorpusCollector(experiment_dir, benchmarks_dir)
        [result] = collector.collect()

        assert result.source_trials == 2
        # shared + only-in-1 + only-in-2 = 3 unique files
        assert result.total_files == 3
        assert result.new_files == 3

        corpus_dir = benchmarks_dir / "bench" / ".aixcc" / "h1" / "corpus"
        manifest = json.loads((corpus_dir / "manifest.json").read_text())
        assert manifest["total_files"] == 3
        # The shared seed surfaces under two original names.
        names_by_entry = [
            entry["original_names"] for entry in manifest["files"].values()
        ]
        assert any(sorted(names) == ["file-a", "file-a2"] for names in names_by_entry)


# ---------------------------------------------------------------------------
# --all mode (multiple benchmark/harness pairs in one tree)
# ---------------------------------------------------------------------------


class TestAllMode:
    def test_all_imports_every_pair(self, tmp_path: Path):
        experiment_dir = tmp_path / "combined"
        benchmarks_dir = tmp_path / "benchmarks"
        _make_benchmark(benchmarks_dir, "proj-a")
        _make_benchmark(benchmarks_dir, "proj-b")

        mtime = time.time() + 100
        _make_trial(
            experiment_dir,
            project="proj-a",
            harness="h1",
            crs_start_time=1000.0,
            seed_files={"a1": (b"a1", mtime)},
        )
        _make_trial(
            experiment_dir,
            project="proj-a",
            harness="h2",
            crs_start_time=1000.0,
            seed_files={"a2": (b"a2", mtime)},
        )
        _make_trial(
            experiment_dir,
            project="proj-b",
            harness="h1",
            crs_start_time=1000.0,
            seed_files={"b1": (b"b1", mtime)},
        )

        collector = CorpusCollector(experiment_dir, benchmarks_dir)
        results = collector.collect(all_mode=True)

        grouped = {(r.benchmark_name, r.harness_name): r for r in results}
        assert set(grouped.keys()) == {
            ("proj-a", "h1"),
            ("proj-a", "h2"),
            ("proj-b", "h1"),
        }
        for result in results:
            assert result.total_files == 1
            assert result.output_dir.exists()

    def test_multiple_pairs_without_all_raises(self, tmp_path: Path):
        experiment_dir = tmp_path / "combined"
        benchmarks_dir = tmp_path / "benchmarks"
        _make_benchmark(benchmarks_dir, "proj-a")
        _make_benchmark(benchmarks_dir, "proj-b")

        mtime = time.time() + 100
        _make_trial(
            experiment_dir,
            project="proj-a",
            harness="h1",
            crs_start_time=1000.0,
            seed_files={"a1": (b"a1", mtime)},
        )
        _make_trial(
            experiment_dir,
            project="proj-b",
            harness="h1",
            crs_start_time=1000.0,
            seed_files={"b1": (b"b1", mtime)},
        )

        collector = CorpusCollector(experiment_dir, benchmarks_dir)
        with pytest.raises(ValueError, match="Found multiple benchmark/harness pairs"):
            collector.collect()

    def test_benchmark_and_harness_filters(self, tmp_path: Path):
        experiment_dir = tmp_path / "combined"
        benchmarks_dir = tmp_path / "benchmarks"
        _make_benchmark(benchmarks_dir, "proj-a")
        _make_benchmark(benchmarks_dir, "proj-b")

        mtime = time.time() + 100
        _make_trial(
            experiment_dir,
            project="proj-a",
            harness="h1",
            crs_start_time=1000.0,
            seed_files={"a1": (b"a1", mtime)},
        )
        _make_trial(
            experiment_dir,
            project="proj-a",
            harness="h2",
            crs_start_time=1000.0,
            seed_files={"a2": (b"a2", mtime)},
        )
        _make_trial(
            experiment_dir,
            project="proj-b",
            harness="h1",
            crs_start_time=1000.0,
            seed_files={"b1": (b"b1", mtime)},
        )

        collector = CorpusCollector(experiment_dir, benchmarks_dir)
        results = collector.collect(
            all_mode=True, benchmark_filter="proj-a", harness_filter="h1"
        )
        assert len(results) == 1
        assert results[0].benchmark_name == "proj-a"
        assert results[0].harness_name == "h1"


# ---------------------------------------------------------------------------
# Merge vs force
# ---------------------------------------------------------------------------


class TestMergeAndForce:
    def test_default_merges_into_existing(self, tmp_path: Path):
        experiment_dir = tmp_path / "experiment"
        benchmarks_dir = tmp_path / "benchmarks"
        _make_benchmark(benchmarks_dir, "bench")

        # Seed an existing corpus/ directory that pre-dates this import.
        preexisting = benchmarks_dir / "bench" / ".aixcc" / "h1" / "corpus"
        preexisting.mkdir(parents=True)
        (preexisting / "keepme").write_bytes(b"preserved")
        (preexisting / "manifest.json").write_text(
            json.dumps(
                {
                    "total_files": 1,
                    "source_trials": [],
                    "updated_at": "2020-01-01",
                    "files": {
                        "keepme": {
                            "size": 9,
                            "original_names": ["legacy"],
                            "first_trial": "manual",
                        }
                    },
                }
            )
        )

        mtime = time.time() + 100
        _make_trial(
            experiment_dir,
            project="bench",
            harness="h1",
            crs_start_time=1000.0,
            seed_files={"fresh": (b"new-stuff", mtime)},
        )

        collector = CorpusCollector(experiment_dir, benchmarks_dir)
        [result] = collector.collect()

        # Pre-existing file kept, new file added.
        assert (preexisting / "keepme").exists()
        assert result.total_files == 2
        assert result.new_files == 1

    def test_force_replaces_existing(self, tmp_path: Path):
        experiment_dir = tmp_path / "experiment"
        benchmarks_dir = tmp_path / "benchmarks"
        _make_benchmark(benchmarks_dir, "bench")

        preexisting = benchmarks_dir / "bench" / ".aixcc" / "h1" / "corpus"
        preexisting.mkdir(parents=True)
        (preexisting / "keepme").write_bytes(b"preserved")
        (preexisting / "manifest.json").write_text("{}")

        mtime = time.time() + 100
        _make_trial(
            experiment_dir,
            project="bench",
            harness="h1",
            crs_start_time=1000.0,
            seed_files={"fresh": (b"new-stuff", mtime)},
        )

        collector = CorpusCollector(experiment_dir, benchmarks_dir)
        [result] = collector.collect(force=True)

        assert not (preexisting / "keepme").exists()
        assert result.total_files == 1
        assert result.new_files == 1


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------


class TestDryRun:
    def test_dry_run_does_not_create_corpus_dir(self, tmp_path: Path):
        experiment_dir = tmp_path / "experiment"
        benchmarks_dir = tmp_path / "benchmarks"
        _make_benchmark(benchmarks_dir, "bench")

        mtime = time.time() + 100
        _make_trial(
            experiment_dir,
            project="bench",
            harness="h1",
            crs_start_time=1000.0,
            seed_files={
                "alpha": (b"A", mtime),
                "beta": (b"B", mtime + 10),
            },
        )

        collector = CorpusCollector(experiment_dir, benchmarks_dir)
        [result] = collector.collect(dry_run=True)

        assert result.new_files == 2
        assert result.total_files == 2
        corpus_dir = benchmarks_dir / "bench" / ".aixcc" / "h1" / "corpus"
        assert not corpus_dir.exists()

    def test_dry_run_merge_counts_only_new_hashes(self, tmp_path: Path):
        import hashlib

        experiment_dir = tmp_path / "experiment"
        benchmarks_dir = tmp_path / "benchmarks"
        _make_benchmark(benchmarks_dir, "bench")

        existing_content = b"already-here"
        existing_hash = hashlib.sha256(existing_content).hexdigest()[:16]
        corpus_dir = benchmarks_dir / "bench" / ".aixcc" / "h1" / "corpus"
        corpus_dir.mkdir(parents=True)
        (corpus_dir / existing_hash).write_bytes(existing_content)
        (corpus_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "total_files": 1,
                    "source_trials": [],
                    "updated_at": "2020-01-01",
                    "files": {
                        existing_hash: {
                            "size": len(existing_content),
                            "original_names": ["legacy"],
                            "first_trial": "manual",
                        }
                    },
                }
            )
        )

        mtime = time.time() + 100
        _make_trial(
            experiment_dir,
            project="bench",
            harness="h1",
            crs_start_time=1000.0,
            seed_files={
                "dup": (existing_content, mtime),  # already present
                "fresh": (b"brand-new", mtime + 5),  # genuinely new
            },
        )

        collector = CorpusCollector(experiment_dir, benchmarks_dir)
        [result] = collector.collect(dry_run=True)

        assert result.new_files == 1
        assert result.total_files == 2
        # Nothing written: pre-existing file intact, manifest untouched, no new files.
        on_disk = {p.name for p in corpus_dir.iterdir() if p.name != "manifest.json"}
        assert on_disk == {existing_hash}

    def test_dry_run_force_leaves_disk_intact(self, tmp_path: Path):
        experiment_dir = tmp_path / "experiment"
        benchmarks_dir = tmp_path / "benchmarks"
        _make_benchmark(benchmarks_dir, "bench")

        corpus_dir = benchmarks_dir / "bench" / ".aixcc" / "h1" / "corpus"
        corpus_dir.mkdir(parents=True)
        (corpus_dir / "keepme").write_bytes(b"preserved")
        (corpus_dir / "manifest.json").write_text("{}")

        mtime = time.time() + 100
        _make_trial(
            experiment_dir,
            project="bench",
            harness="h1",
            crs_start_time=1000.0,
            seed_files={"fresh": (b"brand-new", mtime)},
        )

        collector = CorpusCollector(experiment_dir, benchmarks_dir)
        [result] = collector.collect(dry_run=True, force=True)

        # With --force the on-disk file is ignored, so the new seed is counted.
        assert result.new_files == 1
        assert result.total_files == 1
        # Dry-run + force must still leave the pre-existing directory untouched.
        assert (corpus_dir / "keepme").exists()


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------


class TestFailureModes:
    def test_no_trials_raises(self, tmp_path: Path):
        experiment_dir = tmp_path / "empty"
        experiment_dir.mkdir()
        benchmarks_dir = tmp_path / "benchmarks"
        benchmarks_dir.mkdir()
        collector = CorpusCollector(experiment_dir, benchmarks_dir)
        with pytest.raises(FileNotFoundError):
            collector.collect()

    def test_missing_benchmark_raises(self, tmp_path: Path):
        experiment_dir = tmp_path / "experiment"
        benchmarks_dir = tmp_path / "benchmarks"
        benchmarks_dir.mkdir()

        mtime = time.time() + 100
        _make_trial(
            experiment_dir,
            project="bench-missing",
            harness="h1",
            crs_start_time=1000.0,
            seed_files={"x": (b"x", mtime)},
        )

        collector = CorpusCollector(experiment_dir, benchmarks_dir)
        with pytest.raises(FileNotFoundError, match="Benchmark not found"):
            collector.collect()


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


class TestDataclasses:
    def test_corpus_file(self):
        cf = CorpusFile(
            path=Path("/tmp/x"), content_hash="abc", size=3, relative_time=1.0
        )
        assert cf.content_hash == "abc"
        assert cf.relative_time == 1.0

    def test_trial_source(self):
        ts = TrialSource(
            trial_dir=Path("/tmp/trial-1"),
            seeds_dir=Path("/tmp/trial-1/output/seeds"),
            benchmark="b",
            harness="h",
            crs_start_time=1.0,
        )
        assert ts.benchmark == "b"

    def test_collection_result_defaults(self):
        result = CollectionResult(
            benchmark_name="b",
            harness_name="h",
            total_files=0,
            new_files=0,
            source_trials=0,
            output_dir=Path("/tmp"),
        )
        assert result.warnings == []


# ---------------------------------------------------------------------------
# SeedCorpusPreparer (now reads from .../corpus/)
# ---------------------------------------------------------------------------


class TestSeedCorpusPreparer:
    @staticmethod
    def _populate(benchmark_path: Path, harness: str) -> Path:
        corpus_dir = benchmark_path / ".aixcc" / harness / "corpus"
        corpus_dir.mkdir(parents=True)
        (corpus_dir / "hash1").write_bytes(b"content1")
        (corpus_dir / "hash2").write_bytes(b"content2")
        (corpus_dir / "hash3").write_bytes(b"content3")
        (corpus_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "total_files": 3,
                    "source_trials": [],
                    "updated_at": "2024-01-01",
                    "files": {
                        "hash1": {
                            "size": 8,
                            "relative_time": 100.0,
                            "original_names": ["file1"],
                        },
                        "hash2": {
                            "size": 8,
                            "relative_time": 500.0,
                            "original_names": ["file2"],
                        },
                        "hash3": {
                            "size": 8,
                            "relative_time": 3600.0,
                            "original_names": ["file3"],
                        },
                    },
                }
            )
        )
        return corpus_dir

    def test_has_seed_corpus_true(self, tmp_path: Path):
        bench = tmp_path / "bench"
        self._populate(bench, "h1")
        assert SeedCorpusPreparer(bench, "h1").has_seed_corpus() is True

    def test_has_seed_corpus_false_missing(self, tmp_path: Path):
        bench = tmp_path / "bench"
        bench.mkdir()
        assert SeedCorpusPreparer(bench, "h1").has_seed_corpus() is False

    def test_prepare_copies_all_files(self, tmp_path: Path):
        bench = tmp_path / "bench"
        self._populate(bench, "h1")
        out = tmp_path / "out"

        result = SeedCorpusPreparer(bench, "h1").prepare(out)

        assert result.copied_files == 3
        assert (out / "hash1").exists()
        assert (out / "hash2").exists()
        assert (out / "hash3").exists()
        assert not (out / "manifest.json").exists()

    def test_prepare_time_filter(self, tmp_path: Path):
        bench = tmp_path / "bench"
        self._populate(bench, "h1")
        out = tmp_path / "out"

        result = SeedCorpusPreparer(bench, "h1").prepare(out, max_time=600)
        assert result.copied_files == 2
        assert (out / "hash1").exists()
        assert (out / "hash2").exists()
        assert not (out / "hash3").exists()
