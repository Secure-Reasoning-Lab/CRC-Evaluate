import json
from pathlib import Path

from crsbench.evaluation.replay.discovery import discover_source_povs, make_source_id


def _write_trial(
    source_dir: Path,
    *,
    benchmark: str,
    harness: str,
    trial_name: str,
    povs: dict[str, bytes],
    nested_prefix: tuple[str, ...] = (),
    metadata_sanitizer: str | None = "address",
    mode_dir: str = "full",
    sanitizer_dir: str = "address",
    experiment_name: str | None = None,
    mark_success: bool = True,
    mark_fail: bool = False,
) -> Path:
    trial_dir = (
        source_dir
        / Path(*nested_prefix)
        / benchmark
        / harness
        / mode_dir
        / sanitizer_dir
        / trial_name
    )
    pov_dir = trial_dir / "output" / "povs"
    pov_dir.mkdir(parents=True, exist_ok=True)

    metadata = {
        "timestamp": "2026-04-27T00:00:00+00:00",
        "trial_num": int(trial_name.split("-")[-1]),
        "crs": "codex",
        "benchmark": benchmark,
        "harness": harness,
        "mode": "bug_finding",
        "source": {"path": "/src", "commit": "abc123"},
    }
    if metadata_sanitizer is not None:
        metadata["sanitizer"] = metadata_sanitizer
    if experiment_name is not None:
        metadata["experiment_name"] = experiment_name

    (trial_dir / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    if mark_success:
        (trial_dir / ".success").touch()
    if mark_fail:
        (trial_dir / ".fail").touch()
    (pov_dir / ".hidden.blob").write_bytes(b"hidden")
    for name, payload in povs.items():
        (pov_dir / name).write_bytes(payload)
    return trial_dir


def _write_flat_trial(
    source_dir: Path,
    *,
    benchmark: str,
    harness: str,
    trial_name: str,
    povs: dict[str, bytes],
    crs_name: str = "codex",
    metadata_sanitizer: str | None = None,
    mark_success: bool = True,
) -> Path:
    trial_dir = source_dir / f"{benchmark}__{crs_name}" / trial_name
    pov_dir = trial_dir / "output" / "povs"
    pov_dir.mkdir(parents=True, exist_ok=True)

    metadata = {
        "timestamp": "2026-04-27T00:00:00+00:00",
        "trial_num": int(trial_name.split("-")[-1]),
        "crs": crs_name,
        "benchmark": benchmark,
        "harness": harness,
        "mode": "bug_finding",
        "source": {"path": "/src", "commit": "abc123"},
    }
    if metadata_sanitizer is not None:
        metadata["sanitizer"] = metadata_sanitizer

    (trial_dir / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    if mark_success:
        (trial_dir / ".success").touch()
    for name, payload in povs.items():
        (pov_dir / name).write_bytes(payload)
    return trial_dir


def test_make_source_id_is_stable_and_path_based(tmp_path: Path) -> None:
    source_dir = tmp_path / "exp-a"
    source_dir.mkdir()

    source_id = make_source_id(source_dir)

    assert source_id.startswith("source-")
    assert source_id == make_source_id(source_dir.resolve())


def test_discover_source_povs_reads_multiple_roots_and_resolves_sanitizer(
    tmp_path: Path,
) -> None:
    source_a = tmp_path / "outer-a"
    source_b = tmp_path / "outer-b"
    _write_trial(
        source_a,
        benchmark="afc-curl-delta-01",
        harness="curl_fuzzer",
        trial_name="trial-1",
        metadata_sanitizer="undefined",
        experiment_name="exp-alpha",
        povs={"one.blob": b"A"},
    )
    _write_trial(
        source_b,
        benchmark="afc-curl-delta-01",
        harness="curl_fuzzer",
        trial_name="trial-2",
        metadata_sanitizer=None,
        povs={"two.blob": b"B"},
    )

    records, stats = discover_source_povs([source_a, source_b])

    assert [record.pov_filename for record in records] == ["one.blob", "two.blob"]
    assert records[0].source_sanitizer == "undefined"
    assert records[1].source_sanitizer == "address"
    assert [record.experiment_name for record in records] == ["exp-alpha", "outer-b"]
    assert stats == {
        "source_roots_processed": 2,
        "trials_processed": 2,
        "trials_skipped": 0,
        "original_pov_instances": 2,
    }


def test_discover_source_povs_keeps_paths_relative_to_source_root(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "source-root"
    _write_trial(
        source_root,
        benchmark="afc-wireshark-delta-01",
        harness="wire_fuzzer",
        trial_name="trial-3",
        nested_prefix=("experiment-a", "crs-a"),
        experiment_name="experiment-a",
        povs={"nested.blob": b"NESTED"},
    )

    records, _ = discover_source_povs([source_root])

    assert len(records) == 1
    assert records[0].trial_relative_path == (
        "experiment-a/crs-a/afc-wireshark-delta-01/wire_fuzzer/full/address/trial-3"
    )
    assert records[0].original_pov_relpath == (
        "experiment-a/crs-a/afc-wireshark-delta-01/"
        "wire_fuzzer/full/address/trial-3/output/povs/nested.blob"
    )


def test_discover_source_povs_applies_benchmark_and_trial_filters(
    tmp_path: Path,
) -> None:
    source_dir = tmp_path / "exp-a"
    _write_trial(
        source_dir,
        benchmark="afc-curl-delta-01",
        harness="curl_fuzzer",
        trial_name="trial-1",
        povs={"one.blob": b"A"},
    )
    _write_trial(
        source_dir,
        benchmark="afc-wireshark-delta-01",
        harness="wire_fuzzer",
        trial_name="trial-2",
        povs={"two.blob": b"B"},
    )

    records, stats = discover_source_povs(
        [source_dir],
        benchmark_filters={"afc-wireshark-delta-01"},
        trial_filters={"trial-2"},
    )

    assert [(record.benchmark, record.trial_relative_path) for record in records] == [
        (
            "afc-wireshark-delta-01",
            "afc-wireshark-delta-01/wire_fuzzer/full/address/trial-2",
        )
    ]
    assert stats["trials_processed"] == 1
    assert stats["trials_skipped"] == 1


def test_discover_source_povs_skips_trials_without_success_marker(
    tmp_path: Path,
) -> None:
    source_dir = tmp_path / "exp-a"
    _write_trial(
        source_dir,
        benchmark="afc-curl-delta-01",
        harness="curl_fuzzer",
        trial_name="trial-1",
        mark_success=False,
        povs={"partial.blob": b"A"},
    )
    _write_trial(
        source_dir,
        benchmark="afc-curl-delta-01",
        harness="curl_fuzzer",
        trial_name="trial-2",
        povs={"complete.blob": b"B"},
    )

    records, stats = discover_source_povs([source_dir])

    assert [record.pov_filename for record in records] == ["complete.blob"]
    assert stats["trials_processed"] == 1
    assert stats["trials_skipped"] == 1


def test_discover_source_povs_defaults_sanitizer_for_flat_trial_layout(
    tmp_path: Path,
) -> None:
    source_dir = tmp_path / "exp-a"
    _write_flat_trial(
        source_dir,
        benchmark="afc-curl-delta-01",
        harness="curl_fuzzer",
        trial_name="trial-4",
        povs={"flat.blob": b"FLAT"},
    )

    records, _ = discover_source_povs([source_dir])

    assert len(records) == 1
    assert records[0].source_sanitizer == "address"
    assert records[0].trial_relative_path == "afc-curl-delta-01__codex/trial-4"


def test_discover_source_povs_skips_failed_trials_with_povs(tmp_path: Path) -> None:
    source_dir = tmp_path / "exp-a"
    _write_trial(
        source_dir,
        benchmark="afc-curl-delta-01",
        harness="curl_fuzzer",
        trial_name="trial-5",
        mark_success=False,
        mark_fail=True,
        povs={"failed.blob": b"FAIL"},
    )
    _write_trial(
        source_dir,
        benchmark="afc-curl-delta-01",
        harness="curl_fuzzer",
        trial_name="trial-6",
        povs={"complete.blob": b"OK"},
    )

    records, stats = discover_source_povs([source_dir])

    assert [record.pov_filename for record in records] == ["complete.blob"]
    assert stats["trials_processed"] == 1
    assert stats["trials_skipped"] == 1


def test_discover_source_povs_skips_trials_without_visible_povs(
    tmp_path: Path,
) -> None:
    source_dir = tmp_path / "exp-a"
    _write_trial(
        source_dir,
        benchmark="afc-curl-delta-01",
        harness="curl_fuzzer",
        trial_name="trial-7",
        povs={},
    )

    records, stats = discover_source_povs([source_dir])

    assert records == []
    assert stats["trials_processed"] == 0
    assert stats["trials_skipped"] == 1
