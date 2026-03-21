"""Teardown sub-action: collect artifacts then delete GCE instances with safety gates."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, cast

from crsbench.cloud.cli._config_reconnect import (
    reconnect,
    resolve_cloud_context,
    resolve_effective_experiment_name,
    resolve_remote_experiment_dir,
)
from crsbench.cloud.cli._instance_inventory import (
    list_live_instances as shared_list_live_instances,
)
from crsbench.cloud.cli._instance_inventory import (
    resolve_instance_fleet as shared_resolve_instance_fleet,
)
from crsbench.cloud.collection import ArtifactCollector
from crsbench.cloud.launch_state import delete_launch_state
from crsbench.cloud.providers import (
    provider_adapter_for_context,
    provisioner_for_context,
)
from crsbench.cloud.readiness import CloudInstanceRole
from crsbench.utils.logger import get_logger

if TYPE_CHECKING:
    import argparse

    from crsbench.cloud.cli._config_reconnect import ResolvedCloudContext
    from crsbench.cloud.records import CloudInstanceLike

logger = get_logger(__name__)


def run_teardown(args: argparse.Namespace) -> int:
    """Collect remaining artifacts then delete all cloud instances for the experiment.

    Safety flow:
    1. Validate GCE instances exist
    2. Cross-reference Redis for stale entries
    3. Prompt for confirmation (unless --force)
    4. Collect artifacts from each live worker -- abort on ANY failure
    5. Delete workers only after all collections succeed

    Returns 0 on success, 1 on failure/abort.
    """
    experiment_name = resolve_effective_experiment_name(args.config, args.experiment)
    context = resolve_cloud_context(args.config, experiment_name)
    launch_state = context.launch_state
    experiment_filestore = context.experiment_filestore
    readiness = None
    lifecycle = None
    try:
        _context, _redis_conn, readiness, lifecycle, experiment_filestore = reconnect(
            args.config, experiment_name
        )
    except Exception as exc:
        logger.warning(
            "Redis reconnect unavailable for experiment {}; "
            "continuing teardown with GCE state only: {}",
            experiment_name,
            exc,
        )

    provisioner = provisioner_for_context(context)
    collector = ArtifactCollector(base_path=args.config)

    # Validate GCE state
    live_instances = _list_live_instances(context, experiment_name, provisioner)
    live_names = {w.name for w in live_instances}

    # Cross-reference with Redis readiness state
    redis_workers = (
        _list_readiness_instances(readiness, experiment_name) if readiness else []
    )
    redis_names = {w.instance_name for w in redis_workers}
    stale_names = redis_names - live_names

    if stale_names:
        logger.warning(
            "Stale Redis entries (no matching GCE instance): {}",
            ", ".join(sorted(stale_names)),
        )

    if not live_instances and not redis_workers and launch_state is None:
        logger.info("Nothing to tear down for experiment '{}'", experiment_name)
        return 0

    if not live_instances and redis_workers and launch_state is None:
        logger.warning(
            "No live GCE instances but Redis has {} instance entries (stale state)",
            len(redis_workers),
        )
        return 0

    # Query uncollected jobs
    jobs = lifecycle.list_jobs(experiment_name) if lifecycle else []
    uncollected_count = sum(1 for j in jobs if j.state not in ("completed", "failed"))

    # Confirmation prompt
    if not args.force:
        if not sys.stdin.isatty():
            logger.error("Use --force for non-interactive teardown")
            return 1

        worker_count = len(live_instances) + (1 if launch_state is not None else 0)
        logger.info(
            "This will collect artifacts from {} instances ({} uncollected jobs) "
            "and delete all cloud VMs.",
            worker_count,
            str(uncollected_count) if lifecycle is not None else "unknown",
        )
        confirm = input("Type 'yes' to continue: ").strip().lower()
        if confirm != "yes":
            logger.info("Cancelled.")
            return 0

    # Collect phase -- best effort, but teardown still proceeds to avoid leaked VMs.
    remote_experiment_dir = resolve_remote_experiment_dir(
        context.remote_experiment_root,
        experiment_name,
        args.remote_dir,
    )
    collection_failed = False
    for worker in live_instances:
        try:
            collector.collect_logs(
                worker=worker,
                fleet=_resolve_instance_fleet(context, worker),
                experiment_name=experiment_name,
                experiment_filestore=experiment_filestore,
                remote_experiment_dir=remote_experiment_dir,
            )
            logger.info("Log collection succeeded: {}", worker.name)
        except Exception as exc:
            logger.error(
                "Log collection failed for {}: {} -- continuing with teardown",
                worker.name,
                exc,
            )
            collection_failed = True
        if _collects_experiment_artifacts(worker):
            try:
                collector.collect(
                    worker=worker,
                    fleet=_resolve_instance_fleet(context, worker),
                    experiment_name=experiment_name,
                    experiment_filestore=experiment_filestore,
                    remote_experiment_dir=remote_experiment_dir,
                )
                logger.info("Collection succeeded: {}", worker.name)
            except Exception as exc:
                logger.error(
                    "Collection failed for {}: {} -- continuing with teardown",
                    worker.name,
                    exc,
                )
                collection_failed = True
        else:
            logger.info(
                "Skipping artifact collection for evaluator {}; logs only",
                worker.name,
            )

    if launch_state is not None:
        orchestrator_worker = launch_state.as_orchestrator_record()
        try:
            collector.collect_logs(
                worker=cast("CloudInstanceLike", orchestrator_worker),
                fleet=launch_state.as_transport_config(),
                experiment_name=experiment_name,
                experiment_filestore=experiment_filestore,
                remote_experiment_dir=remote_experiment_dir,
            )
            logger.info("Log collection succeeded: {}", orchestrator_worker.name)
        except Exception as exc:
            logger.error(
                "Log collection failed for {}: {} -- continuing with teardown",
                orchestrator_worker.name,
                exc,
            )
            collection_failed = True

    deletion_failed = False
    try:
        _delete_live_instances(context, experiment_name, provisioner)
    except Exception as exc:
        logger.error(
            "Worker deletion failed for experiment {}: {}", experiment_name, exc
        )
        deletion_failed = True
    if launch_state is not None:
        try:
            provisioner.delete_instance(
                project=launch_state.orchestrator_project,
                zone=launch_state.orchestrator_zone,
                instance_name=launch_state.orchestrator_name,
            )
        except Exception as exc:
            logger.error(
                "Orchestrator deletion failed for {}: {}",
                launch_state.orchestrator_name,
                exc,
            )
            deletion_failed = True

    if launch_state is not None and (
        not deletion_failed
        or _should_remove_launch_state(
            context=context,
            experiment_name=experiment_name,
            launch_state=launch_state,
            provisioner=provisioner,
        )
    ):
        try:
            delete_launch_state(args.config, experiment_name)
        except OSError as exc:
            logger.warning(
                "Failed to remove config-adjacent launch state for {}: {}",
                experiment_name,
                exc,
            )

    if collection_failed or deletion_failed:
        return 1

    logger.info(
        "Teardown complete: {} instances deleted{}",
        len(live_instances),
        " and orchestrator deleted" if launch_state is not None else "",
    )
    return 0


def _list_live_instances(
    context: "ResolvedCloudContext",
    experiment_name: str,
    provisioner,
) -> list["CloudInstanceLike"]:
    return shared_list_live_instances(context, experiment_name, provisioner)


def _should_remove_launch_state(
    *,
    context: "ResolvedCloudContext",
    experiment_name: str,
    launch_state,
    provisioner,
) -> bool:
    """Return whether teardown can safely discard persisted launch state."""
    if _list_live_instances(context, experiment_name, provisioner):
        return False
    try:
        provisioner.get_instance_record(
            project=launch_state.orchestrator_project,
            zone=launch_state.orchestrator_zone,
            instance_name=launch_state.orchestrator_name,
        )
    except Exception as exc:
        return _instance_missing(exc)
    return False


def _instance_missing(exc: Exception) -> bool:
    """Return whether a provider error clearly indicates the instance no longer exists."""
    return "not found" in str(exc).lower()


def _delete_live_instances(
    context: "ResolvedCloudContext",
    experiment_name: str,
    provisioner,
) -> None:
    adapter = provider_adapter_for_context(context, provisioner=provisioner)
    if context.launch_state is not None:
        for fleet in context.worker_fleet_configs:
            provisioner.delete_workers(
                experiment_name=experiment_name,
                fleet=adapter.worker_fleet_from_cloud_placement_record(fleet),
            )
        for fleet in context.evaluator_fleet_configs:
            provisioner.delete_evaluators(
                experiment_name=experiment_name,
                fleet=adapter.worker_fleet_from_cloud_placement_record(fleet),
            )
        return

    if context.launch_plan is not None:
        adapter.delete_workers(plan=context.launch_plan)
        if context.evaluator_fleet_configs:
            adapter.delete_evaluators(plan=context.launch_plan)
        return

    for fleet in context.worker_fleet_configs:
        provisioner.delete_workers(
            experiment_name=experiment_name,
            fleet=adapter.worker_fleet_from_cloud_placement_record(fleet),
        )
    for fleet in context.evaluator_fleet_configs:
        provisioner.delete_workers(
            experiment_name=experiment_name,
            fleet=adapter.worker_fleet_from_cloud_placement_record(fleet),
        )


def _resolve_instance_fleet(
    context: "ResolvedCloudContext",
    worker: "CloudInstanceLike",
):
    return shared_resolve_instance_fleet(context, worker)


def _collects_experiment_artifacts(worker: "CloudInstanceLike") -> bool:
    """Return whether this instance owns a worker-style experiment artifact tree."""
    return worker.labels.get("crsbench-role") != CloudInstanceRole.EVALUATOR.value


def _list_readiness_instances(readiness, experiment_name: str):
    workers = readiness.list_workers(experiment_name)
    workers.extend(
        readiness.list_workers(
            experiment_name,
            role=CloudInstanceRole.EVALUATOR,
        )
    )
    return workers
