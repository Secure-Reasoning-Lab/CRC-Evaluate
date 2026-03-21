"""Cloud experiment lifecycle management command."""

from __future__ import annotations

import argparse

from crsbench.utils.logger import get_logger

logger = get_logger(__name__)


def _add_config_argument(
    parser: argparse.ArgumentParser,
    *,
    required: bool = False,
    suppress_default: bool = False,
) -> None:
    if suppress_default:
        parser.add_argument(
            "--config",
            "--experiment-config",
            dest="config",
            required=required,
            default=argparse.SUPPRESS,
            help="Path to experiment YAML config",
        )
        return
    parser.add_argument(
        "--config",
        "--experiment-config",
        dest="config",
        required=required,
        help="Path to experiment YAML config",
    )


def add_cloud_subparser(subparsers) -> None:
    """Add 'cloud' subcommand to the CLI."""
    parser = subparsers.add_parser(
        "cloud",
        help="Manage cloud experiment lifecycle",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --config config.yaml launch
  %(prog)s --config config.yaml list
  %(prog)s --config config.yaml log
  %(prog)s --config config.yaml exec work-001 -- docker ps
  %(prog)s status my-experiment --config config.yaml
  %(prog)s monitor my-experiment --config config.yaml
  %(prog)s events my-experiment --config config.yaml --type orphan_detected
  %(prog)s teardown my-experiment --config config.yaml --force
        """,
    )
    _add_config_argument(parser)
    cloud_subparsers = parser.add_subparsers(dest="cloud_command", required=True)

    # status
    status_p = cloud_subparsers.add_parser(
        "status", help="Show experiment fleet and job status"
    )
    status_p.add_argument("experiment", help="Experiment name")
    _add_config_argument(status_p, suppress_default=True)
    status_p.add_argument(
        "--json", action="store_true", dest="json_output", help="JSON output"
    )

    # monitor
    monitor_p = cloud_subparsers.add_parser(
        "monitor",
        help="Attach to a launched remote orchestrator and show live queue progress",
    )
    monitor_p.add_argument("experiment", nargs="?", help="Experiment name")
    _add_config_argument(monitor_p, suppress_default=True)

    # teardown
    teardown_p = cloud_subparsers.add_parser(
        "teardown", help="Collect artifacts and delete workers"
    )
    teardown_p.add_argument("experiment", nargs="?", help="Experiment name")
    _add_config_argument(teardown_p, suppress_default=True)
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
    _add_config_argument(collect_p, suppress_default=True)
    collect_p.add_argument(
        "--remote-dir",
        dest="remote_dir",
        help="Absolute path on workers containing the experiment tree",
    )
    collect_p.add_argument(
        "--force",
        action="store_true",
        help="Skip overwrite confirmation when the local destination already exists",
    )
    collect_p.add_argument(
        "--timestamp",
        action="store_true",
        help="Collect into a fresh timestamped sibling directory",
    )

    # list
    list_p = cloud_subparsers.add_parser(
        "list", help="List live cloud instances for the experiment config"
    )
    _add_config_argument(list_p, suppress_default=True)
    list_p.add_argument(
        "--json", action="store_true", dest="json_output", help="JSON output"
    )

    # ssh
    ssh_p = cloud_subparsers.add_parser(
        "ssh",
        aliases=["shell"],
        help="Open an SSH session to a live cloud instance",
    )
    ssh_p.add_argument("instance", nargs="?", help="Instance name or alias")
    _add_config_argument(ssh_p, suppress_default=True)

    # exec
    exec_p = cloud_subparsers.add_parser(
        "exec", help="Run a remote command on a live cloud instance"
    )
    _add_config_argument(exec_p, suppress_default=True)
    exec_p.add_argument(
        "exec_args",
        nargs=argparse.REMAINDER,
        help="Optional instance selector plus remote command after '--'",
    )

    # log
    log_p = cloud_subparsers.add_parser(
        "log", help="Follow the primary CRSBench journal on a live cloud instance"
    )
    log_p.add_argument("instance", nargs="?", help="Instance name or alias")
    _add_config_argument(log_p, suppress_default=True)

    # launch
    launch_p = cloud_subparsers.add_parser(
        "launch",
        help="Launch an orchestrator VM plus worker fleet from this machine",
    )
    _add_config_argument(launch_p, suppress_default=True)

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
    _add_config_argument(events_p, suppress_default=True)
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

    if not getattr(args, "config", None):
        logger.error(
            "Cloud command '{}' requires --config/--experiment-config",
            cmd,
        )
        return 2

    if cmd == "status":
        from crsbench.cloud.cli._status import run_status

        return run_status(args)

    if cmd == "monitor":
        from crsbench.cloud.cli._monitor import run_monitor

        return run_monitor(args)

    if cmd == "list":
        from crsbench.cloud.cli._list import run_list

        return run_list(args)

    if cmd in {"ssh", "shell"}:
        from crsbench.cloud.cli._ssh import run_ssh

        return run_ssh(args)

    if cmd == "exec":
        from crsbench.cloud.cli._exec import run_exec

        return run_exec(args)

    if cmd == "log":
        from crsbench.cloud.cli._log import run_log

        return run_log(args)

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
