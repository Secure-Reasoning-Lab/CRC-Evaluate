"""Tests for orchestrator-side completion markers.

Tests the _write_orchestrator_marker() function in run_experiment.py and
the _check_existing_trial() function in distributed/jobs.py, along with
the worker_machine/worker_trial_dir fields on TrialMetadata.
"""

import json
from pathlib import Path

from crsbench.distributed.jobs import _build_trial_output_path, _check_existing_trial
from crsbench.evaluation.results import TrialMetadata as ResultsTrialMetadata
from crsbench.evaluation.results import TrialResult
from crsbench.run_experiment import _write_orchestrator_marker
from crsbench.validation.schemas import ExperimentConfig, TrialMode
from crsbench.validation.schemas import TrialMetadata as SchemasTrialMetadata

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_experiment_config(tmp_path: Path, **overrides) -> ExperimentConfig:
    """Build a minimal valid ExperimentConfig pointing at tmp_path."""
    defaults = {
        "experiment": "test-exp",
        "trials": 1,
        "mode": "delta",
        "max_total_time": 86400,
        "inputs": {"pov": {"enabled": True, "max_variants_per_cpv": 1}},
        "experiment_filestore": str(tmp_path / "experiment-data"),
        "report_filestore": str(tmp_path / "report-data"),
        "crs_compose": {"test-crs": {"num_cores": 1}},
        "benchmarks": ["bench-01"],
    }
    defaults.update(overrides)
    return ExperimentConfig(**defaults)


def _make_trial_result(
    *,
    crs: str = "test-crs",
    benchmark: str = "bench-01",
    harness: str = "harness_a",
    trial_num: int = 1,
    mode: str = "delta",
    sanitizer: str = "address",
    success: bool = True,
    worker_machine: str | None = "worker-1.example.com",
    worker_trial_dir: str | None = "/mnt/data/trial-1",
) -> TrialResult:
    """Build a TrialResult with sensible defaults."""
    return TrialResult(
        crs=crs,
        benchmark=benchmark,
        harness=harness,
        trial_num=trial_num,
        crs_type="bug-finding",
        mode=mode,
        sanitizer=sanitizer,
        success=success,
        execution_time=42.0,
        report={},
        metadata=ResultsTrialMetadata(
            timestamp_start=1000.0,
            timestamp_end=1042.0,
            worker_machine=worker_machine,
            worker_trial_dir=worker_trial_dir,
        ),
    )


# ===================================================================
# 1. TrialMetadata worker fields
# ===================================================================


class TestTrialMetadataWorkerFields:
    """Worker identification fields on both TrialMetadata classes."""

    # -- results.TrialMetadata --

    def test_results_trial_metadata_worker_fields_default(self):
        meta = ResultsTrialMetadata(timestamp_start=0.0, timestamp_end=0.0)
        assert meta.worker_machine is None
        assert meta.worker_trial_dir is None

    def test_results_trial_metadata_worker_fields_set(self):
        meta = ResultsTrialMetadata(
            timestamp_start=0.0,
            timestamp_end=0.0,
            worker_machine="w1.example.com",
            worker_trial_dir="/data/trial-1",
        )
        assert meta.worker_machine == "w1.example.com"
        assert meta.worker_trial_dir == "/data/trial-1"

    # -- schemas.TrialMetadata --

    def test_schemas_trial_metadata_worker_fields_default(self):
        meta = SchemasTrialMetadata(
            timestamp="2024-01-01T00:00:00",
            trial_num=1,
            crs="crs",
            benchmark="bench",
            harness="harness",
            mode=TrialMode.bug_finding,
            source={"path": "/src", "commit": "abc123"},
        )
        assert meta.worker_machine is None
        assert meta.worker_trial_dir is None

    def test_schemas_trial_metadata_worker_fields_set(self):
        meta = SchemasTrialMetadata(
            timestamp="2024-01-01T00:00:00",
            trial_num=1,
            crs="crs",
            benchmark="bench",
            harness="harness",
            mode=TrialMode.bug_finding,
            source={"path": "/src", "commit": "abc123"},
            worker_machine="w2.example.com",
            worker_trial_dir="/mnt/nfs/trial-2",
        )
        assert meta.worker_machine == "w2.example.com"
        assert meta.worker_trial_dir == "/mnt/nfs/trial-2"

    def test_schemas_trial_metadata_serialization_roundtrip(self):
        meta = SchemasTrialMetadata(
            timestamp="2024-01-01T00:00:00",
            trial_num=1,
            crs="crs",
            benchmark="bench",
            harness="harness",
            mode=TrialMode.bug_finding,
            source={"path": "/src", "commit": "abc123"},
            worker_machine="roundtrip-host",
            worker_trial_dir="/roundtrip/dir",
        )
        dumped = meta.model_dump_json()
        restored = SchemasTrialMetadata.model_validate_json(dumped)
        assert restored.worker_machine == "roundtrip-host"
        assert restored.worker_trial_dir == "/roundtrip/dir"


