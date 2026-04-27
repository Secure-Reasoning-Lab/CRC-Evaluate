"""Collect sub-action: invoke ArtifactCollector for each live GCE instance."""

from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
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
    StagedArtifactCollection,
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
_MAX_PARALLEL_INSTANCE_COLLECTIONS = 8


@dataclass(frozen=True)
class _CollectLiveInstanceResult:
    failed: int
    artifact_failed: bool
    artifact_publish_succeeded: bool
    staged_collection: StagedArtifactCollection | None = None


def _launch_state_collects_experiment_artifacts(launch_state) -> bool:
    return launch_state is not None and launch_state.launch_mode == "reeval"


def run_collect(args: argparse.Namespace) -> int:
    """Collect artifacts from live GCE workers/evaluators for the given experiment.

    Returns 0 if all collections succeed, 1 if any failed.
    """
    requested_experiment_name = resolve_effective_experiment_name(
        args.config,
        args.experiment,
    )
    context = resolve_cloud_context(args.config, requested_experiment_name)
    experiment_name = context.experiment_name
    launch_state = context.launch_state
    dest_override = getattr(args, "dest", None)
    if dest_override:
        experiment_filestore = Path(dest_override)
        base_destination = experiment_filestore
    else:
        experiment_filestore = context.experiment_filestore
        base_destination = experiment_filestore / experiment_name

    readiness = None
    try:
        _context, _redis_conn, readiness, _lifecycle, _reconnect_filestore = reconnect(
            args.config, experiment_name
        )
        if not dest_override:
            experiment_filestore = _reconnect_filestore
            base_destination = experiment_filestore / experiment_name
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

    orchestrator_collects_artifacts = _launch_state_collects_experiment_artifacts(
        launch_state
    )
    if not live_instances and launch_state is None:
        logger.warning(
            "No live GCE instances found for experiment '{}'", experiment_name
        )
        return 0

    if (
        not live_instances
        and orchestrator_collects_artifacts
        and launch_state is not None
        and not _orchestrator_exists(launch_state, provisioner)
    ):
        logger.warning(
            "No live GCE instances found for cloud re-eval experiment '{}' and "
            "orchestrator {} is already gone; skipping collection.",
            experiment_name,
            launch_state.orchestrator_name,
        )
        return 0

    destination = base_destination
    if any(_collects_experiment_artifacts(worker) for worker in live_instances) or (
        orchestrator_collects_artifacts
    ):
        if args.timestamp:
            destination = _fresh_timestamp_destination(
                experiment_filestore,
                experiment_name,
            )
        else:
            resolved_destination = _confirm_destination_overwrite(
                base_destination,
                force=args.force,
                timestamp_destination_factory=lambda: _fresh_timestamp_destination(
                    experiment_filestore,
                    experiment_name,
                ),
            )
            if resolved_destination is None:
                return 1
            destination = resolved_destination

    remote_experiment_dir = resolve_remote_experiment_dir(
        context.remote_experiment_root,
        experiment_name,
        args.remote_dir,
    )
    if orchestrator_collects_artifacts and launch_state is not None:
        orchestrator_worker = launch_state.as_orchestrator_record()
        try:
            collector.collect_reeval_submission_artifacts(
                worker=cast("CloudInstanceLike", orchestrator_worker),
                fleet=launch_state.as_transport_config(),
                experiment_name=experiment_name,
                experiment_filestore=experiment_filestore,
                remote_submission_dir=launch_state.effective_remote_submission_dir(),
                destination=destination,
            )
        except (ArtifactCollectionError, Exception) as exc:
            logger.error(
                "Cloud re-eval submission artifact collection failed for {}: {}",
                orchestrator_worker.name,
                exc,
            )
            return 1

    artifact_publish_succeeded = False
    artifact_failures = 0
    start_time_observations: list[tuple[str | None, str]] = []
    failed, artifact_failures, artifact_publish_succeeded, staged_collections = (
        _collect_live_instances_parallel(
            collector=collector,
            context=context,
            live_instances=live_instances,
            experiment_name=experiment_name,
            experiment_filestore=experiment_filestore,
            remote_experiment_dir=remote_experiment_dir,
            destination=destination,
            start_time_observations=start_time_observations,
        )
    )

    if orchestrator_collects_artifacts and launch_state is not None:
        orchestrator_worker = launch_state.as_orchestrator_record()
        if _collector_supports_staged_publish(collector):
            try:
                staged_collections.append(
                    collector.stage_collection(
                        worker=cast("CloudInstanceLike", orchestrator_worker),
                        fleet=launch_state.as_transport_config(),
                        experiment_name=experiment_name,
                        experiment_filestore=experiment_filestore,
                        remote_experiment_dir=remote_experiment_dir,
                        destination=destination,
                    )
                )
                logger.info("Collection staged: {}", orchestrator_worker.name)
            except (ArtifactCollectionError, Exception) as exc:
                logger.error(
                    "Artifact collection failed for {}: {}",
                    orchestrator_worker.name,
                    exc,
                )
                failed += 1
                artifact_failures += 1
        else:
            try:
                collector.collect(
                    worker=cast("CloudInstanceLike", orchestrator_worker),
                    fleet=launch_state.as_transport_config(),
                    experiment_name=experiment_name,
                    experiment_filestore=experiment_filestore,
                    remote_experiment_dir=remote_experiment_dir,
                    start_time_observations=start_time_observations,
                    destination=destination,
                )
                artifact_publish_succeeded = True
            except (ArtifactCollectionError, Exception) as exc:
                logger.error(
                    "Artifact collection failed for {}: {}",
                    orchestrator_worker.name,
                    exc,
                )
                failed += 1
                artifact_failures += 1

    if artifact_failures == 0 and staged_collections:
        publish_failures, published = _publish_staged_collections(
            collector=collector,
            staged_collections=staged_collections,
            start_time_observations=start_time_observations,
        )
        failed += publish_failures
        artifact_failures += publish_failures
        artifact_publish_succeeded = artifact_publish_succeeded or published

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

    if not artifact_publish_succeeded or not destination.exists():
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
        logger.error(
            "Failed to write collect marker {}: {}",
            collect_marker_path(destination),
            exc,
        )
        return 1

    return 0


