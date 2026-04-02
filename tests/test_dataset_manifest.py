"""Unit tests for dataset manifest and incremental sync planning."""

from pathlib import Path

from crsbench.dataset.download import _plan_download
from crsbench.dataset.manifest import (
    BenchmarkManifestEntry,
    compute_source_fingerprints,
    load_local_manifest,
    load_remote_index,
    write_local_manifest,
    write_remote_index,
)
from crsbench.dataset.upload import _plan_upload


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def test_compute_source_fingerprints_split_hashes(tmp_path: Path) -> None:
    benchmark_dir = tmp_path / "afc-curl-delta-01"
    _write(benchmark_dir / "Dockerfile", "FROM scratch\n")
    _write(benchmark_dir / ".aixcc" / "meta.yaml", "id: 1\n")

    benchmark_hash_1, ground_truth_hash_1 = compute_source_fingerprints(benchmark_dir)

    # Project-file change should affect benchmark hash only.
    _write(benchmark_dir / "Dockerfile", "FROM ubuntu:22.04\n")
    benchmark_hash_2, ground_truth_hash_2 = compute_source_fingerprints(benchmark_dir)
    assert benchmark_hash_2 != benchmark_hash_1
    assert ground_truth_hash_2 == ground_truth_hash_1

    # Ground-truth change should affect ground-truth hash.
    _write(benchmark_dir / ".aixcc" / "meta.yaml", "id: 2\n")
    benchmark_hash_3, ground_truth_hash_3 = compute_source_fingerprints(benchmark_dir)
    assert benchmark_hash_3 == benchmark_hash_2
    assert ground_truth_hash_3 != ground_truth_hash_2


def test_manifest_roundtrip_remote_and_local(tmp_path: Path) -> None:
    entry = BenchmarkManifestEntry(
        benchmark="afc-curl-delta-01",
        benchmark_source_sha256="bench-hash",
        ground_truth_source_sha256="gt-hash",
        has_ground_truth=True,
    )
    data = {entry.benchmark: entry}

    remote_path = tmp_path / "index" / "benchmarks.jsonl"
    write_remote_index(remote_path, data)
    loaded_remote = load_remote_index(remote_path)
    assert loaded_remote[entry.benchmark].to_dict() == entry.to_dict()

    output_dir = tmp_path / "benchmarks"
    write_local_manifest(output_dir, data)
    loaded_local = load_local_manifest(output_dir)
    assert loaded_local[entry.benchmark].to_dict() == entry.to_dict()


def test_manifest_from_dict_coercion() -> None:
    entry = BenchmarkManifestEntry.from_dict(
        {
            "benchmark": None,
            "benchmark_source_sha256": "abc",
            "ground_truth_source_sha256": "def",
            "benchmark_archive_size": "123",
            "ground_truth_archive_size": "not-a-number",
            "has_ground_truth": "true",
        }
    )

    assert entry.benchmark == ""
    assert entry.benchmark_source_sha256 == "abc"
    assert entry.ground_truth_source_sha256 == "def"
    assert entry.benchmark_archive_size == 123
    assert entry.ground_truth_archive_size == 0
    assert entry.has_ground_truth is True

    entry_false = BenchmarkManifestEntry.from_dict(
        {
            "benchmark": "x",
            "benchmark_source_sha256": "a",
            "ground_truth_source_sha256": "b",
            "has_ground_truth": "false",
        }
    )
    assert entry_false.has_ground_truth is False


def test_plan_upload_skips_unchanged(tmp_path: Path) -> None:
    unchanged_dir = tmp_path / "afc-curl-delta-01"
    changed_dir = tmp_path / "afc-libxml2-delta-01"
    _write(unchanged_dir / "Dockerfile", "FROM scratch\n")
    _write(changed_dir / "Dockerfile", "FROM scratch\n")

    unchanged_bench, unchanged_gt = compute_source_fingerprints(unchanged_dir)
    changed_bench, changed_gt = compute_source_fingerprints(changed_dir)
    remote_manifest = {
        "afc-curl-delta-01": BenchmarkManifestEntry(
            benchmark="afc-curl-delta-01",
            benchmark_source_sha256=unchanged_bench,
            ground_truth_source_sha256=unchanged_gt,
            benchmark_archive_sha256="archive-hash",
            benchmark_archive_size=123,
        ),
        "afc-libxml2-delta-01": BenchmarkManifestEntry(
            benchmark="afc-libxml2-delta-01",
            benchmark_source_sha256="stale-bench",
            ground_truth_source_sha256=changed_gt,
        ),
    }

    remote_files = {
        "afc-curl-delta-01/benchmark.tar.gz",
        "afc-libxml2-delta-01/benchmark.tar.gz",
    }
    updates, changed = _plan_upload(
        [unchanged_dir, changed_dir],
        remote_manifest,
        remote_files,
    )
    assert [p.name for p in changed] == ["afc-libxml2-delta-01"]
    assert updates["afc-curl-delta-01"].benchmark_archive_sha256 == "archive-hash"
    assert updates["afc-libxml2-delta-01"].benchmark_source_sha256 == changed_bench