# ===================================================================
# 2. _write_orchestrator_marker()
# ===================================================================


class TestWriteOrchestratorMarker:
    """Tests for _write_orchestrator_marker() in run_experiment.py."""

    def test_writes_success_marker(self, tmp_path: Path):
        config = _make_experiment_config(tmp_path)
        result = _make_trial_result(success=True)

        _write_orchestrator_marker(result, config)

        trial_dir = _build_trial_output_path(
            filestore=config.experiment_filestore.resolve(),
            experiment_name=config.experiment,
            crs=result.crs,
            benchmark=result.benchmark,
            harness=result.harness,
            mode=result.mode,
            sanitizer=result.sanitizer,
            trial_num=result.trial_num,
        )
        assert (trial_dir / ".success").exists()
        assert not (trial_dir / ".fail").exists()

    def test_writes_fail_marker(self, tmp_path: Path):
        config = _make_experiment_config(tmp_path)
        result = _make_trial_result(success=False)

        _write_orchestrator_marker(result, config)

        trial_dir = _build_trial_output_path(
            filestore=config.experiment_filestore.resolve(),
            experiment_name=config.experiment,
            crs=result.crs,
            benchmark=result.benchmark,
            harness=result.harness,
            mode=result.mode,
            sanitizer=result.sanitizer,
            trial_num=result.trial_num,
        )
        assert (trial_dir / ".fail").exists()
        assert not (trial_dir / ".success").exists()

    def test_skips_existing_metadata(self, tmp_path: Path):
        config = _make_experiment_config(tmp_path)
        result = _make_trial_result()

        trial_dir = _build_trial_output_path(
            filestore=config.experiment_filestore.resolve(),
            experiment_name=config.experiment,
            crs=result.crs,
            benchmark=result.benchmark,
            harness=result.harness,
            mode=result.mode,
            sanitizer=result.sanitizer,
            trial_num=result.trial_num,
        )
        trial_dir.mkdir(parents=True)
        existing_content = '{"pre-existing": true}'
        (trial_dir / "metadata.json").write_text(existing_content)

        _write_orchestrator_marker(result, config)

        assert (trial_dir / "metadata.json").read_text() == existing_content

    def test_creates_directory_structure(self, tmp_path: Path):
        config = _make_experiment_config(tmp_path)
        result = _make_trial_result()

        _write_orchestrator_marker(result, config)

        trial_dir = _build_trial_output_path(
            filestore=config.experiment_filestore.resolve(),
            experiment_name=config.experiment,
            crs=result.crs,
            benchmark=result.benchmark,
            harness=result.harness,
            mode=result.mode,
            sanitizer=result.sanitizer,
            trial_num=result.trial_num,
        )
        assert trial_dir.is_dir()

    def test_metadata_contains_worker_fields(self, tmp_path: Path):
        config = _make_experiment_config(tmp_path)
        result = _make_trial_result(
            worker_machine="worker-7",
            worker_trial_dir="/remote/trial",
        )

        _write_orchestrator_marker(result, config)

        trial_dir = _build_trial_output_path(
            filestore=config.experiment_filestore.resolve(),
            experiment_name=config.experiment,
            crs=result.crs,
            benchmark=result.benchmark,
            harness=result.harness,
            mode=result.mode,
            sanitizer=result.sanitizer,
            trial_num=result.trial_num,
        )
        metadata = json.loads((trial_dir / "metadata.json").read_text())
        assert metadata["worker_machine"] == "worker-7"
        assert metadata["worker_trial_dir"] == "/remote/trial"

    def test_metadata_contains_phase_timestamp_fields(self, tmp_path: Path):
        config = _make_experiment_config(tmp_path)
        result = _make_trial_result()
        result.metadata.timestamp_unix = 1000.0
        result.metadata.build_start_time = 1001.0
        result.metadata.build_end_time = 1010.0
        result.metadata.run_start_time = 1011.0
        result.metadata.run_end_time = 1042.0

        _write_orchestrator_marker(result, config)

        trial_dir = _build_trial_output_path(
            filestore=config.experiment_filestore.resolve(),
            experiment_name=config.experiment,
            crs=result.crs,
            benchmark=result.benchmark,
            harness=result.harness,
            mode=result.mode,
            sanitizer=result.sanitizer,
            trial_num=result.trial_num,
        )
        metadata = json.loads((trial_dir / "metadata.json").read_text())
        assert metadata["timestamp_unix"] == 1000.0
        assert metadata["build_start_time"] == 1001.0
        assert metadata["build_end_time"] == 1010.0
        assert metadata["run_start_time"] == 1011.0
        assert metadata["run_end_time"] == 1042.0

    def test_metadata_contains_sanitizer(self, tmp_path: Path):
        config = _make_experiment_config(tmp_path)
        result = _make_trial_result(sanitizer="memory")

        _write_orchestrator_marker(result, config)

        trial_dir = _build_trial_output_path(
            filestore=config.experiment_filestore.resolve(),
            experiment_name=config.experiment,
            crs=result.crs,
            benchmark=result.benchmark,
            harness=result.harness,
            mode=result.mode,
            sanitizer=result.sanitizer,
            trial_num=result.trial_num,
        )
        metadata = json.loads((trial_dir / "metadata.json").read_text())
        assert metadata["sanitizer"] == "memory"

    def test_skips_when_mode_is_none(self, tmp_path: Path):
        config = _make_experiment_config(tmp_path)
        result = _make_trial_result(mode=None)

        _write_orchestrator_marker(result, config)

        # No directory should have been created
        exp_dir = config.experiment_filestore.resolve() / config.experiment
        assert not exp_dir.exists()

    def test_skips_when_sanitizer_is_none(self, tmp_path: Path):
        config = _make_experiment_config(tmp_path)
        result = _make_trial_result(sanitizer=None)

        _write_orchestrator_marker(result, config)

        exp_dir = config.experiment_filestore.resolve() / config.experiment
        assert not exp_dir.exists()

    def test_idempotent_marker_write(self, tmp_path: Path):
        config = _make_experiment_config(tmp_path)
        result = _make_trial_result()

        _write_orchestrator_marker(result, config)
        _write_orchestrator_marker(result, config)

        trial_dir = _build_trial_output_path(
            filestore=config.experiment_filestore.resolve(),
            experiment_name=config.experiment,
            crs=result.crs,
            benchmark=result.benchmark,
            harness=result.harness,
            mode=result.mode,
            sanitizer=result.sanitizer,
            trial_num=result.trial_num,
        )
        assert (trial_dir / ".success").exists()
        assert (trial_dir / "metadata.json").exists()

    def test_removes_opposite_terminal_marker(self, tmp_path: Path):
        config = _make_experiment_config(tmp_path)
        failed = _make_trial_result(success=False)
        succeeded = _make_trial_result(success=True)

        _write_orchestrator_marker(failed, config)
        _write_orchestrator_marker(succeeded, config)

        trial_dir = _build_trial_output_path(
            filestore=config.experiment_filestore.resolve(),
            experiment_name=config.experiment,
            crs=succeeded.crs,
            benchmark=succeeded.benchmark,
            harness=succeeded.harness,
            mode=succeeded.mode,
            sanitizer=succeeded.sanitizer,
            trial_num=succeeded.trial_num,
        )
        assert (trial_dir / ".success").exists()
        assert not (trial_dir / ".fail").exists()


