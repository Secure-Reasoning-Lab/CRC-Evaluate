"""Tests for the from_experiment manifest walker."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from crsbench.cloud.from_experiment_bundle import build_from_experiment_manifest

if TYPE_CHECKING:
    from pathlib import Path


def _seed_pov_store(
    root: Path,
    *,
    benchmark: str,
    harness: str,
    mode: str,
    sanitizer: str,
    trial_id: int,
    entries: dict,
) -> Path:
    """Create ``trial-N/povs/pov_store.json`` with *entries* and return its dir."""
    trial_povs = (
        root / benchmark / harness / mode / sanitizer / f"trial-{trial_id}" / "povs"
    )
    trial_povs.mkdir(parents=True, exist_ok=True)
    (trial_povs / "pov_store.json").write_text(json.dumps(entries))
    return trial_povs


def _write_blob(store_dir: Path, cpv: str, pov_hash: str) -> Path:
    blob = store_dir / "cpvs" / cpv / "blobs" / f"{pov_hash}.blob"
    blob.parent.mkdir(parents=True, exist_ok=True)
    blob.write_bytes(b"\x00")
    return blob


def _write_crash_log(store_dir: Path, cpv: str, name: str) -> Path:
    log = store_dir / "cpvs" / cpv / "crash_logs" / name
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("crash\n")
    return log


def test_manifest_includes_cpv_blob_and_missing_signature_log(tmp_path: Path) -> None:
    store = _seed_pov_store(
        tmp_path,
        benchmark="bench",
        harness="fuzz_a",
        mode="delta",
        sanitizer="address",
        trial_id=1,
        entries={
            "cpv_to_first_pov": {"CPV-1": "abc"},
            "povs": {
                "abc": {
                    "hash": "abc",
                    "status": "cpv",
                    "cpv_matched": ["CPV-1"],
                    "crash_signature": None,
                }
            },
        },
    )
    _write_blob(store, "CPV-1", "abc")
    _write_crash_log(store, "CPV-1", "abc.log")

    manifest = build_from_experiment_manifest(tmp_path)

    rels = set(manifest)
    assert "bench/fuzz_a/delta/address/trial-1/povs/pov_store.json" in rels
    assert "bench/fuzz_a/delta/address/trial-1/povs/cpvs/CPV-1/blobs/abc.blob" in rels
    assert (
        "bench/fuzz_a/delta/address/trial-1/povs/cpvs/CPV-1/crash_logs/abc.log" in rels
    )


def test_manifest_skips_non_cpv_blobs(tmp_path: Path) -> None:
    store = _seed_pov_store(
        tmp_path,
        benchmark="bench",
        harness="fuzz_a",
        mode="delta",
        sanitizer="address",
        trial_id=1,
        entries={
            "povs": {
                "aaa": {
                    "hash": "aaa",
                    "status": "not_vulnerable",
                    "cpv_matched": [],
                    "crash_signature": None,
                }
            },
        },
    )
    _write_blob(store, "CPV-X", "aaa")

    manifest = build_from_experiment_manifest(tmp_path)

    rels = set(manifest)
    assert "bench/fuzz_a/delta/address/trial-1/povs/pov_store.json" in rels
    assert not any(r.endswith("aaa.blob") for r in rels)


def test_manifest_omits_crash_log_when_signature_present(tmp_path: Path) -> None:
    store = _seed_pov_store(
        tmp_path,
        benchmark="bench",
        harness="fuzz_a",
        mode="delta",
        sanitizer="address",
        trial_id=1,
        entries={
            "povs": {
                "abc": {
                    "hash": "abc",
                    "status": "cpv",
                    "cpv_matched": ["CPV-1"],
                    "crash_signature": "sig-1234",
                }
            },
        },
    )
    _write_blob(store, "CPV-1", "abc")
    _write_crash_log(store, "CPV-1", "abc.log")

    manifest = build_from_experiment_manifest(tmp_path)

    rels = set(manifest)
    assert "bench/fuzz_a/delta/address/trial-1/povs/cpvs/CPV-1/blobs/abc.blob" in rels
    # Signature present, so crash log fallback is not needed.
    assert not any(r.endswith("crash_logs/abc.log") for r in rels)


def test_manifest_skips_malformed_pov_store_json(tmp_path: Path) -> None:
    trial_povs = (
        tmp_path / "bench" / "fuzz_a" / "delta" / "address" / "trial-1" / "povs"
    )
    trial_povs.mkdir(parents=True)
    (trial_povs / "pov_store.json").write_text("{not valid json")

    manifest = build_from_experiment_manifest(tmp_path)

    assert manifest == []


def test_manifest_empty_tree_returns_empty_list(tmp_path: Path) -> None:
    manifest = build_from_experiment_manifest(tmp_path)
    assert manifest == []


def test_manifest_rejects_non_directory(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist"
    with pytest.raises(ValueError):
        build_from_experiment_manifest(missing)


def test_manifest_returns_posix_relative_paths(tmp_path: Path) -> None:
    store = _seed_pov_store(
        tmp_path,
        benchmark="bench",
        harness="fuzz_a",
        mode="delta",
        sanitizer="address",
        trial_id=1,
        entries={
            "povs": {
                "abc": {
                    "hash": "abc",
                    "status": "cpv",
                    "cpv_matched": ["CPV-1"],
                    "crash_signature": "sig",
                }
            },
        },
    )
    _write_blob(store, "CPV-1", "abc")

    manifest = build_from_experiment_manifest(tmp_path)

    for entry in manifest:
        assert "\\" not in entry
        assert not entry.startswith("/")