def _confirm_destination_overwrite(
    destination: Path,
    *,
    force: bool,
    timestamp_destination_factory,
) -> Path | None:
    """Gate collection when the local destination already exists."""
    if force or not destination.exists():
        return destination

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
    logger.warning(
        "Rerun with --force to skip this prompt or --timestamp for a fresh sibling."
    )

    if not sys.stdin.isatty():
        logger.error(
            "Local destination already exists and stdin is not interactive. "
            "Rerun with --force to merge into it or --timestamp for a fresh sibling."
        )
        return None

    while True:
        try:
            answer = (
                input("Continue and merge into the existing destination? [Y/n/t] ")
                .strip()
                .lower()
            )
        except (EOFError, KeyboardInterrupt):
            logger.info("Cancelled.")
            return None
        if answer in {"", "y", "yes"}:
            return destination
        if answer in {"n", "no"}:
            logger.info("Cancelled.")
            return None
        if answer in {"t", "timestamp"}:
            return timestamp_destination_factory()
        logger.warning("Please answer y, yes, n, no, t, or timestamp.")


def _format_collect_timestamp(value: str | datetime | None = None) -> str:
    """Return a UTC timestamp string suitable for local collect directories."""
    if value is None:
        current = datetime.now(timezone.utc)
    elif isinstance(value, str):
        current = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        current = value
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    else:
        current = current.astimezone(timezone.utc)
    return current.strftime("%Y-%m-%d-%H-%M")


def _fresh_timestamp_destination(
    experiment_filestore: Path, experiment_name: str
) -> Path:
    """Reserve and return a fresh timestamped sibling directory."""
    base_name = f"{experiment_name}-{_format_collect_timestamp()}"
    suffix = 0
    while True:
        candidate_name = base_name if suffix == 0 else f"{base_name}-{suffix + 1:02d}"
        candidate = experiment_filestore / candidate_name
        try:
            candidate.mkdir(parents=True, exist_ok=False)
            return candidate
        except FileExistsError:
            suffix += 1


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


def _orchestrator_exists(launch_state, provisioner) -> bool:
    try:
        provisioner.get_instance_record(
            project=launch_state.orchestrator_project,
            zone=launch_state.orchestrator_zone,
            instance_name=launch_state.orchestrator_name,
        )
    except Exception:
        return False
    return True


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


def _collector_supports_staged_publish(collector: ArtifactCollector) -> bool:
    """Return whether *collector* exposes the staged-publish collection API."""
    return callable(getattr(type(collector), "stage_collection", None)) and callable(
        getattr(type(collector), "publish_staged_collection", None)
    )


