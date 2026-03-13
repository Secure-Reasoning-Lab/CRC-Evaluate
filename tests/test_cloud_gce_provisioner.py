"""Unit tests for the GCE worker provisioner boundary."""

import base64
import json

import pytest
from crsbench.distributed.registry import RuntimeRegistration
from crsbench.validation.schemas import GceWorkerFleetConfig


def _make_fleet(**overrides) -> GceWorkerFleetConfig:
    data = {
        "project": "test-project",
        "zone": "us-central1-a",
        "worker_count": 2,
        "machine_type": "e2-standard-16",
        "boot_disk_size_gb": 200,
        "image": "projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64",
        "service_account_email": "crsbench-worker@test-project.iam.gserviceaccount.com",
        "owner_label": "team-crs",
        "labels": {"env": "prod"},
        "metadata": {"crsbench-install-spec": "crsbench @ file:///opt/crsbench.whl"},
        "worker_name_prefix": "gce-worker",
        "use_os_login": True,
        "ssh_via_iap": True,
        "readiness_timeout_sec": 900,
    }
    data.update(overrides)
    return GceWorkerFleetConfig(**data)


def _make_registration(**overrides) -> RuntimeRegistration:
    data = {
        "experiment": "exp-cloud-42",
        "trial_queue": "crsbench_trial",
        "build_queue": "crsbench_build",
        "verify_queue": "crsbench_verify",
        "worker_jobs": 3,
        "worker_cores_per_job": 6,
        "worker_cpu_tag": "c3",
        "benchmarks_root": "/mnt/benchmarks",
        "modes": ["delta"],
        "sanitizers": ["address"],
        "config_hash": "cfg-hash",
    }
    data.update(overrides)
    return RuntimeRegistration(**data)


def _decode_payload(encoded: str) -> dict[str, object]:
    return json.loads(base64.b64decode(encoded).decode("utf-8"))


class _RecordingClient:
    def __init__(self) -> None:
        self.inserted: list[tuple[str, str, dict[str, object]]] = []
        self.waited: list[tuple[str, str, str]] = []
        self.deleted: list[tuple[str, str, str]] = []
        self.instances_by_name: dict[str, dict[str, object]] = {}
        self.listed_instances: list[dict[str, object]] = []
        self.operation_errors: dict[str, dict[str, object]] = {}

    def insert_instance(
        self,
        *,
        project: str,
        zone: str,
        instance_resource: dict[str, object],
    ) -> dict[str, object]:
        name = str(instance_resource["name"])
        self.inserted.append((project, zone, instance_resource))
        return {"name": f"op-{name}"}

    def wait_for_zone_operation(
        self,
        *,
        project: str,
        zone: str,
        operation: str,
    ) -> dict[str, object]:
        self.waited.append((project, zone, operation))
        return self.operation_errors.get(operation, {"status": "DONE"})

    def get_instance(
        self,
        *,
        project: str,
        zone: str,
        instance: str,
    ) -> dict[str, object]:
        return self.instances_by_name[instance]

    def list_instances(
        self,
        *,
        project: str,
        zone: str,
        label_selector: dict[str, str],
    ) -> list[dict[str, object]]:
        return list(self.listed_instances)

    def delete_instance(
        self,
        *,
        project: str,
        zone: str,
        instance: str,
    ) -> dict[str, object]:
        self.deleted.append((project, zone, instance))
        return {"name": f"delete-{instance}"}


def test_build_requests_include_experiment_identity_labels_and_bootstrap_metadata():
    """Rendered instance requests should carry stable names, labels, and payloads."""
    from crsbench.cloud.gce.metadata import CRSBENCH_BOOTSTRAP_PAYLOAD_KEY
    from crsbench.cloud.gce.provisioner import GceProvisioner

    provisioner = GceProvisioner(client=_RecordingClient())

    requests = provisioner.build_requests(
        experiment_name="Exp.Cloud 42",
        fleet=_make_fleet(),
        redis_host="redis.internal:6380",
        registration=_make_registration(),
    )

    assert [request.name for request in requests] == [
        "gce-worker-001",
        "gce-worker-002",
    ]
    first = requests[0]
    assert first.zone == "us-central1-a"
    assert first.labels["crsbench-experiment"] == "exp-cloud-42"
    assert first.labels["owner"] == "team-crs"
    assert first.labels["env"] == "prod"
    assert first.metadata["enable-oslogin"] == "TRUE"
    assert first.metadata["serial-port-enable"] == "TRUE"

    payload = _decode_payload(first.metadata[CRSBENCH_BOOTSTRAP_PAYLOAD_KEY])
    assert payload["experiment"] == "Exp.Cloud 42"
    assert payload["redis_host"] == "redis.internal:6380"
    assert payload["worker_name"] == "gce-worker-001"
    assert payload["worker_jobs"] == 3
    assert payload["worker_cores_per_job"] == 6
    assert payload["worker_cpu_tag"] == "c3"


