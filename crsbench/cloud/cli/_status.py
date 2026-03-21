"""Status sub-action for crsbench cloud CLI."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from crsbench.cloud.cli._config_reconnect import (
    reconnect,
    resolve_effective_experiment_name,
)
from crsbench.cloud.readiness import CloudInstanceRole
from crsbench.distributed import queue as queue_module
from crsbench.distributed.job_lifecycle import JobState
from crsbench.distributed.queue_monitor import list_queue_job_entries
from crsbench.utils.logger import (
    get_logger,
    log_key_value,
    log_section,
    log_table,
)

if TYPE_CHECKING:
    import argparse


logger = get_logger(__name__)


def _job_state_value(job) -> str:
    state = getattr(job, "state", "")
    return state.value if hasattr(state, "value") else str(state)


def _load_status_jobs(redis_conn, lifecycle, experiment_name: str):
    """Load status jobs from lifecycle records, or fall back to live queue state."""
    lifecycle_jobs = lifecycle.list_jobs(experiment_name)
    if lifecycle_jobs:
        return lifecycle_jobs

    if not queue_module.REDIS_AVAILABLE or queue_module.rq is None:
        return []

    trial_queue_name, _build_queue_name, _verify_queue_name = (
        queue_module.resolve_queue_names(experiment_name)
    )
    queue = queue_module.rq.Queue(
        trial_queue_name,
        connection=redis_conn,
    )
    return list_queue_job_entries(queue, experiment_name)


def run_status(args: argparse.Namespace) -> int:
    """Show experiment fleet, job, collection, and recovery event summary."""
    experiment_name = resolve_effective_experiment_name(args.config, args.experiment)
    try:
        _context, redis_conn, readiness, lifecycle, _filestore = reconnect(
            args.config,
            experiment_name,
            wait_for_remote_redis=True,
        )
    except Exception as exc:
        logger.error("Cloud status failed: {}", exc)
        return 1

    # Query data
    workers = readiness.list_workers(experiment_name)
    evaluators = readiness.list_workers(
        experiment_name,
        role=CloudInstanceRole.EVALUATOR,
    )
    instances = sorted(
        [*workers, *evaluators],
        key=lambda worker: (worker.role.value, worker.instance_name),
    )
    jobs = _load_status_jobs(redis_conn, lifecycle, experiment_name)
    raw_events = redis_conn.lrange(
        f"crsbench:recovery-events:{experiment_name}", -5, -1
    )
    recent_events = [json.loads(e) for e in raw_events]

    # Compute collection summary
    total_jobs = len(jobs)
    completed = sum(1 for j in jobs if _job_state_value(j) == JobState.COMPLETED.value)
    syncing = sum(1 for j in jobs if _job_state_value(j) == JobState.SYNCING.value)
    running = sum(
        1
        for j in jobs
        if _job_state_value(j) in {JobState.CLAIMED.value, JobState.RUNNING.value}
    )
    failed = sum(1 for j in jobs if _job_state_value(j) == JobState.FAILED.value)
    orphaned = sum(1 for j in jobs if _job_state_value(j) == JobState.ORPHANED.value)
    pending = total_jobs - completed - syncing - running - failed - orphaned
    completion_pct = (
        f"{(completed / total_jobs * 100):.0f}%" if total_jobs > 0 else "0%"
    )

    if args.json_output:
        data = {
            "fleet": [
                {
                    "instance_name": w.instance_name,
                    "role": w.role.value,
                    "state": w.state.value,
                    "zone": w.zone,
                    "internal_ip": w.internal_ip,
                }
                for w in instances
            ],
            "jobs": [
                {
                    "job_id": j.job_id,
                    "trial_key": j.trial_key,
                    "state": _job_state_value(j),
                    "claimed_by": j.claimed_by,
                    "retry_count": j.retry_count,
                }
                for j in jobs
            ],
            "collection": {
                "total": total_jobs,
                "completed": completed,
                "syncing": syncing,
                "running": running,
                "pending": pending,
                "failed": failed,
                "orphaned": orphaned,
                "completion": completion_pct,
            },
            "events": recent_events,
        }
        print(json.dumps(data, indent=2))  # noqa: T201
        return 0

    # Human-readable output
    log_section("Fleet Summary")
    fleet_rows = [
        [w.instance_name, w.role.value, w.state.value, w.zone, w.internal_ip or "-"]
        for w in instances
    ]
    log_table(["Instance", "Role", "State", "Zone", "IP"], fleet_rows)

    log_section("Job Summary")
    job_rows = [
        [
            j.job_id,
            j.trial_key,
            _job_state_value(j),
            j.claimed_by or "-",
            str(j.retry_count),
        ]
        for j in jobs
    ]
    log_table(["Job ID", "Trial", "State", "Claimed By", "Retries"], job_rows)

    log_section("Collection Summary")
    log_key_value(
        {
            "Total": total_jobs,
            "Completed": completed,
            "Syncing": syncing,
            "Running": running,
            "Pending": pending,
            "Failed": failed,
            "Orphaned": orphaned,
            "Completion": completion_pct,
        }
    )

    log_section("Recent Recovery Events")
    event_rows = [
        [
            e.get("ts", "-"),
            e.get("type", "-"),
            e.get("job_id", "-"),
            e.get("detail", "-"),
        ]
        for e in recent_events
    ]
    log_table(["Time", "Type", "Job", "Detail"], event_rows)

    return 0
