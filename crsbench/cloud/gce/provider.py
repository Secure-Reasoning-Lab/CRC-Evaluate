"""Provider-neutral GCE adapter built on top of the zonal provisioner."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Sequence

from crsbench.cloud.gce.provisioner import GceProvisioner
from crsbench.cloud.gce.quota import (
    GceRegionalQuotaClient,
    machine_type_to_family,
    machine_type_to_vcpus,
    zone_to_region,
)
from crsbench.cloud.models import (
    CloudLaunchPlan,
    QuotaShortage,
    ResolvedInstanceProfile,
)
from crsbench.cloud.types import CloudProvider
from crsbench.validation.schemas import GceOrchestratorConfig, GceWorkerFleetConfig

if TYPE_CHECKING:
    from crsbench.cloud.bootstrap import CloudVmBootstrapInputs
    from crsbench.cloud.gce.models import GceWorkerRecord
    from crsbench.distributed.registry import RuntimeRegistration


@dataclass(frozen=True)
class ResolvedGceInstanceProfile:
    """Resolved GCE profile with provider defaults applied."""

    project: str
    machine_type: str | None
    boot_disk_size_gb: int | None
    image: str | None
    instance_template: str | None
    network: str | None
    subnetwork: str | None
    service_account_email: str
    owner_label: str | None
    labels: dict[str, str]
    metadata: dict[str, str]
    startup_script_uri: str | None
    use_os_login: bool
    ssh_via_iap: bool


class GceProviderAdapter:
    """Translate provider-neutral launch plans into GCE-specific operations."""

    def __init__(
        self,
        *,
        provisioner: GceProvisioner | None = None,
        quota_client: GceRegionalQuotaClient | None = None,
    ) -> None:
        self._provisioner = provisioner or GceProvisioner()
        self._quota_client = quota_client or GceRegionalQuotaClient()

    def resolve_instance_profile(
        self, instance_profile: ResolvedInstanceProfile
    ) -> ResolvedGceInstanceProfile:
        """Apply provider defaults to a resolved provider-neutral GCE profile."""
        provider_config = instance_profile.provider_config
        profile_config = instance_profile.profile_config
        return ResolvedGceInstanceProfile(
            project=str(provider_config["project"]),
            machine_type=_get_optional_str(profile_config, "machine_type"),
            boot_disk_size_gb=_get_optional_int(profile_config, "boot_disk_size_gb"),
            image=_get_optional_str(profile_config, "image"),
            instance_template=_get_optional_str(profile_config, "instance_template"),
            network=_get_optional_str(profile_config, "network")
            or _get_optional_str(provider_config, "network"),
            subnetwork=_get_optional_str(profile_config, "subnetwork")
            or _get_optional_str(provider_config, "subnetwork"),
            service_account_email=str(profile_config["service_account_email"]),
            owner_label=_get_optional_str(profile_config, "owner_label"),
            labels=_get_string_map(profile_config, "labels"),
            metadata=_get_string_map(profile_config, "metadata"),
            startup_script_uri=_get_optional_str(profile_config, "startup_script_uri"),
            use_os_login=bool(profile_config.get("use_os_login", True)),
            ssh_via_iap=bool(
                profile_config.get(
                    "ssh_via_iap",
                    provider_config.get("ssh_via_iap", False),
                )
            ),
        )

    def build_orchestrator_config(self, plan: CloudLaunchPlan) -> GceOrchestratorConfig:
        """Build the legacy GCE orchestrator config consumed by the provisioner."""
        resolved = self.resolve_instance_profile(plan.orchestrator.instance_profile)
        return GceOrchestratorConfig(
            project=resolved.project,
            zone=plan.orchestrator.zone,
            machine_type=resolved.machine_type,
            boot_disk_size_gb=resolved.boot_disk_size_gb,
            image=resolved.image,
            instance_template=resolved.instance_template,
            network=resolved.network,
            subnetwork=resolved.subnetwork,
            service_account_email=resolved.service_account_email,
            owner_label=resolved.owner_label,
            labels=resolved.labels,
            metadata=resolved.metadata,
            startup_script_uri=resolved.startup_script_uri,
            use_os_login=resolved.use_os_login,
            ssh_via_iap=resolved.ssh_via_iap,
            crsbench_install_spec=plan.orchestrator.launch_defaults.crsbench_install_spec,
            crsbench_git_ref=plan.orchestrator.launch_defaults.crsbench_git_ref
            or "main",
            github_deploy_key_file=plan.orchestrator.launch_defaults.github_deploy_key_file,
        )

    def build_worker_fleets(self, plan: CloudLaunchPlan) -> list[GceWorkerFleetConfig]:
        """Build one legacy worker-fleet config per provider-neutral placement."""
        fleets: list[GceWorkerFleetConfig] = []
        next_index_by_zone: dict[str, int] = {}
        for placement in plan.worker_placements:
            if placement.provider is not CloudProvider.GCE:
                continue
            resolved = self.resolve_instance_profile(placement.instance_profile)
            start_index = next_index_by_zone.get(placement.zone, 1)
            fleets.append(
                GceWorkerFleetConfig(
                    project=resolved.project,
                    zone=placement.zone,
                    worker_count=placement.count,
                    worker_name_start_index=start_index,
                    machine_type=resolved.machine_type,
                    boot_disk_size_gb=resolved.boot_disk_size_gb,
                    image=resolved.image,
                    instance_template=resolved.instance_template,
                    network=resolved.network,
                    subnetwork=resolved.subnetwork,
                    service_account_email=resolved.service_account_email,
                    owner_label=resolved.owner_label,
                    labels=resolved.labels,
                    metadata=resolved.metadata,
                    worker_name_prefix=(
                        f"crsbench-{plan.experiment_name}-{placement.zone}-work"
                    ),
                    startup_script_uri=resolved.startup_script_uri,
                    use_os_login=resolved.use_os_login,
                    ssh_via_iap=resolved.ssh_via_iap,
                    readiness_timeout_sec=placement.launch_defaults.readiness_timeout_sec
                    or 900,
                    crsbench_install_spec=placement.launch_defaults.crsbench_install_spec,
                    crsbench_git_ref=placement.launch_defaults.crsbench_git_ref
                    or "main",
                    github_deploy_key_file=placement.launch_defaults.github_deploy_key_file,
                )
            )
            next_index_by_zone[placement.zone] = start_index + placement.count
        return fleets

    def build_evaluator_fleets(
        self,
        plan: CloudLaunchPlan,
    ) -> list[GceWorkerFleetConfig]:
        """Build one legacy fleet config per provider-neutral evaluator placement."""
        fleets: list[GceWorkerFleetConfig] = []
        next_index_by_zone: dict[str, int] = {}
        for placement in plan.evaluator_placements:
            if placement.provider is not CloudProvider.GCE:
                continue
            resolved = self.resolve_instance_profile(placement.instance_profile)
            start_index = next_index_by_zone.get(placement.zone, 1)
            fleets.append(
                GceWorkerFleetConfig(
                    project=resolved.project,
                    zone=placement.zone,
                    worker_count=placement.count,
                    worker_name_start_index=start_index,
                    machine_type=resolved.machine_type,
                    boot_disk_size_gb=resolved.boot_disk_size_gb,
                    image=resolved.image,
                    instance_template=resolved.instance_template,
                    network=resolved.network,
                    subnetwork=resolved.subnetwork,
                    service_account_email=resolved.service_account_email,
                    owner_label=resolved.owner_label,
                    labels=resolved.labels,
                    metadata=resolved.metadata,
                    worker_name_prefix=(
                        f"crsbench-{plan.experiment_name}-{placement.zone}-eval"
                    ),
                    startup_script_uri=resolved.startup_script_uri,
                    use_os_login=resolved.use_os_login,
                    ssh_via_iap=resolved.ssh_via_iap,
                    readiness_timeout_sec=placement.launch_defaults.readiness_timeout_sec
                    or 900,
                    crsbench_install_spec=placement.launch_defaults.crsbench_install_spec,
                    crsbench_git_ref=placement.launch_defaults.crsbench_git_ref
                    or "main",
                    github_deploy_key_file=placement.launch_defaults.github_deploy_key_file,
                )
            )
            next_index_by_zone[placement.zone] = start_index + placement.count
        return fleets

    def quota_requirements(
        self,
        plan: CloudLaunchPlan,
        *,
        include_orchestrator: bool = True,
    ) -> list[tuple[str, str, int]]:
        """Return aggregated regional quota demand as `(region, family, required)`."""
        requirements: dict[tuple[str, str], int] = {}

        if include_orchestrator and plan.orchestrator.provider is CloudProvider.GCE:
            resolved = self.resolve_instance_profile(plan.orchestrator.instance_profile)
            _accumulate_requirement(
                requirements=requirements,
                zone=plan.orchestrator.zone,
                machine_type=resolved.machine_type,
                count=1,
            )

        for placement in plan.worker_placements:
            if placement.provider is not CloudProvider.GCE:
                continue
            resolved = self.resolve_instance_profile(placement.instance_profile)
            _accumulate_requirement(
                requirements=requirements,
                zone=placement.zone,
                machine_type=resolved.machine_type,
                count=placement.count,
            )

        for placement in plan.evaluator_placements:
            if placement.provider is not CloudProvider.GCE:
                continue
            resolved = self.resolve_instance_profile(placement.instance_profile)
            _accumulate_requirement(
                requirements=requirements,
                zone=placement.zone,
                machine_type=resolved.machine_type,
                count=placement.count,
            )

        return [
            (region, family, required)
            for (region, family), required in sorted(requirements.items())
        ]

    def quota_shortages(
        self,
        plan: CloudLaunchPlan,
        *,
        include_orchestrator: bool = True,
    ) -> list[QuotaShortage]:
        """Return normalized quota shortages for the GCE portions of a plan."""
        project = self.resolve_instance_profile(
            plan.orchestrator.instance_profile
        ).project
        shortages: list[QuotaShortage] = []
        for region, family, required in self.quota_requirements(
            plan,
            include_orchestrator=include_orchestrator,
        ):
            available = self._quota_client.get_available_capacity(
                project=project,
                region=region,
                resource_family=family,
            )
            if available < required:
                shortages.append(
                    QuotaShortage(
                        provider=CloudProvider.GCE,
                        scope=region,
                        resource_family=family,
                        required=required,
                        available=available,
                    )
                )
        return shortages

    def create_orchestrator(
        self,
        *,
        plan: CloudLaunchPlan,
        experiment_config_path: str,
        env_passthrough: dict[str, str] | None = None,
        redis_password: str,
    ) -> "GceWorkerRecord":
        """Create the remote orchestrator VM for a provider-neutral launch plan."""
        return self._provisioner.create_orchestrator(
            experiment_name=plan.experiment_name,
            orchestrator=self.build_orchestrator_config(plan),
            experiment_config_path=experiment_config_path,
            env_passthrough=env_passthrough,
            redis_password=redis_password,
        )

    def create_workers(
        self,
        *,
        plan: CloudLaunchPlan,
        redis_host: str,
        redis_password: str | None,
        registration: "RuntimeRegistration",
        bootstrap_inputs: "CloudVmBootstrapInputs | None" = None,
        env_passthrough_by_placement: Sequence[dict[str, str]] | None = None,
    ) -> list["GceWorkerRecord"]:
        """Create workers across all zonal placements in the launch plan."""
        workers: list[GceWorkerRecord] = []
        fleets = self.build_worker_fleets(plan)
        _validate_env_passthrough_count(
            role="worker",
            fleets=fleets,
            env_passthrough_by_placement=env_passthrough_by_placement,
        )
        for index, fleet in enumerate(fleets):
            workers.extend(
                self._provisioner.create_workers(
                    experiment_name=plan.experiment_name,
                    fleet=fleet,
                    redis_host=redis_host,
                    redis_password=redis_password,
                    registration=registration,
                    bootstrap_inputs=bootstrap_inputs,
                    env_passthrough=_env_passthrough_for_placement(
                        env_passthrough_by_placement, index
                    ),
                )
            )
        return workers

    def list_workers(self, *, plan: CloudLaunchPlan) -> list["GceWorkerRecord"]:
        """List workers across all placements in a provider-neutral launch plan."""
        workers: list[GceWorkerRecord] = []
        for fleet in self.build_worker_fleets(plan):
            workers.extend(
                self._provisioner.list_workers(
                    experiment_name=plan.experiment_name,
                    fleet=fleet,
                )
            )
        return workers

    def delete_workers(self, *, plan: CloudLaunchPlan) -> list["GceWorkerRecord"]:
        """Delete workers across all placements in a provider-neutral launch plan."""
        workers: list[GceWorkerRecord] = []
        for fleet in self.build_worker_fleets(plan):
            workers.extend(
                self._provisioner.delete_workers(
                    experiment_name=plan.experiment_name,
                    fleet=fleet,
                )
            )
        return workers

    def create_evaluators(
        self,
        *,
        plan: CloudLaunchPlan,
        redis_host: str,
        redis_password: str | None,
        registration: "RuntimeRegistration",
        experiment_config_path: str,
        bootstrap_inputs: "CloudVmBootstrapInputs | None" = None,
        env_passthrough_by_placement: Sequence[dict[str, str]] | None = None,
    ) -> list["GceWorkerRecord"]:
        """Create evaluators across all placements in a provider-neutral launch plan."""
        evaluators: list[GceWorkerRecord] = []
        fleets = self.build_evaluator_fleets(plan)
        _validate_env_passthrough_count(
            role="evaluator",
            fleets=fleets,
            env_passthrough_by_placement=env_passthrough_by_placement,
        )
        for index, fleet in enumerate(fleets):
            evaluators.extend(
                self._provisioner.create_evaluators(
                    experiment_name=plan.experiment_name,
                    fleet=fleet,
                    redis_host=redis_host,
                    redis_password=redis_password,
                    registration=registration,
                    experiment_config_path=experiment_config_path,
                    bootstrap_inputs=bootstrap_inputs,
                    env_passthrough=_env_passthrough_for_placement(
                        env_passthrough_by_placement, index
                    ),
                )
            )
        return evaluators

    def list_evaluators(self, *, plan: CloudLaunchPlan) -> list["GceWorkerRecord"]:
        """List evaluators across all placements in a provider-neutral launch plan."""
        evaluators: list[GceWorkerRecord] = []
        for fleet in self.build_evaluator_fleets(plan):
            evaluators.extend(
                self._provisioner.list_evaluators(
                    experiment_name=plan.experiment_name,
                    fleet=fleet,
                )
            )
        return evaluators

    def delete_evaluators(self, *, plan: CloudLaunchPlan) -> list["GceWorkerRecord"]:
        """Delete evaluators across all placements in a provider-neutral launch plan."""
        evaluators: list[GceWorkerRecord] = []
        for fleet in self.build_evaluator_fleets(plan):
            evaluators.extend(
                self._provisioner.delete_evaluators(
                    experiment_name=plan.experiment_name,
                    fleet=fleet,
                )
            )
        return evaluators


def _accumulate_requirement(
    *,
    requirements: dict[tuple[str, str], int],
    zone: str,
    machine_type: str | None,
    count: int,
) -> None:
    if machine_type is None:
        raise ValueError(
            "GCE quota validation requires instance profiles with explicit machine_type"
        )
    region = zone_to_region(zone)
    family = machine_type_to_family(machine_type)
    requirements[(region, family)] = requirements.get((region, family), 0) + (
        machine_type_to_vcpus(machine_type) * count
    )


def _validate_env_passthrough_count(
    *,
    role: str,
    fleets: Sequence[GceWorkerFleetConfig],
    env_passthrough_by_placement: Sequence[dict[str, str]] | None,
) -> None:
    if env_passthrough_by_placement is None:
        return
    if len(env_passthrough_by_placement) != len(fleets):
        raise ValueError(
            f"Expected {len(fleets)} {role} env payloads, got "
            f"{len(env_passthrough_by_placement)}"
        )


def _env_passthrough_for_placement(
    env_passthrough_by_placement: Sequence[dict[str, str]] | None,
    index: int,
) -> dict[str, str] | None:
    if env_passthrough_by_placement is None:
        return None
    return env_passthrough_by_placement[index]


def _get_optional_str(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    return str(value)


def _get_optional_int(data: dict[str, Any], key: str) -> int | None:
    value = data.get(key)
    if value is None:
        return None
    return int(value)


def _get_string_map(data: dict[str, Any], key: str) -> dict[str, str]:
    value = data.get(key)
    if not isinstance(value, dict):
        return {}
    return {str(map_key): str(map_value) for map_key, map_value in value.items()}
