"""Unit tests for benchmark POV deduplication.

Tests for crsbench/benchmark/packaging/dedup_povs.py.
"""

from pathlib import Path

import pytest
from crsbench.benchmark.packaging.dedup_povs import (
    DedupResult,
    DedupSummary,
    dedup_benchmark_povs,
    dedup_cpv_povs,
    scan_povs,
)

# Sample ASAN crash logs with distinct signatures
CRASH_LOG_A = """\
==1==ERROR: AddressSanitizer: heap-buffer-overflow on address 0x1
    #0 0xaaa in func_a /src/a.c:10:5
    #1 0xbbb in main /src/main.c:5:3
SUMMARY: AddressSanitizer: heap-buffer-overflow /src/a.c:10:5
"""

CRASH_LOG_B = """\
==1==ERROR: AddressSanitizer: use-after-free on address 0x2
    #0 0xccc in func_b /src/b.c:20:5
    #1 0xddd in main /src/main.c:10:3
SUMMARY: AddressSanitizer: use-after-free /src/b.c:20:5
"""

UNPARSEABLE_LOG = """\
some random output that doesn't match any crash pattern
no frames here at all
"""


def _make_cpv_dir(
    tmp_path: Path,
    harness: str,
    cpv: str,
    pov_logs: dict[int, str],
) -> Path:
    """Create a .aixcc/{harness}/{cpv}/ structure with blobs and logs.

    Args:
        tmp_path: Pytest tmp_path fixture
        harness: Harness name
        cpv: CPV name (e.g., "cpv_0")
        pov_logs: Mapping of pov_num -> log content

    Returns:
        Path to the cpv directory
    """
    cpv_dir = tmp_path / ".aixcc" / harness / cpv
    blobs_dir = cpv_dir / "blobs"
    logs_dir = cpv_dir / "logs"
    blobs_dir.mkdir(parents=True)
    logs_dir.mkdir(parents=True)

    for pov_num, log_content in pov_logs.items():
        (blobs_dir / f"pov_{pov_num}.blob").write_bytes(b"\x00" * 10)
        (logs_dir / f"pov_{pov_num}.log").write_text(log_content)

    return cpv_dir


class TestScanPovs:
    """Tests for scan_povs function."""

    def test_finds_blob_log_pairs(self, tmp_path: Path) -> None:
        cpv_dir = _make_cpv_dir(tmp_path, "h1", "cpv_0", {0: "log0", 1: "log1"})

        entries = scan_povs(cpv_dir)

        assert len(entries) == 2
        assert entries[0].pov_num == 0
        assert entries[0].blob_path.name == "pov_0.blob"
        assert entries[0].log_path is not None
        assert entries[0].log_path.name == "pov_0.log"
        assert entries[1].pov_num == 1

    def test_blob_without_log(self, tmp_path: Path) -> None:
        cpv_dir = tmp_path / ".aixcc" / "h1" / "cpv_0"
        blobs_dir = cpv_dir / "blobs"
        blobs_dir.mkdir(parents=True)
        (blobs_dir / "pov_0.blob").write_bytes(b"\x00")
        # No logs dir at all

        entries = scan_povs(cpv_dir)

        assert len(entries) == 1
        assert entries[0].log_path is None

    def test_empty_dir(self, tmp_path: Path) -> None:
        cpv_dir = tmp_path / ".aixcc" / "h1" / "cpv_0"
        cpv_dir.mkdir(parents=True)

        entries = scan_povs(cpv_dir)

        assert entries == []

    def test_sorted_by_pov_num(self, tmp_path: Path) -> None:
        cpv_dir = _make_cpv_dir(
            tmp_path, "h1", "cpv_0", {10: "log", 2: "log", 0: "log"}
        )

        entries = scan_povs(cpv_dir)

        assert [e.pov_num for e in entries] == [0, 2, 10]


