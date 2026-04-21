"""Unit tests for benchmark POV import from bug-finding CRS output.

Tests for crsbench/benchmark/packaging/import_povs.py.
"""

from pathlib import Path

from crsbench.benchmark.packaging.import_povs import (
    import_benchmark_povs,
    import_cpv_povs,
)


def _make_benchmark(tmp_path: Path, harness: str, cpv: str, pov_0: bytes) -> Path:
    """Create a benchmark with a single existing pov_0 blob."""
    bench = tmp_path / "bm"
    blobs = bench / ".aixcc" / harness / cpv / "blobs"
    blobs.mkdir(parents=True)
    (blobs / "pov_0.blob").write_bytes(pov_0)
    return bench


def _make_source(
    tmp_path: Path,
    harness: str,
    cpv: str,
    trial: str,
    blobs: dict[str, bytes],
    *,
    sanitizer: str = "address",
    crash_logs: dict[str, str] | None = None,
) -> Path:
    """Create a bug-finding source tree for one trial."""
    src = tmp_path / "src"
    base = src / harness / "full" / sanitizer / trial / "povs" / "cpvs" / cpv
    blobs_dir = base / "blobs"
    blobs_dir.mkdir(parents=True)
    for name, data in blobs.items():
        (blobs_dir / name).write_bytes(data)
    if crash_logs:
        logs_dir = base / "crash_logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        for name, text in crash_logs.items():
            (logs_dir / name).write_text(text)
    return src


def test_import_skips_duplicate_of_existing(tmp_path: Path) -> None:
    bench = _make_benchmark(tmp_path, "h1", "cpv_0", b"AAAA")
    src = _make_source(
        tmp_path,
        "h1",
        "cpv_0",
        "trial-1",
        {"a.blob": b"AAAA", "b.blob": b"BBBB"},
    )

    result = import_cpv_povs(bench, src, "h1", "cpv_0")

    assert result.existing_povs == 1
    assert result.candidates_found == 2
    assert result.skipped_duplicate == 1
    assert result.imported_povs == [1]
    assert (bench / ".aixcc/h1/cpv_0/blobs/pov_1.blob").read_bytes() == b"BBBB"
    # pov_0 must be untouched
    assert (bench / ".aixcc/h1/cpv_0/blobs/pov_0.blob").read_bytes() == b"AAAA"


def test_import_dedupes_within_run(tmp_path: Path) -> None:
    bench = _make_benchmark(tmp_path, "h1", "cpv_0", b"AAAA")
    src = _make_source(
        tmp_path,
        "h1",
        "cpv_0",
        "trial-1",
        {"a.blob": b"BBBB", "b.blob": b"BBBB", "c.blob": b"CCCC"},
    )

    result = import_cpv_povs(bench, src, "h1", "cpv_0")

    assert result.candidates_found == 3
    assert result.skipped_duplicate == 1
    assert sorted(result.imported_povs) == [1, 2]


def test_import_respects_max_cap(tmp_path: Path) -> None:
    bench = _make_benchmark(tmp_path, "h1", "cpv_0", b"AAAA")
    src = _make_source(
        tmp_path,
        "h1",
        "cpv_0",
        "trial-1",
        {f"x{i}.blob": bytes([i]) * 8 for i in range(1, 11)},
    )

    result = import_cpv_povs(bench, src, "h1", "cpv_0", max_new_povs=3)

    assert result.candidates_found == 10
    assert len(result.imported_povs) == 3
    assert result.skipped_capacity == 7


def test_import_dry_run_does_not_write(tmp_path: Path) -> None:
    bench = _make_benchmark(tmp_path, "h1", "cpv_0", b"AAAA")
    src = _make_source(
        tmp_path,
        "h1",
        "cpv_0",
        "trial-1",
        {"a.blob": b"BBBB"},
    )

    result = import_cpv_povs(bench, src, "h1", "cpv_0", dry_run=True)

    assert result.imported_povs == [1]
    assert not (bench / ".aixcc/h1/cpv_0/blobs/pov_1.blob").exists()


def test_import_aggregates_across_trials_and_sanitizers(tmp_path: Path) -> None:
    bench = _make_benchmark(tmp_path, "h1", "cpv_0", b"AAAA")
    src = tmp_path / "src"
    base = src / "h1" / "full"
    for trial, sanitizer, blobs in [
        ("trial-1", "address", {"a.blob": b"BBBB"}),
        ("trial-2", "address", {"a.blob": b"BBBB", "b.blob": b"CCCC"}),
        ("trial-3", "undefined", {"a.blob": b"DDDD"}),
    ]:
        d = base / sanitizer / trial / "povs" / "cpvs" / "cpv_0" / "blobs"
        d.mkdir(parents=True)
        for name, data in blobs.items():
            (d / name).write_bytes(data)

    result = import_cpv_povs(bench, src, "h1", "cpv_0")

    assert result.candidates_found == 4
    assert result.skipped_duplicate == 1  # b'BBBB' appears in trial-1 and trial-2
    assert sorted(result.imported_povs) == [1, 2, 3]


def test_import_prefers_matching_cpv_log(tmp_path: Path) -> None:
    """For a POV under cpvs/cpv_1/, prefer *-cpv1.log (drop-one build)."""
    bench = _make_benchmark(tmp_path, "h1", "cpv_1", b"AAAA")
    src = _make_source(
        tmp_path,
        "h1",
        "cpv_1",
        "trial-1",
        {"deadbeef.blob": b"BBBB"},
        crash_logs={
            "deadbeef-bm-asan-delta-allpatched.log": "NO CRASH",
            "deadbeef-bm-asan-delta-cpv0.log": "CPV0 DROP (wrong cpv)",
            "deadbeef-bm-asan-delta-cpv1.log": "CPV1 DROP (correct)",
            "deadbeef-bm-asan-deltaref.log": "VULN CRASH",
        },
    )

    import_cpv_povs(bench, src, "h1", "cpv_1")
    log_path = bench / ".aixcc/h1/cpv_1/logs/pov_1.log"
    assert log_path.read_text() == "CPV1 DROP (correct)"


