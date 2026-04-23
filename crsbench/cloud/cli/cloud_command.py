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
  %(prog)s --config config.yaml serial orch
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
    teardown_p.add_argument(
        "--timestamp",
        action="store_true",
        help="Collect artifacts into a fresh timestamped sibling directory",
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
    collect_p.add_argument(
        "--dest",
        dest="dest",
        default=None,
        help="Override local destination directory for collected artifacts",
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

    # serial
    serial_p = cloud_subparsers.add_parser(
        "serial",
        help="Open a serial-console session to a live cloud instance",
    )
    serial_p.add_argument("instance", nargs="?", help="Instance name or alias")
    _add_config_argument(serial_p, suppress_default=True)
    serial_p.add_argument(
        "--port",
        type=int,
        choices=range(1, 5),
        default=1,
        help="GCE serial port number to connect to (default: 1)",
    )

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
        "log",
        help="Follow the primary CRSBench journal on one or more live cloud instances",
    )
    log_p.add_argument("instance", nargs="?", help="Instance name or alias")
    _add_config_argument(log_p, suppress_default=True)
    log_p.add_argument(
        "--instance",
        dest="instances",
        action="append",
        default=[],
        help="Additional instance selector (repeatable for multi-target log fan-in)",
    )
    log_p.add_argument(
        "--role",
        help="Attach to every live instance for one role (orch, work, or eval)",
    )
    log_p.add_argument(
        "--all",
        dest="all_instances",
        action="store_true",
        help="Attach to every live instance for the experiment",
    )
    log_p.add_argument(
        "--merge-by",
        choices=("arrival", "timestamp"),
        default="arrival",
        help="Merge strategy for multi-stream output (default: arrival)",
    )

    # launch
    launch_p = cloud_subparsers.add_parser(
        "launch",
        help="Launch an orchestrator VM plus worker fleet from this machine",
    )
    _add_config_argument(launch_p, suppress_default=True)

    # add-workers
    add_workers_p = cloud_subparsers.add_parser(
        "add-workers",
        help="Add one new worker placement to an active cloud launch",
    )
    _add_config_argument(add_workers_p, suppress_default=True)
    add_workers_p.add_argument(
        "--instance-profile",
        dest="instance_profile",
        help="Existing instance profile name to use for the new worker placement (defaults to cloud.workers.defaults.instance_profile)",
    )
    add_workers_p.add_argument(
        "--count",
        type=int,
        help="Number of workers to add in this placement (default: 1)",
    )
    add_workers_p.add_argument(
        "--regions",
        help="Comma-separated candidate regions for the new worker placement (defaults to worker-role settings, then provider defaults)",
    )
    add_workers_p.add_argument(
        "--zones",
        help="Comma-separated zones for the new worker placement (defaults to worker-role settings, then provider defaults)",
    )
    add_workers_p.add_argument(
        "--force",
        action="store_true",
        help="Skip the confirmation prompt",
    )

    # add-evaluators
    add_evaluators_p = cloud_subparsers.add_parser(
        "add-evaluators",
        help="Add one new evaluator placement to an active cloud launch",
    )
    _add_config_argument(add_evaluators_p, suppress_default=True)
    add_evaluators_p.add_argument(
        "--instance-profile",
        dest="instance_profile",
        help="Existing instance profile name to use for the new evaluator placement (defaults to cloud.evaluators.defaults.instance_profile)",
    )
    add_evaluators_p.add_argument(
        "--count",
        type=int,
        help="Number of evaluators to add in this placement (default: 1)",
    )
    add_evaluators_p.add_argument(
        "--regions",
        help="Comma-separated candidate regions for the new evaluator placement (defaults to evaluator-role settings, then provider defaults)",
    )
    add_evaluators_p.add_argument(
        "--zones",
        help="Comma-separated zones for the new evaluator placement (defaults to evaluator-role settings, then provider defaults)",
    )
    add_evaluators_p.add_argument(
        "--force",
        action="store_true",
        help="Skip the confirmation prompt",
    )

    # preflight
    preflight_p = cloud_subparsers.add_parser(
        "preflight",
        help="Resolve the cloud launch plan and checks without provisioning",
    )
    _add_config_argument(preflight_p, suppress_default=True)
    preflight_p.add_argument(
        "--json", action="store_true", dest="json_output", help="JSON output"
    )
    preflight_p.add_argument(
        "--strict",
        action="store_true",
        help="Treat warnings as a non-zero result",
    )

    # derive-unfinished-trial-keys
    derive_p = cloud_subparsers.add_parser(
        "derive-unfinished-trial-keys",
        help="Derive unfinished trial keys from collected experiment artifacts",
    )
    _add_config_argument(derive_p, suppress_default=True)
    derive_p.add_argument(
        "--from",
        dest="from_path",
        default=None,
        help="Collected experiment root (defaults to experiment_filestore/experiment)",
    )
    derive_p.add_argument(
        "--output",
        dest="output",
        default=None,
        help="Output path for newline-delimited unfinished trial keys",
    )
    derive_p.add_argument(
        "--rerun-failed-trials",
        action="store_true",
        default=False,
        help="Include failed trials in the unfinished selection",
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
    _add_config_argument(events_p, suppress_default=True)
    events_p.add_argument("--type", dest="event_type", help="Filter by event type")
    events_p.add_argument(
        "--json", action="store_true", dest="json_output", help="JSON output"
    )

    parser.set_defaults(command="cloud")


def run_cloud(args: argparse.Namespace) -> int:
    """Dispatch cloud sub-actions."""
    from dotenv import load_dotenv

    load_dotenv()

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

    if cmd == "serial":
        from crsbench.cloud.cli._serial import run_serial

        return run_serial(args)

    if cmd == "exec":
        from crsbench.cloud.cli._exec import run_exec

        return run_exec(args)

    if cmd == "log":
        from crsbench.cloud.cli._log import run_log

        return run_log(args)

    if cmd in {"add-workers", "add-evaluators"}:
        from crsbench.cloud.cli._add_capacity import run_add_capacity

        return run_add_capacity(args)

    if cmd == "events":
        from crsbench.cloud.cli._events import run_events

        return run_events(args)

    if cmd == "collect":
        from crsbench.cloud.cli._collect import run_collect

        return run_collect(args)

    if cmd == "launch":
        from crsbench.cloud.cli._launch import run_launch

        return run_launch(args)

    if cmd == "preflight":
        from crsbench.cloud.cli._preflight import run_preflight

        return run_preflight(args)

    if cmd == "derive-unfinished-trial-keys":
        from crsbench.cloud.cli._derive_unfinished_trial_keys import (
            run_derive_unfinished_trial_keys,
        )

        return run_derive_unfinished_trial_keys(args)

    if cmd == "teardown":
        from crsbench.cloud.cli._teardown import run_teardown

        return run_teardown(args)

    logger.error("Unknown cloud command: {}", cmd)
    return 2
