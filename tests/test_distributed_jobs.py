from pathlib import Path
from unittest.mock import patch

from crsbench.distributed.jobs import _apply_worker_overrides
from crsbench.validation.schemas import EvaluationMode, ExperimentConfig


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
