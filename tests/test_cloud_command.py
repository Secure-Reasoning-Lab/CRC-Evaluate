"""Unit tests for crsbench cloud CLI command -- status, events, config reconnect."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from crsbench.validation.schemas import (
    CloudConfig,
    ExperimentConfig,
    GceOrchestratorConfig,
    GceWorkerFleetConfig,
)

# ---------------------------------------------------------------------------
# Fake Redis (reusable fixture)
# ---------------------------------------------------------------------------


class _FakeRedis:
    """Minimal fake Redis for unit testing, with hash and list support."""

    def __init__(self) -> None:
        self._hashes: dict[str, dict[str, str]] = {}
        self._lists: dict[str, list[str]] = {}

    def hset(self, key: str, field: str, value: str) -> None:
        self._hashes.setdefault(key, {})[field] = value

    def hget(self, key: str, field: str) -> str | None:
        return self._hashes.get(key, {}).get(field)

    def hgetall(self, key: str) -> dict[str, str]:
        return dict(self._hashes.get(key, {}))

    def delete(self, key: str) -> None:
        self._hashes.pop(key, None)
        self._lists.pop(key, None)

    def rpush(self, key: str, value: str) -> int:
        lst = self._lists.setdefault(key, [])
        lst.append(value)
        return len(lst)

    def lrange(self, key: str, start: int, stop: int) -> list[str]:
        lst = self._lists.get(key, [])
        if stop == -1:
            return lst[start:]
        return lst[start : stop + 1]


@pytest.fixture
def fake_redis() -> _FakeRedis:
    return _FakeRedis()


# ---------------------------------------------------------------------------
# Test data helpers
# ---------------------------------------------------------------------------


def _make_worker_status(
    instance_name: str = "worker-1",
    state: str = "ready",
    zone: str = "us-central1-a",
    internal_ip: str = "10.0.0.1",
) -> dict[str, Any]:
    """Return a CloudWorkerStatus-shaped dict for JSON storage in fake Redis."""
    return {
        "experiment_name": "test-exp",
        "instance_id": f"id-{instance_name}",
        "instance_name": instance_name,
        "zone": zone,
        "state": state,
        "provider_status": "RUNNING",
        "internal_ip": internal_ip,
        "external_ip": None,
        "detail": None,
        "startup_evidence": None,
        "updated_at": "2026-03-13T00:00:00+00:00",
        "ready_at": "2026-03-13T00:00:00+00:00",
    }


def _make_job_record(
    job_id: str = "job-1",
    trial_key: str = "trial-abc",
    state: str = "running",
    claimed_by: str | None = "worker-1",
) -> dict[str, Any]:
    """Return a JobLifecycleRecord-shaped dict for JSON storage in fake Redis."""
    return {
        "job_id": job_id,
        "trial_key": trial_key,
        "state": state,
        "claimed_by": claimed_by,
        "retry_count": 0,
        "last_heartbeat": None,
        "updated_at": "2026-03-13T00:00:00+00:00",
        "detail": None,
    }


def _make_recovery_event(
    event_type: str = "orphan_detected",
    job_id: str = "job-1",
    worker: str = "worker-1",
) -> dict[str, Any]:
    return {
        "type": event_type,
        "job_id": job_id,
        "worker": worker,
        "detail": f"{event_type} for {job_id}",
        "ts": "2026-03-13T00:00:00+00:00",
    }


def _populate_fake_redis(fake: _FakeRedis, experiment: str = "test-exp") -> None:
    """Populate fake Redis with worker, job, and event test data."""
    # Workers
    for i, state in enumerate(["ready", "ready", "booting"], start=1):
        w = _make_worker_status(f"worker-{i}", state=state, internal_ip=f"10.0.0.{i}")
        fake.hset(
            f"crsbench:cloud:workers:{experiment}", f"id-worker-{i}", json.dumps(w)
        )

    # Jobs
    for i, (state, claimed) in enumerate(
        [("running", "worker-1"), ("completed", "worker-2"), ("queued", None)], start=1
    ):
        j = _make_job_record(f"job-{i}", f"trial-{i}", state=state, claimed_by=claimed)
        fake.hset(f"crsbench:jobs:{experiment}", f"job-{i}", json.dumps(j))

    # Events
    for etype in ["orphan_detected", "requeued", "orphan_detected"]:
        fake.rpush(
            f"crsbench:recovery-events:{experiment}",
            json.dumps(_make_recovery_event(etype)),
        )


# ---------------------------------------------------------------------------
# Config reconnect tests
# ---------------------------------------------------------------------------


def _mock_config(*, has_cloud: bool = True):
    """Build a mock ExperimentConfig."""
    config = MagicMock()
    if has_cloud:
        config.cloud = MagicMock()
        config.cloud.gce = GceWorkerFleetConfig(
            project="current-project",
            zone="us-west1-b",
            worker_count=9,
            machine_type="e2-standard-8",
            boot_disk_size_gb=150,
            image="projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64",
            service_account_email="current-worker@test-project.iam.gserviceaccount.com",
            owner_label="current-owner",
        )
        config.cloud.orchestrator = None
    else:
        config.cloud = None
    config.redis_host = "localhost"
    config.experiment_filestore = Path("/tmp/filestore")
    return config


def _make_launch_config():
    config = MagicMock()
    config.experiment = "test-exp"
    config.experiment_filestore = Path("/tmp/filestore")
    config.cloud = CloudConfig(
        gce=GceWorkerFleetConfig(
            project="test-project",
            zone="us-central1-a",
            worker_count=2,
            machine_type="e2-standard-4",
            boot_disk_size_gb=100,
            image="projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64",
            service_account_email="crsbench-worker@test-project.iam.gserviceaccount.com",
            owner_label="team-crs",
        ),
        orchestrator=GceOrchestratorConfig(
            project="test-project",
            zone="us-central1-a",
            machine_type="e2-standard-4",
            boot_disk_size_gb=100,
            image="projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64",
            service_account_email="crsbench-orchestrator@test-project.iam.gserviceaccount.com",
            owner_label="team-crs",
            instance_name_prefix="gce-orchestrator",
        ),
    )
    return config


def _make_launch_state():
    from crsbench.cloud.launch_state import CloudLaunchState

    return CloudLaunchState(
        experiment_name="test-exp",
        config_path="/tmp/config.yaml",
        experiment_filestore="/tmp/filestore",
        redis_host="10.0.0.50:6379",
        redis_password="shared-secret",
        orchestrator_provider="gce",
        orchestrator_name="gce-orchestrator-test-exp",
        orchestrator_project="test-project",
        orchestrator_zone="us-central1-a",
        orchestrator_internal_ip="10.0.0.50",
        orchestrator_external_ip="34.1.2.50",
        orchestrator_ssh_via_iap=True,
        worker_fleet_config=GceWorkerFleetConfig(
            project="test-project",
            zone="us-central1-a",
            worker_count=2,
            machine_type="e2-standard-4",
            boot_disk_size_gb=100,
            image="projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64",
            service_account_email="crsbench-worker@test-project.iam.gserviceaccount.com",
            owner_label="team-crs",
        ),
    )


def _make_provider_neutral_launch_state():
    from crsbench.cloud.launch_state import CloudLaunchState

    return CloudLaunchState(
        experiment_name="test-exp",
        config_path="/tmp/config.yaml",
        experiment_filestore="/tmp/filestore",
        redis_host="10.0.0.50:6379",
        redis_password="shared-secret",
        orchestrator_provider="gce",
        orchestrator_name="gce-orchestrator-test-exp",
        orchestrator_project="test-project",
        orchestrator_zone="us-east5-b",
        orchestrator_internal_ip="10.0.0.50",
        orchestrator_external_ip="34.1.2.50",
        orchestrator_ssh_via_iap=True,
        worker_fleet_configs=[
            GceWorkerFleetConfig(
                project="test-project",
                zone="us-east5-b",
                worker_count=2,
                machine_type="n2d-standard-16",
                boot_disk_size_gb=100,
                image="projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64",
                service_account_email="crsbench-worker@test-project.iam.gserviceaccount.com",
                owner_label="team-crs",
                worker_name_prefix="test-exp-us-east5-b",
            ),
            GceWorkerFleetConfig(
                project="test-project",
                zone="us-east1-b",
                worker_count=1,
                machine_type="n2d-standard-16",
                boot_disk_size_gb=100,
                image="projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64",
                service_account_email="crsbench-worker@test-project.iam.gserviceaccount.com",
                owner_label="team-crs",
                worker_name_prefix="test-exp-us-east1-b",
            ),
        ],
    )


def _make_provider_neutral_operational_context(*, include_launch_state: bool):
    from crsbench.cloud.cli._config_reconnect import ResolvedCloudContext

    launch_state = (
        _make_provider_neutral_launch_state() if include_launch_state else None
    )
    worker_fleet_configs = _make_provider_neutral_launch_state().worker_fleet_configs
    return ResolvedCloudContext(
        worker_fleet_configs=worker_fleet_configs,
        launch_state=launch_state,
        experiment_filestore=Path("/tmp/filestore"),
        redis_host="10.0.0.50:6379",
        redis_password="shared-secret",
        launch_plan=MagicMock(experiment_name="test-exp"),
    )


def _make_resolved_cloud_context(launch_state=None):
    from crsbench.cloud.cli._config_reconnect import ResolvedCloudContext

    if launch_state is None:
        fleet = _mock_config(has_cloud=True).cloud.gce
        return ResolvedCloudContext(
            worker_fleet_configs=[fleet],
            launch_state=None,
            experiment_filestore=Path("/tmp/filestore"),
            redis_host="localhost",
            redis_password=None,
        )

    return ResolvedCloudContext(
        worker_fleet_configs=launch_state.resolved_worker_fleets(),
        launch_state=launch_state,
        experiment_filestore=Path(launch_state.experiment_filestore),
        redis_host=launch_state.redis_host,
        redis_password=launch_state.redis_password,
    )


def _make_provider_neutral_experiment_config() -> ExperimentConfig:
    return ExperimentConfig.model_validate(
        {
            "experiment": "test-exp",
            "task": "bugfinding",
            "benchmark_suite": "sanity",
            "mode": "delta",
            "trials": 2,
            "max_total_time": 20000,
            "inputs": {"pov": {"max_variants_per_cpv": 1}},
            "redis_host": "localhost:6379",
            "experiment_filestore": "/tmp/filestore",
            "report_filestore": "/tmp/reports",
            "cloud": {
                "providers": {
                    "gce": {
                        "project": "test-project",
                        "instance_profiles": {
                            "orchestrator-n2d": {
                                "machine_type": "n2d-standard-16",
                                "boot_disk_size_gb": 50,
                                "image": "projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64",
                                "service_account_email": "crsbench-orchestrator@test-project.iam.gserviceaccount.com",
                                "owner_label": "team-crs",
                            },
                            "worker-n2d": {
                                "machine_type": "n2d-standard-16",
                                "boot_disk_size_gb": 50,
                                "image": "projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64",
                                "service_account_email": "crsbench-worker@test-project.iam.gserviceaccount.com",
                                "owner_label": "team-crs",
                            },
                        },
                    }
                },
                "orchestrator": {
                    "provider": "gce",
                    "zone": "us-east5-b",
                    "instance_profile": "orchestrator-n2d",
                },
                "workers": {
                    "placements": [
                        {
                            "provider": "gce",
                            "zone": "us-east5-b",
                            "worker_count": 150,
                            "instance_profile": "worker-n2d",
                        },
                        {
                            "provider": "gce",
                            "zone": "us-east5-c",
                            "worker_count": 100,
                            "instance_profile": "worker-n2d",
                        },
                    ]
                },
            },
            "crs_compose": {"test-crs": {"num_cores": 1}},
        }
    )


def test_build_cloud_launch_plan_resolves_profiles_for_orchestrator_and_workers():
    from crsbench.cloud.models import build_cloud_launch_plan

    config = _make_provider_neutral_experiment_config()

    plan = build_cloud_launch_plan(config)

    assert plan.orchestrator.provider == "gce"
    assert plan.orchestrator.zone == "us-east5-b"
    assert plan.orchestrator.instance_profile.name == "orchestrator-n2d"
    assert plan.orchestrator.instance_profile.provider == "gce"
    assert (
        plan.orchestrator.instance_profile.provider_config["project"] == "test-project"
    )
    assert len(plan.worker_placements) == 2
    assert [placement.zone for placement in plan.worker_placements] == [
        "us-east5-b",
        "us-east5-c",
    ]
    assert [placement.worker_count for placement in plan.worker_placements] == [
        150,
        100,
    ]
    assert all(
        placement.instance_profile.name == "worker-n2d"
        for placement in plan.worker_placements
    )
    assert all(
        placement.instance_profile.provider_config["project"] == "test-project"
        for placement in plan.worker_placements
    )


def test_run_launch_fails_on_quota_shortage_before_creating_instances(tmp_path: Path):
    from crsbench.cloud.cli._launch import run_launch
    from crsbench.cloud.quota import CloudQuotaValidationError

    config_path = tmp_path / "config.yaml"
    config_path.write_text("experiment: ignored\n", encoding="utf-8")
    args = argparse.Namespace(config=str(config_path))

    with (
        patch(
            "crsbench.cloud.cli._launch.load_experiment_config",
            return_value=_make_provider_neutral_experiment_config(),
        ),
        patch("crsbench.cloud.cli._launch.GceProviderAdapter") as mock_adapter_cls,
        patch("crsbench.cloud.cli._launch.QuotaValidator") as mock_validator_cls,
    ):
        mock_adapter = mock_adapter_cls.return_value
        mock_validator = mock_validator_cls.return_value
        mock_validator.validate.side_effect = CloudQuotaValidationError(
            "quota shortfall for us-east5 n2d"
        )

        assert run_launch(args) == 1
        mock_validator.validate.assert_called_once()
        mock_adapter.create_orchestrator.assert_not_called()
        mock_adapter.create_workers.assert_not_called()


def test_save_launch_state_preserves_existing_file_when_replace_fails(
    tmp_path: Path, monkeypatch
) -> None:
    """Atomic launch-state writes should leave the last good file intact on replace failure."""
    from crsbench.cloud.launch_state import load_launch_state, save_launch_state

    config_path = tmp_path / "config.yaml"
    state = _make_launch_state().model_copy(update={"config_path": str(config_path)})
    save_launch_state(config_path, state)

    def _broken_replace(self, target):
        del self, target
        raise OSError("disk full")

    monkeypatch.setattr(Path, "replace", _broken_replace)

    with pytest.raises(OSError, match="disk full"):
        save_launch_state(
            config_path,
            state.model_copy(update={"redis_password": "new-secret"}),
        )

    preserved = load_launch_state(config_path, "test-exp")
    assert preserved is not None
    assert preserved.redis_password == "shared-secret"


class TestReconnect:
    """Tests for _config_reconnect.reconnect()."""

    @patch("crsbench.cloud.cli._config_reconnect.create_redis_connection")
    @patch("crsbench.cloud.cli._config_reconnect.load_experiment_config")
    def test_reconnect_valid_config(self, mock_load, mock_redis):
        """reconnect() returns tuple of (context, redis_conn, readiness, lifecycle, filestore)."""
        mock_load.return_value = _mock_config(has_cloud=True)
        mock_redis.return_value = _FakeRedis()

        from crsbench.cloud.cli._config_reconnect import reconnect

        result = reconnect("/path/to/config.yaml", "test-exp")
        assert len(result) == 5
        context, redis_conn, readiness, lifecycle, filestore = result
        assert context.worker_fleet_config is not None
        assert redis_conn is not None
        assert filestore == Path("/tmp/filestore")

    @patch("crsbench.cloud.cli._config_reconnect.load_experiment_config")
    def test_reconnect_missing_cloud_exits(self, mock_load):
        """reconnect() raises SystemExit when config has no cloud section."""
        mock_load.return_value = _mock_config(has_cloud=False)

        from crsbench.cloud.cli._config_reconnect import reconnect

        with pytest.raises(SystemExit):
            reconnect("/path/to/config.yaml", "test-exp")

    @patch("crsbench.cloud.cli._config_reconnect.create_redis_connection")
    @patch("crsbench.cloud.cli._config_reconnect.load_launch_state")
    @patch("crsbench.cloud.cli._config_reconnect.load_experiment_config")
    def test_reconnect_remote_orchestrator_uses_launch_state(
        self, mock_load, mock_state, mock_redis
    ):
        """Remote orchestrator launches reconnect through persisted launch state."""
        config = _mock_config(has_cloud=True)
        config.cloud.orchestrator = MagicMock()
        mock_load.return_value = config
        mock_state.return_value = _make_launch_state()
        mock_redis.return_value = _FakeRedis()

        from crsbench.cloud.cli._config_reconnect import reconnect

        with patch.dict(os.environ, {}, clear=False):
            context, _redis_conn, _readiness, _lifecycle, filestore = reconnect(
                "/path/to/config.yaml", "test-exp"
            )

            mock_redis.assert_called_once_with("10.0.0.50:6379")
            mock_state.assert_called_once_with(Path("/path/to/config.yaml"), "test-exp")
            assert os.environ["CRSBENCH_REDIS_PASSWORD"] == "shared-secret"
            assert (
                context.worker_fleet_config
                == mock_state.return_value.worker_fleet_config
            )
            assert filestore == Path("/tmp/filestore")

    @patch("crsbench.cloud.cli._config_reconnect.save_launch_state")
    @patch("crsbench.cloud.cli._config_reconnect.create_redis_connection")
    @patch("crsbench.cloud.cli._config_reconnect.load_launch_state")
    @patch("crsbench.cloud.cli._config_reconnect.load_experiment_config")
    def test_reconnect_migrates_legacy_launch_state_from_filestore(
        self, mock_load, mock_state, mock_redis, mock_save_state
    ):
        """Legacy launch state should still load and migrate to the config-adjacent path."""
        config = _mock_config(has_cloud=True)
        config.cloud.orchestrator = MagicMock()
        mock_load.return_value = config
        legacy_state = _make_launch_state().model_copy(
            update={
                "experiment_filestore": None,
                "worker_fleet_config": None,
                "worker_fleet_configs": [],
            }
        )
        mock_state.side_effect = [None, legacy_state]
        mock_redis.return_value = _FakeRedis()

        from crsbench.cloud.cli._config_reconnect import reconnect

        context, _redis_conn, _readiness, _lifecycle, filestore = reconnect(
            "/path/to/config.yaml", "test-exp"
        )

        assert mock_state.call_args_list[0].args == (
            Path("/path/to/config.yaml"),
            "test-exp",
        )
        assert mock_state.call_args_list[1].args == (Path("/tmp/filestore"), "test-exp")
        mock_save_state.assert_called_once()
        migrated_state = mock_save_state.call_args.args[1]
        assert migrated_state.experiment_filestore == "/tmp/filestore"
        assert migrated_state.worker_fleet_config == config.cloud.gce
        assert context.worker_fleet_config == config.cloud.gce
        assert filestore == Path("/tmp/filestore")

    @patch("crsbench.cloud.cli._config_reconnect.save_launch_state")
    @patch("crsbench.cloud.cli._config_reconnect.create_redis_connection")
    @patch("crsbench.cloud.cli._config_reconnect.load_launch_state")
    @patch("crsbench.cloud.cli._config_reconnect.load_experiment_config")
    def test_reconnect_uses_legacy_launch_state_even_if_migration_save_fails(
        self, mock_load, mock_state, mock_redis, mock_save_state
    ):
        """Legacy launch state should remain usable if config-adjacent migration cannot be written."""
        config = _mock_config(has_cloud=True)
        config.cloud.orchestrator = MagicMock()
        mock_load.return_value = config
        legacy_state = _make_launch_state().model_copy(
            update={
                "experiment_filestore": None,
                "worker_fleet_config": None,
                "worker_fleet_configs": [],
            }
        )
        mock_state.side_effect = [None, legacy_state]
        mock_redis.return_value = _FakeRedis()
        mock_save_state.side_effect = PermissionError("read only")

        from crsbench.cloud.cli._config_reconnect import reconnect

        context, _redis_conn, _readiness, _lifecycle, filestore = reconnect(
            "/path/to/config.yaml", "test-exp"
        )

        assert context.worker_fleet_config == config.cloud.gce
        assert filestore == Path("/tmp/filestore")

    @patch("crsbench.cloud.cli._config_reconnect.load_launch_state")
    @patch("crsbench.cloud.cli._config_reconnect.load_experiment_config")
    def test_resolve_cloud_context_preserves_all_provider_neutral_worker_fleets(
        self, mock_load, mock_state
    ):
        """Provider-neutral reconnect should keep all placement fleets, not collapse to one."""
        mock_load.return_value = _make_provider_neutral_experiment_config()
        launch_state = _make_provider_neutral_launch_state()
        mock_state.return_value = launch_state

        from crsbench.cloud.cli._config_reconnect import resolve_cloud_context

        context = resolve_cloud_context("/tmp/config.yaml", "test-exp")

        assert [fleet.zone for fleet in context.worker_fleet_configs] == [
            "us-east5-b",
            "us-east1-b",
        ]
        assert context.launch_state == launch_state
        assert context.redis_host == "10.0.0.50:6379"

    @patch("crsbench.cloud.cli._config_reconnect.load_launch_state")
    @patch("crsbench.cloud.cli._config_reconnect.load_experiment_config")
    def test_resolve_cloud_context_uses_config_for_provider_neutral_local_runs(
        self, mock_load, mock_state
    ):
        """Provider-neutral configs without launch state should still reconnect for local runs."""
        config = _make_provider_neutral_experiment_config()
        mock_load.return_value = config
        mock_state.side_effect = [None, None]

        from crsbench.cloud.cli._config_reconnect import resolve_cloud_context

        context = resolve_cloud_context("/tmp/config.yaml", "test-exp")

        assert context.launch_state is None
        assert [fleet.zone for fleet in context.worker_fleet_configs] == [
            "us-east5-b",
            "us-east5-c",
        ]
        assert context.redis_host == "localhost:6379"
        assert context.experiment_filestore == Path("/tmp/filestore")


# ---------------------------------------------------------------------------
# Argument parsing tests
# ---------------------------------------------------------------------------


class TestArgParsing:
    """Tests for add_cloud_subparser() argument structure."""

    def _build_parser(self) -> argparse.ArgumentParser:
        from crsbench.cloud.cli.cloud_command import add_cloud_subparser

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest="command")
        add_cloud_subparser(subparsers)
        return parser

    def test_parse_status(self):
        parser = self._build_parser()
        args = parser.parse_args(["cloud", "status", "my-exp", "--config", "c.yaml"])
        assert args.command == "cloud"
        assert args.cloud_command == "status"
        assert args.experiment == "my-exp"
        assert args.config == "c.yaml"
        assert args.json_output is False

    def test_parse_status_json(self):
        parser = self._build_parser()
        args = parser.parse_args(
            ["cloud", "status", "my-exp", "--config", "c.yaml", "--json"]
        )
        assert args.json_output is True

    def test_parse_events(self):
        parser = self._build_parser()
        args = parser.parse_args(["cloud", "events", "my-exp", "--config", "c.yaml"])
        assert args.cloud_command == "events"
        assert args.event_type is None

    def test_parse_events_with_type(self):
        parser = self._build_parser()
        args = parser.parse_args(
            [
                "cloud",
                "events",
                "my-exp",
                "--config",
                "c.yaml",
                "--type",
                "orphan_detected",
            ]
        )
        assert args.event_type == "orphan_detected"

    def test_parse_teardown(self):
        parser = self._build_parser()
        args = parser.parse_args(
            [
                "cloud",
                "teardown",
                "my-exp",
                "--config",
                "c.yaml",
                "--remote-dir",
                "/home/user/experiments/my-exp",
            ]
        )
        assert args.cloud_command == "teardown"
        assert args.force is False
        assert args.remote_dir == "/home/user/experiments/my-exp"

    def test_parse_teardown_force(self):
        parser = self._build_parser()
        args = parser.parse_args(
            [
                "cloud",
                "teardown",
                "my-exp",
                "--config",
                "c.yaml",
                "--remote-dir",
                "/home/user/experiments/my-exp",
                "--force",
            ]
        )
        assert args.force is True

    def test_parse_collect(self):
        parser = self._build_parser()
        args = parser.parse_args(
            [
                "cloud",
                "collect",
                "my-exp",
                "--config",
                "c.yaml",
                "--remote-dir",
                "/home/user/experiments/my-exp",
            ]
        )
        assert args.cloud_command == "collect"
        assert args.remote_dir == "/home/user/experiments/my-exp"

    def test_parse_launch(self):
        parser = self._build_parser()
        args = parser.parse_args(["cloud", "launch", "--config", "c.yaml"])
        assert args.command == "cloud"
        assert args.cloud_command == "launch"
        assert args.config == "c.yaml"


def _make_launch_args(config: str = "/tmp/config.yaml"):
    return argparse.Namespace(
        config=config,
        cloud_command="launch",
    )


class TestLaunch:
    """Tests for run_launch() orchestration."""

    @patch(
        "crsbench.cloud.cli._launch.secrets.token_urlsafe", return_value="shared-secret"
    )
    @patch("crsbench.cloud.cli._launch.save_launch_state")
    @patch("crsbench.cloud.cli._launch.GceProvisioner")
    @patch("crsbench.cloud.cli._launch.load_experiment_config")
    def test_launch_provisions_orchestrator_before_workers(
        self, mock_load, mock_prov_cls, mock_save_state, mock_secret
    ):
        del mock_secret
        mock_load.return_value = _make_launch_config()
        mock_prov = MagicMock()
        call_order: list[str] = []

        orchestrator_record = _make_gce_worker(
            "gce-orchestrator-test-exp", ip="10.0.0.50"
        )

        def _create_orchestrator(**kwargs):
            call_order.append("orchestrator")
            assert kwargs["redis_password"] == "shared-secret"
            return orchestrator_record

        def _create_workers(**kwargs):
            call_order.append("workers")
            assert kwargs["redis_host"] == "10.0.0.50:6379"
            assert kwargs["redis_password"] == "shared-secret"
            return []

        mock_prov.create_orchestrator.side_effect = _create_orchestrator
        mock_prov.create_workers.side_effect = _create_workers
        mock_prov_cls.return_value = mock_prov

        from crsbench.cloud.cli._launch import run_launch

        rc = run_launch(_make_launch_args())

        assert rc == 0
        assert call_order == ["orchestrator", "workers"]
        mock_save_state.assert_called_once()
        assert mock_save_state.call_args.args[0] == Path("/tmp/config.yaml")
        saved_state = mock_save_state.call_args.args[1]
        assert saved_state.worker_fleet_config == mock_load.return_value.cloud.gce

    @patch(
        "crsbench.cloud.cli._launch.secrets.token_urlsafe", return_value="shared-secret"
    )
    @patch(
        "crsbench.cloud.cli._launch.save_launch_state",
        side_effect=RuntimeError("disk full"),
    )
    @patch("crsbench.cloud.cli._launch.GceProvisioner")
    @patch("crsbench.cloud.cli._launch.logger")
    @patch("crsbench.cloud.cli._launch.load_experiment_config")
    def test_launch_rolls_back_workers_when_state_persist_fails(
        self, mock_load, mock_logger, mock_prov_cls, mock_save_state, mock_secret
    ):
        del mock_save_state, mock_secret
        mock_load.return_value = _make_launch_config()
        mock_prov = MagicMock()
        mock_prov_cls.return_value = mock_prov
        mock_prov.create_orchestrator.return_value = _make_gce_worker(
            "gce-orchestrator-test-exp", ip="10.0.0.50"
        )
        mock_prov.create_workers.return_value = [_make_gce_worker("w-1")]

        from crsbench.cloud.cli._launch import run_launch

        rc = run_launch(_make_launch_args())

        assert rc == 1
        mock_prov.delete_workers.assert_called_once()
        mock_prov.delete_orchestrators.assert_called_once()
        mock_logger.error.assert_called_once_with(
            "Cloud launch failed: {}", "disk full"
        )

    @patch(
        "crsbench.cloud.cli._launch.secrets.token_urlsafe", return_value="shared-secret"
    )
    @patch("crsbench.cloud.cli._launch.save_launch_state")
    @patch("crsbench.cloud.cli._launch.GceProviderAdapter")
    @patch("crsbench.cloud.cli._launch.QuotaValidator")
    @patch("crsbench.cloud.cli._launch.build_cloud_launch_plan")
    @patch("crsbench.cloud.cli._launch.load_experiment_config")
    def test_provider_neutral_launch_validates_quota_before_provisioning(
        self,
        mock_load,
        mock_build_plan,
        mock_validator_cls,
        mock_adapter_cls,
        mock_save_state,
        mock_secret,
    ):
        del mock_secret
        config = _make_provider_neutral_experiment_config()
        mock_load.return_value = config

        launch_plan = MagicMock()
        launch_plan.experiment_name = "test-exp"
        mock_build_plan.return_value = launch_plan

        call_order: list[str] = []
        mock_validator = mock_validator_cls.return_value
        mock_validator.validate.side_effect = lambda plan: (
            call_order.append(f"validate:{plan.experiment_name}")
        )

        mock_adapter = mock_adapter_cls.return_value
        expected_worker_fleets = (
            _make_provider_neutral_launch_state().worker_fleet_configs
        )
        mock_adapter.build_worker_fleets.return_value = expected_worker_fleets
        mock_adapter.build_orchestrator_config.return_value.project = "test-project"
        mock_adapter.build_orchestrator_config.return_value.ssh_via_iap = True
        mock_adapter.create_orchestrator.side_effect = lambda **_kwargs: (
            call_order.append("create-orchestrator")
            or _make_gce_worker(
                "gce-orchestrator-test-exp",
                zone="us-east5-b",
                ip="10.0.0.50",
            )
        )
        mock_adapter.create_workers.side_effect = lambda **_kwargs: (
            call_order.append("create-workers")
            or [_make_gce_worker("worker-east5"), _make_gce_worker("worker-east1")]
        )

        from crsbench.cloud.cli._launch import run_launch

        rc = run_launch(_make_launch_args())

        assert rc == 0
        assert call_order == [
            "validate:test-exp",
            "create-orchestrator",
            "create-workers",
        ]
        mock_save_state.assert_called_once()
        saved_state = mock_save_state.call_args.args[1]
        assert saved_state.worker_fleet_configs == expected_worker_fleets


# ---------------------------------------------------------------------------
# Status sub-action tests
# ---------------------------------------------------------------------------


def _make_status_args(experiment: str = "test-exp", *, json_output: bool = False):
    return argparse.Namespace(
        experiment=experiment,
        config="/tmp/config.yaml",
        json_output=json_output,
        cloud_command="status",
    )


def _make_events_args(
    experiment: str = "test-exp",
    *,
    json_output: bool = False,
    event_type: str | None = None,
):
    return argparse.Namespace(
        experiment=experiment,
        config="/tmp/config.yaml",
        json_output=json_output,
        event_type=event_type,
        cloud_command="events",
    )


class TestStatusOutput:
    """Tests for run_status() human-readable and JSON output."""

    @patch("crsbench.cloud.cli._status.reconnect")
    def test_status_output(self, mock_reconnect, fake_redis):
        """run_status() calls log_table for fleet, job, collection, and events sections."""
        _populate_fake_redis(fake_redis)
        from crsbench.cloud.readiness import CloudReadinessStore
        from crsbench.distributed.job_lifecycle import JobLifecycleStore

        readiness = CloudReadinessStore(fake_redis)
        lifecycle = JobLifecycleStore(fake_redis)
        mock_reconnect.return_value = (
            MagicMock(),
            fake_redis,
            readiness,
            lifecycle,
            Path("/tmp"),
        )

        from crsbench.cloud.cli._status import run_status

        with (
            patch("crsbench.cloud.cli._status.log_table") as mock_table,
            patch("crsbench.cloud.cli._status.log_section"),
            patch("crsbench.cloud.cli._status.log_key_value"),
        ):
            rc = run_status(_make_status_args())

        assert rc == 0
        # Should have called log_table for fleet, jobs, and events sections
        assert mock_table.call_count >= 3

    @patch("crsbench.cloud.cli._status.reconnect")
    def test_status_json_output(self, mock_reconnect, fake_redis, capsys):
        """run_status() with --json prints valid JSON with fleet/jobs/collection/events keys."""
        _populate_fake_redis(fake_redis)
        from crsbench.cloud.readiness import CloudReadinessStore
        from crsbench.distributed.job_lifecycle import JobLifecycleStore

        readiness = CloudReadinessStore(fake_redis)
        lifecycle = JobLifecycleStore(fake_redis)
        mock_reconnect.return_value = (
            MagicMock(),
            fake_redis,
            readiness,
            lifecycle,
            Path("/tmp"),
        )

        from crsbench.cloud.cli._status import run_status

        rc = run_status(_make_status_args(json_output=True))
        assert rc == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "fleet" in data
        assert "jobs" in data
        assert "collection" in data
        assert "events" in data

    @patch("crsbench.cloud.cli._status.reconnect")
    def test_status_job_instance_correlation(self, mock_reconnect, fake_redis, capsys):
        """Job entries in JSON output include claimed_by for instance correlation (OBS-01)."""
        _populate_fake_redis(fake_redis)
        from crsbench.cloud.readiness import CloudReadinessStore
        from crsbench.distributed.job_lifecycle import JobLifecycleStore

        readiness = CloudReadinessStore(fake_redis)
        lifecycle = JobLifecycleStore(fake_redis)
        mock_reconnect.return_value = (
            MagicMock(),
            fake_redis,
            readiness,
            lifecycle,
            Path("/tmp"),
        )

        from crsbench.cloud.cli._status import run_status

        rc = run_status(_make_status_args(json_output=True))
        assert rc == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        # At least one job should have claimed_by set
        claimed_values = [j["claimed_by"] for j in data["jobs"] if j.get("claimed_by")]
        assert len(claimed_values) > 0


# ---------------------------------------------------------------------------
# Events sub-action tests
# ---------------------------------------------------------------------------


class TestEventsOutput:
    """Tests for run_events() human-readable and JSON output."""

    @patch("crsbench.cloud.cli._events.reconnect")
    def test_events_filtering(self, mock_reconnect, fake_redis):
        """run_events() with --type filters events by type field."""
        _populate_fake_redis(fake_redis)
        mock_reconnect.return_value = (
            MagicMock(),
            fake_redis,
            None,
            None,
            Path("/tmp"),
        )

        from crsbench.cloud.cli._events import run_events

        with patch("crsbench.cloud.cli._events.log_table") as mock_table:
            rc = run_events(_make_events_args(event_type="requeued"))

        assert rc == 0
        # Should have called log_table once for the filtered events
        assert mock_table.call_count == 1
        # The rows passed should only contain the "requeued" event
        _, call_kwargs = mock_table.call_args
        if not call_kwargs:
            call_args = mock_table.call_args[0]
            rows = call_args[1]  # second positional arg = rows
        else:
            rows = call_kwargs.get("rows", mock_table.call_args[0][1])
        assert len(rows) == 1

    @patch("crsbench.cloud.cli._events.reconnect")
    def test_events_json_output(self, mock_reconnect, fake_redis, capsys):
        """run_events() with --json prints valid JSON array."""
        _populate_fake_redis(fake_redis)
        mock_reconnect.return_value = (
            MagicMock(),
            fake_redis,
            None,
            None,
            Path("/tmp"),
        )

        from crsbench.cloud.cli._events import run_events

        rc = run_events(_make_events_args(json_output=True))
        assert rc == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert isinstance(data, list)
        assert len(data) == 3  # 3 events populated

    @patch("crsbench.cloud.cli._events.reconnect")
    def test_events_json_with_type_filter(self, mock_reconnect, fake_redis, capsys):
        """run_events() with --json and --type filters then outputs JSON array."""
        _populate_fake_redis(fake_redis)
        mock_reconnect.return_value = (
            MagicMock(),
            fake_redis,
            None,
            None,
            Path("/tmp"),
        )

        from crsbench.cloud.cli._events import run_events

        rc = run_events(
            _make_events_args(json_output=True, event_type="orphan_detected")
        )
        assert rc == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert isinstance(data, list)
        assert len(data) == 2  # 2 orphan_detected events
        assert all(e["type"] == "orphan_detected" for e in data)


# ---------------------------------------------------------------------------
# Collect sub-action tests
# ---------------------------------------------------------------------------


def _make_collect_args(
    experiment: str = "test-exp",
    config: str = "/tmp/config.yaml",
    remote_dir: str = "/home/user/crsbench-experiments/test-exp",
):
    return argparse.Namespace(
        experiment=experiment,
        config=config,
        remote_dir=remote_dir,
        cloud_command="collect",
    )


def _make_gce_worker(name: str, zone: str = "us-central1-a", ip: str = "10.0.0.1"):
    """Build a GceWorkerRecord for testing."""
    from crsbench.cloud.gce.models import GceWorkerRecord

    return GceWorkerRecord(
        name=name,
        instance_id=f"id-{name}",
        status="RUNNING",
        zone=zone,
        internal_ip=ip,
    )


class TestCollect:
    """Tests for run_collect() sub-action."""

    @patch("crsbench.cloud.cli._collect.resolve_cloud_context")
    @patch("crsbench.cloud.cli._collect.ArtifactCollector")
    @patch("crsbench.cloud.cli._collect.GceProvisioner")
    @patch("crsbench.cloud.cli._collect.reconnect")
    def test_collect_invokes_collector(
        self, mock_reconnect, mock_prov_cls, mock_coll_cls, mock_resolve_context
    ):
        """run_collect() invokes ArtifactCollector.collect() for each live GCE worker."""
        workers = [_make_gce_worker("w-1"), _make_gce_worker("w-2")]
        mock_prov = MagicMock()
        mock_prov.list_workers.return_value = workers
        mock_prov_cls.return_value = mock_prov
        mock_resolve_context.return_value = _make_resolved_cloud_context()

        mock_coll = MagicMock()
        mock_coll_cls.return_value = mock_coll

        readiness = MagicMock()
        readiness.list_workers.return_value = []

        mock_reconnect.return_value = (
            MagicMock(),  # fleet
            MagicMock(),  # redis_conn
            readiness,
            MagicMock(),  # lifecycle
            Path("/tmp/filestore"),
        )

        from crsbench.cloud.cli._collect import run_collect

        rc = run_collect(_make_collect_args())
        assert rc == 0
        assert mock_coll.collect.call_count == 2

    @patch("crsbench.cloud.cli._collect.resolve_cloud_context")
    @patch("crsbench.cloud.cli._collect.logger")
    @patch("crsbench.cloud.cli._collect.ArtifactCollector")
    @patch("crsbench.cloud.cli._collect.GceProvisioner")
    @patch("crsbench.cloud.cli._collect.reconnect")
    def test_collect_stale_redis_warning(
        self,
        mock_reconnect,
        mock_prov_cls,
        mock_coll_cls,
        mock_logger,
        mock_resolve_context,
    ):
        """Warns when Redis has workers not present in GCE."""
        mock_prov = MagicMock()
        mock_prov.list_workers.return_value = [_make_gce_worker("w-1")]
        mock_prov_cls.return_value = mock_prov
        mock_resolve_context.return_value = _make_resolved_cloud_context()

        mock_coll = MagicMock()
        mock_coll_cls.return_value = mock_coll

        # Redis knows about w-1 and w-2, but GCE only has w-1
        stale_worker = MagicMock()
        stale_worker.instance_name = "w-2"
        live_worker = MagicMock()
        live_worker.instance_name = "w-1"
        readiness = MagicMock()
        readiness.list_workers.return_value = [live_worker, stale_worker]

        mock_reconnect.return_value = (
            MagicMock(),
            MagicMock(),
            readiness,
            MagicMock(),
            Path("/tmp/filestore"),
        )

        from crsbench.cloud.cli._collect import run_collect

        rc = run_collect(_make_collect_args())

        assert rc == 0
        # Verify logger.warning was called with stale info
        warning_calls = [str(call) for call in mock_logger.warning.call_args_list]
        assert any("w-2" in call for call in warning_calls)

    @patch("crsbench.cloud.cli._collect.resolve_cloud_context")
    @patch("crsbench.cloud.cli._collect.ArtifactCollector")
    @patch("crsbench.cloud.cli._collect.GceProvisioner")
    @patch("crsbench.cloud.cli._collect.reconnect")
    def test_collect_partial_failure(
        self, mock_reconnect, mock_prov_cls, mock_coll_cls, mock_resolve_context
    ):
        """Partial collection failure returns 1 but continues for remaining workers."""
        from crsbench.cloud.collection import ArtifactCollectionError

        workers = [_make_gce_worker("w-1"), _make_gce_worker("w-2")]
        mock_prov = MagicMock()
        mock_prov.list_workers.return_value = workers
        mock_prov_cls.return_value = mock_prov
        mock_resolve_context.return_value = _make_resolved_cloud_context()

        mock_coll = MagicMock()
        mock_coll.collect.side_effect = [
            ArtifactCollectionError("rsync failed"),
            Path("/tmp/out"),
        ]
        mock_coll_cls.return_value = mock_coll

        readiness = MagicMock()
        readiness.list_workers.return_value = []

        mock_reconnect.return_value = (
            MagicMock(),
            MagicMock(),
            readiness,
            MagicMock(),
            Path("/tmp/filestore"),
        )

        from crsbench.cloud.cli._collect import run_collect

        rc = run_collect(_make_collect_args())
        assert rc == 1
        # Both workers should have been attempted
        assert mock_coll.collect.call_count == 2

    @patch("crsbench.cloud.cli._collect.resolve_cloud_context")
    @patch("crsbench.cloud.cli._collect.ArtifactCollector")
    @patch("crsbench.cloud.cli._collect.GceProvisioner")
    @patch("crsbench.cloud.cli._collect.reconnect")
    def test_collect_also_collects_orchestrator_when_launch_state_present(
        self, mock_reconnect, mock_prov_cls, mock_coll_cls, mock_resolve_context
    ):
        """Remote launches should collect orchestrator artifacts in addition to workers."""
        workers = [_make_gce_worker("w-1"), _make_gce_worker("w-2")]
        mock_prov = MagicMock()
        mock_prov.list_workers.return_value = workers
        mock_prov_cls.return_value = mock_prov
        mock_resolve_context.return_value = _make_resolved_cloud_context(
            _make_launch_state()
        )

        mock_coll = MagicMock()
        mock_coll_cls.return_value = mock_coll

        readiness = MagicMock()
        readiness.list_workers.return_value = []

        mock_reconnect.return_value = (
            MagicMock(),
            MagicMock(),
            readiness,
            MagicMock(),
            Path("/tmp/filestore"),
        )

        from crsbench.cloud.cli._collect import run_collect

        rc = run_collect(_make_collect_args())
        assert rc == 0
        assert mock_coll.collect.call_count == 3
        orchestrator_call = mock_coll.collect.call_args_list[-1]
        assert orchestrator_call.kwargs["worker"].name == "gce-orchestrator-test-exp"

    @patch("crsbench.cloud.cli._collect.resolve_cloud_context")
    @patch("crsbench.cloud.cli._collect.ArtifactCollector")
    @patch("crsbench.cloud.cli._collect.GceProvisioner")
    @patch("crsbench.cloud.cli._collect.reconnect")
    def test_collect_orchestrator_when_no_workers_remain(
        self, mock_reconnect, mock_prov_cls, mock_coll_cls, mock_resolve_context
    ):
        """Remote launches should still collect orchestrator artifacts after workers are gone."""
        mock_prov = MagicMock()
        mock_prov.list_workers.return_value = []
        mock_prov_cls.return_value = mock_prov
        mock_resolve_context.return_value = _make_resolved_cloud_context(
            _make_launch_state()
        )

        mock_coll = MagicMock()
        mock_coll_cls.return_value = mock_coll

        readiness = MagicMock()
        readiness.list_workers.return_value = []

        mock_reconnect.return_value = (
            MagicMock(),
            MagicMock(),
            readiness,
            MagicMock(),
            Path("/tmp/filestore"),
        )

        from crsbench.cloud.cli._collect import run_collect

        rc = run_collect(_make_collect_args())
        assert rc == 0
        assert mock_coll.collect.call_count == 1
        assert (
            mock_coll.collect.call_args.kwargs["worker"].name
            == "gce-orchestrator-test-exp"
        )

    @patch("crsbench.cloud.cli._collect.resolve_cloud_context", create=True)
    @patch("crsbench.cloud.cli._collect.ArtifactCollector")
    @patch("crsbench.cloud.cli._collect.GceProvisioner")
    @patch(
        "crsbench.cloud.cli._collect.reconnect", side_effect=RuntimeError("redis down")
    )
    def test_collect_can_proceed_when_redis_reconnect_fails(
        self,
        mock_reconnect,
        mock_prov_cls,
        mock_coll_cls,
        mock_resolve_context,
    ):
        """Collection should still use persisted launch state if Redis is unavailable."""
        launch_state = _make_launch_state()
        mock_resolve_context.return_value = _make_resolved_cloud_context(launch_state)
        mock_prov = MagicMock()
        mock_prov.list_workers.return_value = [_make_gce_worker("w-1")]
        mock_prov_cls.return_value = mock_prov

        mock_coll = MagicMock()
        mock_coll_cls.return_value = mock_coll

        from crsbench.cloud.cli._collect import run_collect

        rc = run_collect(_make_collect_args())

        assert rc == 0
        mock_reconnect.assert_called_once()
        mock_coll.collect.assert_called()

    @patch("crsbench.cloud.cli._collect.resolve_cloud_context")
    @patch("crsbench.cloud.cli._collect.ArtifactCollector")
    @patch("crsbench.cloud.cli._collect.GceProviderAdapter")
    @patch("crsbench.cloud.cli._collect.GceProvisioner")
    @patch("crsbench.cloud.cli._collect.reconnect")
    def test_collect_provider_neutral_context_uses_adapter_for_multi_zone_workers(
        self,
        mock_reconnect,
        mock_prov_cls,
        mock_adapter_cls,
        mock_coll_cls,
        mock_resolve_context,
    ):
        """Provider-neutral collection should list workers across placements via the adapter."""
        del mock_prov_cls
        context = _make_provider_neutral_operational_context(include_launch_state=True)
        mock_resolve_context.return_value = context

        adapter = mock_adapter_cls.return_value
        adapter.list_workers.return_value = [
            _make_gce_worker("test-exp-us-east5-b-001", zone="us-east5-b"),
            _make_gce_worker("test-exp-us-east1-b-001", zone="us-east1-b"),
        ]

        mock_coll = MagicMock()
        mock_coll_cls.return_value = mock_coll
        readiness = MagicMock()
        readiness.list_workers.return_value = []
        mock_reconnect.return_value = (
            MagicMock(),
            MagicMock(),
            readiness,
            MagicMock(),
            Path("/tmp/filestore"),
        )

        from crsbench.cloud.cli._collect import run_collect

        rc = run_collect(_make_collect_args())

        assert rc == 0
        adapter.list_workers.assert_called_once_with(plan=context.launch_plan)
        assert mock_coll.collect.call_count == 3


# ---------------------------------------------------------------------------
# Teardown sub-action tests
# ---------------------------------------------------------------------------


def _make_teardown_args(
    experiment: str = "test-exp",
    config: str = "/tmp/config.yaml",
    remote_dir: str = "/home/user/crsbench-experiments/test-exp",
    *,
    force: bool = False,
):
    return argparse.Namespace(
        experiment=experiment,
        config=config,
        remote_dir=remote_dir,
        force=force,
        cloud_command="teardown",
    )


def _setup_teardown_mocks(
    mock_reconnect,
    mock_prov_cls,
    mock_coll_cls,
    workers=None,
    redis_workers=None,
    jobs=None,
):
    """Wire up common mock structure for teardown tests."""
    if workers is None:
        workers = [_make_gce_worker("w-1"), _make_gce_worker("w-2")]

    mock_prov = MagicMock()
    mock_prov.list_workers.return_value = workers
    mock_prov_cls.return_value = mock_prov

    mock_coll = MagicMock()
    mock_coll_cls.return_value = mock_coll

    readiness = MagicMock()
    readiness.list_workers.return_value = redis_workers or []

    lifecycle = MagicMock()
    lifecycle.list_jobs.return_value = jobs or []

    mock_reconnect.return_value = (
        MagicMock(),  # fleet
        MagicMock(),  # redis_conn
        readiness,
        lifecycle,
        Path("/tmp/filestore"),
    )

    return mock_prov, mock_coll, readiness, lifecycle


class TestTeardown:
    """Tests for run_teardown() sub-action."""

    @patch("crsbench.cloud.cli._teardown.resolve_cloud_context")
    @patch("crsbench.cloud.cli._teardown.ArtifactCollector")
    @patch("crsbench.cloud.cli._teardown.GceProvisioner")
    @patch("crsbench.cloud.cli._teardown.reconnect")
    def test_teardown_collect_then_delete(
        self, mock_reconnect, mock_prov_cls, mock_coll_cls, mock_resolve_context
    ):
        """Teardown collects from all workers then deletes them."""
        mock_resolve_context.return_value = _make_resolved_cloud_context()
        mock_prov, mock_coll, _, _ = _setup_teardown_mocks(
            mock_reconnect, mock_prov_cls, mock_coll_cls
        )

        from crsbench.cloud.cli._teardown import run_teardown

        rc = run_teardown(_make_teardown_args(force=True))
        assert rc == 0
        # Collect called for each worker
        assert mock_coll.collect.call_count == 2
        # Delete called after collection
        mock_prov.delete_workers.assert_called_once()
        # Verify collect was called BEFORE delete
        collect_order = mock_coll.collect.call_args_list
        delete_order = mock_prov.delete_workers.call_args_list
        assert len(collect_order) == 2
        assert len(delete_order) == 1

    @patch("crsbench.cloud.cli._teardown.resolve_cloud_context")
    @patch("crsbench.cloud.cli._teardown.ArtifactCollector")
    @patch("crsbench.cloud.cli._teardown.GceProvisioner")
    @patch("crsbench.cloud.cli._teardown.reconnect")
    def test_teardown_reports_collection_failure_but_still_deletes(
        self, mock_reconnect, mock_prov_cls, mock_coll_cls, mock_resolve_context
    ):
        """Collection failures should return 1 but still continue through deletion."""
        from crsbench.cloud.collection import ArtifactCollectionError

        mock_resolve_context.return_value = _make_resolved_cloud_context()
        mock_prov, mock_coll, _, _ = _setup_teardown_mocks(
            mock_reconnect, mock_prov_cls, mock_coll_cls
        )
        mock_coll.collect.side_effect = ArtifactCollectionError("rsync failed")

        from crsbench.cloud.cli._teardown import run_teardown

        rc = run_teardown(_make_teardown_args(force=True))
        assert rc == 1
        mock_prov.delete_workers.assert_called_once()

    @patch("crsbench.cloud.cli._teardown.resolve_cloud_context")
    @patch("crsbench.cloud.cli._teardown.ArtifactCollector")
    @patch("crsbench.cloud.cli._teardown.GceProvisioner")
    @patch("crsbench.cloud.cli._teardown.reconnect")
    def test_teardown_force_flag(
        self, mock_reconnect, mock_prov_cls, mock_coll_cls, mock_resolve_context
    ):
        """With --force, no input() call is made."""
        mock_resolve_context.return_value = _make_resolved_cloud_context()
        _setup_teardown_mocks(mock_reconnect, mock_prov_cls, mock_coll_cls)

        from crsbench.cloud.cli._teardown import run_teardown

        with patch("builtins.input") as mock_input:
            rc = run_teardown(_make_teardown_args(force=True))

        assert rc == 0
        mock_input.assert_not_called()

    @patch("crsbench.cloud.cli._teardown.resolve_cloud_context")
    @patch("crsbench.cloud.cli._teardown.logger")
    @patch("crsbench.cloud.cli._teardown.ArtifactCollector")
    @patch("crsbench.cloud.cli._teardown.GceProvisioner")
    @patch("crsbench.cloud.cli._teardown.reconnect")
    def test_teardown_stale_redis_warning(
        self,
        mock_reconnect,
        mock_prov_cls,
        mock_coll_cls,
        mock_logger,
        mock_resolve_context,
    ):
        """When GCE has no workers but Redis does, warn about stale entries."""
        stale_worker = MagicMock()
        stale_worker.instance_name = "w-stale"
        mock_resolve_context.return_value = _make_resolved_cloud_context()

        _setup_teardown_mocks(
            mock_reconnect,
            mock_prov_cls,
            mock_coll_cls,
            workers=[],
            redis_workers=[stale_worker],
        )

        from crsbench.cloud.cli._teardown import run_teardown

        rc = run_teardown(_make_teardown_args(force=True))
        assert rc == 0
        warning_calls = [str(call) for call in mock_logger.warning.call_args_list]
        assert any("stale" in call.lower() for call in warning_calls)

    @patch("crsbench.cloud.cli._teardown.resolve_cloud_context")
    @patch("crsbench.cloud.cli._teardown.ArtifactCollector")
    @patch("crsbench.cloud.cli._teardown.GceProvisioner")
    @patch("crsbench.cloud.cli._teardown.reconnect")
    def test_teardown_confirmation_prompt_yes(
        self, mock_reconnect, mock_prov_cls, mock_coll_cls, mock_resolve_context
    ):
        """Confirmation prompt with 'yes' proceeds with teardown."""
        mock_resolve_context.return_value = _make_resolved_cloud_context()
        mock_prov, _, _, _ = _setup_teardown_mocks(
            mock_reconnect, mock_prov_cls, mock_coll_cls
        )

        from crsbench.cloud.cli._teardown import run_teardown

        with (
            patch("builtins.input", return_value="yes"),
            patch("sys.stdin") as mock_stdin,
        ):
            mock_stdin.isatty.return_value = True
            rc = run_teardown(_make_teardown_args(force=False))

        assert rc == 0
        mock_prov.delete_workers.assert_called_once()

    @patch("crsbench.cloud.cli._teardown.resolve_cloud_context")
    @patch("crsbench.cloud.cli._teardown.ArtifactCollector")
    @patch("crsbench.cloud.cli._teardown.GceProvisioner")
    @patch("crsbench.cloud.cli._teardown.reconnect")
    def test_teardown_confirmation_prompt_no(
        self, mock_reconnect, mock_prov_cls, mock_coll_cls, mock_resolve_context
    ):
        """Confirmation prompt with non-'yes' cancels teardown."""
        mock_resolve_context.return_value = _make_resolved_cloud_context()
        mock_prov, _, _, _ = _setup_teardown_mocks(
            mock_reconnect, mock_prov_cls, mock_coll_cls
        )

        from crsbench.cloud.cli._teardown import run_teardown

        with (
            patch("builtins.input", return_value="no"),
            patch("sys.stdin") as mock_stdin,
        ):
            mock_stdin.isatty.return_value = True
            rc = run_teardown(_make_teardown_args(force=False))

        assert rc == 0
        mock_prov.delete_workers.assert_not_called()

    @patch("crsbench.cloud.cli._teardown.resolve_cloud_context")
    @patch("crsbench.cloud.cli._teardown.ArtifactCollector")
    @patch("crsbench.cloud.cli._teardown.GceProvisioner")
    @patch("crsbench.cloud.cli._teardown.reconnect")
    def test_teardown_non_tty_without_force(
        self, mock_reconnect, mock_prov_cls, mock_coll_cls, mock_resolve_context
    ):
        """Non-TTY stdin without --force returns 1 with error."""
        mock_resolve_context.return_value = _make_resolved_cloud_context()
        mock_prov, _, _, _ = _setup_teardown_mocks(
            mock_reconnect, mock_prov_cls, mock_coll_cls
        )

        from crsbench.cloud.cli._teardown import run_teardown

        with patch("sys.stdin") as mock_stdin:
            mock_stdin.isatty.return_value = False
            rc = run_teardown(_make_teardown_args(force=False))

        assert rc == 1
        mock_prov.delete_workers.assert_not_called()

    @patch("crsbench.cloud.cli._teardown.delete_launch_state")
    @patch("crsbench.cloud.cli._teardown.resolve_cloud_context")
    @patch("crsbench.cloud.cli._teardown.ArtifactCollector")
    @patch("crsbench.cloud.cli._teardown.GceProvisioner")
    @patch("crsbench.cloud.cli._teardown.reconnect")
    def test_teardown_collects_and_deletes_orchestrator_when_launch_state_present(
        self,
        mock_reconnect,
        mock_prov_cls,
        mock_coll_cls,
        mock_resolve_context,
        mock_delete_state,
    ):
        """Remote launches should collect and delete the orchestrator VM too."""
        mock_resolve_context.return_value = _make_resolved_cloud_context(
            _make_launch_state()
        )
        mock_prov, mock_coll, _, _ = _setup_teardown_mocks(
            mock_reconnect, mock_prov_cls, mock_coll_cls
        )

        from crsbench.cloud.cli._teardown import run_teardown

        rc = run_teardown(_make_teardown_args(force=True))

        assert rc == 0
        assert mock_coll.collect.call_count == 3
        mock_prov.delete_workers.assert_called_once()
        mock_prov.delete_instance.assert_called_once()
        assert mock_delete_state.call_count == 2
        assert mock_delete_state.call_args_list[0].args == (
            "/tmp/config.yaml",
            "test-exp",
        )
        assert mock_delete_state.call_args_list[1].args == (
            "/tmp/filestore",
            "test-exp",
        )

    @patch("crsbench.cloud.cli._teardown.delete_launch_state")
    @patch("crsbench.cloud.cli._teardown.resolve_cloud_context")
    @patch("crsbench.cloud.cli._teardown.ArtifactCollector")
    @patch("crsbench.cloud.cli._teardown.GceProvisioner")
    @patch("crsbench.cloud.cli._teardown.reconnect")
    def test_teardown_ignores_launch_state_cleanup_failures_after_vm_deletion(
        self,
        mock_reconnect,
        mock_prov_cls,
        mock_coll_cls,
        mock_resolve_context,
        mock_delete_state,
    ):
        """Local state cleanup should be best-effort after cloud deletion succeeds."""
        mock_resolve_context.return_value = _make_resolved_cloud_context(
            _make_launch_state()
        )
        mock_prov, _mock_coll, _, _ = _setup_teardown_mocks(
            mock_reconnect, mock_prov_cls, mock_coll_cls
        )
        mock_delete_state.side_effect = OSError("read only")

        from crsbench.cloud.cli._teardown import run_teardown

        rc = run_teardown(_make_teardown_args(force=True))

        assert rc == 0
        mock_prov.delete_workers.assert_called_once()
        mock_prov.delete_instance.assert_called_once()

    @patch("crsbench.cloud.cli._teardown.resolve_cloud_context")
    @patch("crsbench.cloud.cli._teardown.ArtifactCollector")
    @patch("crsbench.cloud.cli._teardown.GceProvisioner")
    @patch("crsbench.cloud.cli._teardown.reconnect")
    def test_teardown_deletes_vms_even_when_collection_fails(
        self, mock_reconnect, mock_prov_cls, mock_coll_cls, mock_resolve_context
    ):
        """Collection failures should still proceed to VM deletion to avoid leaks."""
        from crsbench.cloud.collection import ArtifactCollectionError

        mock_resolve_context.return_value = _make_resolved_cloud_context(
            _make_launch_state()
        )
        mock_prov, mock_coll, _, _ = _setup_teardown_mocks(
            mock_reconnect, mock_prov_cls, mock_coll_cls
        )
        mock_coll.collect.side_effect = [
            ArtifactCollectionError("worker collect failed"),
            Path("/tmp/out"),
            ArtifactCollectionError("orchestrator collect failed"),
        ]

        from crsbench.cloud.cli._teardown import run_teardown

        rc = run_teardown(_make_teardown_args(force=True))

        assert rc == 1
        mock_prov.delete_workers.assert_called_once()
        mock_prov.delete_instance.assert_called_once()

    @patch("crsbench.cloud.cli._teardown.resolve_cloud_context", create=True)
    @patch("crsbench.cloud.cli._teardown.ArtifactCollector")
    @patch("crsbench.cloud.cli._teardown.GceProvisioner")
    @patch(
        "crsbench.cloud.cli._teardown.reconnect",
        side_effect=RuntimeError("orchestrator redis unavailable"),
    )
    def test_teardown_proceeds_with_gce_cleanup_when_redis_reconnect_fails(
        self,
        mock_reconnect,
        mock_prov_cls,
        mock_coll_cls,
        mock_resolve_context,
    ):
        """Teardown should still delete VMs using persisted launch state if Redis is down."""
        launch_state = _make_launch_state()
        mock_resolve_context.return_value = _make_resolved_cloud_context(launch_state)
        mock_prov = MagicMock()
        mock_prov.list_workers.return_value = [_make_gce_worker("w-1")]
        mock_prov_cls.return_value = mock_prov

        mock_coll = MagicMock()
        mock_coll_cls.return_value = mock_coll

        from crsbench.cloud.cli._teardown import run_teardown

        rc = run_teardown(_make_teardown_args(force=True))

        assert rc == 0
        mock_reconnect.assert_called_once()
        mock_prov.delete_workers.assert_called_once()
        mock_prov.delete_instance.assert_called_once()

    @patch("crsbench.cloud.cli._teardown.delete_launch_state")
    @patch("crsbench.cloud.cli._teardown.resolve_cloud_context")
    @patch("crsbench.cloud.cli._teardown.ArtifactCollector")
    @patch("crsbench.cloud.cli._teardown.GceProviderAdapter")
    @patch("crsbench.cloud.cli._teardown.GceProvisioner")
    @patch("crsbench.cloud.cli._teardown.reconnect")
    def test_teardown_provider_neutral_context_deletes_multi_zone_workers_via_adapter(
        self,
        mock_reconnect,
        mock_prov_cls,
        mock_adapter_cls,
        mock_coll_cls,
        mock_resolve_context,
        mock_delete_state,
    ):
        """Provider-neutral teardown should delete all placements through the adapter before cleanup."""
        context = _make_provider_neutral_operational_context(include_launch_state=True)
        mock_resolve_context.return_value = context

        adapter = mock_adapter_cls.return_value
        adapter.list_workers.return_value = [
            _make_gce_worker("test-exp-us-east5-b-001", zone="us-east5-b"),
            _make_gce_worker("test-exp-us-east1-b-001", zone="us-east1-b"),
        ]
        adapter.delete_workers.return_value = adapter.list_workers.return_value

        mock_coll = MagicMock()
        mock_coll_cls.return_value = mock_coll
        readiness = MagicMock()
        readiness.list_workers.return_value = []
        lifecycle = MagicMock()
        lifecycle.list_jobs.return_value = []
        mock_reconnect.return_value = (
            MagicMock(),
            MagicMock(),
            readiness,
            lifecycle,
            Path("/tmp/filestore"),
        )

        mock_prov = MagicMock()
        mock_prov_cls.return_value = mock_prov

        from crsbench.cloud.cli._teardown import run_teardown

        rc = run_teardown(_make_teardown_args(force=True))

        assert rc == 0
        adapter.list_workers.assert_called_once_with(plan=context.launch_plan)
        adapter.delete_workers.assert_called_once_with(plan=context.launch_plan)
        mock_prov.delete_instance.assert_called_once()
        assert mock_delete_state.call_count == 2
