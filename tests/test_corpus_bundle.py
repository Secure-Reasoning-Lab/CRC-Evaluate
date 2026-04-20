"""Tests for corpus-aware bundling and source fingerprinting."""

from __future__ import annotations

import tarfile
from typing import TYPE_CHECKING

from crsbench.dataset.bundle import (
    BENCHMARK_ARCHIVE,
    CORPUS_ARCHIVE,
    GROUND_TRUTH_ARCHIVE,
    bundle_benchmark,
    unbundle_benchmark,
)
from crsbench.dataset.manifest import compute_source_fingerprints

if TYPE_CHECKING:
    from pathlib import Path


def _make_benchmark(tmp_path: Path) -> Path:
    """Build a benchmark tree exercising all three archive buckets."""
    bench = tmp_path / "afc-bench"
    bench.mkdir()

    # Project files -> benchmark.tar.gz
    (bench / "Dockerfile").write_text("FROM scratch\n")
    (bench / "build.sh").write_text("#!/bin/sh\n")

    # Ground truth (minus corpus/) -> ground-truth.tar.gz
    aixcc = bench / ".aixcc"
    (aixcc / "harness-a").mkdir(parents=True)
    (aixcc / "meta.yaml").write_text("name: afc-bench\n")
    (aixcc / "harness-a" / "hints.txt").write_text("ground-truth-hint\n")

    # Per-harness corpus -> corpus.tar.gz (must NOT land in ground-truth.tar.gz)
    corpus_a = aixcc / "harness-a" / "corpus"
    corpus_a.mkdir()
    (corpus_a / "seed-1").write_bytes(b"seed-1-content")
    (corpus_a / "manifest.json").write_text('{"total_files": 1}\n')

    # A second harness with only ground truth (no corpus).
    (aixcc / "harness-b").mkdir()
    (aixcc / "harness-b" / "hints.txt").write_text("other-hint\n")

    return bench


class TestBundleBenchmark:
    def test_creates_all_three_archives(self, tmp_path: Path):
        bench = _make_benchmark(tmp_path)
        staging = tmp_path / "staging"
        archives = bundle_benchmark(bench, staging)

        names = {p.name for p in archives}
        assert names == {BENCHMARK_ARCHIVE, GROUND_TRUTH_ARCHIVE, CORPUS_ARCHIVE}

    def test_ground_truth_archive_excludes_corpus(self, tmp_path: Path):
        bench = _make_benchmark(tmp_path)
        staging = tmp_path / "staging"
        bundle_benchmark(bench, staging)

        with tarfile.open(staging / GROUND_TRUTH_ARCHIVE, "r:gz") as tar:
            members = tar.getnames()

        # Ground truth carries .aixcc/, but the corpus subdir must be absent.
        assert any(name.startswith(".aixcc/") for name in members)
        assert not any("/corpus" in name for name in members)

    def test_corpus_archive_contains_only_corpus_dirs(self, tmp_path: Path):
        bench = _make_benchmark(tmp_path)
        staging = tmp_path / "staging"
        bundle_benchmark(bench, staging)

        with tarfile.open(staging / CORPUS_ARCHIVE, "r:gz") as tar:
            members = [m for m in tar.getnames() if m.strip()]

        # Every entry lives under .aixcc/<harness>/corpus/.
        for name in members:
            parts = name.split("/")
            assert parts[0] == ".aixcc"
            assert parts[2] == "corpus"

    def test_roundtrip_restores_layout(self, tmp_path: Path):
        bench = _make_benchmark(tmp_path)
        staging = tmp_path / "staging"
        bundle_benchmark(bench, staging)

        restored = tmp_path / "restored"
        unbundle_benchmark(staging, restored)

        # Top-level project files.
        assert (restored / "Dockerfile").read_text() == "FROM scratch\n"
        assert (restored / "build.sh").read_text() == "#!/bin/sh\n"

        # Ground truth.
        assert (
            restored / ".aixcc" / "harness-a" / "hints.txt"
        ).read_text() == "ground-truth-hint\n"
        assert (
            restored / ".aixcc" / "harness-b" / "hints.txt"
        ).read_text() == "other-hint\n"

        # Corpus.
        corpus_a = restored / ".aixcc" / "harness-a" / "corpus"
        assert corpus_a.is_dir()
        assert (corpus_a / "seed-1").read_bytes() == b"seed-1-content"
        assert (corpus_a / "manifest.json").read_text() == '{"total_files": 1}\n'

    def test_no_corpus_dir_produces_two_archives(self, tmp_path: Path):
        bench = tmp_path / "noa-corpus"
        bench.mkdir()
        (bench / "Dockerfile").write_text("FROM scratch\n")
        aixcc = bench / ".aixcc"
        aixcc.mkdir()
        (aixcc / "meta.yaml").write_text("name: x\n")

        staging = tmp_path / "staging"
        archives = bundle_benchmark(bench, staging)
        names = {p.name for p in archives}
        assert names == {BENCHMARK_ARCHIVE, GROUND_TRUTH_ARCHIVE}
        assert not (staging / CORPUS_ARCHIVE).exists()


class TestSourceFingerprints:
    def test_three_way_classification(self, tmp_path: Path):
        bench = _make_benchmark(tmp_path)
        fingerprints = compute_source_fingerprints(bench)
        assert fingerprints.benchmark
        assert fingerprints.ground_truth
        assert fingerprints.corpus
        # Distinct buckets yield distinct hashes.
        assert len({*fingerprints}) == 3

    def test_corpus_changes_only_corpus_hash(self, tmp_path: Path):
        bench = _make_benchmark(tmp_path)
        before = compute_source_fingerprints(bench)

        # Mutate the corpus only.
        corpus_file = bench / ".aixcc" / "harness-a" / "corpus" / "seed-1"
        corpus_file.write_bytes(b"mutated")

        after = compute_source_fingerprints(bench)
        assert before.benchmark == after.benchmark
        assert before.ground_truth == after.ground_truth
        assert before.corpus != after.corpus

    def test_ground_truth_changes_only_ground_truth_hash(self, tmp_path: Path):
        bench = _make_benchmark(tmp_path)
        before = compute_source_fingerprints(bench)

        (bench / ".aixcc" / "harness-a" / "hints.txt").write_text("changed\n")

        after = compute_source_fingerprints(bench)
        assert before.benchmark == after.benchmark
        assert before.ground_truth != after.ground_truth
        assert before.corpus == after.corpus
