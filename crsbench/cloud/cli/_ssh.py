"""Open an operator SSH session to a live cloud instance."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

from crsbench.cloud.cli._config_reconnect import (
    resolve_cloud_context,
    resolve_effective_experiment_name,
)
from crsbench.cloud.cli._instance_inventory import (
    list_cloud_instances,
)
from crsbench.cloud.cli._remote_access import build_ssh_command, select_target
from crsbench.cloud.providers import provisioner_for_context
from crsbench.utils.logger import get_logger

if TYPE_CHECKING:
    import argparse


logger = get_logger(__name__)


def run_ssh(args: argparse.Namespace) -> int:
    """Resolve a live cloud instance and open an interactive SSH session."""
    try:
        experiment_name = resolve_effective_experiment_name(args.config, None)
        context = resolve_cloud_context(args.config, experiment_name)
        provisioner = provisioner_for_context(context)
        rows = list_cloud_instances(context, experiment_name, provisioner)
        if not rows:
            logger.error("No live instances found for experiment {}", experiment_name)
            return 1

        selected = select_target(rows, args.instance)
        if selected is None:
            return 1

        cmd = build_ssh_command(
            selected,
            remote_command=["sudo", "su", "-", "crsbench"],
            tty=True,
        )
        return subprocess.run(cmd, check=False).returncode
    except KeyboardInterrupt:
        return 130
