"""Shared config reconnect helper for standalone cloud CLI commands."""

from __future__ import annotations

import atexit
import dataclasses
import os
import re
import weakref
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeAlias, cast

from crsbench.cloud.gce.metadata import (
    CRSBENCH_REDIS_PASSWORD_KEY,
    CRSBENCH_SSH_VIA_IAP_METADATA_KEY,
)
from crsbench.cloud.launch_state import (
    CloudLaunchState,
    find_launch_state_for_source_experiment,
    load_launch_state,
    save_launch_state,
)
from crsbench.cloud.locations import region_for_provider_zone
from crsbench.cloud.models import CloudLaunchPlan, build_cloud_launch_plan
from crsbench.cloud.orchestrator_tunnel import OrchestratorRedisTunnel
from crsbench.cloud.providers import provider_adapter_for_launch_plan
from crsbench.cloud.readiness import CloudReadinessStore
from crsbench.distributed.job_lifecycle import JobLifecycleStore
from crsbench.distributed.queue import (
    create_redis_connection,
    wait_for_redis_connection,
)
from crsbench.run_experiment import load_experiment_config
from crsbench.utils.logger import get_logger
from crsbench.utils.storage_warning import warn_for_persisted_storage_roots
from crsbench.validation.schemas import CloudOrchestratorPlacementConfig

if TYPE_CHECKING:
    from crsbench.cloud.readiness import ReadinessRedisProtocol
    from crsbench.cloud.records import CloudFleetPlacementRecord, CloudInstanceLike
    from crsbench.distributed.job_lifecycle import LifecycleRedisProtocol


@dataclasses.dataclass(frozen=True)
class ResolvedCloudContext:
    """Resolved cloud runtime context for standalone operational commands."""

    experiment_name: str
    worker_fleet_configs: list[CloudFleetPlacementRecord]
    launch_state: CloudLaunchState | None
    experiment_filestore: Path
    remote_experiment_root: Path
    redis_host: str
    redis_password: str | None
    launch_plan: CloudLaunchPlan | None = None
    evaluator_fleet_configs: list[CloudFleetPlacementRecord] = dataclasses.field(
        default_factory=list
    )


logger = get_logger(__name__)
_DEFAULT_REMOTE_REDIS_READY_TIMEOUT_SEC = 300
_GCE_INDEXED_NAME_RE = re.compile(r"^(?P<prefix>.+)-(?P<index>\d{3})$")
_SyntheticFleetGroupKey: TypeAlias = tuple[
    str,
    str,
    str,
    bool,
    tuple[tuple[str, str], ...],
    str | None,
]


