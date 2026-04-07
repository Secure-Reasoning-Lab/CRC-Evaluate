from pathlib import Path
from unittest.mock import MagicMock, patch

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
    _initialize_job_lifecycle_runtime,
    _lifecycle_runtime_is_current_owner,
    _publish_trial_terminal_artifacts,
    run_crs_trial,
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

    def delete(self, key: str) -> int:
        removed = 0
        if key in self._hashes:
            del self._hashes[key]
            removed += 1
        if key in self._lists:
            del self._lists[key]
            removed += 1
        return removed


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


def test_lifecycle_runtime_is_current_owner_rejects_non_active_states() -> None:
    fake = _FakeRedis()
    store = JobLifecycleStore(fake)
    store.set(
        "exp-1",
        JobLifecycleRecord(
            job_id="job-1",
            trial_key="trial-1",
            state=JobState.FAILED,
            claimed_by="worker-1",
        ),
    )

    runtime = JobLifecycleRuntime(
        experiment_name="exp-1",
        job_id="job-1",
        worker_name="worker-1",
        store=store,
    )

    assert _lifecycle_runtime_is_current_owner(runtime) is False


def test_lifecycle_runtime_is_current_owner_rejects_missing_runtime() -> None:
    assert _lifecycle_runtime_is_current_owner(None) is False


def test_initialize_job_lifecycle_runtime_skips_heartbeat_for_terminal_record() -> None:
    fake = _FakeRedis()
    store = JobLifecycleStore(fake)
    store.set(
        "exp-1",
        JobLifecycleRecord(
            job_id="job-1",
            trial_key="trial-1",
            state=JobState.COMPLETED,
            claimed_by=None,
        ),
    )
    config = ExperimentConfig(
        experiment="exp-1",
        trials=1,
        mode=EvaluationMode.DELTA,
        max_total_time=21600,
        inputs={"pov": {"enabled": True, "max_variants_per_cpv": 1}},
        experiment_filestore=Path("/tmp/exp"),
        report_filestore=Path("/tmp/report"),
        crs_compose={"test-crs": {"num_cores": 1}},
        benchmarks=["test-bench"],
    )

    rq_job = type(
        "CurrentJob",
        (),
        {"connection": fake, "id": "job-1"},
    )()

    with patch("rq.get_current_job", return_value=rq_job):
        runtime = _initialize_job_lifecycle_runtime(
            config=config,
            trial_key="trial-1",
            runtime_worker_name="worker-1",
        )

    assert runtime is not None
    fetched = store.get("exp-1", "job-1")
    assert fetched is not None
    assert fetched.state is JobState.COMPLETED
    assert fetched.last_heartbeat is None


def test_initialize_job_lifecycle_runtime_does_not_steal_foreign_claim() -> None:
    fake = _FakeRedis()
    store = JobLifecycleStore(fake)
    store.set(
        "exp-1",
        JobLifecycleRecord(
            job_id="job-1",
            trial_key="trial-1",
            state=JobState.CLAIMED,
            claimed_by="worker-2",
        ),
    )
    config = ExperimentConfig(
        experiment="exp-1",
        trials=1,
        mode=EvaluationMode.DELTA,
        max_total_time=21600,
        inputs={"pov": {"enabled": True, "max_variants_per_cpv": 1}},
        experiment_filestore=Path("/tmp/exp"),
        report_filestore=Path("/tmp/report"),
        crs_compose={"test-crs": {"num_cores": 1}},
        benchmarks=["test-bench"],
    )
    rq_job = type(
        "CurrentJob",
        (),
        {"connection": fake, "id": "job-1"},
    )()

    with patch("rq.get_current_job", return_value=rq_job):
        runtime = _initialize_job_lifecycle_runtime(
            config=config,
            trial_key="trial-1",
            runtime_worker_name="worker-1",
        )

    assert runtime is not None
    fetched = store.get("exp-1", "job-1")
    assert fetched is not None
    assert fetched.state is JobState.CLAIMED
    assert fetched.claimed_by == "worker-2"
    assert fetched.last_heartbeat is None


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


