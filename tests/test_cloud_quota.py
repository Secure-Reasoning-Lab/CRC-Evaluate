"""Unit tests for provider-neutral cloud quota validation."""

from __future__ import annotations

import pytest
from crsbench.cloud.models import build_cloud_launch_plan
from crsbench.validation.schemas import ExperimentConfig


def _make_provider_neutral_experiment_config() -> ExperimentConfig:
    return ExperimentConfig.model_validate(
        {
            "experiment": "quota-exp",
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
                            "worker_count": 2,
                            "instance_profile": "worker-n2d",
                        },
                        {
                            "provider": "gce",
                            "zone": "us-east5-c",
                            "worker_count": 1,
                            "instance_profile": "worker-n2d",
                        },
                        {
                            "provider": "gce",
                            "zone": "us-east1-b",
                            "worker_count": 1,
                            "instance_profile": "worker-n2d",
                        },
                    ]
                },
            },
            "crs_compose": {"test-crs": {"num_cores": 1}},
        }
    )


class _QuotaClient:
    def __init__(self, availability: dict[tuple[str, str], int]) -> None:
        self.availability = availability
        self.lookups: list[tuple[str, str, str]] = []

    def get_available_capacity(
        self, *, project: str, region: str, resource_family: str
    ) -> int:
        self.lookups.append((project, region, resource_family))
        return self.availability[(region, resource_family)]


def test_gce_quota_requirements_include_orchestrator_and_group_by_region():
    from crsbench.cloud.gce.provider import GceProviderAdapter

    plan = build_cloud_launch_plan(_make_provider_neutral_experiment_config())
    adapter = GceProviderAdapter()

    requirements = adapter.quota_requirements(plan)

    assert requirements == [
        ("us-east1", "n2d", 16),
        ("us-east5", "n2d", 64),
    ]


def test_quota_validator_reports_normalized_shortage_before_launch():
    from crsbench.cloud.gce.provider import GceProviderAdapter
    from crsbench.cloud.quota import CloudQuotaValidationError, QuotaValidator

    plan = build_cloud_launch_plan(_make_provider_neutral_experiment_config())
    adapter = GceProviderAdapter(
        quota_client=_QuotaClient(
            {
                ("us-east5", "n2d"): 32,
                ("us-east1", "n2d"): 32,
            }
        )
    )

    validator = QuotaValidator(adapters={"gce": adapter})

    with pytest.raises(CloudQuotaValidationError, match="us-east5"):
        validator.validate(plan)
