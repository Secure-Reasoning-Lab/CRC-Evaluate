"""Status sub-action for crsbench cloud CLI."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from crsbench.cloud.cli._config_reconnect import reconnect
from crsbench.distributed.job_lifecycle import JobState
from crsbench.utils.logger import log_key_value, log_section, log_table

if TYPE_CHECKING:
    import argparse


def run_status(args: argparse.Namespace) -> int:
    """Show experiment fleet, job, collection, and recovery event summary."""
    fleet, redis_conn, readiness, lifecycle, _filestore = reconnect(
        args.config, args.experiment
    )

    # Query data
    workers = readiness.list_workers(args.experiment)
    jobs = lifecycle.list_jobs(args.experiment)
    raw_events = redis_conn.lrange(
        f"crsbench:recovery-events:{args.experiment}", -5, -1
    )
    recent_events = [json.loads(e) for e in raw_events]

    # Compute collection summary
    total_jobs = len(jobs)
    completed = sum(1 for j in jobs if j.state == JobState.COMPLETED)
    syncing = sum(1 for j in jobs if j.state == JobState.SYNCING)
    failed = sum(1 for j in jobs if j.state == JobState.FAILED)
    pending = total_jobs - completed - syncing - failed
    completion_pct = f"{(completed / total_jobs * 100):.0f}%" if total_jobs > 0 else "0%"

    if args.json_output:
        data = {
            "fleet": [
                {
                    "instance_name": w.instance_name,
                    "state": w.state.value,
                    "zone": w.zone,
                    "internal_ip": w.internal_ip,
                }
                for w in workers
            ],
            "jobs": [
                {
                    "job_id": j.job_id,
                    "trial_key": j.trial_key,
                    "state": j.state.value,
                    "claimed_by": j.claimed_by,
                    "retry_count": j.retry_count,
                }
                for j in jobs
            ],
            "collection": {
                "total": total_jobs,
                "completed": completed,
                "syncing": syncing,
                "pending": pending,
                "failed": failed,
                "completion": completion_pct,
            },
            "events": recent_events,
        }
        print(json.dumps(data, indent=2))  # noqa: T201
        return 0

    # Human-readable output
    log_section("Fleet Summary")
    fleet_rows = [
        [w.instance_name, w.state.value, w.zone, w.internal_ip or "-"]
        for w in workers
    ]
    log_table(["Instance", "State", "Zone", "IP"], fleet_rows)

    log_section("Job Summary")
    job_rows = [
        [j.job_id, j.trial_key, j.state.value, j.claimed_by or "-", str(j.retry_count)]
        for j in jobs
    ]
    log_table(["Job ID", "Trial", "State", "Claimed By", "Retries"], job_rows)

    log_section("Collection Summary")
    log_key_value({
        "Total": total_jobs,
        "Completed": completed,
        "Syncing": syncing,
        "Pending": pending,
        "Failed": failed,
        "Completion": completion_pct,
    })

    log_section("Recent Recovery Events")
    event_rows = [
        [e.get("ts", "-"), e.get("type", "-"), e.get("job_id", "-"), e.get("detail", "-")]
        for e in recent_events
    ]
    log_table(["Time", "Type", "Job", "Detail"], event_rows)

    return 0
