"""Run a remote command on a live cloud instance."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

from crsbench.cloud.cli._config_reconnect import (
    resolve_cloud_context,
    resolve_effective_experiment_name,
)
from crsbench.cloud.cli._instance_inventory import (
    list_cloud_instances,
    list_inventory_selector_matches,
)
from crsbench.cloud.cli._remote_access import build_ssh_command, select_target
from crsbench.cloud.providers import provisioner_for_context
from crsbench.utils.logger import get_logger

if TYPE_CHECKING:
    import argparse

logger = get_logger(__name__)


def run_exec(args: argparse.Namespace) -> int:
    """Resolve a live cloud instance and run one remote command on it."""
    if (
        hasattr(args, "instance")
        and hasattr(args, "exec_command")
        and not args.exec_command
    ):
        logger.error("cloud exec requires a command after '--'")
        return 2

    experiment_name = resolve_effective_experiment_name(args.config, None)
    context = resolve_cloud_context(args.config, experiment_name)
    provisioner = provisioner_for_context(context)
    rows = list_cloud_instances(context, experiment_name, provisioner)
    if not rows:
        logger.error("No live instances found for experiment {}", experiment_name)
        return 1

    selector, exec_command = _resolve_exec_request(args, rows)
    if exec_command is None:
        return 2

    selected = select_target(rows, selector)
    if selected is None:
        return 1

    cmd = build_ssh_command(
        selected,
        remote_command=exec_command,
        tty=False,
    )
    try:
        return subprocess.run(cmd, check=False).returncode
    except KeyboardInterrupt:
        return 130


def _resolve_exec_request(
    args: argparse.Namespace,
    rows,
) -> tuple[str | None, list[str] | None]:
    """Resolve optional target selector and remote command from parsed args."""
    if hasattr(args, "instance") and hasattr(args, "exec_command"):
        exec_command = list(args.exec_command or [])
        if not exec_command:
            logger.error("cloud exec requires a command after '--'")
            return None, None
        return getattr(args, "instance", None), exec_command

    raw_args = list(getattr(args, "exec_args", []) or [])
    if raw_args and raw_args[0] == "--":
        raw_args = raw_args[1:]
    if not raw_args:
        logger.error("cloud exec requires a command after '--'")
        return None, None

    selector: str | None = None
    exec_command = raw_args
    if len(raw_args) > 1:
        candidate = raw_args[0]
        if list_inventory_selector_matches(rows, candidate):
            selector = candidate
            exec_command = raw_args[1:]
            if exec_command and exec_command[0] == "--":
                exec_command = exec_command[1:]

    if not exec_command:
        logger.error("cloud exec requires a command after '--'")
        return None, None
    return selector, exec_command
