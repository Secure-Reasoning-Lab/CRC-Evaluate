"""Pipeline tests for bug-finding → bug-fixing via inputs.pov.from_experiment.

Exercises two end-to-end seams:
1. ``generate_trial_matrix`` filters bug-fixing CPVs to those that appear in
   the source bug-finding experiment store.
2. ``BenchmarkRunner._prepare_bugfix_inputs_from_experiment`` stages POV
   blobs from the source experiment instead of benchmark ground truth.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from crsbench.evaluation.adapter import OssCrsAdapter
from crsbench.evaluation.runner import BenchmarkRunner
from crsbench.evaluation.verification.models import PovVerificationStatus
from crsbench.run_experiment import generate_trial_matrix
from crsbench.validation.schemas import (
    POV,
    BenchmarkHarness,
    ExperimentConfig,
    HarnessFile,
    Vulnerability,
)

# ---------------------------------------------------------------------------
# Fixtures: synthetic bug-finding experiment store
# ---------------------------------------------------------------------------


def _seed_bugfind_experiment(
    experiment_dir: Path,
    *,
    benchmark: str,
    harness: str,
    sanitizer: str,
    mode: str,
    cpv_id: str,
    pov_hash: str,
    blob_content: bytes,
) -> Path:
    """Seed a minimal experiment-data tree with one CPV POV. Returns blob path."""
    store_dir = (
        experiment_dir / benchmark / harness / mode / sanitizer / "trial-1" / "povs"
    )
    blobs = store_dir / "cpvs" / cpv_id / "blobs"
    blobs.mkdir(parents=True)
    blob_path = blobs / f"{pov_hash}.blob"
    blob_path.write_bytes(blob_content)

    store_json = store_dir / "pov_store.json"
    store_json.write_text(
        json.dumps(
            {
                "crs_run_start_time": 0.0,
                "povs": {
                    pov_hash: {
                        "hash": pov_hash,
                        "first_seen_ts": 100.0,
                        "file_mtime": None,
                        "file_size": len(blob_content),
                        "status": PovVerificationStatus.CPV.value,
                        "cpv_matched": [cpv_id],
                        "crash_log_path": None,
                        "crash_signature": "SIG_X",
                        "verification_duration": 0.0,
                    }
                },
                "cpv_to_first_pov": {
                    cpv_id: {
                        "pov_hash": pov_hash,
                        "discovery_ts": 100.0,
                        "relative_time": 100.0,
                    }
                },
            }
        )
    )
    return blob_path


def _make_bugfix_config(tmp_path: Path, experiment_dir: Path) -> ExperimentConfig:
    return ExperimentConfig(
        experiment="bugfix-pipeline",
        task="bugfixing",
        trials=1,
        mode="full",
        max_total_time=20000,
        inputs={
            "pov": {
                "enabled": True,
                "max_variants_per_cpv": 1,
                "from_experiment": str(experiment_dir),
            }
        },
        experiment_filestore=str(tmp_path / "filestore"),
        report_filestore=str(tmp_path / "report"),
        crs_compose={"crs1": {"num_cores": 1}},
        benchmarks=["bench1"],
        only_cpv_harnesses=True,
    )


def _make_benchmark_harness(
    *, benchmark: str, harness_name: str, cpv_ids: list[str]
) -> BenchmarkHarness:
    vulns = [
        Vulnerability(
            vuln_keyword=cpv,
            povs=[POV(id="pov_0", sanitizer="address")],
        )
        for cpv in cpv_ids
    ]
    return BenchmarkHarness(
        name=benchmark,
        path=Path(f"/tmp/{benchmark}"),
        harness=HarnessFile(
            name=harness_name, path=f"/src/{harness_name}.c", vulns=vulns
        ),
    )


# ---------------------------------------------------------------------------
# generate_trial_matrix tests
# ---------------------------------------------------------------------------


def _generate_with_mock(benchmark_harnesses, crs_type, config):
    mock_adapter = MagicMock()

    def mock_get_harness(harness_name):
        for bh in benchmark_harnesses:
            if bh.harness.name == harness_name:
                return bh.harness
        return None

    mock_adapter.get_harness = mock_get_harness

    with (
        patch("crsbench.run_experiment.get_crs_type", return_value=crs_type),
        patch(
            "crsbench.run_experiment.MetaYamlAdapter.from_meta_yaml",
            return_value=mock_adapter,
        ),
    ):
        return generate_trial_matrix(
            benchmark_harnesses,
            ["crs1"],
            config,
            registry_dir=Path("/tmp/registry"),
        )


def test_trial_matrix_skips_cpvs_missing_from_source(tmp_path: Path):
    """Only CPVs discovered in the source bug-finding store get trials."""
    exp_dir = tmp_path / "bugfind-exp"
    _seed_bugfind_experiment(
        exp_dir,
        benchmark="bench1",
        harness="h1",
        sanitizer="address",
        mode="full",
        cpv_id="cpv_0",
        pov_hash="aaaa000011112222",
        blob_content=b"bugfind-crash-input",
    )

    config = _make_bugfix_config(tmp_path, exp_dir)
    bh = _make_benchmark_harness(
        benchmark="bench1", harness_name="h1", cpv_ids=["cpv_0", "cpv_1", "cpv_2"]
    )

    trials = _generate_with_mock([bh], "bug-fixing", config)

    assert {t.target_cpv_id for t in trials} == {"cpv_0"}
    assert all(t.crs == "crs1" for t in trials)
    assert all(t.mode == "full" for t in trials)


def test_trial_matrix_empty_when_source_found_nothing(tmp_path: Path):
    """Empty source experiment → no bug-fixing trials scheduled."""
    exp_dir = tmp_path / "bugfind-empty"
    # Create the directory but no trials inside.
    exp_dir.mkdir()

    config = _make_bugfix_config(tmp_path, exp_dir)
    bh = _make_benchmark_harness(
        benchmark="bench1", harness_name="h1", cpv_ids=["cpv_0", "cpv_1"]
    )

    trials = _generate_with_mock([bh], "bug-fixing", config)
    assert trials == []


def test_from_experiment_requires_bugfixing_task(tmp_path: Path):
    """Schema rejects from_experiment with task='bugfinding'."""
    exp_dir = tmp_path / "bugfind-exp"
    exp_dir.mkdir()

    with pytest.raises(ValueError, match="from_experiment is only valid"):
        ExperimentConfig(
            experiment="bad",
            task="bugfinding",
            trials=1,
            mode="full",
            max_total_time=20000,
            inputs={"pov": {"from_experiment": str(exp_dir)}},
            experiment_filestore=str(tmp_path / "fs"),
            report_filestore=str(tmp_path / "rp"),
            crs_compose={"crs1": {"num_cores": 1}},
            benchmarks=["bench1"],
        )


# ---------------------------------------------------------------------------
# Staging tests
# ---------------------------------------------------------------------------


def _make_stub_bugfix_runner(
    *, pov_from_experiment: Path, oss_fuzz: Path
) -> BenchmarkRunner:
    adapter = OssCrsAdapter(
        crs_config_name="stub",
        oss_fuzz_path=oss_fuzz,
        registry_dir=Path("/tmp/fake/registry"),
        benchmarks_root=Path("/tmp/fake/benchmarks"),
        mode="bug-fixing",
    )
    return BenchmarkRunner(
        adapter=adapter,
        snapshot_period=0,
        pov_input_enabled=True,
        pov_from_experiment=pov_from_experiment,
        max_pov_variants_per_cpv=1,
    )


def test_staging_copies_source_blob_not_ground_truth(tmp_path: Path):
    """_prepare_bugfix_inputs_from_experiment stages the source experiment blob."""
    exp_dir = tmp_path / "bugfind-exp"
    src_blob = _seed_bugfind_experiment(
        exp_dir,
        benchmark="bench1",
        harness="h1",
        sanitizer="address",
        mode="full",
        cpv_id="cpv_0",
        pov_hash="aaaa000011112222",
        blob_content=b"source-experiment-blob",
    )

    # A fake benchmark path whose .aixcc would normally hold ground truth.
    # The new staging path must NOT read from it.
    benchmark_path = tmp_path / "benchmarks" / "bench1"
    benchmark_path.mkdir(parents=True)

    trial_output_dir = tmp_path / "filestore" / "trial-1"
    trial_output_dir.mkdir(parents=True)

    runner = _make_stub_bugfix_runner(
        pov_from_experiment=exp_dir, oss_fuzz=tmp_path / "oss-fuzz"
    )
    runner._prepare_bugfix_inputs_from_experiment(
        benchmark_path=benchmark_path,
        harness_name="h1",
        trial_output_dir=trial_output_dir,
        target_cpv_id="cpv_0",
        sanitizer="address",
    )

    staged_input = trial_output_dir / "crs-input" / "povs" / "cpv_0"
    staged_adapter = trial_output_dir / "povs" / "cpv_0"
    assert staged_input.is_file()
    assert staged_adapter.is_file()
    assert staged_input.read_bytes() == b"source-experiment-blob"
    assert staged_adapter.read_bytes() == b"source-experiment-blob"
    assert staged_input.read_bytes() == src_blob.read_bytes()

    # CPV marker directory exists for accounting.
    assert (trial_output_dir / "crs-input" / "cpvs" / "cpv_0").is_dir()


def test_staging_raises_when_cpv_missing(tmp_path: Path):
    exp_dir = tmp_path / "bugfind-exp"
    _seed_bugfind_experiment(
        exp_dir,
        benchmark="bench1",
        harness="h1",
        sanitizer="address",
        mode="full",
        cpv_id="cpv_0",
        pov_hash="aaaa000011112222",
        blob_content=b"src",
    )

    benchmark_path = tmp_path / "benchmarks" / "bench1"
    benchmark_path.mkdir(parents=True)
    trial_output_dir = tmp_path / "trial"
    trial_output_dir.mkdir()

    runner = _make_stub_bugfix_runner(
        pov_from_experiment=exp_dir, oss_fuzz=tmp_path / "oss-fuzz"
    )
    with pytest.raises(Exception, match="No POVs found in source experiment"):
        runner._prepare_bugfix_inputs_from_experiment(
            benchmark_path=benchmark_path,
            harness_name="h1",
            trial_output_dir=trial_output_dir,
            target_cpv_id="cpv_999",
            sanitizer="address",
        )


def test_staging_respects_max_variants(tmp_path: Path):
    """When the source has multiple variants, max_pov_variants_per_cpv caps staging."""
    exp_dir = tmp_path / "bugfind-exp"

    store_dir = exp_dir / "bench1" / "h1" / "full" / "address" / "trial-1" / "povs"
    blobs = store_dir / "cpvs" / "cpv_0" / "blobs"
    blobs.mkdir(parents=True)

    povs = {}
    for idx, (h, ts, sig, content) in enumerate(
        [
            ("hashearly00000000", 10.0, "SIG_EARLY", b"e"),
            ("hashmid0000000000", 50.0, "SIG_MID", b"m"),
            ("hashlate000000000", 99.0, "SIG_LATE", b"l"),
        ]
    ):
        (blobs / f"{h}.blob").write_bytes(content)
        povs[h] = {
            "hash": h,
            "first_seen_ts": ts,
            "file_mtime": None,
            "file_size": len(content),
            "status": PovVerificationStatus.CPV.value,
            "cpv_matched": ["cpv_0"],
            "crash_log_path": None,
            "crash_signature": sig,
            "verification_duration": 0.0,
        }
    (store_dir / "pov_store.json").write_text(
        json.dumps(
            {
                "crs_run_start_time": 0.0,
                "povs": povs,
                "cpv_to_first_pov": {
                    "cpv_0": {
                        "pov_hash": "hashearly00000000",
                        "discovery_ts": 10.0,
                        "relative_time": 10.0,
                    }
                },
            }
        )
    )

    trial_output_dir = tmp_path / "trial"
    trial_output_dir.mkdir()

    # Build a runner with max_pov_variants_per_cpv=2.
    adapter = OssCrsAdapter(
        crs_config_name="stub",
        oss_fuzz_path=tmp_path / "oss-fuzz",
        registry_dir=Path("/tmp/fake/registry"),
        benchmarks_root=Path("/tmp/fake/benchmarks"),
        mode="bug-fixing",
    )
    runner = BenchmarkRunner(
        adapter=adapter,
        snapshot_period=0,
        pov_input_enabled=True,
        pov_from_experiment=exp_dir,
        max_pov_variants_per_cpv=2,
    )
    runner._prepare_bugfix_inputs_from_experiment(
        benchmark_path=tmp_path / "benchmarks" / "bench1",
        harness_name="h1",
        trial_output_dir=trial_output_dir,
        target_cpv_id="cpv_0",
        sanitizer="address",
    )

    staged = sorted((trial_output_dir / "crs-input" / "povs").iterdir())
    # 2 files: one for the first variant (named "cpv_0"), one for the second
    # (named "cpv_0__<hash>").
    assert len(staged) == 2
    first = trial_output_dir / "crs-input" / "povs" / "cpv_0"
    assert first.is_file()
    assert first.read_bytes() == b"e"  # earliest discovery_ts wins for slot 0
