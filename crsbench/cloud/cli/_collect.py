"""Collect sub-action: invoke ArtifactCollector for each live GCE worker."""

from __future__ import annotations

from typing import TYPE_CHECKING

from crsbench.cloud.cli._config_reconnect import reconnect
from crsbench.cloud.launch_state import load_launch_state

if TYPE_CHECKING:
    import argparse
from crsbench.cloud.collection import ArtifactCollectionError, ArtifactCollector
from crsbench.cloud.gce.provisioner import GceProvisioner
from crsbench.utils.logger import get_logger

logger = get_logger(__name__)


def run_collect(args: argparse.Namespace) -> int:
    """Collect artifacts from live GCE workers for the given experiment.

    Returns 0 if all collections succeed, 1 if any failed.
    """
    fleet, _redis_conn, readiness, _lifecycle, experiment_filestore = reconnect(
        args.config, args.experiment
    )

    provisioner = GceProvisioner()
    collector = ArtifactCollector()

    # Validate GCE state
    live_workers = provisioner.list_workers(
        experiment_name=args.experiment, fleet=fleet
    )
    launch_state = load_launch_state(experiment_filestore, args.experiment)
    live_names = {w.name for w in live_workers}

    # Cross-reference with Redis readiness state
    redis_workers = readiness.list_workers(args.experiment)
    redis_names = {w.instance_name for w in redis_workers}
    stale_names = redis_names - live_names
    if stale_names:
        logger.warning(
            "Stale Redis entries (no matching GCE instance): %s",
            ", ".join(sorted(stale_names)),
        )

    if not live_workers:
        logger.warning(
            "No live GCE instances found for experiment '%s'", args.experiment
        )
        return 0

    remote_experiment_dir = args.remote_dir
    failed = 0

    for worker in live_workers:
        try:
            collector.collect(
                worker=worker,
                fleet=fleet,
                experiment_name=args.experiment,
                experiment_filestore=experiment_filestore,
                remote_experiment_dir=remote_experiment_dir,
            )
            logger.info("Collection succeeded: %s", worker.name)
        except (ArtifactCollectionError, Exception) as exc:
            logger.error("Collection failed for %s: %s", worker.name, exc)
            failed += 1

    if launch_state is not None:
        orchestrator_worker = launch_state.as_orchestrator_record()
        try:
            collector.collect(
                worker=orchestrator_worker,
                fleet=launch_state.as_transport_config(),
                experiment_name=args.experiment,
                experiment_filestore=experiment_filestore,
                remote_experiment_dir=remote_experiment_dir,
            )
            logger.info("Collection succeeded: %s", orchestrator_worker.name)
        except (ArtifactCollectionError, Exception) as exc:
            logger.error("Collection failed for %s: %s", orchestrator_worker.name, exc)
            failed += 1

    return 1 if failed else 0
