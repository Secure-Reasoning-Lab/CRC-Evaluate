"""Status sub-action for crsbench cloud CLI."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from crsbench.cloud.cli._config_reconnect import (
    reconnect,
    resolve_effective_experiment_name,
)
from crsbench.cloud.cli._instance_inventory import resolve_instance_fleet_record
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


def _event_name(event: dict[str, object]) -> str:
    """Return the recovery-event name across old and new payload shapes."""
    raw = event.get("event")
    if isinstance(raw, str) and raw.strip():
        return raw
    raw = event.get("type")
    if isinstance(raw, str) and raw.strip():
        return raw
    return "-"


def _load_status_jobs(redis_conn, lifecycle, experiment_name: str):
    """Load status jobs from lifecycle records plus any uncovered live queue state."""
    lifecycle_jobs = lifecycle.list_jobs(experiment_name)
    lifecycle_job_ids = {job.job_id for job in lifecycle_jobs}

    if not queue_module.REDIS_AVAILABLE or queue_module.rq is None:
        return lifecycle_jobs

    trial_queue_name, _build_queue_name, _verify_queue_name = (
        queue_module.resolve_queue_names(experiment_name)
    )
    queue = queue_module.rq.Queue(
        trial_queue_name,
        connection=redis_conn,
    )
    queue_entries = list_queue_job_entries(queue, experiment_name)
    if not lifecycle_jobs:
        return queue_entries
    return [
        *lifecycle_jobs,
        *(job for job in queue_entries if job.job_id not in lifecycle_job_ids),
    ]


def _placement_source_for_status_instance(context, instance) -> str:
    """Resolve fleet provenance for one readiness-backed worker/evaluator row."""
    if not hasattr(context, "worker_fleet_configs"):
        return "unknown"
    try:
        fleet = resolve_instance_fleet_record(
            context,
            instance_name=instance.instance_name,
            zone=instance.zone,
            role=instance.role.value,
        )
    except Exception:
        return "unknown"
    return getattr(fleet, "placement_source", "config")


def run_status(args: argparse.Namespace) -> int:
    """Show experiment fleet, job, collection, and recovery event summary."""
    experiment_name = resolve_effective_experiment_name(args.config, args.experiment)
    try:
        context, redis_conn, readiness, lifecycle, _filestore = reconnect(
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
                    "placement_source": _placement_source_for_status_instance(
                        context,
                        w,
                    ),
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
        [
            w.instance_name,
            w.role.value,
            _placement_source_for_status_instance(context, w),
            w.state.value,
            w.zone,
            w.internal_ip or "-",
        ]
        for w in instances
    ]
    log_table(["Instance", "Role", "Source", "State", "Zone", "IP"], fleet_rows)

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
            _event_name(e),
            e.get("job_id", "-"),
            e.get("detail", "-"),
        ]
        for e in recent_events
    ]
    log_table(["Time", "Type", "Job", "Detail"], event_rows)

    return 0
