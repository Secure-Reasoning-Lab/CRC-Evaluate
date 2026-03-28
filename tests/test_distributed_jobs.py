from pathlib import Path
from unittest.mock import patch

from crsbench.distributed.job_lifecycle import (
    JobLifecycleRecord,
    JobLifecycleStore,
    JobState,
)
from crsbench.distributed.jobs import (
    JobLifecycleRuntime,
    _apply_worker_overrides,
    _build_trial_output_path,
    _finish_job_lifecycle,
    _lifecycle_runtime_is_current_owner,
    _publish_trial_terminal_artifacts,
)
from crsbench.validation.schemas import EvaluationMode, ExperimentConfig


class _FakeRedis:
    def __init__(self) -> None:
        self._hashes: dict[str, dict[str, str]] = {}
        self._lists: dict[str, list[str]] = {}

    def hset(self, key: str, field: str, value: str) -> int:
        self._hashes.setdefault(key, {})[field] = value
        return 1

    def hget(self, key: str, field: str):
        return self._hashes.get(key, {}).get(field)

    def hgetall(self, key: str):
        return self._hashes.get(key, {}).copy()

    def rpush(self, key: str, value: str) -> int:
        bucket = self._lists.setdefault(key, [])
        bucket.append(value)
        return len(bucket)


def test_apply_worker_overrides_uses_grouped_storage(tmp_path: Path):
    config = ExperimentConfig(
        experiment="test-exp",
        trials=1,
        mode=EvaluationMode.DELTA,
        max_total_time=21600,
        inputs={"pov": {"enabled": True, "max_variants_per_cpv": 1}},
        experiment_filestore=tmp_path / "exp-orchestrator",
        report_filestore=tmp_path / "rep-orchestrator",
        crs_compose={"test-crs": {"num_cores": 1}},
        benchmarks=["test-bench"],
        worker={
            "jobs": 1,
            "storage": {
                "experiment_filestore": str(tmp_path / "exp-worker"),
                "report_filestore": str(tmp_path / "rep-worker"),
                "results_filestore": str(tmp_path / "results-worker"),
                "keep_only_results": True,
                "cleanup_after_trial": True,
                "copy_results_after_trial": True,
            },
        },
    )

    _apply_worker_overrides(config)

    assert config.experiment_filestore == (tmp_path / "exp-worker").resolve()
    assert config.report_filestore == (tmp_path / "rep-worker").resolve()
    assert config.results_filestore == (tmp_path / "results-worker").resolve()
    assert config.keep_only_results is True
    assert config.cleanup_after_trial is True
    assert config.copy_results_after_trial is True


def test_apply_worker_overrides_warns_for_effective_storage_paths(tmp_path: Path):
    config = ExperimentConfig(
        experiment="test-exp",
        trials=1,
        mode=EvaluationMode.DELTA,
        max_total_time=21600,
        inputs={"pov": {"enabled": True, "max_variants_per_cpv": 1}},
        experiment_filestore=tmp_path / "exp-orchestrator",
        report_filestore=tmp_path / "rep-orchestrator",
        crs_compose={"test-crs": {"num_cores": 1}},
        benchmarks=["test-bench"],
        worker={
            "jobs": 1,
            "storage": {
                "experiment_filestore": str(tmp_path / "exp-worker"),
                "report_filestore": str(tmp_path / "rep-worker"),
                "results_filestore": str(tmp_path / "results-worker"),
                "copy_results_after_trial": True,
            },
        },
    )

    with patch(
        "crsbench.distributed.jobs.warn_for_persisted_storage_roots"
    ) as mock_warn:
        _apply_worker_overrides(config)

    mock_warn.assert_called_once_with(
        experiment_filestore=(tmp_path / "exp-worker").resolve(),
        report_filestore=(tmp_path / "rep-worker").resolve(),
        copy_results_after_trial=True,
        results_filestore=(tmp_path / "results-worker").resolve(),
    )


def test_lifecycle_runtime_is_current_owner_requires_matching_claimed_by() -> None:
    fake = _FakeRedis()
    store = JobLifecycleStore(fake)
    store.set(
        "exp-1",
        JobLifecycleRecord(
            job_id="job-1",
            trial_key="trial-1",
            state=JobState.RUNNING,
            claimed_by="worker-2",
        ),
    )

    runtime = JobLifecycleRuntime(
        experiment_name="exp-1",
        job_id="job-1",
        worker_name="worker-1",
        store=store,
    )

    assert _lifecycle_runtime_is_current_owner(runtime) is False