# ===================================================================
# 3. Disk-based filtering via _check_existing_trial()
# ===================================================================


class TestDiskBasedFiltering:
    """Tests for _check_existing_trial() with real filesystem markers."""

    def _trial_dir(self, config: ExperimentConfig, filestore: Path) -> Path:
        return _build_trial_output_path(
            filestore=filestore,
            experiment_name=config.experiment,
            crs="test-crs",
            benchmark="bench-01",
            harness="harness_a",
            mode="delta",
            sanitizer="address",
            trial_num=1,
        )

    def _call_check(self, config: ExperimentConfig):
        return _check_existing_trial(
            config=config,
            crs="test-crs",
            benchmark="bench-01",
            harness="harness_a",
            mode="delta",
            sanitizer="address",
            trial_num=1,
        )

    def test_returns_none_when_no_markers(self, tmp_path: Path):
        config = _make_experiment_config(tmp_path)
        assert self._call_check(config) is None

    def test_returns_result_for_success_marker(self, tmp_path: Path):
        config = _make_experiment_config(tmp_path)
        trial_dir = self._trial_dir(config, config.experiment_filestore.resolve())
        trial_dir.mkdir(parents=True)
        (trial_dir / ".success").touch()

        result = self._call_check(config)

        assert result is not None
        assert result.success is True

    def test_returns_result_for_fail_marker(self, tmp_path: Path):
        config = _make_experiment_config(tmp_path)
        trial_dir = self._trial_dir(config, config.experiment_filestore.resolve())
        trial_dir.mkdir(parents=True)
        (trial_dir / ".fail").touch()

        result = self._call_check(config)

        assert result is not None
        assert result.success is False

    def test_checks_experiment_filestore(self, tmp_path: Path):
        config = _make_experiment_config(tmp_path)
        trial_dir = self._trial_dir(config, config.experiment_filestore.resolve())
        trial_dir.mkdir(parents=True)
        (trial_dir / ".success").touch()

        result = self._call_check(config)

        assert result is not None

    def test_checks_results_filestore_first(self, tmp_path: Path):
        results_fs = tmp_path / "results-data"
        config = _make_experiment_config(tmp_path, results_filestore=str(results_fs))

        # Place a .success marker ONLY in results_filestore
        trial_dir_results = self._trial_dir(config, results_fs.resolve())
        trial_dir_results.mkdir(parents=True)
        (trial_dir_results / ".success").touch()
        # Write metadata so we can verify it was read from results_filestore
        metadata = {
            "mode": "bug_finding",
            "povs_found": 99,
            "total_povs": 100,
        }
        (trial_dir_results / "metadata.json").write_text(json.dumps(metadata))

        result = self._call_check(config)

        assert result is not None
        assert result.success is True
        assert result.povs_found == 99


# ===================================================================
# 4. End-to-end marker flow
# ===================================================================


class TestEndToEndMarkerFlow:
    """Integration: write marker then check trial is skipped."""

    def test_write_then_filter_skips_trial(self, tmp_path: Path):
        config = _make_experiment_config(tmp_path)
        trial_result = _make_trial_result()

        _write_orchestrator_marker(trial_result, config)

        existing = _check_existing_trial(
            config=config,
            crs=trial_result.crs,
            benchmark=trial_result.benchmark,
            harness=trial_result.harness,
            mode=trial_result.mode,
            sanitizer=trial_result.sanitizer,
            trial_num=trial_result.trial_num,
        )

        assert existing is not None
        assert existing.success is True
