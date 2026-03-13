"""Cloud experiment lifecycle management command."""

from __future__ import annotations

import argparse

from crsbench.utils.logger import get_logger

logger = get_logger(__name__)


def add_cloud_subparser(subparsers) -> None:
    """Add 'cloud' subcommand to the CLI."""
    parser = subparsers.add_parser(
        "cloud",
        help="Manage cloud experiment lifecycle",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s status my-experiment --config config.yaml
  %(prog)s events my-experiment --config config.yaml --type orphan_detected
  %(prog)s teardown my-experiment --config config.yaml --force
        """,
    )
    cloud_subparsers = parser.add_subparsers(dest="cloud_command", required=True)

    # status
    status_p = cloud_subparsers.add_parser("status", help="Show experiment fleet and job status")
    status_p.add_argument("experiment", help="Experiment name")
    status_p.add_argument("--config", required=True, help="Path to experiment YAML config")
    status_p.add_argument("--json", action="store_true", dest="json_output", help="JSON output")

    # teardown
    teardown_p = cloud_subparsers.add_parser(
        "teardown", help="Collect artifacts and delete workers"
    )
    teardown_p.add_argument("experiment", help="Experiment name")
    teardown_p.add_argument("--config", required=True, help="Path to experiment YAML config")
    teardown_p.add_argument("--force", action="store_true", help="Skip confirmation prompt")

    # collect
    collect_p = cloud_subparsers.add_parser("collect", help="Collect artifacts from workers")
    collect_p.add_argument("experiment", help="Experiment name")
    collect_p.add_argument("--config", required=True, help="Path to experiment YAML config")

    # events
    events_p = cloud_subparsers.add_parser("events", help="Show recovery event timeline")
    events_p.add_argument("experiment", help="Experiment name")
    events_p.add_argument("--config", required=True, help="Path to experiment YAML config")
    events_p.add_argument("--type", dest="event_type", help="Filter by event type")
    events_p.add_argument("--json", action="store_true", dest="json_output", help="JSON output")

    parser.set_defaults(command="cloud")


def run_cloud(args: argparse.Namespace) -> int:
    """Dispatch cloud sub-actions."""
    cmd = args.cloud_command

    if cmd == "status":
        from crsbench.cloud.cli._status import run_status

        return run_status(args)

    if cmd == "events":
        from crsbench.cloud.cli._events import run_events

        return run_events(args)

    if cmd == "collect":
        logger.error("'cloud collect' is not implemented yet (see Plan 04-02).")
        return 1

    if cmd == "teardown":
        logger.error("'cloud teardown' is not implemented yet (see Plan 04-02).")
        return 1

    logger.error("Unknown cloud command: %s", cmd)
    return 2
