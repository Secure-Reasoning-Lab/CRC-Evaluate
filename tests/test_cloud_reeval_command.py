from __future__ import annotations

import argparse
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import yaml
from crsbench.cloud.preflight import CloudLaunchPreflight
from crsbench.cloud.records import CloudFleetPlacementRecord
from crsbench.cloud.reeval_bundle import ReevalBundleBuildResult
from crsbench.cloud.types import CloudProvider

from tests.test_cloud_command import (
    _make_provider_neutral_experiment_config_with_evaluators,
    _make_reeval_launch_state,
)


def _write_config(path: Path, config) -> None:
    path.write_text(
        yaml.safe_dump(
            config.model_dump(mode="json", exclude_none=True),
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _make_orchestrator_record(name: str = "gce-orchestrator-remote-exp"):
    return SimpleNamespace(
        name=name,
        zone="us-east5-b",
        internal_ip="10.0.0.50",
        external_ip="34.1.2.50",
    )


@patch("crsbench.cloud.cli._reeval.save_launch_state")
@patch("crsbench.cloud.cli._reeval.append_created_instance_records")
@patch("crsbench.cloud.cli._reeval.provider_adapter_for_launch_plan")
@patch("crsbench.cloud.cli._reeval.prepare_launch_inputs")
@patch("crsbench.cloud.cli._reeval.find_launch_target_conflicts", return_value=[])
@patch("crsbench.cloud.cli._reeval.QuotaValidator")
@patch("crsbench.cloud.cli._reeval.RuntimeRegistration")
@patch("crsbench.cloud.cli._reeval.CloudVmBootstrapInputs")
def test_provision_cloud_reeval_fleet_skips_workers_and_saves_reeval_state(
    mock_bootstrap_inputs,
    mock_registration_cls,
    mock_quota_validator_cls,
    mock_find_conflicts,
    mock_prepare_launch_inputs,
    mock_provider_adapter_for_launch_plan,
    mock_append_created,
    mock_save_launch_state,
    tmp_path: Path,
) -> None:
    from crsbench.cloud.cli._reeval import provision_cloud_reeval_fleet

    source_config = _make_provider_neutral_experiment_config_with_evaluators()
    remote_config = source_config.model_copy(update={"experiment": "remote-exp"})
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, source_config)

    evaluator_fleet = CloudFleetPlacementRecord(
        provider=CloudProvider.GCE,
        role="evaluator",
        project="test-project",
        zone="us-east5-b",
        zones=["us-east5-b"],
        region="us-east5",
        owner_label="team-crs",
        count=1,
        name_prefix="remote-exp-eval",
        name_start_index=1,
        ssh_via_iap=True,
        provider_metadata={"project": "test-project", "zone": "us-east5-b"},
    )
    mock_prepare_launch_inputs.return_value = CloudLaunchPreflight(
        resolved_plan=MagicMock(),
        redacted_worker_fleets=[],
        redacted_evaluator_fleets=[evaluator_fleet],
        orchestrator_env={},
        worker_placement_envs=[],
        evaluator_placement_envs=[{}],
    )
    mock_adapter = mock_provider_adapter_for_launch_plan.return_value
    mock_adapter.build_orchestrator_config.return_value = SimpleNamespace(
        project="test-project",
        ssh_via_iap=True,
    )
    mock_adapter.create_orchestrator.return_value = _make_orchestrator_record()
    mock_adapter.create_evaluators.return_value = [
        SimpleNamespace(name="remote-exp-eval-001", zone="us-east5-b")
    ]
    mock_quota_validator_cls.return_value.validate.return_value = None
    mock_registration_cls.from_experiment_config.return_value = MagicMock()
    mock_bootstrap_inputs.from_experiment_config.return_value = MagicMock()

    state = provision_cloud_reeval_fleet(
        source_config_path=config_path,
        source_config=source_config,
        remote_config=remote_config,
        remote_experiment_name="remote-exp",
    )

    mock_adapter.create_workers.assert_not_called()
    mock_adapter.create_evaluators.assert_called_once()
    mock_find_conflicts.assert_called_once()
    saved_state = mock_save_launch_state.call_args.args[1]
    assert saved_state.launch_mode == "reeval"
    assert saved_state.source_experiment_name == "test-exp"
    assert saved_state.remote_experiment_name == "remote-exp"
    assert saved_state.worker_fleet_configs == []
    assert saved_state.remote_submission_dir.endswith(
        "/.crsbench-cloud/reeval/remote-exp"
    )
    assert saved_state.remote_experiment_root.endswith(
        "/.crsbench-cloud/reeval/remote-exp/workspace"
    )
    assert state == saved_state
    mock_append_created.assert_called()


@patch("crsbench.cloud.cli._reeval.publish_cloud_reeval_submission")
@patch("crsbench.cloud.cli._reeval.provision_cloud_reeval_fleet")
@patch("crsbench.cloud.cli._reeval.build_reeval_bundle")
@patch("crsbench.cloud.cli._reeval.load_experiment_config")
@patch("crsbench.cloud.cli._reeval._utc_timestamp", return_value="20260424-123456")
def test_run_cloud_reeval_builds_bundle_and_publishes_submission(
    mock_timestamp,
    mock_load_config,
    mock_build_bundle,
    mock_provision_fleet,
    mock_publish_submission,
    tmp_path: Path,
) -> None:
    from crsbench.cloud.cli._reeval import run_cloud_reeval

    del mock_timestamp

    config = _make_provider_neutral_experiment_config_with_evaluators().model_copy(
        update={"experiment_filestore": tmp_path / "filestore"}
    )
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, config)
    (Path(config.experiment_filestore) / "test-exp").mkdir(parents=True, exist_ok=True)
    mock_load_config.return_value = config
    bundle_result = ReevalBundleBuildResult(
        bundle_root=tmp_path / "bundle",
        manifest_path=tmp_path / "bundle" / "manifest.json",
        selected_trial_count=1,
        skipped_trial_count=0,
    )
    mock_build_bundle.return_value = bundle_result
    mock_provision_fleet.return_value = _make_reeval_launch_state().model_copy(
        update={"config_path": str(config_path)}
    )

    rc = run_cloud_reeval(
        argparse.Namespace(
            config=str(config_path),
            from_path=None,
            remote_experiment=None,
        )
    )

    assert rc == 0
    mock_build_bundle.assert_called_once()
    assert (
        mock_build_bundle.call_args.kwargs["source_experiment_root"]
        == Path(config.experiment_filestore) / "test-exp"
    )
    assert (
        mock_build_bundle.call_args.kwargs["remote_experiment_name"]
        == "test-exp-reeval-20260424-123456"
    )
    mock_provision_fleet.assert_called_once()
    mock_publish_submission.assert_called_once_with(
        launch_state=mock_provision_fleet.return_value,
        bundle_root=bundle_result.bundle_root,
        source_experiment_name="test-exp",
        remote_experiment_name="test-exp-reeval-20260424-123456",
        config_path=config_path,
    )


def test_remote_publish_and_launch_command_passes_clone_dir_and_redis_password() -> (
    None
):
    from crsbench.cloud.cli._reeval import _remote_publish_and_launch_command

    command = _remote_publish_and_launch_command(
        remote_submission_dir=Path("/remote/submission"),
        remote_upload_dir=Path("/remote/submission.uploading"),
    )

    assert 'CLONE_DIR="${CLONE_DIR}"' in command
    assert 'CRSBENCH_REDIS_PASSWORD="${CRSBENCH_REDIS_PASSWORD:-}"' in command
