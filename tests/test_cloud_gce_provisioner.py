"""Unit tests for the GCE worker provisioner boundary."""

import base64
import json

import pytest
from crsbench.cloud.models import build_cloud_launch_plan
from crsbench.distributed.registry import RuntimeRegistration
from crsbench.validation.schemas import (
    ExperimentConfig,
    GceOrchestratorConfig,
    GceWorkerFleetConfig,
)


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


def _make_orchestrator(**overrides) -> GceOrchestratorConfig:
    data = {
        "project": "test-project",
        "zone": "us-central1-a",
        "machine_type": "e2-standard-16",
        "boot_disk_size_gb": 200,
        "image": "projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64",
        "service_account_email": "crsbench-orchestrator@test-project.iam.gserviceaccount.com",
        "owner_label": "team-crs",
        "labels": {"env": "prod"},
        "metadata": {"custom-key": "custom-value"},
        "instance_name_prefix": "gce-orchestrator",
        "use_os_login": True,
        "ssh_via_iap": True,
    }
    data.update(overrides)
    return GceOrchestratorConfig(**data)


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
        source_instance_template: str | None = None,
    ) -> dict[str, object]:
        del source_instance_template
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


class _ExtendedOperation:
    def __init__(self, name: str) -> None:
        self.name = name


def _make_provider_neutral_experiment_config() -> ExperimentConfig:
    return ExperimentConfig.model_validate(
        {
            "experiment": "exp-cloud-42",
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
                            "ssh_via_iap": True,
                        },
                        "instance_profiles": {
                            "gce-orchestrator-n2d": {},
                            "gce-worker-n2d": {},
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
                    "placements": [
                        {
                            "zone": "us-east5-b",
                            "count": 2,
                        },
                        {
                            "zone": "us-east1-b",
                        },
                    ],
                },
            },
            "crs_compose": {"test-crs": {"num_cores": 1}},
        }
    )


def _make_provider_neutral_experiment_config_with_evaluators() -> ExperimentConfig:
    config = _make_provider_neutral_experiment_config().model_dump(
        mode="json", exclude_none=True
    )
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


def _make_provider_neutral_experiment_config_with_duplicate_zone_placements() -> (
    ExperimentConfig
):
    config = _make_provider_neutral_experiment_config().model_dump(
        mode="json", exclude_none=True
    )
    config["cloud"]["workers"]["placements"] = [
        {
            "zone": "us-east5-b",
            "count": 2,
        },
        {
            "zone": "us-east5-b",
            "count": 1,
        },
    ]
    return ExperimentConfig.model_validate(config)


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


def test_build_evaluator_requests_include_role_labels_and_config_metadata(tmp_path):
    """Evaluator requests should carry evaluator role labels and serialized config metadata."""
    from crsbench.cloud.gce.metadata import CRSBENCH_EXPERIMENT_CONFIG_B64_KEY
    from crsbench.cloud.gce.provisioner import GceProvisioner

    config_path = tmp_path / "config.yaml"
    config_path.write_text("experiment: exp-cloud-42\n", encoding="utf-8")
    provisioner = GceProvisioner(client=_RecordingClient())

    requests = provisioner.build_evaluator_requests(
        experiment_name="Exp.Cloud 42",
        fleet=_make_fleet(
            worker_name_prefix="evaluator-exp-cloud-42-us-east5-b",
            worker_count=1,
        ),
        redis_host="redis.internal:6380",
        registration=_make_registration(),
        experiment_config_path=config_path,
    )

    assert [request.name for request in requests] == [
        "evaluator-exp-cloud-42-us-east5-b-001",
    ]
    first = requests[0]
    assert first.labels["crsbench-role"] == "evaluator"
    assert CRSBENCH_EXPERIMENT_CONFIG_B64_KEY in first.metadata


def test_build_worker_names_rejects_invalid_gce_name_length():
    from crsbench.cloud.gce.provisioner import GceProvisioner, GceProvisioningError

    provisioner = GceProvisioner(client=_RecordingClient())

    with pytest.raises(
        GceProvisioningError, match="Generated GCE instance name is invalid"
    ):
        provisioner.build_worker_names(
            experiment_name="exp-cloud-42",
            fleet=_make_fleet(
                worker_name_prefix="crsbench-" + ("x" * 60),
                worker_count=1,
            ),
        )