def test_publish_trial_terminal_artifacts_writes_requested_marker_without_conflict(
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


def test_publish_trial_terminal_artifacts_preserves_preexisting_canonical_marker(
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
    (trial_output_dir / ".success").touch()

    marker_attempted = False

    def _write_metadata() -> None:
        nonlocal marker_attempted
        marker_attempted = True

    published = _publish_trial_terminal_artifacts(
        config=config,
        trial_output_dir=trial_output_dir,
        success=False,
        results_timestamp="20260327-120000",
        lifecycle_runtime=None,
        metadata_writer=_write_metadata,
    )

    assert published is False
    assert (trial_output_dir / ".success").exists()
    assert not (trial_output_dir / ".fail").exists()
    assert marker_attempted is False


def test_publish_trial_terminal_artifacts_skips_when_runtime_missing_for_active_rq_job(
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

    rq_job = type(
        "CurrentJob",
        (),
        {"connection": _FakeRedis(), "id": "job-1"},
    )()

    with patch("rq.get_current_job", return_value=rq_job):
        published = _publish_trial_terminal_artifacts(
            config=config,
            trial_output_dir=trial_output_dir,
            success=True,
            results_timestamp="20260327-120000",
            lifecycle_runtime=None,
        )

    assert published is False
    assert not (trial_output_dir / ".success").exists()
    assert not (trial_output_dir / ".fail").exists()


def test_run_crs_trial_publishes_fail_marker_before_failed_lifecycle_transition(
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
            claimed_by="worker-1",
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
        crs_compose={"test-crs": {"num_cores": 1}},
        benchmarks=["test-bench"],
        skip_litellm=True,
    )

    class _EffectiveInputs:
        hints_enabled = False
        hint_sarif_level = 0
        hint_corpus_level = 0
        seed_corpus_enabled = False
        seed_corpus_max_time = 0
        diff_enabled = False
        ground_truth_patch_enabled = False
        pov_enabled = True
        max_pov_variants_per_cpv = 1
        patch_verify_variants = 0

        @staticmethod
        def to_dict() -> dict[str, object]:
            return {
                "hints_enabled": False,
                "seed_corpus_enabled": False,
                "diff_enabled": False,
                "pov_enabled": True,
                "max_pov_variants_per_cpv": 1,
                "patch_verify_variants": 0,
            }

    class _NoopResourceContext:
        def __init__(self, **_kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            return False

    benchmark_path = tmp_path / "benchmark"
    benchmark_path.mkdir()
    oss_fuzz_path = tmp_path / "oss-fuzz"
    oss_fuzz_path.mkdir()

    runner = MagicMock()
    runner.run_benchmark.side_effect = RuntimeError("boom")
    adapter = MagicMock()

    with (
        patch(
            "crsbench.distributed.jobs._resolve_effective_input_settings",
            return_value=_EffectiveInputs(),
        ),
        patch("crsbench.distributed.jobs._check_existing_trial", return_value=None),
        patch(
            "crsbench.distributed.jobs._initialize_job_lifecycle_runtime",
            return_value=runtime,
        ),
        patch(
            "crsbench.distributed.jobs._start_job_lifecycle_heartbeat",
            return_value=None,
        ),
        patch(
            "crsbench.distributed.jobs.ensure_oss_fuzz_root",
            return_value=str(oss_fuzz_path),
        ),
        patch("crsbench.distributed.jobs.get_crs_type", return_value="bug-finding"),
        patch("crsbench.distributed.jobs.create_adapter", return_value=adapter),
        patch(
            "crsbench.distributed.jobs._resolve_benchmark_path",
            return_value=benchmark_path,
        ),
        patch("crsbench.distributed.jobs._load_benchmark_language", return_value="c"),
        patch("crsbench.distributed.jobs.BenchmarkRunner", return_value=runner),
        patch("crsbench.distributed.jobs._ensure_project_symlink"),
        patch("crsbench.distributed.jobs.add_file_handler", return_value=object()),
        patch("crsbench.distributed.jobs.remove_file_handler"),
        patch("crsbench.distributed.jobs.set_trial_context"),
        patch("crsbench.distributed.jobs._cleanup_llm_tracking"),
        patch(
            "crsbench.evaluation.resource_context.ResourceContext", _NoopResourceContext
        ),
        patch("rq.get_current_job", return_value=None),
    ):
        result = run_crs_trial(
            crs="test-crs",
            benchmark="test-bench",
            harness_name="fuzz_target",
            harness_path="./fuzz_target.c",
            trial_num=1,
            trial_id="trial-1",
            config_dict=config.model_dump(mode="json"),
            mode="delta",
            sanitizer="address",
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

    assert result.success is False
    assert result.error == "boom"
    assert (trial_output_dir / ".fail").exists()
    record = store.get("exp-1", "job-1")
    assert record is not None
    assert record.state is JobState.FAILED


def test_run_crs_trial_finalizes_failed_lifecycle_when_fail_publication_raises(
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
            claimed_by="worker-1",
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
        crs_compose={"test-crs": {"num_cores": 1}},
        benchmarks=["test-bench"],
        skip_litellm=True,
    )

    class _EffectiveInputs:
        hints_enabled = False
        hint_sarif_level = 0
        hint_corpus_level = 0
        seed_corpus_enabled = False
        seed_corpus_max_time = 0
        diff_enabled = False
        ground_truth_patch_enabled = False
        pov_enabled = True
        max_pov_variants_per_cpv = 1
        patch_verify_variants = 0

        @staticmethod
        def to_dict() -> dict[str, object]:
            return {
                "hints_enabled": False,
                "seed_corpus_enabled": False,
                "diff_enabled": False,
                "pov_enabled": True,
                "max_pov_variants_per_cpv": 1,
                "patch_verify_variants": 0,
            }

    class _NoopResourceContext:
        def __init__(self, **_kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            return False

    benchmark_path = tmp_path / "benchmark"
    benchmark_path.mkdir()
    oss_fuzz_path = tmp_path / "oss-fuzz"
    oss_fuzz_path.mkdir()

    runner = MagicMock()
    runner.run_benchmark.side_effect = RuntimeError("boom")
    adapter = MagicMock()
    heartbeat_runtime = object()

    with (
        patch(
            "crsbench.distributed.jobs._resolve_effective_input_settings",
            return_value=_EffectiveInputs(),
        ),
        patch("crsbench.distributed.jobs._check_existing_trial", return_value=None),
        patch(
            "crsbench.distributed.jobs._initialize_job_lifecycle_runtime",
            return_value=runtime,
        ),
        patch(
            "crsbench.distributed.jobs._start_job_lifecycle_heartbeat",
            return_value=heartbeat_runtime,
        ),
        patch(
            "crsbench.distributed.jobs.ensure_oss_fuzz_root",
            return_value=str(oss_fuzz_path),
        ),
        patch("crsbench.distributed.jobs.get_crs_type", return_value="bug-finding"),
        patch("crsbench.distributed.jobs.create_adapter", return_value=adapter),
        patch(
            "crsbench.distributed.jobs._resolve_benchmark_path",
            return_value=benchmark_path,
        ),
        patch("crsbench.distributed.jobs._load_benchmark_language", return_value="c"),
        patch("crsbench.distributed.jobs.BenchmarkRunner", return_value=runner),
        patch("crsbench.distributed.jobs._ensure_project_symlink"),
        patch("crsbench.distributed.jobs.add_file_handler", return_value=object()),
        patch("crsbench.distributed.jobs.remove_file_handler"),
        patch("crsbench.distributed.jobs.set_trial_context"),
        patch("crsbench.distributed.jobs._cleanup_llm_tracking"),
        patch(
            "crsbench.evaluation.resource_context.ResourceContext", _NoopResourceContext
        ),
        patch("rq.get_current_job", return_value=None),
        patch(
            "crsbench.distributed.jobs._publish_trial_terminal_artifacts",
            side_effect=OSError("disk full"),
        ),
        patch("crsbench.distributed.jobs._stop_job_lifecycle_heartbeat") as stop_hb,
    ):
        result = run_crs_trial(
            crs="test-crs",
            benchmark="test-bench",
            harness_name="fuzz_target",
            harness_path="./fuzz_target.c",
            trial_num=1,
            trial_id="trial-1",
            config_dict=config.model_dump(mode="json"),
            mode="delta",
            sanitizer="address",
        )

    assert result.success is False
    assert result.error == "boom"
    stop_hb.assert_called_once_with(heartbeat_runtime)
    record = store.get("exp-1", "job-1")
    assert record is not None
    assert record.state is JobState.FAILED
