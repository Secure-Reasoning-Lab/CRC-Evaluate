"""Tests for provider-neutral shared cloud record models."""

from __future__ import annotations

import pytest
from crsbench.cloud.records import CloudFleetPlacementRecord
from crsbench.cloud.types import CloudProvider
from crsbench.validation.schemas import ExperimentConfig


def _make_runtime_expansion_config() -> ExperimentConfig:
    return ExperimentConfig.model_validate(
        {
            "experiment": "test-exp",
            "task": "bugfinding",
            "benchmark_suite": "sanity",
            "mode": "delta",
            "trials": 1,
            "max_total_time": 20000,
            "inputs": {"pov": {"max_variants_per_cpv": 1}},
            "redis_host": "localhost:6379",
            "experiment_filestore": "/tmp/filestore",
            "report_filestore": "/tmp/reports",
            "cloud": {
                "providers": {
                    "gce": {
                        "project": "test-project",
                        "regions": ["us-east5", "us-east1"],
                        "fallback": True,
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
                            "gce-evaluator-c3": {
                                "machine_type": "c3-standard-8",
                                "service_account_email": "crsbench-evaluator@test-project.iam.gserviceaccount.com",
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
                        "count": 1,
                    },
                    "placements": [{"zone": "us-east5-b"}],
                },
            },
            "crs_compose": {"test-crs": {"num_cores": 1}},
        }
    )


def test_cloud_instance_record_roundtrip_preserves_shared_fields() -> None:
    from crsbench.cloud.records import CloudInstanceRecord

    record = CloudInstanceRecord(
        provider=CloudProvider.GCE,
        role="worker",
        name="crsbench-test-work-001",
        instance_id="1001",
        status="RUNNING",
        project="test-project",
        zone="us-east5-b",
        region="us-east5",
        internal_ip="10.0.0.10",
        external_ip=None,
        ssh_via_iap=True,
        labels={"crsbench-role": "worker"},
        provider_metadata={"raw_status": "RUNNING"},
    )

    payload = record.model_dump(mode="json")

    assert payload["provider"] == "gce"
    assert payload["role"] == "worker"
    assert payload["project"] == "test-project"
    assert payload["zone"] == "us-east5-b"
    assert payload["region"] == "us-east5"
    assert payload["ssh_via_iap"] is True


def test_cloud_fleet_placement_record_from_legacy_gce_dict_preserves_common_fields() -> (
    None
):
    from crsbench.cloud.records import cloud_fleet_placement_record_from_legacy_gce_dict

    record = cloud_fleet_placement_record_from_legacy_gce_dict(
        {
            "project": "test-project",
            "zone": "us-east5-b",
            "zones": ["us-east5-b", "us-east1-c"],
            "worker_count": 2,
            "worker_name_prefix": "crsbench-test-work",
            "worker_name_start_index": 3,
            "ssh_via_iap": True,
            "labels": {"owner": "team-crs"},
            "owner_label": "team-crs",
            "github_deploy_key_path": None,
        },
        role="worker",
    )

    assert record.provider is CloudProvider.GCE
    assert record.role == "worker"
    assert record.project == "test-project"
    assert record.zone == "us-east5-b"
    assert record.zones == ["us-east5-b", "us-east1-c"]
    assert record.region == "us-east5"
    assert record.count == 2
    assert record.name_prefix == "crsbench-test-work"
    assert record.name_start_index == 3
    assert record.ssh_via_iap is True
    assert record.placement_source == "config"
    assert record.provider_metadata["github_deploy_key_path"] is None


def test_cloud_fleet_placement_record_roundtrip_preserves_runtime_added_source() -> (
    None
):
    record = CloudFleetPlacementRecord(
        provider=CloudProvider.GCE,
        role="worker",
        project="test-project",
        zones=["us-east5-b"],
        region="us-east5",
        count=2,
        name_prefix="crsbench-test-work",
        name_start_index=4,
        placement_source="runtime_added",
    )

    payload = record.model_dump(mode="json")

    assert payload["placement_source"] == "runtime_added"


def test_next_name_start_index_counts_existing_runtime_added_worker_fleets() -> None:
    from crsbench.cloud.expansion import next_name_start_index

    fleet_records = [
        CloudFleetPlacementRecord(
            provider=CloudProvider.GCE,
            role="worker",
            project="test-project",
            zone="us-east5-b",
            zones=["us-east5-b"],
            region="us-east5",
            count=2,
            name_prefix="crsbench-test-work",
            name_start_index=1,
            placement_source="config",
        ),
        CloudFleetPlacementRecord(
            provider=CloudProvider.GCE,
            role="worker",
            project="test-project",
            zone="us-east1-b",
            zones=["us-east1-b"],
            region="us-east1",
            count=3,
            name_prefix="crsbench-test-work",
            name_start_index=3,
            placement_source="runtime_added",
        ),
    ]

    assert next_name_start_index(fleet_records, role="worker") == 6


def test_build_dynamic_placement_request_normalizes_regions_and_zones() -> None:
    from crsbench.cloud.expansion import build_dynamic_placement_request

    request = build_dynamic_placement_request(
        role="worker",
        config=_make_runtime_expansion_config(),
        instance_profile="gce-worker-n2d",
        count=2,
        regions="us-east5,us-east1",
        zones="us-east5-b,us-east1-b",
    )

    assert request.role == "worker"
    assert request.instance_profile == "gce-worker-n2d"
    assert request.count == 2
    assert request.fallback is True
    assert request.regions == ("us-east5", "us-east1")
    assert request.zones == ("us-east5-b", "us-east1-b")
    assert request.provider is CloudProvider.GCE


def test_build_dynamic_placement_request_rejects_missing_locations() -> None:
    from crsbench.cloud.expansion import build_dynamic_placement_request

    with pytest.raises(ValueError, match="requires --regions and/or --zones"):
        build_dynamic_placement_request(
            role="worker",
            config=_make_runtime_expansion_config(),
            instance_profile="gce-worker-n2d",
            count=2,
            regions=None,
            zones=None,
        )


def test_resolve_dynamic_placement_env_merges_shared_and_profile_layers(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from crsbench.cloud.expansion import resolve_dynamic_placement_env

    config = _make_runtime_expansion_config().model_copy(deep=True)
    assert config.cloud is not None
    assert config.cloud.providers is not None
    assert config.cloud.providers.gce is not None
    config.cloud.env = {
        "HF_TOKEN": "os.environ/HF_TOKEN",
        "SHARED_LITERAL": "shared-value",
    }
    config.cloud.providers.gce.instance_profiles["gce-worker-n2d"].env = {
        "PROFILE_ONLY": "file:profile-secret.txt",
        "SHARED_LITERAL": "profile-wins",
    }
    (tmp_path / "profile-secret.txt").write_text("profile-secret", encoding="utf-8")
    monkeypatch.setenv("HF_TOKEN", "hf-secret")

    resolved = resolve_dynamic_placement_env(
        config=config,
        instance_profile="gce-worker-n2d",
        cwd=tmp_path,
    )

    assert resolved == {
        "HF_TOKEN": "hf-secret",
        "SHARED_LITERAL": "profile-wins",
        "PROFILE_ONLY": "profile-secret",
    }


def test_launch_state_orchestrator_record_uses_neutral_instance_model() -> None:
    from crsbench.cloud.launch_state import CloudLaunchState
    from crsbench.cloud.records import CloudInstanceRecord

    state = CloudLaunchState(
        experiment_name="test-exp",
        config_path="/tmp/config.yaml",
        redis_host="10.0.0.1:6379",
        redis_password="secret",
        orchestrator_provider=CloudProvider.GCE,
        orchestrator_name="crsbench-test-orch",
        orchestrator_project="test-project",
        orchestrator_zone="us-east5-b",
        orchestrator_internal_ip="10.0.0.50",
        orchestrator_external_ip="34.1.2.3",
        orchestrator_ssh_via_iap=True,
    )

    record = state.as_orchestrator_record()

    assert isinstance(record, CloudInstanceRecord)
    assert record.provider is CloudProvider.GCE
    assert record.role == "orchestrator"
    assert record.region == "us-east5"


def test_append_fleet_records_preserves_existing_launch_state() -> None:
    from crsbench.cloud.launch_state import CloudLaunchState, append_fleet_records

    state = CloudLaunchState(
        experiment_name="test-exp",
        config_path="/tmp/config.yaml",
        redis_host="10.0.0.1:6379",
        redis_password="secret",
        orchestrator_provider=CloudProvider.GCE,
        orchestrator_name="crsbench-test-orch",
        orchestrator_project="test-project",
        orchestrator_zone="us-east5-b",
        worker_fleet_configs=[
            CloudFleetPlacementRecord(
                provider=CloudProvider.GCE,
                role="worker",
                project="test-project",
                zone="us-east5-b",
                zones=["us-east5-b"],
                region="us-east5",
                count=2,
                name_prefix="crsbench-test-work",
                name_start_index=1,
            )
        ],
    )

    updated = append_fleet_records(
        state,
        workers=[
            CloudFleetPlacementRecord(
                provider=CloudProvider.GCE,
                role="worker",
                project="test-project",
                zone="us-east1-b",
                zones=["us-east1-b"],
                region="us-east1",
                count=1,
                name_prefix="crsbench-test-work",
                name_start_index=3,
                placement_source="runtime_added",
            )
        ],
    )

    assert len(state.worker_fleet_configs) == 1
    assert len(updated.worker_fleet_configs) == 2
    assert updated.worker_fleet_configs[0].placement_source == "config"
    assert updated.worker_fleet_configs[1].placement_source == "runtime_added"
