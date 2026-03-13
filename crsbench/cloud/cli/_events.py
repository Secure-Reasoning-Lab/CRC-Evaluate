"""Events sub-action for crsbench cloud CLI."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from crsbench.cloud.cli._config_reconnect import reconnect
from crsbench.utils.logger import log_table

if TYPE_CHECKING:
    import argparse


def run_events(args: argparse.Namespace) -> int:
    """Show chronological recovery event timeline."""
    _fleet, redis_conn, _readiness, _lifecycle, _filestore = reconnect(
        args.config, args.experiment
    )

    raw_events = redis_conn.lrange(
        f"crsbench:recovery-events:{args.experiment}", 0, -1
    )
    events = [json.loads(e) for e in raw_events]

    # Filter by type if requested
    if args.event_type:
        events = [e for e in events if e.get("type") == args.event_type]

    if args.json_output:
        print(json.dumps(events, indent=2))  # noqa: T201
        return 0

    # Human-readable output
    event_rows = [
        [
            e.get("ts", "-"),
            e.get("type", "-"),
            e.get("job_id", "-"),
            e.get("worker", "-"),
            e.get("detail", "-"),
        ]
        for e in events
    ]
    log_table(["Timestamp", "Type", "Job ID", "Worker", "Detail"], event_rows)

    return 0
