"""Collect sub-action: invoke ArtifactCollector for each live GCE instance."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
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
from crsbench.cloud.collection import (
    ArtifactCollectionError,
    ArtifactCollector,
    collect_marker_path,
    merge_experiment_start_time,
    read_collect_marker,
    write_collect_marker,
)
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


def run_collect(args: argparse.Namespace) -> int:
    """Collect artifacts from live GCE workers/evaluators for the given experiment.

    Returns 0 if all collections succeed, 1 if any failed.
    """
    experiment_name = resolve_effective_experiment_name(args.config, args.experiment)
    context = resolve_cloud_context(args.config, experiment_name)
    launch_state = context.launch_state
    experiment_filestore = context.experiment_filestore
    destination = experiment_filestore / experiment_name

    if not _confirm_destination_overwrite(destination, force=args.force):
        return 1

    readiness = None
    try:
        _context, _redis_conn, readiness, _lifecycle, experiment_filestore = reconnect(
            args.config, experiment_name
        )
    except Exception as exc:
        logger.warning(
            "Redis reconnect unavailable for experiment {}; "
            "continuing collection with GCE state only: {}",
            experiment_name,
            exc,
        )

    provisioner = provisioner_for_context(context)
    collector = ArtifactCollector(base_path=args.config)

    # Validate GCE state
    live_instances = _list_live_instances(context, experiment_name, provisioner)
    live_names = {w.name for w in live_instances}

    # Cross-reference with Redis readiness state
    if readiness is not None:
        redis_workers = _list_readiness_instances(readiness, experiment_name)
        redis_names = {w.instance_name for w in redis_workers}
        stale_names = redis_names - live_names
        if stale_names:
            logger.warning(
                "Stale Redis entries (no matching GCE instance): {}",
                ", ".join(sorted(stale_names)),
            )

    if not live_instances and launch_state is None:
        logger.warning(
            "No live GCE instances found for experiment '{}'", experiment_name
        )
        return 0

    remote_experiment_dir = resolve_remote_experiment_dir(
        context.remote_experiment_root,
        experiment_name,
        args.remote_dir,
    )
    failed = 0
    start_time_observations: list[tuple[str | None, str]] = []

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
        except (ArtifactCollectionError, Exception) as exc:
            logger.error("Log collection failed for {}: {}", worker.name, exc)
            failed += 1
        if _collects_experiment_artifacts(worker):
            try:
                collector.collect(
                    worker=worker,
                    fleet=_resolve_instance_fleet(context, worker),
                    experiment_name=experiment_name,
                    experiment_filestore=experiment_filestore,
                    remote_experiment_dir=remote_experiment_dir,
                    start_time_observations=start_time_observations,
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
                worker=cast("CloudInstanceLike", orchestrator_worker),
                fleet=launch_state.as_transport_config(),
                experiment_name=experiment_name,
                experiment_filestore=experiment_filestore,
                remote_experiment_dir=remote_experiment_dir,
            )
            logger.info("Log collection succeeded: {}", orchestrator_worker.name)
        except (ArtifactCollectionError, Exception) as exc:
            logger.error(
                "Log collection failed for {}: {}", orchestrator_worker.name, exc
            )
            failed += 1

    if failed:
        return 1

    if not destination.exists():
        return 0

    current_start_time = _resolve_current_run_start_time(start_time_observations)
    marker = _build_collect_marker(
        destination=destination,
        experiment_name=experiment_name,
        prior_marker=read_collect_marker(destination),
        current_start_time=current_start_time,
    )
    try:
        write_collect_marker(destination, marker)
    except OSError as exc:
        logger.error("Failed to write collect marker {}: {}", destination, exc)
        return 1

    return 0


def _confirm_destination_overwrite(destination, *, force: bool) -> bool:
    """Gate collection when the local destination already exists."""
    if force or not destination.exists():
        return True

    marker = read_collect_marker(destination)
    marker_path = collect_marker_path(destination)
    if marker is None and marker_path.exists():
        logger.warning(
            "Local collect marker is malformed; ignoring prior collect metadata: {}",
            marker_path,
        )

    logger.warning("Local destination already exists: {}", destination)
    if marker is not None:
        last_collect_time = marker.get("last_collect_time")
        experiment_start_time = marker.get("experiment_start_time")
        if isinstance(last_collect_time, str):
            logger.warning("Last collected: {}", last_collect_time)
        if isinstance(experiment_start_time, str):
            logger.warning("Experiment started: {}", experiment_start_time)
    logger.warning("Rerun with --force to skip this prompt.")

    if not sys.stdin.isatty():
        logger.error(
            "Local destination already exists and stdin is not interactive. "
            "Rerun with --force to continue."
        )
        return False

    while True:
        answer = (
            input("Continue and merge into the existing destination? [Y/n] ")
            .strip()
            .lower()
        )
        if answer in {"", "y", "yes"}:
            return True
        if answer in {"n", "no"}:
            logger.info("Cancelled.")
            return False
        logger.warning("Please answer y, yes, n, or no.")


def _build_collect_marker(
    *,
    destination,
    experiment_name: str,
    prior_marker: dict[str, object] | None,
    current_start_time: tuple[str | None, str],
) -> dict[str, object]:
    experiment_start_time, experiment_start_time_source = merge_experiment_start_time(
        current=current_start_time,
        prior=prior_marker,
    )
    return {
        "schema_version": 1,
        "experiment_name": experiment_name,
        "local_destination": str(destination),
        "last_collect_time": _current_time_iso8601(),
        "experiment_start_time": experiment_start_time,
        "experiment_start_time_source": experiment_start_time_source,
    }


def _resolve_current_run_start_time(
    observations: list[tuple[str | None, str]],
) -> tuple[str | None, str]:
    timestamp_start_values = sorted(
        (
            value
            for value, source in observations
            if value is not None and source == "earliest_trial_timestamp_start"
        ),
        key=_sort_timestamp,
    )
    if timestamp_start_values:
        return timestamp_start_values[0], "earliest_trial_timestamp_start"

    timestamp_values = sorted(
        (
            value
            for value, source in observations
            if value is not None and source == "earliest_trial_timestamp"
        ),
        key=_sort_timestamp,
    )
    if timestamp_values:
        return timestamp_values[0], "earliest_trial_timestamp"
    return None, "unknown"


def _sort_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _current_time_iso8601() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _list_live_instances(
    context: "ResolvedCloudContext",
    experiment_name: str,
    provisioner,
) -> list["CloudInstanceLike"]:
    if context.launch_plan is not None and context.launch_state is None:
        adapter = provider_adapter_for_context(context, provisioner=provisioner)
        workers = adapter.list_workers(plan=context.launch_plan)
        if context.evaluator_fleet_configs:
            workers.extend(adapter.list_evaluators(plan=context.launch_plan))
        return workers
    return shared_list_live_instances(context, experiment_name, provisioner)


def _resolve_instance_fleet(
    context: "ResolvedCloudContext",
    worker: "CloudInstanceLike",
):
    return shared_resolve_instance_fleet(context, worker)


def _list_readiness_instances(readiness, experiment_name: str):
    workers = readiness.list_workers(experiment_name)
    workers.extend(
        readiness.list_workers(
            experiment_name,
            role=CloudInstanceRole.EVALUATOR,
        )
    )
    return workers


def _collects_experiment_artifacts(worker: "CloudInstanceLike") -> bool:
    """Return whether this instance owns a worker-style experiment artifact tree."""
    return worker.labels.get("crsbench-role") != CloudInstanceRole.EVALUATOR.value