def test_finish_job_lifecycle_noops_for_superseded_worker() -> None:
    fake = _FakeRedis()
    store = JobLifecycleStore(fake)
    store.set(
        "exp-1",
        JobLifecycleRecord(
            job_id="job-1",
            trial_key="trial-1",
            state=JobState.RUNNING,
            claimed_by="worker-2",
        ),
    )

    runtime = JobLifecycleRuntime(
        experiment_name="exp-1",
        job_id="job-1",
        worker_name="worker-1",
        store=store,
    )

    _finish_job_lifecycle(runtime, success=True, detail=None)

    record = store.get("exp-1", "job-1")
    assert record is not None
    assert record.state is JobState.RUNNING
    assert record.claimed_by == "worker-2"


def test_publish_trial_terminal_artifacts_skips_superseded_worker(
    tmp_path: Path,
) -> None:
    fake = _FakeRedis()
    store = JobLifecycleStore(fake)
    store.set(
        "exp-1",
        JobLifecycleRecord(
            job_id="job-1",
            trial_key="trial-1",
            state=JobState.RUNNING,
            claimed_by="worker-2",
        ),
    )

    runtime = JobLifecycleRuntime(
        experiment_name="exp-1",
        job_id="job-1",
        worker_name="worker-1",
        store=store,
    )
    config = ExperimentConfig(
        experiment="exp-1",
        trials=1,
        mode=EvaluationMode.DELTA,
        max_total_time=21600,
        inputs={"pov": {"enabled": True, "max_variants_per_cpv": 1}},
        experiment_filestore=tmp_path / "experiment-store",
        report_filestore=tmp_path / "report-store",
        results_filestore=tmp_path / "results-store",
        crs_compose={"test-crs": {"num_cores": 1}},
        benchmarks=["test-bench"],
        cleanup_after_trial=True,
        copy_results_after_trial=True,
    )

    trial_output_dir = _build_trial_output_path(
        filestore=config.experiment_filestore.resolve(),
        experiment_name=config.experiment,
        crs="test-crs",
        benchmark="test-bench",
        harness="fuzz_target",
        mode="delta",
        sanitizer="address",
        trial_num=1,
        target_cpv_id=None,
    )
    trial_output_dir.mkdir(parents=True, exist_ok=True)
    payload = trial_output_dir / "artifact.txt"
    payload.write_text("stale worker output", encoding="utf-8")

    published = _publish_trial_terminal_artifacts(
        config=config,
        trial_output_dir=trial_output_dir,
        success=True,
        results_timestamp="20260327-120000",
        lifecycle_runtime=runtime,
    )

    assert published is False
    assert (trial_output_dir / ".success").exists() is False
    assert (trial_output_dir / ".fail").exists() is False
    assert payload.exists()


def test_publish_trial_terminal_artifacts_removes_opposite_terminal_marker(
    tmp_path: Path,
) -> None:
    config = ExperimentConfig(
        experiment="exp-1",
        trials=1,
        mode=EvaluationMode.DELTA,
        max_total_time=21600,
        inputs={"pov": {"enabled": True, "max_variants_per_cpv": 1}},
        experiment_filestore=tmp_path / "experiment-store",
        report_filestore=tmp_path / "report-store",
        crs_compose={"test-crs": {"num_cores": 1}},
        benchmarks=["test-bench"],
    )

    trial_output_dir = _build_trial_output_path(
        filestore=config.experiment_filestore.resolve(),
        experiment_name=config.experiment,
        crs="test-crs",
        benchmark="test-bench",
        harness="fuzz_target",
        mode="delta",
        sanitizer="address",
        trial_num=1,
        target_cpv_id=None,
    )
    trial_output_dir.mkdir(parents=True, exist_ok=True)
    (trial_output_dir / ".fail").touch()

    published = _publish_trial_terminal_artifacts(
        config=config,
        trial_output_dir=trial_output_dir,
        success=True,
        results_timestamp="20260327-120000",
        lifecycle_runtime=None,
    )

    assert published is True
    assert (trial_output_dir / ".success").exists()
    assert not (trial_output_dir / ".fail").exists()