def test_instance_request_renders_compute_proto_field_names() -> None:
    """Rendered instance resources must match compute_v1.Instance field names."""
    from crsbench.cloud.gce.models import GceInstanceRequest
    from google.cloud.compute_v1.types import Instance

    request = GceInstanceRequest(
        project="test-project",
        zone="us-central1-a",
        name="gce-worker-001",
        labels={"owner": "team-crs"},
        metadata={"startup-script": "#!/bin/bash"},
        service_account_email="crsbench-worker@test-project.iam.gserviceaccount.com",
        ssh_via_iap=True,
        assign_external_ip=True,
        machine_type="e2-standard-16",
        boot_disk_size_gb=200,
        image="projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64",
    )

    resource = request.to_instance_resource()
    instance = Instance(resource)

    assert instance.service_accounts[0].email == request.service_account_email
    assert instance.machine_type == "zones/us-central1-a/machineTypes/e2-standard-16"
    assert instance.network_interfaces == [Instance(resource).network_interfaces[0]]
    assert len(instance.network_interfaces[0].access_configs) == 1
    assert instance.disks[0].initialize_params.source_image == request.image
    assert instance.disks[0].initialize_params.disk_size_gb == 200


def test_instance_request_can_disable_external_nat() -> None:
    """Private-only launches should omit access configs explicitly."""
    from crsbench.cloud.gce.models import GceInstanceRequest

    request = GceInstanceRequest(
        project="test-project",
        zone="us-central1-a",
        name="gce-worker-001",
        labels={"owner": "team-crs"},
        metadata={"startup-script": "#!/bin/bash"},
        service_account_email="crsbench-worker@test-project.iam.gserviceaccount.com",
        ssh_via_iap=True,
        assign_external_ip=False,
        machine_type="e2-standard-16",
        boot_disk_size_gb=200,
        image="projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64",
    )

    resource = request.to_instance_resource()

    assert "access_configs" not in resource["network_interfaces"][0]


def test_gce_provider_adapter_resolves_named_instance_profile():
    from crsbench.cloud.gce.provider import GceProviderAdapter

    plan = build_cloud_launch_plan(_make_provider_neutral_experiment_config())
    adapter = GceProviderAdapter()

    resolved = adapter.resolve_instance_profile(plan.orchestrator.instance_profile)

    assert resolved.project == "test-project"
    assert resolved.machine_type == "n2d-standard-16"
    assert (
        resolved.service_account_email
        == "crsbench@test-project.iam.gserviceaccount.com"
    )


def test_gce_provider_adapter_builds_worker_fleets_per_placement():
    from crsbench.cloud.gce.provider import GceProviderAdapter

    plan = build_cloud_launch_plan(_make_provider_neutral_experiment_config())
    adapter = GceProviderAdapter()

    fleets = adapter.build_worker_fleets(plan)

    assert [fleet.zone for fleet in fleets] == ["us-east5-b", "us-east1-b"]
    assert [fleet.worker_count for fleet in fleets] == [2, 1]
    assert all(fleet.project == "test-project" for fleet in fleets)
    assert all(fleet.readiness_timeout_sec == 1200 for fleet in fleets)
    assert all(
        fleet.crsbench_install_spec
        == "git+ssh://git@github.com/sslab-gatech/CRSBench.git"
        for fleet in fleets
    )
    assert all(fleet.crsbench_git_ref == "feat/gcp" for fleet in fleets)
    assert [fleet.worker_name_prefix for fleet in fleets] == [
        "crsbench-exp-cloud-42-us-east5-b-work",
        "crsbench-exp-cloud-42-us-east1-b-work",
    ]


def test_gce_provider_adapter_offsets_worker_suffixes_for_same_zone_placements():
    from crsbench.cloud.gce.provider import GceProviderAdapter
    from crsbench.cloud.gce.provisioner import GceProvisioner

    plan = build_cloud_launch_plan(
        _make_provider_neutral_experiment_config_with_duplicate_zone_placements()
    )
    adapter = GceProviderAdapter()
    provisioner = GceProvisioner(client=_RecordingClient())

    fleets = adapter.build_worker_fleets(plan)

    assert [fleet.zone for fleet in fleets] == ["us-east5-b", "us-east5-b"]
    assert [fleet.worker_name_start_index for fleet in fleets] == [1, 3]
    assert provisioner.build_worker_names(
        experiment_name=plan.experiment_name,
        fleet=fleets[0],
    ) == [
        "crsbench-exp-cloud-42-us-east5-b-work-001",
        "crsbench-exp-cloud-42-us-east5-b-work-002",
    ]
    assert provisioner.build_worker_names(
        experiment_name=plan.experiment_name,
        fleet=fleets[1],
    ) == [
        "crsbench-exp-cloud-42-us-east5-b-work-003",
    ]