class TestPov0NeverRemoved:
    """Tests that pov_0 (ground truth) is always kept."""

    def test_pov0_always_kept(self, tmp_path: Path) -> None:
        cpv_dir = _make_cpv_dir(
            tmp_path,
            "h1",
            "cpv_0",
            {0: CRASH_LOG_A, 1: CRASH_LOG_A, 2: CRASH_LOG_A},
        )

        result = dedup_cpv_povs(cpv_dir, "h1", "cpv_0")

        assert 0 in result.kept
        assert 0 not in result.removed


class TestSameSignatureDeduped:
    """Tests that POVs with same crash signature are deduplicated."""

    def test_duplicates_removed(self, tmp_path: Path) -> None:
        cpv_dir = _make_cpv_dir(
            tmp_path,
            "h1",
            "cpv_0",
            {0: CRASH_LOG_A, 1: CRASH_LOG_A, 2: CRASH_LOG_A},
        )

        result = dedup_cpv_povs(cpv_dir, "h1", "cpv_0")

        assert result.total_povs == 3
        assert len(result.kept) == 1  # only pov_0
        assert 0 in result.kept
        assert sorted(result.removed) == [1, 2]


class TestDifferentSignaturesKept:
    """Tests that POVs with different crash signatures are all kept."""

    def test_unique_sigs_kept(self, tmp_path: Path) -> None:
        cpv_dir = _make_cpv_dir(
            tmp_path,
            "h1",
            "cpv_0",
            {0: CRASH_LOG_A, 1: CRASH_LOG_B},
        )

        result = dedup_cpv_povs(cpv_dir, "h1", "cpv_0")

        assert len(result.kept) == 2
        assert result.removed == []


class TestUnparseableLogKept:
    """Tests that POVs with unparseable logs are conservatively kept."""

    def test_unparseable_kept(self, tmp_path: Path) -> None:
        cpv_dir = _make_cpv_dir(
            tmp_path,
            "h1",
            "cpv_0",
            {0: CRASH_LOG_A, 1: UNPARSEABLE_LOG},
        )

        result = dedup_cpv_povs(cpv_dir, "h1", "cpv_0")

        assert len(result.kept) == 2
        assert 1 in result.unparseable
        assert 1 not in result.removed

    def test_no_log_file_kept(self, tmp_path: Path) -> None:
        """POV with missing log file is kept."""
        cpv_dir = _make_cpv_dir(tmp_path, "h1", "cpv_0", {0: CRASH_LOG_A})
        # Add blob without log
        blobs_dir = cpv_dir / "blobs"
        (blobs_dir / "pov_5.blob").write_bytes(b"\x00")

        result = dedup_cpv_povs(cpv_dir, "h1", "cpv_0")

        assert 5 in result.kept
        assert 5 in result.unparseable


class TestDryRun:
    """Tests for dry-run vs actual deletion."""

    def test_dry_run_no_deletion(self, tmp_path: Path) -> None:
        cpv_dir = _make_cpv_dir(
            tmp_path,
            "h1",
            "cpv_0",
            {0: CRASH_LOG_A, 1: CRASH_LOG_A},
        )

        result = dedup_cpv_povs(cpv_dir, "h1", "cpv_0", dry_run=True)

        assert len(result.removed) == 1
        # Files still exist
        assert (cpv_dir / "blobs" / "pov_1.blob").exists()
        assert (cpv_dir / "logs" / "pov_1.log").exists()

    def test_actual_deletion(self, tmp_path: Path) -> None:
        cpv_dir = _make_cpv_dir(
            tmp_path,
            "h1",
            "cpv_0",
            {0: CRASH_LOG_A, 1: CRASH_LOG_A, 2: CRASH_LOG_B},
        )

        result = dedup_cpv_povs(cpv_dir, "h1", "cpv_0", dry_run=False)

        assert len(result.removed) == 1
        assert 1 in result.removed
        # pov_1 files deleted
        assert not (cpv_dir / "blobs" / "pov_1.blob").exists()
        assert not (cpv_dir / "logs" / "pov_1.log").exists()
        # pov_0 and pov_2 still exist
        assert (cpv_dir / "blobs" / "pov_0.blob").exists()
        assert (cpv_dir / "blobs" / "pov_2.blob").exists()


