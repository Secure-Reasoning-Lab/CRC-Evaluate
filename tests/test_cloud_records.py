"""Tests for provider-neutral shared cloud record models."""

from __future__ import annotations

from crsbench.cloud.types import CloudProvider


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
    assert record.provider_metadata["github_deploy_key_path"] is None


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