def test_gce_provider_adapter_builds_evaluator_fleets_per_placement():
    from crsbench.cloud.gce.provider import GceProviderAdapter

    plan = build_cloud_launch_plan(
        _make_provider_neutral_experiment_config_with_evaluators()
    )
    adapter = GceProviderAdapter()

    fleets = adapter.build_evaluator_fleets(plan)

    assert [fleet.zone for fleet in fleets] == ["us-east5-b", "us-east1-b"]
    assert [fleet.worker_count for fleet in fleets] == [1, 2]
    assert all(fleet.project == "test-project" for fleet in fleets)
    assert [fleet.worker_name_prefix for fleet in fleets] == [
        "crsbench-exp-cloud-42-us-east5-b-eval",
        "crsbench-exp-cloud-42-us-east1-b-eval",
    ]


def test_gce_provider_adapter_applies_provider_launch_defaults_override():
    from crsbench.cloud.gce.provider import GceProviderAdapter

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
    adapter = GceProviderAdapter()

    fleets = adapter.build_worker_fleets(plan)

    assert all(fleet.readiness_timeout_sec == 1500 for fleet in fleets)
    assert all(fleet.crsbench_git_ref == "provider-ref" for fleet in fleets)


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
            "labels": {
                "crsbench-experiment": "exp-cloud-42",
                "crsbench-role": "worker",
                "env": "prod",
                "owner": "team-crs",
            },
            "serviceAccounts": [
                {"email": "crsbench-worker@test-project.iam.gserviceaccount.com"}
            ],
        },
        {
            "id": "2001",
            "name": "gce-worker-other-owner",
            "status": "RUNNING",
            "zone": "zones/us-central1-a",
            "labels": {
                "crsbench-experiment": "exp-cloud-42",
                "crsbench-role": "worker",
                "env": "prod",
                "owner": "other-team",
            },
            "serviceAccounts": [
                {"email": "crsbench-worker@test-project.iam.gserviceaccount.com"}
            ],
        },
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