class TestTopNValidation:
    """Tests for top_n argument validation."""

    def test_invalid_top_n_raises(self, tmp_path: Path) -> None:
        cpv_dir = _make_cpv_dir(tmp_path, "h1", "cpv_0", {0: CRASH_LOG_A})

        with pytest.raises(ValueError, match="top_n must be > 0"):
            dedup_cpv_povs(cpv_dir, "h1", "cpv_0", top_n=0)


class TestBenchmarkDedup:
    """Tests for dedup_benchmark_povs (full benchmark scan)."""

    def _setup_benchmark(self, tmp_path: Path) -> Path:
        """Create a benchmark with multiple harnesses and CPVs."""
        bench = tmp_path / "test-bench"
        aixcc = bench / ".aixcc"

        # meta.yaml (non-directory, should be skipped)
        aixcc.mkdir(parents=True)
        (aixcc / "meta.yaml").write_text("version: 1")

        # harness1/cpv_0: 3 POVs, 2 duplicates
        _make_cpv_dir(
            bench,
            "harness1",
            "cpv_0",
            {0: CRASH_LOG_A, 1: CRASH_LOG_A, 2: CRASH_LOG_B},
        )

        # harness2/cpv_0: 2 POVs, no duplicates
        _make_cpv_dir(
            bench,
            "harness2",
            "cpv_0",
            {0: CRASH_LOG_A, 1: CRASH_LOG_B},
        )

        return bench

    def test_full_benchmark_scan(self, tmp_path: Path) -> None:
        bench = self._setup_benchmark(tmp_path)

        summary = dedup_benchmark_povs(bench)

        assert summary.benchmark == bench.name
        assert len(summary.cpv_results) == 2
        assert summary.total_povs == 5
        assert summary.total_removed == 1  # only harness1/cpv_0/pov_1

    def test_harness_filter(self, tmp_path: Path) -> None:
        bench = self._setup_benchmark(tmp_path)

        summary = dedup_benchmark_povs(bench, harness_filter="harness1")

        assert len(summary.cpv_results) == 1
        assert summary.cpv_results[0].harness == "harness1"

    def test_cpv_filter(self, tmp_path: Path) -> None:
        bench = self._setup_benchmark(tmp_path)

        # Add a cpv_1 to harness1
        _make_cpv_dir(bench, "harness1", "cpv_1", {0: CRASH_LOG_B})

        summary = dedup_benchmark_povs(bench, cpv_filter="cpv_0")

        # Should only process cpv_0 from each harness
        for r in summary.cpv_results:
            assert r.cpv == "cpv_0"

    def test_no_aixcc_dir_raises(self, tmp_path: Path) -> None:
        bench = tmp_path / "no-bench"
        bench.mkdir()

        with pytest.raises(ValueError, match="No .aixcc directory"):
            dedup_benchmark_povs(bench)


class TestDedupSummary:
    """Tests for DedupSummary serialization."""

    def test_to_dict(self) -> None:
        summary = DedupSummary(
            benchmark="test-bench",
            cpv_results=[
                DedupResult(
                    harness="h1",
                    cpv="cpv_0",
                    total_povs=3,
                    kept=[0, 2],
                    removed=[1],
                    unparseable=[],
                ),
            ],
        )

        d = summary.to_dict()

        assert d["benchmark"] == "test-bench"
        assert d["total_povs"] == 3
        assert d["total_kept"] == 2
        assert d["total_removed"] == 1
        assert len(d["cpv_results"]) == 1

    def test_to_json(self) -> None:
        summary = DedupSummary(benchmark="b", cpv_results=[])

        json_str = summary.to_json()

        assert '"benchmark": "b"' in json_str
