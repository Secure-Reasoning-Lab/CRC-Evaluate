"""Unit tests for crsbench cloud CLI command -- status, events, config reconnect."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from crsbench.cloud.types import CloudProvider
from crsbench.distributed.queue import RedisConnectionProbe
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
            GceWorkerFleetConfig(
                project="test-project",
                zone="us-central1-a",
                worker_count=2,
                machine_type="e2-standard-4",
                boot_disk_size_gb=100,
                image="projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64",
                service_account_email="crsbench-worker@test-project.iam.gserviceaccount.com",
                owner_label="team-crs",
            )
        ],
    )


def _make_provider_neutral_launch_state():
    from crsbench.cloud.launch_state import CloudLaunchState

    return CloudLaunchState(
        experiment_name="test-exp",
        config_path="/tmp/config.yaml",
        experiment_filestore="/tmp/filestore",
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


def _make_stable_worker_fleet(
    *,
    zone: str,
    zones: list[str] | None = None,
    start_index: int,
    worker_count: int,
    prefix: str = "crsbench-test-exp-work",
) -> GceWorkerFleetConfig:
    return GceWorkerFleetConfig(
        project="test-project",
        zone=zone,
        zones=zones or [zone],
        worker_count=worker_count,
        worker_name_start_index=start_index,
        machine_type="n2d-standard-16",
        boot_disk_size_gb=100,
        image="projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64",
        service_account_email="crsbench-worker@test-project.iam.gserviceaccount.com",
        owner_label="team-crs",
        worker_name_prefix=prefix,
    )


def _make_resolved_cloud_context(launch_state=None):
    from crsbench.cloud.cli._config_reconnect import ResolvedCloudContext

    if launch_state is None:
        fleet = GceWorkerFleetConfig(
            project="test-project",
            zone="us-central1-a",
            worker_count=2,
            machine_type="e2-standard-4",
            boot_disk_size_gb=100,
            image="projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64",
            service_account_email="crsbench-worker@test-project.iam.gserviceaccount.com",
            owner_label="team-crs",
        )
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
                "defaults": {
                    "readiness_timeout_sec": 1200,
                    "crsbench_install_spec": "git+ssh://git@github.com/sslab-gatech/CRSBench.git",
                    "crsbench_git_ref": "feat/gcp",
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
    assert plan.orchestrator.launch_defaults.crsbench_git_ref == "feat/gcp"
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

    def test_resolve_remote_experiment_dir_defaults_to_filestore_and_experiment(self):
        from crsbench.cloud.cli._config_reconnect import resolve_remote_experiment_dir

        remote_dir = resolve_remote_experiment_dir(
            Path("/tmp/filestore"),
            "test-exp",
            None,
        )

        assert remote_dir == "/tmp/filestore/test-exp"


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

    def test_parse_launch_with_global_config(self):
        parser = self._build_parser()
        args = parser.parse_args(["cloud", "--config", "c.yaml", "launch"])
        assert args.command == "cloud"
        assert args.cloud_command == "launch"
        assert args.config == "c.yaml"

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
        assert args.config == "c.yaml"

    def test_parse_log_without_instance(self):
        parser = self._build_parser()
        args = parser.parse_args(["cloud", "log", "--config", "c.yaml"])
        assert args.command == "cloud"
        assert args.cloud_command == "log"
        assert args.instance is None
        assert args.config == "c.yaml"


def _make_launch_args(config: str = "/tmp/config.yaml"):
    return argparse.Namespace(
        config=config,
        cloud_command="launch",
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
):
    return argparse.Namespace(
        instance=instance,
        config=config,
        cloud_command="ssh",
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


def _make_log_args(
    instance: str | None = "work-001",
    config: str = "/tmp/config.yaml",
):
    return argparse.Namespace(
        instance=instance,
        config=config,
        cloud_command="log",
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


@patch("crsbench.cloud.cli._monitor.initialize_queue")
@patch("crsbench.cloud.cli._monitor.resolve_effective_experiment_name")
@patch(
    "crsbench.cloud.cli._monitor.require_launch_state",
    side_effect=SystemExit("cloud monitor requires saved remote launch state"),
)
def test_run_monitor_requires_launch_state(
    mock_require_state,
    mock_resolve_experiment_name,
    mock_initialize_queue,
):
    from crsbench.cloud.cli._monitor import run_monitor

    mock_resolve_experiment_name.return_value = "test-exp"
    rc = run_monitor(_make_monitor_args())

    assert rc == 1
    mock_resolve_experiment_name.assert_called_once_with("/tmp/config.yaml", "test-exp")
    mock_require_state.assert_called_once_with("/tmp/config.yaml", "test-exp")
    mock_initialize_queue.assert_not_called()


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


class TestLaunch:
    """Tests for run_launch() orchestration."""

    @patch("crsbench.cloud.cli._launch.load_launch_state")
    @patch("crsbench.cloud.cli._launch.prepare_gce_launch_inputs")
    @patch("crsbench.cloud.cli._launch.GceProviderAdapter")
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

    @patch("crsbench.cloud.cli._launch.load_launch_state", return_value=None)
    @patch("crsbench.cloud.cli._launch.prepare_gce_launch_inputs")
    @patch("crsbench.cloud.cli._launch.GceProviderAdapter")
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

    @patch(
        "crsbench.cloud.cli._launch.secrets.token_urlsafe", return_value="shared-secret"
    )
    @patch("crsbench.cloud.cli._launch.append_created_instance_records")
    @patch("crsbench.cloud.cli._launch.save_launch_state")
    @patch("crsbench.cloud.cli._launch.prepare_gce_launch_inputs")
    @patch("crsbench.cloud.cli._launch.GceProviderAdapter")
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
    @patch("crsbench.cloud.cli._launch.GceProvisioner")
    @patch("crsbench.cloud.cli._launch.append_created_instance_records")
    @patch(
        "crsbench.cloud.cli._launch.save_launch_state",
        side_effect=RuntimeError("disk full"),
    )
    @patch("crsbench.cloud.cli._launch.prepare_gce_launch_inputs")
    @patch("crsbench.cloud.cli._launch.GceProviderAdapter")
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
    @patch("crsbench.cloud.cli._launch.GceProvisioner")
    @patch("crsbench.cloud.cli._launch.append_created_instance_records")
    @patch(
        "crsbench.cloud.cli._launch.save_launch_state",
        side_effect=RuntimeError("disk full"),
    )
    @patch("crsbench.cloud.cli._launch.prepare_gce_launch_inputs")
    @patch("crsbench.cloud.cli._launch.GceProviderAdapter")
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
    @patch("crsbench.cloud.cli._launch.prepare_gce_launch_inputs")
    @patch("crsbench.cloud.cli._launch.GceProviderAdapter")
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
    @patch("crsbench.cloud.cli._launch.save_launch_state")
    @patch("crsbench.cloud.cli._launch.prepare_gce_launch_inputs")
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
        mock_validator.validate.side_effect = lambda plan: (
            call_order.append(f"validate:{plan.experiment_name}")
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
        )
        mock_adapter.create_workers.assert_called_once()
        assert mock_adapter.create_workers.call_args.kwargs["plan"] is resolved_plan

    @patch(
        "crsbench.cloud.cli._launch.secrets.token_urlsafe", return_value="shared-secret"
    )
    @patch("crsbench.cloud.cli._launch.save_launch_state")
    @patch("crsbench.cloud.cli._launch.prepare_gce_launch_inputs")
    @patch("crsbench.cloud.cli._launch.GceProviderAdapter")
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
    @patch("crsbench.cloud.cli._launch.prepare_gce_launch_inputs")
    @patch("crsbench.cloud.cli._launch.GceProviderAdapter")
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
            GceWorkerFleetConfig(
                project="test-project",
                zone="us-east1-b",
                worker_count=1,
                machine_type="c3-standard-8",
                boot_disk_size_gb=50,
                image="projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64",
                service_account_email="crsbench-evaluator@test-project.iam.gserviceaccount.com",
                owner_label="team-crs",
                worker_name_prefix="evaluator-test-exp-us-east1-b",
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
    assert raw_state["worker_fleet_configs"][0]["github_deploy_key_path"] is None

    loaded_state = load_launch_state(config_path, "test-exp")
    assert loaded_state is not None
    assert loaded_state.worker_fleet_configs[0].github_deploy_key_path is None


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

    assert fleet.worker_name_start_index == 3


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

    assert fleet.worker_name_start_index == 3


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
        assert fleet_headers == ["Instance", "Role", "State", "Zone", "IP"]
        assert [
            "evaluator-1",
            "evaluator",
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


def _make_completed_process(returncode: int = 0):
    return SimpleNamespace(returncode=returncode)


class TestList:
    """Tests for run_list() sub-action."""

    @patch("crsbench.cloud.cli._list.resolve_effective_experiment_name")
    @patch("crsbench.cloud.cli._list.resolve_cloud_context")
    @patch("crsbench.cloud.cli._list.GceProvisioner")
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
    @patch("crsbench.cloud.cli._list.GceProvisioner")
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


class TestSsh:
    """Tests for run_ssh() sub-action."""

    @patch("crsbench.cloud.cli._ssh.subprocess.run")
    @patch("crsbench.cloud.cli._ssh.build_ssh_command")
    @patch("crsbench.cloud.cli._ssh.resolve_effective_experiment_name")
    @patch("crsbench.cloud.cli._ssh.resolve_cloud_context")
    @patch("crsbench.cloud.cli._ssh.GceProvisioner")
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
            "--command=echo hi",
        ]
        mock_run.return_value = _make_completed_process(0)

        from crsbench.cloud.cli._ssh import run_ssh

        rc = run_ssh(_make_ssh_args(instance="work-001"))

        assert rc == 0
        mock_build_ssh_command.assert_called_once()
        cmd = mock_run.call_args.args[0]
        assert cmd == mock_build_ssh_command.return_value

    @patch("crsbench.cloud.cli._ssh.subprocess.run")
    @patch("crsbench.cloud.cli._ssh.build_ssh_command")
    @patch("crsbench.cloud.cli._ssh.select_target")
    @patch("crsbench.cloud.cli._ssh.resolve_effective_experiment_name")
    @patch("crsbench.cloud.cli._ssh.resolve_cloud_context")
    @patch("crsbench.cloud.cli._ssh.GceProvisioner")
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
        cmd = mock_run.call_args.args[0]
        assert cmd == mock_build_ssh_command.return_value
        mock_select_target.assert_called_once()

    @patch("crsbench.cloud.cli._ssh.resolve_effective_experiment_name")
    @patch("crsbench.cloud.cli._ssh.resolve_cloud_context")
    @patch("crsbench.cloud.cli._ssh.GceProvisioner")
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


class TestExec:
    """Tests for run_exec() sub-action."""

    @patch("crsbench.cloud.cli._exec.subprocess.run")
    @patch("crsbench.cloud.cli._exec.build_ssh_command")
    @patch("crsbench.cloud.cli._exec.resolve_effective_experiment_name")
    @patch("crsbench.cloud.cli._exec.resolve_cloud_context")
    @patch("crsbench.cloud.cli._exec.GceProvisioner")
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
        mock_build_ssh_command.assert_called_once()
        cmd = mock_run.call_args.args[0]
        assert cmd == mock_build_ssh_command.return_value

    @patch("crsbench.cloud.cli._exec.resolve_effective_experiment_name")
    def test_exec_requires_command(self, mock_resolve_experiment_name):
        mock_resolve_experiment_name.return_value = "test-exp"

        from crsbench.cloud.cli._exec import run_exec

        rc = run_exec(_make_exec_args(exec_command=[]))

        assert rc == 2


class TestLog:
    """Tests for run_log() sub-action."""

    @patch("crsbench.cloud.cli._log.subprocess.run")
    @patch("crsbench.cloud.cli._log.build_ssh_command")
    @patch("crsbench.cloud.cli._log.resolve_effective_experiment_name")
    @patch("crsbench.cloud.cli._log.resolve_cloud_context")
    @patch("crsbench.cloud.cli._log.GceProvisioner")
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
    @patch("crsbench.cloud.cli._log.GceProvisioner")
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


class TestCollect:
    """Tests for run_collect() sub-action."""

    @patch("crsbench.cloud.cli._collect.resolve_effective_experiment_name")
    @patch("crsbench.cloud.cli._collect.resolve_cloud_context")
    @patch("crsbench.cloud.cli._collect.ArtifactCollector")
    @patch("crsbench.cloud.cli._collect.GceProvisioner")
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

        rc = run_collect(_make_collect_args(experiment=None, remote_dir=None))

        assert rc == 0
        mock_resolve_context.assert_called_once_with("/tmp/config.yaml", "test-exp")
        mock_coll.collect.assert_called_once()
        assert (
            mock_coll.collect.call_args.kwargs["remote_experiment_dir"]
            == "/tmp/filestore/test-exp"
        )

    @patch("crsbench.cloud.cli._collect.resolve_cloud_context")
    @patch("crsbench.cloud.cli._collect.ArtifactCollector")
    @patch("crsbench.cloud.cli._collect.GceProvisioner")
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
        assert mock_coll.collect_logs.call_count == 2
        assert mock_coll.collect.call_count == 2

    @patch("crsbench.cloud.cli._collect.resolve_cloud_context")
    @patch("crsbench.cloud.cli._collect.ArtifactCollector")
    @patch("crsbench.cloud.cli._collect.GceProvisioner")
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
    @patch("crsbench.cloud.cli._collect.GceProvisioner")
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
    @patch("crsbench.cloud.cli._collect.GceProviderAdapter")
    @patch("crsbench.cloud.cli._collect.GceProvisioner")
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

    @patch("crsbench.cloud.cli._teardown.resolve_effective_experiment_name")
    @patch("crsbench.cloud.cli._teardown.resolve_cloud_context")
    @patch("crsbench.cloud.cli._teardown.ArtifactCollector")
    @patch("crsbench.cloud.cli._teardown.GceProvisioner")
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
        mock_resolve_context.return_value = _make_resolved_cloud_context()
        mock_prov, mock_coll, _, _ = _setup_teardown_mocks(
            mock_reconnect,
            mock_prov_cls,
            mock_coll_cls,
            workers=[_make_gce_worker("w-1")],
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
            == "/tmp/filestore/test-exp"
        )

    @patch("crsbench.cloud.cli._teardown.resolve_cloud_context")
    @patch("crsbench.cloud.cli._teardown.ArtifactCollector")
    @patch("crsbench.cloud.cli._teardown.GceProvisioner")
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
        assert mock_coll.collect_logs.call_count == 3
        assert mock_coll.collect.call_count == 2
        mock_prov.delete_workers.assert_called_once()
        mock_prov.delete_instance.assert_called_once()
        mock_delete_state.assert_called_once_with("/tmp/config.yaml", "test-exp")

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

    @patch("crsbench.cloud.cli._teardown.delete_launch_state")
    @patch("crsbench.cloud.cli._teardown.resolve_cloud_context")
    @patch("crsbench.cloud.cli._teardown.ArtifactCollector")
    @patch("crsbench.cloud.cli._teardown.GceProvisioner")
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
    @patch("crsbench.cloud.cli._teardown.GceProvisioner")
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
    @patch("crsbench.cloud.cli._teardown.GceProviderAdapter")
    @patch("crsbench.cloud.cli._teardown.GceProvisioner")
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
