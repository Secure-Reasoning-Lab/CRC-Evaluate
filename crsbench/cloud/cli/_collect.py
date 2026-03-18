"""Collect sub-action: invoke ArtifactCollector for each live GCE instance."""

from __future__ import annotations

from typing import TYPE_CHECKING

from crsbench.cloud.cli._config_reconnect import reconnect, resolve_cloud_context
from crsbench.cloud.gce.provider import GceProviderAdapter
from crsbench.cloud.readiness import CloudInstanceRole

if TYPE_CHECKING:
    import argparse

    from crsbench.cloud.cli._config_reconnect import ResolvedCloudContext
    from crsbench.cloud.gce.models import GceWorkerRecord
from crsbench.cloud.collection import ArtifactCollectionError, ArtifactCollector
from crsbench.cloud.gce.provisioner import GceProvisioner
from crsbench.utils.logger import get_logger

logger = get_logger(__name__)


def run_collect(args: argparse.Namespace) -> int:
    """Collect artifacts from live GCE workers/evaluators for the given experiment.

    Returns 0 if all collections succeed, 1 if any failed.
    """
    context = resolve_cloud_context(args.config, args.experiment)
    launch_state = context.launch_state
    experiment_filestore = context.experiment_filestore
    readiness = None
    try:
        _context, _redis_conn, readiness, _lifecycle, experiment_filestore = reconnect(
            args.config, args.experiment
        )
    except Exception as exc:
        logger.warning(
            "Redis reconnect unavailable for experiment {}; "
            "continuing collection with GCE state only: {}",
            args.experiment,
            exc,
        )

    provisioner = GceProvisioner()
    collector = ArtifactCollector(base_path=args.config)

    # Validate GCE state
    live_instances = _list_live_instances(context, args.experiment, provisioner)
    live_names = {w.name for w in live_instances}

    # Cross-reference with Redis readiness state
    if readiness is not None:
        redis_workers = _list_readiness_instances(readiness, args.experiment)
        redis_names = {w.instance_name for w in redis_workers}
        stale_names = redis_names - live_names
        if stale_names:
            logger.warning(
                "Stale Redis entries (no matching GCE instance): {}",
                ", ".join(sorted(stale_names)),
            )

    if not live_instances and launch_state is None:
        logger.warning(
            "No live GCE instances found for experiment '{}'", args.experiment
        )
        return 0

    remote_experiment_dir = args.remote_dir
    failed = 0

    for worker in live_instances:
        try:
            collector.collect_logs(
                worker=worker,
                fleet=_resolve_instance_fleet(context, worker),
                experiment_name=args.experiment,
                experiment_filestore=experiment_filestore,
                remote_experiment_dir=remote_experiment_dir,
            )
            logger.info("Log collection succeeded: {}", worker.name)
        except (ArtifactCollectionError, Exception) as exc:
            logger.error("Log collection failed for {}: {}", worker.name, exc)
            failed += 1
        if _collects_experiment_artifacts(worker):
            try:
                collector.collect(
                    worker=worker,
                    fleet=_resolve_instance_fleet(context, worker),
                    experiment_name=args.experiment,
                    experiment_filestore=experiment_filestore,
                    remote_experiment_dir=remote_experiment_dir,
                )
                logger.info("Collection succeeded: {}", worker.name)
            except (ArtifactCollectionError, Exception) as exc:
                logger.error("Collection failed for {}: {}", worker.name, exc)
                failed += 1
        else:
            logger.info(
                "Skipping artifact collection for evaluator {}; logs only",
                worker.name,
            )

    if launch_state is not None:
        orchestrator_worker = launch_state.as_orchestrator_record()
        try:
            collector.collect_logs(
                worker=orchestrator_worker,
                fleet=launch_state.as_transport_config(),
                experiment_name=args.experiment,
                experiment_filestore=experiment_filestore,
                remote_experiment_dir=remote_experiment_dir,
            )
            logger.info("Log collection succeeded: {}", orchestrator_worker.name)
        except (ArtifactCollectionError, Exception) as exc:
            logger.error(
                "Log collection failed for {}: {}", orchestrator_worker.name, exc
            )
            failed += 1

    return 1 if failed else 0


def _list_live_instances(
    context: "ResolvedCloudContext",
    experiment_name: str,
    provisioner: GceProvisioner,
) -> list["GceWorkerRecord"]:
    if context.launch_plan is not None:
        adapter = GceProviderAdapter(provisioner=provisioner)
        workers = adapter.list_workers(plan=context.launch_plan)
        if context.evaluator_fleet_configs:
            workers.extend(adapter.list_evaluators(plan=context.launch_plan))
        return workers

    workers: list[GceWorkerRecord] = []
    for fleet in context.worker_fleet_configs:
        workers.extend(
            provisioner.list_workers(experiment_name=experiment_name, fleet=fleet)
        )
    for fleet in context.evaluator_fleet_configs:
        workers.extend(
            provisioner.list_workers(experiment_name=experiment_name, fleet=fleet)
        )
    return workers


def _resolve_instance_fleet(
    context: "ResolvedCloudContext",
    worker: "GceWorkerRecord",
):
    role = worker.labels.get("crsbench-role")
    candidate_fleets = (
        context.evaluator_fleet_configs
        if role == "evaluator"
        else context.worker_fleet_configs
    )
    prefix_matches = [
        fleet
        for fleet in candidate_fleets
        if fleet.zone == worker.zone
        and isinstance(fleet.worker_name_prefix, str)
        and fleet.worker_name_prefix
        and worker.name.startswith(fleet.worker_name_prefix)
    ]
    if prefix_matches:
        return prefix_matches[0]

    zone_matches = [fleet for fleet in candidate_fleets if fleet.zone == worker.zone]
    if zone_matches:
        return zone_matches[0]

    raise RuntimeError(
        f"No cloud fleet config matched instance {worker.name} in zone {worker.zone}"
    )


def _list_readiness_instances(readiness, experiment_name: str):
    workers = readiness.list_workers(experiment_name)
    workers.extend(
        readiness.list_workers(
            experiment_name,
            role=CloudInstanceRole.EVALUATOR,
        )
    )
    return workers


def _collects_experiment_artifacts(worker: "GceWorkerRecord") -> bool:
    """Return whether this instance owns a worker-style experiment artifact tree."""
    return worker.labels.get("crsbench-role") != CloudInstanceRole.EVALUATOR.value
