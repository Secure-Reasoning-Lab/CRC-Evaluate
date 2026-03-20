"""Follow the primary CRSBench systemd journal for a live cloud instance."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

from crsbench.cloud.cli._config_reconnect import (
    resolve_cloud_context,
    resolve_effective_experiment_name,
)
from crsbench.cloud.cli._instance_inventory import list_cloud_instances
from crsbench.cloud.cli._remote_access import build_ssh_command, select_target
from crsbench.cloud.gce.provisioner import GceProvisioner

if TYPE_CHECKING:
    import argparse


def run_log(args: argparse.Namespace) -> int:
    """Follow the role-appropriate CRSBench user journal on a live VM."""
    experiment_name = resolve_effective_experiment_name(args.config, None)
    context = resolve_cloud_context(args.config, experiment_name)
    provisioner = GceProvisioner()
    rows = list_cloud_instances(context, experiment_name, provisioner)
    if not rows:
        return 1

    selected = select_target(rows, args.instance)
    if selected is None:
        return 1

    uid_expr = 'uid="$(id -u crsbench 2>/dev/null || echo 1001)"'
    remote_command = [
        "bash",
        "-lc",
        (
            f"{uid_expr}; "
            "sudo -u crsbench env "
            'XDG_RUNTIME_DIR="/run/user/${uid}" '
            'DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/${uid}/bus" '
            f"journalctl --user -u {_service_name_for_role(selected.role)} -b -f --no-pager"
        ),
    ]
    cmd = build_ssh_command(selected, remote_command=remote_command)
    return subprocess.run(cmd, check=False).returncode


def _service_name_for_role(role: str) -> str:
    if role == "orchestrator":
        return "crsbench-orchestrator.service"
    if role == "evaluator":
        return "crsbench-evaluator.service"
    return "crsbench-worker.service"