def resolve_effective_experiment_name(
    config_path: str,
    experiment_name: str | None,
) -> str:
    """Return the CLI experiment name, inferring from config when omitted."""
    if experiment_name:
        return experiment_name
    resolved_config_path = Path(config_path)
    config = load_experiment_config(resolved_config_path)
    try:
        launch_state = (
            find_launch_state_for_source_experiment(
                resolved_config_path,
                config.experiment,
            )
            if resolved_config_path.exists()
            else None
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if launch_state is not None:
        return launch_state.effective_remote_experiment_name()
    return config.experiment


def resolve_remote_experiment_dir(
    experiment_filestore: Path,
    remote_experiment_root: Path,
    experiment_name: str,
    remote_dir: str | None,
    *,
    launch_state: CloudLaunchState | None = None,
) -> str:
    """Return the remote experiment tree path used by standalone cloud commands."""
    if remote_dir:
        return remote_dir
    if launch_state is not None and launch_state.launch_mode == "reeval":
        return str(remote_experiment_root / experiment_name)
    return str(experiment_filestore / experiment_name)


def _resolve_remote_redis_ready_timeout_sec(context: ResolvedCloudContext) -> int:
    """Return how long reconnect callers should wait for remote Redis startup."""
    launch_plan = context.launch_plan
    launch_defaults = getattr(
        getattr(launch_plan, "orchestrator", None), "launch_defaults", None
    )
    timeout = getattr(launch_defaults, "readiness_timeout_sec", None)
    if isinstance(timeout, int) and timeout > 0:
        return timeout
    return _DEFAULT_REMOTE_REDIS_READY_TIMEOUT_SEC


def _register_tunnel_cleanup(redis_conn, tunnel: OrchestratorRedisTunnel) -> None:
    """Keep a remote Redis tunnel alive for the session and stop it on teardown."""
    atexit.register(tunnel.stop)
    try:
        weakref.finalize(redis_conn, tunnel.stop)
    except TypeError:
        pass
    try:
        redis_conn._crsbench_orchestrator_tunnel = tunnel
    except Exception:
        logger.debug("Unable to attach orchestrator tunnel handle to Redis client")


def resolve_cloud_context(
    config_path: str,
    experiment_name: str,
) -> ResolvedCloudContext:
    """Resolve cloud command context without requiring a live Redis connection."""
    config = load_experiment_config(Path(config_path))
    if config.cloud is None:
        raise SystemExit("Experiment config has no 'cloud' section.")

    launch_plan: CloudLaunchPlan | None = None
    derived_worker_fleets: list[CloudFleetPlacementRecord] = []
    derived_evaluator_fleets: list[CloudFleetPlacementRecord] = []
    uses_provider_neutral_cloud = (
        config.cloud.providers is not None
        and config.cloud.workers is not None
        and isinstance(config.cloud.orchestrator, CloudOrchestratorPlacementConfig)
    )
    if not uses_provider_neutral_cloud:
        raise SystemExit(
            "Experiment config must use provider-neutral cloud.providers/cloud.orchestrator/cloud.workers"
        )
    launch_plan = build_cloud_launch_plan(config)
    adapter = provider_adapter_for_launch_plan(launch_plan)
    derived_worker_fleets = [
        adapter.to_cloud_fleet_placement_record(fleet, role="worker")
        for fleet in adapter.build_worker_fleets(launch_plan)
    ]
    derived_evaluator_fleets = [
        adapter.to_cloud_fleet_placement_record(fleet, role="evaluator")
        for fleet in adapter.build_evaluator_fleets(launch_plan)
    ]

    launch_state = load_launch_state(Path(config_path), experiment_name)
    effective_experiment_name = experiment_name
    if (
        launch_state is None
        and Path(config_path).exists()
        and experiment_name == config.experiment
    ):
        try:
            launch_state = find_launch_state_for_source_experiment(
                Path(config_path),
                config.experiment,
            )
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
    if launch_state is None and Path(config_path).exists():
        launch_state = _reconstruct_launch_state_from_live_orchestrator(
            config_path=Path(config_path),
            config=config,
            experiment_name=experiment_name,
            launch_plan=launch_plan,
            adapter=adapter,
            derived_worker_fleets=derived_worker_fleets,
            derived_evaluator_fleets=derived_evaluator_fleets,
        )
    if launch_state is not None:
        effective_experiment_name = launch_state.effective_remote_experiment_name()
    launch_state_changed = False
    if launch_state is not None:
        launch_state_updates: dict[str, object] = {}
        if launch_state.experiment_filestore is None:
            launch_state_updates["experiment_filestore"] = str(
                config.experiment_filestore
            )
        if launch_state.remote_experiment_root is None:
            launch_state_updates["remote_experiment_root"] = str(
                _resolve_remote_experiment_root_from_config(config)
            )
        if (
            launch_state.launch_mode != "reeval"
            and not launch_state.worker_fleet_configs
            and derived_worker_fleets
        ):
            launch_state_updates["worker_fleet_configs"] = derived_worker_fleets
        if not launch_state.evaluator_fleet_configs and derived_evaluator_fleets:
            launch_state_updates["evaluator_fleet_configs"] = derived_evaluator_fleets
        if launch_state_updates:
            launch_state = launch_state.model_copy(update=launch_state_updates)
            launch_state_changed = True
        if launch_state_changed:
            try:
                save_launch_state(Path(config_path), launch_state)
            except OSError as exc:
                logger.warning(
                    "Failed to persist migrated launch state next to config {}: {}",
                    config_path,
                    exc,
                )

    if launch_state is not None and config.cloud.orchestrator is not None:
        if launch_state.experiment_filestore is None:
            raise SystemExit(
                "Remote orchestrator launch state missing experiment filestore"
            )
        if launch_state.remote_experiment_root is None:
            raise SystemExit(
                "Remote orchestrator launch state missing remote experiment root"
            )
        if (
            launch_state.launch_mode != "reeval"
            and not launch_state.worker_fleet_configs
        ):
            raise SystemExit(
                "Remote orchestrator launch state missing worker fleet config"
            )
        context = ResolvedCloudContext(
            experiment_name=effective_experiment_name,
            worker_fleet_configs=launch_state.resolved_worker_fleets(),
            evaluator_fleet_configs=launch_state.resolved_evaluator_fleets(),
            launch_state=launch_state,
            experiment_filestore=Path(launch_state.experiment_filestore),
            remote_experiment_root=Path(launch_state.remote_experiment_root),
            redis_host=launch_state.redis_host,
            redis_password=launch_state.redis_password,
            launch_plan=launch_plan,
        )
        _warn_for_cloud_storage_roots(context)
        return context

    if not derived_worker_fleets:
        raise SystemExit("Experiment config has no supported cloud worker config.")

    context = ResolvedCloudContext(
        experiment_name=effective_experiment_name,
        worker_fleet_configs=derived_worker_fleets,
        evaluator_fleet_configs=derived_evaluator_fleets,
        launch_state=launch_state,
        experiment_filestore=Path(config.experiment_filestore),
        remote_experiment_root=_resolve_remote_experiment_root_from_config(config),
        redis_host=config.redis_host or "localhost",
        redis_password=os.environ.get("CRSBENCH_REDIS_PASSWORD"),
        launch_plan=launch_plan,
    )
    _warn_for_cloud_storage_roots(context)
    return context


def _reconstruct_launch_state_from_live_orchestrator(
    *,
    config_path: Path,
    config,
    experiment_name: str,
    launch_plan: CloudLaunchPlan,
    adapter,
    derived_worker_fleets: list["CloudFleetPlacementRecord"],
    derived_evaluator_fleets: list["CloudFleetPlacementRecord"],
) -> CloudLaunchState | None:
    """Rebuild missing local launch state from live provider inventory."""
    try:
        orchestrators = adapter.list_orchestrators(plan=launch_plan)
    except Exception as exc:
        logger.debug(
            "Unable to reconstruct launch state for {} from live orchestrator: {}",
            experiment_name,
            exc,
        )
        return None
    if not orchestrators:
        return None
    if len(orchestrators) > 1:
        names = ", ".join(sorted(orchestrator.name for orchestrator in orchestrators))
        raise SystemExit(
            f"Multiple live orchestrators found for experiment {experiment_name!r}: "
            f"{names}. Restore the original launch state or tear down stale launches."
        )

    orchestrator = orchestrators[0]
    if not orchestrator.internal_ip:
        raise SystemExit(
            f"Live orchestrator {orchestrator.name!r} has no internal IP; "
            "cannot reconstruct Redis tunnel state"
        )
    redis_password = _metadata_value(orchestrator, CRSBENCH_REDIS_PASSWORD_KEY)
    if not redis_password:
        raise SystemExit(
            f"Live orchestrator {orchestrator.name!r} is missing "
            f"{CRSBENCH_REDIS_PASSWORD_KEY!r} metadata; cannot reconnect"
        )

    orchestrator_config = adapter.build_orchestrator_config(launch_plan)
    launch_state = CloudLaunchState(
        experiment_name=experiment_name,
        config_path=str(config_path),
        experiment_filestore=str(config.experiment_filestore),
        remote_experiment_root=str(_resolve_remote_experiment_root_from_config(config)),
        redis_host=f"{orchestrator.internal_ip}:6379",
        redis_password=redis_password,
        orchestrator_name=orchestrator.name,
        orchestrator_project=orchestrator_config.project,
        orchestrator_zone=orchestrator.zone,
        orchestrator_internal_ip=orchestrator.internal_ip,
        orchestrator_external_ip=orchestrator.external_ip,
        orchestrator_ssh_via_iap=_instance_ssh_via_iap(
            orchestrator,
            template=None,
            fallback=orchestrator_config.ssh_via_iap,
        ),
        worker_fleet_configs=_append_live_instance_fleet_records(
            adapter=adapter,
            launch_plan=launch_plan,
            role="worker",
            base_fleets=derived_worker_fleets,
            template_fleets=[
                *derived_worker_fleets,
                *derived_evaluator_fleets,
            ],
        ),
        evaluator_fleet_configs=_append_live_instance_fleet_records(
            adapter=adapter,
            launch_plan=launch_plan,
            role="evaluator",
            base_fleets=derived_evaluator_fleets,
            template_fleets=[
                *derived_evaluator_fleets,
                *derived_worker_fleets,
            ],
        ),
    )
    try:
        save_launch_state(config_path, launch_state)
    except OSError as exc:
        logger.warning(
            "Failed to persist reconstructed launch state next to config {}: {}",
            config_path,
            exc,
        )
    else:
        logger.info(
            "Reconstructed cloud launch state for experiment {} from live "
            "orchestrator {}",
            experiment_name,
            orchestrator.name,
        )
    return launch_state


def _metadata_value(instance: "CloudInstanceLike", key: str) -> str | None:
    metadata = getattr(instance, "raw", {}).get("metadata")
    if not isinstance(metadata, Mapping):
        return None
    items = metadata.get("items")
    if not isinstance(items, Sequence):
        return None
    for item in items:
        if not isinstance(item, Mapping):
            continue
        normalized_item = cast("Mapping[str, Any]", item)
        if normalized_item.get("key") == key:
            value = normalized_item.get("value")
            return value if isinstance(value, str) else None
    return None


def _append_live_instance_fleet_records(
    *,
    adapter,
    launch_plan: CloudLaunchPlan,
    role: str,
    base_fleets: list["CloudFleetPlacementRecord"],
    template_fleets: Sequence["CloudFleetPlacementRecord"],
) -> list["CloudFleetPlacementRecord"]:
    """Append synthetic runtime-added fleet records for live VMs outside config."""
    list_by_role = getattr(adapter, "list_instances_by_role", None)
    if not callable(list_by_role):
        return list(base_fleets)
    try:
        live_instances = list_by_role(plan=launch_plan, role=role)
    except Exception as exc:
        logger.debug(
            "Unable to discover live {} instances while reconstructing launch state: {}",
            role,
            exc,
        )
        return list(base_fleets)

    missing_instances = [
        instance
        for instance in live_instances
        if not _instance_is_covered_by_fleets(instance, base_fleets)
    ]
    if not missing_instances:
        return list(base_fleets)

    synthetic_fleets = _synthetic_fleets_for_live_instances(
        role=role,
        instances=missing_instances,
        templates=template_fleets,
    )
    return [*base_fleets, *synthetic_fleets]


def _instance_is_covered_by_fleets(
    instance: "CloudInstanceLike",
    fleets: Sequence["CloudFleetPlacementRecord"],
) -> bool:
    project = _instance_project(instance)
    for fleet in fleets:
        if project and fleet.project != project:
            continue
        if instance.name not in _expected_instance_names(fleet):
            continue
        if not _fleet_location_covers_instance(fleet, instance):
            continue
        return True
    return False


def _expected_instance_names(fleet: "CloudFleetPlacementRecord") -> set[str]:
    start = fleet.name_start_index
    return {
        f"{fleet.name_prefix}-{index:03d}"
        for index in range(start, start + fleet.count)
    }


def _fleet_location_covers_instance(
    fleet: "CloudFleetPlacementRecord",
    instance: "CloudInstanceLike",
) -> bool:
    if fleet.zones:
        return instance.zone in set(fleet.zones)
    if fleet.zone:
        return instance.zone == fleet.zone
    region = region_for_provider_zone(fleet.provider, instance.zone)
    if fleet.regions:
        return region in set(fleet.regions)
    if fleet.region:
        return region == fleet.region
    return True


def _synthetic_fleets_for_live_instances(
    *,
    role: str,
    instances: Sequence["CloudInstanceLike"],
    templates: Sequence["CloudFleetPlacementRecord"],
) -> list["CloudFleetPlacementRecord"]:
    from crsbench.cloud.records import CloudFleetPlacementRecord
    from crsbench.cloud.types import CloudProvider

    grouped: dict[_SyntheticFleetGroupKey, list[tuple[int, "CloudInstanceLike"]]] = {}
    fallback_prefixes: dict[_SyntheticFleetGroupKey, str] = {}
    group_templates: dict[
        _SyntheticFleetGroupKey, CloudFleetPlacementRecord | None
    ] = {}
    for instance in instances:
        template = _matching_template_for_instance(instance, templates=templates)
        parsed = _parse_indexed_instance_name(instance.name)
        if parsed is None:
            prefix = instance.name
            index = 1
        else:
            prefix, index = parsed
        project = _instance_project(instance) or (
            str(template.project) if template is not None else ""
        )
        region = region_for_provider_zone(CloudProvider.GCE, instance.zone) or ""
        ssh_via_iap = _instance_ssh_via_iap(instance, template=template)
        labels, owner_label = _fleet_labels_from_live_instances(
            [(index, instance)],
            template=template,
        )
        key = (
            project,
            prefix,
            region,
            ssh_via_iap,
            tuple(sorted(labels.items())),
            owner_label,
        )
        fallback_prefixes[key] = prefix
        group_templates[key] = template
        grouped.setdefault(key, []).append((index, instance))

    synthetic: list[CloudFleetPlacementRecord] = []
    for (
        project,
        prefix,
        region,
        ssh_via_iap,
        label_items,
        owner_label,
    ), indexed_instances in sorted(grouped.items()):
        key = (project, prefix, region, ssh_via_iap, label_items, owner_label)
        template = group_templates.get(key)
        indexes = [index for index, _instance in indexed_instances]
        zones = sorted({instance.zone for _index, instance in indexed_instances})
        start_index = min(indexes)
        count = max(indexes) - start_index + 1
        provider_metadata = _synthetic_provider_metadata(
            template=template,
            indexed_instances=indexed_instances,
            project=project,
            zones=zones,
        )
        provider_metadata.update(
            {
                "project": project,
                "zone": zones[0] if zones else None,
                "zones": zones,
                "region": region or None,
                "regions": [region] if region else [],
                "worker_count": count,
                "worker_name_prefix": fallback_prefixes[key],
                "worker_name_start_index": start_index,
                "ssh_via_iap": ssh_via_iap,
            }
        )
        labels = dict(label_items)
        synthetic.append(
            CloudFleetPlacementRecord(
                provider=CloudProvider.GCE,
                role=role,
                project=project,
                zone=zones[0] if zones else None,
                zones=zones,
                region=region or None,
                count=count,
                name_prefix=prefix,
                name_start_index=start_index,
                ssh_via_iap=ssh_via_iap,
                labels=labels,
                owner_label=owner_label,
                placement_source="reconstructed_live",
                provider_metadata=provider_metadata,
            )
        )
    return synthetic


def _parse_indexed_instance_name(name: str) -> tuple[str, int] | None:
    match = _GCE_INDEXED_NAME_RE.fullmatch(name)
    if match is None:
        return None
    return match.group("prefix"), int(match.group("index"))


def _instance_project(instance: "CloudInstanceLike") -> str:
    raw = getattr(instance, "raw", {})
    if isinstance(raw, Mapping):
        project = raw.get("project")
        if isinstance(project, str):
            return project
    return ""


def _matching_template_for_instance(
    instance: "CloudInstanceLike",
    *,
    templates: Sequence["CloudFleetPlacementRecord"],
) -> "CloudFleetPlacementRecord | None":
    project = _instance_project(instance)
    if project:
        for template in templates:
            if str(template.project) == project:
                return template
    if templates:
        return templates[0]
    return None


def _synthetic_provider_metadata(
    *,
    template: "CloudFleetPlacementRecord | None",
    indexed_instances: Sequence[tuple[int, "CloudInstanceLike"]],
    project: str,
    zones: Sequence[str],
) -> dict[str, Any]:
    if template is not None:
        return dict(template.provider_metadata)

    instance = indexed_instances[0][1] if indexed_instances else None
    service_account_email = (
        getattr(instance, "service_account_email", None)
        if instance is not None
        else None
    )
    if not isinstance(service_account_email, str) or not service_account_email:
        service_account_email = f"reconstructed-live@{project}.iam.gserviceaccount.com"
    return {
        "project": project,
        "zone": zones[0] if zones else None,
        "zones": list(zones),
        "machine_type": _instance_machine_type(instance),
        "boot_disk_size_gb": _instance_boot_disk_size_gb(instance),
        "image": "reconstructed-live-placeholder",
        "service_account_email": service_account_email,
    }


def _instance_machine_type(instance: "CloudInstanceLike | None") -> str:
    raw = getattr(instance, "raw", {}) if instance is not None else {}
    if isinstance(raw, Mapping):
        machine_type = raw.get("machineType")
        if isinstance(machine_type, str) and machine_type:
            return machine_type.rstrip("/").split("/")[-1]
    return "n2d-standard-2"


def _instance_boot_disk_size_gb(instance: "CloudInstanceLike | None") -> int:
    raw = getattr(instance, "raw", {}) if instance is not None else {}
    if isinstance(raw, Mapping):
        disks = raw.get("disks")
        if isinstance(disks, Sequence):
            for disk in disks:
                if not isinstance(disk, Mapping):
                    continue
                size = disk.get("diskSizeGb")
                if isinstance(size, int):
                    return max(size, 10)
                if isinstance(size, str) and size.isdigit():
                    return max(int(size), 10)
    return 10


def _instance_ssh_via_iap(
    instance: "CloudInstanceLike",
    *,
    template: "CloudFleetPlacementRecord | None",
    fallback: bool = False,
) -> bool:
    metadata_value = _metadata_value(instance, CRSBENCH_SSH_VIA_IAP_METADATA_KEY)
    if metadata_value is not None:
        return metadata_value.strip().lower() in {"1", "true", "yes", "on"}
    raw = getattr(instance, "raw", {})
    if isinstance(raw, Mapping):
        ssh_via_iap = raw.get("ssh_via_iap")
        if isinstance(ssh_via_iap, bool):
            return ssh_via_iap
    return bool(template.ssh_via_iap) if template is not None else fallback


def _fleet_labels_from_live_instances(
    indexed_instances: Sequence[tuple[int, "CloudInstanceLike"]],
    *,
    template: "CloudFleetPlacementRecord | None",
) -> tuple[dict[str, str], str | None]:
    if not indexed_instances:
        return (dict(template.labels), template.owner_label) if template else ({}, None)

    labels = dict(indexed_instances[0][1].labels)
    for reserved in ("crsbench-experiment", "crsbench-role"):
        labels.pop(reserved, None)
    owner_label = labels.pop("owner", None)
    if owner_label is None and template is not None:
        owner_label = template.owner_label
    return labels, owner_label


def _warn_for_cloud_storage_roots(context: ResolvedCloudContext) -> None:
    """Warn when resolved cloud storage roots use Linux /tmp-backed paths."""
    warn_for_persisted_storage_roots(
        experiment_filestore=context.experiment_filestore,
        report_filestore=None,
        copy_results_after_trial=False,
        results_filestore=None,
        remote_experiment_root=context.remote_experiment_root,
    )


def _resolve_remote_experiment_root_from_config(config) -> Path:
    """Return the remote VM experiment root configured for cloud collection."""
    remote = getattr(config.cloud, "remote", None) if config.cloud is not None else None
    experiment_root = getattr(remote, "experiment_root", None)
    if experiment_root is not None:
        return Path(experiment_root)
    return Path(config.experiment_filestore)


def reconnect(
    config_path: str,
    experiment_name: str,
    *,
    wait_for_remote_redis: bool = False,
):  # noqa: ARG001
    """Bootstrap operational context from a config YAML for standalone cloud commands.

    Args:
        config_path: Path to the experiment YAML config.
        experiment_name: Experiment name (reserved for future use).

    Returns:
        Tuple of (fleet, redis_conn, readiness_store, lifecycle_store, experiment_filestore).

    Raises:
        SystemExit: If the config has no ``cloud`` section.
    """
    context = resolve_cloud_context(config_path, experiment_name)

    if context.redis_password:
        os.environ["CRSBENCH_REDIS_PASSWORD"] = context.redis_password
    else:
        os.environ.pop("CRSBENCH_REDIS_PASSWORD", None)

    tunnel: OrchestratorRedisTunnel | None = None
    redis_host = context.redis_host
    if context.launch_state is not None:
        tunnel = OrchestratorRedisTunnel.from_launch_state(
            Path(config_path),
            context.launch_state,
        )
        tunnel.start()
        redis_host = tunnel.redis_host
        if wait_for_remote_redis:
            wait_for_redis_connection(
                redis_host,
                redis_password=context.redis_password,
                timeout_sec=_resolve_remote_redis_ready_timeout_sec(context),
            )

    try:
        redis_conn = create_redis_connection(redis_host)
        readiness = CloudReadinessStore(cast("ReadinessRedisProtocol", redis_conn))
        lifecycle = JobLifecycleStore(cast("LifecycleRedisProtocol", redis_conn))
    except Exception:
        if tunnel is not None:
            tunnel.stop()
        raise

    if tunnel is not None:
        _register_tunnel_cleanup(redis_conn, tunnel)

    return context, redis_conn, readiness, lifecycle, context.experiment_filestore
