"""Unit tests for crsbench.evaluation.external_pov_source.

Covers crash-signature dedup (reuses parse_crash_signature logic), unparseable
conservative keep, discovery_ts ordering, and list_found_cpvs.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Optional

import pytest
from crsbench.evaluation.external_pov_source import (
    ExternalPovSource,
    POVRecord,
)
from crsbench.evaluation.verification.models import PovVerificationStatus

if TYPE_CHECKING:
    from pathlib import Path

ASAN_SIG_A = """\
==1==ERROR: AddressSanitizer: heap-buffer-overflow on address 0x1
    #0 0xaaa in func_a /src/a.c:10:5
    #1 0xbbb in main /src/main.c:5:3
SUMMARY: AddressSanitizer: heap-buffer-overflow /src/a.c:10:5
"""

ASAN_SIG_B = """\
==1==ERROR: AddressSanitizer: use-after-free on address 0x2
    #0 0xccc in func_b /src/b.c:20:5
    #1 0xddd in main /src/main.c:10:3
SUMMARY: AddressSanitizer: use-after-free /src/b.c:20:5
"""

UNPARSEABLE_LOG = "this is not a sanitizer crash log\n"


def _write_pov(
    store_dir: Path,
    cpv_id: str,
    pov_hash: str,
    *,
    first_seen_ts: float,
    crash_signature: Optional[str] = None,
    crash_log: Optional[str] = None,
    blob_content: bytes = b"blob",
) -> None:
    """Write a POV blob + crash log and register a POVEntry in pov_store.json."""
    cpvs_dir = store_dir / "cpvs" / cpv_id
    blobs = cpvs_dir / "blobs"
    logs = cpvs_dir / "crash_logs"
    blobs.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)

    blob_path = blobs / f"{pov_hash}.blob"
    blob_path.write_bytes(blob_content)

    crash_log_rel: Optional[str] = None
    if crash_log is not None:
        log_path = logs / f"{pov_hash}.log"
        log_path.write_text(crash_log)
        crash_log_rel = str(log_path.relative_to(store_dir))

    store_path = store_dir / "pov_store.json"
    if store_path.is_file():
        data = json.loads(store_path.read_text())
    else:
        data = {
            "crs_run_start_time": 0.0,
            "povs": {},
            "cpv_to_first_pov": {},
        }

    entry = {
        "hash": pov_hash,
        "first_seen_ts": first_seen_ts,
        "file_mtime": None,
        "file_size": len(blob_content),
        "status": PovVerificationStatus.CPV.value,
        "cpv_matched": [cpv_id],
        "crash_log_path": crash_log_rel,
        "crash_signature": crash_signature,
        "verification_duration": 0.0,
    }
    data["povs"][pov_hash] = entry
    if cpv_id not in data["cpv_to_first_pov"]:
        data["cpv_to_first_pov"][cpv_id] = {
            "pov_hash": pov_hash,
            "discovery_ts": first_seen_ts,
            "relative_time": first_seen_ts,
        }
    store_path.write_text(json.dumps(data, indent=2))


@pytest.fixture
def experiment_dir(tmp_path: Path) -> Path:
    """Synthesize an experiment-data tree with two trials of bug-finding output."""
    bench = "mock-bench"
    harness = "fuzz_harness"
    mode = "full"
    sanitizer = "address"

    trial_1 = tmp_path / bench / harness / mode / sanitizer / "trial-1" / "povs"
    trial_2 = tmp_path / bench / harness / mode / sanitizer / "trial-2" / "povs"
    trial_1.mkdir(parents=True)
    trial_2.mkdir(parents=True)

    _write_pov(
        trial_1,
        cpv_id="cpv_0",
        pov_hash="aaaa111111111111",
        first_seen_ts=100.0,
        crash_signature="SIG_A",
        crash_log=ASAN_SIG_A,
        blob_content=b"payload-a",
    )
    _write_pov(
        trial_1,
        cpv_id="cpv_0",
        pov_hash="aaaa222222222222",
        first_seen_ts=200.0,
        crash_signature="SIG_A",
        crash_log=ASAN_SIG_A,
        blob_content=b"payload-a-variant",
    )

    _write_pov(
        trial_2,
        cpv_id="cpv_0",
        pov_hash="bbbb111111111111",
        first_seen_ts=150.0,
        crash_signature="SIG_B",
        crash_log=ASAN_SIG_B,
        blob_content=b"payload-b",
    )
    _write_pov(
        trial_2,
        cpv_id="cpv_1",
        pov_hash="cccc111111111111",
        first_seen_ts=300.0,
        crash_signature=None,
        crash_log=UNPARSEABLE_LOG,
        blob_content=b"payload-c",
    )
    return tmp_path


def test_list_found_cpvs_per_trial_isolation(experiment_dir: Path):
    """1:1 mode: each trial only sees its own CPVs, no cross-trial pooling."""
    source_t1 = ExternalPovSource(experiment_dir, trial_num=1)
    source_t2 = ExternalPovSource(experiment_dir, trial_num=2)

    found_t1 = source_t1.list_found_cpvs(
        benchmark="mock-bench", harness="fuzz_harness", sanitizer="address"
    )
    found_t2 = source_t2.list_found_cpvs(
        benchmark="mock-bench", harness="fuzz_harness", sanitizer="address"
    )
    # trial-1 only had cpv_0 entries; trial-2 had cpv_0 (different sig) and cpv_1.
    assert found_t1 == {"cpv_0"}
    assert found_t2 == {"cpv_0", "cpv_1"}


def test_list_found_cpvs_empty_for_missing_trial(experiment_dir: Path):
    """A trial_num that did not run yields an empty CPV set, not an error."""
    source = ExternalPovSource(experiment_dir, trial_num=99)
    assert (
        source.list_found_cpvs(
            benchmark="mock-bench", harness="fuzz_harness", sanitizer="address"
        )
        == set()
    )


def test_list_found_cpvs_empty_for_unknown_tuple(experiment_dir: Path):
    source = ExternalPovSource(experiment_dir, trial_num=1)
    assert (
        source.list_found_cpvs(
            benchmark="other", harness="fuzz_harness", sanitizer="address"
        )
        == set()
    )


def test_get_pov_blobs_dedups_within_single_trial(experiment_dir: Path):
    """Per-trial dedup: trial-1 has two SIG_A POVs → one survives."""
    source = ExternalPovSource(experiment_dir, trial_num=1)
    records = source.get_pov_blobs(
        benchmark="mock-bench",
        harness="fuzz_harness",
        sanitizer="address",
        cpv_id="cpv_0",
    )
    # trial-1 has two SIG_A POVs (ts=100 and ts=200); earliest wins. trial-2's
    # SIG_B POV is NOT pooled in (1:1 isolation).
    assert [r.crash_signature for r in records] == ["SIG_A"]
    assert [r.content_hash for r in records] == ["aaaa111111111111"]


def test_get_pov_blobs_does_not_pool_across_trials(experiment_dir: Path):
    """trial-1 has only cpv_0 (SIG_A); trial-2 has cpv_0 (SIG_B) and cpv_1.
    Each trial returns only its own POVs."""
    source_t1 = ExternalPovSource(experiment_dir, trial_num=1)
    source_t2 = ExternalPovSource(experiment_dir, trial_num=2)

    t1_cpv0 = source_t1.get_pov_blobs(
        benchmark="mock-bench",
        harness="fuzz_harness",
        sanitizer="address",
        cpv_id="cpv_0",
    )
    t2_cpv0 = source_t2.get_pov_blobs(
        benchmark="mock-bench",
        harness="fuzz_harness",
        sanitizer="address",
        cpv_id="cpv_0",
    )
    assert [r.crash_signature for r in t1_cpv0] == ["SIG_A"]
    assert [r.crash_signature for r in t2_cpv0] == ["SIG_B"]


def test_get_pov_blobs_conservatively_keeps_unparseable(experiment_dir: Path):
    """trial-2 has the unparseable cpv_1 entry; trial-1 should not see it."""
    source = ExternalPovSource(experiment_dir, trial_num=2)
    records = source.get_pov_blobs(
        benchmark="mock-bench",
        harness="fuzz_harness",
        sanitizer="address",
        cpv_id="cpv_1",
    )
    assert len(records) == 1
    assert records[0].crash_signature is None
    assert records[0].content_hash == "cccc111111111111"


def test_get_pov_blobs_returns_empty_for_missing_cpv(experiment_dir: Path):
    source = ExternalPovSource(experiment_dir, trial_num=1)
    records = source.get_pov_blobs(
        benchmark="mock-bench",
        harness="fuzz_harness",
        sanitizer="address",
        cpv_id="cpv_missing",
    )
    assert records == []


def test_missing_experiment_path_raises(tmp_path: Path):
    with pytest.raises(ValueError, match="from_experiment path does not exist"):
        ExternalPovSource(tmp_path / "nope", trial_num=1)


def test_trial_num_must_be_positive(tmp_path: Path):
    with pytest.raises(ValueError, match="trial_num must be"):
        ExternalPovSource(tmp_path, trial_num=0)
    with pytest.raises(ValueError, match="trial_num must be"):
        ExternalPovSource(tmp_path, trial_num=-1)


def test_crash_signature_fallback_to_parser(tmp_path: Path):
    """When POVEntry.crash_signature is None, fall back to parsing crash log."""
    store = tmp_path / "b" / "h" / "full" / "address" / "trial-1" / "povs"
    store.mkdir(parents=True)

    _write_pov(
        store,
        cpv_id="cpv_0",
        pov_hash="dd11111111111111",
        first_seen_ts=50.0,
        crash_signature=None,
        crash_log=ASAN_SIG_A,
        blob_content=b"x",
    )
    _write_pov(
        store,
        cpv_id="cpv_0",
        pov_hash="dd22222222222222",
        first_seen_ts=60.0,
        crash_signature=None,
        crash_log=ASAN_SIG_A,  # same signature — should dedup after parsing
        blob_content=b"y",
    )
    _write_pov(
        store,
        cpv_id="cpv_0",
        pov_hash="dd33333333333333",
        first_seen_ts=70.0,
        crash_signature=None,
        crash_log=ASAN_SIG_B,
        blob_content=b"z",
    )

    source = ExternalPovSource(tmp_path, trial_num=1)
    records = source.get_pov_blobs(
        benchmark="b", harness="h", sanitizer="address", cpv_id="cpv_0"
    )
    # Fallback parser should see SIG_A for first two and dedup, keep SIG_B.
    assert len(records) == 2
    hashes = [r.content_hash for r in records]
    assert "dd11111111111111" in hashes
    assert "dd33333333333333" in hashes
    # dd2 was a duplicate of dd1's signature
    assert "dd22222222222222" not in hashes


def test_non_cpv_status_ignored(tmp_path: Path):
    """Entries with status != CPV are not surfaced as POV blobs."""
    store = tmp_path / "b" / "h" / "full" / "address" / "trial-1" / "povs"
    store.mkdir(parents=True)

    cpvs_dir = store / "cpvs" / "cpv_0"
    (cpvs_dir / "blobs").mkdir(parents=True)
    blob_path = cpvs_dir / "blobs" / "noncpv0000000000.blob"
    blob_path.write_bytes(b"ignored")

    store_path = store / "pov_store.json"
    store_path.write_text(
        json.dumps(
            {
                "crs_run_start_time": 0.0,
                "povs": {
                    "noncpv0000000000": {
                        "hash": "noncpv0000000000",
                        "first_seen_ts": 10.0,
                        "file_mtime": None,
                        "file_size": 7,
                        "status": PovVerificationStatus.UNINTENDED_CRASH.value,
                        "cpv_matched": [],
                        "crash_log_path": None,
                        "crash_signature": "SIG_X",
                        "verification_duration": 0.0,
                    }
                },
                "cpv_to_first_pov": {},
            }
        )
    )

    source = ExternalPovSource(tmp_path, trial_num=1)
    assert (
        source.get_pov_blobs(
            benchmark="b", harness="h", sanitizer="address", cpv_id="cpv_0"
        )
        == []
    )


def test_multi_cpv_shared_blob_resolves_via_primary(tmp_path: Path):
    """A POV matching multiple CPVs is stored under the first matched CPV only.

    The loader must resolve blob paths via ``cpv_matched[0]`` so secondary
    CPVs can still return the shared blob. This reproduces PDFExtractTextFuzzer
    behavior seen in real bug-finding experiment stores.
    """
    store = tmp_path / "b" / "h" / "full" / "address" / "trial-1" / "povs"
    # Blob and log live under cpv_0 (the primary/first matched CPV).
    blobs = store / "cpvs" / "cpv_0" / "blobs"
    logs = store / "cpvs" / "cpv_0" / "crash_logs"
    blobs.mkdir(parents=True)
    logs.mkdir(parents=True)
    (blobs / "shared0000000000.blob").write_bytes(b"shared-payload")
    (logs / "shared0000000000.log").write_text(ASAN_SIG_A)

    (store / "pov_store.json").write_text(
        json.dumps(
            {
                "crs_run_start_time": 0.0,
                "povs": {
                    "shared0000000000": {
                        "hash": "shared0000000000",
                        "first_seen_ts": 42.0,
                        "file_mtime": None,
                        "file_size": 14,
                        "status": PovVerificationStatus.CPV.value,
                        "cpv_matched": ["cpv_0", "cpv_1", "cpv_3"],
                        "crash_log_path": None,
                        "crash_signature": "SIG_SHARED",
                        "verification_duration": 0.0,
                    }
                },
                "cpv_to_first_pov": {
                    "cpv_0": {
                        "pov_hash": "shared0000000000",
                        "discovery_ts": 42.0,
                        "relative_time": 42.0,
                    },
                    "cpv_1": {
                        "pov_hash": "shared0000000000",
                        "discovery_ts": 42.0,
                        "relative_time": 42.0,
                    },
                    "cpv_3": {
                        "pov_hash": "shared0000000000",
                        "discovery_ts": 42.0,
                        "relative_time": 42.0,
                    },
                },
            }
        )
    )

    source = ExternalPovSource(tmp_path, trial_num=1)
    for cpv in ("cpv_0", "cpv_1", "cpv_3"):
        records = source.get_pov_blobs(
            benchmark="b", harness="h", sanitizer="address", cpv_id=cpv
        )
        assert len(records) == 1, f"expected shared blob for {cpv}"
        assert records[0].content_hash == "shared0000000000"
        assert records[0].blob_path.read_bytes() == b"shared-payload"

    # A CPV not in cpv_matched must not surface the shared blob.
    assert (
        source.get_pov_blobs(
            benchmark="b", harness="h", sanitizer="address", cpv_id="cpv_2"
        )
        == []
    )


def test_record_fields_populated(experiment_dir: Path):
    source = ExternalPovSource(experiment_dir, trial_num=1)
    records = source.get_pov_blobs(
        benchmark="mock-bench",
        harness="fuzz_harness",
        sanitizer="address",
        cpv_id="cpv_0",
    )
    assert records
    rec = records[0]
    assert isinstance(rec, POVRecord)
    assert rec.cpv_id == "cpv_0"
    assert rec.blob_path.is_file()
    assert rec.source_trial_dir.name == "trial-1"
