"""Teardown sub-action: collect artifacts then delete GCE workers with safety gates."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from crsbench.cloud.cli._config_reconnect import reconnect
from crsbench.cloud.collection import ArtifactCollector
from crsbench.cloud.gce.provisioner import GceProvisioner
from crsbench.cloud.launch_state import delete_launch_state, load_launch_state
from crsbench.utils.logger import get_logger

if TYPE_CHECKING:
    import argparse

logger = get_logger(__name__)


def run_teardown(args: argparse.Namespace) -> int:
    """Collect remaining artifacts then delete all workers for the experiment.

    Safety flow:
    1. Validate GCE instances exist
    2. Cross-reference Redis for stale entries
    3. Prompt for confirmation (unless --force)
    4. Collect artifacts from each live worker -- abort on ANY failure
    5. Delete workers only after all collections succeed

    Returns 0 on success, 1 on failure/abort.
    """
    fleet, _redis_conn, readiness, lifecycle, experiment_filestore = reconnect(
        args.config, args.experiment
    )

    provisioner = GceProvisioner()
    collector = ArtifactCollector()
    launch_state = load_launch_state(experiment_filestore, args.experiment)

    # Validate GCE state
    live_workers = provisioner.list_workers(
        experiment_name=args.experiment, fleet=fleet
    )
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

    if not live_workers and not redis_workers and launch_state is None:
        logger.info("Nothing to tear down for experiment '%s'", args.experiment)
        return 0

    if not live_workers and redis_workers and launch_state is None:
        logger.warning(
            "No live GCE instances but Redis has %d worker entries (stale state)",
            len(redis_workers),
        )
        return 0

    # Query uncollected jobs
    jobs = lifecycle.list_jobs(args.experiment)
    uncollected_count = sum(1 for j in jobs if j.state not in ("completed", "failed"))

    # Confirmation prompt
    if not args.force:
        if not sys.stdin.isatty():
            logger.error("Use --force for non-interactive teardown")
            return 1

        worker_count = len(live_workers) + (1 if launch_state is not None else 0)
        logger.info(
            "This will collect artifacts from %d instances (%d uncollected jobs) "
            "and delete all cloud VMs.",
            worker_count,
            uncollected_count,
        )
        confirm = input("Type 'yes' to continue: ").strip().lower()
        if confirm != "yes":
            logger.info("Cancelled.")
            return 0

    # Collect phase -- abort on ANY failure
    remote_experiment_dir = args.remote_dir
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
        except Exception as exc:
            logger.error(
                "Collection failed for %s: %s -- aborting teardown, workers left alive",
                worker.name,
                exc,
            )
            return 1

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
        except Exception as exc:
            logger.error(
                "Collection failed for %s: %s -- aborting teardown, workers left alive",
                orchestrator_worker.name,
                exc,
            )
            return 1

    # Delete phase -- only reached if all collections succeeded
    provisioner.delete_workers(experiment_name=args.experiment, fleet=fleet)
    if launch_state is not None:
        provisioner.delete_instance(
            project=launch_state.orchestrator_project,
            zone=launch_state.orchestrator_zone,
            instance_name=launch_state.orchestrator_name,
        )
        delete_launch_state(experiment_filestore, args.experiment)
    logger.info(
        "Teardown complete: %d workers deleted%s",
        len(live_workers),
        " and orchestrator deleted" if launch_state is not None else "",
    )
    return 0