def test_create_workers_rolls_back_partial_fleet_on_failure() -> None:
    """Partial create failures should best-effort delete already-created VMs."""
    from crsbench.cloud.gce.provisioner import GceProvisioner, GceProvisioningError

    client = _RecordingClient()
    client.instances_by_name = {
        "gce-worker-001": {
            "id": "1001",
            "name": "gce-worker-001",
            "status": "RUNNING",
            "zone": "zones/us-central1-a",
            "labels": {"crsbench-experiment": "exp-cloud-42", "owner": "team-crs"},
            "serviceAccounts": [
                {"email": "crsbench-worker@test-project.iam.gserviceaccount.com"}
            ],
        }
    }
    client.operation_errors = {
        "op-gce-worker-002": {
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
            fleet=_make_fleet(worker_count=2),
            redis_host="redis.internal:6380",
            registration=_make_registration(),
        )

    assert set(client.deleted) == {
        ("test-project", "us-central1-a", "gce-worker-001"),
        ("test-project", "us-central1-a", "gce-worker-002"),
    }


def test_google_compute_client_accepts_extended_operation_objects() -> None:
    """Real GCE insert/delete calls return ExtendedOperation objects, not dicts."""
    from crsbench.cloud.gce.provisioner import GoogleComputeClient

    class _InstancesClient:
        def insert(self, **_kwargs) -> object:
            return _ExtendedOperation("insert-op")

        def get(self, **_kwargs) -> object:
            return {"id": "1001"}

        def list(self, **_kwargs) -> list[object]:
            return []

        def delete(self, **_kwargs) -> object:
            return _ExtendedOperation("delete-op")

    client = GoogleComputeClient(
        instances_client=_InstancesClient(),
        zone_operations_client=None,
    )

    assert client.insert_instance(
        project="test-project",
        zone="us-central1-a",
        instance_resource={"name": "gce-worker-001"},
    ) == {"name": "insert-op"}
    assert client.delete_instance(
        project="test-project",
        zone="us-central1-a",
        instance="gce-worker-001",
    ) == {"name": "delete-op"}


def test_google_compute_client_builds_label_filter_for_list_requests() -> None:
    """Instance listing should pass the full fleet selector to the provider API."""
    from crsbench.cloud.gce.provisioner import GoogleComputeClient

    class _InstancesClient:
        def __init__(self) -> None:
            self.requests: list[object] = []

        def insert(self, **_kwargs) -> object:
            raise AssertionError("insert not used")

        def get(self, **_kwargs) -> object:
            raise AssertionError("get not used")

        def list(self, request: object | None = None, **_kwargs) -> list[object]:
            self.requests.append(request)
            return []

        def delete(self, **_kwargs) -> object:
            raise AssertionError("delete not used")

    instances_client = _InstancesClient()
    client = GoogleComputeClient(
        instances_client=instances_client,
        zone_operations_client=None,
    )

    client.list_instances(
        project="test-project",
        zone="us-central1-a",
        label_selector={
            "crsbench-experiment": "exp-cloud-42",
            "crsbench-role": "worker",
            "owner": "team-crs",
        },
    )

    assert instances_client.requests == [
        {
            "project": "test-project",
            "zone": "us-central1-a",
            "filter": (
                '(labels.crsbench-experiment = "exp-cloud-42") '
                '(labels.crsbench-role = "worker") '
                '(labels.owner = "team-crs")'
            ),
        }
    ]


def test_google_compute_client_passes_source_instance_template_separately() -> None:
    """Instance templates belong on InsertInstanceRequest, not Instance."""
    from crsbench.cloud.gce.provisioner import GoogleComputeClient
    from google.cloud.compute_v1.types import InsertInstanceRequest, Instance

    class _InstancesClient:
        def __init__(self) -> None:
            self.calls: list[tuple[object | None, dict[str, object]]] = []

        def insert(self, request: object | None = None, **kwargs) -> object:
            self.calls.append((request, kwargs))
            return _ExtendedOperation("insert-op")

        def get(self, **_kwargs) -> object:
            raise AssertionError("get not used")

        def list(self, **_kwargs) -> list[object]:
            raise AssertionError("list not used")

        def delete(self, **_kwargs) -> object:
            raise AssertionError("delete not used")

    instances_client = _InstancesClient()
    client = GoogleComputeClient(
        instances_client=instances_client,
        zone_operations_client=None,
    )

    client.insert_instance(
        project="test-project",
        zone="us-central1-a",
        instance_resource={
            "name": "gce-worker-001",
            "metadata": {"items": [{"key": "startup-script", "value": "#!/bin/bash"}]},
            "service_accounts": [
                {
                    "email": "crsbench-worker@test-project.iam.gserviceaccount.com",
                    "scopes": ["https://www.googleapis.com/auth/cloud-platform"],
                }
            ],
            "network_interfaces": [{}],
        },
        source_instance_template="global/instanceTemplates/crsbench-template",
    )

    assert instances_client.calls == [
        (
            InsertInstanceRequest(
                {
                    "project": "test-project",
                    "zone": "us-central1-a",
                    "instance_resource": Instance(
                        {
                            "name": "gce-worker-001",
                            "metadata": {
                                "items": [
                                    {
                                        "key": "startup-script",
                                        "value": "#!/bin/bash",
                                    }
                                ]
                            },
                            "service_accounts": [
                                {
                                    "email": "crsbench-worker@test-project.iam.gserviceaccount.com",
                                    "scopes": [
                                        "https://www.googleapis.com/auth/cloud-platform"
                                    ],
                                }
                            ],
                            "network_interfaces": [{}],
                        }
                    ),
                    "source_instance_template": "global/instanceTemplates/crsbench-template",
                }
            ),
            {},
        )
    ]


def test_google_compute_client_omits_empty_source_instance_template() -> None:
    """Image-based launches must not serialize an empty template URL."""
    from crsbench.cloud.gce.provisioner import GoogleComputeClient
    from google.cloud.compute_v1.types import InsertInstanceRequest, Instance

    class _InstancesClient:
        def __init__(self) -> None:
            self.calls: list[tuple[object | None, dict[str, object]]] = []

        def insert(self, request: object | None = None, **kwargs) -> object:
            self.calls.append((request, kwargs))
            return _ExtendedOperation("insert-op")

        def get(self, **_kwargs) -> object:
            raise AssertionError("get not used")

        def list(self, **_kwargs) -> list[object]:
            raise AssertionError("list not used")

        def delete(self, **_kwargs) -> object:
            raise AssertionError("delete not used")

    instances_client = _InstancesClient()
    client = GoogleComputeClient(
        instances_client=instances_client,
        zone_operations_client=None,
    )

    client.insert_instance(
        project="test-project",
        zone="us-central1-a",
        instance_resource={
            "name": "gce-worker-001",
            "metadata": {"items": [{"key": "startup-script", "value": "#!/bin/bash"}]},
            "service_accounts": [
                {
                    "email": "crsbench-worker@test-project.iam.gserviceaccount.com",
                    "scopes": ["https://www.googleapis.com/auth/cloud-platform"],
                }
            ],
            "network_interfaces": [{}],
        },
    )

    assert instances_client.calls == [
        (
            InsertInstanceRequest(
                {
                    "project": "test-project",
                    "zone": "us-central1-a",
                    "instance_resource": Instance(
                        {
                            "name": "gce-worker-001",
                            "metadata": {
                                "items": [
                                    {
                                        "key": "startup-script",
                                        "value": "#!/bin/bash",
                                    }
                                ]
                            },
                            "service_accounts": [
                                {
                                    "email": "crsbench-worker@test-project.iam.gserviceaccount.com",
                                    "scopes": [
                                        "https://www.googleapis.com/auth/cloud-platform"
                                    ],
                                }
                            ],
                            "network_interfaces": [{}],
                        }
                    ),
                }
            ),
            {},
        )
    ]


def test_create_orchestrator_waits_for_operation_and_returns_instance_record():
    """Create orchestrator should return the normalized provider record."""
    from crsbench.cloud.gce.provisioner import GceProvisioner

    client = _RecordingClient()
    client.instances_by_name = {
        "gce-orchestrator-exp-cloud-42": {
            "id": "3001",
            "name": "gce-orchestrator-exp-cloud-42",
            "status": "RUNNING",
            "zone": "zones/us-central1-a",
            "networkInterfaces": [
                {
                    "networkIP": "10.0.0.50",
                    "accessConfigs": [{"natIP": "34.1.2.50"}],
                }
            ],
            "labels": {"crsbench-experiment": "exp-cloud-42", "owner": "team-crs"},
            "serviceAccounts": [
                {"email": "crsbench-orchestrator@test-project.iam.gserviceaccount.com"}
            ],
        }
    }
    provisioner = GceProvisioner(client=client)

    worker = provisioner.create_orchestrator(
        experiment_name="Exp.Cloud 42",
        orchestrator=_make_orchestrator(),
        experiment_config_path="config.yaml",
        redis_password="shared-secret",
    )

    assert worker.name == "gce-orchestrator-exp-cloud-42"
    assert worker.instance_id == "3001"
    assert worker.internal_ip == "10.0.0.50"
    assert client.waited == [
        ("test-project", "us-central1-a", "op-gce-orchestrator-exp-cloud-42")
    ]


def test_create_orchestrator_defaults_to_experiment_zone_type_name():
    """Default orchestrator names should sort with experiment and zone first."""
    from crsbench.cloud.gce.provisioner import GceProvisioner

    client = _RecordingClient()
    client.instances_by_name = {
        "crsbench-exp-cloud-42-us-east5-b-orch": {
            "id": "3002",
            "name": "crsbench-exp-cloud-42-us-east5-b-orch",
            "status": "RUNNING",
            "zone": "zones/us-east5-b",
            "networkInterfaces": [{"networkIP": "10.0.0.51"}],
            "labels": {"crsbench-experiment": "exp-cloud-42", "owner": "team-crs"},
            "serviceAccounts": [
                {"email": "crsbench-orchestrator@test-project.iam.gserviceaccount.com"}
            ],
        }
    }
    provisioner = GceProvisioner(client=client)

    worker = provisioner.create_orchestrator(
        experiment_name="Exp.Cloud 42",
        orchestrator=_make_orchestrator(
            zone="us-east5-b",
            instance_name_prefix=None,
        ),
        experiment_config_path="config.yaml",
        redis_password="shared-secret",
    )

    assert worker.name == "crsbench-exp-cloud-42-us-east5-b-orch"
    assert client.waited == [
        (
            "test-project",
            "us-east5-b",
            "op-crsbench-exp-cloud-42-us-east5-b-orch",
        )
    ]


def test_create_orchestrator_rejects_invalid_gce_name_length():
    from crsbench.cloud.gce.provisioner import GceProvisioner, GceProvisioningError

    provisioner = GceProvisioner(client=_RecordingClient())

    with pytest.raises(
        GceProvisioningError, match="Generated GCE instance name is invalid"
    ):
        provisioner.create_orchestrator(
            experiment_name="x" * 48,
            orchestrator=_make_orchestrator(
                zone="us-east5-b",
                instance_name_prefix=None,
            ),
            experiment_config_path="config.yaml",
            redis_password="shared-secret",
        )