def _collect_live_instances_parallel(
    *,
    collector: ArtifactCollector,
    context: "ResolvedCloudContext",
    live_instances: list["CloudInstanceLike"],
    experiment_name: str,
    experiment_filestore: Path,
    remote_experiment_dir: str,
    destination: Path,
    start_time_observations: list[tuple[str | None, str]],
    continue_on_error: bool = False,
) -> tuple[int, int, bool, list[StagedArtifactCollection]]:
    """Collect logs and artifacts from live instances with bounded parallelism."""
    if not live_instances:
        return 0, 0, False, []

    max_workers = min(_MAX_PARALLEL_INSTANCE_COLLECTIONS, len(live_instances))
    suffix = " -- continuing with teardown" if continue_on_error else ""
    futures = {}
    failed = 0
    artifact_failures = 0
    artifact_publish_succeeded = False
    staged_collections: list[StagedArtifactCollection] = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for worker in live_instances:
            futures[
                executor.submit(
                    _collect_single_live_instance,
                    collector=collector,
                    context=context,
                    worker=worker,
                    experiment_name=experiment_name,
                    experiment_filestore=experiment_filestore,
                    remote_experiment_dir=remote_experiment_dir,
                    destination=destination,
                    start_time_observations=start_time_observations,
                    failure_suffix=suffix,
                )
            ] = worker.name
        for future in as_completed(futures):
            result = future.result()
            failed += result.failed
            artifact_failures += int(result.artifact_failed)
            artifact_publish_succeeded = (
                artifact_publish_succeeded or result.artifact_publish_succeeded
            )
            if result.staged_collection is not None:
                staged_collections.append(result.staged_collection)
    return failed, artifact_failures, artifact_publish_succeeded, staged_collections


def _collect_single_live_instance(
    *,
    collector: ArtifactCollector,
    context: "ResolvedCloudContext",
    worker: "CloudInstanceLike",
    experiment_name: str,
    experiment_filestore: Path,
    remote_experiment_dir: str,
    destination: Path,
    start_time_observations: list[tuple[str | None, str]],
    failure_suffix: str,
) -> _CollectLiveInstanceResult:
    """Collect logs and optional artifacts for one live instance."""
    failed = 0
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
        logger.error(
            "Log collection failed for {}: {}{}", worker.name, exc, failure_suffix
        )
        failed += 1

    if not _collects_experiment_artifacts(worker):
        logger.info(
            "Skipping artifact collection for evaluator {}; logs only",
            worker.name,
        )
        return _CollectLiveInstanceResult(
            failed=failed,
            artifact_failed=False,
            artifact_publish_succeeded=False,
        )

    if _collector_supports_staged_publish(collector):
        try:
            staged_collection = collector.stage_collection(
                worker=worker,
                fleet=_resolve_instance_fleet(context, worker),
                experiment_name=experiment_name,
                experiment_filestore=experiment_filestore,
                remote_experiment_dir=remote_experiment_dir,
                destination=destination,
            )
            logger.info("Collection staged: {}", worker.name)
            return _CollectLiveInstanceResult(
                failed=failed,
                artifact_failed=False,
                artifact_publish_succeeded=False,
                staged_collection=staged_collection,
            )
        except (ArtifactCollectionError, Exception) as exc:
            logger.error(
                "Collection failed for {}: {}{}", worker.name, exc, failure_suffix
            )
            return _CollectLiveInstanceResult(
                failed=failed + 1,
                artifact_failed=True,
                artifact_publish_succeeded=False,
            )

    try:
        collector.collect(
            worker=worker,
            fleet=_resolve_instance_fleet(context, worker),
            experiment_name=experiment_name,
            experiment_filestore=experiment_filestore,
            remote_experiment_dir=remote_experiment_dir,
            start_time_observations=start_time_observations,
            destination=destination,
        )
        logger.info("Collection succeeded: {}", worker.name)
    except (ArtifactCollectionError, Exception) as exc:
        logger.error("Collection failed for {}: {}{}", worker.name, exc, failure_suffix)
        failed += 1
        return _CollectLiveInstanceResult(
            failed=failed,
            artifact_failed=True,
            artifact_publish_succeeded=False,
        )
    return _CollectLiveInstanceResult(
        failed=failed,
        artifact_failed=False,
        artifact_publish_succeeded=True,
    )


def _publish_staged_collections(
    *,
    collector: ArtifactCollector,
    staged_collections: list[StagedArtifactCollection],
    start_time_observations: list[tuple[str | None, str]],
) -> tuple[int, bool]:
    """Publish verified staged worker trees after all artifact staging succeeds."""
    published = False
    for staged_collection in sorted(
        staged_collections, key=lambda item: item.worker_name
    ):
        try:
            collector.publish_staged_collection(staged_collection)
            start_time_observations.append(staged_collection.start_time_observation)
            logger.info("Collection succeeded: {}", staged_collection.worker_name)
            published = True
        except (ArtifactCollectionError, Exception) as exc:
            logger.error(
                "Collection failed for {}: {}",
                staged_collection.worker_name,
                exc,
            )
            return 1, published
    return 0, published
