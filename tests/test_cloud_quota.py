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
                        "profile_defaults": {
                            "machine_type": "n2d-standard-16",
                            "boot_disk_size_gb": 50,
                            "image": "projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64",
                            "service_account_email": "crsbench@test-project.iam.gserviceaccount.com",
                            "owner_label": "team-crs",
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
                            "zone": "us-east5-c",
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
                ("us-east1", "n2d"): 0,
            }
        )
    )

    validator = QuotaValidator(adapters={"gce": adapter})

    with pytest.raises(CloudQuotaValidationError, match="us-east5"):
        validator.validate(plan)


def test_quota_validator_allows_multi_region_fallback_when_later_region_has_capacity():
    from crsbench.cloud.gce.provider import GceProviderAdapter
    from crsbench.cloud.quota import QuotaValidator

    config = _make_provider_neutral_experiment_config()
    assert config.cloud is not None
    assert config.cloud.workers is not None
    config.cloud.workers.placements = [
        config.cloud.workers.placements[0].model_copy(
            update={
                "region": "us-east5",
                "regions": ["us-east5", "us-east1"],
                "zones": ["us-east5-b", "us-east1-b"],
                "count": 2,
            }
        )
    ]
    plan = build_cloud_launch_plan(
        ExperimentConfig.model_validate(
            config.model_dump(mode="json", exclude_none=True)
        )
    )
    adapter = GceProviderAdapter(
        quota_client=_QuotaClient(
            {
                ("us-east5", "n2d"): 0,
                ("us-east1", "n2d"): 32,
            }
        )
    )

    validator = QuotaValidator(adapters={"gce": adapter})

    validator.validate(plan, include_orchestrator=False)


def test_quota_validator_rejects_multiple_flexible_placements_that_overcommit_shared_regions():
    from crsbench.cloud.gce.provider import GceProviderAdapter
    from crsbench.cloud.quota import CloudQuotaValidationError, QuotaValidator

    config = _make_provider_neutral_experiment_config()
    assert config.cloud is not None
    assert config.cloud.workers is not None
    config.cloud.workers.placements = [
        config.cloud.workers.placements[0].model_copy(
            update={
                "region": "us-east5",
                "regions": ["us-east5", "us-east1"],
                "zones": ["us-east5-b", "us-east1-b"],
                "count": 2,
            }
        ),
        config.cloud.workers.placements[1].model_copy(
            update={
                "region": "us-east5",
                "regions": ["us-east5", "us-east1"],
                "zones": ["us-east5-c", "us-east1-c"],
                "count": 2,
            }
        ),
    ]
    plan = build_cloud_launch_plan(
        ExperimentConfig.model_validate(
            config.model_dump(mode="json", exclude_none=True)
        )
    )
    adapter = GceProviderAdapter(
        quota_client=_QuotaClient(
            {
                ("us-east5", "n2d"): 32,
                ("us-east1", "n2d"): 0,
            }
        )
    )

    validator = QuotaValidator(adapters={"gce": adapter})

    with pytest.raises(
        CloudQuotaValidationError,
        match="us-east5\\|us-east1|us-east1\\|us-east5",
    ):
        validator.validate(plan, include_orchestrator=False)
