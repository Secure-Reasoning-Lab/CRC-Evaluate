"""Unit tests for crsbench cloud CLI command -- status, events, config reconnect."""

from __future__ import annotations

import argparse
import dataclasses
import io
import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock
from unittest.mock import MagicMock, patch

import pytest
from crsbench.cloud.cli._instance_inventory import CloudInstanceInventoryRow
from crsbench.cloud.collection import collect_marker_path, read_collect_marker
from crsbench.cloud.records import CloudFleetPlacementRecord
from crsbench.cloud.types import CloudProvider
from crsbench.distributed.queue import RedisConnectionProbe
from crsbench.experiment.trial_selection import (
    TRIAL_KEY_ALLOWLIST_ENV_VAR,
    encode_trial_key_allowlist,
)
from crsbench.validation.schemas import (
    CloudBootstrapConfig,
    ExperimentConfig,
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
    role: str = "worker",
) -> dict[str, Any]:
    """Return a CloudWorkerStatus-shaped dict for JSON storage in fake Redis."""
    return {
        "experiment_name": "test-exp",
        "instance_id": f"id-{instance_name}",
        "instance_name": instance_name,
        "zone": zone,
        "state": state,
        "role": role,
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


def _make_inventory_row(
    *,
    alias: str,
    name: str,
    role: str,
    zone: str = "us-central1-a",
) -> CloudInstanceInventoryRow:
    """Return a live inventory row for cloud remote-access selector tests."""
    return CloudInstanceInventoryRow(
        alias=alias,
        name=name,
        role=role,
        placement_source="config",
        provider="gce",
        project="test-project",
        zone=zone,
        region="us-central1",
        status="RUNNING",
        internal_ip="10.0.0.10",
        external_ip=None,
        ssh_via_iap=True,
    )


def _make_recovery_event(
    event_type: str = "orphan_detected",
    job_id: str = "job-1",
    worker: str = "worker-1",
) -> dict[str, Any]:
    return {
        "event": event_type,
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
    if has_cloud:
        return _make_provider_neutral_experiment_config()

    config = MagicMock()
    config.cloud = None
    config.redis_host = "localhost"
    config.experiment_filestore = Path("/tmp/filestore")
    return config


def _make_launch_config():
    return _make_provider_neutral_experiment_config()


def _make_launch_state():
    from crsbench.cloud.launch_state import CloudLaunchState

    return CloudLaunchState(
        experiment_name="test-exp",
        config_path="/tmp/config.yaml",
        experiment_filestore="/tmp/filestore",
        remote_experiment_root="/tmp/remote-root",
        redis_host="10.0.0.50:6379",
        redis_password="shared-secret",
        orchestrator_provider=CloudProvider.GCE,
        orchestrator_name="gce-orchestrator-test-exp",
        orchestrator_project="test-project",
        orchestrator_zone="us-central1-a",
        orchestrator_internal_ip="10.0.0.50",
        orchestrator_external_ip="34.1.2.50",
        orchestrator_ssh_via_iap=True,
        worker_fleet_configs=[
            CloudFleetPlacementRecord(
                provider=CloudProvider.GCE,
                role="worker",
                project="test-project",
                zone="us-central1-a",
                zones=["us-central1-a"],
                region="us-central1",
                owner_label="team-crs",
                count=2,
                name_prefix="crsbench-test-exp-work",
                name_start_index=1,
                ssh_via_iap=True,
                provider_metadata={
                    "project": "test-project",
                    "zone": "us-central1-a",
                    "zones": ["us-central1-a"],
                    "worker_count": 2,
                    "worker_name_start_index": 1,
                    "worker_name_prefix": "crsbench-test-exp-work",
                    "machine_type": "e2-standard-4",
                    "boot_disk_size_gb": 100,
                    "image": "projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64",
                    "service_account_email": "crsbench-worker@test-project.iam.gserviceaccount.com",
                    "owner_label": "team-crs",
                    "ssh_via_iap": True,
                },
            )
        ],
    )


def _make_provider_neutral_launch_state():
    from crsbench.cloud.launch_state import CloudLaunchState

    return CloudLaunchState(
        experiment_name="test-exp",
        config_path="/tmp/config.yaml",
        experiment_filestore="/tmp/filestore",
        remote_experiment_root="/tmp/remote-root",
        redis_host="10.0.0.50:6379",
        redis_password="shared-secret",
        orchestrator_provider=CloudProvider.GCE,
        orchestrator_name="gce-orchestrator-test-exp",
        orchestrator_project="test-project",
        orchestrator_zone="us-east5-b",
        orchestrator_internal_ip="10.0.0.50",
        orchestrator_external_ip="34.1.2.50",
        orchestrator_ssh_via_iap=True,
        worker_fleet_configs=[
            CloudFleetPlacementRecord(
                provider=CloudProvider.GCE,
                role="worker",
                project="test-project",
                zone="us-east5-b",
                zones=["us-east5-b"],
                region="us-east5",
                owner_label="team-crs",
                count=2,
                name_prefix="test-exp-us-east5-b",
                name_start_index=1,
                ssh_via_iap=True,
                provider_metadata={
                    "project": "test-project",
                    "zone": "us-east5-b",
                    "zones": ["us-east5-b"],
                    "worker_count": 2,
                    "worker_name_start_index": 1,
                    "worker_name_prefix": "test-exp-us-east5-b",
                    "machine_type": "n2d-standard-16",
                    "boot_disk_size_gb": 100,
                    "image": "projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64",
                    "service_account_email": "crsbench-worker@test-project.iam.gserviceaccount.com",
                    "owner_label": "team-crs",
                    "ssh_via_iap": True,
                },
            ),
            CloudFleetPlacementRecord(
                provider=CloudProvider.GCE,
                role="worker",
                project="test-project",
                zone="us-east1-b",
                zones=["us-east1-b"],
                region="us-east1",
                owner_label="team-crs",
                count=1,
                name_prefix="test-exp-us-east1-b",
                name_start_index=3,
                ssh_via_iap=True,
                provider_metadata={
                    "project": "test-project",
                    "zone": "us-east1-b",
                    "zones": ["us-east1-b"],
                    "worker_count": 1,
                    "worker_name_start_index": 3,
                    "worker_name_prefix": "test-exp-us-east1-b",
                    "machine_type": "n2d-standard-16",
                    "boot_disk_size_gb": 100,
                    "image": "projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64",
                    "service_account_email": "crsbench-worker@test-project.iam.gserviceaccount.com",
                    "owner_label": "team-crs",
                    "ssh_via_iap": True,
                },
            ),
        ],
    )


def _make_reeval_launch_state():
    return _make_provider_neutral_launch_state().model_copy(
        update={
            "experiment_name": "test-exp-reeval-20260424-010203",
            "launch_mode": "reeval",
            "source_experiment_name": "test-exp",
            "remote_experiment_name": "test-exp-reeval-20260424-010203",
            "worker_fleet_configs": [],
            "remote_experiment_root": "/tmp/remote-root/.crsbench-cloud/reeval/test-exp-reeval-20260424-010203/workspace",
            "remote_submission_dir": "/tmp/remote-root/.crsbench-cloud/reeval/test-exp-reeval-20260424-010203",
            "remote_bundle_path": "/tmp/remote-root/.crsbench-cloud/reeval/test-exp-reeval-20260424-010203/bundle",
        }
    )


def _make_provider_neutral_operational_context(
    *,
    include_launch_state: bool,
    launch_plan: object | None = None,
):
    from crsbench.cloud.cli._config_reconnect import ResolvedCloudContext

    launch_state = (
        _make_provider_neutral_launch_state() if include_launch_state else None
    )
    worker_fleet_configs = _make_provider_neutral_launch_state().worker_fleet_configs
    return ResolvedCloudContext(
        experiment_name="test-exp",
        worker_fleet_configs=worker_fleet_configs,
        launch_state=launch_state,
        experiment_filestore=Path("/tmp/filestore"),
        remote_experiment_root=Path("/tmp/remote-root"),
        redis_host="10.0.0.50:6379",
        redis_password="shared-secret",
        launch_plan=launch_plan if launch_plan is not None else MagicMock(),
    )


def _make_stable_worker_fleet(
    *,
    zone: str,
    zones: list[str] | None = None,
    start_index: int,
    worker_count: int,
    prefix: str = "crsbench-test-exp-work",
) -> CloudFleetPlacementRecord:
    return CloudFleetPlacementRecord(
        provider=CloudProvider.GCE,
        role="worker",
        project="test-project",
        zone=zone,
        zones=zones or [zone],
        region=zone.rsplit("-", 1)[0],
        owner_label="team-crs",
        count=worker_count,
        name_start_index=start_index,
        name_prefix=prefix,
        provider_metadata={
            "project": "test-project",
            "zone": zone,
            "zones": zones or [zone],
            "worker_count": worker_count,
            "worker_name_start_index": start_index,
            "worker_name_prefix": prefix,
            "machine_type": "n2d-standard-16",
            "boot_disk_size_gb": 100,
            "image": "projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64",
            "service_account_email": "crsbench-worker@test-project.iam.gserviceaccount.com",
            "owner_label": "team-crs",
        },
    )


def _make_stable_evaluator_fleet(
    *,
    zone: str,
    start_index: int = 1,
    evaluator_count: int = 1,
    prefix: str = "evaluator-test-exp",
) -> CloudFleetPlacementRecord:
    return CloudFleetPlacementRecord(
        provider=CloudProvider.GCE,
        role="evaluator",
        project="test-project",
        zone=zone,
        zones=[zone],
        region=zone.rsplit("-", 1)[0],
        owner_label="team-crs",
        count=evaluator_count,
        name_start_index=start_index,
        name_prefix=prefix,
        provider_metadata={
            "project": "test-project",
            "zone": zone,
            "zones": [zone],
            "worker_count": evaluator_count,
            "worker_name_start_index": start_index,
            "worker_name_prefix": prefix,
            "machine_type": "c3-standard-8",
            "boot_disk_size_gb": 50,
            "image": "projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64",
            "service_account_email": "crsbench-evaluator@test-project.iam.gserviceaccount.com",
            "owner_label": "team-crs",
        },
    )


def _make_resolved_cloud_context(launch_state=None):
    from crsbench.cloud.cli._config_reconnect import ResolvedCloudContext

    if launch_state is None:
        fleet = _make_stable_worker_fleet(
            zone="us-central1-a",
            start_index=1,
            worker_count=2,
        ).model_copy(
            update={
                "region": "us-central1",
                "zones": ["us-central1-a"],
                "provider_metadata": {
                    **_make_stable_worker_fleet(
                        zone="us-central1-a",
                        start_index=1,
                        worker_count=2,
                    ).provider_metadata,
                    "zone": "us-central1-a",
                    "zones": ["us-central1-a"],
                },
            }
        )
        return ResolvedCloudContext(
            experiment_name="test-exp",
            worker_fleet_configs=[fleet],
            launch_state=None,
            experiment_filestore=Path("/tmp/filestore"),
            remote_experiment_root=Path("/tmp/remote-root"),
            redis_host="localhost",
            redis_password=None,
        )

    return ResolvedCloudContext(
        experiment_name=launch_state.effective_remote_experiment_name(),
        worker_fleet_configs=launch_state.resolved_worker_fleets(),
        launch_state=launch_state,
        experiment_filestore=Path(launch_state.experiment_filestore),
        remote_experiment_root=Path(launch_state.remote_experiment_root),
        redis_host=launch_state.redis_host,
        redis_password=launch_state.redis_password,
    )


def _make_collect_context(
    *,
    experiment_filestore: Path,
    remote_experiment_root: Path | None = None,
    launch_state=None,
):
    """Build a resolved cloud context for collect tests with explicit paths."""
    context = _make_resolved_cloud_context(launch_state)
    return context.__class__(
        experiment_name=context.experiment_name,
        worker_fleet_configs=context.worker_fleet_configs,
        launch_state=context.launch_state,
        experiment_filestore=experiment_filestore,
        remote_experiment_root=remote_experiment_root
        if remote_experiment_root is not None
        else context.remote_experiment_root,
        redis_host=context.redis_host,
        redis_password=context.redis_password,
        launch_plan=context.launch_plan,
        evaluator_fleet_configs=context.evaluator_fleet_configs,
    )


def _write_collect_marker_metadata(
    destination: Path,
    *,
    last_collect_time: str,
    experiment_start_time: str,
) -> Path:
    """Create explicit marker metadata for overwrite-preflight warning tests."""
    destination.mkdir(parents=True, exist_ok=True)
    marker_path = destination / ".crsbench-collect.json"
    marker_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "experiment_name": destination.name,
                "local_destination": str(destination),
                "last_collect_time": last_collect_time,
                "experiment_start_time": experiment_start_time,
                "experiment_start_time_source": "earliest_trial_timestamp_start",
            }
        )
    )
    return marker_path


@patch("crsbench.cloud.cli._config_reconnect.provider_adapter_for_launch_plan")
@patch("crsbench.cloud.cli._config_reconnect.build_cloud_launch_plan")
@patch("crsbench.cloud.cli._config_reconnect.load_experiment_config")
def test_resolve_cloud_context_resolves_adapter_from_launch_plan(
    mock_load_config,
    mock_build_launch_plan,
    mock_provider_adapter_for_launch_plan,
):
    config = _make_provider_neutral_experiment_config()
    mock_load_config.return_value = config
    launch_plan = MagicMock()
    mock_build_launch_plan.return_value = launch_plan
    adapter = MagicMock()
    adapter.build_worker_fleets.return_value = [MagicMock()]
    adapter.build_evaluator_fleets.return_value = []
    adapter.to_cloud_fleet_placement_record.return_value = CloudFleetPlacementRecord(
        provider=CloudProvider.GCE,
        role="worker",
        project="test-project",
        zone="us-central1-a",
        zones=["us-central1-a"],
        region="us-central1",
        count=1,
        name_prefix="crsbench-test-exp-work",
        name_start_index=1,
        ssh_via_iap=True,
    )
    mock_provider_adapter_for_launch_plan.return_value = adapter

    from crsbench.cloud.cli._config_reconnect import resolve_cloud_context

    resolve_cloud_context("/tmp/config.yaml", "test-exp")

    mock_provider_adapter_for_launch_plan.assert_called_once_with(launch_plan)


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
                "remote": {
                    "experiment_root": "/tmp/remote-root",
                },
                "defaults": {
                    "readiness_timeout_sec": 1200,
                    "crsbench_install_spec": "git+ssh://git@github.com/sslab-gatech/CRSBench.git",
                    "crsbench_git_ref": "main",
                },
                "providers": {
                    "gce": {
                        "project": "test-project",
                        "profile_defaults": {
                            "machine_type": "n2d-standard-16",
                            "boot_disk_size_gb": 50,
                            "image": "projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64",
                            "service_account_email": "crsbench@test-project.iam.gserviceaccount.com",
                            "owner_label": "team-crs",
                        },
                        "instance_profiles": {
                            "gce-orchestrator-n2d": {
                                "service_account_email": "crsbench-orchestrator@test-project.iam.gserviceaccount.com",
                            },
                            "gce-worker-n2d": {
                                "service_account_email": "crsbench-worker@test-project.iam.gserviceaccount.com",
                            },
                        },
                    }
                },
                "orchestrator": {
                    "zone": "us-east5-b",
                    "instance_profile": "gce-orchestrator-n2d",
                },
                "workers": {
                    "defaults": {
                        "instance_profile": "gce-worker-n2d",
                        "count": 150,
                    },
                    "placements": [
                        {
                            "zone": "us-east5-b",
                        },
                        {
                            "zone": "us-east5-c",
                            "count": 100,
                        },
                    ],
                },
            },
            "crs_compose": {"test-crs": {"num_cores": 1}},
        }
    )


def _make_provider_neutral_experiment_config_with_evaluators() -> ExperimentConfig:
    config = _make_provider_neutral_experiment_config().model_dump()
    config["cloud"]["providers"]["gce"]["instance_profiles"]["gce-evaluator-c3"] = {
        "machine_type": "c3-standard-8",
        "service_account_email": "crsbench-evaluator@test-project.iam.gserviceaccount.com",
    }
    config["cloud"]["evaluators"] = {
        "defaults": {
            "instance_profile": "gce-evaluator-c3",
            "count": 1,
        },
        "placements": [
            {
                "zone": "us-east5-b",
            },
            {
                "zone": "us-east1-b",
                "count": 2,
            },
        ],
    }
    return ExperimentConfig.model_validate(config)


def _add_secret_refs_to_provider_neutral_config(
    config: ExperimentConfig,
    *,
    deploy_key_ref: str = ".crsbench-keys/crsbench-deploy",
    hf_token_ref: str = "os.environ/HF_TOKEN",
) -> ExperimentConfig:
    config = config.model_copy(deep=True)
    assert config.cloud is not None
    assert config.cloud.defaults is not None
    config.cloud.defaults.github_deploy_key_path = deploy_key_ref
    if config.cloud.env is None:
        config.cloud.env = {}
    config.cloud.env["HF_TOKEN"] = hf_token_ref
    return config


def _with_layered_env_overrides(
    config: ExperimentConfig,
) -> ExperimentConfig:
    config = config.model_copy(deep=True)
    assert config.cloud is not None
    assert config.cloud.providers is not None
    assert config.cloud.providers.gce is not None
    config.cloud.env = {
        "GLOBAL_ONLY": "global-value",
        "SHARED_KEY": "global-value",
        "COMMON_LEGACY": "explicit-common-value",
    }
    config.cloud.providers.gce.profile_defaults.env = {
        "PROFILE_DEFAULT_ONLY": "profile-default-value",
        "SHARED_KEY": "profile-default-value",
    }
    config.cloud.providers.gce.instance_profiles["gce-orchestrator-n2d"].env = {
        "PROFILE_ONLY": "orchestrator-profile-value",
        "SHARED_KEY": "orchestrator-profile-value",
    }
    config.cloud.providers.gce.instance_profiles["gce-worker-n2d"].env = {
        "PROFILE_ONLY": "worker-profile-value",
        "SHARED_KEY": "worker-profile-value",
    }
    if "gce-evaluator-c3" in config.cloud.providers.gce.instance_profiles:
        config.cloud.providers.gce.instance_profiles["gce-evaluator-c3"].env = {
            "PROFILE_ONLY": "evaluator-profile-value",
            "SHARED_KEY": "evaluator-profile-value",
        }
    assert config.cloud.orchestrator is not None
    config.cloud.orchestrator.env = {
        "TARGET_ONLY": "orchestrator-value",
        "SHARED_KEY": "orchestrator-value",
    }
    config.cloud.workers.defaults.env = {
        "ROLE_ONLY": "worker-role-value",
        "SHARED_KEY": "worker-role-value",
    }
    config.cloud.workers.placements[0].env = {
        "TARGET_ONLY": "worker-east5-value",
        "SHARED_KEY": "worker-east5-value",
    }
    config.cloud.workers.placements[1].env = {
        "TARGET_ONLY": "worker-east5c-value",
        "SHARED_KEY": "worker-east5c-value",
    }
    if config.cloud.evaluators is not None:
        config.cloud.evaluators.defaults.env = {
            "ROLE_ONLY": "evaluator-role-value",
            "SHARED_KEY": "evaluator-role-value",
        }
        config.cloud.evaluators.placements[0].env = {
            "TARGET_ONLY": "evaluator-east5-value",
            "SHARED_KEY": "evaluator-east5-value",
        }
        config.cloud.evaluators.placements[1].env = {
            "TARGET_ONLY": "evaluator-east1-value",
            "SHARED_KEY": "evaluator-east1-value",
        }
    return ExperimentConfig.model_validate(
        config.model_dump(mode="json", exclude_none=True)
    )


def test_build_cloud_launch_plan_resolves_profiles_for_orchestrator_and_workers():
    from crsbench.cloud.models import build_cloud_launch_plan

    config = _make_provider_neutral_experiment_config()

    plan = build_cloud_launch_plan(config)

    assert plan.orchestrator.provider is CloudProvider.GCE
    assert plan.orchestrator.zones == ["us-east5-b"]
    assert plan.orchestrator.fallback is True
    assert plan.orchestrator.instance_profile.name == "gce-orchestrator-n2d"
    assert plan.orchestrator.instance_profile.provider is CloudProvider.GCE
    assert (
        plan.orchestrator.instance_profile.provider_config["project"] == "test-project"
    )
    assert len(plan.worker_placements) == 2
    assert [placement.zones for placement in plan.worker_placements] == [
        ["us-east5-b"],
        ["us-east5-c"],
    ]
    assert all(placement.fallback is True for placement in plan.worker_placements)
    assert [placement.count for placement in plan.worker_placements] == [
        150,
        100,
    ]
    assert all(
        placement.instance_profile.name == "gce-worker-n2d"
        for placement in plan.worker_placements
    )
    assert all(
        placement.instance_profile.provider_config["project"] == "test-project"
        for placement in plan.worker_placements
    )
    assert all(
        placement.provider is CloudProvider.GCE for placement in plan.worker_placements
    )
    assert plan.orchestrator.launch_defaults.readiness_timeout_sec == 1200
    assert (
        plan.orchestrator.launch_defaults.crsbench_install_spec
        == "git+ssh://git@github.com/sslab-gatech/CRSBench.git"
    )
    assert plan.orchestrator.launch_defaults.crsbench_git_ref == "main"
    assert (
        plan.worker_placements[0].launch_defaults == plan.orchestrator.launch_defaults
    )


def test_build_cloud_launch_plan_inherits_provider_default_zones_and_fallback():
    from crsbench.cloud.models import build_cloud_launch_plan

    raw_config = _make_provider_neutral_experiment_config().model_dump(
        mode="json",
        exclude_none=True,
        exclude_defaults=True,
    )
    raw_config["cloud"]["providers"]["gce"]["zones"] = ["us-east5-b", "us-east1-b"]
    raw_config["cloud"]["providers"]["gce"]["fallback"] = False
    raw_config["cloud"]["orchestrator"] = {
        "instance_profile": "gce-orchestrator-n2d",
    }
    raw_config["cloud"]["workers"]["placements"] = [
        {
            "count": 1,
            "instance_profile": "gce-worker-n2d",
        }
    ]
    config = ExperimentConfig.model_validate(raw_config)

    plan = build_cloud_launch_plan(config)

    assert plan.orchestrator.zones == ["us-east5-b", "us-east1-b"]
    assert plan.orchestrator.fallback is False
    assert plan.worker_placements[0].zones == ["us-east5-b", "us-east1-b"]
    assert plan.worker_placements[0].fallback is False


def test_build_cloud_launch_plan_prefers_specific_zone_lists_and_fallback_override():
    from crsbench.cloud.models import build_cloud_launch_plan

    raw_config = _make_provider_neutral_experiment_config().model_dump(
        mode="json",
        exclude_none=True,
        exclude_defaults=True,
    )
    raw_config["cloud"]["providers"]["gce"]["zones"] = ["us-east5-b", "us-east1-b"]
    raw_config["cloud"]["providers"]["gce"]["fallback"] = False
    raw_config["cloud"]["orchestrator"]["zones"] = ["us-central1-a", "us-west1-b"]
    raw_config["cloud"]["orchestrator"].pop("zone", None)
    raw_config["cloud"]["orchestrator"]["fallback"] = True
    raw_config["cloud"]["workers"]["placements"][0] = {
        "zones": ["us-central1-a", "us-west1-b"],
        "fallback": True,
        "instance_profile": "gce-worker-n2d",
        "count": 2,
    }
    config = ExperimentConfig.model_validate(raw_config)

    plan = build_cloud_launch_plan(config)

    assert plan.orchestrator.zones == ["us-central1-a", "us-west1-b"]
    assert plan.orchestrator.fallback is True
    assert plan.worker_placements[0].zones == ["us-central1-a", "us-west1-b"]
    assert plan.worker_placements[0].fallback is True


def test_build_cloud_launch_plan_inherits_provider_default_region_with_zone_allowlist():
    from crsbench.cloud.models import build_cloud_launch_plan

    raw_config = _make_provider_neutral_experiment_config().model_dump(
        mode="json",
        exclude_none=True,
        exclude_defaults=True,
    )
    raw_config["cloud"]["providers"]["gce"]["region"] = "us-east5"
    raw_config["cloud"]["providers"]["gce"]["zones"] = ["us-east5-b", "us-east5-c"]
    raw_config["cloud"]["orchestrator"] = {
        "instance_profile": "gce-orchestrator-n2d",
    }
    raw_config["cloud"]["workers"]["placements"] = [
        {
            "count": 1,
            "instance_profile": "gce-worker-n2d",
        }
    ]
    config = ExperimentConfig.model_validate(raw_config)

    plan = build_cloud_launch_plan(config)

    assert plan.orchestrator.region == "us-east5"
    assert plan.orchestrator.zones == ["us-east5-b", "us-east5-c"]
    assert plan.worker_placements[0].region == "us-east5"
    assert plan.worker_placements[0].zones == ["us-east5-b", "us-east5-c"]


def test_build_cloud_launch_plan_prefers_specific_region_and_zone_allowlist_override():
    from crsbench.cloud.models import build_cloud_launch_plan

    raw_config = _make_provider_neutral_experiment_config().model_dump(
        mode="json",
        exclude_none=True,
        exclude_defaults=True,
    )
    raw_config["cloud"]["providers"]["gce"]["region"] = "us-east5"
    raw_config["cloud"]["providers"]["gce"]["zones"] = ["us-east5-b", "us-east5-c"]
    raw_config["cloud"]["orchestrator"]["region"] = "us-central1"
    raw_config["cloud"]["orchestrator"]["zones"] = ["us-central1-a", "us-central1-f"]
    raw_config["cloud"]["orchestrator"].pop("zone", None)
    raw_config["cloud"]["workers"]["placements"][0] = {
        "region": "us-central1",
        "zones": ["us-central1-a", "us-central1-f"],
        "instance_profile": "gce-worker-n2d",
        "count": 2,
    }
    config = ExperimentConfig.model_validate(raw_config)

    plan = build_cloud_launch_plan(config)

    assert plan.orchestrator.region == "us-central1"
    assert plan.orchestrator.zones == ["us-central1-a", "us-central1-f"]
    assert plan.worker_placements[0].region == "us-central1"
    assert plan.worker_placements[0].zones == ["us-central1-a", "us-central1-f"]


def test_build_cloud_launch_plan_inherits_ordered_provider_regions_and_fallback():
    from crsbench.cloud.models import build_cloud_launch_plan

    raw_config = _make_provider_neutral_experiment_config().model_dump(
        mode="json",
        exclude_none=True,
        exclude_defaults=True,
    )
    raw_config["cloud"]["providers"]["gce"]["regions"] = ["us-east5", "us-east1"]
    raw_config["cloud"]["providers"]["gce"]["zones"] = ["us-east5-b", "us-east1-b"]
    raw_config["cloud"]["providers"]["gce"]["fallback"] = False
    raw_config["cloud"]["orchestrator"] = {
        "instance_profile": "gce-orchestrator-n2d",
    }
    raw_config["cloud"]["workers"]["placements"] = [
        {
            "count": 1,
            "instance_profile": "gce-worker-n2d",
        }
    ]
    config = ExperimentConfig.model_validate(raw_config)

    plan = build_cloud_launch_plan(config)

    assert plan.orchestrator.region == "us-east5"
    assert plan.orchestrator.regions == ["us-east5", "us-east1"]
    assert plan.orchestrator.zones == ["us-east5-b", "us-east1-b"]
    assert plan.orchestrator.fallback is False
    assert plan.worker_placements[0].region == "us-east5"
    assert plan.worker_placements[0].regions == ["us-east5", "us-east1"]
    assert plan.worker_placements[0].zones == ["us-east5-b", "us-east1-b"]
    assert plan.worker_placements[0].fallback is False


def test_build_cloud_launch_plan_merges_provider_launch_defaults_override():
    from crsbench.cloud.models import build_cloud_launch_plan

    config = _make_provider_neutral_experiment_config()
    assert config.cloud is not None
    assert config.cloud.providers is not None
    assert config.cloud.providers.gce is not None
    config.cloud.providers.gce.defaults.readiness_timeout_sec = 1500
    config.cloud.providers.gce.defaults.crsbench_git_ref = "provider-ref"
    config = ExperimentConfig.model_validate(
        config.model_dump(mode="json", exclude_none=True)
    )

    plan = build_cloud_launch_plan(config)

    assert plan.orchestrator.launch_defaults.readiness_timeout_sec == 1500
    assert plan.worker_placements[0].launch_defaults.readiness_timeout_sec == 1500
    assert plan.orchestrator.launch_defaults.crsbench_git_ref == "provider-ref"
    assert (
        plan.orchestrator.launch_defaults.crsbench_install_spec
        == "git+ssh://git@github.com/sslab-gatech/CRSBench.git"
    )


def test_provider_level_ssh_via_iap_flows_to_resolved_gce_configs():
    from crsbench.cloud.gce.provider import GceProviderAdapter
    from crsbench.cloud.models import build_cloud_launch_plan

    raw_config = _make_provider_neutral_experiment_config().model_dump(
        mode="json",
        exclude_none=True,
        exclude_defaults=True,
    )
    raw_config["cloud"]["providers"]["gce"]["ssh_via_iap"] = True
    config = ExperimentConfig.model_validate(raw_config)

    plan = build_cloud_launch_plan(config)
    adapter = GceProviderAdapter()

    assert adapter.build_orchestrator_config(plan).ssh_via_iap is True
    assert all(fleet.ssh_via_iap is True for fleet in adapter.build_worker_fleets(plan))


def test_profile_level_ssh_via_iap_override_beats_provider_default():
    from crsbench.cloud.gce.provider import GceProviderAdapter
    from crsbench.cloud.models import build_cloud_launch_plan

    raw_config = _make_provider_neutral_experiment_config().model_dump(
        mode="json",
        exclude_none=True,
        exclude_defaults=True,
    )
    raw_config["cloud"]["providers"]["gce"]["ssh_via_iap"] = True
    raw_config["cloud"]["providers"]["gce"]["instance_profiles"][
        "gce-orchestrator-n2d"
    ]["ssh_via_iap"] = False
    raw_config["cloud"]["providers"]["gce"]["instance_profiles"]["gce-worker-n2d"][
        "ssh_via_iap"
    ] = False
    config = ExperimentConfig.model_validate(raw_config)

    plan = build_cloud_launch_plan(config)
    adapter = GceProviderAdapter()

    assert adapter.build_orchestrator_config(plan).ssh_via_iap is False
    assert all(
        fleet.ssh_via_iap is False for fleet in adapter.build_worker_fleets(plan)
    )


def test_provider_level_assign_external_ip_flows_to_resolved_gce_configs():
    from crsbench.cloud.gce.provider import GceProviderAdapter
    from crsbench.cloud.models import build_cloud_launch_plan

    raw_config = _make_provider_neutral_experiment_config().model_dump(
        mode="json",
        exclude_none=True,
        exclude_defaults=True,
    )
    raw_config["cloud"]["providers"]["gce"]["assign_external_ip"] = False
    config = ExperimentConfig.model_validate(raw_config)

    plan = build_cloud_launch_plan(config)
    adapter = GceProviderAdapter()

    assert adapter.build_orchestrator_config(plan).assign_external_ip is False
    assert all(
        fleet.assign_external_ip is False for fleet in adapter.build_worker_fleets(plan)
    )


def test_profile_level_assign_external_ip_override_beats_provider_default():
    from crsbench.cloud.gce.provider import GceProviderAdapter
    from crsbench.cloud.models import build_cloud_launch_plan

    raw_config = _make_provider_neutral_experiment_config().model_dump(
        mode="json",
        exclude_none=True,
        exclude_defaults=True,
    )
    raw_config["cloud"]["providers"]["gce"]["assign_external_ip"] = False
    raw_config["cloud"]["providers"]["gce"]["instance_profiles"][
        "gce-orchestrator-n2d"
    ]["assign_external_ip"] = True
    raw_config["cloud"]["providers"]["gce"]["instance_profiles"]["gce-worker-n2d"][
        "assign_external_ip"
    ] = True
    config = ExperimentConfig.model_validate(raw_config)

    plan = build_cloud_launch_plan(config)
    adapter = GceProviderAdapter()

    assert adapter.build_orchestrator_config(plan).assign_external_ip is True
    assert all(
        fleet.assign_external_ip is True for fleet in adapter.build_worker_fleets(plan)
    )


def test_build_cloud_launch_plan_merges_layered_env_for_targets():
    from crsbench.cloud.models import build_cloud_launch_plan

    config = _with_layered_env_overrides(
        _make_provider_neutral_experiment_config_with_evaluators()
    )

    plan = build_cloud_launch_plan(config)

    assert plan.orchestrator.env == {
        "COMMON_LEGACY": "explicit-common-value",
        "GLOBAL_ONLY": "global-value",
        "PROFILE_DEFAULT_ONLY": "profile-default-value",
        "PROFILE_ONLY": "orchestrator-profile-value",
        "SHARED_KEY": "orchestrator-value",
        "TARGET_ONLY": "orchestrator-value",
    }
    assert plan.worker_placements[0].env == {
        "COMMON_LEGACY": "explicit-common-value",
        "GLOBAL_ONLY": "global-value",
        "PROFILE_DEFAULT_ONLY": "profile-default-value",
        "PROFILE_ONLY": "worker-profile-value",
        "ROLE_ONLY": "worker-role-value",
        "SHARED_KEY": "worker-east5-value",
        "TARGET_ONLY": "worker-east5-value",
    }
    assert plan.worker_placements[1].env == {
        "COMMON_LEGACY": "explicit-common-value",
        "GLOBAL_ONLY": "global-value",
        "PROFILE_DEFAULT_ONLY": "profile-default-value",
        "PROFILE_ONLY": "worker-profile-value",
        "ROLE_ONLY": "worker-role-value",
        "SHARED_KEY": "worker-east5c-value",
        "TARGET_ONLY": "worker-east5c-value",
    }
    assert plan.evaluator_placements[0].env == {
        "COMMON_LEGACY": "explicit-common-value",
        "GLOBAL_ONLY": "global-value",
        "PROFILE_DEFAULT_ONLY": "profile-default-value",
        "PROFILE_ONLY": "evaluator-profile-value",
        "ROLE_ONLY": "evaluator-role-value",
        "SHARED_KEY": "evaluator-east5-value",
        "TARGET_ONLY": "evaluator-east5-value",
    }


def test_build_cloud_launch_plan_resolves_profiles_for_evaluators():
    from crsbench.cloud.models import build_cloud_launch_plan

    config = _make_provider_neutral_experiment_config_with_evaluators()

    plan = build_cloud_launch_plan(config)

    assert len(plan.evaluator_placements) == 2
    assert [placement.zones for placement in plan.evaluator_placements] == [
        ["us-east5-b"],
        ["us-east1-b"],
    ]
    assert all(placement.fallback is True for placement in plan.evaluator_placements)
    assert [placement.count for placement in plan.evaluator_placements] == [
        1,
        2,
    ]
    assert all(
        placement.instance_profile.name == "gce-evaluator-c3"
        for placement in plan.evaluator_placements
    )


def test_prepare_gce_launch_inputs_resolves_provider_neutral_secret_refs(
    tmp_path: Path,
) -> None:
    from crsbench.cloud.gce.launch_preflight import prepare_gce_launch_inputs
    from crsbench.cloud.models import build_cloud_launch_plan

    key_dir = tmp_path / ".crsbench-keys"
    key_dir.mkdir()
    key_path = key_dir / "crsbench-deploy"
    key_path.write_text("PRIVATE KEY", encoding="utf-8")

    config = _add_secret_refs_to_provider_neutral_config(
        _make_provider_neutral_experiment_config()
    )
    launch_plan = build_cloud_launch_plan(config)

    preflight = prepare_gce_launch_inputs(
        plan=launch_plan,
        cwd=tmp_path,
        env={"HF_TOKEN": "hf_secret_value"},
    )

    assert launch_plan.orchestrator.env["HF_TOKEN"] == "os.environ/HF_TOKEN"
    assert (
        launch_plan.worker_placements[0].launch_defaults.github_deploy_key_path
        == ".crsbench-keys/crsbench-deploy"
    )
    assert preflight.orchestrator_env["HF_TOKEN"] == "hf_secret_value"
    assert preflight.worker_placement_envs[0]["HF_TOKEN"] == "hf_secret_value"
    assert preflight.resolved_plan.worker_placements[
        0
    ].launch_defaults.github_deploy_key_path == str(key_path)
    assert preflight.redacted_worker_fleets[0].github_deploy_key_path is None


def test_prepare_gce_launch_inputs_resolves_layered_env_per_placement(
    tmp_path: Path,
) -> None:
    from crsbench.cloud.gce.launch_preflight import prepare_gce_launch_inputs
    from crsbench.cloud.models import build_cloud_launch_plan

    secret_dir = tmp_path / ".secrets"
    secret_dir.mkdir()
    worker_secret = secret_dir / "worker-token"
    worker_secret.write_text("worker-file-value", encoding="utf-8")

    config = _with_layered_env_overrides(
        _make_provider_neutral_experiment_config_with_evaluators()
    )
    config.cloud.env["COMMON_REF"] = "os.environ/COMMON_REF"
    config.cloud.orchestrator.env["ORCH_REF"] = "os.environ/ORCH_REF"
    config.cloud.workers.defaults.env["WORKER_REF"] = "os.environ/WORKER_REF"
    config.cloud.evaluators.defaults.env["EVALUATOR_REF"] = "os.environ/EVALUATOR_REF"
    config.cloud.providers.gce.instance_profiles["gce-worker-n2d"].env[
        "PROFILE_FILE"
    ] = "file:.secrets/worker-token"
    config.cloud.workers.placements[0].env["TARGET_REF"] = "os.environ/WORKER_ZERO"
    config.cloud.workers.placements[1].env["TARGET_REF"] = "os.environ/WORKER_ONE"
    config.cloud.evaluators.defaults.env["ROLE_REF"] = "os.environ/EVAL_ROLE"
    config.cloud.orchestrator.env["TARGET_REF"] = "os.environ/ORCH_TARGET"
    config = ExperimentConfig.model_validate(
        config.model_dump(mode="json", exclude_none=True)
    )

    launch_plan = build_cloud_launch_plan(config)

    preflight = prepare_gce_launch_inputs(
        plan=launch_plan,
        cwd=tmp_path,
        env={
            "COMMON_REF": "common-ref-value",
            "ORCH_REF": "orchestrator-ref-value",
            "WORKER_REF": "worker-ref-value",
            "EVALUATOR_REF": "evaluator-ref-value",
            "ORCH_TARGET": "orchestrator-target-ref",
            "WORKER_ZERO": "worker-zero-ref",
            "WORKER_ONE": "worker-one-ref",
            "EVAL_ROLE": "evaluator-role-ref",
        },
    )

    assert preflight.orchestrator_env == {
        "COMMON_LEGACY": "explicit-common-value",
        "COMMON_REF": "common-ref-value",
        "GLOBAL_ONLY": "global-value",
        "ORCH_REF": "orchestrator-ref-value",
        "PROFILE_DEFAULT_ONLY": "profile-default-value",
        "PROFILE_ONLY": "orchestrator-profile-value",
        "SHARED_KEY": "orchestrator-value",
        "TARGET_ONLY": "orchestrator-value",
        "TARGET_REF": "orchestrator-target-ref",
    }
    assert preflight.worker_placement_envs == [
        {
            "COMMON_LEGACY": "explicit-common-value",
            "COMMON_REF": "common-ref-value",
            "GLOBAL_ONLY": "global-value",
            "PROFILE_DEFAULT_ONLY": "profile-default-value",
            "PROFILE_FILE": "worker-file-value",
            "PROFILE_ONLY": "worker-profile-value",
            "ROLE_ONLY": "worker-role-value",
            "SHARED_KEY": "worker-east5-value",
            "TARGET_ONLY": "worker-east5-value",
            "TARGET_REF": "worker-zero-ref",
            "WORKER_REF": "worker-ref-value",
        },
        {
            "COMMON_LEGACY": "explicit-common-value",
            "COMMON_REF": "common-ref-value",
            "GLOBAL_ONLY": "global-value",
            "PROFILE_DEFAULT_ONLY": "profile-default-value",
            "PROFILE_FILE": "worker-file-value",
            "PROFILE_ONLY": "worker-profile-value",
            "ROLE_ONLY": "worker-role-value",
            "SHARED_KEY": "worker-east5c-value",
            "TARGET_ONLY": "worker-east5c-value",
            "TARGET_REF": "worker-one-ref",
            "WORKER_REF": "worker-ref-value",
        },
    ]
    assert preflight.evaluator_placement_envs == [
        {
            "COMMON_LEGACY": "explicit-common-value",
            "COMMON_REF": "common-ref-value",
            "EVALUATOR_REF": "evaluator-ref-value",
            "GLOBAL_ONLY": "global-value",
            "PROFILE_DEFAULT_ONLY": "profile-default-value",
            "PROFILE_ONLY": "evaluator-profile-value",
            "ROLE_ONLY": "evaluator-role-value",
            "ROLE_REF": "evaluator-role-ref",
            "SHARED_KEY": "evaluator-east5-value",
            "TARGET_ONLY": "evaluator-east5-value",
        },
        {
            "COMMON_LEGACY": "explicit-common-value",
            "COMMON_REF": "common-ref-value",
            "EVALUATOR_REF": "evaluator-ref-value",
            "GLOBAL_ONLY": "global-value",
            "PROFILE_DEFAULT_ONLY": "profile-default-value",
            "PROFILE_ONLY": "evaluator-profile-value",
            "ROLE_ONLY": "evaluator-role-value",
            "ROLE_REF": "evaluator-role-ref",
            "SHARED_KEY": "evaluator-east1-value",
            "TARGET_ONLY": "evaluator-east1-value",
        },
    ]


def test_prepare_gce_launch_inputs_rejects_empty_layered_env_reference_value() -> None:
    from crsbench.cloud.gce.launch_preflight import prepare_gce_launch_inputs
    from crsbench.cloud.models import build_cloud_launch_plan
    from crsbench.cloud.secret_refs import CloudSecretReferenceError

    config = _make_provider_neutral_experiment_config()
    config.cloud.env = {"CRSBENCH_LLM_MASTER_KEY": "os.environ/CRSBENCH_LLM_MASTER_KEY"}
    config = ExperimentConfig.model_validate(
        config.model_dump(mode="json", exclude_none=True)
    )
    launch_plan = build_cloud_launch_plan(config)

    with pytest.raises(
        CloudSecretReferenceError,
        match="CRSBENCH_LLM_MASTER_KEY",
    ):
        prepare_gce_launch_inputs(
            plan=launch_plan,
            env={"CRSBENCH_LLM_MASTER_KEY": ""},
        )


def test_prepare_gce_launch_inputs_rejects_missing_secret_before_provisioning(
    tmp_path: Path,
) -> None:
    from crsbench.cloud.gce.launch_preflight import prepare_gce_launch_inputs
    from crsbench.cloud.models import build_cloud_launch_plan
    from crsbench.cloud.secret_refs import CloudSecretReferenceError

    key_dir = tmp_path / ".crsbench-keys"
    key_dir.mkdir()
    (key_dir / "crsbench-deploy").write_text("PRIVATE KEY", encoding="utf-8")

    config = _add_secret_refs_to_provider_neutral_config(
        _make_provider_neutral_experiment_config()
    )
    launch_plan = build_cloud_launch_plan(config)

    with pytest.raises(CloudSecretReferenceError, match="HF_TOKEN"):
        prepare_gce_launch_inputs(plan=launch_plan, cwd=tmp_path, env={})


def test_prepare_gce_launch_inputs_rejects_non_git_install_spec_for_checkout_mode():
    from crsbench.cloud.gce.launch_preflight import prepare_gce_launch_inputs
    from crsbench.cloud.models import build_cloud_launch_plan

    config = _make_provider_neutral_experiment_config()
    assert config.cloud is not None
    assert config.cloud.defaults is not None
    config.cloud.defaults.crsbench_install_spec = "crsbench==0.1.0"
    launch_plan = build_cloud_launch_plan(config)

    with pytest.raises(ValueError, match="git\\+"):
        prepare_gce_launch_inputs(plan=launch_plan, cwd=Path.cwd(), env={})


def test_run_launch_fails_on_quota_shortage_before_creating_instances(tmp_path: Path):
    from crsbench.cloud.cli._launch import run_launch
    from crsbench.cloud.quota import CloudQuotaValidationError

    config_path = tmp_path / "config.yaml"
    config_path.write_text("experiment: ignored\n", encoding="utf-8")
    args = argparse.Namespace(
        config=str(config_path),
        only_trial_keys_file=None,
        only_unfinished_from=None,
        rerun_failed_trials=False,
    )

    with (
        patch(
            "crsbench.cloud.cli._launch.load_experiment_config",
            return_value=_make_provider_neutral_experiment_config(),
        ),
        patch(
            "crsbench.cloud.cli._launch.provider_adapter_for_launch_plan"
        ) as mock_adapter_cls,
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
        assert context.worker_fleet_configs
        assert redis_conn is not None
        assert filestore == Path("/tmp/filestore")

    @patch("crsbench.cloud.cli._config_reconnect.load_experiment_config")
    def test_reconnect_missing_cloud_exits(self, mock_load):
        """reconnect() raises SystemExit when config has no cloud section."""
        mock_load.return_value = _mock_config(has_cloud=False)

        from crsbench.cloud.cli._config_reconnect import reconnect

        with pytest.raises(SystemExit):
            reconnect("/path/to/config.yaml", "test-exp")

    @patch(
        "crsbench.cloud.cli._config_reconnect.OrchestratorRedisTunnel.from_launch_state"
    )
    @patch("crsbench.cloud.cli._config_reconnect.create_redis_connection")
    @patch("crsbench.cloud.cli._config_reconnect.load_launch_state")
    @patch("crsbench.cloud.cli._config_reconnect.load_experiment_config")
    def test_reconnect_remote_orchestrator_uses_launch_state(
        self, mock_load, mock_state, mock_redis, mock_tunnel_cls
    ):
        """Remote orchestrator launches reconnect through persisted launch state."""
        config = _mock_config(has_cloud=True)
        mock_load.return_value = config
        launch_state = _make_launch_state()
        mock_state.return_value = launch_state
        mock_redis.return_value = _FakeRedis()
        mock_tunnel = MagicMock()
        mock_tunnel.redis_host = "127.0.0.1:16379"
        mock_tunnel_cls.return_value = mock_tunnel

        from crsbench.cloud.cli._config_reconnect import reconnect

        with patch.dict(os.environ, {}, clear=False):
            context, _redis_conn, _readiness, _lifecycle, filestore = reconnect(
                "/path/to/config.yaml", "test-exp"
            )

            mock_tunnel_cls.assert_called_once_with(
                Path("/path/to/config.yaml"),
                launch_state,
            )
            mock_tunnel.start.assert_called_once_with()
            mock_redis.assert_called_once_with("127.0.0.1:16379")
            mock_state.assert_called_once_with(Path("/path/to/config.yaml"), "test-exp")
            assert os.environ["CRSBENCH_REDIS_PASSWORD"] == "shared-secret"
            assert (
                context.worker_fleet_configs
                == mock_state.return_value.worker_fleet_configs
            )
            assert filestore == Path("/tmp/filestore")

    @patch("crsbench.cloud.cli._config_reconnect.wait_for_redis_connection")
    @patch(
        "crsbench.cloud.cli._config_reconnect.OrchestratorRedisTunnel.from_launch_state"
    )
    @patch("crsbench.cloud.cli._config_reconnect.create_redis_connection")
    @patch("crsbench.cloud.cli._config_reconnect.load_launch_state")
    @patch("crsbench.cloud.cli._config_reconnect.load_experiment_config")
    def test_reconnect_waits_for_remote_redis_when_requested(
        self,
        mock_load,
        mock_state,
        mock_redis,
        mock_tunnel_cls,
        mock_wait_for_redis_connection,
    ):
        """Status-style reconnects should wait for remote Redis instead of failing during bootstrap."""
        config = _mock_config(has_cloud=True)
        mock_load.return_value = config
        launch_state = _make_launch_state()
        mock_state.return_value = launch_state
        mock_redis.return_value = _FakeRedis()
        mock_tunnel = MagicMock()
        mock_tunnel.redis_host = "127.0.0.1:16379"
        mock_tunnel_cls.return_value = mock_tunnel

        from crsbench.cloud.cli._config_reconnect import reconnect

        reconnect(
            "/path/to/config.yaml",
            "test-exp",
            wait_for_remote_redis=True,
        )

        mock_wait_for_redis_connection.assert_called_once_with(
            "127.0.0.1:16379",
            redis_password="shared-secret",
            timeout_sec=1200,
        )
        mock_redis.assert_called_once_with("127.0.0.1:16379")

    @patch(
        "crsbench.cloud.cli._config_reconnect.find_launch_state_for_source_experiment"
    )
    @patch("crsbench.cloud.cli._config_reconnect.load_experiment_config")
    def test_resolve_effective_experiment_name_prefers_remote_reeval_state(
        self,
        mock_load,
        mock_find_launch_state,
        tmp_path,
    ):
        """Commands without an explicit experiment should target the live remote re-eval namespace."""
        mock_load.return_value = _make_provider_neutral_experiment_config()
        mock_find_launch_state.return_value = _make_reeval_launch_state()
        config_path = tmp_path / "config.yaml"
        config_path.write_text("experiment: test-exp\n", encoding="utf-8")

        from crsbench.cloud.cli._config_reconnect import (
            resolve_effective_experiment_name,
        )

        resolved = resolve_effective_experiment_name(str(config_path), None)

        assert resolved == "test-exp-reeval-20260424-010203"
        mock_find_launch_state.assert_called_once_with(
            config_path,
            "test-exp",
        )

    @patch(
        "crsbench.cloud.cli._config_reconnect.find_launch_state_for_source_experiment",
        side_effect=ValueError("multiple matches"),
    )
    @patch("crsbench.cloud.cli._config_reconnect.load_experiment_config")
    def test_resolve_effective_experiment_name_reports_multiple_reeval_matches(
        self,
        mock_load,
        mock_find_launch_state,
        tmp_path,
    ):
        del mock_find_launch_state
        mock_load.return_value = _make_provider_neutral_experiment_config()
        config_path = tmp_path / "config.yaml"
        config_path.write_text("experiment: test-exp\n", encoding="utf-8")

        from crsbench.cloud.cli._config_reconnect import (
            resolve_effective_experiment_name,
        )

        with pytest.raises(SystemExit, match="multiple matches"):
            resolve_effective_experiment_name(str(config_path), None)

    @patch("crsbench.cloud.cli._config_reconnect.load_launch_state")
    @patch("crsbench.cloud.cli._config_reconnect.load_experiment_config")
    def test_resolve_cloud_context_allows_workerless_reeval_launch_state(
        self,
        mock_load,
        mock_state,
    ):
        """Cloud re-eval reconnect must accept launch state without worker fleets."""
        mock_load.return_value = _make_provider_neutral_experiment_config()
        mock_state.return_value = _make_reeval_launch_state()

        from crsbench.cloud.cli._config_reconnect import resolve_cloud_context

        context = resolve_cloud_context(
            "/tmp/config.yaml", "test-exp-reeval-20260424-010203"
        )

        assert context.launch_state is not None
        assert context.launch_state.launch_mode == "reeval"
        assert context.experiment_name == "test-exp-reeval-20260424-010203"
        assert context.worker_fleet_configs == []
        assert context.remote_experiment_root == Path(
            "/tmp/remote-root/.crsbench-cloud/reeval/test-exp-reeval-20260424-010203/workspace"
        )

    @patch(
        "crsbench.cloud.cli._config_reconnect.find_launch_state_for_source_experiment"
    )
    @patch("crsbench.cloud.cli._config_reconnect.load_launch_state", return_value=None)
    @patch("crsbench.cloud.cli._config_reconnect.load_experiment_config")
    def test_resolve_cloud_context_aliases_source_experiment_to_remote_reeval_state(
        self,
        mock_load,
        mock_load_state,
        mock_find_launch_state,
        tmp_path,
    ):
        del mock_load_state
        mock_load.return_value = _make_provider_neutral_experiment_config()
        mock_find_launch_state.return_value = _make_reeval_launch_state()
        config_path = tmp_path / "config.yaml"
        config_path.write_text("experiment: test-exp\n", encoding="utf-8")

        from crsbench.cloud.cli._config_reconnect import resolve_cloud_context

        context = resolve_cloud_context(str(config_path), "test-exp")

        assert context.experiment_name == "test-exp-reeval-20260424-010203"
        assert context.launch_state is not None
        mock_find_launch_state.assert_called_once_with(
            config_path,
            "test-exp",
        )

    @patch(
        "crsbench.cloud.cli._config_reconnect.find_launch_state_for_source_experiment"
    )
    @patch("crsbench.cloud.cli._config_reconnect.load_launch_state", return_value=None)
    @patch("crsbench.cloud.cli._config_reconnect.load_experiment_config")
    def test_resolve_cloud_context_does_not_alias_unrelated_experiment_names(
        self,
        mock_load,
        mock_load_state,
        mock_find_launch_state,
    ):
        del mock_load_state
        mock_load.return_value = _make_provider_neutral_experiment_config()

        from crsbench.cloud.cli._config_reconnect import resolve_cloud_context

        context = resolve_cloud_context("/tmp/config.yaml", "other-exp")

        assert context.experiment_name == "other-exp"
        mock_find_launch_state.assert_not_called()

    @patch("crsbench.cloud.cli._config_reconnect.load_launch_state")
    @patch("crsbench.cloud.cli._config_reconnect.load_experiment_config")
    def test_resolve_cloud_context_does_not_fall_back_to_legacy_filestore_state(
        self, mock_load, mock_state
    ):
        """Reconnect should only consult the config-adjacent launch-state path."""
        mock_load.return_value = _make_provider_neutral_experiment_config()
        mock_state.return_value = None

        from crsbench.cloud.cli._config_reconnect import resolve_cloud_context

        context = resolve_cloud_context("/path/to/config.yaml", "test-exp")

        mock_state.assert_called_once_with(Path("/path/to/config.yaml"), "test-exp")
        assert [fleet.zone for fleet in context.worker_fleet_configs] == [
            "us-east5-b",
            "us-east5-c",
        ]
        assert context.launch_state is None
        assert context.redis_host == "localhost:6379"

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
    @patch("crsbench.cloud.cli._config_reconnect.warn_for_persisted_storage_roots")
    @patch("crsbench.cloud.cli._config_reconnect.load_experiment_config")
    def test_resolve_cloud_context_uses_config_for_provider_neutral_local_runs(
        self, mock_load, mock_warn, mock_state
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
        assert context.remote_experiment_root == Path("/tmp/remote-root")
        mock_warn.assert_called_once_with(
            experiment_filestore=Path("/tmp/filestore"),
            report_filestore=None,
            copy_results_after_trial=False,
            results_filestore=None,
            remote_experiment_root=Path("/tmp/remote-root"),
        )

    @patch("crsbench.cloud.cli._config_reconnect.load_launch_state")
    @patch("crsbench.cloud.cli._config_reconnect.warn_for_persisted_storage_roots")
    @patch("crsbench.cloud.cli._config_reconnect.load_experiment_config")
    def test_resolve_cloud_context_warns_for_remote_root_fallback(
        self, mock_load, mock_warn, mock_state
    ):
        """Unset remote roots should warn on the effective filestore fallback."""
        config = _make_provider_neutral_experiment_config()
        config.cloud.remote.experiment_root = None
        mock_load.return_value = config
        mock_state.return_value = None

        from crsbench.cloud.cli._config_reconnect import resolve_cloud_context

        context = resolve_cloud_context("/tmp/config.yaml", "test-exp")

        assert context.remote_experiment_root == Path("/tmp/filestore")
        mock_warn.assert_called_once_with(
            experiment_filestore=Path("/tmp/filestore"),
            report_filestore=None,
            copy_results_after_trial=False,
            results_filestore=None,
            remote_experiment_root=Path("/tmp/filestore"),
        )

    @patch("crsbench.cloud.cli._config_reconnect.load_experiment_config")
    def test_resolve_effective_experiment_name_uses_config_when_omitted(
        self, mock_load
    ):
        mock_load.return_value = _make_provider_neutral_experiment_config()

        from crsbench.cloud.cli._config_reconnect import (
            resolve_effective_experiment_name,
        )

        experiment_name = resolve_effective_experiment_name("/tmp/config.yaml", None)

        assert experiment_name == "test-exp"
        mock_load.assert_called_once_with(Path("/tmp/config.yaml"))

    def test_resolve_remote_experiment_dir_defaults_to_experiment_filestore_for_run_mode(
        self,
    ):
        from crsbench.cloud.cli._config_reconnect import resolve_remote_experiment_dir

        remote_dir = resolve_remote_experiment_dir(
            Path("/tmp/remote-root/scoped-filestore"),
            Path("/tmp/remote-root"),
            "test-exp",
            None,
        )

        assert remote_dir == "/tmp/remote-root/scoped-filestore/test-exp"

    def test_resolve_remote_experiment_dir_uses_workspace_root_for_reeval(self):
        from crsbench.cloud.cli._config_reconnect import resolve_remote_experiment_dir

        launch_state = _make_reeval_launch_state()
        remote_dir = resolve_remote_experiment_dir(
            Path("/tmp/filestore"),
            Path(launch_state.remote_experiment_root),
            launch_state.effective_remote_experiment_name(),
            None,
            launch_state=launch_state,
        )

        assert (
            remote_dir
            == f"{launch_state.remote_experiment_root}/{launch_state.remote_experiment_name}"
        )

    @patch("crsbench.cloud.cli._config_reconnect.load_launch_state")
    @patch("crsbench.cloud.cli._config_reconnect.warn_for_persisted_storage_roots")
    @patch("crsbench.cloud.cli._config_reconnect.load_experiment_config")
    def test_resolve_cloud_context_falls_back_remote_root_to_experiment_filestore(
        self, mock_load, mock_warn, mock_state
    ):
        config = _make_provider_neutral_experiment_config().model_copy(deep=True)
        assert config.cloud is not None
        assert config.cloud.remote is not None
        config.cloud.remote.experiment_root = None
        mock_load.return_value = config
        mock_state.side_effect = [None, None]

        from crsbench.cloud.cli._config_reconnect import resolve_cloud_context

        context = resolve_cloud_context("/tmp/config.yaml", "test-exp")

        assert context.remote_experiment_root == Path("/tmp/filestore")
        mock_warn.assert_called_once_with(
            experiment_filestore=Path("/tmp/filestore"),
            report_filestore=None,
            copy_results_after_trial=False,
            results_filestore=None,
            remote_experiment_root=Path("/tmp/filestore"),
        )

    @patch("crsbench.cloud.cli._config_reconnect.warn_for_persisted_storage_roots")
    @patch("crsbench.cloud.cli._config_reconnect.save_launch_state")
    @patch("crsbench.cloud.cli._config_reconnect.load_launch_state")
    @patch("crsbench.cloud.cli._config_reconnect.load_experiment_config")
    def test_resolve_cloud_context_migrates_missing_remote_experiment_root(
        self,
        mock_load,
        mock_state,
        mock_save_state,
        mock_warn,
    ):
        mock_load.return_value = _make_provider_neutral_experiment_config()
        launch_state = _make_provider_neutral_launch_state().model_copy(
            update={"remote_experiment_root": None}
        )
        mock_state.return_value = launch_state

        from crsbench.cloud.cli._config_reconnect import resolve_cloud_context

        context = resolve_cloud_context("/tmp/config.yaml", "test-exp")

        assert context.remote_experiment_root == Path("/tmp/remote-root")
        mock_save_state.assert_called_once()
        mock_warn.assert_called_once_with(
            experiment_filestore=Path("/tmp/filestore"),
            report_filestore=None,
            copy_results_after_trial=False,
            results_filestore=None,
            remote_experiment_root=Path("/tmp/remote-root"),
        )


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

    def test_parse_status_with_global_config(self):
        parser = self._build_parser()
        args = parser.parse_args(["cloud", "--config", "c.yaml", "status", "my-exp"])
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

    def test_parse_teardown_timestamp(self):
        parser = self._build_parser()
        args = parser.parse_args(
            [
                "cloud",
                "teardown",
                "my-exp",
                "--config",
                "c.yaml",
                "--timestamp",
            ]
        )
        assert args.cloud_command == "teardown"
        assert args.timestamp is True

    def test_parse_teardown_skip_collect(self):
        parser = self._build_parser()
        args = parser.parse_args(
            [
                "cloud",
                "teardown",
                "my-exp",
                "--config",
                "c.yaml",
                "--skip-collect",
            ]
        )
        assert args.cloud_command == "teardown"
        assert args.skip_collect is True

    def test_parse_teardown_rejects_timestamp_with_skip_collect(self):
        parser = self._build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(
                [
                    "cloud",
                    "teardown",
                    "my-exp",
                    "--config",
                    "c.yaml",
                    "--timestamp",
                    "--skip-collect",
                ]
            )

    def test_parse_teardown_allows_inferred_experiment_and_remote_dir(self):
        parser = self._build_parser()
        args = parser.parse_args(
            [
                "cloud",
                "teardown",
                "--config",
                "c.yaml",
                "--force",
            ]
        )
        assert args.cloud_command == "teardown"
        assert args.experiment is None
        assert args.remote_dir is None
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

    def test_parse_collect_force(self):
        parser = self._build_parser()
        args = parser.parse_args(
            [
                "cloud",
                "collect",
                "my-exp",
                "--config",
                "c.yaml",
                "--force",
            ]
        )
        assert args.cloud_command == "collect"
        assert args.force is True

    def test_parse_collect_timestamp(self):
        parser = self._build_parser()
        args = parser.parse_args(
            [
                "cloud",
                "collect",
                "my-exp",
                "--config",
                "c.yaml",
                "--timestamp",
            ]
        )
        assert args.cloud_command == "collect"
        assert args.timestamp is True

    def test_parse_collect_allows_inferred_experiment_and_remote_dir(self):
        parser = self._build_parser()
        args = parser.parse_args(
            [
                "cloud",
                "collect",
                "--config",
                "c.yaml",
            ]
        )
        assert args.cloud_command == "collect"
        assert args.experiment is None
        assert args.remote_dir is None

    def test_parse_launch(self):
        parser = self._build_parser()
        args = parser.parse_args(["cloud", "launch", "--config", "c.yaml"])
        assert args.command == "cloud"
        assert args.cloud_command == "launch"
        assert args.config == "c.yaml"
        assert args.only_trial_keys_file is None
        assert args.only_unfinished_from is None
        assert args.rerun_failed_trials is False
        assert args.best_effort_workers is False

    def test_parse_launch_with_best_effort_workers(self):
        parser = self._build_parser()
        args = parser.parse_args(
            ["cloud", "launch", "--config", "c.yaml", "--best-effort-workers"]
        )
        assert args.command == "cloud"
        assert args.cloud_command == "launch"
        assert args.config == "c.yaml"
        assert args.best_effort_workers is True

    def test_parse_launch_with_global_config(self):
        parser = self._build_parser()
        args = parser.parse_args(["cloud", "--config", "c.yaml", "launch"])
        assert args.command == "cloud"
        assert args.cloud_command == "launch"
        assert args.config == "c.yaml"

    def test_parse_re_eval(self):
        parser = self._build_parser()
        args = parser.parse_args(
            [
                "cloud",
                "re-eval",
                "--config",
                "c.yaml",
                "--from",
                "/tmp/source-exp",
                "--remote-experiment",
                "source-exp-reeval-20260424",
            ]
        )
        assert args.command == "cloud"
        assert args.cloud_command == "re-eval"
        assert args.config == "c.yaml"
        assert args.from_path == "/tmp/source-exp"
        assert args.remote_experiment == "source-exp-reeval-20260424"

    def test_parse_launch_with_only_trial_keys_file(self):
        parser = self._build_parser()
        args = parser.parse_args(
            [
                "cloud",
                "launch",
                "--config",
                "c.yaml",
                "--only-trial-keys-file",
                "/tmp/only-trial-keys.txt",
            ]
        )
        assert args.command == "cloud"
        assert args.cloud_command == "launch"
        assert args.config == "c.yaml"
        assert args.only_trial_keys_file == "/tmp/only-trial-keys.txt"
        assert args.only_unfinished_from is None
        assert args.rerun_failed_trials is False

    def test_parse_launch_with_unfinished_selector_and_rerun_flag(self):
        parser = self._build_parser()
        args = parser.parse_args(
            [
                "cloud",
                "launch",
                "--config",
                "c.yaml",
                "--only-unfinished-from",
                "/tmp/collected",
                "--rerun-failed-trials",
            ]
        )
        assert args.command == "cloud"
        assert args.cloud_command == "launch"
        assert args.config == "c.yaml"
        assert args.only_trial_keys_file is None
        assert args.only_unfinished_from == "/tmp/collected"
        assert args.rerun_failed_trials is True

    def test_parse_launch_rejects_conflicting_selector_sources(self):
        parser = self._build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(
                [
                    "cloud",
                    "launch",
                    "--config",
                    "c.yaml",
                    "--only-trial-keys-file",
                    "/tmp/only-trial-keys.txt",
                    "--only-unfinished-from",
                    "/tmp/collected",
                ]
            )

    def test_parse_preflight(self):
        parser = self._build_parser()
        args = parser.parse_args(["cloud", "preflight", "--config", "c.yaml"])
        assert args.command == "cloud"
        assert args.cloud_command == "preflight"
        assert args.config == "c.yaml"
        assert args.json_output is False
        assert args.strict is False

    def test_parse_preflight_json_strict(self):
        parser = self._build_parser()
        args = parser.parse_args(
            ["cloud", "preflight", "--config", "c.yaml", "--json", "--strict"]
        )
        assert args.command == "cloud"
        assert args.cloud_command == "preflight"
        assert args.config == "c.yaml"
        assert args.json_output is True
        assert args.strict is True

    def test_parse_add_workers(self):
        parser = self._build_parser()
        args = parser.parse_args(
            [
                "cloud",
                "add-workers",
                "--config",
                "c.yaml",
                "--instance-profile",
                "gce-worker-n2d",
                "--count",
                "2",
                "--regions",
                "us-east5,us-east1",
                "--zones",
                "us-east5-b,us-east1-b",
            ]
        )
        assert args.command == "cloud"
        assert args.cloud_command == "add-workers"
        assert args.config == "c.yaml"
        assert args.instance_profile == "gce-worker-n2d"
        assert args.count == 2
        assert args.regions == "us-east5,us-east1"
        assert args.zones == "us-east5-b,us-east1-b"
        assert args.force is False

    def test_parse_add_workers_allows_runtime_defaults(self):
        parser = self._build_parser()
        args = parser.parse_args(["cloud", "add-workers", "--config", "c.yaml"])

        assert args.command == "cloud"
        assert args.cloud_command == "add-workers"
        assert args.config == "c.yaml"
        assert args.instance_profile is None
        assert args.count is None
        assert args.regions is None
        assert args.zones is None
        assert args.force is False

    def test_parse_add_evaluators(self):
        parser = self._build_parser()
        args = parser.parse_args(
            [
                "cloud",
                "add-evaluators",
                "--config",
                "c.yaml",
                "--instance-profile",
                "gce-evaluator-n2d",
                "--count",
                "1",
                "--zones",
                "us-central1-a",
                "--force",
            ]
        )
        assert args.command == "cloud"
        assert args.cloud_command == "add-evaluators"
        assert args.config == "c.yaml"
        assert args.instance_profile == "gce-evaluator-n2d"
        assert args.count == 1
        assert args.regions is None
        assert args.zones == "us-central1-a"
        assert args.force is True

    def test_parse_add_evaluators_allows_runtime_defaults(self):
        parser = self._build_parser()
        args = parser.parse_args(["cloud", "add-evaluators", "--config", "c.yaml"])

        assert args.command == "cloud"
        assert args.cloud_command == "add-evaluators"
        assert args.config == "c.yaml"
        assert args.instance_profile is None
        assert args.count is None
        assert args.regions is None
        assert args.zones is None
        assert args.force is False

    def test_parse_monitor(self):
        parser = self._build_parser()
        args = parser.parse_args(["cloud", "monitor", "my-exp", "--config", "c.yaml"])
        assert args.command == "cloud"
        assert args.cloud_command == "monitor"
        assert args.experiment == "my-exp"
        assert args.config == "c.yaml"

    def test_parse_monitor_allows_inferred_experiment(self):
        parser = self._build_parser()
        args = parser.parse_args(["cloud", "monitor", "--config", "c.yaml"])
        assert args.command == "cloud"
        assert args.cloud_command == "monitor"
        assert args.experiment is None
        assert args.config == "c.yaml"

    def test_parse_list(self):
        parser = self._build_parser()
        args = parser.parse_args(["cloud", "list", "--config", "c.yaml"])
        assert args.command == "cloud"
        assert args.cloud_command == "list"
        assert args.config == "c.yaml"
        assert args.json_output is False

    def test_parse_list_with_global_config(self):
        parser = self._build_parser()
        args = parser.parse_args(["cloud", "--config", "c.yaml", "list"])
        assert args.command == "cloud"
        assert args.cloud_command == "list"
        assert args.config == "c.yaml"
        assert args.json_output is False

    def test_parse_list_json(self):
        parser = self._build_parser()
        args = parser.parse_args(["cloud", "list", "--config", "c.yaml", "--json"])
        assert args.command == "cloud"
        assert args.cloud_command == "list"
        assert args.json_output is True

    def test_parse_ssh_with_instance(self):
        parser = self._build_parser()
        args = parser.parse_args(["cloud", "ssh", "work-001", "--config", "c.yaml"])
        assert args.command == "cloud"
        assert args.cloud_command == "ssh"
        assert args.instance == "work-001"
        assert args.config == "c.yaml"

    def test_parse_ssh_without_instance(self):
        parser = self._build_parser()
        args = parser.parse_args(["cloud", "ssh", "--config", "c.yaml"])
        assert args.command == "cloud"
        assert args.cloud_command == "ssh"
        assert args.instance is None
        assert args.config == "c.yaml"

    def test_parse_ssh_with_global_config(self):
        parser = self._build_parser()
        args = parser.parse_args(["cloud", "--config", "c.yaml", "ssh", "work-001"])
        assert args.command == "cloud"
        assert args.cloud_command == "ssh"
        assert args.instance == "work-001"
        assert args.config == "c.yaml"

    def test_parse_shell_with_instance(self):
        parser = self._build_parser()
        args = parser.parse_args(["cloud", "shell", "work-001", "--config", "c.yaml"])
        assert args.command == "cloud"
        assert args.cloud_command == "shell"
        assert args.instance == "work-001"
        assert args.config == "c.yaml"

    def test_parse_serial_with_instance_and_port(self):
        parser = self._build_parser()
        args = parser.parse_args(
            ["cloud", "serial", "work-001", "--config", "c.yaml", "--port", "2"]
        )
        assert args.command == "cloud"
        assert args.cloud_command == "serial"
        assert args.instance == "work-001"
        assert args.port == 2
        assert args.config == "c.yaml"

    def test_parse_serial_without_instance(self):
        parser = self._build_parser()
        args = parser.parse_args(["cloud", "serial", "--config", "c.yaml"])
        assert args.command == "cloud"
        assert args.cloud_command == "serial"
        assert args.instance is None
        assert args.port == 1
        assert args.config == "c.yaml"

    def test_parse_exec_with_instance_and_command(self):
        parser = self._build_parser()
        args = parser.parse_args(
            ["cloud", "--config", "c.yaml", "exec", "work-001", "--", "echo", "hi"]
        )
        assert args.command == "cloud"
        assert args.cloud_command == "exec"
        assert args.exec_args == ["work-001", "--", "echo", "hi"]
        assert args.config == "c.yaml"

    def test_parse_exec_without_instance(self):
        parser = self._build_parser()
        args = parser.parse_args(["cloud", "--config", "c.yaml", "exec", "--", "pwd"])
        assert args.command == "cloud"
        assert args.cloud_command == "exec"
        assert args.exec_args == ["--", "pwd"]
        assert args.config == "c.yaml"

    def test_parse_log_with_instance(self):
        parser = self._build_parser()
        args = parser.parse_args(["cloud", "log", "work-001", "--config", "c.yaml"])
        assert args.command == "cloud"
        assert args.cloud_command == "log"
        assert args.instance == "work-001"
        assert args.instances == []
        assert args.role is None
        assert args.all_instances is False
        assert args.merge_by == "arrival"
        assert args.config == "c.yaml"

    def test_parse_log_without_instance(self):
        parser = self._build_parser()
        args = parser.parse_args(["cloud", "log", "--config", "c.yaml"])
        assert args.command == "cloud"
        assert args.cloud_command == "log"
        assert args.instance is None
        assert args.instances == []
        assert args.role is None
        assert args.all_instances is False
        assert args.merge_by == "arrival"
        assert args.config == "c.yaml"

    def test_parse_log_multi_target_flags(self):
        parser = self._build_parser()
        args = parser.parse_args(
            [
                "cloud",
                "log",
                "orch",
                "--config",
                "c.yaml",
                "--instance",
                "work-001",
                "--instance",
                "eval-001",
                "--role",
                "worker",
                "--all",
                "--merge-by",
                "timestamp",
            ]
        )
        assert args.command == "cloud"
        assert args.cloud_command == "log"
        assert args.instance == "orch"
        assert args.instances == ["work-001", "eval-001"]
        assert args.role == "worker"
        assert args.all_instances is True
        assert args.merge_by == "timestamp"
        assert args.config == "c.yaml"

    def test_parse_derive_unfinished_trial_keys(self):
        parser = self._build_parser()
        args = parser.parse_args(
            [
                "cloud",
                "derive-unfinished-trial-keys",
                "--config",
                "c.yaml",
            ]
        )
        assert args.command == "cloud"
        assert args.cloud_command == "derive-unfinished-trial-keys"
        assert args.config == "c.yaml"
        assert args.from_path is None
        assert args.output is None
        assert args.rerun_failed_trials is False

    def test_parse_derive_unfinished_trial_keys_with_overrides(self):
        parser = self._build_parser()
        args = parser.parse_args(
            [
                "cloud",
                "derive-unfinished-trial-keys",
                "--config",
                "c.yaml",
                "--from",
                "/tmp/collected",
                "--output",
                "/tmp/unfinished-keys.txt",
                "--rerun-failed-trials",
            ]
        )
        assert args.command == "cloud"
        assert args.cloud_command == "derive-unfinished-trial-keys"
        assert args.config == "c.yaml"
        assert args.from_path == "/tmp/collected"
        assert args.output == "/tmp/unfinished-keys.txt"
        assert args.rerun_failed_trials is True

    def test_parse_derive_unfinished_trial_keys_with_global_config(self):
        parser = self._build_parser()
        args = parser.parse_args(
            [
                "cloud",
                "--config",
                "c.yaml",
                "derive-unfinished-trial-keys",
            ]
        )
        assert args.command == "cloud"
        assert args.cloud_command == "derive-unfinished-trial-keys"
        assert args.config == "c.yaml"
        assert args.from_path is None
        assert args.output is None
        assert args.rerun_failed_trials is False

    def test_parse_derive_unfinished_trial_keys_requires_config(self):
        parser = self._build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["cloud", "derive-unfinished-trial-keys"])


def _make_launch_args(
    config: str = "/tmp/config.yaml",
    *,
    only_trial_keys_file: str | None = None,
    only_unfinished_from: str | None = None,
    rerun_failed_trials: bool = False,
    best_effort_workers: bool = False,
):
    return argparse.Namespace(
        config=config,
        only_trial_keys_file=only_trial_keys_file,
        only_unfinished_from=only_unfinished_from,
        rerun_failed_trials=rerun_failed_trials,
        best_effort_workers=best_effort_workers,
        cloud_command="launch",
    )


def _make_preflight_args(
    config: str = "/tmp/config.yaml",
    *,
    json_output: bool = False,
    strict: bool = False,
):
    return argparse.Namespace(
        config=config,
        json_output=json_output,
        strict=strict,
        cloud_command="preflight",
    )


def _make_monitor_args(
    experiment: str | None = "test-exp",
    config: str = "/tmp/config.yaml",
):
    return argparse.Namespace(
        experiment=experiment,
        config=config,
        cloud_command="monitor",
    )


def _make_add_capacity_args(
    *,
    cloud_command: str,
    config: str = "/tmp/config.yaml",
    instance_profile: str = "gce-worker-n2d",
    count: int = 2,
    regions: str | None = "us-east5,us-east1",
    zones: str | None = "us-east5-b,us-east1-b",
    force: bool = False,
):
    return argparse.Namespace(
        config=config,
        cloud_command=cloud_command,
        instance_profile=instance_profile,
        count=count,
        regions=regions,
        zones=zones,
        force=force,
    )


def _make_list_args(
    config: str = "/tmp/config.yaml",
    *,
    json_output: bool = False,
):
    return argparse.Namespace(
        config=config,
        json_output=json_output,
        cloud_command="list",
    )


def _make_ssh_args(
    instance: str | None = "work-001",
    config: str = "/tmp/config.yaml",
    cloud_command: str = "ssh",
):
    return argparse.Namespace(
        instance=instance,
        config=config,
        cloud_command=cloud_command,
    )


def _make_exec_args(
    instance: str | None = "work-001",
    config: str = "/tmp/config.yaml",
    exec_command: list[str] | None = None,
):
    return argparse.Namespace(
        instance=instance,
        config=config,
        exec_command=["echo", "hi"] if exec_command is None else exec_command,
        cloud_command="exec",
    )


def _make_serial_args(
    instance: str | None = "work-001",
    config: str = "/tmp/config.yaml",
    *,
    port: int = 1,
):
    return argparse.Namespace(
        instance=instance,
        config=config,
        port=port,
        cloud_command="serial",
    )


def _make_log_args(
    instance: str | None = "work-001",
    config: str = "/tmp/config.yaml",
    *,
    instances: list[str] | None = None,
    role: str | None = None,
    all_instances: bool = False,
    merge_by: str = "arrival",
):
    return argparse.Namespace(
        instance=instance,
        instances=[] if instances is None else instances,
        role=role,
        all_instances=all_instances,
        merge_by=merge_by,
        config=config,
        cloud_command="log",
    )


def _make_derive_args(
    config: str = "/tmp/config.yaml",
    *,
    from_path: str | None = None,
    output: str | None = None,
    rerun_failed_trials: bool = False,
):
    return argparse.Namespace(
        config=config,
        from_path=from_path,
        output=output,
        rerun_failed_trials=rerun_failed_trials,
        cloud_command="derive-unfinished-trial-keys",
    )


@patch("crsbench.cloud.cli._monitor.run_monitor", return_value=0)
def test_run_cloud_dispatches_monitor(mock_run_monitor):
    from crsbench.cloud.cli.cloud_command import run_cloud

    rc = run_cloud(_make_monitor_args())

    assert rc == 0
    mock_run_monitor.assert_called_once()


@patch("crsbench.cloud.cli._list.run_list", return_value=0)
def test_run_cloud_dispatches_list(mock_run_list):
    from crsbench.cloud.cli.cloud_command import run_cloud

    rc = run_cloud(_make_list_args())

    assert rc == 0
    mock_run_list.assert_called_once()


@patch("crsbench.cloud.cli._reeval.run_cloud_reeval", return_value=0)
def test_run_cloud_dispatches_re_eval(mock_run_reeval):
    from crsbench.cloud.cli.cloud_command import run_cloud

    rc = run_cloud(
        argparse.Namespace(
            cloud_command="re-eval",
            config="config.yaml",
            from_path=None,
            remote_experiment=None,
        )
    )

    assert rc == 0
    mock_run_reeval.assert_called_once()


def test_run_cloud_requires_config_for_non_keygen_commands():
    from crsbench.cloud.cli.cloud_command import run_cloud

    rc = run_cloud(argparse.Namespace(cloud_command="launch", config=None))

    assert rc == 2


@patch("crsbench.cloud.cli._ssh.run_ssh", return_value=0)
def test_run_cloud_dispatches_ssh(mock_run_ssh):
    from crsbench.cloud.cli.cloud_command import run_cloud

    rc = run_cloud(_make_ssh_args())

    assert rc == 0
    mock_run_ssh.assert_called_once()


@patch("crsbench.cloud.cli._ssh.run_ssh", return_value=0)
def test_run_cloud_dispatches_shell(mock_run_ssh):
    from crsbench.cloud.cli.cloud_command import run_cloud

    rc = run_cloud(_make_ssh_args(cloud_command="shell"))

    assert rc == 0
    mock_run_ssh.assert_called_once()


@patch("crsbench.cloud.cli._serial.run_serial", return_value=0)
def test_run_cloud_dispatches_serial(mock_run_serial):
    from crsbench.cloud.cli.cloud_command import run_cloud

    rc = run_cloud(_make_serial_args())

    assert rc == 0
    mock_run_serial.assert_called_once()


@patch("crsbench.cloud.cli._exec.run_exec", return_value=0)
def test_run_cloud_dispatches_exec(mock_run_exec):
    from crsbench.cloud.cli.cloud_command import run_cloud

    rc = run_cloud(_make_exec_args())

    assert rc == 0
    mock_run_exec.assert_called_once()


@patch("crsbench.cloud.cli._log.run_log", return_value=0)
def test_run_cloud_dispatches_log(mock_run_log):
    from crsbench.cloud.cli.cloud_command import run_cloud

    rc = run_cloud(_make_log_args())

    assert rc == 0
    mock_run_log.assert_called_once()


@patch(
    "crsbench.cloud.cli._derive_unfinished_trial_keys.run_derive_unfinished_trial_keys",
    return_value=0,
)
def test_run_cloud_dispatches_derive_unfinished_trial_keys(mock_run_derive):
    from crsbench.cloud.cli.cloud_command import run_cloud

    args = _make_derive_args()
    rc = run_cloud(args)

    assert rc == 0
    mock_run_derive.assert_called_once_with(args)


@patch("crsbench.cloud.cli._add_capacity.run_add_capacity", return_value=0)
def test_run_cloud_dispatches_add_workers(mock_run_add_capacity):
    from crsbench.cloud.cli.cloud_command import run_cloud

    rc = run_cloud(_make_add_capacity_args(cloud_command="add-workers"))

    assert rc == 0
    mock_run_add_capacity.assert_called_once()


class TestDeriveUnfinishedTrialKeys:
    def test_derive_uses_config_defaults_for_collected_root_and_output(
        self, tmp_path: Path
    ):
        from crsbench.cloud.cli._derive_unfinished_trial_keys import (
            run_derive_unfinished_trial_keys,
        )

        config_path = tmp_path / "config.yaml"
        config_path.write_text("experiment: ignored\n", encoding="utf-8")
        config = SimpleNamespace(experiment="exp-defaults")
        default_collected = Path("/tmp/filestore/exp-defaults")
        default_output = tmp_path / "exp-defaults-unfinished-trial-keys.txt"
        derived = SimpleNamespace(
            selected_keys=["trial-a", "trial-b"],
            finished_success_keys=["trial-done"],
            finished_fail_keys=["trial-failed"],
        )

        with (
            patch(
                "crsbench.cloud.cli._derive_unfinished_trial_keys.load_experiment_config",
                return_value=config,
            ) as mock_load_config,
            patch(
                "crsbench.cloud.cli._derive_unfinished_trial_keys.default_collected_experiment_path",
                return_value=default_collected,
            ) as mock_default_collected,
            patch(
                "crsbench.cloud.cli._derive_unfinished_trial_keys.default_selector_output_path",
                return_value=default_output,
            ) as mock_default_output,
            patch(
                "crsbench.cloud.cli._derive_unfinished_trial_keys.derive_unfinished_trial_keys_from_config",
                return_value=derived,
            ) as mock_derive,
            patch(
                "crsbench.cloud.cli._derive_unfinished_trial_keys.logger"
            ) as mock_logger,
        ):
            rc = run_derive_unfinished_trial_keys(
                _make_derive_args(config=str(config_path))
            )

        assert rc == 0
        mock_load_config.assert_called_once_with(config_path)
        mock_default_collected.assert_called_once_with(config)
        mock_default_output.assert_called_once_with("exp-defaults")
        mock_derive.assert_called_once_with(
            config,
            collected_root=default_collected,
            rerun_failed_trials=False,
        )
        assert default_output.read_text(encoding="utf-8") == "trial-a\ntrial-b\n"
        assert mock_logger.info.call_args.args[1:] == (2, 1, 1)

    def test_derive_respects_overrides_and_rerun_failed_trials_flag(
        self, tmp_path: Path
    ):
        from crsbench.cloud.cli._derive_unfinished_trial_keys import (
            run_derive_unfinished_trial_keys,
        )

        config_path = tmp_path / "config.yaml"
        config_path.write_text("experiment: ignored\n", encoding="utf-8")
        from_path = tmp_path / "custom-collected"
        from_path.mkdir(parents=True)
        output_path = tmp_path / "custom-output.txt"
        config = SimpleNamespace(experiment="exp-overrides")
        derived = SimpleNamespace(
            selected_keys=["trial-x"],
            finished_success_keys=[],
            finished_fail_keys=["trial-y"],
        )

        with (
            patch(
                "crsbench.cloud.cli._derive_unfinished_trial_keys.load_experiment_config",
                return_value=config,
            ) as mock_load_config,
            patch(
                "crsbench.cloud.cli._derive_unfinished_trial_keys.default_collected_experiment_path"
            ) as mock_default_collected,
            patch(
                "crsbench.cloud.cli._derive_unfinished_trial_keys.default_selector_output_path"
            ) as mock_default_output,
            patch(
                "crsbench.cloud.cli._derive_unfinished_trial_keys.derive_unfinished_trial_keys_from_config",
                return_value=derived,
            ) as mock_derive,
            patch(
                "crsbench.cloud.cli._derive_unfinished_trial_keys.logger"
            ) as mock_logger,
        ):
            rc = run_derive_unfinished_trial_keys(
                _make_derive_args(
                    config=str(config_path),
                    from_path=str(from_path),
                    output=str(output_path),
                    rerun_failed_trials=True,
                )
            )

        assert rc == 0
        mock_load_config.assert_called_once_with(config_path)
        mock_default_collected.assert_not_called()
        mock_default_output.assert_not_called()
        mock_derive.assert_called_once_with(
            config,
            collected_root=from_path,
            rerun_failed_trials=True,
        )
        assert output_path.read_text(encoding="utf-8") == "trial-x\n"
        assert mock_logger.info.call_args.args[1:] == (1, 0, 1)

    def test_derive_fails_when_explicit_from_path_does_not_exist(self, tmp_path: Path):
        from crsbench.cloud.cli._derive_unfinished_trial_keys import (
            run_derive_unfinished_trial_keys,
        )

        config_path = tmp_path / "config.yaml"
        config_path.write_text("experiment: ignored\n", encoding="utf-8")
        missing_path = tmp_path / "missing-collected-root"
        config = SimpleNamespace(experiment="exp-invalid-from")

        with (
            patch(
                "crsbench.cloud.cli._derive_unfinished_trial_keys.load_experiment_config",
                return_value=config,
            ),
            patch(
                "crsbench.cloud.cli._derive_unfinished_trial_keys.derive_unfinished_trial_keys_from_config"
            ) as mock_derive,
            patch(
                "crsbench.cloud.cli._derive_unfinished_trial_keys.logger"
            ) as mock_logger,
        ):
            rc = run_derive_unfinished_trial_keys(
                _make_derive_args(
                    config=str(config_path),
                    from_path=str(missing_path),
                )
            )

        assert rc == 2
        mock_derive.assert_not_called()
        assert mock_logger.error.call_args.args == (
            "Collected root does not exist: {}",
            missing_path,
        )

    def test_derive_fails_when_explicit_from_path_is_not_directory(
        self, tmp_path: Path
    ):
        from crsbench.cloud.cli._derive_unfinished_trial_keys import (
            run_derive_unfinished_trial_keys,
        )

        config_path = tmp_path / "config.yaml"
        config_path.write_text("experiment: ignored\n", encoding="utf-8")
        invalid_path = tmp_path / "not-a-directory.txt"
        invalid_path.write_text("not a directory", encoding="utf-8")
        config = SimpleNamespace(experiment="exp-invalid-from")

        with (
            patch(
                "crsbench.cloud.cli._derive_unfinished_trial_keys.load_experiment_config",
                return_value=config,
            ),
            patch(
                "crsbench.cloud.cli._derive_unfinished_trial_keys.derive_unfinished_trial_keys_from_config"
            ) as mock_derive,
            patch(
                "crsbench.cloud.cli._derive_unfinished_trial_keys.logger"
            ) as mock_logger,
        ):
            rc = run_derive_unfinished_trial_keys(
                _make_derive_args(
                    config=str(config_path),
                    from_path=str(invalid_path),
                )
            )

        assert rc == 2
        mock_derive.assert_not_called()
        assert mock_logger.error.call_args.args == (
            "Collected root is not a directory: {}",
            invalid_path,
        )

    def test_derive_creates_output_parent_directories(self, tmp_path: Path):
        from crsbench.cloud.cli._derive_unfinished_trial_keys import (
            run_derive_unfinished_trial_keys,
        )

        config_path = tmp_path / "config.yaml"
        config_path.write_text("experiment: ignored\n", encoding="utf-8")
        config = SimpleNamespace(experiment="exp-output-parent")
        default_collected = tmp_path / "collected-root"
        default_collected.mkdir(parents=True)
        output_path = tmp_path / "nested" / "selectors" / "unfinished-keys.txt"
        derived = SimpleNamespace(
            selected_keys=["trial-a"],
            finished_success_keys=[],
            finished_fail_keys=[],
        )

        with (
            patch(
                "crsbench.cloud.cli._derive_unfinished_trial_keys.load_experiment_config",
                return_value=config,
            ),
            patch(
                "crsbench.cloud.cli._derive_unfinished_trial_keys.default_collected_experiment_path",
                return_value=default_collected,
            ),
            patch(
                "crsbench.cloud.cli._derive_unfinished_trial_keys.default_selector_output_path",
                return_value=output_path,
            ),
            patch(
                "crsbench.cloud.cli._derive_unfinished_trial_keys.derive_unfinished_trial_keys_from_config",
                return_value=derived,
            ),
        ):
            rc = run_derive_unfinished_trial_keys(
                _make_derive_args(config=str(config_path))
            )

        assert rc == 0
        assert output_path.parent.exists()
        assert output_path.read_text(encoding="utf-8") == "trial-a\n"

    def test_derive_logs_and_returns_2_when_derivation_raises_value_error(
        self, tmp_path: Path
    ):
        from crsbench.cloud.cli._derive_unfinished_trial_keys import (
            run_derive_unfinished_trial_keys,
        )

        config_path = tmp_path / "config.yaml"
        config_path.write_text("experiment: ignored\n", encoding="utf-8")
        config = SimpleNamespace(experiment="exp-derive-error")
        default_collected = tmp_path / "collected-root"
        default_collected.mkdir(parents=True)
        output_path = tmp_path / "unfinished-keys.txt"

        with (
            patch(
                "crsbench.cloud.cli._derive_unfinished_trial_keys.load_experiment_config",
                return_value=config,
            ),
            patch(
                "crsbench.cloud.cli._derive_unfinished_trial_keys.default_collected_experiment_path",
                return_value=default_collected,
            ),
            patch(
                "crsbench.cloud.cli._derive_unfinished_trial_keys.default_selector_output_path",
                return_value=output_path,
            ),
            patch(
                "crsbench.cloud.cli._derive_unfinished_trial_keys.derive_unfinished_trial_keys_from_config",
                side_effect=ValueError("unknown finished trial key"),
            ),
            patch(
                "crsbench.cloud.cli._derive_unfinished_trial_keys.logger"
            ) as mock_logger,
        ):
            rc = run_derive_unfinished_trial_keys(
                _make_derive_args(config=str(config_path))
            )

        assert rc == 2
        assert mock_logger.error.call_args.args[0] == (
            "Failed to derive unfinished trial keys: {}"
        )
        assert "unknown finished trial key" in str(mock_logger.error.call_args.args[1])

    def test_derive_logs_and_returns_2_when_output_write_raises_os_error(
        self, tmp_path: Path
    ):
        from crsbench.cloud.cli._derive_unfinished_trial_keys import (
            run_derive_unfinished_trial_keys,
        )

        config_path = tmp_path / "config.yaml"
        config_path.write_text("experiment: ignored\n", encoding="utf-8")
        config = SimpleNamespace(experiment="exp-write-error")
        default_collected = tmp_path / "collected-root"
        default_collected.mkdir(parents=True)
        output_path = tmp_path / "nested" / "unfinished-keys.txt"
        derived = SimpleNamespace(
            selected_keys=["trial-a"],
            finished_success_keys=[],
            finished_fail_keys=[],
        )

        with (
            patch(
                "crsbench.cloud.cli._derive_unfinished_trial_keys.load_experiment_config",
                return_value=config,
            ),
            patch(
                "crsbench.cloud.cli._derive_unfinished_trial_keys.default_collected_experiment_path",
                return_value=default_collected,
            ),
            patch(
                "crsbench.cloud.cli._derive_unfinished_trial_keys.default_selector_output_path",
                return_value=output_path,
            ),
            patch(
                "crsbench.cloud.cli._derive_unfinished_trial_keys.derive_unfinished_trial_keys_from_config",
                return_value=derived,
            ),
            patch(
                "crsbench.cloud.cli._derive_unfinished_trial_keys.Path.write_text",
                side_effect=OSError("disk full"),
            ),
            patch(
                "crsbench.cloud.cli._derive_unfinished_trial_keys.logger"
            ) as mock_logger,
        ):
            rc = run_derive_unfinished_trial_keys(
                _make_derive_args(config=str(config_path))
            )

        assert rc == 2
        assert mock_logger.error.call_args.args[0] == (
            "Failed to write unfinished trial keys to {}: {}"
        )
        assert mock_logger.error.call_args.args[1] == output_path
        assert "disk full" in str(mock_logger.error.call_args.args[2])


@patch("crsbench.cloud.cli._add_capacity.run_add_capacity", return_value=0)
def test_run_cloud_dispatches_add_evaluators(mock_run_add_capacity):
    from crsbench.cloud.cli.cloud_command import run_cloud

    rc = run_cloud(
        _make_add_capacity_args(
            cloud_command="add-evaluators",
            instance_profile="gce-evaluator-c3",
            count=1,
            regions=None,
            zones="us-central1-a",
            force=True,
        )
    )

    assert rc == 0
    mock_run_add_capacity.assert_called_once()


@patch(
    "crsbench.cloud.cli._add_capacity.require_launch_state",
    side_effect=SystemExit(
        "cloud add-workers requires saved remote launch state for the target experiment"
    ),
)
@patch("crsbench.cloud.cli._add_capacity.resolve_effective_experiment_name")
def test_run_add_capacity_requires_launch_state(
    mock_resolve_experiment_name,
    mock_require_launch_state,
):
    from crsbench.cloud.cli._add_capacity import run_add_capacity

    mock_resolve_experiment_name.return_value = "test-exp"

    rc = run_add_capacity(_make_add_capacity_args(cloud_command="add-workers"))

    assert rc == 1
    mock_require_launch_state.assert_called_once()


@patch("crsbench.cloud.cli._add_capacity._apply_runtime_added_placement", create=True)
@patch("crsbench.cloud.cli._add_capacity.build_dynamic_placement_request")
@patch("crsbench.cloud.cli._add_capacity.load_experiment_config")
@patch("crsbench.cloud.cli._add_capacity.require_launch_state")
@patch("crsbench.cloud.cli._add_capacity.resolve_effective_experiment_name")
def test_run_add_capacity_loads_config_from_path_object(
    mock_resolve_experiment_name,
    mock_require_launch_state,
    mock_load_experiment_config,
    mock_build_request,
    mock_apply_runtime_added_placement,
):
    from pathlib import Path

    from crsbench.cloud.cli._add_capacity import run_add_capacity
    from crsbench.cloud.expansion import CloudDynamicPlacementRequest

    mock_resolve_experiment_name.return_value = "test-exp"
    mock_require_launch_state.return_value = _make_provider_neutral_operational_context(
        include_launch_state=True
    )
    mock_load_experiment_config.return_value = (
        _make_provider_neutral_experiment_config()
    )
    mock_build_request.return_value = CloudDynamicPlacementRequest(
        role="worker",
        provider=CloudProvider.GCE,
        instance_profile="gce-worker-n2d",
        count=1,
        regions=("us-east5",),
        zones=(),
    )
    mock_apply_runtime_added_placement.return_value = 0

    rc = run_add_capacity(
        _make_add_capacity_args(cloud_command="add-workers", force=True)
    )

    assert rc == 0
    config_arg = mock_load_experiment_config.call_args.args[0]
    assert isinstance(config_arg, Path)


@patch("crsbench.cloud.cli._add_capacity.logger")
@patch("crsbench.cloud.cli._add_capacity._apply_runtime_added_placement", create=True)
@patch("crsbench.cloud.cli._add_capacity.build_dynamic_placement_request")
@patch("crsbench.cloud.cli._add_capacity.load_experiment_config")
@patch("crsbench.cloud.cli._add_capacity.require_launch_state")
@patch("crsbench.cloud.cli._add_capacity.resolve_effective_experiment_name")
def test_run_add_capacity_confirmation_prompt_no(
    mock_resolve_experiment_name,
    mock_require_launch_state,
    mock_load_experiment_config,
    mock_build_request,
    mock_apply_runtime_added_placement,
    mock_logger,
):
    from crsbench.cloud.cli._add_capacity import run_add_capacity
    from crsbench.cloud.expansion import CloudDynamicPlacementRequest

    mock_resolve_experiment_name.return_value = "test-exp"
    mock_require_launch_state.return_value = _make_provider_neutral_operational_context(
        include_launch_state=True
    )
    mock_load_experiment_config.return_value = (
        _make_provider_neutral_experiment_config()
    )
    mock_build_request.return_value = CloudDynamicPlacementRequest(
        role="worker",
        provider=CloudProvider.GCE,
        instance_profile="gce-worker-n2d",
        count=2,
        regions=("us-east5", "us-east1"),
        zones=("us-east5-b", "us-east1-b"),
    )

    with (
        patch("builtins.input", return_value="no"),
        patch("sys.stdin") as mock_stdin,
    ):
        mock_stdin.isatty.return_value = True
        rc = run_add_capacity(_make_add_capacity_args(cloud_command="add-workers"))

    assert rc == 0
    mock_apply_runtime_added_placement.assert_not_called()
    info_calls = [str(call) for call in mock_logger.info.call_args_list]
    assert any("Projected totals after apply" in call for call in info_calls)


@patch("crsbench.cloud.cli._add_capacity.logger")
@patch("crsbench.cloud.cli._add_capacity._apply_runtime_added_placement", create=True)
@patch("crsbench.cloud.cli._add_capacity.build_dynamic_placement_request")
@patch("crsbench.cloud.cli._add_capacity.load_experiment_config")
@patch("crsbench.cloud.cli._add_capacity.require_launch_state")
@patch("crsbench.cloud.cli._add_capacity.resolve_effective_experiment_name")
def test_run_add_capacity_non_tty_without_force(
    mock_resolve_experiment_name,
    mock_require_launch_state,
    mock_load_experiment_config,
    mock_build_request,
    mock_apply_runtime_added_placement,
    mock_logger,
):
    from crsbench.cloud.cli._add_capacity import run_add_capacity
    from crsbench.cloud.expansion import CloudDynamicPlacementRequest

    mock_resolve_experiment_name.return_value = "test-exp"
    mock_require_launch_state.return_value = _make_provider_neutral_operational_context(
        include_launch_state=True
    )
    mock_load_experiment_config.return_value = (
        _make_provider_neutral_experiment_config()
    )
    mock_build_request.return_value = CloudDynamicPlacementRequest(
        role="worker",
        provider=CloudProvider.GCE,
        instance_profile="gce-worker-n2d",
        count=2,
        regions=("us-east5",),
        zones=("us-east5-b",),
    )

    with patch("sys.stdin") as mock_stdin:
        mock_stdin.isatty.return_value = False
        rc = run_add_capacity(_make_add_capacity_args(cloud_command="add-workers"))

    assert rc == 1
    mock_apply_runtime_added_placement.assert_not_called()
    error_calls = [str(call) for call in mock_logger.error.call_args_list]
    assert any("--force" in call for call in error_calls)


@patch("crsbench.cloud.cli._add_capacity._apply_runtime_added_placement", create=True)
@patch("crsbench.cloud.cli._add_capacity.build_dynamic_placement_request")
@patch("crsbench.cloud.cli._add_capacity.load_experiment_config")
@patch("crsbench.cloud.cli._add_capacity.require_launch_state")
@patch("crsbench.cloud.cli._add_capacity.resolve_effective_experiment_name")
def test_run_add_capacity_force_skips_prompt(
    mock_resolve_experiment_name,
    mock_require_launch_state,
    mock_load_experiment_config,
    mock_build_request,
    mock_apply_runtime_added_placement,
):
    from crsbench.cloud.cli._add_capacity import run_add_capacity
    from crsbench.cloud.expansion import CloudDynamicPlacementRequest

    mock_resolve_experiment_name.return_value = "test-exp"
    mock_require_launch_state.return_value = _make_provider_neutral_operational_context(
        include_launch_state=True
    )
    mock_load_experiment_config.return_value = (
        _make_provider_neutral_experiment_config()
    )
    mock_build_request.return_value = CloudDynamicPlacementRequest(
        role="worker",
        provider=CloudProvider.GCE,
        instance_profile="gce-worker-n2d",
        count=2,
        regions=("us-east5",),
        zones=("us-east5-b",),
    )
    mock_apply_runtime_added_placement.return_value = 0

    with patch("builtins.input") as mock_input:
        rc = run_add_capacity(
            _make_add_capacity_args(cloud_command="add-workers", force=True)
        )

    assert rc == 0
    mock_input.assert_not_called()
    mock_apply_runtime_added_placement.assert_called_once()


@patch("crsbench.cloud.cli._add_capacity.save_launch_state")
@patch("crsbench.cloud.cli._add_capacity.append_created_instance_records")
@patch("crsbench.cloud.cli._add_capacity.CloudFleetStatusManager")
@patch("crsbench.cloud.cli._add_capacity.reconnect")
@patch("crsbench.cloud.cli._add_capacity.provisioner_for_context")
@patch("crsbench.cloud.cli._add_capacity.provider_adapter_for_context")
@patch("crsbench.cloud.cli._add_capacity.CloudVmBootstrapInputs.from_experiment_config")
@patch("crsbench.cloud.cli._add_capacity.RuntimeRegistration.from_experiment_config")
def test_apply_runtime_added_worker_placement_saves_launch_state(
    mock_runtime_registration,
    mock_bootstrap_inputs,
    mock_provider_adapter_for_context,
    mock_provisioner_for_context,
    mock_reconnect,
    mock_status_manager_cls,
    mock_append_created_instance_records,
    mock_save_launch_state,
):
    from crsbench.cloud.cli._add_capacity import _apply_runtime_added_placement
    from crsbench.cloud.expansion import CloudDynamicPlacementRequest
    from crsbench.cloud.gce.models import GceWorkerRecord
    from crsbench.cloud.gce.provider import GceProviderAdapter

    config = _make_provider_neutral_experiment_config()
    context = _make_provider_neutral_operational_context(include_launch_state=True)
    request = CloudDynamicPlacementRequest(
        role="worker",
        provider=CloudProvider.GCE,
        instance_profile="gce-worker-n2d",
        count=2,
        regions=("us-east5",),
        zones=("us-east5-b",),
    )
    args = _make_add_capacity_args(cloud_command="add-workers", force=True)

    provisioner = MagicMock()
    provisioner.create_workers.return_value = [
        GceWorkerRecord(
            name="crsbench-test-exp-work-004",
            instance_id="1004",
            status="RUNNING",
            zone="us-east5-b",
            internal_ip="10.0.0.14",
            external_ip=None,
            service_account_email="crsbench@test-project.iam.gserviceaccount.com",
            labels={"crsbench-role": "worker", "owner": "team-crs"},
            raw={},
        )
    ]
    adapter = GceProviderAdapter(provisioner=provisioner)
    adapter.quota_shortages_for_dynamic_fleet = MagicMock(return_value=[])
    mock_provider_adapter_for_context.return_value = adapter
    mock_provisioner_for_context.return_value = provisioner
    mock_runtime_registration.return_value = MagicMock()
    mock_bootstrap_inputs.return_value = MagicMock()
    mock_reconnect.return_value = (
        context,
        MagicMock(),
        MagicMock(),
        MagicMock(),
        Path("/tmp/filestore"),
    )
    mock_status_manager = mock_status_manager_cls.return_value
    mock_status_manager.wait_for_added_instances.return_value = MagicMock(
        requested_count=1,
        ready_count=1,
    )

    rc = _apply_runtime_added_placement(
        args,
        context=context,
        config=config,
        request=request,
    )

    assert rc == 0
    provisioner.create_workers.assert_called_once()
    mock_status_manager.wait_for_added_instances.assert_called_once()
    mock_append_created_instance_records.assert_called_once()
    mock_save_launch_state.assert_called_once()
    saved_state = mock_save_launch_state.call_args.args[1]
    assert saved_state.worker_fleet_configs[-1].placement_source == "runtime_added"
    assert saved_state.worker_fleet_configs[-1].role == "worker"


@patch("crsbench.cloud.cli._add_capacity.save_launch_state")
@patch("crsbench.cloud.cli._add_capacity.append_created_instance_records")
@patch("crsbench.cloud.cli._add_capacity.CloudFleetStatusManager")
@patch("crsbench.cloud.cli._add_capacity.reconnect")
@patch("crsbench.cloud.cli._add_capacity.provisioner_for_context")
@patch("crsbench.cloud.cli._add_capacity.provider_adapter_for_context")
@patch("crsbench.cloud.cli._add_capacity.CloudVmBootstrapInputs.from_experiment_config")
@patch("crsbench.cloud.cli._add_capacity.RuntimeRegistration.from_experiment_config")
def test_apply_runtime_added_evaluator_placement_saves_launch_state(
    mock_runtime_registration,
    mock_bootstrap_inputs,
    mock_provider_adapter_for_context,
    mock_provisioner_for_context,
    mock_reconnect,
    mock_status_manager_cls,
    mock_append_created_instance_records,
    mock_save_launch_state,
):
    from crsbench.cloud.cli._add_capacity import _apply_runtime_added_placement
    from crsbench.cloud.expansion import CloudDynamicPlacementRequest
    from crsbench.cloud.gce.models import GceWorkerRecord
    from crsbench.cloud.gce.provider import GceProviderAdapter

    config = _make_provider_neutral_experiment_config_with_evaluators()
    context = _make_provider_neutral_operational_context(include_launch_state=True)
    request = CloudDynamicPlacementRequest(
        role="evaluator",
        provider=CloudProvider.GCE,
        instance_profile="gce-evaluator-c3",
        count=1,
        zones=("us-east1-b",),
    )
    args = _make_add_capacity_args(
        cloud_command="add-evaluators",
        instance_profile="gce-evaluator-c3",
        count=1,
        regions=None,
        zones="us-east1-b",
        force=True,
    )

    provisioner = MagicMock()
    provisioner.create_evaluators.return_value = [
        GceWorkerRecord(
            name="crsbench-test-exp-eval-001",
            instance_id="2001",
            status="RUNNING",
            zone="us-east1-b",
            internal_ip="10.0.0.24",
            external_ip=None,
            service_account_email="crsbench@test-project.iam.gserviceaccount.com",
            labels={"crsbench-role": "evaluator", "owner": "team-crs"},
            raw={},
        )
    ]
    adapter = GceProviderAdapter(provisioner=provisioner)
    adapter.quota_shortages_for_dynamic_fleet = MagicMock(return_value=[])
    mock_provider_adapter_for_context.return_value = adapter
    mock_provisioner_for_context.return_value = provisioner
    mock_runtime_registration.return_value = MagicMock()
    mock_bootstrap_inputs.return_value = MagicMock()
    mock_reconnect.return_value = (
        context,
        MagicMock(),
        MagicMock(),
        MagicMock(),
        Path("/tmp/filestore"),
    )
    mock_status_manager = mock_status_manager_cls.return_value
    mock_status_manager.wait_for_added_instances.return_value = MagicMock(
        requested_count=1,
        ready_count=1,
    )

    rc = _apply_runtime_added_placement(
        args,
        context=context,
        config=config,
        request=request,
    )

    assert rc == 0
    provisioner.create_evaluators.assert_called_once()
    provisioner.delete_evaluators.assert_not_called()
    mock_status_manager.wait_for_added_instances.assert_called_once()
    mock_append_created_instance_records.assert_called_once()
    mock_save_launch_state.assert_called_once()
    saved_state = mock_save_launch_state.call_args.args[1]
    assert saved_state.evaluator_fleet_configs[-1].placement_source == "runtime_added"
    assert saved_state.evaluator_fleet_configs[-1].role == "evaluator"


@patch("crsbench.cloud.cli._add_capacity.save_launch_state")
@patch("crsbench.cloud.cli._add_capacity.append_created_instance_records")
@patch("crsbench.cloud.cli._add_capacity.CloudFleetStatusManager")
@patch("crsbench.cloud.cli._add_capacity.reconnect")
@patch("crsbench.cloud.cli._add_capacity.provisioner_for_context")
@patch("crsbench.cloud.cli._add_capacity.provider_adapter_for_context")
@patch("crsbench.cloud.cli._add_capacity.CloudVmBootstrapInputs.from_experiment_config")
@patch("crsbench.cloud.cli._add_capacity.RuntimeRegistration.from_experiment_config")
def test_apply_runtime_added_worker_placement_rolls_back_on_readiness_failure(
    mock_runtime_registration,
    mock_bootstrap_inputs,
    mock_provider_adapter_for_context,
    mock_provisioner_for_context,
    mock_reconnect,
    mock_status_manager_cls,
    mock_append_created_instance_records,
    mock_save_launch_state,
):
    from crsbench.cloud.cli._add_capacity import _apply_runtime_added_placement
    from crsbench.cloud.expansion import CloudDynamicPlacementRequest
    from crsbench.cloud.gce.models import GceWorkerRecord
    from crsbench.cloud.gce.provider import GceProviderAdapter

    config = _make_provider_neutral_experiment_config()
    context = _make_provider_neutral_operational_context(include_launch_state=True)
    request = CloudDynamicPlacementRequest(
        role="worker",
        provider=CloudProvider.GCE,
        instance_profile="gce-worker-n2d",
        count=1,
        regions=("us-east5",),
        zones=("us-east5-b",),
    )
    args = _make_add_capacity_args(
        cloud_command="add-workers",
        count=1,
        regions="us-east5",
        zones="us-east5-b",
        force=True,
    )

    provisioner = MagicMock()
    provisioner.create_workers.return_value = [
        GceWorkerRecord(
            name="crsbench-test-exp-work-004",
            instance_id="1004",
            status="RUNNING",
            zone="us-east5-b",
            internal_ip="10.0.0.14",
            external_ip=None,
            service_account_email="crsbench@test-project.iam.gserviceaccount.com",
            labels={"crsbench-role": "worker", "owner": "team-crs"},
            raw={},
        )
    ]
    adapter = GceProviderAdapter(provisioner=provisioner)
    adapter.quota_shortages_for_dynamic_fleet = MagicMock(return_value=[])
    mock_provider_adapter_for_context.return_value = adapter
    mock_provisioner_for_context.return_value = provisioner
    mock_runtime_registration.return_value = MagicMock()
    mock_bootstrap_inputs.return_value = MagicMock()
    mock_reconnect.return_value = (
        context,
        MagicMock(),
        MagicMock(),
        MagicMock(),
        Path("/tmp/filestore"),
    )
    mock_status_manager = mock_status_manager_cls.return_value
    mock_status_manager.wait_for_added_instances.side_effect = RuntimeError(
        "bootstrap failed"
    )

    rc = _apply_runtime_added_placement(
        args,
        context=context,
        config=config,
        request=request,
    )

    assert rc == 1
    provisioner.create_workers.assert_called_once()
    provisioner.delete_workers.assert_called_once()
    mock_append_created_instance_records.assert_not_called()
    mock_save_launch_state.assert_not_called()


@patch("crsbench.cloud.cli._add_capacity.save_launch_state")
@patch("crsbench.cloud.cli._add_capacity.append_created_instance_records")
@patch("crsbench.cloud.cli._add_capacity.CloudFleetStatusManager")
@patch("crsbench.cloud.cli._add_capacity.reconnect")
@patch("crsbench.cloud.cli._add_capacity.provisioner_for_context")
@patch("crsbench.cloud.cli._add_capacity.provider_adapter_for_context")
@patch("crsbench.cloud.cli._add_capacity.CloudVmBootstrapInputs.from_experiment_config")
@patch("crsbench.cloud.cli._add_capacity.RuntimeRegistration.from_experiment_config")
def test_apply_runtime_added_worker_placement_stops_on_quota_shortage(
    mock_runtime_registration,
    mock_bootstrap_inputs,
    mock_provider_adapter_for_context,
    mock_provisioner_for_context,
    mock_reconnect,
    mock_status_manager_cls,
    mock_append_created_instance_records,
    mock_save_launch_state,
):
    from crsbench.cloud.cli._add_capacity import _apply_runtime_added_placement
    from crsbench.cloud.expansion import CloudDynamicPlacementRequest
    from crsbench.cloud.gce.provider import GceProviderAdapter
    from crsbench.cloud.models import QuotaShortage

    config = _make_provider_neutral_experiment_config()
    context = _make_provider_neutral_operational_context(include_launch_state=True)
    request = CloudDynamicPlacementRequest(
        role="worker",
        provider=CloudProvider.GCE,
        instance_profile="gce-worker-n2d",
        count=4,
        regions=("us-east5",),
        zones=("us-east5-b",),
    )
    args = _make_add_capacity_args(
        cloud_command="add-workers",
        count=4,
        regions="us-east5",
        zones="us-east5-b",
        force=True,
    )

    provisioner = MagicMock()
    adapter = GceProviderAdapter(provisioner=provisioner)
    adapter.quota_shortages_for_dynamic_fleet = MagicMock(
        return_value=[
            QuotaShortage(
                provider=CloudProvider.GCE,
                scope="us-east5",
                resource_family="N2D_CPUS",
                required=64,
                available=0,
            )
        ]
    )
    mock_provider_adapter_for_context.return_value = adapter
    mock_provisioner_for_context.return_value = provisioner
    mock_runtime_registration.return_value = MagicMock()
    mock_bootstrap_inputs.return_value = MagicMock()
    mock_reconnect.return_value = (
        context,
        MagicMock(),
        MagicMock(),
        MagicMock(),
        Path("/tmp/filestore"),
    )

    rc = _apply_runtime_added_placement(
        args,
        context=context,
        config=config,
        request=request,
    )

    assert rc == 1
    provisioner.create_workers.assert_not_called()
    provisioner.delete_workers.assert_not_called()
    mock_reconnect.assert_not_called()
    mock_status_manager_cls.assert_not_called()
    mock_append_created_instance_records.assert_not_called()
    mock_save_launch_state.assert_not_called()


@patch("crsbench.cloud.cli._monitor.initialize_queue")
@patch("crsbench.cloud.cli._monitor.load_apprise_notification_config")
@patch("crsbench.cloud.cli._monitor.send_apprise_message")
@patch("crsbench.cloud.cli._monitor.resolve_effective_experiment_name")
@patch(
    "crsbench.cloud.cli._monitor.require_launch_state",
    side_effect=SystemExit("cloud monitor requires saved remote launch state"),
)
def test_run_monitor_requires_launch_state(
    mock_require_state,
    mock_resolve_experiment_name,
    mock_initialize_queue,
    mock_load_notification_config,
    mock_send_apprise_message,
):
    from crsbench.cloud.cli._monitor import run_monitor

    mock_resolve_experiment_name.return_value = "test-exp"
    rc = run_monitor(_make_monitor_args())

    assert rc == 1
    mock_resolve_experiment_name.assert_called_once_with("/tmp/config.yaml", "test-exp")
    mock_require_state.assert_called_once_with("/tmp/config.yaml", "test-exp")
    mock_initialize_queue.assert_not_called()
    mock_load_notification_config.assert_not_called()
    mock_send_apprise_message.assert_not_called()


@patch("crsbench.cloud.cli._monitor.monitor_queue")
@patch("crsbench.cloud.cli._monitor.initialize_queue")
@patch("crsbench.cloud.cli._monitor.probe_redis_connection")
@patch("crsbench.cloud.cli._monitor.OrchestratorRedisTunnel.from_launch_state")
@patch("crsbench.cloud.cli._monitor.require_launch_state")
@patch("crsbench.cloud.cli._monitor.resolve_effective_experiment_name")
def test_run_monitor_passes_saved_redis_password(
    mock_resolve_experiment_name,
    mock_require_state,
    mock_tunnel_cls,
    mock_probe_redis_connection,
    mock_initialize_queue,
    mock_monitor_queue,
):
    from crsbench.cloud.cli._monitor import run_monitor

    context = _make_provider_neutral_operational_context(include_launch_state=True)
    assert context.launch_state is not None
    mock_resolve_experiment_name.return_value = "test-exp"
    mock_require_state.return_value = context
    mock_tunnel = MagicMock()
    mock_tunnel.redis_host = "127.0.0.1:16379"
    mock_tunnel_cls.return_value.__enter__.return_value = mock_tunnel
    mock_probe_redis_connection.return_value = (RedisConnectionProbe.READY, None)
    mock_initialize_queue.return_value = MagicMock()

    rc = run_monitor(_make_monitor_args())

    assert rc == 0
    mock_resolve_experiment_name.assert_called_once_with("/tmp/config.yaml", "test-exp")
    mock_initialize_queue.assert_called_once_with(
        "127.0.0.1:16379",
        "test-exp",
        redis_password="shared-secret",
    )
    mock_monitor_queue.assert_called_once()
    assert mock_monitor_queue.call_args.kwargs["exit_when_idle"] is False


@patch("crsbench.cloud.cli._monitor.monitor_queue")
@patch("crsbench.cloud.cli._monitor.initialize_queue")
@patch("crsbench.cloud.cli._monitor.probe_redis_connection")
@patch("crsbench.cloud.cli._monitor.OrchestratorRedisTunnel.from_launch_state")
@patch("crsbench.cloud.cli._monitor.require_launch_state")
@patch("crsbench.cloud.cli._monitor.resolve_effective_experiment_name")
def test_run_monitor_infers_experiment_from_config(
    mock_resolve_experiment_name,
    mock_require_state,
    mock_tunnel_cls,
    mock_probe_redis_connection,
    mock_initialize_queue,
    mock_monitor_queue,
):
    from crsbench.cloud.cli._monitor import run_monitor

    context = _make_provider_neutral_operational_context(include_launch_state=True)
    assert context.launch_state is not None
    mock_resolve_experiment_name.return_value = "inferred-exp"
    mock_require_state.return_value = context
    mock_tunnel = MagicMock()
    mock_tunnel.redis_host = "127.0.0.1:16379"
    mock_tunnel_cls.return_value.__enter__.return_value = mock_tunnel
    mock_probe_redis_connection.return_value = (RedisConnectionProbe.READY, None)
    mock_initialize_queue.return_value = MagicMock()

    rc = run_monitor(_make_monitor_args(experiment=None))

    assert rc == 0
    mock_resolve_experiment_name.assert_called_once_with("/tmp/config.yaml", None)
    mock_require_state.assert_called_once_with("/tmp/config.yaml", "inferred-exp")
    mock_initialize_queue.assert_called_once_with(
        "127.0.0.1:16379",
        "inferred-exp",
        redis_password="shared-secret",
    )
    mock_monitor_queue.assert_called_once()


@patch("crsbench.cloud.cli._monitor.time.sleep")
@patch("crsbench.cloud.cli._monitor.monitor_queue")
@patch("crsbench.cloud.cli._monitor.initialize_queue")
@patch("crsbench.cloud.cli._monitor.probe_redis_connection")
@patch("crsbench.cloud.cli._monitor.OrchestratorRedisTunnel.from_launch_state")
@patch("crsbench.cloud.cli._monitor.require_launch_state")
def test_run_monitor_retries_until_redis_is_ready(
    mock_require_state,
    mock_tunnel_cls,
    mock_probe_redis_connection,
    mock_initialize_queue,
    mock_monitor_queue,
    mock_sleep,
):
    from crsbench.cloud.cli._monitor import run_monitor

    context = _make_provider_neutral_operational_context(include_launch_state=True)
    assert context.launch_state is not None
    mock_require_state.return_value = context
    mock_tunnel = MagicMock()
    mock_tunnel.redis_host = "127.0.0.1:16379"
    mock_tunnel_cls.return_value.__enter__.return_value = mock_tunnel
    mock_probe_redis_connection.side_effect = [
        (RedisConnectionProbe.RETRYABLE, "connection refused"),
        (RedisConnectionProbe.READY, None),
    ]
    mock_initialize_queue.return_value = MagicMock()

    rc = run_monitor(_make_monitor_args())

    assert rc == 0
    assert mock_probe_redis_connection.call_count == 2
    mock_sleep.assert_called_once()
    mock_initialize_queue.assert_called_once_with(
        "127.0.0.1:16379",
        "test-exp",
        redis_password="shared-secret",
    )
    mock_monitor_queue.assert_called_once()


@patch("crsbench.cloud.cli._monitor.logger.warning")
@patch("crsbench.cloud.cli._monitor.monitor_queue")
@patch("crsbench.cloud.cli._monitor.initialize_queue")
@patch("crsbench.cloud.cli._monitor.probe_redis_connection")
@patch("crsbench.cloud.cli._monitor.OrchestratorRedisTunnel.from_launch_state")
@patch("crsbench.cloud.cli._monitor.require_launch_state")
@patch("crsbench.cloud.cli._monitor.resolve_effective_experiment_name")
def test_run_monitor_warns_when_operator_and_orchestrator_apprise_notifications_are_enabled(
    mock_resolve_experiment_name,
    mock_require_state,
    mock_tunnel_cls,
    mock_probe_redis_connection,
    mock_initialize_queue,
    mock_monitor_queue,
    mock_logger_warning,
    monkeypatch,
):
    from crsbench.cloud.cli._monitor import run_monitor
    from crsbench.cloud.models import build_cloud_launch_plan

    config = _with_layered_env_overrides(_make_provider_neutral_experiment_config())
    assert config.cloud is not None
    config.cloud.env["CRSBENCH_NOTIFY_APPRISE_URLS"] = "os.environ/ORCH_APPRISE_URLS"
    config.cloud.orchestrator.env["CRSBENCH_NOTIFY_APPRISE_TITLE"] = "Orchestrator"
    config.cloud.orchestrator.env["CRSBENCH_NOTIFY_APPRISE_TAG"] = "ops"
    launch_plan = build_cloud_launch_plan(config)
    context = _make_provider_neutral_operational_context(
        include_launch_state=True,
        launch_plan=launch_plan,
    )
    assert context.launch_state is not None
    mock_resolve_experiment_name.return_value = "test-exp"
    mock_require_state.return_value = context
    mock_tunnel = MagicMock()
    mock_tunnel.redis_host = "127.0.0.1:16379"
    mock_tunnel_cls.return_value.__enter__.return_value = mock_tunnel
    mock_probe_redis_connection.return_value = (RedisConnectionProbe.READY, None)
    mock_initialize_queue.return_value = MagicMock()
    monkeypatch.setenv("ORCH_APPRISE_URLS", "discord://global/apprise")
    monkeypatch.setenv("CRSBENCH_NOTIFY_APPRISE_URLS", "discord://operator/apprise")
    monkeypatch.setenv("CRSBENCH_NOTIFY_APPRISE_TITLE", "Operator")

    rc = run_monitor(_make_monitor_args())

    assert rc == 0
    mock_logger_warning.assert_called_once()
    warning_text = str(mock_logger_warning.call_args.args[0])
    assert "duplicate terminal notifications" in warning_text
    assert "CRSBENCH_NOTIFY_APPRISE_URLS" in warning_text
    mock_monitor_queue.assert_called_once()


@patch("crsbench.cloud.cli._monitor.logger.warning")
@patch("crsbench.cloud.cli._monitor.monitor_queue")
@patch("crsbench.cloud.cli._monitor.initialize_queue")
@patch("crsbench.cloud.cli._monitor.probe_redis_connection")
@patch("crsbench.cloud.cli._monitor.OrchestratorRedisTunnel.from_launch_state")
@patch("crsbench.cloud.cli._monitor.require_launch_state")
@patch("crsbench.cloud.cli._monitor.resolve_effective_experiment_name")
def test_run_monitor_does_not_warn_when_orchestrator_apprise_env_ref_is_unset(
    mock_resolve_experiment_name,
    mock_require_state,
    mock_tunnel_cls,
    mock_probe_redis_connection,
    mock_initialize_queue,
    mock_monitor_queue,
    mock_logger_warning,
    monkeypatch,
):
    from crsbench.cloud.cli._monitor import run_monitor
    from crsbench.cloud.models import build_cloud_launch_plan

    config = _with_layered_env_overrides(_make_provider_neutral_experiment_config())
    assert config.cloud is not None
    config.cloud.env["CRSBENCH_NOTIFY_APPRISE_URLS"] = (
        "os.environ/MISSING_ORCH_APPRISE_URLS"
    )
    config.cloud.orchestrator.env["CRSBENCH_NOTIFY_APPRISE_TITLE"] = "Orchestrator"
    launch_plan = build_cloud_launch_plan(config)
    context = _make_provider_neutral_operational_context(
        include_launch_state=True,
        launch_plan=launch_plan,
    )
    assert context.launch_state is not None
    mock_resolve_experiment_name.return_value = "test-exp"
    mock_require_state.return_value = context
    mock_tunnel = MagicMock()
    mock_tunnel.redis_host = "127.0.0.1:16379"
    mock_tunnel_cls.return_value.__enter__.return_value = mock_tunnel
    mock_probe_redis_connection.return_value = (RedisConnectionProbe.READY, None)
    mock_initialize_queue.return_value = MagicMock()
    monkeypatch.delenv("MISSING_ORCH_APPRISE_URLS", raising=False)
    monkeypatch.setenv("CRSBENCH_NOTIFY_APPRISE_URLS", "discord://operator/apprise")

    rc = run_monitor(_make_monitor_args())

    assert rc == 0
    mock_logger_warning.assert_not_called()
    mock_monitor_queue.assert_called_once()


@patch("crsbench.cloud.cli._monitor.send_apprise_message")
@patch("crsbench.cloud.cli._monitor.load_apprise_notification_config")
@patch("crsbench.cloud.cli._monitor.initialize_queue")
@patch("crsbench.cloud.cli._monitor.probe_redis_connection")
@patch("crsbench.cloud.cli._monitor.OrchestratorRedisTunnel.from_launch_state")
@patch("crsbench.cloud.cli._monitor.require_launch_state")
def test_run_monitor_does_not_notify_on_pre_monitor_redis_wait_failure(
    mock_require_state,
    mock_tunnel_cls,
    mock_probe_redis_connection,
    mock_initialize_queue,
    mock_load_notification_config,
    mock_send_apprise_message,
):
    from crsbench.cloud.cli._monitor import run_monitor

    context = _make_provider_neutral_operational_context(include_launch_state=True)
    assert context.launch_state is not None
    mock_require_state.return_value = context
    mock_tunnel = MagicMock()
    mock_tunnel.redis_host = "127.0.0.1:16379"
    mock_tunnel_cls.return_value.__enter__.return_value = mock_tunnel
    mock_probe_redis_connection.side_effect = [
        (RedisConnectionProbe.RETRYABLE, "connection refused"),
        (RedisConnectionProbe.FATAL, "auth failed"),
    ]

    rc = run_monitor(_make_monitor_args())

    assert rc == 1
    mock_initialize_queue.assert_not_called()
    mock_load_notification_config.assert_not_called()
    mock_send_apprise_message.assert_not_called()


@patch("crsbench.cloud.cli._monitor.monitor_queue", side_effect=KeyboardInterrupt)
@patch("crsbench.cloud.cli._monitor.initialize_queue")
@patch("crsbench.cloud.cli._monitor.probe_redis_connection")
@patch("crsbench.cloud.cli._monitor.OrchestratorRedisTunnel.from_launch_state")
@patch("crsbench.cloud.cli._monitor.require_launch_state")
@patch("crsbench.cloud.cli._monitor.resolve_effective_experiment_name")
def test_run_monitor_treats_keyboard_interrupt_as_normal_exit(
    mock_resolve_experiment_name,
    mock_require_state,
    mock_tunnel_cls,
    mock_probe_redis_connection,
    mock_initialize_queue,
    mock_monitor_queue,
):
    from crsbench.cloud.cli._monitor import run_monitor

    context = _make_provider_neutral_operational_context(include_launch_state=True)
    assert context.launch_state is not None
    mock_resolve_experiment_name.return_value = "test-exp"
    mock_require_state.return_value = context
    mock_tunnel = MagicMock()
    mock_tunnel.redis_host = "127.0.0.1:16379"
    mock_tunnel_cls.return_value.__enter__.return_value = mock_tunnel
    mock_probe_redis_connection.return_value = (RedisConnectionProbe.READY, None)
    mock_initialize_queue.return_value = MagicMock()
    assert mock_monitor_queue is not None

    rc = run_monitor(_make_monitor_args())

    assert rc == 130


@patch("crsbench.cloud.cli._monitor.send_apprise_message")
@patch("crsbench.cloud.cli._monitor.load_apprise_notification_config")
@patch("crsbench.cloud.cli._monitor.rq.job.Job.fetch")
@patch("crsbench.cloud.cli._monitor.is_job_for_experiment")
@patch("crsbench.cloud.cli._monitor.monitor_queue")
@patch("crsbench.cloud.cli._monitor.initialize_queue")
@patch("crsbench.cloud.cli._monitor.probe_redis_connection")
@patch("crsbench.cloud.cli._monitor.OrchestratorRedisTunnel.from_launch_state")
@patch("crsbench.cloud.cli._monitor.require_launch_state")
@patch("crsbench.cloud.cli._monitor.resolve_effective_experiment_name")
def test_run_monitor_sends_completion_notification_on_first_active_to_idle_snapshot(
    mock_resolve_experiment_name,
    mock_require_state,
    mock_tunnel_cls,
    mock_probe_redis_connection,
    mock_initialize_queue,
    mock_monitor_queue,
    mock_is_job_for_experiment,
    mock_job_fetch,
    mock_load_notification_config,
    mock_send_apprise_message,
):
    from crsbench.cloud.cli._monitor import run_monitor

    context = _make_provider_neutral_operational_context(include_launch_state=True)
    assert context.launch_state is not None
    mock_resolve_experiment_name.return_value = "test-exp"
    mock_require_state.return_value = context
    mock_tunnel = MagicMock()
    mock_tunnel.redis_host = "127.0.0.1:16379"
    mock_tunnel_cls.return_value.__enter__.return_value = mock_tunnel
    mock_probe_redis_connection.return_value = (RedisConnectionProbe.READY, None)
    mock_queue = MagicMock()
    mock_queue.deferred_job_registry.get_job_ids.return_value = []
    mock_queue.scheduled_job_registry.get_job_ids.return_value = []
    mock_initialize_queue.return_value = mock_queue
    mock_load_notification_config.return_value = MagicMock()
    mock_is_job_for_experiment.return_value = False
    mock_job_fetch.return_value = None

    def _monitor_side_effect(*args, **kwargs):
        del args
        callbacks = kwargs["callbacks"]
        callbacks.on_snapshot(
            SimpleNamespace(
                stats={"queued": 1, "started": 0, "finished": 0, "failed": 0}
            )
        )
        callbacks.on_snapshot(
            SimpleNamespace(
                stats={"queued": 0, "started": 0, "finished": 1, "failed": 0}
            )
        )

    mock_monitor_queue.side_effect = _monitor_side_effect

    rc = run_monitor(_make_monitor_args())

    assert rc == 0
    mock_load_notification_config.assert_called_once_with()
    mock_send_apprise_message.assert_called_once()
    body = mock_send_apprise_message.call_args.kwargs["body"]
    assert "Cloud monitor run completed" in body
    assert "Experiment: test-exp" in body
    assert "Result: queue drained" in body
    assert "Queued: 0" in body
    assert "Started: 0" in body
    assert "Finished: 1" in body
    assert "Failed: 0" in body
    mock_monitor_queue.assert_called_once()
    assert mock_monitor_queue.call_args.kwargs["exit_when_idle"] is False


@patch("crsbench.cloud.cli._monitor.send_apprise_message")
@patch("crsbench.cloud.cli._monitor.load_apprise_notification_config")
@patch("crsbench.cloud.cli._monitor.rq.job.Job.fetch")
@patch("crsbench.cloud.cli._monitor.is_job_for_experiment")
@patch("crsbench.cloud.cli._monitor.monitor_queue")
@patch("crsbench.cloud.cli._monitor.initialize_queue")
@patch("crsbench.cloud.cli._monitor.probe_redis_connection")
@patch("crsbench.cloud.cli._monitor.OrchestratorRedisTunnel.from_launch_state")
@patch("crsbench.cloud.cli._monitor.require_launch_state")
@patch("crsbench.cloud.cli._monitor.resolve_effective_experiment_name")
def test_run_monitor_does_not_notify_completion_while_deferred_or_scheduled_work_remains(
    mock_resolve_experiment_name,
    mock_require_state,
    mock_tunnel_cls,
    mock_probe_redis_connection,
    mock_initialize_queue,
    mock_monitor_queue,
    mock_is_job_for_experiment,
    mock_job_fetch,
    mock_load_notification_config,
    mock_send_apprise_message,
):
    from crsbench.cloud.cli._monitor import run_monitor

    context = _make_provider_neutral_operational_context(include_launch_state=True)
    assert context.launch_state is not None
    mock_resolve_experiment_name.return_value = "test-exp"
    mock_require_state.return_value = context
    mock_tunnel = MagicMock()
    mock_tunnel.redis_host = "127.0.0.1:16379"
    mock_tunnel_cls.return_value.__enter__.return_value = mock_tunnel
    mock_probe_redis_connection.return_value = (RedisConnectionProbe.READY, None)
    mock_queue = MagicMock()
    mock_queue.deferred_job_registry.get_job_ids.return_value = ["deferred-1"]
    mock_queue.scheduled_job_registry.get_job_ids.return_value = ["scheduled-1"]
    mock_initialize_queue.return_value = mock_queue
    mock_load_notification_config.return_value = MagicMock()
    mock_job_fetch.return_value = MagicMock()
    mock_is_job_for_experiment.return_value = True

    def _monitor_side_effect(*args, **kwargs):
        del args
        callbacks = kwargs["callbacks"]
        callbacks.on_snapshot(
            SimpleNamespace(
                stats={"queued": 1, "started": 0, "finished": 0, "failed": 0}
            )
        )
        callbacks.on_snapshot(
            SimpleNamespace(
                stats={"queued": 0, "started": 0, "finished": 1, "failed": 0}
            )
        )

    mock_monitor_queue.side_effect = _monitor_side_effect

    rc = run_monitor(_make_monitor_args())

    assert rc == 0
    mock_load_notification_config.assert_called_once_with()
    mock_send_apprise_message.assert_not_called()
    mock_monitor_queue.assert_called_once()


@patch("crsbench.cloud.cli._monitor.send_apprise_message")
@patch("crsbench.cloud.cli._monitor.load_apprise_notification_config")
@patch("crsbench.cloud.cli._monitor.rq.job.Job.fetch")
@patch("crsbench.cloud.cli._monitor.is_job_for_experiment")
@patch("crsbench.cloud.cli._monitor.monitor_queue")
@patch("crsbench.cloud.cli._monitor.initialize_queue")
@patch("crsbench.cloud.cli._monitor.probe_redis_connection")
@patch("crsbench.cloud.cli._monitor.OrchestratorRedisTunnel.from_launch_state")
@patch("crsbench.cloud.cli._monitor.require_launch_state")
@patch("crsbench.cloud.cli._monitor.resolve_effective_experiment_name")
def test_run_monitor_sends_completion_notification_after_initial_idle_then_active_to_idle(
    mock_resolve_experiment_name,
    mock_require_state,
    mock_tunnel_cls,
    mock_probe_redis_connection,
    mock_initialize_queue,
    mock_monitor_queue,
    mock_is_job_for_experiment,
    mock_job_fetch,
    mock_load_notification_config,
    mock_send_apprise_message,
):
    from crsbench.cloud.cli._monitor import run_monitor

    context = _make_provider_neutral_operational_context(include_launch_state=True)
    assert context.launch_state is not None
    mock_resolve_experiment_name.return_value = "test-exp"
    mock_require_state.return_value = context
    mock_tunnel = MagicMock()
    mock_tunnel.redis_host = "127.0.0.1:16379"
    mock_tunnel_cls.return_value.__enter__.return_value = mock_tunnel
    mock_probe_redis_connection.return_value = (RedisConnectionProbe.READY, None)
    mock_queue = MagicMock()
    mock_queue.deferred_job_registry.get_job_ids.return_value = []
    mock_queue.scheduled_job_registry.get_job_ids.return_value = []
    mock_initialize_queue.return_value = mock_queue
    mock_load_notification_config.return_value = MagicMock()
    mock_is_job_for_experiment.return_value = False
    mock_job_fetch.return_value = None

    def _monitor_side_effect(*args, **kwargs):
        del args
        callbacks = kwargs["callbacks"]
        callbacks.on_snapshot(
            SimpleNamespace(
                stats={"queued": 0, "started": 0, "finished": 0, "failed": 0}
            )
        )
        callbacks.on_snapshot(
            SimpleNamespace(
                stats={"queued": 1, "started": 0, "finished": 0, "failed": 0}
            )
        )
        callbacks.on_snapshot(
            SimpleNamespace(
                stats={"queued": 0, "started": 0, "finished": 1, "failed": 0}
            )
        )

    mock_monitor_queue.side_effect = _monitor_side_effect

    rc = run_monitor(_make_monitor_args())

    assert rc == 0
    mock_load_notification_config.assert_called_once_with()
    mock_send_apprise_message.assert_called_once()
    body = mock_send_apprise_message.call_args.kwargs["body"]
    assert "Cloud monitor run completed" in body
    assert "Result: queue drained" in body
    assert "Queued: 0" in body
    assert "Started: 0" in body
    assert "Finished: 1" in body
    assert "Failed: 0" in body
    assert "Source: cloud monitor" in body


@patch("crsbench.cloud.cli._monitor.send_apprise_message")
@patch("crsbench.cloud.cli._monitor.load_apprise_notification_config")
@patch("crsbench.cloud.cli._monitor.rq.job.Job.fetch")
@patch("crsbench.cloud.cli._monitor.is_job_for_experiment")
@patch("crsbench.cloud.cli._monitor.monitor_queue")
@patch("crsbench.cloud.cli._monitor.initialize_queue")
@patch("crsbench.cloud.cli._monitor.probe_redis_connection")
@patch("crsbench.cloud.cli._monitor.OrchestratorRedisTunnel.from_launch_state")
@patch("crsbench.cloud.cli._monitor.require_launch_state")
@patch("crsbench.cloud.cli._monitor.resolve_effective_experiment_name")
def test_run_monitor_reports_failed_terminal_drain_in_completion_notification(
    mock_resolve_experiment_name,
    mock_require_state,
    mock_tunnel_cls,
    mock_probe_redis_connection,
    mock_initialize_queue,
    mock_monitor_queue,
    mock_is_job_for_experiment,
    mock_job_fetch,
    mock_load_notification_config,
    mock_send_apprise_message,
):
    from crsbench.cloud.cli._monitor import run_monitor

    context = _make_provider_neutral_operational_context(include_launch_state=True)
    assert context.launch_state is not None
    mock_resolve_experiment_name.return_value = "test-exp"
    mock_require_state.return_value = context
    mock_tunnel = MagicMock()
    mock_tunnel.redis_host = "127.0.0.1:16379"
    mock_tunnel_cls.return_value.__enter__.return_value = mock_tunnel
    mock_probe_redis_connection.return_value = (RedisConnectionProbe.READY, None)
    mock_queue = MagicMock()
    mock_queue.deferred_job_registry.get_job_ids.return_value = []
    mock_queue.scheduled_job_registry.get_job_ids.return_value = []
    mock_initialize_queue.return_value = mock_queue
    mock_load_notification_config.return_value = MagicMock()
    mock_is_job_for_experiment.return_value = False
    mock_job_fetch.return_value = None

    def _monitor_side_effect(*args, **kwargs):
        del args
        callbacks = kwargs["callbacks"]
        callbacks.on_snapshot(
            SimpleNamespace(
                stats={"queued": 1, "started": 0, "finished": 0, "failed": 0}
            )
        )
        callbacks.on_snapshot(
            SimpleNamespace(
                stats={"queued": 0, "started": 0, "finished": 3, "failed": 2}
            )
        )

    mock_monitor_queue.side_effect = _monitor_side_effect

    rc = run_monitor(_make_monitor_args())

    assert rc == 0
    mock_send_apprise_message.assert_called_once()
    body = mock_send_apprise_message.call_args.kwargs["body"]
    assert "Cloud monitor run failed: queue drained with failed jobs" in body
    assert "Result: queue drained with failures" in body
    assert "Finished: 3" in body
    assert "Failed: 2" in body
    assert "completed" not in body


@patch("crsbench.cloud.cli._monitor.send_apprise_message")
@patch("crsbench.cloud.cli._monitor.load_apprise_notification_config")
@patch("crsbench.cloud.cli._monitor.monitor_queue")
@patch("crsbench.cloud.cli._monitor.initialize_queue")
@patch("crsbench.cloud.cli._monitor.probe_redis_connection")
@patch("crsbench.cloud.cli._monitor.OrchestratorRedisTunnel.from_launch_state")
@patch("crsbench.cloud.cli._monitor.require_launch_state")
@patch("crsbench.cloud.cli._monitor.resolve_effective_experiment_name")
def test_run_monitor_does_not_notify_completion_when_pending_lookup_fails(
    mock_resolve_experiment_name,
    mock_require_state,
    mock_tunnel_cls,
    mock_probe_redis_connection,
    mock_initialize_queue,
    mock_monitor_queue,
    mock_load_notification_config,
    mock_send_apprise_message,
):
    from crsbench.cloud.cli._monitor import run_monitor

    context = _make_provider_neutral_operational_context(include_launch_state=True)
    assert context.launch_state is not None
    mock_resolve_experiment_name.return_value = "test-exp"
    mock_require_state.return_value = context
    mock_tunnel = MagicMock()
    mock_tunnel.redis_host = "127.0.0.1:16379"
    mock_tunnel_cls.return_value.__enter__.return_value = mock_tunnel
    mock_probe_redis_connection.return_value = (RedisConnectionProbe.READY, None)
    mock_queue = MagicMock()
    mock_queue.deferred_job_registry.get_job_ids.side_effect = RuntimeError(
        "redis probe failed"
    )
    mock_queue.scheduled_job_registry.get_job_ids.return_value = []
    mock_initialize_queue.return_value = mock_queue
    mock_load_notification_config.return_value = MagicMock()

    def _monitor_side_effect(*args, **kwargs):
        del args
        callbacks = kwargs["callbacks"]
        callbacks.on_snapshot(
            SimpleNamespace(
                stats={"queued": 1, "started": 0, "finished": 0, "failed": 0}
            )
        )
        callbacks.on_snapshot(
            SimpleNamespace(
                stats={"queued": 0, "started": 0, "finished": 1, "failed": 0}
            )
        )

    mock_monitor_queue.side_effect = _monitor_side_effect

    rc = run_monitor(_make_monitor_args())

    assert rc == 0
    mock_send_apprise_message.assert_not_called()


@patch("crsbench.cloud.cli._monitor.send_apprise_message")
@patch("crsbench.cloud.cli._monitor.load_apprise_notification_config")
@patch("crsbench.cloud.cli._monitor.monitor_queue")
@patch("crsbench.cloud.cli._monitor.initialize_queue")
@patch("crsbench.cloud.cli._monitor.probe_redis_connection")
@patch("crsbench.cloud.cli._monitor.OrchestratorRedisTunnel.from_launch_state")
@patch("crsbench.cloud.cli._monitor.require_launch_state")
@patch("crsbench.cloud.cli._monitor.resolve_effective_experiment_name")
def test_run_monitor_does_not_notify_completion_after_initial_idle_lookup_failure(
    mock_resolve_experiment_name,
    mock_require_state,
    mock_tunnel_cls,
    mock_probe_redis_connection,
    mock_initialize_queue,
    mock_monitor_queue,
    mock_load_notification_config,
    mock_send_apprise_message,
):
    from crsbench.cloud.cli._monitor import run_monitor

    context = _make_provider_neutral_operational_context(include_launch_state=True)
    assert context.launch_state is not None
    mock_resolve_experiment_name.return_value = "test-exp"
    mock_require_state.return_value = context
    mock_tunnel = MagicMock()
    mock_tunnel.redis_host = "127.0.0.1:16379"
    mock_tunnel_cls.return_value.__enter__.return_value = mock_tunnel
    mock_probe_redis_connection.return_value = (RedisConnectionProbe.READY, None)
    mock_queue = MagicMock()
    mock_queue.deferred_job_registry.get_job_ids.side_effect = [
        RuntimeError("redis probe failed"),
        [],
    ]
    mock_queue.scheduled_job_registry.get_job_ids.return_value = []
    mock_initialize_queue.return_value = mock_queue
    mock_load_notification_config.return_value = MagicMock()

    def _monitor_side_effect(*args, **kwargs):
        del args
        callbacks = kwargs["callbacks"]
        callbacks.on_snapshot(
            SimpleNamespace(
                stats={"queued": 0, "started": 0, "finished": 0, "failed": 0}
            )
        )
        callbacks.on_snapshot(
            SimpleNamespace(
                stats={"queued": 0, "started": 0, "finished": 1, "failed": 0}
            )
        )

    mock_monitor_queue.side_effect = _monitor_side_effect

    rc = run_monitor(_make_monitor_args())

    assert rc == 0
    mock_send_apprise_message.assert_not_called()


@patch("crsbench.cloud.cli._monitor.send_apprise_message")
@patch("crsbench.cloud.cli._monitor.load_apprise_notification_config")
@patch("crsbench.cloud.cli._monitor.monitor_queue")
@patch("crsbench.cloud.cli._monitor.initialize_queue")
@patch("crsbench.cloud.cli._monitor.probe_redis_connection")
@patch("crsbench.cloud.cli._monitor.OrchestratorRedisTunnel.from_launch_state")
@patch("crsbench.cloud.cli._monitor.require_launch_state")
@patch("crsbench.cloud.cli._monitor.resolve_effective_experiment_name")
def test_run_monitor_sends_failure_notification_after_session_start(
    mock_resolve_experiment_name,
    mock_require_state,
    mock_tunnel_cls,
    mock_probe_redis_connection,
    mock_initialize_queue,
    mock_monitor_queue,
    mock_load_notification_config,
    mock_send_apprise_message,
):
    from crsbench.cloud.cli._monitor import run_monitor

    context = _make_provider_neutral_operational_context(include_launch_state=True)
    assert context.launch_state is not None
    mock_resolve_experiment_name.return_value = "test-exp"
    mock_require_state.return_value = context
    mock_tunnel = MagicMock()
    mock_tunnel.redis_host = "127.0.0.1:16379"
    mock_tunnel_cls.return_value.__enter__.return_value = mock_tunnel
    mock_probe_redis_connection.return_value = (RedisConnectionProbe.READY, None)
    mock_initialize_queue.return_value = MagicMock()
    mock_load_notification_config.return_value = MagicMock()

    def _monitor_side_effect(*args, **kwargs):
        del args
        callbacks = kwargs["callbacks"]
        callbacks.on_snapshot(
            SimpleNamespace(
                stats={"queued": 1, "started": 0, "finished": 0, "failed": 0}
            )
        )
        raise RuntimeError("monitor loop exploded")

    mock_monitor_queue.side_effect = _monitor_side_effect

    rc = run_monitor(_make_monitor_args())

    assert rc == 1
    mock_load_notification_config.assert_called_once_with()
    mock_send_apprise_message.assert_called_once()
    body = mock_send_apprise_message.call_args.kwargs["body"]
    assert "Cloud monitor run failed: monitor loop exploded" in body
    assert "monitor loop exploded" in body


@patch("crsbench.cloud.cli._monitor.send_apprise_message")
@patch("crsbench.cloud.cli._monitor.load_apprise_notification_config")
@patch("crsbench.cloud.cli._monitor.monitor_queue")
@patch("crsbench.cloud.cli._monitor.initialize_queue")
@patch("crsbench.cloud.cli._monitor.probe_redis_connection")
@patch("crsbench.cloud.cli._monitor.OrchestratorRedisTunnel.from_launch_state")
@patch("crsbench.cloud.cli._monitor.require_launch_state")
@patch("crsbench.cloud.cli._monitor.resolve_effective_experiment_name")
def test_run_monitor_does_not_send_failure_after_completion_notification(
    mock_resolve_experiment_name,
    mock_require_state,
    mock_tunnel_cls,
    mock_probe_redis_connection,
    mock_initialize_queue,
    mock_monitor_queue,
    mock_load_notification_config,
    mock_send_apprise_message,
):
    from crsbench.cloud.cli._monitor import run_monitor

    context = _make_provider_neutral_operational_context(include_launch_state=True)
    assert context.launch_state is not None
    mock_resolve_experiment_name.return_value = "test-exp"
    mock_require_state.return_value = context
    mock_tunnel = MagicMock()
    mock_tunnel.redis_host = "127.0.0.1:16379"
    mock_tunnel_cls.return_value.__enter__.return_value = mock_tunnel
    mock_probe_redis_connection.return_value = (RedisConnectionProbe.READY, None)
    mock_queue = MagicMock()
    mock_queue.deferred_job_registry.get_job_ids.return_value = []
    mock_queue.scheduled_job_registry.get_job_ids.return_value = []
    mock_initialize_queue.return_value = mock_queue
    mock_load_notification_config.return_value = MagicMock()

    def _monitor_side_effect(*args, **kwargs):
        del args
        callbacks = kwargs["callbacks"]
        callbacks.on_snapshot(
            SimpleNamespace(
                stats={"queued": 1, "started": 0, "finished": 0, "failed": 0}
            )
        )
        callbacks.on_snapshot(
            SimpleNamespace(
                stats={"queued": 0, "started": 0, "finished": 1, "failed": 0}
            )
        )
        raise RuntimeError("post-completion disconnect")

    mock_monitor_queue.side_effect = _monitor_side_effect

    rc = run_monitor(_make_monitor_args())

    assert rc == 1
    mock_send_apprise_message.assert_called_once()
    body = mock_send_apprise_message.call_args.kwargs["body"]
    assert "Cloud monitor run completed" in body


@patch("crsbench.cloud.cli._monitor.send_apprise_message")
@patch("crsbench.cloud.cli._monitor.load_apprise_notification_config")
@patch("crsbench.cloud.cli._monitor.monitor_queue")
@patch("crsbench.cloud.cli._monitor.initialize_queue")
@patch("crsbench.cloud.cli._monitor.probe_redis_connection")
@patch("crsbench.cloud.cli._monitor.OrchestratorRedisTunnel.from_launch_state")
@patch("crsbench.cloud.cli._monitor.require_launch_state")
@patch("crsbench.cloud.cli._monitor.resolve_effective_experiment_name")
def test_run_monitor_sends_failure_notification_when_monitor_fails_before_snapshot(
    mock_resolve_experiment_name,
    mock_require_state,
    mock_tunnel_cls,
    mock_probe_redis_connection,
    mock_initialize_queue,
    mock_monitor_queue,
    mock_load_notification_config,
    mock_send_apprise_message,
):
    from crsbench.cloud.cli._monitor import run_monitor

    context = _make_provider_neutral_operational_context(include_launch_state=True)
    assert context.launch_state is not None
    mock_resolve_experiment_name.return_value = "test-exp"
    mock_require_state.return_value = context
    mock_tunnel = MagicMock()
    mock_tunnel.redis_host = "127.0.0.1:16379"
    mock_tunnel_cls.return_value.__enter__.return_value = mock_tunnel
    mock_probe_redis_connection.return_value = (RedisConnectionProbe.READY, None)
    mock_initialize_queue.return_value = MagicMock()
    mock_load_notification_config.return_value = MagicMock()
    mock_monitor_queue.side_effect = RuntimeError("poller crashed before snapshot")

    rc = run_monitor(_make_monitor_args())

    assert rc == 1
    mock_load_notification_config.assert_called_once_with()
    mock_send_apprise_message.assert_called_once()
    body = mock_send_apprise_message.call_args.kwargs["body"]
    assert "Cloud monitor run failed: poller crashed before snapshot" in body
    assert "poller crashed before snapshot" in body


@patch("crsbench.cloud.cli._monitor.send_apprise_message")
@patch("crsbench.cloud.cli._monitor.load_apprise_notification_config")
@patch("crsbench.cloud.cli._monitor.initialize_queue", return_value=None)
@patch("crsbench.cloud.cli._monitor.probe_redis_connection")
@patch("crsbench.cloud.cli._monitor.OrchestratorRedisTunnel.from_launch_state")
@patch("crsbench.cloud.cli._monitor.require_launch_state")
@patch("crsbench.cloud.cli._monitor.resolve_effective_experiment_name")
def test_run_monitor_does_not_notify_on_pre_monitor_queue_initialization_failure(
    mock_resolve_experiment_name,
    mock_require_state,
    mock_tunnel_cls,
    mock_probe_redis_connection,
    mock_initialize_queue,
    mock_load_notification_config,
    mock_send_apprise_message,
):
    from crsbench.cloud.cli._monitor import run_monitor

    del mock_initialize_queue

    context = _make_provider_neutral_operational_context(include_launch_state=True)
    assert context.launch_state is not None
    mock_resolve_experiment_name.return_value = "test-exp"
    mock_require_state.return_value = context
    mock_tunnel = MagicMock()
    mock_tunnel.redis_host = "127.0.0.1:16379"
    mock_tunnel_cls.return_value.__enter__.return_value = mock_tunnel
    mock_probe_redis_connection.return_value = (RedisConnectionProbe.READY, None)

    rc = run_monitor(_make_monitor_args())

    assert rc == 1
    mock_load_notification_config.assert_not_called()
    mock_send_apprise_message.assert_not_called()


def test_find_launch_target_conflicts_reports_saved_state_and_live_instances():
    from crsbench.cloud.launch_checks import find_launch_target_conflicts

    config_path = Path("/tmp/config.yaml")
    adapter = MagicMock()
    adapter.list_orchestrators.return_value = [
        _make_gce_worker("gce-orchestrator-test-exp", ip="10.0.0.50")
    ]
    adapter.list_workers.return_value = [
        _make_gce_worker("worker-b"),
        _make_gce_worker("worker-a"),
    ]
    adapter.list_evaluators.return_value = [
        _make_gce_worker("worker-a"),
    ]

    with patch(
        "crsbench.cloud.launch_checks.load_launch_state",
        return_value=_make_launch_state(),
    ):
        conflicts = find_launch_target_conflicts(
            config_path=config_path,
            experiment_name="test-exp",
            adapter=adapter,
            plan=MagicMock(),
        )

    assert conflicts == [
        "saved launch state exists at /tmp/.crsbench-cloud/test-exp.json",
        "live cloud instances already exist: gce-orchestrator-test-exp, worker-a, worker-b",
    ]


class TestLaunch:
    """Tests for run_launch() orchestration."""

    @patch("crsbench.cloud.launch_checks.load_launch_state")
    @patch("crsbench.cloud.cli._launch.prepare_launch_inputs")
    @patch("crsbench.cloud.cli._launch.provider_adapter_for_launch_plan")
    @patch("crsbench.cloud.cli._launch.QuotaValidator")
    @patch("crsbench.cloud.cli._launch.build_cloud_launch_plan")
    @patch("crsbench.cloud.cli._launch.load_experiment_config")
    def test_launch_rejects_existing_saved_launch_state(
        self,
        mock_load,
        mock_build_plan,
        mock_validator_cls,
        mock_adapter_cls,
        mock_preflight,
        mock_load_state,
    ):
        """Launch should fail fast when the experiment already has persisted launch state."""
        mock_load.return_value = _make_launch_config()
        launch_plan = MagicMock(experiment_name="test-exp")
        resolved_plan = MagicMock(experiment_name="test-exp")
        mock_build_plan.return_value = launch_plan
        mock_validator_cls.return_value.validate.return_value = None
        mock_preflight.return_value = MagicMock(
            resolved_plan=resolved_plan,
            redacted_worker_fleets=[_make_launch_state().worker_fleet_configs[0]],
            redacted_evaluator_fleets=[],
            orchestrator_env={},
            worker_placement_envs=[],
            evaluator_placement_envs=[],
        )
        mock_load_state.return_value = _make_launch_state()
        mock_adapter = MagicMock()
        mock_adapter.build_orchestrator_config.return_value.project = "test-project"
        mock_adapter.list_orchestrators.return_value = []
        mock_adapter.list_workers.return_value = []
        mock_adapter.list_evaluators.return_value = []
        mock_adapter_cls.return_value = mock_adapter

        from crsbench.cloud.cli._launch import run_launch

        rc = run_launch(_make_launch_args())

        assert rc == 1
        mock_load_state.assert_called_once_with(Path("/tmp/config.yaml"), "test-exp")
        mock_adapter.create_orchestrator.assert_not_called()
        mock_adapter.create_workers.assert_not_called()
        mock_adapter.create_evaluators.assert_not_called()

    @patch("crsbench.cloud.launch_checks.load_launch_state", return_value=None)
    @patch("crsbench.cloud.cli._launch.prepare_launch_inputs")
    @patch("crsbench.cloud.cli._launch.provider_adapter_for_launch_plan")
    @patch("crsbench.cloud.cli._launch.QuotaValidator")
    @patch("crsbench.cloud.cli._launch.build_cloud_launch_plan")
    @patch("crsbench.cloud.cli._launch.load_experiment_config")
    def test_launch_rejects_existing_live_instances(
        self,
        mock_load,
        mock_build_plan,
        mock_validator_cls,
        mock_adapter_cls,
        mock_preflight,
        mock_load_state,
    ):
        """Launch should fail fast when matching live instances already exist."""
        del mock_load_state
        mock_load.return_value = _make_launch_config()
        launch_plan = MagicMock(experiment_name="test-exp")
        resolved_plan = MagicMock(experiment_name="test-exp")
        mock_build_plan.return_value = launch_plan
        mock_validator_cls.return_value.validate.return_value = None
        mock_preflight.return_value = MagicMock(
            resolved_plan=resolved_plan,
            redacted_worker_fleets=[_make_launch_state().worker_fleet_configs[0]],
            redacted_evaluator_fleets=[],
            orchestrator_env={},
            worker_placement_envs=[],
            evaluator_placement_envs=[],
        )
        mock_adapter = MagicMock()
        mock_adapter.build_orchestrator_config.return_value.project = "test-project"
        mock_adapter.list_orchestrators.return_value = [
            _make_gce_worker("gce-orchestrator-test-exp", ip="10.0.0.50")
        ]
        mock_adapter.list_workers.return_value = []
        mock_adapter.list_evaluators.return_value = []
        mock_adapter_cls.return_value = mock_adapter

        from crsbench.cloud.cli._launch import run_launch

        rc = run_launch(_make_launch_args())

        assert rc == 1
        mock_adapter.list_orchestrators.assert_called_once_with(plan=resolved_plan)
        mock_adapter.create_orchestrator.assert_not_called()
        mock_adapter.create_workers.assert_not_called()
        mock_adapter.create_evaluators.assert_not_called()

    @patch(
        "crsbench.cloud.cli._launch.secrets.token_urlsafe", return_value="shared-secret"
    )
    @patch("crsbench.cloud.cli._launch.append_created_instance_records")
    @patch("crsbench.cloud.cli._launch.save_launch_state")
    @patch("crsbench.cloud.cli._launch.prepare_launch_inputs")
    @patch("crsbench.cloud.cli._launch.provider_adapter_for_launch_plan")
    @patch("crsbench.cloud.cli._launch.QuotaValidator")
    @patch("crsbench.cloud.cli._launch.build_cloud_launch_plan")
    @patch("crsbench.cloud.cli._launch.load_experiment_config")
    def test_launch_provisions_orchestrator_before_workers(
        self,
        mock_load,
        mock_build_plan,
        mock_validator_cls,
        mock_adapter_cls,
        mock_preflight,
        mock_save_state,
        mock_append_instances,
        mock_secret,
    ):
        del mock_secret
        mock_load.return_value = _make_launch_config()
        launch_plan = MagicMock(experiment_name="test-exp")
        resolved_plan = MagicMock(experiment_name="test-exp")
        mock_build_plan.return_value = launch_plan
        mock_validator_cls.return_value.validate.return_value = None
        mock_adapter = MagicMock()
        call_order: list[str] = []
        redacted_fleet = _make_launch_state().worker_fleet_configs[0]
        mock_preflight.return_value = MagicMock(
            resolved_plan=resolved_plan,
            redacted_worker_fleets=[redacted_fleet],
            redacted_evaluator_fleets=[],
            orchestrator_env={},
            worker_placement_envs=[],
            evaluator_placement_envs=[],
        )

        orchestrator_record = _make_gce_worker(
            "gce-orchestrator-test-exp", ip="10.0.0.50"
        )

        def _create_orchestrator(**kwargs):
            call_order.append("orchestrator")
            assert kwargs["redis_password"] == "shared-secret"
            assert kwargs["experiment_config_path"] == "/tmp/config.yaml"
            assert kwargs["plan"] is resolved_plan
            assert kwargs["env_passthrough"] == {}
            return orchestrator_record

        def _create_workers(**kwargs):
            call_order.append("workers")
            assert kwargs["redis_host"] == "10.0.0.50:6379"
            assert kwargs["redis_password"] == "shared-secret"
            assert kwargs["plan"] is resolved_plan
            assert kwargs["env_passthrough_by_placement"] == []
            return [_make_gce_worker("w-1")]

        mock_adapter.build_orchestrator_config.return_value.project = "test-project"
        mock_adapter.build_orchestrator_config.return_value.ssh_via_iap = True
        mock_adapter.create_orchestrator.side_effect = _create_orchestrator
        mock_adapter.create_workers.side_effect = _create_workers
        mock_adapter.create_evaluators.return_value = []
        mock_adapter_cls.return_value = mock_adapter

        from crsbench.cloud.cli._launch import run_launch

        rc = run_launch(_make_launch_args())

        assert rc == 0
        assert call_order == ["orchestrator", "workers"]
        assert mock_append_instances.call_count == 2
        assert mock_append_instances.call_args_list[0].args[0] == Path(
            "/tmp/config.yaml"
        )
        assert (
            mock_append_instances.call_args_list[0].kwargs["experiment_name"]
            == "test-exp"
        )
        assert [
            record.instance_name
            for record in mock_append_instances.call_args_list[0].kwargs["records"]
        ] == [
            "gce-orchestrator-test-exp",
        ]
        assert [
            record.instance_name
            for record in mock_append_instances.call_args_list[1].kwargs["records"]
        ] == [
            "w-1",
        ]
        mock_save_state.assert_called_once()
        assert mock_save_state.call_args.args[0] == Path("/tmp/config.yaml")
        saved_state = mock_save_state.call_args.args[1]
        assert saved_state.worker_fleet_configs == [redacted_fleet]

    @patch(
        "crsbench.cloud.cli._launch.secrets.token_urlsafe", return_value="shared-secret"
    )
    @patch("crsbench.cloud.cli._launch.append_created_instance_records")
    @patch("crsbench.cloud.cli._launch.save_launch_state")
    @patch("crsbench.cloud.cli._launch.prepare_launch_inputs")
    @patch("crsbench.cloud.cli._launch.provider_adapter_for_launch_plan")
    @patch("crsbench.cloud.cli._launch.QuotaValidator")
    @patch("crsbench.cloud.cli._launch.build_cloud_launch_plan")
    @patch("crsbench.cloud.cli._launch.load_experiment_config")
    def test_launch_best_effort_workers_skips_quota_and_uses_partial_worker_create(
        self,
        mock_load,
        mock_build_plan,
        mock_validator_cls,
        mock_adapter_cls,
        mock_preflight,
        mock_save_state,
        mock_append_instances,
        mock_secret,
    ):
        del mock_secret
        mock_load.return_value = _make_launch_config()
        launch_plan = MagicMock(experiment_name="test-exp")
        resolved_plan = MagicMock(experiment_name="test-exp")
        mock_build_plan.return_value = launch_plan
        redacted_fleet = _make_launch_state().worker_fleet_configs[0]
        mock_preflight.return_value = MagicMock(
            resolved_plan=resolved_plan,
            redacted_worker_fleets=[redacted_fleet],
            redacted_evaluator_fleets=[],
            orchestrator_env={},
            worker_placement_envs=[],
            evaluator_placement_envs=[],
        )

        orchestrator_record = _make_gce_worker(
            "gce-orchestrator-test-exp", ip="10.0.0.50"
        )
        worker_record = _make_gce_worker("w-1")
        mock_adapter = MagicMock()
        mock_adapter.build_orchestrator_config.return_value.project = "test-project"
        mock_adapter.build_orchestrator_config.return_value.ssh_via_iap = True
        mock_adapter.create_orchestrator.return_value = orchestrator_record
        mock_adapter.create_workers_best_effort.return_value = [worker_record]
        mock_adapter.create_evaluators.return_value = []
        mock_adapter_cls.return_value = mock_adapter

        from crsbench.cloud.cli._launch import run_launch

        rc = run_launch(_make_launch_args(best_effort_workers=True))

        assert rc == 0
        mock_validator_cls.assert_not_called()
        mock_adapter.create_workers.assert_not_called()
        mock_adapter.create_workers_best_effort.assert_called_once_with(
            plan=resolved_plan,
            redis_host="10.0.0.50:6379",
            redis_password="shared-secret",
            registration=mock.ANY,
            bootstrap_inputs=mock.ANY,
            env_passthrough_by_placement=[],
        )
        assert mock_adapter.create_orchestrator.call_args.kwargs["env_passthrough"] == {
            "CRSBENCH_CLOUD_BEST_EFFORT_WORKERS": "1"
        }
        mock_adapter.mark_best_effort_workers_complete.assert_called_once_with(
            plan=resolved_plan,
            orchestrator=orchestrator_record,
        )
        assert mock_append_instances.call_count == 2
        mock_save_state.assert_called_once()

    @patch(
        "crsbench.cloud.cli._launch.secrets.token_urlsafe", return_value="shared-secret"
    )
    @patch("crsbench.cloud.cli._launch.provisioner_for_provider")
    @patch("crsbench.cloud.cli._launch.append_created_instance_records")
    @patch(
        "crsbench.cloud.cli._launch.save_launch_state",
        side_effect=RuntimeError("disk full"),
    )
    @patch("crsbench.cloud.cli._launch.prepare_launch_inputs")
    @patch("crsbench.cloud.cli._launch.provider_adapter_for_launch_plan")
    @patch("crsbench.cloud.cli._launch.QuotaValidator")
    @patch("crsbench.cloud.cli._launch.build_cloud_launch_plan")
    @patch("crsbench.cloud.cli._launch.logger")
    @patch("crsbench.cloud.cli._launch.load_experiment_config")
    def test_launch_rolls_back_workers_by_actual_created_zone(
        self,
        mock_load,
        mock_logger,
        mock_build_plan,
        mock_validator_cls,
        mock_adapter_cls,
        mock_preflight,
        mock_save_state,
        mock_append_instances,
        mock_provisioner_cls,
        mock_secret,
    ):
        del mock_save_state, mock_secret
        mock_load.return_value = _make_launch_config()
        resolved_plan = MagicMock(experiment_name="test-exp")
        mock_build_plan.return_value = MagicMock(experiment_name="test-exp")
        mock_validator_cls.return_value.validate.return_value = None
        mock_adapter = mock_adapter_cls.return_value
        redacted_fleet = (
            _make_launch_state()
            .worker_fleet_configs[0]
            .model_copy(update={"zone": "us-east5-b"})
        )
        mock_preflight.return_value = MagicMock(
            resolved_plan=resolved_plan,
            redacted_worker_fleets=[redacted_fleet],
            redacted_evaluator_fleets=[],
            orchestrator_env={},
            worker_placement_envs=[],
            evaluator_placement_envs=[],
        )
        mock_adapter.build_orchestrator_config.return_value.project = "test-project"
        mock_adapter.build_orchestrator_config.return_value.ssh_via_iap = True
        mock_adapter.create_orchestrator.return_value = _make_gce_worker(
            "gce-orchestrator-test-exp", zone="us-east5-b", ip="10.0.0.50"
        )
        mock_adapter.create_workers.return_value = [
            _make_gce_worker("w-1", zone="us-east1-b")
        ]
        mock_adapter.create_evaluators.return_value = []

        from crsbench.cloud.cli._launch import run_launch

        rc = run_launch(_make_launch_args())

        assert rc == 1
        mock_adapter.delete_workers.assert_not_called()
        mock_provisioner_cls.return_value.delete_instance.assert_any_call(
            project="test-project",
            zone="us-east1-b",
            instance_name="w-1",
        )
        mock_logger.error.assert_called_once_with(
            "Cloud launch failed: {}", "disk full"
        )

    @patch(
        "crsbench.cloud.cli._launch.secrets.token_urlsafe", return_value="shared-secret"
    )
    @patch("crsbench.cloud.cli._launch.provisioner_for_provider")
    @patch("crsbench.cloud.cli._launch.append_created_instance_records")
    @patch(
        "crsbench.cloud.cli._launch.save_launch_state",
        side_effect=RuntimeError("disk full"),
    )
    @patch("crsbench.cloud.cli._launch.prepare_launch_inputs")
    @patch("crsbench.cloud.cli._launch.provider_adapter_for_launch_plan")
    @patch("crsbench.cloud.cli._launch.QuotaValidator")
    @patch("crsbench.cloud.cli._launch.build_cloud_launch_plan")
    @patch("crsbench.cloud.cli._launch.logger")
    @patch("crsbench.cloud.cli._launch.load_experiment_config")
    def test_launch_rolls_back_workers_when_state_persist_fails(
        self,
        mock_load,
        mock_logger,
        mock_build_plan,
        mock_validator_cls,
        mock_adapter_cls,
        mock_preflight,
        mock_save_state,
        mock_append_instances,
        mock_provisioner_cls,
        mock_secret,
    ):
        del mock_save_state, mock_secret
        mock_load.return_value = _make_launch_config()
        resolved_plan = MagicMock(experiment_name="test-exp")
        mock_build_plan.return_value = MagicMock(experiment_name="test-exp")
        mock_validator_cls.return_value.validate.return_value = None
        mock_adapter = mock_adapter_cls.return_value
        redacted_fleet = _make_launch_state().worker_fleet_configs[0]
        mock_preflight.return_value = MagicMock(
            resolved_plan=resolved_plan,
            redacted_worker_fleets=[redacted_fleet],
            redacted_evaluator_fleets=[],
            orchestrator_env={},
            worker_placement_envs=[],
            evaluator_placement_envs=[],
        )
        mock_adapter.build_orchestrator_config.return_value.project = "test-project"
        mock_adapter.build_orchestrator_config.return_value.ssh_via_iap = True
        mock_adapter.create_orchestrator.return_value = _make_gce_worker(
            "gce-orchestrator-test-exp", ip="10.0.0.50"
        )
        mock_adapter.create_workers.return_value = [_make_gce_worker("w-1")]
        mock_adapter.create_evaluators.return_value = []

        from crsbench.cloud.cli._launch import run_launch

        rc = run_launch(_make_launch_args())

        assert rc == 1
        assert mock_append_instances.call_count == 2
        assert [
            record.instance_name
            for record in mock_append_instances.call_args_list[0].kwargs["records"]
        ] == ["gce-orchestrator-test-exp"]
        assert [
            record.instance_name
            for record in mock_append_instances.call_args_list[1].kwargs["records"]
        ] == ["w-1"]
        mock_adapter.delete_workers.assert_not_called()
        mock_provisioner_cls.return_value.delete_instance.assert_any_call(
            project="test-project",
            zone="us-central1-a",
            instance_name="w-1",
        )
        mock_logger.error.assert_called_once_with(
            "Cloud launch failed: {}", "disk full"
        )

    @patch(
        "crsbench.cloud.cli._launch.secrets.token_urlsafe", return_value="shared-secret"
    )
    @patch("crsbench.cloud.cli._launch.append_created_instance_records")
    @patch("crsbench.cloud.cli._launch.prepare_launch_inputs")
    @patch("crsbench.cloud.cli._launch.provider_adapter_for_launch_plan")
    @patch("crsbench.cloud.cli._launch.QuotaValidator")
    @patch("crsbench.cloud.cli._launch.build_cloud_launch_plan")
    @patch("crsbench.cloud.cli._launch.logger")
    @patch("crsbench.cloud.cli._launch.load_experiment_config")
    def test_launch_records_orchestrator_before_worker_provisioning_failure(
        self,
        mock_load,
        mock_logger,
        mock_build_plan,
        mock_validator_cls,
        mock_adapter_cls,
        mock_preflight,
        mock_append_instances,
        mock_secret,
    ):
        del mock_secret
        mock_load.return_value = _make_launch_config()
        resolved_plan = MagicMock(experiment_name="test-exp")
        mock_build_plan.return_value = MagicMock(experiment_name="test-exp")
        mock_validator_cls.return_value.validate.return_value = None
        mock_adapter = mock_adapter_cls.return_value
        redacted_fleet = _make_launch_state().worker_fleet_configs[0]
        mock_preflight.return_value = MagicMock(
            resolved_plan=resolved_plan,
            redacted_worker_fleets=[redacted_fleet],
            redacted_evaluator_fleets=[],
            orchestrator_env={},
            worker_placement_envs=[],
            evaluator_placement_envs=[],
        )
        mock_adapter.build_orchestrator_config.return_value.project = "test-project"
        mock_adapter.build_orchestrator_config.return_value.ssh_via_iap = True
        mock_adapter.create_orchestrator.return_value = _make_gce_worker(
            "gce-orchestrator-test-exp", ip="10.0.0.50"
        )
        mock_adapter.create_workers.side_effect = RuntimeError("worker boom")
        mock_adapter.create_evaluators.return_value = []

        from crsbench.cloud.cli._launch import run_launch

        rc = run_launch(_make_launch_args())

        assert rc == 1
        mock_append_instances.assert_called_once()
        assert [
            record.instance_name
            for record in mock_append_instances.call_args.kwargs["records"]
        ] == ["gce-orchestrator-test-exp"]
        mock_adapter.delete_workers.assert_not_called()
        mock_logger.error.assert_called_once_with(
            "Cloud launch failed: {}", "worker boom"
        )

    @patch(
        "crsbench.cloud.cli._launch.secrets.token_urlsafe", return_value="shared-secret"
    )
    @patch("crsbench.cloud.cli._launch.append_created_instance_records")
    @patch("crsbench.cloud.cli._launch.prepare_launch_inputs")
    @patch("crsbench.cloud.cli._launch.provider_adapter_for_launch_plan")
    @patch("crsbench.cloud.cli._launch.QuotaValidator")
    @patch("crsbench.cloud.cli._launch.build_cloud_launch_plan")
    @patch("crsbench.cloud.cli._launch.logger")
    @patch("crsbench.cloud.cli._launch.load_experiment_config")
    def test_launch_records_workers_before_evaluator_provisioning_failure(
        self,
        mock_load,
        mock_logger,
        mock_build_plan,
        mock_validator_cls,
        mock_adapter_cls,
        mock_preflight,
        mock_append_instances,
        mock_secret,
    ):
        del mock_secret
        mock_load.return_value = (
            _make_provider_neutral_experiment_config_with_evaluators()
        )
        resolved_plan = MagicMock(experiment_name="test-exp")
        mock_build_plan.return_value = MagicMock(experiment_name="test-exp")
        mock_validator_cls.return_value.validate.return_value = None
        mock_adapter = mock_adapter_cls.return_value
        expected_worker_fleets = (
            _make_provider_neutral_launch_state().worker_fleet_configs
        )
        expected_evaluator_fleets = [
            _make_stable_evaluator_fleet(
                zone="us-east1-b",
                prefix="evaluator-test-exp-us-east1-b",
            )
        ]
        mock_preflight.return_value = MagicMock(
            resolved_plan=resolved_plan,
            redacted_worker_fleets=expected_worker_fleets,
            redacted_evaluator_fleets=expected_evaluator_fleets,
            orchestrator_env={},
            worker_placement_envs=[],
            evaluator_placement_envs=[],
        )
        mock_adapter.build_orchestrator_config.return_value.project = "test-project"
        mock_adapter.build_orchestrator_config.return_value.ssh_via_iap = True
        mock_adapter.create_orchestrator.return_value = _make_gce_worker(
            "gce-orchestrator-test-exp", ip="10.0.0.50"
        )
        mock_adapter.create_workers.return_value = [_make_gce_worker("w-1")]
        mock_adapter.create_evaluators.side_effect = RuntimeError("evaluator boom")

        from crsbench.cloud.cli._launch import run_launch

        rc = run_launch(_make_launch_args())

        assert rc == 1
        assert mock_append_instances.call_count == 2
        assert [
            record.instance_name
            for record in mock_append_instances.call_args_list[0].kwargs["records"]
        ] == ["gce-orchestrator-test-exp"]
        assert [
            record.instance_name
            for record in mock_append_instances.call_args_list[1].kwargs["records"]
        ] == ["w-1"]
        mock_logger.error.assert_called_once_with(
            "Cloud launch failed: {}", "evaluator boom"
        )

    @patch(
        "crsbench.cloud.cli._launch.secrets.token_urlsafe", return_value="shared-secret"
    )
    @patch("crsbench.cloud.cli._launch.save_launch_state")
    @patch("crsbench.cloud.cli._launch.prepare_launch_inputs")
    @patch("crsbench.cloud.cli._launch.provider_adapter_for_launch_plan")
    @patch("crsbench.cloud.cli._launch.QuotaValidator")
    @patch("crsbench.cloud.cli._launch.build_cloud_launch_plan")
    @patch("crsbench.cloud.cli._launch.load_experiment_config")
    def test_provider_neutral_launch_validates_quota_before_provisioning(
        self,
        mock_load,
        mock_build_plan,
        mock_validator_cls,
        mock_adapter_cls,
        mock_preflight,
        mock_save_state,
        mock_secret,
    ):
        del mock_secret
        config = _make_provider_neutral_experiment_config()
        config.cloud.bootstrap = CloudBootstrapConfig(
            prepare_mode="skip_base_images",
            download_benchmarks="always",
        )
        mock_load.return_value = config

        launch_plan = MagicMock()
        launch_plan.experiment_name = "test-exp"
        mock_build_plan.return_value = launch_plan
        resolved_plan = MagicMock()
        resolved_plan.experiment_name = "test-exp"

        call_order: list[str] = []
        mock_preflight.side_effect = lambda **_kwargs: (
            call_order.append("preflight")
            or MagicMock(
                resolved_plan=resolved_plan,
                redacted_worker_fleets=(
                    _make_provider_neutral_launch_state().worker_fleet_configs
                ),
                orchestrator_env={},
                worker_placement_envs=[],
            )
        )
        mock_validator = mock_validator_cls.return_value
        mock_validator.validate.side_effect = lambda plan: call_order.append(
            f"validate:{plan.experiment_name}"
        )

        mock_adapter = mock_adapter_cls.return_value
        expected_worker_fleets = (
            _make_provider_neutral_launch_state().worker_fleet_configs
        )
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

        def _create_workers(**kwargs):
            bootstrap_inputs = kwargs["bootstrap_inputs"]
            assert bootstrap_inputs.prepare_mode == "skip_base_images"
            assert bootstrap_inputs.download_benchmarks == "always"
            assert bootstrap_inputs.benchmark_suite == "sanity"
            assert kwargs["env_passthrough_by_placement"] == []
            call_order.append("create-workers")
            return [_make_gce_worker("worker-east5"), _make_gce_worker("worker-east1")]

        mock_adapter.create_workers.side_effect = _create_workers

        from crsbench.cloud.cli._launch import run_launch

        rc = run_launch(_make_launch_args())

        assert rc == 0
        assert call_order == [
            "preflight",
            "validate:test-exp",
            "create-orchestrator",
            "create-workers",
        ]
        mock_adapter.create_orchestrator.assert_called_once_with(
            plan=resolved_plan,
            experiment_config_path="/tmp/config.yaml",
            redis_password="shared-secret",
            env_passthrough={},
            from_experiment_remote_path=None,
            from_experiment_remote_by_crs=None,
        )
        mock_adapter.create_workers.assert_called_once()
        assert mock_adapter.create_workers.call_args.kwargs["plan"] is resolved_plan

    @patch(
        "crsbench.cloud.cli._launch.secrets.token_urlsafe", return_value="shared-secret"
    )
    @patch("crsbench.cloud.cli._launch.save_launch_state")
    @patch("crsbench.cloud.cli._launch.prepare_launch_inputs")
    @patch("crsbench.cloud.cli._launch.provider_adapter_for_launch_plan")
    @patch("crsbench.cloud.cli._launch.QuotaValidator")
    @patch("crsbench.cloud.cli._launch.build_cloud_launch_plan")
    @patch("crsbench.cloud.cli._launch.load_experiment_config")
    def test_provider_neutral_launch_passes_layered_env_payloads(
        self,
        mock_load,
        mock_build_plan,
        mock_validator_cls,
        mock_adapter_cls,
        mock_preflight,
        mock_save_state,
        mock_secret,
    ):
        del mock_secret
        config = _make_provider_neutral_experiment_config()
        mock_load.return_value = config

        launch_plan = MagicMock()
        launch_plan.experiment_name = "test-exp"
        resolved_plan = MagicMock()
        resolved_plan.experiment_name = "test-exp"
        expected_worker_fleets = (
            _make_provider_neutral_launch_state().worker_fleet_configs
        )
        mock_build_plan.return_value = launch_plan
        mock_preflight.return_value = MagicMock(
            resolved_plan=resolved_plan,
            redacted_worker_fleets=expected_worker_fleets,
            orchestrator_env={
                "CRSBENCH_LLM_UPSTREAM_BASE_URL": "https://llm.example.test",
                "CRSBENCH_LLM_MASTER_KEY": "master-key",
            },
            worker_placement_envs=[
                {
                    "CRSBENCH_LLM_UPSTREAM_BASE_URL": "https://llm.example.test",
                    "OPENAI_API_KEY": "openai-key",
                },
                {
                    "CRSBENCH_LLM_UPSTREAM_BASE_URL": "https://llm.example.test",
                    "OPENAI_API_KEY": "openai-key",
                },
            ],
        )
        mock_validator_cls.return_value.validate.return_value = None

        mock_adapter = mock_adapter_cls.return_value
        mock_adapter.build_orchestrator_config.return_value.project = "test-project"
        mock_adapter.build_orchestrator_config.return_value.ssh_via_iap = True
        mock_adapter.create_orchestrator.return_value = _make_gce_worker(
            "gce-orchestrator-test-exp",
            zone="us-east5-b",
            ip="10.0.0.50",
        )
        mock_adapter.create_workers.return_value = [_make_gce_worker("worker-east5")]

        from crsbench.cloud.cli._launch import run_launch

        rc = run_launch(_make_launch_args())

        assert rc == 0
        mock_preflight.assert_called_once_with(
            plan=launch_plan,
            cwd=Path.cwd(),
        )
        assert mock_adapter.create_orchestrator.call_args.kwargs["env_passthrough"] == {
            "CRSBENCH_LLM_UPSTREAM_BASE_URL": "https://llm.example.test",
            "CRSBENCH_LLM_MASTER_KEY": "master-key",
        }
        assert mock_adapter.create_workers.call_args.kwargs[
            "env_passthrough_by_placement"
        ] == [
            {
                "CRSBENCH_LLM_UPSTREAM_BASE_URL": "https://llm.example.test",
                "OPENAI_API_KEY": "openai-key",
            },
            {
                "CRSBENCH_LLM_UPSTREAM_BASE_URL": "https://llm.example.test",
                "OPENAI_API_KEY": "openai-key",
            },
        ]
        mock_save_state.assert_called_once()
        saved_state = mock_save_state.call_args.args[1]
        assert saved_state.worker_fleet_configs == expected_worker_fleets

    @patch(
        "crsbench.cloud.cli._launch.secrets.token_urlsafe", return_value="shared-secret"
    )
    @patch("crsbench.cloud.cli._launch.save_launch_state")
    @patch(
        "crsbench.cloud.cli._launch.load_trial_key_file",
        return_value=["trial-key-2", "trial-key-1"],
    )
    @patch("crsbench.cloud.cli._launch.prepare_launch_inputs")
    @patch("crsbench.cloud.cli._launch.provider_adapter_for_launch_plan")
    @patch("crsbench.cloud.cli._launch.QuotaValidator")
    @patch("crsbench.cloud.cli._launch.build_cloud_launch_plan")
    @patch("crsbench.cloud.cli._launch.load_experiment_config")
    def test_provider_neutral_launch_passes_selector_file_allowlist_env(
        self,
        mock_load,
        mock_build_plan,
        mock_validator_cls,
        mock_adapter_cls,
        mock_preflight,
        mock_load_trial_key_file,
        mock_save_state,
        mock_secret,
    ):
        del mock_save_state, mock_secret
        config = _make_provider_neutral_experiment_config()
        mock_load.return_value = config

        launch_plan = MagicMock(experiment_name="test-exp")
        resolved_plan = MagicMock(experiment_name="test-exp")
        expected_worker_fleets = (
            _make_provider_neutral_launch_state().worker_fleet_configs
        )
        mock_build_plan.return_value = launch_plan
        mock_preflight.return_value = MagicMock(
            resolved_plan=resolved_plan,
            redacted_worker_fleets=expected_worker_fleets,
            redacted_evaluator_fleets=[],
            orchestrator_env={
                "CRSBENCH_LLM_UPSTREAM_BASE_URL": "https://llm.example.test",
            },
            worker_placement_envs=[],
            evaluator_placement_envs=[],
        )
        mock_validator_cls.return_value.validate.return_value = None
        mock_adapter = mock_adapter_cls.return_value
        mock_adapter.build_orchestrator_config.return_value.project = "test-project"
        mock_adapter.build_orchestrator_config.return_value.ssh_via_iap = True
        mock_adapter.create_orchestrator.return_value = _make_gce_worker(
            "gce-orchestrator-test-exp", zone="us-east5-b", ip="10.0.0.50"
        )
        mock_adapter.create_workers.return_value = [_make_gce_worker("worker-east5")]
        mock_adapter.create_evaluators.return_value = []

        from crsbench.cloud.cli._launch import run_launch

        rc = run_launch(_make_launch_args(only_trial_keys_file="/tmp/trial-keys.txt"))

        assert rc == 0
        mock_load_trial_key_file.assert_called_once_with("/tmp/trial-keys.txt")
        assert mock_adapter.create_orchestrator.call_args.kwargs["env_passthrough"] == {
            "CRSBENCH_LLM_UPSTREAM_BASE_URL": "https://llm.example.test",
            TRIAL_KEY_ALLOWLIST_ENV_VAR: encode_trial_key_allowlist(
                ["trial-key-2", "trial-key-1"]
            ),
        }

    @patch(
        "crsbench.cloud.cli._launch.secrets.token_urlsafe", return_value="shared-secret"
    )
    @patch("crsbench.cloud.cli._launch.save_launch_state")
    @patch(
        "crsbench.cloud.cli._launch.derive_unfinished_trial_keys_from_config",
        return_value=SimpleNamespace(selected_keys=["trial-key-3", "trial-key-4"]),
    )
    @patch("crsbench.cloud.cli._launch.prepare_launch_inputs")
    @patch("crsbench.cloud.cli._launch.provider_adapter_for_launch_plan")
    @patch("crsbench.cloud.cli._launch.QuotaValidator")
    @patch("crsbench.cloud.cli._launch.build_cloud_launch_plan")
    @patch("crsbench.cloud.cli._launch.load_experiment_config")
    def test_provider_neutral_launch_passes_derived_selector_allowlist_env(
        self,
        mock_load,
        mock_build_plan,
        mock_validator_cls,
        mock_adapter_cls,
        mock_preflight,
        mock_derive_trial_keys,
        mock_save_state,
        mock_secret,
        tmp_path: Path,
    ):
        del mock_save_state, mock_secret
        config = _make_provider_neutral_experiment_config()
        mock_load.return_value = config
        collected_root = tmp_path / "collected"
        collected_root.mkdir(parents=True)

        launch_plan = MagicMock(experiment_name="test-exp")
        resolved_plan = MagicMock(experiment_name="test-exp")
        expected_worker_fleets = (
            _make_provider_neutral_launch_state().worker_fleet_configs
        )
        mock_build_plan.return_value = launch_plan
        mock_preflight.return_value = MagicMock(
            resolved_plan=resolved_plan,
            redacted_worker_fleets=expected_worker_fleets,
            redacted_evaluator_fleets=[],
            orchestrator_env={"CRSBENCH_LLM_MASTER_KEY": "master-key"},
            worker_placement_envs=[],
            evaluator_placement_envs=[],
        )
        mock_validator_cls.return_value.validate.return_value = None
        mock_adapter = mock_adapter_cls.return_value
        mock_adapter.build_orchestrator_config.return_value.project = "test-project"
        mock_adapter.build_orchestrator_config.return_value.ssh_via_iap = True
        mock_adapter.create_orchestrator.return_value = _make_gce_worker(
            "gce-orchestrator-test-exp", zone="us-east5-b", ip="10.0.0.50"
        )
        mock_adapter.create_workers.return_value = [_make_gce_worker("worker-east5")]
        mock_adapter.create_evaluators.return_value = []

        from crsbench.cloud.cli._launch import run_launch

        rc = run_launch(
            _make_launch_args(
                only_unfinished_from=str(collected_root),
                rerun_failed_trials=True,
            )
        )

        assert rc == 0
        mock_derive_trial_keys.assert_called_once_with(
            config,
            collected_root=str(collected_root),
            rerun_failed_trials=True,
        )
        assert mock_adapter.create_orchestrator.call_args.kwargs["env_passthrough"] == {
            "CRSBENCH_LLM_MASTER_KEY": "master-key",
            TRIAL_KEY_ALLOWLIST_ENV_VAR: encode_trial_key_allowlist(
                ["trial-key-3", "trial-key-4"]
            ),
        }

    @patch("crsbench.cloud.cli._launch.QuotaValidator")
    @patch("crsbench.cloud.cli._launch.prepare_launch_inputs")
    @patch("crsbench.cloud.cli._launch.build_cloud_launch_plan")
    @patch("crsbench.cloud.cli._launch.logger")
    @patch("crsbench.cloud.cli._launch.load_experiment_config")
    def test_launch_fails_fast_for_rerun_failed_trials_without_unfinished_source(
        self,
        mock_load,
        mock_logger,
        mock_build_plan,
        mock_preflight,
        mock_validator_cls,
    ):
        mock_load.return_value = _make_provider_neutral_experiment_config()

        from crsbench.cloud.cli._launch import run_launch

        rc = run_launch(_make_launch_args(rerun_failed_trials=True))

        assert rc == 1
        mock_build_plan.assert_not_called()
        mock_preflight.assert_not_called()
        mock_validator_cls.assert_not_called()
        mock_logger.error.assert_called_once_with(
            "Cloud launch failed: {}",
            "--rerun-failed-trials requires --only-unfinished-from",
        )

    @patch("crsbench.cloud.cli._launch.QuotaValidator")
    @patch("crsbench.cloud.cli._launch.prepare_launch_inputs")
    @patch("crsbench.cloud.cli._launch.build_cloud_launch_plan")
    @patch("crsbench.cloud.cli._launch.logger")
    @patch("crsbench.cloud.cli._launch.derive_unfinished_trial_keys_from_config")
    @patch("crsbench.cloud.cli._launch.load_experiment_config")
    def test_launch_fails_fast_for_missing_unfinished_source_path(
        self,
        mock_load,
        mock_derive_trial_keys,
        mock_logger,
        mock_build_plan,
        mock_preflight,
        mock_validator_cls,
    ):
        del mock_validator_cls
        mock_load.return_value = _make_provider_neutral_experiment_config()
        missing_path = "/tmp/does-not-exist-collected-root"

        from crsbench.cloud.cli._launch import run_launch

        rc = run_launch(_make_launch_args(only_unfinished_from=missing_path))

        assert rc == 1
        mock_derive_trial_keys.assert_not_called()
        mock_build_plan.assert_not_called()
        mock_preflight.assert_not_called()
        mock_logger.error.assert_called_once_with(
            "Cloud launch failed: {}",
            f"Collected root does not exist: {Path(missing_path)}",
        )

    @patch("crsbench.cloud.cli._launch.QuotaValidator")
    @patch("crsbench.cloud.cli._launch.prepare_launch_inputs")
    @patch("crsbench.cloud.cli._launch.build_cloud_launch_plan")
    @patch("crsbench.cloud.cli._launch.logger")
    @patch("crsbench.cloud.cli._launch.derive_unfinished_trial_keys_from_config")
    @patch("crsbench.cloud.cli._launch.load_experiment_config")
    def test_launch_fails_fast_for_unfinished_source_file_path(
        self,
        mock_load,
        mock_derive_trial_keys,
        mock_logger,
        mock_build_plan,
        mock_preflight,
        mock_validator_cls,
        tmp_path: Path,
    ):
        del mock_validator_cls
        mock_load.return_value = _make_provider_neutral_experiment_config()
        file_path = tmp_path / "collected-root.txt"
        file_path.write_text("not a directory", encoding="utf-8")

        from crsbench.cloud.cli._launch import run_launch

        rc = run_launch(_make_launch_args(only_unfinished_from=str(file_path)))

        assert rc == 1
        mock_derive_trial_keys.assert_not_called()
        mock_build_plan.assert_not_called()
        mock_preflight.assert_not_called()
        mock_logger.error.assert_called_once_with(
            "Cloud launch failed: {}",
            f"Collected root is not a directory: {file_path}",
        )

    @patch(
        "crsbench.cloud.cli._launch.secrets.token_urlsafe", return_value="shared-secret"
    )
    @patch("crsbench.cloud.cli._launch.save_launch_state")
    @patch("crsbench.cloud.cli._launch.load_trial_key_file", return_value=[])
    @patch("crsbench.cloud.cli._launch.prepare_launch_inputs")
    @patch("crsbench.cloud.cli._launch.provider_adapter_for_launch_plan")
    @patch("crsbench.cloud.cli._launch.QuotaValidator")
    @patch("crsbench.cloud.cli._launch.build_cloud_launch_plan")
    @patch("crsbench.cloud.cli._launch.load_experiment_config")
    def test_provider_neutral_launch_transports_empty_selector_allowlist_env(
        self,
        mock_load,
        mock_build_plan,
        mock_validator_cls,
        mock_adapter_cls,
        mock_preflight,
        mock_load_trial_key_file,
        mock_save_state,
        mock_secret,
    ):
        del mock_save_state, mock_secret
        config = _make_provider_neutral_experiment_config()
        mock_load.return_value = config

        launch_plan = MagicMock(experiment_name="test-exp")
        resolved_plan = MagicMock(experiment_name="test-exp")
        expected_worker_fleets = (
            _make_provider_neutral_launch_state().worker_fleet_configs
        )
        mock_build_plan.return_value = launch_plan
        mock_preflight.return_value = MagicMock(
            resolved_plan=resolved_plan,
            redacted_worker_fleets=expected_worker_fleets,
            redacted_evaluator_fleets=[],
            orchestrator_env={"CRSBENCH_LLM_MASTER_KEY": "master-key"},
            worker_placement_envs=[],
            evaluator_placement_envs=[],
        )
        mock_validator_cls.return_value.validate.return_value = None
        mock_adapter = mock_adapter_cls.return_value
        mock_adapter.build_orchestrator_config.return_value.project = "test-project"
        mock_adapter.build_orchestrator_config.return_value.ssh_via_iap = True
        mock_adapter.create_orchestrator.return_value = _make_gce_worker(
            "gce-orchestrator-test-exp", zone="us-east5-b", ip="10.0.0.50"
        )
        mock_adapter.create_workers.return_value = [_make_gce_worker("worker-east5")]
        mock_adapter.create_evaluators.return_value = []

        from crsbench.cloud.cli._launch import run_launch

        rc = run_launch(_make_launch_args(only_trial_keys_file="/tmp/trial-keys.txt"))

        assert rc == 0
        mock_load_trial_key_file.assert_called_once_with("/tmp/trial-keys.txt")
        assert mock_adapter.create_orchestrator.call_args.kwargs["env_passthrough"] == {
            "CRSBENCH_LLM_MASTER_KEY": "master-key",
            TRIAL_KEY_ALLOWLIST_ENV_VAR: encode_trial_key_allowlist([]),
        }

    @patch(
        "crsbench.cloud.cli._launch.secrets.token_urlsafe", return_value="shared-secret"
    )
    @patch("crsbench.cloud.cli._launch.save_launch_state")
    @patch("crsbench.cloud.cli._launch.prepare_launch_inputs")
    @patch("crsbench.cloud.cli._launch.provider_adapter_for_launch_plan")
    @patch("crsbench.cloud.cli._launch.QuotaValidator")
    @patch("crsbench.cloud.cli._launch.build_cloud_launch_plan")
    @patch("crsbench.cloud.cli._launch.load_experiment_config")
    def test_provider_neutral_launch_creates_and_persists_evaluators(
        self,
        mock_load,
        mock_build_plan,
        mock_validator_cls,
        mock_adapter_cls,
        mock_preflight,
        mock_save_state,
        mock_secret,
    ):
        del mock_secret
        config = _make_provider_neutral_experiment_config_with_evaluators()
        mock_load.return_value = config

        launch_plan = MagicMock()
        launch_plan.experiment_name = "test-exp"
        resolved_plan = MagicMock()
        resolved_plan.experiment_name = "test-exp"
        expected_worker_fleets = (
            _make_provider_neutral_launch_state().worker_fleet_configs
        )
        expected_evaluator_fleets = [
            _make_stable_evaluator_fleet(
                zone="us-east1-b",
                prefix="evaluator-test-exp-us-east1-b",
            )
        ]
        mock_build_plan.return_value = launch_plan
        mock_preflight.return_value = MagicMock(
            resolved_plan=resolved_plan,
            redacted_worker_fleets=expected_worker_fleets,
            redacted_evaluator_fleets=expected_evaluator_fleets,
            orchestrator_env={},
            worker_placement_envs=[],
            evaluator_placement_envs=[{"ANTHROPIC_API_KEY": "anthropic-key"}],
        )
        mock_validator_cls.return_value.validate.return_value = None

        mock_adapter = mock_adapter_cls.return_value
        mock_adapter.build_orchestrator_config.return_value.project = "test-project"
        mock_adapter.build_orchestrator_config.return_value.ssh_via_iap = True
        mock_adapter.create_orchestrator.return_value = _make_gce_worker(
            "gce-orchestrator-test-exp",
            zone="us-east5-b",
            ip="10.0.0.50",
        )
        mock_adapter.create_workers.return_value = [_make_gce_worker("worker-east5")]
        evaluator = _make_gce_worker("evaluator-east1", zone="us-east1-b")
        evaluator.labels["crsbench-role"] = "evaluator"
        mock_adapter.create_evaluators.return_value = [evaluator]

        from crsbench.cloud.cli._launch import run_launch

        rc = run_launch(_make_launch_args())

        assert rc == 0
        assert mock_adapter.create_evaluators.call_args.kwargs["plan"] is resolved_plan
        assert mock_adapter.create_evaluators.call_args.kwargs[
            "env_passthrough_by_placement"
        ] == [{"ANTHROPIC_API_KEY": "anthropic-key"}]
        saved_state = mock_save_state.call_args.args[1]
        assert saved_state.evaluator_fleet_configs == expected_evaluator_fleets


def test_save_launch_state_redacts_secret_bearing_worker_fields(tmp_path: Path) -> None:
    from crsbench.cloud.launch_state import (
        CloudLaunchState,
        load_launch_state,
        save_launch_state,
    )

    config_path = tmp_path / "config.yaml"
    config_path.write_text("experiment: test-exp\n", encoding="utf-8")
    state = CloudLaunchState(
        experiment_name="test-exp",
        config_path=str(config_path),
        experiment_filestore="/tmp/filestore",
        redis_host="10.0.0.50:6379",
        redis_password="shared-secret",
        orchestrator_provider=CloudProvider.GCE,
        orchestrator_name="gce-orchestrator-test-exp",
        orchestrator_project="test-project",
        orchestrator_zone="us-east5-b",
        worker_fleet_configs=[
            GceWorkerFleetConfig(
                project="test-project",
                zone="us-east5-b",
                worker_count=1,
                machine_type="n2d-standard-16",
                boot_disk_size_gb=100,
                image="projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64",
                service_account_email="crsbench-worker@test-project.iam.gserviceaccount.com",
                owner_label="team-crs",
                worker_name_prefix="test-exp-us-east5-b",
                github_deploy_key_path=".crsbench-keys/crsbench-deploy",
            )
        ],
    )

    save_launch_state(config_path, state)

    raw_state = json.loads(
        (config_path.parent / ".crsbench-cloud" / "test-exp.json").read_text(
            encoding="utf-8"
        )
    )
    assert (
        raw_state["worker_fleet_configs"][0]["provider_metadata"][
            "github_deploy_key_path"
        ]
        is None
    )

    loaded_state = load_launch_state(config_path, "test-exp")
    assert loaded_state is not None
    assert (
        loaded_state.worker_fleet_configs[0].provider_metadata["github_deploy_key_path"]
        is None
    )


def test_append_created_instance_records_appends_jsonl_entries(tmp_path: Path) -> None:
    from crsbench.cloud.launch_state import (
        CreatedCloudInstanceRecord,
        append_created_instance_records,
        created_instance_cache_path,
    )

    config_path = tmp_path / "config.yaml"
    config_path.write_text("experiment: test-exp\n", encoding="utf-8")

    first_path = append_created_instance_records(
        config_path,
        experiment_name="test-exp",
        records=[
            CreatedCloudInstanceRecord(
                provider=CloudProvider.GCE,
                project="test-project",
                zone="us-east5-b",
                instance_name="gce-orchestrator-test-exp",
            ),
            CreatedCloudInstanceRecord(
                provider=CloudProvider.GCE,
                project="test-project",
                zone="us-east5-b",
                instance_name="worker-east5-001",
            ),
        ],
    )
    second_path = append_created_instance_records(
        config_path,
        experiment_name="test-exp",
        records=[
            CreatedCloudInstanceRecord(
                provider=CloudProvider.GCE,
                project="test-project",
                zone="us-east1-b",
                instance_name="worker-east1-001",
            )
        ],
    )

    assert first_path == created_instance_cache_path(config_path)
    assert second_path == first_path

    entries = [
        json.loads(line)
        for line in first_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [entry["instance_name"] for entry in entries] == [
        "gce-orchestrator-test-exp",
        "worker-east5-001",
        "worker-east1-001",
    ]


def test_collect_list_live_instances_prefers_persisted_fleets_when_launch_state_present():
    from crsbench.cloud.cli._collect import _list_live_instances

    launch_state = _make_provider_neutral_launch_state()
    context = MagicMock()
    context.launch_state = launch_state
    context.launch_plan = MagicMock(experiment_name="test-exp")
    context.worker_fleet_configs = launch_state.worker_fleet_configs
    context.evaluator_fleet_configs = []

    provisioner = MagicMock()
    provisioner.list_workers.side_effect = [
        [_make_gce_worker("crsbench-test-exp-work-001", zone="us-east1-b")],
        [_make_gce_worker("crsbench-test-exp-work-002", zone="us-east5-b")],
    ]

    workers = _list_live_instances(context, "test-exp", provisioner)

    assert [worker.name for worker in workers] == [
        "crsbench-test-exp-work-001",
        "crsbench-test-exp-work-002",
    ]
    assert provisioner.list_workers.call_count == 2


def test_collect_list_live_instances_deduplicates_overlapping_persisted_fleets():
    from crsbench.cloud.cli._collect import _list_live_instances

    launch_state = _make_provider_neutral_launch_state()
    context = MagicMock()
    context.launch_state = launch_state
    context.launch_plan = MagicMock(experiment_name="test-exp")
    context.worker_fleet_configs = launch_state.worker_fleet_configs
    context.evaluator_fleet_configs = []

    repeated_workers = [
        _make_gce_worker("crsbench-test-exp-work-001", zone="us-east5-b"),
        _make_gce_worker("crsbench-test-exp-work-002", zone="us-east5-b"),
    ]
    provisioner = MagicMock()
    provisioner.list_workers.side_effect = [
        repeated_workers,
        repeated_workers,
    ]

    workers = _list_live_instances(context, "test-exp", provisioner)

    assert [worker.name for worker in workers] == [
        "crsbench-test-exp-work-001",
        "crsbench-test-exp-work-002",
    ]
    assert provisioner.list_workers.call_count == 2


def test_collect_resolves_fallback_worker_fleet_by_stable_name_index():
    from crsbench.cloud.cli._collect import _resolve_instance_fleet

    context = MagicMock()
    context.worker_fleet_configs = [
        _make_stable_worker_fleet(
            zone="us-east5-b",
            zones=["us-east5-b", "us-east1-b"],
            start_index=1,
            worker_count=2,
        ),
        _make_stable_worker_fleet(
            zone="us-central1-a",
            start_index=3,
            worker_count=1,
        ),
    ]
    context.evaluator_fleet_configs = []
    worker = _make_gce_worker("crsbench-test-exp-work-003", zone="us-east1-b")

    fleet = _resolve_instance_fleet(context, worker)

    assert fleet.name_start_index == 3


def test_teardown_delete_live_instances_prefers_persisted_fleets_when_launch_state_present():
    from crsbench.cloud.cli._teardown import _delete_live_instances

    launch_state = _make_provider_neutral_launch_state()
    context = MagicMock()
    context.launch_state = launch_state
    context.launch_plan = MagicMock(experiment_name="test-exp")
    context.worker_fleet_configs = launch_state.worker_fleet_configs
    context.evaluator_fleet_configs = []

    provisioner = MagicMock()

    _delete_live_instances(context, "test-exp", provisioner)

    assert provisioner.delete_workers.call_count == 2


def test_teardown_resolves_fallback_worker_fleet_by_stable_name_index():
    from crsbench.cloud.cli._teardown import _resolve_instance_fleet

    context = MagicMock()
    context.worker_fleet_configs = [
        _make_stable_worker_fleet(
            zone="us-east5-b",
            zones=["us-east5-b", "us-east1-b"],
            start_index=1,
            worker_count=2,
        ),
        _make_stable_worker_fleet(
            zone="us-central1-a",
            start_index=3,
            worker_count=1,
        ),
    ]
    context.evaluator_fleet_configs = []
    worker = _make_gce_worker("crsbench-test-exp-work-003", zone="us-east1-b")

    fleet = _resolve_instance_fleet(context, worker)

    assert fleet.name_start_index == 3


class TestPreflight:
    """Tests for run_preflight() orchestration."""

    def _make_config_without_remote_root(self) -> ExperimentConfig:
        config = _make_provider_neutral_experiment_config()
        assert config.cloud is not None
        assert config.cloud.remote is not None
        config = config.model_copy(deep=True)
        config.cloud.remote.experiment_root = None
        return config

    @patch("crsbench.cloud.cli._preflight.run_preflight", return_value=0)
    def test_preflight_dispatches_through_run_cloud(self, mock_run_preflight):
        from crsbench.cloud.cli.cloud_command import run_cloud

        args = _make_preflight_args()
        rc = run_cloud(args)

        assert rc == 0
        mock_run_preflight.assert_called_once_with(args)

    @patch("crsbench.cloud.cli._preflight.load_experiment_config")
    def test_preflight_ready_path_reports_summary(
        self,
        mock_load_config,
        capsys,
    ):
        from crsbench.cloud.models import build_cloud_launch_plan

        config = _make_provider_neutral_experiment_config()
        mock_load_config.return_value = config

        launch_plan = build_cloud_launch_plan(config)
        preflight = MagicMock(
            resolved_plan=launch_plan,
            redacted_worker_fleets=[],
            redacted_evaluator_fleets=[],
            orchestrator_env={},
            worker_placement_envs=[],
            evaluator_placement_envs=[],
        )
        adapter = MagicMock()

        with (
            patch(
                "crsbench.cloud.cli._preflight.build_cloud_launch_plan",
                return_value=launch_plan,
            ) as mock_build_launch_plan,
            patch(
                "crsbench.cloud.cli._preflight.provider_adapter_for_launch_plan",
                return_value=adapter,
            ) as mock_provider_adapter_for_launch_plan,
            patch(
                "crsbench.cloud.cli._preflight.find_launch_target_conflicts",
                return_value=[],
            ) as mock_find_conflicts,
            patch(
                "crsbench.cloud.cli._preflight.prepare_launch_inputs",
                return_value=preflight,
            ) as mock_prepare_launch_inputs,
            patch(
                "crsbench.cloud.cli._preflight.QuotaValidator"
            ) as mock_quota_validator_cls,
        ):
            mock_quota_validator = mock_quota_validator_cls.return_value
            mock_quota_validator.validate.return_value = None

            from crsbench.cloud.cli._preflight import run_preflight

            rc = run_preflight(_make_preflight_args())

        assert rc == 0
        mock_load_config.assert_called_once_with(Path("/tmp/config.yaml"))
        mock_build_launch_plan.assert_called_once_with(config)
        mock_provider_adapter_for_launch_plan.assert_called_once_with(launch_plan)
        mock_find_conflicts.assert_called_once_with(
            config_path=Path("/tmp/config.yaml"),
            experiment_name="test-exp",
            adapter=adapter,
            plan=launch_plan,
        )
        mock_prepare_launch_inputs.assert_called_once_with(
            plan=launch_plan,
            cwd=Path.cwd(),
        )
        mock_quota_validator.validate.assert_called_once_with(launch_plan)
        output = capsys.readouterr().out.lower()
        assert "test-exp" in output
        assert "gce" in output
        assert "verdict" in output
        assert "plan" in output
        assert "defaults" in output
        assert "environment" in output

    @patch("crsbench.cloud.cli._preflight.build_cloud_launch_plan")
    @patch("crsbench.cloud.cli._preflight.load_experiment_config")
    def test_preflight_requires_cloud_config_as_usage_error(
        self,
        mock_load_config,
        mock_build_launch_plan,
    ):
        mock_load_config.return_value = _mock_config(has_cloud=False)

        from crsbench.cloud.cli._preflight import run_preflight

        rc = run_preflight(_make_preflight_args())

        assert rc == 2
        mock_build_launch_plan.assert_not_called()

    @patch("crsbench.cloud.cli._preflight.save_launch_state", create=True)
    @patch("crsbench.cloud.cli._preflight.load_experiment_config")
    def test_preflight_json_shape_excludes_warning_and_error_top_level_keys(
        self,
        mock_load_config,
        mock_save_launch_state,
        capsys,
    ):
        from crsbench.cloud.models import build_cloud_launch_plan

        config = _make_provider_neutral_experiment_config()
        mock_load_config.return_value = config

        launch_plan = build_cloud_launch_plan(config)
        preflight = MagicMock(
            resolved_plan=launch_plan,
            redacted_worker_fleets=[],
            redacted_evaluator_fleets=[],
            orchestrator_env={},
            worker_placement_envs=[],
            evaluator_placement_envs=[],
        )
        adapter = MagicMock()

        with (
            patch(
                "crsbench.cloud.cli._preflight.build_cloud_launch_plan",
                return_value=launch_plan,
            ) as mock_build_launch_plan,
            patch(
                "crsbench.cloud.cli._preflight.provider_adapter_for_launch_plan",
                return_value=adapter,
            ) as mock_provider_adapter_for_launch_plan,
            patch(
                "crsbench.cloud.cli._preflight.find_launch_target_conflicts",
                return_value=[],
            ) as mock_find_conflicts,
            patch(
                "crsbench.cloud.cli._preflight.prepare_launch_inputs",
                return_value=preflight,
            ) as mock_prepare_launch_inputs,
            patch(
                "crsbench.cloud.cli._preflight.QuotaValidator"
            ) as mock_quota_validator_cls,
        ):
            mock_quota_validator = mock_quota_validator_cls.return_value
            mock_quota_validator.validate.return_value = None

            from crsbench.cloud.cli._preflight import run_preflight

            rc = run_preflight(_make_preflight_args(json_output=True))

        assert rc == 0
        mock_load_config.assert_called_once_with(Path("/tmp/config.yaml"))
        mock_build_launch_plan.assert_called_once_with(config)
        mock_provider_adapter_for_launch_plan.assert_called_once_with(launch_plan)
        mock_find_conflicts.assert_called_once()
        mock_prepare_launch_inputs.assert_called_once_with(
            plan=launch_plan,
            cwd=Path.cwd(),
        )
        mock_quota_validator.validate.assert_called_once_with(launch_plan)
        payload = json.loads(capsys.readouterr().out)
        assert payload["schema_version"] == 1
        assert payload["provider"] == "gce"
        assert "verdict" in payload
        assert "checks" in payload
        assert "warnings" not in payload
        assert "errors" not in payload
        mock_save_launch_state.assert_not_called()

    @patch("crsbench.cloud.cli._preflight.save_launch_state", create=True)
    @patch("crsbench.cloud.cli._preflight.load_experiment_config")
    def test_preflight_blocks_duplicate_saved_launch_state(
        self,
        mock_load_config,
        mock_save_launch_state,
    ):
        from crsbench.cloud.models import build_cloud_launch_plan

        mock_load_config.return_value = _make_provider_neutral_experiment_config()
        mock_save_launch_state.return_value = None

        with (
            patch(
                "crsbench.cloud.cli._preflight.build_cloud_launch_plan"
            ) as mock_build_plan,
            patch(
                "crsbench.cloud.cli._preflight.provider_adapter_for_launch_plan"
            ) as mock_adapter_for_plan,
            patch(
                "crsbench.cloud.cli._preflight.find_launch_target_conflicts",
                return_value=[
                    "saved launch state exists at /tmp/.crsbench-cloud/test-exp.json"
                ],
            ) as mock_find_conflicts,
            patch(
                "crsbench.cloud.cli._preflight.prepare_launch_inputs"
            ) as mock_prepare,
            patch(
                "crsbench.cloud.cli._preflight.QuotaValidator"
            ) as mock_quota_validator_cls,
        ):
            launch_plan = build_cloud_launch_plan(mock_load_config.return_value)
            mock_build_plan.return_value = launch_plan
            mock_adapter = MagicMock()
            mock_adapter_for_plan.return_value = mock_adapter
            mock_quota_validator_cls.return_value.validate.return_value = None

            from crsbench.cloud.cli._preflight import run_preflight

            rc = run_preflight(_make_preflight_args())

        assert rc == 1
        mock_save_launch_state.assert_not_called()
        mock_build_plan.assert_called_once()
        mock_adapter_for_plan.assert_called_once_with(launch_plan)
        mock_find_conflicts.assert_called_once()
        mock_prepare.assert_not_called()
        mock_quota_validator_cls.return_value.validate.assert_not_called()

    @patch("crsbench.cloud.cli._preflight.save_launch_state", create=True)
    @patch("crsbench.cloud.cli._preflight.load_experiment_config")
    def test_preflight_blocks_duplicate_live_instances(
        self,
        mock_load_config,
        mock_save_launch_state,
    ):
        from crsbench.cloud.models import build_cloud_launch_plan

        config = _make_provider_neutral_experiment_config()
        mock_load_config.return_value = config
        mock_save_launch_state.return_value = None

        adapter = MagicMock()

        with (
            patch(
                "crsbench.cloud.cli._preflight.build_cloud_launch_plan"
            ) as mock_build_plan,
            patch(
                "crsbench.cloud.cli._preflight.provider_adapter_for_launch_plan",
                return_value=adapter,
            ) as mock_provider_adapter_for_launch_plan,
            patch(
                "crsbench.cloud.cli._preflight.find_launch_target_conflicts",
                return_value=[
                    "live cloud instances already exist: existing-orchestrator"
                ],
            ) as mock_find_conflicts,
            patch(
                "crsbench.cloud.cli._preflight.prepare_launch_inputs"
            ) as mock_prepare_launch_inputs,
            patch(
                "crsbench.cloud.cli._preflight.QuotaValidator"
            ) as mock_quota_validator_cls,
        ):
            launch_plan = build_cloud_launch_plan(config)
            mock_build_plan.return_value = launch_plan
            mock_quota_validator_cls.return_value.validate.return_value = None

            from crsbench.cloud.cli._preflight import run_preflight

            rc = run_preflight(_make_preflight_args())

        assert rc == 1
        mock_save_launch_state.assert_not_called()
        mock_provider_adapter_for_launch_plan.assert_called_once_with(launch_plan)
        mock_find_conflicts.assert_called_once()
        mock_prepare_launch_inputs.assert_not_called()
        mock_quota_validator_cls.return_value.validate.assert_not_called()

    @patch("crsbench.cloud.cli._preflight.save_launch_state", create=True)
    @patch("crsbench.cloud.cli._preflight.load_experiment_config")
    def test_preflight_blocks_provider_inventory_failure(
        self,
        mock_load_config,
        mock_save_launch_state,
    ):
        from crsbench.cloud.models import build_cloud_launch_plan

        config = _make_provider_neutral_experiment_config()
        mock_load_config.return_value = config
        mock_save_launch_state.return_value = None

        adapter = MagicMock()

        with (
            patch(
                "crsbench.cloud.cli._preflight.build_cloud_launch_plan"
            ) as mock_build_plan,
            patch(
                "crsbench.cloud.cli._preflight.provider_adapter_for_launch_plan",
                return_value=adapter,
            ) as mock_provider_adapter_for_launch_plan,
            patch(
                "crsbench.cloud.cli._preflight.find_launch_target_conflicts",
                side_effect=RuntimeError("instance inventory unavailable"),
            ) as mock_find_conflicts,
            patch(
                "crsbench.cloud.cli._preflight.prepare_launch_inputs"
            ) as mock_prepare_launch_inputs,
        ):
            launch_plan = build_cloud_launch_plan(config)
            mock_build_plan.return_value = launch_plan

            from crsbench.cloud.cli._preflight import run_preflight

            rc = run_preflight(_make_preflight_args())

        assert rc == 1
        mock_save_launch_state.assert_not_called()
        mock_provider_adapter_for_launch_plan.assert_called_once_with(launch_plan)
        mock_find_conflicts.assert_called_once()
        mock_prepare_launch_inputs.assert_not_called()

    @patch("crsbench.cloud.cli._preflight.save_launch_state", create=True)
    @patch("crsbench.cloud.cli._preflight.load_experiment_config")
    def test_preflight_blocks_quota_validation_failure(
        self,
        mock_load_config,
        mock_save_launch_state,
    ):
        from crsbench.cloud.models import build_cloud_launch_plan

        config = _make_provider_neutral_experiment_config()
        mock_load_config.return_value = config
        mock_save_launch_state.return_value = None
        from crsbench.cloud.quota import CloudQuotaValidationError

        adapter = MagicMock()

        with (
            patch(
                "crsbench.cloud.cli._preflight.build_cloud_launch_plan"
            ) as mock_build_plan,
            patch(
                "crsbench.cloud.cli._preflight.provider_adapter_for_launch_plan",
                return_value=adapter,
            ) as mock_provider_adapter_for_launch_plan,
            patch(
                "crsbench.cloud.cli._preflight.find_launch_target_conflicts",
                return_value=[],
            ) as mock_find_conflicts,
            patch(
                "crsbench.cloud.cli._preflight.prepare_launch_inputs"
            ) as mock_prepare_launch_inputs,
            patch(
                "crsbench.cloud.cli._preflight.QuotaValidator"
            ) as mock_quota_validator_cls,
        ):
            launch_plan = build_cloud_launch_plan(config)
            mock_build_plan.return_value = launch_plan
            mock_prepare_launch_inputs.return_value = MagicMock(
                resolved_plan=launch_plan,
                redacted_worker_fleets=[],
                redacted_evaluator_fleets=[],
                orchestrator_env={},
                worker_placement_envs=[],
                evaluator_placement_envs=[],
            )
            mock_quota_validator = mock_quota_validator_cls.return_value
            mock_quota_validator.validate.side_effect = CloudQuotaValidationError(
                "quota validation failed: gce:region:n2d required=4 available=2"
            )

            from crsbench.cloud.cli._preflight import run_preflight

            rc = run_preflight(_make_preflight_args())

        assert rc == 1
        mock_save_launch_state.assert_not_called()
        mock_provider_adapter_for_launch_plan.assert_called_once_with(launch_plan)
        mock_find_conflicts.assert_called_once()
        mock_quota_validator.validate.assert_called_once_with(launch_plan)

    @patch("crsbench.cloud.cli._preflight.save_launch_state", create=True)
    @patch("crsbench.cloud.cli._preflight.load_experiment_config")
    def test_preflight_blocks_quota_backend_failure(
        self,
        mock_load_config,
        mock_save_launch_state,
    ):
        from crsbench.cloud.models import build_cloud_launch_plan

        config = _make_provider_neutral_experiment_config()
        mock_load_config.return_value = config
        mock_save_launch_state.return_value = None

        adapter = MagicMock()

        with (
            patch(
                "crsbench.cloud.cli._preflight.build_cloud_launch_plan"
            ) as mock_build_plan,
            patch(
                "crsbench.cloud.cli._preflight.provider_adapter_for_launch_plan",
                return_value=adapter,
            ) as mock_provider_adapter_for_launch_plan,
            patch(
                "crsbench.cloud.cli._preflight.find_launch_target_conflicts",
                return_value=[],
            ) as mock_find_conflicts,
            patch(
                "crsbench.cloud.cli._preflight.prepare_launch_inputs"
            ) as mock_prepare_launch_inputs,
            patch(
                "crsbench.cloud.cli._preflight.QuotaValidator"
            ) as mock_quota_validator_cls,
        ):
            launch_plan = build_cloud_launch_plan(config)
            mock_build_plan.return_value = launch_plan
            mock_prepare_launch_inputs.return_value = MagicMock(
                resolved_plan=launch_plan,
                redacted_worker_fleets=[],
                redacted_evaluator_fleets=[],
                orchestrator_env={},
                worker_placement_envs=[],
                evaluator_placement_envs=[],
            )
            mock_quota_validator = mock_quota_validator_cls.return_value
            mock_quota_validator.validate.side_effect = RuntimeError(
                "quota backend unavailable"
            )

            from crsbench.cloud.cli._preflight import run_preflight

            rc = run_preflight(_make_preflight_args())

        assert rc == 1
        mock_save_launch_state.assert_not_called()
        mock_provider_adapter_for_launch_plan.assert_called_once_with(launch_plan)
        mock_find_conflicts.assert_called_once()
        mock_prepare_launch_inputs.assert_called_once_with(
            plan=launch_plan,
            cwd=Path.cwd(),
        )
        mock_quota_validator.validate.assert_called_once_with(launch_plan)

    @patch("crsbench.cloud.cli._preflight.save_launch_state", create=True)
    @patch("crsbench.cloud.cli._preflight.load_experiment_config")
    def test_preflight_warns_on_legacy_remote_path_fallback(
        self,
        mock_load_config,
        mock_save_launch_state,
        capsys,
    ):
        from crsbench.cloud.models import build_cloud_launch_plan

        config = self._make_config_without_remote_root()
        mock_load_config.return_value = config

        launch_plan = build_cloud_launch_plan(config)
        preflight = MagicMock(
            resolved_plan=launch_plan,
            redacted_worker_fleets=[],
            redacted_evaluator_fleets=[],
            orchestrator_env={},
            worker_placement_envs=[],
            evaluator_placement_envs=[],
        )
        adapter = MagicMock()

        with (
            patch(
                "crsbench.cloud.cli._preflight.build_cloud_launch_plan",
                return_value=launch_plan,
            ) as mock_build_launch_plan,
            patch(
                "crsbench.cloud.cli._preflight.provider_adapter_for_launch_plan",
                return_value=adapter,
            ) as mock_provider_adapter_for_launch_plan,
            patch(
                "crsbench.cloud.cli._preflight.find_launch_target_conflicts",
                return_value=[],
            ) as mock_find_conflicts,
            patch(
                "crsbench.cloud.cli._preflight.prepare_launch_inputs",
                return_value=preflight,
            ) as mock_prepare_launch_inputs,
            patch(
                "crsbench.cloud.cli._preflight.QuotaValidator"
            ) as mock_quota_validator_cls,
        ):
            mock_quota_validator = mock_quota_validator_cls.return_value
            mock_quota_validator.validate.return_value = None

            from crsbench.cloud.cli._preflight import run_preflight

            rc = run_preflight(_make_preflight_args())

        assert rc == 0
        mock_load_config.assert_called_once_with(Path("/tmp/config.yaml"))
        mock_build_launch_plan.assert_called_once_with(config)
        mock_provider_adapter_for_launch_plan.assert_called_once_with(launch_plan)
        mock_find_conflicts.assert_called_once()
        mock_prepare_launch_inputs.assert_called_once_with(
            plan=launch_plan,
            cwd=Path.cwd(),
        )
        mock_quota_validator.validate.assert_called_once_with(launch_plan)
        output = capsys.readouterr().out.lower()
        assert "warning" in output
        assert "remote" in output
        assert "fallback" in output
        mock_save_launch_state.assert_not_called()

    @patch("crsbench.cloud.cli._preflight.save_launch_state", create=True)
    @patch("crsbench.cloud.cli._preflight.load_experiment_config")
    def test_preflight_strict_upgrades_warning_to_block(
        self,
        mock_load_config,
        mock_save_launch_state,
        capsys,
    ):
        from crsbench.cloud.models import build_cloud_launch_plan

        config = self._make_config_without_remote_root()
        mock_load_config.return_value = config

        launch_plan = build_cloud_launch_plan(config)
        preflight = MagicMock(
            resolved_plan=launch_plan,
            redacted_worker_fleets=[],
            redacted_evaluator_fleets=[],
            orchestrator_env={},
            worker_placement_envs=[],
            evaluator_placement_envs=[],
        )
        adapter = MagicMock()

        with (
            patch(
                "crsbench.cloud.cli._preflight.build_cloud_launch_plan",
                return_value=launch_plan,
            ) as mock_build_launch_plan,
            patch(
                "crsbench.cloud.cli._preflight.provider_adapter_for_launch_plan",
                return_value=adapter,
            ) as mock_provider_adapter_for_launch_plan,
            patch(
                "crsbench.cloud.cli._preflight.find_launch_target_conflicts",
                return_value=[],
            ) as mock_find_conflicts,
            patch(
                "crsbench.cloud.cli._preflight.prepare_launch_inputs",
                return_value=preflight,
            ) as mock_prepare_launch_inputs,
            patch(
                "crsbench.cloud.cli._preflight.QuotaValidator"
            ) as mock_quota_validator_cls,
        ):
            mock_quota_validator = mock_quota_validator_cls.return_value
            mock_quota_validator.validate.return_value = None

            from crsbench.cloud.cli._preflight import run_preflight

            rc = run_preflight(_make_preflight_args(strict=True))

        assert rc == 1
        mock_load_config.assert_called_once_with(Path("/tmp/config.yaml"))
        mock_build_launch_plan.assert_called_once_with(config)
        mock_provider_adapter_for_launch_plan.assert_called_once_with(launch_plan)
        mock_find_conflicts.assert_called_once()
        mock_prepare_launch_inputs.assert_called_once_with(
            plan=launch_plan,
            cwd=Path.cwd(),
        )
        mock_quota_validator.validate.assert_called_once_with(launch_plan)
        output = capsys.readouterr().out.lower()
        assert "warning" in output
        assert "blocked" in output
        mock_save_launch_state.assert_not_called()

    @patch("crsbench.cloud.cli._preflight.save_launch_state", create=True)
    @patch("crsbench.cloud.cli._preflight.load_experiment_config")
    def test_preflight_does_not_persist_launch_state(
        self,
        mock_load_config,
        mock_save_launch_state,
    ):
        from crsbench.cloud.models import build_cloud_launch_plan

        config = _make_provider_neutral_experiment_config()
        mock_load_config.return_value = config

        launch_plan = build_cloud_launch_plan(config)
        preflight = MagicMock(
            resolved_plan=launch_plan,
            redacted_worker_fleets=[],
            redacted_evaluator_fleets=[],
            orchestrator_env={},
            worker_placement_envs=[],
            evaluator_placement_envs=[],
        )
        adapter = MagicMock()

        with (
            patch(
                "crsbench.cloud.cli._preflight.build_cloud_launch_plan",
                return_value=launch_plan,
            ) as mock_build_launch_plan,
            patch(
                "crsbench.cloud.cli._preflight.provider_adapter_for_launch_plan",
                return_value=adapter,
            ) as mock_provider_adapter_for_launch_plan,
            patch(
                "crsbench.cloud.cli._preflight.find_launch_target_conflicts",
                return_value=[],
            ) as mock_find_conflicts,
            patch(
                "crsbench.cloud.cli._preflight.prepare_launch_inputs",
                return_value=preflight,
            ) as mock_prepare_launch_inputs,
            patch(
                "crsbench.cloud.cli._preflight.QuotaValidator"
            ) as mock_quota_validator_cls,
        ):
            mock_quota_validator = mock_quota_validator_cls.return_value
            mock_quota_validator.validate.return_value = None

            from crsbench.cloud.cli._preflight import run_preflight

            rc = run_preflight(_make_preflight_args())

        assert rc == 0
        mock_load_config.assert_called_once_with(Path("/tmp/config.yaml"))
        mock_build_launch_plan.assert_called_once_with(config)
        mock_provider_adapter_for_launch_plan.assert_called_once_with(launch_plan)
        mock_find_conflicts.assert_called_once()
        mock_prepare_launch_inputs.assert_called_once_with(
            plan=launch_plan,
            cwd=Path.cwd(),
        )
        mock_quota_validator.validate.assert_called_once_with(launch_plan)
        mock_save_launch_state.assert_not_called()


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
        _headers, event_rows = mock_table.call_args_list[-1].args
        assert event_rows[0][1] == "orphan_detected"

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
    def test_status_json_output_marks_runtime_added_instances(
        self, mock_reconnect, fake_redis, capsys
    ):
        """JSON status output should expose runtime-added fleet provenance."""
        from crsbench.cloud.readiness import CloudReadinessStore
        from crsbench.distributed.job_lifecycle import JobLifecycleStore

        fake_redis.hset(
            "crsbench:cloud:workers:test-exp",
            "id-worker-3",
            json.dumps(
                _make_worker_status(
                    "crsbench-test-exp-work-003",
                    state="ready",
                    internal_ip="10.0.0.3",
                )
            ),
        )
        readiness = CloudReadinessStore(fake_redis)
        lifecycle = JobLifecycleStore(fake_redis)
        base_launch_state = _make_launch_state()
        launch_state = base_launch_state.model_copy(
            update={
                "worker_fleet_configs": [
                    *base_launch_state.worker_fleet_configs,
                    CloudFleetPlacementRecord(
                        provider=CloudProvider.GCE,
                        role="worker",
                        project="test-project",
                        zone="us-central1-a",
                        zones=["us-central1-a"],
                        region="us-central1",
                        count=1,
                        name_prefix="crsbench-test-exp-work",
                        name_start_index=3,
                        ssh_via_iap=True,
                        owner_label="team-crs",
                        placement_source="runtime_added",
                        provider_metadata={
                            **base_launch_state.worker_fleet_configs[
                                0
                            ].provider_metadata,
                            "project": "test-project",
                            "zone": "us-central1-a",
                            "zones": ["us-central1-a"],
                            "worker_count": 1,
                            "worker_name_start_index": 3,
                            "worker_name_prefix": "crsbench-test-exp-work",
                        },
                    ),
                ]
            }
        )
        mock_reconnect.return_value = (
            _make_resolved_cloud_context(launch_state),
            fake_redis,
            readiness,
            lifecycle,
            Path("/tmp"),
        )

        from crsbench.cloud.cli._status import run_status

        rc = run_status(_make_status_args(json_output=True))
        assert rc == 0

        data = json.loads(capsys.readouterr().out)
        worker_entry = next(
            entry
            for entry in data["fleet"]
            if entry["instance_name"] == "crsbench-test-exp-work-003"
        )
        assert worker_entry["placement_source"] == "runtime_added"

    @patch("crsbench.cloud.cli._status._load_status_jobs", create=True)
    @patch("crsbench.cloud.cli._status.reconnect")
    def test_status_json_output_falls_back_to_queue_snapshot_when_lifecycle_empty(
        self,
        mock_reconnect,
        mock_load_status_jobs,
        fake_redis,
        capsys,
    ):
        """Status should still report remote queue work when lifecycle tracking is empty."""
        for i, state in enumerate(["ready", "ready"], start=1):
            w = _make_worker_status(
                f"worker-{i}",
                state=state,
                internal_ip=f"10.0.0.{i}",
            )
            fake_redis.hset(
                "crsbench:cloud:workers:test-exp",
                f"id-worker-{i}",
                json.dumps(w),
            )
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
        mock_load_status_jobs.return_value = [
            SimpleNamespace(
                job_id="job-running",
                trial_key="trial-running",
                state="running",
                claimed_by="worker-1",
                retry_count=0,
            ),
            SimpleNamespace(
                job_id="job-completed",
                trial_key="trial-completed",
                state="completed",
                claimed_by="worker-2",
                retry_count=1,
            ),
        ]

        from crsbench.cloud.cli._status import run_status

        rc = run_status(_make_status_args(json_output=True))
        assert rc == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["collection"]["total"] == 2
        assert data["collection"]["running"] == 1
        assert data["collection"]["completed"] == 1
        assert {job["job_id"] for job in data["jobs"]} == {
            "job-running",
            "job-completed",
        }
        mock_load_status_jobs.assert_called_once()

    @patch("crsbench.cloud.cli._status.list_queue_job_entries")
    @patch("crsbench.cloud.cli._status.queue_module.rq")
    @patch("crsbench.cloud.cli._status.queue_module.REDIS_AVAILABLE", new=True)
    def test_load_status_jobs_merges_queue_only_jobs_when_lifecycle_is_partial(
        self,
        mock_rq,
        mock_list_queue_job_entries,
    ):
        """Status should not hide queue-only jobs once lifecycle tracking has started."""
        from crsbench.cloud.cli._status import _load_status_jobs
        from crsbench.distributed.job_lifecycle import JobLifecycleRecord, JobState

        lifecycle = MagicMock()
        lifecycle.list_jobs.return_value = [
            JobLifecycleRecord(
                job_id="job-1",
                trial_key="trial-1",
                state=JobState.RUNNING,
                claimed_by="worker-1",
                retry_count=0,
            )
        ]
        mock_rq.Queue.return_value = MagicMock()
        mock_list_queue_job_entries.return_value = [
            SimpleNamespace(
                job_id="job-1",
                trial_key="trial-1",
                state="running",
                claimed_by="worker-1",
                retry_count=0,
            ),
            SimpleNamespace(
                job_id="job-2",
                trial_key="trial-2",
                state="queued",
                claimed_by=None,
                retry_count=0,
            ),
        ]

        jobs = _load_status_jobs(MagicMock(), lifecycle, "test-exp")

        assert [job.job_id for job in jobs] == ["job-1", "job-2"]

    @patch("crsbench.cloud.cli._status.reconnect")
    def test_status_output_includes_evaluator_role(self, mock_reconnect, fake_redis):
        """Status output should include evaluator instances in the fleet summary."""
        _populate_fake_redis(fake_redis)
        fake_redis.hset(
            "crsbench:cloud:evaluators:test-exp",
            "id-evaluator-1",
            json.dumps(
                _make_worker_status(
                    "evaluator-1",
                    state="ready",
                    internal_ip="10.0.1.10",
                    role="evaluator",
                )
            ),
        )
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
        fleet_headers, fleet_rows = mock_table.call_args_list[0].args
        assert fleet_headers == ["Instance", "Role", "Source", "State", "Zone", "IP"]
        assert [
            "evaluator-1",
            "evaluator",
            "unknown",
            "ready",
            "us-central1-a",
            "10.0.1.10",
        ] in fleet_rows

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

    @patch("crsbench.cloud.cli._status.reconnect")
    def test_status_json_output_includes_evaluator_role(
        self, mock_reconnect, fake_redis, capsys
    ):
        """JSON status output should include evaluator fleet entries with their role."""
        _populate_fake_redis(fake_redis)
        fake_redis.hset(
            "crsbench:cloud:evaluators:test-exp",
            "id-evaluator-1",
            json.dumps(
                _make_worker_status(
                    "evaluator-1",
                    state="ready",
                    internal_ip="10.0.1.10",
                    role="evaluator",
                )
            ),
        )
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
        evaluator_entry = next(
            entry for entry in data["fleet"] if entry["instance_name"] == "evaluator-1"
        )
        assert evaluator_entry["role"] == "evaluator"

    @patch("crsbench.cloud.cli._status.resolve_effective_experiment_name")
    @patch("crsbench.cloud.cli._status.reconnect")
    def test_status_uses_resolved_experiment_name_when_cli_experiment_is_omitted(
        self,
        mock_reconnect,
        mock_resolve_experiment_name,
        fake_redis,
    ):
        """Status should query Redis using the config-resolved experiment name."""
        from crsbench.cloud.readiness import CloudReadinessStore
        from crsbench.distributed.job_lifecycle import JobLifecycleStore

        mock_resolve_experiment_name.return_value = "resolved-exp"
        fake_redis.hset(
            "crsbench:cloud:workers:resolved-exp",
            "id-worker-1",
            json.dumps(_make_worker_status("worker-1", state="ready")),
        )
        fake_redis.rpush(
            "crsbench:recovery-events:resolved-exp",
            json.dumps(_make_recovery_event()),
        )
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

        rc = run_status(_make_status_args(experiment=None, json_output=True))
        assert rc == 0
        mock_reconnect.assert_called_once_with(
            "/tmp/config.yaml",
            "resolved-exp",
            wait_for_remote_redis=True,
        )


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
        assert all(e["event"] == "orphan_detected" for e in data)

    @patch("crsbench.cloud.cli._events.reconnect")
    def test_events_filter_accepts_legacy_type_field(self, mock_reconnect, fake_redis):
        """Event filtering remains compatible with older records keyed by type."""
        fake_redis.rpush(
            "crsbench:recovery-events:test-exp",
            json.dumps(
                {
                    "type": "requeued",
                    "job_id": "job-1",
                    "worker": "worker-1",
                    "detail": "requeued for job-1",
                    "ts": "2026-03-13T00:00:00+00:00",
                }
            ),
        )
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
        _headers, rows = mock_table.call_args.args
        assert rows == [
            [
                "2026-03-13T00:00:00+00:00",
                "requeued",
                "job-1",
                "worker-1",
                "requeued for job-1",
            ]
        ]

    @patch("crsbench.cloud.cli._events.resolve_effective_experiment_name")
    @patch("crsbench.cloud.cli._events.reconnect")
    def test_events_uses_resolved_experiment_name_when_cli_experiment_is_omitted(
        self,
        mock_reconnect,
        mock_resolve_experiment_name,
        fake_redis,
        capsys,
    ):
        """Events should query Redis using the config-resolved experiment name."""
        mock_resolve_experiment_name.return_value = "resolved-exp"
        fake_redis.rpush(
            "crsbench:recovery-events:resolved-exp",
            json.dumps(_make_recovery_event()),
        )
        mock_reconnect.return_value = (
            MagicMock(),
            fake_redis,
            None,
            None,
            Path("/tmp"),
        )

        from crsbench.cloud.cli._events import run_events

        rc = run_events(_make_events_args(experiment=None, json_output=True))
        assert rc == 0
        mock_reconnect.assert_called_once_with(
            "/tmp/config.yaml",
            "resolved-exp",
            wait_for_remote_redis=True,
        )
        data = json.loads(capsys.readouterr().out)
        assert len(data) == 1


# ---------------------------------------------------------------------------
# Collect sub-action tests
# ---------------------------------------------------------------------------


def _make_collect_args(
    experiment: str = "test-exp",
    config: str = "/tmp/config.yaml",
    remote_dir: str = "/home/user/crsbench-experiments/test-exp",
    *,
    force: bool = False,
    timestamp: bool = False,
):
    return argparse.Namespace(
        experiment=experiment,
        config=config,
        remote_dir=remote_dir,
        force=force,
        timestamp=timestamp,
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


def _make_completed_process(returncode: int = 0):
    return SimpleNamespace(returncode=returncode)


class TestList:
    """Tests for run_list() sub-action."""

    @patch("crsbench.cloud.cli._list.resolve_effective_experiment_name")
    @patch("crsbench.cloud.cli._list.resolve_cloud_context")
    @patch("crsbench.cloud.cli._list.provisioner_for_context")
    def test_list_prints_live_instances_table(
        self,
        mock_provisioner_cls,
        mock_resolve_context,
        mock_resolve_experiment_name,
        capsys,
    ):
        mock_resolve_experiment_name.return_value = "test-exp"
        context = _make_resolved_cloud_context(_make_launch_state())
        mock_resolve_context.return_value = context
        provisioner = mock_provisioner_cls.return_value
        provisioner.list_workers.return_value = [
            _make_gce_worker("crsbench-test-exp-work-001")
        ]
        provisioner.list_evaluators.return_value = []
        provisioner.get_instance_record.return_value = _make_gce_worker(
            "crsbench-test-exp-orch",
            zone="us-east5-b",
            ip="10.0.0.50",
        )
        provisioner.get_instance_record.return_value.labels["crsbench-role"] = (
            "orchestrator"
        )

        from crsbench.cloud.cli._list import run_list

        rc = run_list(_make_list_args())

        assert rc == 0
        out = capsys.readouterr().out
        assert "crsbench-test-exp-orch" in out
        assert "crsbench-test-exp-work-001" in out
        assert "orchestrator" in out
        assert "worker" in out

    @patch("crsbench.cloud.cli._list.resolve_effective_experiment_name")
    @patch("crsbench.cloud.cli._list.resolve_cloud_context")
    @patch("crsbench.cloud.cli._list.provisioner_for_context")
    def test_list_supports_json_output(
        self,
        mock_provisioner_cls,
        mock_resolve_context,
        mock_resolve_experiment_name,
        capsys,
    ):
        mock_resolve_experiment_name.return_value = "test-exp"
        context = _make_resolved_cloud_context(_make_launch_state())
        mock_resolve_context.return_value = context
        provisioner = mock_provisioner_cls.return_value
        provisioner.list_workers.return_value = [
            _make_gce_worker("crsbench-test-exp-work-001")
        ]
        provisioner.list_evaluators.return_value = []
        provisioner.get_instance_record.return_value = _make_gce_worker(
            "crsbench-test-exp-orch",
            zone="us-east5-b",
            ip="10.0.0.50",
        )
        provisioner.get_instance_record.return_value.labels["crsbench-role"] = (
            "orchestrator"
        )

        from crsbench.cloud.cli._list import run_list

        rc = run_list(_make_list_args(json_output=True))

        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert [row["name"] for row in payload] == [
            "crsbench-test-exp-orch",
            "crsbench-test-exp-work-001",
        ]

    @patch("crsbench.cloud.cli._list.resolve_effective_experiment_name")
    @patch("crsbench.cloud.cli._list.resolve_cloud_context")
    @patch("crsbench.cloud.cli._list.provisioner_for_context")
    def test_list_json_output_marks_runtime_added_instances(
        self,
        mock_provisioner_cls,
        mock_resolve_context,
        mock_resolve_experiment_name,
        capsys,
    ):
        mock_resolve_experiment_name.return_value = "test-exp"
        base_launch_state = _make_launch_state()
        launch_state = base_launch_state.model_copy(
            update={
                "worker_fleet_configs": [
                    *base_launch_state.worker_fleet_configs,
                    CloudFleetPlacementRecord(
                        provider=CloudProvider.GCE,
                        role="worker",
                        project="test-project",
                        zone="us-central1-a",
                        zones=["us-central1-a"],
                        region="us-central1",
                        count=1,
                        name_prefix="crsbench-test-exp-work",
                        name_start_index=3,
                        ssh_via_iap=True,
                        owner_label="team-crs",
                        placement_source="runtime_added",
                        provider_metadata={
                            **base_launch_state.worker_fleet_configs[
                                0
                            ].provider_metadata,
                            "project": "test-project",
                            "zone": "us-central1-a",
                            "zones": ["us-central1-a"],
                            "worker_count": 1,
                            "worker_name_start_index": 3,
                            "worker_name_prefix": "crsbench-test-exp-work",
                        },
                    ),
                ]
            }
        )
        context = _make_resolved_cloud_context(launch_state)
        mock_resolve_context.return_value = context
        provisioner = mock_provisioner_cls.return_value
        provisioner.list_workers.return_value = [
            _make_gce_worker("crsbench-test-exp-work-003")
        ]
        provisioner.list_evaluators.return_value = []
        provisioner.get_instance_record.return_value = None

        from crsbench.cloud.cli._list import run_list

        rc = run_list(_make_list_args(json_output=True))

        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        worker_entry = next(
            row for row in payload if row["name"] == "crsbench-test-exp-work-003"
        )
        assert worker_entry["placement_source"] == "runtime_added"

    @patch("crsbench.cloud.cli._list.provisioner_for_context")
    @patch("crsbench.cloud.cli._list.resolve_effective_experiment_name")
    @patch("crsbench.cloud.cli._list.resolve_cloud_context")
    def test_list_resolves_provisioner_from_context(
        self,
        mock_resolve_context,
        mock_resolve_experiment_name,
        mock_provisioner_for_context,
    ):
        mock_resolve_experiment_name.return_value = "test-exp"
        context = _make_resolved_cloud_context(_make_launch_state())
        mock_resolve_context.return_value = context
        provisioner = MagicMock()
        provisioner.get_instance_record.return_value = None
        provisioner.list_workers.return_value = []
        provisioner.list_evaluators.return_value = []
        mock_provisioner_for_context.return_value = provisioner

        from crsbench.cloud.cli._list import run_list

        rc = run_list(_make_list_args())

        assert rc == 0
        mock_provisioner_for_context.assert_called_once_with(context)


class TestSsh:
    """Tests for run_ssh() sub-action."""

    @patch("crsbench.cloud.cli._ssh.subprocess.run")
    @patch("crsbench.cloud.cli._ssh.build_ssh_command")
    @patch("crsbench.cloud.cli._ssh.resolve_effective_experiment_name")
    @patch("crsbench.cloud.cli._ssh.resolve_cloud_context")
    @patch("crsbench.cloud.cli._ssh.provisioner_for_context")
    def test_ssh_runs_selected_instance_command(
        self,
        mock_provisioner_cls,
        mock_resolve_context,
        mock_resolve_experiment_name,
        mock_build_ssh_command,
        mock_run,
    ):
        mock_resolve_experiment_name.return_value = "test-exp"
        context = _make_resolved_cloud_context(_make_launch_state())
        mock_resolve_context.return_value = context
        provisioner = mock_provisioner_cls.return_value
        provisioner.list_workers.return_value = [
            _make_gce_worker("crsbench-test-exp-work-001", zone="us-central1-a")
        ]
        provisioner.list_evaluators.return_value = []
        provisioner.get_instance_record.return_value = _make_gce_worker(
            "crsbench-test-exp-orch",
            zone="us-east5-b",
            ip="10.0.0.50",
        )
        provisioner.get_instance_record.return_value.labels["crsbench-role"] = (
            "orchestrator"
        )
        mock_build_ssh_command.return_value = [
            "gcloud",
            "compute",
            "ssh",
            "crsbench-test-exp-work-001",
            "--zone=us-central1-a",
            "--ssh-flag=-t",
            '--command=sudo -iu crsbench env -C /opt/crsbench bash -lc \'if [[ -f "/var/lib/crsbench/worker.env" ]]; then set -a; source "/var/lib/crsbench/worker.env"; set +a; fi; exec bash -il\'',
        ]
        mock_run.return_value = _make_completed_process(0)

        from crsbench.cloud.cli._ssh import run_ssh

        rc = run_ssh(_make_ssh_args(instance="work-001"))

        assert rc == 0
        mock_build_ssh_command.assert_called_once_with(
            mock.ANY,
            remote_command=[
                "sudo",
                "-iu",
                "crsbench",
                "env",
                "-C",
                "/opt/crsbench",
                "bash",
                "-lc",
                'if [[ -f "/var/lib/crsbench/worker.env" ]]; then set -a; source "/var/lib/crsbench/worker.env"; set +a; fi; exec bash -il',
            ],
            tty=True,
        )
        cmd = mock_run.call_args.args[0]
        assert cmd == mock_build_ssh_command.return_value

    @patch("crsbench.cloud.cli._ssh.subprocess.run")
    @patch("crsbench.cloud.cli._ssh.build_ssh_command")
    @patch("crsbench.cloud.cli._ssh.select_target")
    @patch("crsbench.cloud.cli._ssh.resolve_effective_experiment_name")
    @patch("crsbench.cloud.cli._ssh.resolve_cloud_context")
    @patch("crsbench.cloud.cli._ssh.provisioner_for_context")
    def test_ssh_interactively_selects_instance_when_omitted(
        self,
        mock_provisioner_cls,
        mock_resolve_context,
        mock_resolve_experiment_name,
        mock_select_target,
        mock_build_ssh_command,
        mock_run,
        monkeypatch,
        capsys,
    ):
        mock_resolve_experiment_name.return_value = "test-exp"
        context = _make_resolved_cloud_context(_make_launch_state())
        mock_resolve_context.return_value = context
        provisioner = mock_provisioner_cls.return_value
        provisioner.list_workers.return_value = [
            _make_gce_worker("crsbench-test-exp-work-001", zone="us-central1-a")
        ]
        provisioner.list_evaluators.return_value = []
        provisioner.get_instance_record.return_value = _make_gce_worker(
            "crsbench-test-exp-orch",
            zone="us-east5-b",
            ip="10.0.0.50",
        )
        provisioner.get_instance_record.return_value.labels["crsbench-role"] = (
            "orchestrator"
        )
        mock_select_target.return_value = type(
            "Row",
            (),
            {
                "name": "crsbench-test-exp-work-001",
                "alias": "work-001",
                "role": "worker",
                "zone": "us-central1-a",
                "project": "test-project",
                "ssh_via_iap": False,
            },
        )()
        mock_build_ssh_command.return_value = [
            "gcloud",
            "compute",
            "ssh",
            "crsbench-test-exp-work-001",
        ]
        mock_run.return_value = _make_completed_process(0)
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)

        from crsbench.cloud.cli._ssh import run_ssh

        rc = run_ssh(_make_ssh_args(instance=None))

        assert rc == 0
        mock_build_ssh_command.assert_called_once_with(
            mock_select_target.return_value,
            remote_command=[
                "sudo",
                "-iu",
                "crsbench",
                "env",
                "-C",
                "/opt/crsbench",
                "bash",
                "-lc",
                'if [[ -f "/var/lib/crsbench/worker.env" ]]; then set -a; source "/var/lib/crsbench/worker.env"; set +a; fi; exec bash -il',
            ],
            tty=True,
        )
        cmd = mock_run.call_args.args[0]
        assert cmd == mock_build_ssh_command.return_value
        mock_select_target.assert_called_once()

    @patch("crsbench.cloud.cli._ssh.subprocess.run")
    @patch("crsbench.cloud.cli._ssh.build_ssh_command")
    @patch("crsbench.cloud.cli._ssh.resolve_effective_experiment_name")
    @patch("crsbench.cloud.cli._ssh.resolve_cloud_context")
    @patch("crsbench.cloud.cli._ssh.provisioner_for_context")
    def test_ssh_uses_orchestrator_env_file_for_orchestrator_role(
        self,
        mock_provisioner_cls,
        mock_resolve_context,
        mock_resolve_experiment_name,
        mock_build_ssh_command,
        mock_run,
    ):
        mock_resolve_experiment_name.return_value = "test-exp"
        context = _make_resolved_cloud_context(_make_launch_state())
        mock_resolve_context.return_value = context
        provisioner = mock_provisioner_cls.return_value
        provisioner.list_workers.return_value = []
        provisioner.list_evaluators.return_value = []
        provisioner.get_instance_record.return_value = _make_gce_worker(
            "crsbench-test-exp-orch",
            zone="us-east5-b",
            ip="10.0.0.50",
        )
        provisioner.get_instance_record.return_value.labels["crsbench-role"] = (
            "orchestrator"
        )
        mock_build_ssh_command.return_value = ["gcloud", "compute", "ssh", "dummy"]
        mock_run.return_value = _make_completed_process(0)

        from crsbench.cloud.cli._ssh import run_ssh

        rc = run_ssh(_make_ssh_args(instance="orch"))

        assert rc == 0
        mock_build_ssh_command.assert_called_once_with(
            mock.ANY,
            remote_command=[
                "sudo",
                "-iu",
                "crsbench",
                "env",
                "-C",
                "/opt/crsbench",
                "bash",
                "-lc",
                'if [[ -f "/var/lib/crsbench/orchestrator.env" ]]; then set -a; source "/var/lib/crsbench/orchestrator.env"; set +a; fi; exec bash -il',
            ],
            tty=True,
        )

    @patch("crsbench.cloud.cli._ssh.resolve_effective_experiment_name")
    @patch("crsbench.cloud.cli._ssh.resolve_cloud_context")
    @patch("crsbench.cloud.cli._ssh.provisioner_for_context")
    def test_ssh_requires_instance_when_stdin_is_not_a_tty(
        self,
        mock_provisioner_cls,
        mock_resolve_context,
        mock_resolve_experiment_name,
        monkeypatch,
        capsys,
    ):
        mock_resolve_experiment_name.return_value = "test-exp"
        context = _make_resolved_cloud_context(_make_launch_state())
        mock_resolve_context.return_value = context
        provisioner = mock_provisioner_cls.return_value
        provisioner.list_workers.return_value = [
            _make_gce_worker("crsbench-test-exp-work-001", zone="us-central1-a")
        ]
        provisioner.list_evaluators.return_value = []
        provisioner.get_instance_record.return_value = _make_gce_worker(
            "crsbench-test-exp-orch",
            zone="us-east5-b",
            ip="10.0.0.50",
        )
        provisioner.get_instance_record.return_value.labels["crsbench-role"] = (
            "orchestrator"
        )
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)

        from crsbench.cloud.cli._ssh import run_ssh

        rc = run_ssh(_make_ssh_args(instance=None))

        assert rc == 1
        out = capsys.readouterr().out
        assert "crsbench-test-exp-orch" in out
        assert "crsbench-test-exp-work-001" in out

    @patch("crsbench.cloud.cli._ssh.select_target", side_effect=KeyboardInterrupt)
    @patch("crsbench.cloud.cli._ssh.resolve_effective_experiment_name")
    @patch("crsbench.cloud.cli._ssh.resolve_cloud_context")
    @patch("crsbench.cloud.cli._ssh.provisioner_for_context")
    def test_ssh_treats_keyboard_interrupt_as_normal_exit(
        self,
        mock_provisioner_cls,
        mock_resolve_context,
        mock_resolve_experiment_name,
        mock_select_target,
    ):
        mock_resolve_experiment_name.return_value = "test-exp"
        assert mock_select_target is not None
        context = _make_resolved_cloud_context(_make_launch_state())
        mock_resolve_context.return_value = context
        provisioner = mock_provisioner_cls.return_value
        provisioner.list_workers.return_value = [
            _make_gce_worker("crsbench-test-exp-work-001", zone="us-central1-a")
        ]
        provisioner.list_evaluators.return_value = []
        provisioner.get_instance_record.return_value = _make_gce_worker(
            "crsbench-test-exp-orch",
            zone="us-east5-b",
            ip="10.0.0.50",
        )
        provisioner.get_instance_record.return_value.labels["crsbench-role"] = (
            "orchestrator"
        )

        from crsbench.cloud.cli._ssh import run_ssh

        rc = run_ssh(_make_ssh_args(instance=None))

        assert rc == 130


class TestSerial:
    """Tests for run_serial() sub-action."""

    @patch("crsbench.cloud.cli._serial.subprocess.run")
    @patch("crsbench.cloud.cli._serial.build_serial_command")
    @patch("crsbench.cloud.cli._serial.resolve_effective_experiment_name")
    @patch("crsbench.cloud.cli._serial.resolve_cloud_context")
    @patch("crsbench.cloud.cli._serial.provisioner_for_context")
    def test_serial_runs_selected_instance_console(
        self,
        mock_provisioner_cls,
        mock_resolve_context,
        mock_resolve_experiment_name,
        mock_build_serial_command,
        mock_run,
    ):
        mock_resolve_experiment_name.return_value = "test-exp"
        context = _make_resolved_cloud_context(_make_launch_state())
        mock_resolve_context.return_value = context
        provisioner = mock_provisioner_cls.return_value
        provisioner.list_workers.return_value = [
            _make_gce_worker("crsbench-test-exp-work-001", zone="us-central1-a")
        ]
        provisioner.list_evaluators.return_value = []
        provisioner.get_instance_record.return_value = _make_gce_worker(
            "crsbench-test-exp-orch",
            zone="us-east5-b",
            ip="10.0.0.50",
        )
        provisioner.get_instance_record.return_value.labels["crsbench-role"] = (
            "orchestrator"
        )
        mock_build_serial_command.return_value = [
            "gcloud",
            "compute",
            "connect-to-serial-port",
            "crsbench-test-exp-work-001",
            "--port=2",
        ]
        mock_run.return_value = _make_completed_process(0)

        from crsbench.cloud.cli._serial import run_serial

        rc = run_serial(_make_serial_args(instance="work-001", port=2))

        assert rc == 0
        mock_build_serial_command.assert_called_once_with(mock.ANY, port=2)
        assert mock_run.call_args.args[0] == mock_build_serial_command.return_value

    @patch("crsbench.cloud.cli._serial.select_target", side_effect=KeyboardInterrupt)
    @patch("crsbench.cloud.cli._serial.resolve_effective_experiment_name")
    @patch("crsbench.cloud.cli._serial.resolve_cloud_context")
    @patch("crsbench.cloud.cli._serial.provisioner_for_context")
    def test_serial_treats_keyboard_interrupt_as_normal_exit(
        self,
        mock_provisioner_cls,
        mock_resolve_context,
        mock_resolve_experiment_name,
        mock_select_target,
    ):
        mock_resolve_experiment_name.return_value = "test-exp"
        assert mock_select_target is not None
        context = _make_resolved_cloud_context(_make_launch_state())
        mock_resolve_context.return_value = context
        provisioner = mock_provisioner_cls.return_value
        provisioner.list_workers.return_value = [
            _make_gce_worker("crsbench-test-exp-work-001", zone="us-central1-a")
        ]
        provisioner.list_evaluators.return_value = []
        provisioner.get_instance_record.return_value = _make_gce_worker(
            "crsbench-test-exp-orch",
            zone="us-east5-b",
            ip="10.0.0.50",
        )
        provisioner.get_instance_record.return_value.labels["crsbench-role"] = (
            "orchestrator"
        )

        from crsbench.cloud.cli._serial import run_serial

        rc = run_serial(_make_serial_args(instance=None))

        assert rc == 130


class TestRemoteAccess:
    """Tests for provider-neutral remote access helpers."""

    def test_resolve_inventory_selector_accepts_unique_short_form_suffix(self):
        from crsbench.cloud.cli._instance_inventory import resolve_inventory_selector

        rows = [
            _make_inventory_row(
                alias="work-west-001",
                name="crsbench-test-exp-work-west-001",
                role="worker",
            ),
            _make_inventory_row(
                alias="eval-central-eval-001",
                name="crsbench-test-exp-eval-central-eval-001",
                role="evaluator",
            ),
        ]

        selected = resolve_inventory_selector(rows, "eval-001")

        assert selected == rows[1]

    def test_resolve_inventory_selector_accepts_unique_role_alias(self):
        from crsbench.cloud.cli._instance_inventory import resolve_inventory_selector

        rows = [
            _make_inventory_row(
                alias="orch",
                name="crsbench-test-exp-orch",
                role="orchestrator",
            ),
            _make_inventory_row(
                alias="work-001",
                name="crsbench-test-exp-work-001",
                role="worker",
            ),
            _make_inventory_row(
                alias="eval-long-name-001",
                name="crsbench-test-exp-eval-long-name-001",
                role="evaluator",
            ),
        ]

        selected = resolve_inventory_selector(rows, "eval")

        assert selected == rows[2]

    def test_resolve_inventory_selector_rejects_ambiguous_filtered_match(self):
        from crsbench.cloud.cli._instance_inventory import resolve_inventory_selector

        rows = [
            _make_inventory_row(
                alias="eval-east-001",
                name="crsbench-test-exp-eval-east-001",
                role="evaluator",
            ),
            _make_inventory_row(
                alias="eval-west-002",
                name="crsbench-test-exp-eval-west-002",
                role="evaluator",
            ),
        ]

        assert resolve_inventory_selector(rows, "eval") is None

    def test_select_target_prompts_when_selector_is_ambiguous_and_interactive(
        self,
        monkeypatch,
    ):
        from crsbench.cloud.cli._remote_access import select_target

        rows = [
            _make_inventory_row(
                alias="work-001",
                name="crsbench-test-exp-work-001",
                role="worker",
            ),
            _make_inventory_row(
                alias="work-002",
                name="crsbench-test-exp-work-002",
                role="worker",
            ),
        ]
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda _: "2")

        selected = select_target(rows, "work")

        assert selected == rows[1]

    def test_select_target_rejects_ambiguous_selector_when_not_interactive(
        self,
        monkeypatch,
        capsys,
    ):
        from crsbench.cloud.cli._remote_access import select_target

        rows = [
            _make_inventory_row(
                alias="work-001",
                name="crsbench-test-exp-work-001",
                role="worker",
            ),
            _make_inventory_row(
                alias="work-002",
                name="crsbench-test-exp-work-002",
                role="worker",
            ),
        ]
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)

        selected = select_target(rows, "work")

        assert selected is None
        out = capsys.readouterr().out
        assert "crsbench-test-exp-work-001" in out
        assert "crsbench-test-exp-work-002" in out

    @patch("crsbench.cloud.cli._remote_access.transport_for_provider")
    def test_build_ssh_command_delegates_to_provider_transport(
        self,
        mock_transport_for_provider,
    ):
        from crsbench.cloud.cli._instance_inventory import CloudInstanceInventoryRow
        from crsbench.cloud.cli._remote_access import build_ssh_command

        target = CloudInstanceInventoryRow(
            alias="work-001",
            name="crsbench-test-exp-work-001",
            role="worker",
            placement_source="config",
            provider="gce",
            project="test-project",
            zone="us-east5-b",
            region="us-east5",
            status="RUNNING",
            internal_ip="10.0.0.11",
            external_ip=None,
            ssh_via_iap=True,
        )
        transport = MagicMock()
        transport.build_ssh_command.return_value = ["provider-ssh", "target"]
        mock_transport_for_provider.return_value = transport

        cmd = build_ssh_command(target, remote_command=["echo", "hi"], tty=True)

        assert cmd == ["provider-ssh", "target"]
        mock_transport_for_provider.assert_called_once_with("gce")
        transport.build_ssh_command.assert_called_once_with(
            target,
            remote_command=["echo", "hi"],
            tty=True,
        )

    @patch("crsbench.cloud.cli._remote_access.transport_for_provider")
    def test_build_serial_command_delegates_to_provider_transport(
        self,
        mock_transport_for_provider,
    ):
        from crsbench.cloud.cli._instance_inventory import CloudInstanceInventoryRow
        from crsbench.cloud.cli._remote_access import build_serial_command

        target = CloudInstanceInventoryRow(
            alias="work-001",
            name="crsbench-test-exp-work-001",
            role="worker",
            placement_source="config",
            provider="gce",
            project="test-project",
            zone="us-east5-b",
            region="us-east5",
            status="RUNNING",
            internal_ip="10.0.0.11",
            external_ip=None,
            ssh_via_iap=True,
        )
        transport = MagicMock()
        transport.build_serial_command.return_value = ["provider-serial", "target"]
        mock_transport_for_provider.return_value = transport

        cmd = build_serial_command(target, port=2)

        assert cmd == ["provider-serial", "target"]
        mock_transport_for_provider.assert_called_once_with("gce")
        transport.build_serial_command.assert_called_once_with(
            target,
            port=2,
        )

    def test_gce_transport_build_serial_command_uses_gcloud_serial_console(self):
        from crsbench.cloud.gce.transport import GceCloudTransport

        target = _make_inventory_row(
            alias="work-001",
            name="crsbench-test-exp-work-001",
            role="worker",
            zone="us-east5-b",
        )

        cmd = GceCloudTransport().build_serial_command(target, port=2)

        assert cmd == [
            "gcloud",
            "compute",
            "connect-to-serial-port",
            "crsbench-test-exp-work-001",
            "--project=test-project",
            "--zone=us-east5-b",
            "--port=2",
        ]


class TestExec:
    """Tests for run_exec() sub-action."""

    def test_resolve_exec_request_uses_short_form_selector(self):
        from crsbench.cloud.cli._exec import _resolve_exec_request

        rows = [
            _make_inventory_row(
                alias="eval-central-eval-001",
                name="crsbench-test-exp-eval-central-eval-001",
                role="evaluator",
            )
        ]
        args = SimpleNamespace(exec_args=["eval-001", "--", "hostname"])

        selector, exec_command = _resolve_exec_request(args, rows)

        assert selector == "eval-001"
        assert exec_command == ["hostname"]

    def test_resolve_exec_request_keeps_ambiguous_selector_as_selector(self):
        from crsbench.cloud.cli._exec import _resolve_exec_request

        rows = [
            _make_inventory_row(
                alias="eval-east-001",
                name="crsbench-test-exp-eval-east-001",
                role="evaluator",
            ),
            _make_inventory_row(
                alias="eval-west-002",
                name="crsbench-test-exp-eval-west-002",
                role="evaluator",
            ),
        ]
        args = SimpleNamespace(exec_args=["eval", "--", "hostname"])

        selector, exec_command = _resolve_exec_request(args, rows)

        assert selector == "eval"
        assert exec_command == ["hostname"]

    @patch("crsbench.cloud.cli._exec.subprocess.run")
    @patch("crsbench.cloud.cli._exec.build_ssh_command")
    @patch("crsbench.cloud.cli._exec.resolve_effective_experiment_name")
    @patch("crsbench.cloud.cli._exec.resolve_cloud_context")
    @patch("crsbench.cloud.cli._exec.provisioner_for_context")
    def test_exec_runs_remote_command_on_selected_instance(
        self,
        mock_provisioner_cls,
        mock_resolve_context,
        mock_resolve_experiment_name,
        mock_build_ssh_command,
        mock_run,
    ):
        mock_resolve_experiment_name.return_value = "test-exp"
        context = _make_resolved_cloud_context(_make_launch_state())
        mock_resolve_context.return_value = context
        provisioner = mock_provisioner_cls.return_value
        provisioner.list_workers.return_value = [
            _make_gce_worker("crsbench-test-exp-work-001", zone="us-central1-a")
        ]
        provisioner.list_evaluators.return_value = []
        provisioner.get_instance_record.return_value = _make_gce_worker(
            "crsbench-test-exp-orch",
            zone="us-east5-b",
            ip="10.0.0.50",
        )
        provisioner.get_instance_record.return_value.labels["crsbench-role"] = (
            "orchestrator"
        )
        mock_build_ssh_command.return_value = [
            "gcloud",
            "compute",
            "ssh",
            "crsbench-test-exp-work-001",
            "--zone=us-central1-a",
            "--command=echo hi",
        ]
        mock_run.return_value = _make_completed_process(0)

        from crsbench.cloud.cli._exec import run_exec

        rc = run_exec(_make_exec_args(exec_command=["echo", "hi"]))

        assert rc == 0
        mock_build_ssh_command.assert_called_once_with(
            mock.ANY,
            remote_command=["echo", "hi"],
            tty=False,
        )
        cmd = mock_run.call_args.args[0]
        assert cmd == mock_build_ssh_command.return_value

    @patch("crsbench.cloud.cli._exec.resolve_effective_experiment_name")
    def test_exec_requires_command(self, mock_resolve_experiment_name):
        mock_resolve_experiment_name.return_value = "test-exp"

        from crsbench.cloud.cli._exec import run_exec

        rc = run_exec(_make_exec_args(exec_command=[]))

        assert rc == 2

    @patch("crsbench.cloud.cli._exec.subprocess.run")
    @patch("crsbench.cloud.cli._exec.build_ssh_command")
    @patch("crsbench.cloud.cli._exec.resolve_effective_experiment_name")
    @patch("crsbench.cloud.cli._exec.resolve_cloud_context")
    @patch("crsbench.cloud.cli._exec.provisioner_for_context")
    def test_exec_treats_keyboard_interrupt_as_normal_exit(
        self,
        mock_provisioner_cls,
        mock_resolve_context,
        mock_resolve_experiment_name,
        mock_build_ssh_command,
        mock_run,
    ):
        mock_resolve_experiment_name.return_value = "test-exp"
        context = _make_resolved_cloud_context(_make_launch_state())
        mock_resolve_context.return_value = context
        provisioner = mock_provisioner_cls.return_value
        provisioner.list_workers.return_value = [
            _make_gce_worker("crsbench-test-exp-work-001", zone="us-central1-a")
        ]
        provisioner.list_evaluators.return_value = []
        provisioner.get_instance_record.return_value = _make_gce_worker(
            "crsbench-test-exp-orch",
            zone="us-east5-b",
            ip="10.0.0.50",
        )
        provisioner.get_instance_record.return_value.labels["crsbench-role"] = (
            "orchestrator"
        )
        mock_build_ssh_command.return_value = ["gcloud", "compute", "ssh", "dummy"]
        mock_run.side_effect = KeyboardInterrupt

        from crsbench.cloud.cli._exec import run_exec

        rc = run_exec(_make_exec_args(exec_command=["tail", "-f"]))

        assert rc == 130


class TestLog:
    """Tests for run_log() sub-action."""

    def test_resolve_log_targets_supports_role_and_explicit_selector_union(self):
        from crsbench.cloud.cli._log import _resolve_log_targets

        rows = [
            _make_inventory_row(
                alias="orch",
                name="crsbench-test-exp-orch",
                role="orchestrator",
            ),
            _make_inventory_row(
                alias="work-001",
                name="crsbench-test-exp-work-001",
                role="worker",
            ),
            _make_inventory_row(
                alias="work-002",
                name="crsbench-test-exp-work-002",
                role="worker",
            ),
            _make_inventory_row(
                alias="eval-001",
                name="crsbench-test-exp-eval-001",
                role="evaluator",
            ),
        ]

        targets = _resolve_log_targets(
            rows,
            _make_log_args(
                instance="orch",
                instances=["eval-001"],
                role="worker",
            ),
        )

        assert targets == [rows[1], rows[2], rows[0], rows[3]]

    def test_resolve_log_targets_deduplicates_all_role_and_explicit_selectors(self):
        from crsbench.cloud.cli._log import _resolve_log_targets

        rows = [
            _make_inventory_row(
                alias="orch",
                name="crsbench-test-exp-orch",
                role="orchestrator",
            ),
            _make_inventory_row(
                alias="work-001",
                name="crsbench-test-exp-work-001",
                role="worker",
            ),
            _make_inventory_row(
                alias="work-002",
                name="crsbench-test-exp-work-002",
                role="worker",
            ),
            _make_inventory_row(
                alias="eval-001",
                name="crsbench-test-exp-eval-001",
                role="evaluator",
            ),
        ]

        targets = _resolve_log_targets(
            rows,
            _make_log_args(
                instance="orch",
                instances=["work-001"],
                role="worker",
                all_instances=True,
            ),
        )

        assert targets == rows

    def test_resolve_log_targets_rejects_ambiguous_explicit_selector(
        self,
        capsys,
    ):
        from crsbench.cloud.cli._log import _resolve_log_targets

        rows = [
            _make_inventory_row(
                alias="work-001",
                name="crsbench-test-exp-work-001",
                role="worker",
            ),
            _make_inventory_row(
                alias="work-002",
                name="crsbench-test-exp-work-002",
                role="worker",
            ),
        ]

        targets = _resolve_log_targets(
            rows,
            _make_log_args(instance=None, instances=["work"]),
        )

        assert targets is None
        out = capsys.readouterr().out
        assert "crsbench-test-exp-work-001" in out
        assert "crsbench-test-exp-work-002" in out

    @patch("crsbench.cloud.cli._log.subprocess.run")
    @patch("crsbench.cloud.cli._log.build_ssh_command")
    @patch("crsbench.cloud.cli._log.resolve_effective_experiment_name")
    @patch("crsbench.cloud.cli._log.resolve_cloud_context")
    @patch("crsbench.cloud.cli._log.provisioner_for_context")
    def test_log_uses_worker_service_unit(
        self,
        mock_provisioner_cls,
        mock_resolve_context,
        mock_resolve_experiment_name,
        mock_build_ssh_command,
        mock_run,
    ):
        from crsbench.cloud.cli._log import run_log

        mock_resolve_experiment_name.return_value = "test-exp"
        context = _make_resolved_cloud_context(_make_launch_state())
        mock_resolve_context.return_value = context
        provisioner = mock_provisioner_cls.return_value
        provisioner.list_workers.return_value = [
            _make_gce_worker("crsbench-test-exp-work-001", zone="us-central1-a")
        ]
        provisioner.list_evaluators.return_value = []
        provisioner.get_instance_record.return_value = _make_gce_worker(
            "crsbench-test-exp-orch",
            zone="us-east5-b",
            ip="10.0.0.50",
        )
        provisioner.get_instance_record.return_value.labels["crsbench-role"] = (
            "orchestrator"
        )
        mock_build_ssh_command.return_value = ["gcloud", "compute", "ssh", "dummy"]
        mock_run.return_value = _make_completed_process(0)
        rc = run_log(_make_log_args(instance="work-001"))

        assert rc == 0
        remote_command = mock_build_ssh_command.call_args.kwargs["remote_command"]
        assert "crsbench-worker.service" in " ".join(remote_command)

    @patch("crsbench.cloud.cli._log.subprocess.run")
    @patch("crsbench.cloud.cli._log.build_ssh_command")
    @patch("crsbench.cloud.cli._log.resolve_effective_experiment_name")
    @patch("crsbench.cloud.cli._log.resolve_cloud_context")
    @patch("crsbench.cloud.cli._log.provisioner_for_context")
    def test_log_uses_orchestrator_service_unit(
        self,
        mock_provisioner_cls,
        mock_resolve_context,
        mock_resolve_experiment_name,
        mock_build_ssh_command,
        mock_run,
    ):
        from crsbench.cloud.cli._log import run_log

        mock_resolve_experiment_name.return_value = "test-exp"
        context = _make_resolved_cloud_context(_make_launch_state())
        mock_resolve_context.return_value = context
        provisioner = mock_provisioner_cls.return_value
        provisioner.list_workers.return_value = []
        provisioner.list_evaluators.return_value = []
        provisioner.get_instance_record.return_value = _make_gce_worker(
            "crsbench-test-exp-orch",
            zone="us-east5-b",
            ip="10.0.0.50",
        )
        provisioner.get_instance_record.return_value.labels["crsbench-role"] = (
            "orchestrator"
        )
        mock_build_ssh_command.return_value = ["gcloud", "compute", "ssh", "dummy"]
        mock_run.return_value = _make_completed_process(0)
        rc = run_log(_make_log_args(instance="orch"))

        assert rc == 0
        remote_command = mock_build_ssh_command.call_args.kwargs["remote_command"]
        assert "crsbench-orchestrator.service" in " ".join(remote_command)

    @patch("crsbench.cloud.cli._log._run_multi_target_log_session", return_value=0)
    @patch("crsbench.cloud.cli._log.resolve_effective_experiment_name")
    @patch("crsbench.cloud.cli._log.resolve_cloud_context")
    @patch("crsbench.cloud.cli._log.provisioner_for_context")
    def test_log_dispatches_explicit_multi_target_mode(
        self,
        mock_provisioner_cls,
        mock_resolve_context,
        mock_resolve_experiment_name,
        mock_run_multi_target,
    ):
        from crsbench.cloud.cli._log import run_log

        mock_resolve_experiment_name.return_value = "test-exp"
        context = _make_resolved_cloud_context(_make_launch_state())
        mock_resolve_context.return_value = context
        provisioner = mock_provisioner_cls.return_value
        provisioner.list_workers.return_value = [
            _make_gce_worker("crsbench-test-exp-work-001", zone="us-central1-a"),
            _make_gce_worker("crsbench-test-exp-work-002", zone="us-central1-b"),
        ]
        provisioner.list_evaluators.return_value = []
        provisioner.get_instance_record.return_value = _make_gce_worker(
            "crsbench-test-exp-orch",
            zone="us-east5-b",
            ip="10.0.0.50",
        )
        provisioner.get_instance_record.return_value.labels["crsbench-role"] = (
            "orchestrator"
        )

        rc = run_log(
            _make_log_args(
                instance="orch",
                role="worker",
                merge_by="timestamp",
            )
        )

        assert rc == 0
        mock_run_multi_target.assert_called_once()
        targets = mock_run_multi_target.call_args.args[0]
        assert [target.alias for target in targets] == ["work-001", "work-002", "orch"]
        assert mock_run_multi_target.call_args.kwargs["merge_by"] == "timestamp"

    def test_multi_target_log_session_renders_prefixed_output(
        self, monkeypatch, capsys
    ):
        from crsbench.cloud.cli._log import _run_multi_target_log_session

        targets = [
            _make_inventory_row(
                alias="orch",
                name="crsbench-test-exp-orch",
                role="orchestrator",
            ),
            _make_inventory_row(
                alias="work-001",
                name="crsbench-test-exp-work-001",
                role="worker",
            ),
        ]

        commands: list[list[str]] = []

        class _FakePopen:
            def __init__(
                self,
                stdout_text: str,
                stderr_text: str = "",
                returncode: int = 0,
            ):
                self.stdout = io.StringIO(stdout_text)
                self.stderr = io.StringIO(stderr_text)
                self._returncode = returncode

            def wait(self):
                return self._returncode

            def poll(self):
                return self._returncode

            def terminate(self):
                self._returncode = 0

            def kill(self):
                self._returncode = -9

        def _fake_build_ssh_command(target, *, remote_command=None, tty=False):
            assert tty is False
            commands.append(remote_command or [])
            return [target.alias]

        def _fake_popen(cmd, **_kwargs):
            alias = cmd[0]
            if alias == "orch":
                return _FakePopen(
                    '{"__REALTIME_TIMESTAMP":"1000000","MESSAGE":"orch line"}\n'
                )
            return _FakePopen(
                '{"__REALTIME_TIMESTAMP":"2000000","MESSAGE":"worker line\\ntrace"}\n'
            )

        monkeypatch.setattr(
            "crsbench.cloud.cli._log.build_ssh_command",
            _fake_build_ssh_command,
        )
        monkeypatch.setattr("crsbench.cloud.cli._log.subprocess.Popen", _fake_popen)

        rc = _run_multi_target_log_session(targets, merge_by="arrival")

        assert rc == 0
        out = capsys.readouterr().out
        assert "orch | orchestrator | journal | orch line" in out
        assert "work-001 | worker | journal | worker line" in out
        assert "work-001 | worker | journal | trace" in out
        assert all(" -o json" in " ".join(command) for command in commands)

    def test_multi_target_log_session_orders_timestamp_merge(
        self,
        monkeypatch,
        capsys,
    ):
        from crsbench.cloud.cli import _log as log_module

        targets = [
            _make_inventory_row(
                alias="orch",
                name="crsbench-test-exp-orch",
                role="orchestrator",
            ),
            _make_inventory_row(
                alias="work-001",
                name="crsbench-test-exp-work-001",
                role="worker",
            ),
        ]
        worker_done = threading.Event()

        def _fake_stream_target_logs(
            target,
            event_queue,
            stop_event,
            active_processes,
            active_processes_lock,
        ):
            del stop_event, active_processes, active_processes_lock
            if target.alias == "work-001":
                event_queue.put(
                    log_module._LogStreamEvent(kind="stream_started", target=target)
                )
                event_queue.put(
                    log_module._LogStreamEvent(
                        kind="journal_record",
                        target=target,
                        message="worker second",
                        timestamp=1.0,
                    )
                )
                event_queue.put(
                    log_module._LogStreamEvent(
                        kind="target_done",
                        target=target,
                        state="detached",
                    )
                )
                worker_done.set()
                return

            assert worker_done.wait(timeout=1.0)
            event_queue.put(
                log_module._LogStreamEvent(kind="stream_started", target=target)
            )
            event_queue.put(
                log_module._LogStreamEvent(
                    kind="journal_record",
                    target=target,
                    message="orch first",
                    timestamp=0.5,
                )
            )
            event_queue.put(
                log_module._LogStreamEvent(
                    kind="target_done",
                    target=target,
                    state="detached",
                )
            )

        monkeypatch.setattr(
            log_module,
            "_stream_target_logs",
            _fake_stream_target_logs,
        )

        rc = log_module._run_multi_target_log_session(targets, merge_by="timestamp")

        assert rc == 0
        lines = [line for line in capsys.readouterr().out.splitlines() if line]
        assert lines[0] == "orch | orchestrator | journal | orch first"
        assert lines[1] == "work-001 | worker | journal | worker second"

    def test_multi_target_log_session_retries_failed_target_without_stalling_others(
        self,
        monkeypatch,
        capsys,
    ):
        from crsbench.cloud.cli._log import _run_multi_target_log_session

        targets = [
            _make_inventory_row(
                alias="orch",
                name="crsbench-test-exp-orch",
                role="orchestrator",
            ),
            _make_inventory_row(
                alias="work-001",
                name="crsbench-test-exp-work-001",
                role="worker",
            ),
        ]

        class _FakePopen:
            def __init__(
                self,
                stdout_text: str = "",
                stderr_text: str = "",
                returncode: int = 0,
            ):
                self.stdout = io.StringIO(stdout_text)
                self.stderr = io.StringIO(stderr_text)
                self._returncode = returncode

            def wait(self):
                return self._returncode

            def poll(self):
                return self._returncode

            def terminate(self):
                self._returncode = 0

            def kill(self):
                self._returncode = -9

        attempts = {"orch": 0, "work-001": 0}

        def _fake_build_ssh_command(target, *, remote_command=None, tty=False):
            assert tty is False
            assert remote_command is not None
            return [target.alias]

        def _fake_popen(cmd, **_kwargs):
            alias = cmd[0]
            attempts[alias] += 1
            if alias == "orch" and attempts[alias] == 1:
                return _FakePopen(stderr_text="transport boom\n", returncode=255)
            if alias == "orch":
                return _FakePopen(
                    '{"__REALTIME_TIMESTAMP":"1000000","MESSAGE":"orch recovered"}\n'
                )
            return _FakePopen(
                '{"__REALTIME_TIMESTAMP":"2000000","MESSAGE":"worker steady"}\n'
            )

        monkeypatch.setattr(
            "crsbench.cloud.cli._log.build_ssh_command",
            _fake_build_ssh_command,
        )
        monkeypatch.setattr("crsbench.cloud.cli._log.subprocess.Popen", _fake_popen)
        monkeypatch.setattr("crsbench.cloud.cli._log._FAN_IN_RETRY_DELAY_SEC", 0.0)
        monkeypatch.setattr("crsbench.cloud.cli._log._FAN_IN_MAX_RETRIES", 1)

        rc = _run_multi_target_log_session(targets, merge_by="arrival")

        assert rc == 0
        out = capsys.readouterr().out
        assert "orch | orchestrator | transport | transport boom" in out
        assert (
            "orch | orchestrator | control | log stream exited with code 255; "
            "retrying in 0.0s (1/1)" in out
        )
        assert "work-001 | worker | journal | worker steady" in out
        assert "orch | orchestrator | journal | orch recovered" in out
        assert attempts == {"orch": 2, "work-001": 1}

    def test_multi_target_log_session_returns_error_when_no_stream_starts(
        self,
        monkeypatch,
        capsys,
    ):
        from crsbench.cloud.cli._log import _run_multi_target_log_session

        targets = [
            _make_inventory_row(
                alias="work-001",
                name="crsbench-test-exp-work-001",
                role="worker",
            )
        ]

        class _FakePopen:
            def __init__(
                self,
                stdout_text: str = "",
                stderr_text: str = "",
                returncode: int = 255,
            ):
                self.stdout = io.StringIO(stdout_text)
                self.stderr = io.StringIO(stderr_text)
                self._returncode = returncode

            def wait(self):
                return self._returncode

            def poll(self):
                return self._returncode

            def terminate(self):
                self._returncode = 0

            def kill(self):
                self._returncode = -9

        def _fake_build_ssh_command(target, *, remote_command=None, tty=False):
            assert tty is False
            assert remote_command is not None
            return [target.alias]

        def _fake_popen(_cmd, **_kwargs):
            return _FakePopen(stderr_text="transport boom\n")

        monkeypatch.setattr(
            "crsbench.cloud.cli._log.build_ssh_command",
            _fake_build_ssh_command,
        )
        monkeypatch.setattr(
            "crsbench.cloud.cli._log.subprocess.Popen",
            _fake_popen,
        )

        rc = _run_multi_target_log_session(targets, merge_by="arrival")

        assert rc == 1
        out = capsys.readouterr().out
        assert "work-001 | worker | transport | transport boom" in out

    @patch("crsbench.cloud.cli._log.subprocess.run")
    @patch("crsbench.cloud.cli._log.build_ssh_command")
    @patch("crsbench.cloud.cli._log.resolve_effective_experiment_name")
    @patch("crsbench.cloud.cli._log.resolve_cloud_context")
    @patch("crsbench.cloud.cli._log.provisioner_for_context")
    def test_log_treats_keyboard_interrupt_as_normal_exit(
        self,
        mock_provisioner_cls,
        mock_resolve_context,
        mock_resolve_experiment_name,
        mock_build_ssh_command,
        mock_run,
    ):
        from crsbench.cloud.cli._log import run_log

        mock_resolve_experiment_name.return_value = "test-exp"
        context = _make_resolved_cloud_context(_make_launch_state())
        mock_resolve_context.return_value = context
        provisioner = mock_provisioner_cls.return_value
        provisioner.list_workers.return_value = [
            _make_gce_worker("crsbench-test-exp-work-001", zone="us-central1-a")
        ]
        provisioner.list_evaluators.return_value = []
        provisioner.get_instance_record.return_value = _make_gce_worker(
            "crsbench-test-exp-orch",
            zone="us-east5-b",
            ip="10.0.0.50",
        )
        provisioner.get_instance_record.return_value.labels["crsbench-role"] = (
            "orchestrator"
        )
        mock_build_ssh_command.return_value = ["gcloud", "compute", "ssh", "dummy"]
        mock_run.side_effect = KeyboardInterrupt

        rc = run_log(_make_log_args(instance="work-001"))

        assert rc == 130


class TestCollect:
    """Tests for run_collect() sub-action."""

    def test_timestamp_directory_format_uses_utc_minute_precision(self):
        from crsbench.cloud.cli._collect import _format_collect_timestamp

        assert (
            _format_collect_timestamp("2026-03-21T21:45:59+00:00") == "2026-03-21-21-45"
        )

    def test_fresh_timestamp_destination_reserves_unique_siblings(
        self,
        tmp_path: Path,
    ):
        experiment_filestore = tmp_path / "filestore"
        experiment_filestore.mkdir()

        with patch(
            "crsbench.cloud.cli._collect._format_collect_timestamp",
            return_value="2026-03-21-21-45",
        ):
            from crsbench.cloud.cli._collect import _fresh_timestamp_destination

            first = _fresh_timestamp_destination(experiment_filestore, "test-exp")
            second = _fresh_timestamp_destination(experiment_filestore, "test-exp")

        assert first == experiment_filestore / "test-exp-2026-03-21-21-45"
        assert second == experiment_filestore / "test-exp-2026-03-21-21-45-02"
        assert first.is_dir()
        assert second.is_dir()

    @patch("crsbench.cloud.cli._collect.reconnect")
    @patch("crsbench.cloud.cli._collect.provisioner_for_context")
    @patch("crsbench.cloud.cli._collect.ArtifactCollector")
    @patch("crsbench.cloud.cli._collect.resolve_cloud_context")
    @patch("crsbench.cloud.cli._collect.resolve_effective_experiment_name")
    def test_collect_resolves_provisioner_from_context(
        self,
        mock_resolve_experiment_name,
        mock_resolve_context,
        mock_collector_cls,
        mock_provisioner_for_context,
        mock_reconnect,
    ):
        mock_resolve_experiment_name.return_value = "test-exp"
        context = _make_resolved_cloud_context(_make_launch_state())
        mock_resolve_context.return_value = context
        mock_reconnect.side_effect = RuntimeError("redis unavailable")
        provisioner = MagicMock()
        provisioner.get_instance_record.return_value = None
        provisioner.list_workers.return_value = []
        provisioner.list_evaluators.return_value = []
        mock_provisioner_for_context.return_value = provisioner
        mock_collector_cls.return_value = MagicMock()

        from crsbench.cloud.cli._collect import run_collect

        rc = run_collect(_make_collect_args())

        assert rc == 0
        mock_provisioner_for_context.assert_called_once_with(context)

    @patch("crsbench.cloud.cli._collect.resolve_effective_experiment_name")
    @patch("crsbench.cloud.cli._collect.resolve_cloud_context")
    @patch("crsbench.cloud.cli._collect.ArtifactCollector")
    @patch("crsbench.cloud.cli._collect.provisioner_for_context")
    @patch("crsbench.cloud.cli._collect.reconnect")
    def test_collect_infers_experiment_and_remote_dir_from_config(
        self,
        mock_reconnect,
        mock_prov_cls,
        mock_coll_cls,
        mock_resolve_context,
        mock_resolve_experiment_name,
    ):
        workers = [_make_gce_worker("w-1")]
        mock_resolve_experiment_name.return_value = "test-exp"
        mock_prov = MagicMock()
        mock_prov.list_workers.return_value = workers
        mock_prov_cls.return_value = mock_prov
        mock_resolve_context.return_value = _make_collect_context(
            experiment_filestore=Path("/tmp/remote-root/scoped-filestore"),
            remote_experiment_root=Path("/tmp/remote-root"),
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
            Path("/tmp/remote-root/scoped-filestore"),
        )

        from crsbench.cloud.cli._collect import run_collect

        rc = run_collect(_make_collect_args(experiment=None, remote_dir=None))

        assert rc == 0
        mock_resolve_context.assert_called_once_with("/tmp/config.yaml", "test-exp")
        mock_coll.collect.assert_called_once()
        assert (
            mock_coll.collect.call_args.kwargs["remote_experiment_dir"]
            == "/tmp/remote-root/scoped-filestore/test-exp"
        )

    @patch("crsbench.cloud.cli._collect.resolve_cloud_context")
    @patch("crsbench.cloud.cli._collect.ArtifactCollector")
    @patch("crsbench.cloud.cli._collect.provisioner_for_context")
    @patch("crsbench.cloud.cli._collect.reconnect")
    def test_collect_uses_config_scoped_collector_and_collects_logs(
        self, mock_reconnect, mock_prov_cls, mock_coll_cls, mock_resolve_context
    ):
        """Collection should use the config path for SSH/log state and fetch logs per worker."""
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
            MagicMock(),
            MagicMock(),
            readiness,
            MagicMock(),
            Path("/tmp/filestore"),
        )

        from crsbench.cloud.cli._collect import run_collect

        rc = run_collect(_make_collect_args())

        assert rc == 0
        mock_coll_cls.assert_called_once_with(base_path="/tmp/config.yaml")
        assert mock_coll.collect_logs.call_count == 2
        assert mock_coll.collect.call_count == 2

    @patch("crsbench.cloud.cli._collect.resolve_cloud_context")
    @patch("crsbench.cloud.cli._collect.ArtifactCollector")
    @patch("crsbench.cloud.cli._collect.provisioner_for_context")
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
    @patch("crsbench.cloud.cli._collect.ArtifactCollector")
    @patch("crsbench.cloud.cli._collect.provisioner_for_context")
    @patch("crsbench.cloud.cli._collect.reconnect")
    def test_collect_reeval_mode_collects_orchestrator_artifacts_without_workers(
        self, mock_reconnect, mock_prov_cls, mock_coll_cls, mock_resolve_context
    ):
        """Cloud re-eval collection should pull the authoritative result tree from the orchestrator."""
        launch_state = _make_reeval_launch_state()
        mock_resolve_context.return_value = _make_collect_context(
            experiment_filestore=Path("/tmp/filestore"),
            remote_experiment_root=Path(launch_state.remote_experiment_root),
            launch_state=launch_state,
        )
        mock_prov = MagicMock()
        mock_prov.list_workers.return_value = []
        mock_prov_cls.return_value = mock_prov

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

        rc = run_collect(
            _make_collect_args(
                experiment="test-exp",
                remote_dir=None,
            )
        )

        assert rc == 0
        assert mock_coll.collect.call_count == 1
        assert mock_coll.collect_logs.call_count == 1
        assert (
            mock_coll.collect.call_args.kwargs["worker"].name
            == launch_state.orchestrator_name
        )
        assert (
            mock_coll.collect.call_args.kwargs["remote_experiment_dir"]
            == f"{launch_state.remote_experiment_root}/{launch_state.experiment_name}"
        )
        mock_coll.collect_reeval_submission_artifacts.assert_called_once_with(
            worker=mock.ANY,
            fleet=launch_state.as_transport_config(),
            experiment_name=launch_state.effective_remote_experiment_name(),
            experiment_filestore=Path("/tmp/filestore"),
            remote_submission_dir=launch_state.remote_submission_dir,
            destination=Path("/tmp/filestore")
            / launch_state.effective_remote_experiment_name(),
        )

    @patch("crsbench.cloud.cli._collect.resolve_cloud_context")
    @patch("crsbench.cloud.cli._collect.ArtifactCollector")
    @patch("crsbench.cloud.cli._collect.provisioner_for_context")
    @patch("crsbench.cloud.cli._collect.reconnect")
    def test_collect_reeval_mode_skips_stale_missing_orchestrator(
        self, mock_reconnect, mock_prov_cls, mock_coll_cls, mock_resolve_context
    ):
        launch_state = _make_reeval_launch_state()
        mock_resolve_context.return_value = _make_collect_context(
            experiment_filestore=Path("/tmp/filestore"),
            remote_experiment_root=Path(launch_state.remote_experiment_root),
            launch_state=launch_state,
        )
        mock_prov = MagicMock()
        mock_prov.list_workers.return_value = []
        mock_prov.get_instance_record.side_effect = RuntimeError("instance not found")
        mock_prov_cls.return_value = mock_prov

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

        rc = run_collect(_make_collect_args(experiment="test-exp", remote_dir=None))

        assert rc == 0
        mock_coll.collect.assert_not_called()
        mock_coll.collect_logs.assert_not_called()

    @patch("crsbench.cloud.cli._collect.resolve_cloud_context")
    @patch("crsbench.cloud.cli._collect.ArtifactCollector")
    @patch("crsbench.cloud.cli._collect.provisioner_for_context")
    @patch("crsbench.cloud.cli._collect.reconnect")
    def test_collect_runs_live_instance_collection_in_parallel(
        self,
        mock_reconnect,
        mock_prov_cls,
        mock_coll_cls,
        mock_resolve_context,
    ):
        """Two live workers should enter collection concurrently instead of serializing."""
        workers = [_make_gce_worker("w-1"), _make_gce_worker("w-2")]
        mock_prov = MagicMock()
        mock_prov.list_workers.return_value = workers
        mock_prov_cls.return_value = mock_prov
        mock_resolve_context.return_value = _make_resolved_cloud_context()

        barrier = threading.Barrier(2, timeout=1.0)
        mock_coll = MagicMock()
        mock_coll.collect_logs.side_effect = lambda **_kwargs: barrier.wait()
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
        assert mock_coll.collect_logs.call_count == 2
        assert mock_coll.collect.call_count == 2

    def test_collect_existing_destination_requires_force_when_noninteractive(
        self,
        tmp_path: Path,
    ):
        experiment_filestore = tmp_path / "filestore"
        (experiment_filestore / "test-exp").mkdir(parents=True)
        with (
            patch(
                "crsbench.cloud.cli._collect.resolve_cloud_context"
            ) as mock_resolve_context,
            patch("crsbench.cloud.cli._collect.ArtifactCollector") as mock_coll_cls,
            patch(
                "crsbench.cloud.cli._collect.provisioner_for_context"
            ) as mock_prov_cls,
            patch("crsbench.cloud.cli._collect.reconnect") as mock_reconnect,
            patch("sys.stdin.isatty", return_value=False),
        ):
            mock_prov = MagicMock()
            mock_prov.list_workers.return_value = [_make_gce_worker("w-1")]
            mock_prov_cls.return_value = mock_prov
            mock_resolve_context.return_value = _make_collect_context(
                experiment_filestore=experiment_filestore,
                remote_experiment_root=tmp_path / "remote-root",
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
                experiment_filestore,
            )

            from crsbench.cloud.cli._collect import run_collect

            rc = run_collect(_make_collect_args(force=False))

        assert rc == 1
        mock_coll.collect.assert_not_called()

    def test_collect_existing_destination_with_no_live_instances_returns_zero(
        self,
        tmp_path: Path,
    ):
        experiment_filestore = tmp_path / "filestore"
        (experiment_filestore / "test-exp").mkdir(parents=True)
        with (
            patch(
                "crsbench.cloud.cli._collect.resolve_cloud_context"
            ) as mock_resolve_context,
            patch("crsbench.cloud.cli._collect.ArtifactCollector") as mock_coll_cls,
            patch(
                "crsbench.cloud.cli._collect.provisioner_for_context"
            ) as mock_prov_cls,
            patch("crsbench.cloud.cli._collect.reconnect") as mock_reconnect,
            patch("sys.stdin.isatty", return_value=False),
        ):
            mock_prov = MagicMock()
            mock_prov.list_workers.return_value = []
            mock_prov_cls.return_value = mock_prov
            mock_resolve_context.return_value = _make_collect_context(
                experiment_filestore=experiment_filestore,
                remote_experiment_root=tmp_path / "remote-root",
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
                experiment_filestore,
            )

            from crsbench.cloud.cli._collect import run_collect

            rc = run_collect(_make_collect_args(force=False))

        assert rc == 0
        mock_coll.collect.assert_not_called()

    def test_collect_existing_destination_accepts_empty_interactive_confirmation(
        self,
        tmp_path: Path,
    ):
        experiment_filestore = tmp_path / "filestore"
        (experiment_filestore / "test-exp").mkdir(parents=True)
        with (
            patch(
                "crsbench.cloud.cli._collect.resolve_cloud_context"
            ) as mock_resolve_context,
            patch("crsbench.cloud.cli._collect.ArtifactCollector") as mock_coll_cls,
            patch(
                "crsbench.cloud.cli._collect.provisioner_for_context"
            ) as mock_prov_cls,
            patch("crsbench.cloud.cli._collect.reconnect") as mock_reconnect,
            patch("sys.stdin.isatty", return_value=True),
            patch("builtins.input", return_value="") as mock_input,
        ):
            mock_prov = MagicMock()
            mock_prov.list_workers.return_value = [_make_gce_worker("w-1")]
            mock_prov_cls.return_value = mock_prov
            mock_resolve_context.return_value = _make_collect_context(
                experiment_filestore=experiment_filestore,
                remote_experiment_root=tmp_path / "remote-root",
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
                experiment_filestore,
            )

            from crsbench.cloud.cli._collect import run_collect

            rc = run_collect(_make_collect_args(force=False))

        assert rc == 0
        mock_input.assert_called()

    def test_collect_timestamp_flag_uses_fresh_sibling_destination(
        self,
        tmp_path: Path,
    ):
        experiment_filestore = tmp_path / "filestore"
        base_destination = experiment_filestore / "test-exp"
        timestamp_destination = experiment_filestore / "test-exp-2026-03-21-17-45"
        base_destination.mkdir(parents=True)
        with (
            patch(
                "crsbench.cloud.cli._collect.resolve_cloud_context"
            ) as mock_resolve_context,
            patch("crsbench.cloud.cli._collect.ArtifactCollector") as mock_coll_cls,
            patch(
                "crsbench.cloud.cli._collect.provisioner_for_context"
            ) as mock_prov_cls,
            patch("crsbench.cloud.cli._collect.reconnect") as mock_reconnect,
            patch(
                "crsbench.cloud.cli._collect._fresh_timestamp_destination",
                return_value=timestamp_destination,
            ) as mock_timestamp_destination,
            patch("builtins.input") as mock_input,
        ):
            mock_prov = MagicMock()
            mock_prov.list_workers.return_value = [_make_gce_worker("w-1")]
            mock_prov_cls.return_value = mock_prov
            mock_resolve_context.return_value = _make_collect_context(
                experiment_filestore=experiment_filestore,
                remote_experiment_root=tmp_path / "remote-root",
            )

            mock_coll = MagicMock()

            def _collect_side_effect(**kwargs):
                kwargs["destination"].mkdir(parents=True, exist_ok=True)
                return kwargs["destination"]

            mock_coll.collect.side_effect = _collect_side_effect
            mock_coll_cls.return_value = mock_coll

            readiness = MagicMock()
            readiness.list_workers.return_value = []
            mock_reconnect.return_value = (
                MagicMock(),
                MagicMock(),
                readiness,
                MagicMock(),
                experiment_filestore,
            )

            from crsbench.cloud.cli._collect import run_collect

            rc = run_collect(_make_collect_args(timestamp=True))

        assert rc == 0
        mock_input.assert_not_called()
        mock_timestamp_destination.assert_called_once_with(
            experiment_filestore,
            "test-exp",
        )
        assert (
            mock_coll.collect.call_args.kwargs["destination"] == timestamp_destination
        )
        marker = read_collect_marker(timestamp_destination)
        assert marker is not None
        assert marker["local_destination"] == str(timestamp_destination)

    def test_collect_existing_destination_timestamp_prompt_selects_sibling(
        self,
        tmp_path: Path,
    ):
        experiment_filestore = tmp_path / "filestore"
        base_destination = experiment_filestore / "test-exp"
        timestamp_destination = experiment_filestore / "test-exp-2026-03-21-17-45"
        base_destination.mkdir(parents=True)
        with (
            patch(
                "crsbench.cloud.cli._collect.resolve_cloud_context"
            ) as mock_resolve_context,
            patch("crsbench.cloud.cli._collect.ArtifactCollector") as mock_coll_cls,
            patch(
                "crsbench.cloud.cli._collect.provisioner_for_context"
            ) as mock_prov_cls,
            patch("crsbench.cloud.cli._collect.reconnect") as mock_reconnect,
            patch("sys.stdin.isatty", return_value=True),
            patch("builtins.input", return_value="t") as mock_input,
            patch(
                "crsbench.cloud.cli._collect._fresh_timestamp_destination",
                return_value=timestamp_destination,
            ) as mock_timestamp_destination,
        ):
            mock_prov = MagicMock()
            mock_prov.list_workers.return_value = [_make_gce_worker("w-1")]
            mock_prov_cls.return_value = mock_prov
            mock_resolve_context.return_value = _make_collect_context(
                experiment_filestore=experiment_filestore,
                remote_experiment_root=tmp_path / "remote-root",
            )

            mock_coll = MagicMock()

            def _collect_side_effect(**kwargs):
                kwargs["destination"].mkdir(parents=True, exist_ok=True)
                return kwargs["destination"]

            mock_coll.collect.side_effect = _collect_side_effect
            mock_coll_cls.return_value = mock_coll

            readiness = MagicMock()
            readiness.list_workers.return_value = []
            mock_reconnect.return_value = (
                MagicMock(),
                MagicMock(),
                readiness,
                MagicMock(),
                experiment_filestore,
            )

            from crsbench.cloud.cli._collect import run_collect

            rc = run_collect(_make_collect_args(force=False))

        assert rc == 0
        mock_input.assert_called()
        mock_timestamp_destination.assert_called_once_with(
            experiment_filestore,
            "test-exp",
        )
        assert (
            mock_coll.collect.call_args.kwargs["destination"] == timestamp_destination
        )
        marker = read_collect_marker(timestamp_destination)
        assert marker is not None
        assert marker["local_destination"] == str(timestamp_destination)

    def test_collect_existing_destination_reprompts_on_invalid_input(
        self,
        tmp_path: Path,
    ):
        experiment_filestore = tmp_path / "filestore"
        (experiment_filestore / "test-exp").mkdir(parents=True)
        with (
            patch(
                "crsbench.cloud.cli._collect.resolve_cloud_context"
            ) as mock_resolve_context,
            patch("crsbench.cloud.cli._collect.ArtifactCollector") as mock_coll_cls,
            patch(
                "crsbench.cloud.cli._collect.provisioner_for_context"
            ) as mock_prov_cls,
            patch("crsbench.cloud.cli._collect.reconnect") as mock_reconnect,
            patch("sys.stdin.isatty", return_value=True),
            patch("builtins.input", side_effect=["maybe", "n"]) as mock_input,
        ):
            mock_prov = MagicMock()
            mock_prov.list_workers.return_value = [_make_gce_worker("w-1")]
            mock_prov_cls.return_value = mock_prov
            mock_resolve_context.return_value = _make_collect_context(
                experiment_filestore=experiment_filestore,
                remote_experiment_root=tmp_path / "remote-root",
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
                experiment_filestore,
            )

            from crsbench.cloud.cli._collect import run_collect

            rc = run_collect(_make_collect_args(force=False))

        assert rc == 1
        assert mock_input.call_count >= 2

    def test_collect_existing_destination_interactive_warning_includes_marker_context(
        self,
        tmp_path: Path,
    ):
        experiment_filestore = tmp_path / "filestore"
        destination = experiment_filestore / "test-exp"
        _write_collect_marker_metadata(
            destination,
            last_collect_time="2026-03-10T01:02:03+00:00",
            experiment_start_time="2026-03-09T23:00:00+00:00",
        )
        with (
            patch(
                "crsbench.cloud.cli._collect.resolve_cloud_context"
            ) as mock_resolve_context,
            patch("crsbench.cloud.cli._collect.ArtifactCollector") as mock_coll_cls,
            patch(
                "crsbench.cloud.cli._collect.provisioner_for_context"
            ) as mock_prov_cls,
            patch("crsbench.cloud.cli._collect.reconnect") as mock_reconnect,
            patch("sys.stdin.isatty", return_value=True),
            patch("builtins.input", return_value="") as mock_input,
            patch("crsbench.cloud.cli._collect.logger") as mock_logger,
        ):
            mock_prov = MagicMock()
            mock_prov.list_workers.return_value = [_make_gce_worker("w-1")]
            mock_prov_cls.return_value = mock_prov
            mock_resolve_context.return_value = _make_collect_context(
                experiment_filestore=experiment_filestore,
                remote_experiment_root=tmp_path / "remote-root",
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
                experiment_filestore,
            )

            from crsbench.cloud.cli._collect import run_collect

            rc = run_collect(_make_collect_args(force=False))

        assert rc == 0
        warning_calls = [str(call) for call in mock_logger.warning.call_args_list]
        warning_text = "\n".join(warning_calls)
        assert "Last collected:" in warning_text
        assert "Experiment started:" in warning_text
        assert "2026-03-10T01:02:03+00:00" in warning_text
        assert "2026-03-09T23:00:00+00:00" in warning_text
        assert "--force" in warning_text

    def test_collect_success_writes_marker_after_successful_collection(
        self,
        tmp_path: Path,
    ):
        experiment_filestore = tmp_path / "filestore"
        destination = experiment_filestore / "test-exp"
        with (
            patch(
                "crsbench.cloud.cli._collect.resolve_cloud_context"
            ) as mock_resolve_context,
            patch("crsbench.cloud.cli._collect.ArtifactCollector") as mock_coll_cls,
            patch(
                "crsbench.cloud.cli._collect.provisioner_for_context"
            ) as mock_prov_cls,
            patch("crsbench.cloud.cli._collect.reconnect") as mock_reconnect,
        ):
            mock_prov = MagicMock()
            mock_prov.list_workers.return_value = [_make_gce_worker("w-1")]
            mock_prov_cls.return_value = mock_prov
            mock_resolve_context.return_value = _make_collect_context(
                experiment_filestore=experiment_filestore,
                remote_experiment_root=tmp_path / "remote-root",
            )

            mock_coll = MagicMock()

            def _collect_side_effect(**kwargs):
                kwargs["start_time_observations"].append(
                    (
                        "2026-03-09T23:00:00+00:00",
                        "earliest_trial_timestamp_start",
                    )
                )
                destination.mkdir(parents=True, exist_ok=True)
                return destination

            mock_coll.collect.side_effect = _collect_side_effect
            mock_coll_cls.return_value = mock_coll

            readiness = MagicMock()
            readiness.list_workers.return_value = []
            mock_reconnect.return_value = (
                MagicMock(),
                MagicMock(),
                readiness,
                MagicMock(),
                experiment_filestore,
            )

            from crsbench.cloud.cli._collect import run_collect

            rc = run_collect(_make_collect_args())

        assert rc == 0
        marker = read_collect_marker(destination)
        assert marker is not None
        assert marker["schema_version"] == 1
        assert marker["experiment_name"] == "test-exp"
        assert marker["local_destination"] == str(destination)
        assert marker["experiment_start_time"] == "2026-03-09T23:00:00+00:00"
        assert (
            marker["experiment_start_time_source"] == "earliest_trial_timestamp_start"
        )
        assert isinstance(marker["last_collect_time"], str)

    def test_collect_success_preserves_prior_start_time_when_current_run_has_none(
        self,
        tmp_path: Path,
    ):
        experiment_filestore = tmp_path / "filestore"
        destination = experiment_filestore / "test-exp"
        prior_marker = {
            "schema_version": 1,
            "experiment_name": "test-exp",
            "local_destination": str(destination),
            "last_collect_time": "2026-03-10T01:02:03+00:00",
            "experiment_start_time": "2026-03-09T23:00:00+00:00",
            "experiment_start_time_source": "earliest_trial_timestamp_start",
        }
        destination.mkdir(parents=True, exist_ok=True)
        collect_marker_path(destination).write_text(json.dumps(prior_marker))
        with (
            patch(
                "crsbench.cloud.cli._collect.resolve_cloud_context"
            ) as mock_resolve_context,
            patch("crsbench.cloud.cli._collect.ArtifactCollector") as mock_coll_cls,
            patch(
                "crsbench.cloud.cli._collect.provisioner_for_context"
            ) as mock_prov_cls,
            patch("crsbench.cloud.cli._collect.reconnect") as mock_reconnect,
        ):
            mock_prov = MagicMock()
            mock_prov.list_workers.return_value = [_make_gce_worker("w-1")]
            mock_prov_cls.return_value = mock_prov
            mock_resolve_context.return_value = _make_collect_context(
                experiment_filestore=experiment_filestore,
                remote_experiment_root=tmp_path / "remote-root",
            )

            mock_coll = MagicMock()
            mock_coll.collect.side_effect = lambda **_kwargs: destination
            mock_coll_cls.return_value = mock_coll

            readiness = MagicMock()
            readiness.list_workers.return_value = []
            mock_reconnect.return_value = (
                MagicMock(),
                MagicMock(),
                readiness,
                MagicMock(),
                experiment_filestore,
            )

            from crsbench.cloud.cli._collect import run_collect

            rc = run_collect(_make_collect_args(force=True))

        assert rc == 0
        marker = read_collect_marker(destination)
        assert marker is not None
        assert marker["experiment_start_time"] == "2026-03-09T23:00:00+00:00"
        assert (
            marker["experiment_start_time_source"] == "earliest_trial_timestamp_start"
        )

    def test_collect_partial_failure_does_not_update_existing_marker(
        self,
        tmp_path: Path,
    ):
        experiment_filestore = tmp_path / "filestore"
        destination = experiment_filestore / "test-exp"
        prior_marker = {
            "schema_version": 1,
            "experiment_name": "test-exp",
            "local_destination": str(destination),
            "last_collect_time": "2026-03-10T01:02:03+00:00",
            "experiment_start_time": "2026-03-09T23:00:00+00:00",
            "experiment_start_time_source": "earliest_trial_timestamp_start",
        }
        destination.mkdir(parents=True, exist_ok=True)
        collect_marker_path(destination).write_text(json.dumps(prior_marker))
        with (
            patch(
                "crsbench.cloud.cli._collect.resolve_cloud_context"
            ) as mock_resolve_context,
            patch("crsbench.cloud.cli._collect.ArtifactCollector") as mock_coll_cls,
            patch(
                "crsbench.cloud.cli._collect.provisioner_for_context"
            ) as mock_prov_cls,
            patch("crsbench.cloud.cli._collect.reconnect") as mock_reconnect,
        ):
            mock_prov = MagicMock()
            mock_prov.list_workers.return_value = [_make_gce_worker("w-1")]
            mock_prov_cls.return_value = mock_prov
            mock_resolve_context.return_value = _make_collect_context(
                experiment_filestore=experiment_filestore,
                remote_experiment_root=tmp_path / "remote-root",
            )

            mock_coll = MagicMock()
            from crsbench.cloud.collection import ArtifactCollectionError

            mock_coll.collect.side_effect = ArtifactCollectionError("rsync failed")
            mock_coll_cls.return_value = mock_coll

            readiness = MagicMock()
            readiness.list_workers.return_value = []
            mock_reconnect.return_value = (
                MagicMock(),
                MagicMock(),
                readiness,
                MagicMock(),
                experiment_filestore,
            )

            from crsbench.cloud.cli._collect import run_collect

            rc = run_collect(_make_collect_args(force=True))

        assert rc == 1
        assert read_collect_marker(destination) == prior_marker

    def test_collect_marker_write_failure_returns_non_zero_after_publish(
        self,
        tmp_path: Path,
    ):
        experiment_filestore = tmp_path / "filestore"
        destination = experiment_filestore / "test-exp"
        with (
            patch(
                "crsbench.cloud.cli._collect.resolve_cloud_context"
            ) as mock_resolve_context,
            patch("crsbench.cloud.cli._collect.ArtifactCollector") as mock_coll_cls,
            patch(
                "crsbench.cloud.cli._collect.provisioner_for_context"
            ) as mock_prov_cls,
            patch("crsbench.cloud.cli._collect.reconnect") as mock_reconnect,
            patch(
                "crsbench.cloud.cli._collect.write_collect_marker",
                side_effect=OSError("disk full"),
            ),
        ):
            mock_prov = MagicMock()
            mock_prov.list_workers.return_value = [_make_gce_worker("w-1")]
            mock_prov_cls.return_value = mock_prov
            mock_resolve_context.return_value = _make_collect_context(
                experiment_filestore=experiment_filestore,
                remote_experiment_root=tmp_path / "remote-root",
            )

            mock_coll = MagicMock()

            def _collect_side_effect(**kwargs):
                kwargs["start_time_observations"].append(
                    (
                        "2026-03-09T23:00:00+00:00",
                        "earliest_trial_timestamp_start",
                    )
                )
                destination.mkdir(parents=True, exist_ok=True)
                return destination

            mock_coll.collect.side_effect = _collect_side_effect
            mock_coll_cls.return_value = mock_coll

            readiness = MagicMock()
            readiness.list_workers.return_value = []
            mock_reconnect.return_value = (
                MagicMock(),
                MagicMock(),
                readiness,
                MagicMock(),
                experiment_filestore,
            )

            from crsbench.cloud.cli._collect import run_collect

            rc = run_collect(_make_collect_args())

        assert rc == 1
        assert destination.exists()

    def test_collect_log_only_run_does_not_refresh_existing_marker(
        self,
        tmp_path: Path,
    ):
        experiment_filestore = tmp_path / "filestore"
        destination = experiment_filestore / "test-exp"
        prior_marker = {
            "schema_version": 1,
            "experiment_name": "test-exp",
            "local_destination": str(destination),
            "last_collect_time": "2026-03-10T01:02:03+00:00",
            "experiment_start_time": "2026-03-09T23:00:00+00:00",
            "experiment_start_time_source": "earliest_trial_timestamp_start",
        }
        destination.mkdir(parents=True, exist_ok=True)
        collect_marker_path(destination).write_text(json.dumps(prior_marker))
        evaluator = dataclasses.replace(
            _make_gce_worker("eval-1"),
            labels={"crsbench-role": "evaluator"},
        )
        with (
            patch(
                "crsbench.cloud.cli._collect.resolve_cloud_context"
            ) as mock_resolve_context,
            patch("crsbench.cloud.cli._collect.ArtifactCollector") as mock_coll_cls,
            patch(
                "crsbench.cloud.cli._collect._resolve_instance_fleet"
            ) as mock_resolve_fleet,
            patch(
                "crsbench.cloud.cli._collect.provisioner_for_context"
            ) as mock_prov_cls,
            patch("crsbench.cloud.cli._collect.reconnect") as mock_reconnect,
        ):
            mock_prov = MagicMock()
            mock_prov.list_workers.return_value = [evaluator]
            mock_prov_cls.return_value = mock_prov
            mock_resolve_fleet.return_value = MagicMock()
            mock_resolve_context.return_value = _make_collect_context(
                experiment_filestore=experiment_filestore,
                remote_experiment_root=tmp_path / "remote-root",
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
                experiment_filestore,
            )

            from crsbench.cloud.cli._collect import run_collect

            rc = run_collect(_make_collect_args(force=True))

        assert rc == 0
        assert read_collect_marker(destination) == prior_marker

    def test_collect_existing_destination_eof_cancels_cleanly(
        self,
        tmp_path: Path,
    ):
        experiment_filestore = tmp_path / "filestore"
        (experiment_filestore / "test-exp").mkdir(parents=True)
        with (
            patch(
                "crsbench.cloud.cli._collect.resolve_cloud_context"
            ) as mock_resolve_context,
            patch("crsbench.cloud.cli._collect.ArtifactCollector") as mock_coll_cls,
            patch(
                "crsbench.cloud.cli._collect.provisioner_for_context"
            ) as mock_prov_cls,
            patch("crsbench.cloud.cli._collect.reconnect") as mock_reconnect,
            patch("sys.stdin.isatty", return_value=True),
            patch("builtins.input", side_effect=EOFError),
        ):
            mock_prov = MagicMock()
            mock_prov.list_workers.return_value = [_make_gce_worker("w-1")]
            mock_prov_cls.return_value = mock_prov
            mock_resolve_context.return_value = _make_collect_context(
                experiment_filestore=experiment_filestore,
                remote_experiment_root=tmp_path / "remote-root",
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
                experiment_filestore,
            )

            from crsbench.cloud.cli._collect import run_collect

            rc = run_collect(_make_collect_args(force=False))

        assert rc == 1
        mock_coll.collect.assert_not_called()

    @patch("crsbench.cloud.cli._collect.resolve_cloud_context")
    @patch("crsbench.cloud.cli._collect.logger")
    @patch("crsbench.cloud.cli._collect.ArtifactCollector")
    @patch("crsbench.cloud.cli._collect.provisioner_for_context")
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
    @patch("crsbench.cloud.cli._collect.provisioner_for_context")
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
        assert mock_coll.collect_logs.call_count == 2
        assert mock_coll.collect.call_count == 2

    def test_collect_partial_failure_leaves_worker_artifacts_in_staging(
        self, tmp_path: Path
    ) -> None:
        """Failed multi-worker collect must not publish staged artifacts into the final destination."""
        from crsbench.cloud.collection import ArtifactCollectionError

        workers = [_make_gce_worker("w-1"), _make_gce_worker("w-2")]
        experiment_filestore = tmp_path / "filestore"
        experiment_filestore.mkdir()
        context = SimpleNamespace(
            experiment_name="test-exp",
            launch_state=None,
            experiment_filestore=experiment_filestore,
            remote_experiment_root=Path("/tmp/remote-root"),
        )
        readiness = MagicMock()
        readiness.list_workers.return_value = []
        provisioner = MagicMock()
        provisioner.list_workers.return_value = workers
        fleet = MagicMock()

        @dataclasses.dataclass
        class _FakeStage:
            staging_dir: Path
            final_dir: Path

        class _FakeCollector:
            def __init__(self, *args: object, **kwargs: object) -> None:
                del args, kwargs

            def collect_logs(self, *args: object, **kwargs: object) -> Path:
                del args, kwargs
                return experiment_filestore / "logs"

            def collect(self, **kwargs: object) -> Path:
                worker = kwargs["worker"]
                destination = kwargs["destination"]
                if worker.name == "w-1":
                    destination.mkdir(parents=True, exist_ok=True)
                    (destination / "partial.txt").write_text(
                        "published too early\n", encoding="utf-8"
                    )
                    return destination
                raise ArtifactCollectionError("rsync failed")

            def stage_collection(self, **kwargs: object) -> _FakeStage:
                worker = kwargs["worker"]
                destination = kwargs["destination"]
                if worker.name == "w-1":
                    staging_dir = (
                        experiment_filestore
                        / ".collect-staging"
                        / worker.name
                        / kwargs["experiment_name"]
                    )
                    staging_dir.mkdir(parents=True, exist_ok=True)
                    (staging_dir / "partial.txt").write_text(
                        "staged artifact\n", encoding="utf-8"
                    )
                    return _FakeStage(staging_dir=staging_dir, final_dir=destination)
                raise ArtifactCollectionError("rsync failed")

            def publish_staged_collection(self, stage: _FakeStage) -> Path:
                stage.final_dir.mkdir(parents=True, exist_ok=True)
                (stage.final_dir / "partial.txt").write_text(
                    "published after barrier\n", encoding="utf-8"
                )
                return stage.final_dir

        with (
            patch(
                "crsbench.cloud.cli._collect.resolve_cloud_context",
                return_value=context,
            ),
            patch(
                "crsbench.cloud.cli._collect.provisioner_for_context",
                return_value=provisioner,
            ),
            patch(
                "crsbench.cloud.cli._collect._list_live_instances",
                return_value=workers,
            ),
            patch(
                "crsbench.cloud.cli._collect._resolve_instance_fleet",
                return_value=fleet,
            ),
            patch(
                "crsbench.cloud.cli._collect.reconnect",
                return_value=(
                    MagicMock(),
                    MagicMock(),
                    readiness,
                    MagicMock(),
                    experiment_filestore,
                ),
            ),
            patch("crsbench.cloud.cli._collect.ArtifactCollector", _FakeCollector),
        ):
            from crsbench.cloud.cli._collect import run_collect

            rc = run_collect(_make_collect_args(config=str(tmp_path / "config.yaml")))

        destination = experiment_filestore / "test-exp"
        staged_file = (
            experiment_filestore
            / ".collect-staging"
            / "w-1"
            / "test-exp"
            / "partial.txt"
        )
        assert rc == 1
        assert not destination.exists()
        assert staged_file.read_text(encoding="utf-8") == "staged artifact\n"

    @patch("crsbench.cloud.cli._collect.resolve_cloud_context")
    @patch("crsbench.cloud.cli._collect.ArtifactCollector")
    @patch("crsbench.cloud.cli._collect.provisioner_for_context")
    @patch("crsbench.cloud.cli._collect.reconnect")
    def test_collect_also_collects_orchestrator_when_launch_state_present(
        self, mock_reconnect, mock_prov_cls, mock_coll_cls, mock_resolve_context
    ):
        """Remote launches should collect orchestrator logs in addition to worker artifacts."""
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
        assert mock_coll.collect_logs.call_count == 3
        assert mock_coll.collect.call_count == 2
        orchestrator_log_call = mock_coll.collect_logs.call_args_list[-1]
        assert (
            orchestrator_log_call.kwargs["worker"].name == "gce-orchestrator-test-exp"
        )

    @patch("crsbench.cloud.cli._collect.resolve_cloud_context")
    @patch("crsbench.cloud.cli._collect.ArtifactCollector")
    @patch("crsbench.cloud.cli._collect.provisioner_for_context")
    @patch("crsbench.cloud.cli._collect.reconnect")
    def test_collect_orchestrator_when_no_workers_remain(
        self, mock_reconnect, mock_prov_cls, mock_coll_cls, mock_resolve_context
    ):
        """Remote launches should still collect orchestrator logs after workers are gone."""
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
        assert mock_coll.collect_logs.call_count == 1
        assert mock_coll.collect.call_count == 0
        assert (
            mock_coll.collect_logs.call_args.kwargs["worker"].name
            == "gce-orchestrator-test-exp"
        )

    @patch("crsbench.cloud.cli._collect.resolve_cloud_context", create=True)
    @patch("crsbench.cloud.cli._collect.ArtifactCollector")
    @patch("crsbench.cloud.cli._collect.provisioner_for_context")
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
    @patch("crsbench.cloud.cli._collect.provider_adapter_for_context")
    @patch("crsbench.cloud.cli._collect.provisioner_for_context")
    @patch("crsbench.cloud.cli._collect.reconnect")
    def test_collect_provider_neutral_context_uses_adapter_for_multi_zone_workers(
        self,
        mock_reconnect,
        mock_prov_cls,
        mock_adapter_cls,
        mock_coll_cls,
        mock_resolve_context,
    ):
        """Provider-neutral collection should use persisted fleets when launch state exists."""
        del mock_adapter_cls
        context = _make_provider_neutral_operational_context(include_launch_state=True)
        mock_resolve_context.return_value = context

        provisioner = mock_prov_cls.return_value
        provisioner.list_workers.side_effect = [
            [_make_gce_worker("test-exp-us-east5-b-001", zone="us-east5-b")],
            [_make_gce_worker("test-exp-us-east1-b-001", zone="us-east1-b")],
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
        assert provisioner.list_workers.call_count == 2
        assert mock_coll.collect_logs.call_count == 3
        assert mock_coll.collect.call_count == 2

    @patch("crsbench.cloud.cli._collect.resolve_cloud_context")
    @patch("crsbench.cloud.cli._collect.ArtifactCollector")
    @patch("crsbench.cloud.cli._collect.provider_adapter_for_context")
    @patch("crsbench.cloud.cli._collect.provisioner_for_context")
    @patch("crsbench.cloud.cli._collect.reconnect")
    def test_collect_provider_neutral_context_also_collects_evaluators(
        self,
        mock_reconnect,
        mock_prov_cls,
        mock_adapter_cls,
        mock_coll_cls,
        mock_resolve_context,
    ):
        """Provider-neutral collection should collect evaluator VMs in addition to workers."""
        del mock_adapter_cls
        launch_state = _make_provider_neutral_launch_state()
        context = MagicMock()
        context.experiment_name = "test-exp"
        context.launch_plan = MagicMock(experiment_name="test-exp")
        context.launch_state = launch_state
        context.experiment_filestore = Path("/tmp/filestore")
        context.worker_fleet_configs = launch_state.worker_fleet_configs
        context.evaluator_fleet_configs = [
            _make_stable_worker_fleet(
                zone="us-east1-b",
                start_index=1,
                worker_count=1,
                prefix="evaluator-test-exp-us-east1-b",
            )
        ]
        mock_resolve_context.return_value = context

        provisioner = mock_prov_cls.return_value
        evaluator = _make_gce_worker(
            "evaluator-test-exp-us-east1-b-001", zone="us-east1-b"
        )
        evaluator.labels["crsbench-role"] = "evaluator"
        provisioner.list_workers.side_effect = [
            [_make_gce_worker("test-exp-us-east5-b-001", zone="us-east5-b")],
            [],
        ]
        provisioner.list_evaluators.return_value = [evaluator]

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
        assert provisioner.list_workers.call_count == 2
        provisioner.list_evaluators.assert_called_once()
        assert mock_coll.collect_logs.call_count == 3
        assert mock_coll.collect.call_count == 1
        assert (
            mock_coll.collect.call_args.kwargs["worker"].name
            == "test-exp-us-east5-b-001"
        )


# ---------------------------------------------------------------------------
# Teardown sub-action tests
# ---------------------------------------------------------------------------


def _make_teardown_args(
    experiment: str = "test-exp",
    config: str = "/tmp/config.yaml",
    remote_dir: str = "/home/user/crsbench-experiments/test-exp",
    *,
    force: bool = False,
    timestamp: bool = False,
    skip_collect: bool = False,
):
    return argparse.Namespace(
        experiment=experiment,
        config=config,
        remote_dir=remote_dir,
        force=force,
        timestamp=timestamp,
        skip_collect=skip_collect,
        cloud_command="teardown",
    )


def _setup_teardown_mocks(
    mock_reconnect,
    mock_prov_cls,
    mock_coll_cls,
    workers=None,
    redis_workers=None,
    jobs=None,
    experiment_filestore: Path | None = None,
):
    """Wire up common mock structure for teardown tests."""
    if workers is None:
        workers = [_make_gce_worker("w-1"), _make_gce_worker("w-2")]
    if experiment_filestore is None:
        experiment_filestore = Path("/tmp/filestore")

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
        experiment_filestore,
    )

    return mock_prov, mock_coll, readiness, lifecycle


class TestTeardown:
    """Tests for run_teardown() sub-action."""

    @patch("crsbench.cloud.cli._teardown.list_queue_job_entries")
    @patch("crsbench.cloud.cli._teardown.queue_module.rq")
    @patch("crsbench.cloud.cli._teardown.queue_module.REDIS_AVAILABLE", new=True)
    def test_count_uncollected_jobs_merges_queue_only_jobs_when_lifecycle_is_partial(
        self,
        mock_rq,
        mock_list_queue_job_entries,
    ):
        """Teardown safety should count uncovered queue work, not just lifecycle rows."""
        from crsbench.cloud.cli._teardown import _count_uncollected_jobs
        from crsbench.distributed.job_lifecycle import JobLifecycleRecord, JobState

        lifecycle = MagicMock()
        lifecycle.list_jobs.return_value = [
            JobLifecycleRecord(
                job_id="job-completed",
                trial_key="trial-1",
                state=JobState.COMPLETED,
                claimed_by=None,
                retry_count=0,
            )
        ]
        mock_rq.Queue.return_value = MagicMock()
        mock_list_queue_job_entries.return_value = [
            SimpleNamespace(
                job_id="job-completed",
                trial_key="trial-1",
                state="completed",
                claimed_by=None,
                retry_count=0,
            ),
            SimpleNamespace(
                job_id="job-running",
                trial_key="trial-2",
                state="running",
                claimed_by="worker-1",
                retry_count=0,
            ),
        ]

        count = _count_uncollected_jobs(MagicMock(), lifecycle, "test-exp")

        assert count == 1

    def test_teardown_timestamp_flag_uses_fresh_sibling_destination(
        self,
        tmp_path: Path,
    ):
        experiment_filestore = tmp_path / "filestore"
        base_destination = experiment_filestore / "test-exp"
        timestamp_destination = experiment_filestore / "test-exp-2026-03-21-17-45"
        base_destination.mkdir(parents=True)
        with (
            patch(
                "crsbench.cloud.cli._teardown.resolve_cloud_context"
            ) as mock_resolve_context,
            patch("crsbench.cloud.cli._teardown.ArtifactCollector") as mock_coll_cls,
            patch(
                "crsbench.cloud.cli._teardown.provisioner_for_context"
            ) as mock_prov_cls,
            patch("crsbench.cloud.cli._teardown.reconnect") as mock_reconnect,
            patch(
                "crsbench.cloud.cli._teardown._fresh_timestamp_destination",
                return_value=timestamp_destination,
            ) as mock_timestamp_destination,
        ):
            mock_prov = MagicMock()
            mock_prov.list_workers.return_value = [_make_gce_worker("w-1")]
            mock_prov_cls.return_value = mock_prov
            mock_resolve_context.return_value = _make_collect_context(
                experiment_filestore=experiment_filestore,
                remote_experiment_root=tmp_path / "remote-root",
            )

            mock_coll = MagicMock()

            def _collect_side_effect(**kwargs):
                kwargs["destination"].mkdir(parents=True, exist_ok=True)
                return kwargs["destination"]

            mock_coll.collect.side_effect = _collect_side_effect
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
                experiment_filestore,
            )

            from crsbench.cloud.cli._teardown import run_teardown

            rc = run_teardown(_make_teardown_args(force=True, timestamp=True))

        assert rc == 0
        mock_timestamp_destination.assert_called_once_with(
            experiment_filestore,
            "test-exp",
        )
        assert (
            mock_coll.collect.call_args.kwargs["destination"] == timestamp_destination
        )
        marker = read_collect_marker(timestamp_destination)
        assert marker is not None
        assert marker["local_destination"] == str(timestamp_destination)

    @patch("crsbench.cloud.cli._teardown.resolve_cloud_context")
    @patch("crsbench.cloud.cli._teardown.ArtifactCollector")
    @patch("crsbench.cloud.cli._teardown.provisioner_for_context")
    @patch("crsbench.cloud.cli._teardown.reconnect")
    def test_teardown_skip_collect_flag_deletes_without_collecting(
        self, mock_reconnect, mock_prov_cls, mock_coll_cls, mock_resolve_context
    ):
        """Teardown should delete VMs without collection when --skip-collect is set."""
        mock_resolve_context.return_value = _make_resolved_cloud_context()
        mock_prov, mock_coll, _, _ = _setup_teardown_mocks(
            mock_reconnect, mock_prov_cls, mock_coll_cls
        )

        from crsbench.cloud.cli._teardown import run_teardown

        rc = run_teardown(_make_teardown_args(force=True, skip_collect=True))

        assert rc == 0
        mock_coll.collect.assert_not_called()
        mock_coll.collect_logs.assert_not_called()
        mock_coll.collect_reeval_submission_artifacts.assert_not_called()
        mock_prov.delete_workers.assert_called_once()

    def test_teardown_existing_destination_prompt_can_skip_collection(
        self,
        tmp_path: Path,
    ):
        experiment_filestore = tmp_path / "filestore"
        (experiment_filestore / "test-exp").mkdir(parents=True)
        with (
            patch(
                "crsbench.cloud.cli._teardown.resolve_cloud_context"
            ) as mock_resolve_context,
            patch("crsbench.cloud.cli._teardown.ArtifactCollector") as mock_coll_cls,
            patch(
                "crsbench.cloud.cli._teardown.provisioner_for_context"
            ) as mock_prov_cls,
            patch("crsbench.cloud.cli._teardown.reconnect") as mock_reconnect,
            patch("sys.stdin.isatty", return_value=True),
            patch("builtins.input", side_effect=["yes", "s"]) as mock_input,
        ):
            mock_prov = MagicMock()
            mock_prov.list_workers.return_value = [_make_gce_worker("w-1")]
            mock_prov_cls.return_value = mock_prov
            mock_resolve_context.return_value = _make_collect_context(
                experiment_filestore=experiment_filestore,
                remote_experiment_root=tmp_path / "remote-root",
            )

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
                experiment_filestore,
            )

            from crsbench.cloud.cli._teardown import run_teardown

            rc = run_teardown(_make_teardown_args(force=False))

        assert rc == 0
        assert mock_input.call_count == 2
        mock_coll.collect.assert_not_called()
        mock_coll.collect_logs.assert_not_called()
        mock_coll.collect_reeval_submission_artifacts.assert_not_called()
        mock_prov.delete_workers.assert_called_once()

    @patch("crsbench.cloud.cli._teardown.resolve_effective_experiment_name")
    @patch("crsbench.cloud.cli._teardown.resolve_cloud_context")
    @patch("crsbench.cloud.cli._teardown.ArtifactCollector")
    @patch("crsbench.cloud.cli._teardown.provisioner_for_context")
    @patch("crsbench.cloud.cli._teardown.reconnect")
    def test_teardown_infers_experiment_and_remote_dir_from_config(
        self,
        mock_reconnect,
        mock_prov_cls,
        mock_coll_cls,
        mock_resolve_context,
        mock_resolve_experiment_name,
    ):
        mock_resolve_experiment_name.return_value = "test-exp"
        mock_resolve_context.return_value = _make_collect_context(
            experiment_filestore=Path("/tmp/remote-root/scoped-filestore"),
            remote_experiment_root=Path("/tmp/remote-root"),
        )
        mock_prov, mock_coll, _, _ = _setup_teardown_mocks(
            mock_reconnect,
            mock_prov_cls,
            mock_coll_cls,
            workers=[_make_gce_worker("w-1")],
            experiment_filestore=Path("/tmp/remote-root/scoped-filestore"),
        )

        from crsbench.cloud.cli._teardown import run_teardown

        rc = run_teardown(
            _make_teardown_args(experiment=None, remote_dir=None, force=True)
        )

        assert rc == 0
        mock_resolve_context.assert_called_once_with("/tmp/config.yaml", "test-exp")
        mock_prov.delete_workers.assert_called_once()
        mock_coll.collect.assert_called_once()
        assert (
            mock_coll.collect.call_args.kwargs["remote_experiment_dir"]
            == "/tmp/remote-root/scoped-filestore/test-exp"
        )

    @patch("crsbench.cloud.cli._teardown.resolve_cloud_context")
    @patch("crsbench.cloud.cli._teardown.ArtifactCollector")
    @patch("crsbench.cloud.cli._teardown.provisioner_for_context")
    @patch("crsbench.cloud.cli._teardown.reconnect")
    def test_teardown_collects_logs_before_deletion(
        self, mock_reconnect, mock_prov_cls, mock_coll_cls, mock_resolve_context
    ):
        """Teardown should collect remote logs in addition to artifacts before deleting VMs."""
        mock_resolve_context.return_value = _make_resolved_cloud_context()
        mock_prov, mock_coll, _, _ = _setup_teardown_mocks(
            mock_reconnect, mock_prov_cls, mock_coll_cls
        )

        from crsbench.cloud.cli._teardown import run_teardown

        rc = run_teardown(_make_teardown_args(force=True))

        assert rc == 0
        mock_coll_cls.assert_called_once_with(base_path="/tmp/config.yaml")
        assert mock_coll.collect_logs.call_count == 2
        assert mock_coll.collect.call_count == 2
        mock_prov.delete_workers.assert_called_once()

    @patch("crsbench.cloud.cli._teardown.resolve_cloud_context")
    @patch("crsbench.cloud.cli._teardown.ArtifactCollector")
    @patch("crsbench.cloud.cli._teardown.provisioner_for_context")
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
    @patch("crsbench.cloud.cli._teardown.provisioner_for_context")
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
    @patch("crsbench.cloud.cli._teardown.provisioner_for_context")
    @patch("crsbench.cloud.cli._teardown.reconnect")
    def test_teardown_aborts_before_deletion_on_collect_failure_without_force(
        self, mock_reconnect, mock_prov_cls, mock_coll_cls, mock_resolve_context
    ):
        """Without --force, collect failures must abort teardown before deletion."""
        from crsbench.cloud.collection import ArtifactCollectionError

        mock_resolve_context.return_value = _make_resolved_cloud_context()
        mock_prov, mock_coll, _, _ = _setup_teardown_mocks(
            mock_reconnect, mock_prov_cls, mock_coll_cls
        )
        mock_coll.collect.side_effect = ArtifactCollectionError("rsync failed")

        from crsbench.cloud.cli._teardown import run_teardown

        with (
            patch("builtins.input", return_value="yes"),
            patch("sys.stdin") as mock_stdin,
        ):
            mock_stdin.isatty.return_value = True
            rc = run_teardown(_make_teardown_args(force=False))

        assert rc == 1
        mock_prov.delete_workers.assert_not_called()
        mock_prov.delete_instance.assert_not_called()

    @patch("crsbench.cloud.cli._teardown.resolve_cloud_context")
    @patch("crsbench.cloud.cli._teardown.ArtifactCollector")
    @patch("crsbench.cloud.cli._teardown.provisioner_for_context")
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
    @patch("crsbench.cloud.cli._teardown.provisioner_for_context")
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
    @patch("crsbench.cloud.cli._teardown.provisioner_for_context")
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
    @patch("crsbench.cloud.cli._teardown.provisioner_for_context")
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
    @patch("crsbench.cloud.cli._teardown.provisioner_for_context")
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
    @patch("crsbench.cloud.cli._teardown.provisioner_for_context")
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
        assert mock_coll.collect_logs.call_count == 3
        assert mock_coll.collect.call_count == 2
        mock_prov.delete_workers.assert_called_once()
        mock_prov.delete_instance.assert_called_once()
        mock_delete_state.assert_called_once_with("/tmp/config.yaml", "test-exp")

    @patch("crsbench.cloud.cli._teardown.delete_launch_state")
    @patch("crsbench.cloud.cli._teardown.resolve_cloud_context")
    @patch("crsbench.cloud.cli._teardown.ArtifactCollector")
    @patch("crsbench.cloud.cli._teardown.provisioner_for_context")
    @patch("crsbench.cloud.cli._teardown.reconnect")
    def test_teardown_reeval_mode_collects_orchestrator_artifacts_without_workers(
        self,
        mock_reconnect,
        mock_prov_cls,
        mock_coll_cls,
        mock_resolve_context,
        mock_delete_state,
    ):
        """Cloud re-eval teardown should collect the orchestrator workspace even with no worker fleets."""
        launch_state = _make_reeval_launch_state()
        mock_resolve_context.return_value = _make_collect_context(
            experiment_filestore=Path("/tmp/filestore"),
            remote_experiment_root=Path(launch_state.remote_experiment_root),
            launch_state=launch_state,
        )
        mock_prov = MagicMock()
        mock_prov.list_workers.return_value = []
        mock_prov_cls.return_value = mock_prov

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

        from crsbench.cloud.cli._teardown import run_teardown

        rc = run_teardown(
            _make_teardown_args(
                experiment="test-exp",
                force=True,
            )
        )

        assert rc == 0
        assert mock_coll.collect.call_count == 1
        assert mock_coll.collect_logs.call_count == 1
        mock_coll.collect_reeval_submission_artifacts.assert_called_once_with(
            worker=mock.ANY,
            fleet=launch_state.as_transport_config(),
            experiment_name=launch_state.effective_remote_experiment_name(),
            experiment_filestore=Path("/tmp/filestore"),
            remote_submission_dir=launch_state.remote_submission_dir,
            destination=Path("/tmp/filestore")
            / launch_state.effective_remote_experiment_name(),
        )
        mock_prov.delete_workers.assert_not_called()
        mock_prov.delete_instance.assert_called_once()
        mock_delete_state.assert_called_once_with(
            "/tmp/config.yaml",
            launch_state.effective_remote_experiment_name(),
        )

    @patch("crsbench.cloud.cli._teardown.delete_launch_state")
    @patch("crsbench.cloud.cli._teardown.resolve_cloud_context")
    @patch("crsbench.cloud.cli._teardown.ArtifactCollector")
    @patch("crsbench.cloud.cli._teardown.provisioner_for_context")
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

    @patch("crsbench.cloud.cli._teardown.delete_launch_state")
    @patch("crsbench.cloud.cli._teardown.resolve_cloud_context")
    @patch("crsbench.cloud.cli._teardown.ArtifactCollector")
    @patch("crsbench.cloud.cli._teardown.provisioner_for_context")
    @patch("crsbench.cloud.cli._teardown.reconnect")
    def test_teardown_deletes_vms_even_when_collection_fails(
        self,
        mock_reconnect,
        mock_prov_cls,
        mock_coll_cls,
        mock_resolve_context,
        mock_delete_state,
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
        ]

        from crsbench.cloud.cli._teardown import run_teardown

        rc = run_teardown(_make_teardown_args(force=True))

        assert rc == 1
        mock_prov.delete_workers.assert_called_once()
        mock_prov.delete_instance.assert_called_once()
        mock_delete_state.assert_called_once_with("/tmp/config.yaml", "test-exp")

    @patch("crsbench.cloud.cli._teardown.delete_launch_state")
    @patch("crsbench.cloud.cli._teardown.resolve_cloud_context")
    @patch("crsbench.cloud.cli._teardown.ArtifactCollector")
    @patch("crsbench.cloud.cli._teardown.provisioner_for_context")
    @patch("crsbench.cloud.cli._teardown.reconnect")
    def test_teardown_clears_launch_state_when_orchestrator_is_already_gone(
        self,
        mock_reconnect,
        mock_prov_cls,
        mock_coll_cls,
        mock_resolve_context,
        mock_delete_state,
    ):
        """State cleanup should still happen when deletion reports not-found after VMs are already gone."""
        mock_resolve_context.return_value = _make_resolved_cloud_context(
            _make_launch_state()
        )
        mock_prov, _mock_coll, _, _ = _setup_teardown_mocks(
            mock_reconnect, mock_prov_cls, mock_coll_cls
        )
        mock_prov.list_workers.side_effect = [
            [_make_gce_worker("w-1"), _make_gce_worker("w-2")],
            [],
        ]
        mock_prov.delete_instance.side_effect = RuntimeError(
            "instance gce-orchestrator-test-exp was not found"
        )
        mock_prov.get_instance_record.side_effect = RuntimeError(
            "instance gce-orchestrator-test-exp was not found"
        )

        from crsbench.cloud.cli._teardown import run_teardown

        rc = run_teardown(_make_teardown_args(force=True))

        assert rc == 1
        mock_prov.delete_workers.assert_called_once()
        mock_prov.delete_instance.assert_called_once()
        mock_delete_state.assert_called_once_with("/tmp/config.yaml", "test-exp")

    @patch("crsbench.cloud.cli._teardown.resolve_cloud_context", create=True)
    @patch("crsbench.cloud.cli._teardown.ArtifactCollector")
    @patch("crsbench.cloud.cli._teardown.provisioner_for_context")
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
    @patch("crsbench.cloud.cli._teardown.provider_adapter_for_context")
    @patch("crsbench.cloud.cli._teardown.provisioner_for_context")
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
        """Provider-neutral teardown should delete all persisted fleets when launch state exists."""
        context = _make_provider_neutral_operational_context(include_launch_state=True)
        mock_resolve_context.return_value = context

        del mock_adapter_cls
        mock_prov = mock_prov_cls.return_value
        mock_prov.list_workers.side_effect = [
            [_make_gce_worker("test-exp-us-east5-b-001", zone="us-east5-b")],
            [_make_gce_worker("test-exp-us-east1-b-001", zone="us-east1-b")],
        ]

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

        from crsbench.cloud.cli._teardown import run_teardown

        rc = run_teardown(_make_teardown_args(force=True))

        assert rc == 0
        assert mock_prov.list_workers.call_count == 2
        assert mock_prov.delete_workers.call_count == 2
        mock_prov.delete_instance.assert_called_once()
        mock_delete_state.assert_called_once_with("/tmp/config.yaml", "test-exp")

    @patch("crsbench.cloud.cli._teardown.delete_launch_state")
    @patch("crsbench.cloud.cli._teardown.resolve_cloud_context")
    @patch("crsbench.cloud.cli._teardown.ArtifactCollector")
    @patch("crsbench.cloud.cli._teardown.provider_adapter_for_context")
    @patch("crsbench.cloud.cli._teardown.provisioner_for_context")
    @patch("crsbench.cloud.cli._teardown.reconnect")
    def test_teardown_provider_neutral_context_deletes_evaluators_via_adapter(
        self,
        mock_reconnect,
        mock_prov_cls,
        mock_adapter_cls,
        mock_coll_cls,
        mock_resolve_context,
        mock_delete_state,
    ):
        """Provider-neutral teardown should collect and delete persisted evaluator fleets too."""
        launch_state = _make_provider_neutral_launch_state()
        context = MagicMock()
        context.experiment_name = "test-exp"
        context.launch_plan = MagicMock(experiment_name="test-exp")
        context.launch_state = launch_state
        context.experiment_filestore = Path("/tmp/filestore")
        context.worker_fleet_configs = launch_state.worker_fleet_configs
        context.evaluator_fleet_configs = [
            _make_stable_worker_fleet(
                zone="us-east1-b",
                start_index=1,
                worker_count=1,
                prefix="evaluator-test-exp-us-east1-b",
            )
        ]
        mock_resolve_context.return_value = context

        del mock_adapter_cls
        mock_prov = mock_prov_cls.return_value
        mock_prov.list_workers.side_effect = [
            [_make_gce_worker("test-exp-us-east5-b-001", zone="us-east5-b")],
            [],
        ]
        evaluator = _make_gce_worker(
            "evaluator-test-exp-us-east1-b-001", zone="us-east1-b"
        )
        evaluator.labels["crsbench-role"] = "evaluator"
        mock_prov.list_evaluators.return_value = [evaluator]

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

        from crsbench.cloud.cli._teardown import run_teardown

        rc = run_teardown(_make_teardown_args(force=True))

        assert rc == 0
        assert mock_prov.list_workers.call_count == 2
        mock_prov.list_evaluators.assert_called_once()
        assert mock_prov.delete_workers.call_count == 2
        mock_prov.delete_evaluators.assert_called_once()
        mock_prov.delete_instance.assert_called_once()
        assert mock_coll.collect_logs.call_count == 3
        assert mock_coll.collect.call_count == 1
        assert (
            mock_coll.collect.call_args.kwargs["worker"].name
            == "test-exp-us-east5-b-001"
        )
        mock_delete_state.assert_called_once_with("/tmp/config.yaml", "test-exp")
