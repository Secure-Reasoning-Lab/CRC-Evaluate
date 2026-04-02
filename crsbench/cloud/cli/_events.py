"""Events sub-action for crsbench cloud CLI."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from crsbench.cloud.cli._config_reconnect import (
    reconnect,
    resolve_effective_experiment_name,
)
from crsbench.utils.logger import get_logger, log_table

if TYPE_CHECKING:
    import argparse


logger = get_logger(__name__)


def _event_name(event: dict[str, object]) -> str:
    """Return the recovery-event name across old and new payload shapes."""
    raw = event.get("event")
    if isinstance(raw, str) and raw.strip():
        return raw
    raw = event.get("type")
    if isinstance(raw, str) and raw.strip():
        return raw
    return "-"


def run_events(args: argparse.Namespace) -> int:
    """Show chronological recovery event timeline."""
    experiment_name = resolve_effective_experiment_name(args.config, args.experiment)
    try:
        _context, redis_conn, _readiness, _lifecycle, _filestore = reconnect(
            args.config,
            experiment_name,
            wait_for_remote_redis=True,
        )
    except Exception as exc:
        logger.error("Cloud events failed: {}", exc)
        return 1

    raw_events = redis_conn.lrange(
        f"crsbench:recovery-events:{experiment_name}",
        0,
        -1,
    )
    events = [json.loads(e) for e in raw_events]

    # Filter by type if requested
    if args.event_type:
        events = [e for e in events if _event_name(e) == args.event_type]

    if args.json_output:
        print(json.dumps(events, indent=2))  # noqa: T201
        return 0

    # Human-readable output
    event_rows = [
        [
            e.get("ts", "-"),
            _event_name(e),
            e.get("job_id", "-"),
            e.get("worker", "-"),
            e.get("detail", "-"),
        ]
        for e in events
    ]
    log_table(["Timestamp", "Type", "Job ID", "Worker", "Detail"], event_rows)

    return 0
