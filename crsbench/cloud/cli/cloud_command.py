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
  %(prog)s monitor my-experiment --config config.yaml
  %(prog)s events my-experiment --config config.yaml --type orphan_detected
  %(prog)s teardown my-experiment --config config.yaml --force
        """,
    )
    cloud_subparsers = parser.add_subparsers(dest="cloud_command", required=True)

    # status
    status_p = cloud_subparsers.add_parser(
        "status", help="Show experiment fleet and job status"
    )
    status_p.add_argument("experiment", help="Experiment name")
    status_p.add_argument(
        "--config",
        "--experiment-config",
        dest="config",
        required=True,
        help="Path to experiment YAML config",
    )
    status_p.add_argument(
        "--json", action="store_true", dest="json_output", help="JSON output"
    )

    # monitor
    monitor_p = cloud_subparsers.add_parser(
        "monitor",
        help="Attach to a launched remote orchestrator and show live queue progress",
    )
    monitor_p.add_argument("experiment", nargs="?", help="Experiment name")
    monitor_p.add_argument(
        "--config",
        "--experiment-config",
        dest="config",
        required=True,
        help="Path to experiment YAML config",
    )

    # teardown
    teardown_p = cloud_subparsers.add_parser(
        "teardown", help="Collect artifacts and delete workers"
    )
    teardown_p.add_argument("experiment", nargs="?", help="Experiment name")
    teardown_p.add_argument(
        "--config",
        "--experiment-config",
        dest="config",
        required=True,
        help="Path to experiment YAML config",
    )
    teardown_p.add_argument(
        "--force", action="store_true", help="Skip confirmation prompt"
    )
    teardown_p.add_argument(
        "--remote-dir",
        dest="remote_dir",
        help="Absolute path on workers containing the experiment tree",
    )

    # collect
    collect_p = cloud_subparsers.add_parser(
        "collect", help="Collect artifacts from workers"
    )
    collect_p.add_argument("experiment", nargs="?", help="Experiment name")
    collect_p.add_argument(
        "--config",
        "--experiment-config",
        dest="config",
        required=True,
        help="Path to experiment YAML config",
    )

    # list
    list_p = cloud_subparsers.add_parser(
        "list", help="List live cloud instances for the experiment config"
    )
    list_p.add_argument(
        "--config",
        "--experiment-config",
        dest="config",
        required=True,
        help="Path to experiment YAML config",
    )
    list_p.add_argument(
        "--json", action="store_true", dest="json_output", help="JSON output"
    )

    # ssh
    ssh_p = cloud_subparsers.add_parser(
        "ssh", help="Open an SSH session to a live cloud instance"
    )
    ssh_p.add_argument("instance", nargs="?", help="Instance name or alias")
    ssh_p.add_argument(
        "--config",
        "--experiment-config",
        dest="config",
        required=True,
        help="Path to experiment YAML config",
    )
    collect_p.add_argument(
        "--remote-dir",
        dest="remote_dir",
        help="Absolute path on workers containing the experiment tree",
    )

    # launch
    launch_p = cloud_subparsers.add_parser(
        "launch",
        help="Launch an orchestrator VM plus worker fleet from this machine",
    )
    launch_p.add_argument(
        "--config",
        "--experiment-config",
        dest="config",
        required=True,
        help="Path to experiment YAML config",
    )

    # keygen
    keygen_p = cloud_subparsers.add_parser(
        "keygen", help="Generate an SSH ed25519 deploy key pair"
    )
    keygen_p.add_argument(
        "--output-dir",
        default=".crsbench-keys/",
        dest="output_dir",
        help="Directory to write the key pair into (default: .crsbench-keys/)",
    )
    keygen_p.add_argument(
        "--name",
        default="crsbench-deploy",
        help="Base name for key files (default: crsbench-deploy)",
    )
    keygen_p.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing key files",
    )

    # events
    events_p = cloud_subparsers.add_parser(
        "events", help="Show recovery event timeline"
    )
    events_p.add_argument("experiment", help="Experiment name")
    events_p.add_argument(
        "--config",
        "--experiment-config",
        dest="config",
        required=True,
        help="Path to experiment YAML config",
    )
    events_p.add_argument("--type", dest="event_type", help="Filter by event type")
    events_p.add_argument(
        "--json", action="store_true", dest="json_output", help="JSON output"
    )

    parser.set_defaults(command="cloud")


def run_cloud(args: argparse.Namespace) -> int:
    """Dispatch cloud sub-actions."""
    cmd = args.cloud_command

    if cmd == "keygen":
        from crsbench.cloud.cli._keygen import run_keygen

        return run_keygen(args)

    if cmd == "status":
        from crsbench.cloud.cli._status import run_status

        return run_status(args)

    if cmd == "monitor":
        from crsbench.cloud.cli._monitor import run_monitor

        return run_monitor(args)

    if cmd == "list":
        from crsbench.cloud.cli._list import run_list

        return run_list(args)

    if cmd == "ssh":
        from crsbench.cloud.cli._ssh import run_ssh

        return run_ssh(args)

    if cmd == "events":
        from crsbench.cloud.cli._events import run_events

        return run_events(args)

    if cmd == "collect":
        from crsbench.cloud.cli._collect import run_collect

        return run_collect(args)

    if cmd == "launch":
        from crsbench.cloud.cli._launch import run_launch

        return run_launch(args)

    if cmd == "teardown":
        from crsbench.cloud.cli._teardown import run_teardown

        return run_teardown(args)

    logger.error("Unknown cloud command: {}", cmd)
    return 2