def test_create_workers_waits_for_operations_and_normalizes_provider_instances():
    """Create should wait for each zonal operation and return worker records."""
    from crsbench.cloud.gce.provisioner import GceProvisioner

    client = _RecordingClient()
    client.instances_by_name = {
        "gce-worker-001": {
            "id": "1001",
            "name": "gce-worker-001",
            "status": "RUNNING",
            "zone": "zones/us-central1-a",
            "networkInterfaces": [
                {
                    "networkIP": "10.0.0.10",
                    "accessConfigs": [{"natIP": "34.1.2.3"}],
                }
            ],
            "labels": {"crsbench-experiment": "exp-cloud-42", "owner": "team-crs"},
            "serviceAccounts": [
                {"email": "crsbench-worker@test-project.iam.gserviceaccount.com"}
            ],
        },
        "gce-worker-002": {
            "id": "1002",
            "name": "gce-worker-002",
            "status": "PROVISIONING",
            "zone": "zones/us-central1-a",
            "networkInterfaces": [{"networkIP": "10.0.0.11"}],
            "labels": {"crsbench-experiment": "exp-cloud-42", "owner": "team-crs"},
            "serviceAccounts": [
                {"email": "crsbench-worker@test-project.iam.gserviceaccount.com"}
            ],
        },
    }
    provisioner = GceProvisioner(client=client)

    workers = provisioner.create_workers(
        experiment_name="Exp.Cloud 42",
        fleet=_make_fleet(),
        redis_host="redis.internal:6380",
        registration=_make_registration(),
    )

    assert [worker.name for worker in workers] == ["gce-worker-001", "gce-worker-002"]
    assert [worker.instance_id for worker in workers] == ["1001", "1002"]
    assert workers[0].zone == "us-central1-a"
    assert workers[0].internal_ip == "10.0.0.10"
    assert workers[0].external_ip == "34.1.2.3"
    assert workers[1].status == "PROVISIONING"
    assert client.waited == [
        ("test-project", "us-central1-a", "op-gce-worker-001"),
        ("test-project", "us-central1-a", "op-gce-worker-002"),
    ]


def test_create_workers_translates_operation_errors():
    """Provisioning failures should surface operation error details."""
    from crsbench.cloud.gce.provisioner import GceProvisioner, GceProvisioningError

    client = _RecordingClient()
    client.operation_errors = {
        "op-gce-worker-001": {
            "status": "DONE",
            "error": {
                "errors": [
                    {
                        "code": "ZONE_RESOURCE_POOL_EXHAUSTED",
                        "message": "No capacity left in zone",
                    }
                ]
            },
        }
    }
    provisioner = GceProvisioner(client=client)

    with pytest.raises(GceProvisioningError, match="ZONE_RESOURCE_POOL_EXHAUSTED"):
        provisioner.create_workers(
            experiment_name="Exp.Cloud 42",
            fleet=_make_fleet(worker_count=1),
            redis_host="redis.internal:6380",
            registration=_make_registration(),
        )


def test_list_and_delete_workers_use_experiment_scoped_labels():
    """List/delete should target only workers belonging to the experiment."""
    from crsbench.cloud.gce.provisioner import GceProvisioner

    client = _RecordingClient()
    client.listed_instances = [
        {
            "id": "1001",
            "name": "gce-worker-001",
            "status": "RUNNING",
            "zone": "zones/us-central1-a",
            "labels": {"crsbench-experiment": "exp-cloud-42", "owner": "team-crs"},
            "serviceAccounts": [
                {"email": "crsbench-worker@test-project.iam.gserviceaccount.com"}
            ],
        }
    ]
    provisioner = GceProvisioner(client=client)

    workers = provisioner.list_workers(
        experiment_name="Exp.Cloud 42",
        fleet=_make_fleet(worker_count=1),
    )
    deleted = provisioner.delete_workers(
        experiment_name="Exp.Cloud 42",
        fleet=_make_fleet(worker_count=1),
    )

    assert [worker.name for worker in workers] == ["gce-worker-001"]
    assert [worker.name for worker in deleted] == ["gce-worker-001"]
    assert client.deleted == [("test-project", "us-central1-a", "gce-worker-001")]