def test_plan_upload_marks_changed_when_remote_archives_missing(tmp_path: Path) -> None:
    benchmark_dir = tmp_path / "afc-curl-delta-01"
    _write(benchmark_dir / "Dockerfile", "FROM scratch\n")

    benchmark_hash, ground_truth_hash = compute_source_fingerprints(benchmark_dir)
    remote_manifest = {
        benchmark_dir.name: BenchmarkManifestEntry(
            benchmark=benchmark_dir.name,
            benchmark_source_sha256=benchmark_hash,
            ground_truth_source_sha256=ground_truth_hash,
        )
    }

    # Manifest says same hash, but remote archive is missing.
    updates, changed = _plan_upload([benchmark_dir], remote_manifest, set())
    assert [p.name for p in changed] == [benchmark_dir.name]
    assert updates[benchmark_dir.name].benchmark_source_sha256 == benchmark_hash


def test_plan_download_skips_when_local_is_current(tmp_path: Path) -> None:
    output_dir = tmp_path / "benchmarks"
    benchmark_name = "afc-curl-delta-01"
    benchmark_dir = output_dir / benchmark_name
    _write(benchmark_dir / "Dockerfile", "FROM scratch\n")
    _write(benchmark_dir / ".aixcc" / "meta.yaml", "id: 1\n")

    remote_entry = BenchmarkManifestEntry(
        benchmark=benchmark_name,
        benchmark_source_sha256="bench-hash",
        ground_truth_source_sha256="gt-hash",
        has_ground_truth=True,
    )
    remote = {benchmark_name: remote_entry}
    local = {
        benchmark_name: BenchmarkManifestEntry(
            benchmark=benchmark_name,
            benchmark_source_sha256="bench-hash",
            ground_truth_source_sha256="gt-hash",
            has_ground_truth=True,
        )
    }

    plan = _plan_download(
        requested=[benchmark_name],
        remote_manifest=remote,
        local_manifest=local,
        output_dir=output_dir,
        no_ground_truth=False,
    )
    assert plan.download == []
    assert plan.skipped == [benchmark_name]

    # Missing .aixcc requires redownload when ground truth is requested.
    (benchmark_dir / ".aixcc").rename(benchmark_dir / ".aixcc.bak")
    plan_missing_gt = _plan_download(
        requested=[benchmark_name],
        remote_manifest=remote,
        local_manifest=local,
        output_dir=output_dir,
        no_ground_truth=False,
    )
    assert plan_missing_gt.download == [benchmark_name]

    # But it can still be skipped for --no-ground-truth downloads.
    plan_no_gt = _plan_download(
        requested=[benchmark_name],
        remote_manifest=remote,
        local_manifest=local,
        output_dir=output_dir,
        no_ground_truth=True,
    )
    assert plan_no_gt.download == []


def test_plan_download_redownloads_existing_dirs_without_local_manifest(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "benchmarks"
    benchmark_name = "afc-libxml2-delta-03"
    benchmark_dir = output_dir / benchmark_name
    _write(benchmark_dir / "Dockerfile", "FROM scratch\n")
    _write(benchmark_dir / ".aixcc" / "meta.yaml", "id: 1\n")

    remote = {
        benchmark_name: BenchmarkManifestEntry(
            benchmark=benchmark_name,
            benchmark_source_sha256="bench-hash",
            ground_truth_source_sha256="gt-hash",
            has_ground_truth=True,
        )
    }

    plan = _plan_download(
        requested=[benchmark_name],
        remote_manifest=remote,
        local_manifest={},
        output_dir=output_dir,
        no_ground_truth=False,
    )

    assert plan.download == [benchmark_name]
    assert plan.skipped == []