def test_import_falls_back_to_deltaref_when_no_cpv_log(tmp_path: Path) -> None:
    bench = _make_benchmark(tmp_path, "h1", "cpv_0", b"AAAA")
    src = _make_source(
        tmp_path,
        "h1",
        "cpv_0",
        "trial-1",
        {"deadbeef.blob": b"BBBB"},
        crash_logs={
            "deadbeef-bm-asan-delta-allpatched.log": "NO CRASH",
            "deadbeef-bm-asan-deltaref.log": "VULN CRASH",
        },
    )

    import_cpv_povs(bench, src, "h1", "cpv_0")
    log_path = bench / ".aixcc/h1/cpv_0/logs/pov_1.log"
    assert log_path.read_text() == "VULN CRASH"


def test_import_falls_back_to_fullbase(tmp_path: Path) -> None:
    bench = _make_benchmark(tmp_path, "h1", "cpv_0", b"AAAA")
    src = _make_source(
        tmp_path,
        "h1",
        "cpv_0",
        "trial-1",
        {"deadbeef.blob": b"BBBB"},
        crash_logs={
            "deadbeef-bm-asan-full-allpatched.log": "NO CRASH",
            "deadbeef-bm-asan-fullbase.log": "VULN CRASH",
        },
    )

    import_cpv_povs(bench, src, "h1", "cpv_0")
    log_path = bench / ".aixcc/h1/cpv_0/logs/pov_1.log"
    assert log_path.read_text() == "VULN CRASH"


def test_import_skips_log_when_only_allpatched_or_other_cpv(tmp_path: Path) -> None:
    """No cpv-matching log, no deltaref/fullbase → skip copying log."""
    bench = _make_benchmark(tmp_path, "h1", "cpv_0", b"AAAA")
    src = _make_source(
        tmp_path,
        "h1",
        "cpv_0",
        "trial-1",
        {"deadbeef.blob": b"BBBB"},
        crash_logs={
            "deadbeef-bm-asan-full-allpatched.log": "NO CRASH",
            "deadbeef-bm-asan-full-cpv1.log": "CPV1 DROP (different cpv)",
        },
    )

    import_cpv_povs(bench, src, "h1", "cpv_0")
    assert not (bench / ".aixcc/h1/cpv_0/logs/pov_1.log").exists()


def test_import_no_logs_flag(tmp_path: Path) -> None:
    bench = _make_benchmark(tmp_path, "h1", "cpv_0", b"AAAA")
    src = _make_source(
        tmp_path,
        "h1",
        "cpv_0",
        "trial-1",
        {"deadbeef.blob": b"BBBB"},
        crash_logs={"deadbeef-bm-asan-delta-cpv0.log": "CRASH"},
    )

    import_cpv_povs(bench, src, "h1", "cpv_0", copy_logs=False)
    assert not (bench / ".aixcc/h1/cpv_0/logs/pov_1.log").exists()


def test_import_preserves_existing_pov_numbering(tmp_path: Path) -> None:
    bench = _make_benchmark(tmp_path, "h1", "cpv_0", b"AAAA")
    blobs = bench / ".aixcc/h1/cpv_0/blobs"
    (blobs / "pov_5.blob").write_bytes(b"EEEE")

    src = _make_source(
        tmp_path,
        "h1",
        "cpv_0",
        "trial-1",
        {"a.blob": b"BBBB"},
    )

    result = import_cpv_povs(bench, src, "h1", "cpv_0")

    # next_pov_num should be max existing (5) + 1 = 6
    assert result.imported_povs == [6]
    assert (bench / ".aixcc/h1/cpv_0/blobs/pov_6.blob").read_bytes() == b"BBBB"


def test_import_benchmark_processes_all_cpvs(tmp_path: Path) -> None:
    bench = tmp_path / "bm"
    for cpv in ("cpv_0", "cpv_1"):
        d = bench / ".aixcc/h1" / cpv / "blobs"
        d.mkdir(parents=True)
        (d / "pov_0.blob").write_bytes(b"X")

    src = tmp_path / "src"
    for cpv, payload in (("cpv_0", b"NEW0"), ("cpv_1", b"NEW1")):
        d = src / "h1/full/address/trial-1/povs/cpvs" / cpv / "blobs"
        d.mkdir(parents=True)
        (d / "a.blob").write_bytes(payload)

    summary = import_benchmark_povs(bench, src)

    by_cpv = {r.cpv: r for r in summary.cpv_results}
    assert by_cpv["cpv_0"].imported_povs == [1]
    assert by_cpv["cpv_1"].imported_povs == [1]
    assert summary.total_imported == 2


def test_import_filter_by_harness_and_cpv(tmp_path: Path) -> None:
    bench = tmp_path / "bm"
    for harness, cpv in (("h1", "cpv_0"), ("h2", "cpv_0")):
        d = bench / ".aixcc" / harness / cpv / "blobs"
        d.mkdir(parents=True)
        (d / "pov_0.blob").write_bytes(b"X")

    src = tmp_path / "src"
    for harness in ("h1", "h2"):
        d = src / harness / "full/address/trial-1/povs/cpvs/cpv_0/blobs"
        d.mkdir(parents=True)
        (d / "a.blob").write_bytes(f"NEW-{harness}".encode())

    summary = import_benchmark_povs(bench, src, harness_filter="h2")
    assert {r.harness for r in summary.cpv_results} == {"h2"}
