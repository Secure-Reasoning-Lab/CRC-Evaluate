"""Open an operator SSH session to a live cloud instance."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from crsbench.cloud.cli._config_reconnect import (
    resolve_cloud_context,
    resolve_effective_experiment_name,
)
from crsbench.cloud.cli._instance_inventory import (
    CloudInstanceInventoryRow,
    list_cloud_instances,
    resolve_inventory_selector,
)
from crsbench.cloud.gce.provisioner import GceProvisioner
from crsbench.utils.logger import get_logger

if TYPE_CHECKING:
    import argparse


logger = get_logger(__name__)


def run_ssh(args: argparse.Namespace) -> int:
    """Resolve a live cloud instance and open an interactive SSH session."""
    experiment_name = resolve_effective_experiment_name(args.config, None)
    context = resolve_cloud_context(args.config, experiment_name)
    provisioner = GceProvisioner()
    rows = list_cloud_instances(context, experiment_name, provisioner)
    if not rows:
        logger.error("No live instances found for experiment {}", experiment_name)
        return 1

    selected = _select_target(rows, args.instance)
    if selected is None:
        return 1

    cmd = _build_ssh_command(selected)
    return subprocess.run(cmd, check=False).returncode


def _select_target(
    rows: list[CloudInstanceInventoryRow],
    selector: str | None,
) -> CloudInstanceInventoryRow | None:
    if selector:
        selected = resolve_inventory_selector(rows, selector)
        if selected is None:
            logger.error("No live cloud instance matched selector {}", selector)
            _print_selection_rows(rows)
            return None
        return selected

    _print_selection_rows(rows)
    if not sys.stdin.isatty():
        logger.error("Specify an instance name when stdin is not interactive")
        return None

    raw_value = input("Select instance number: ").strip()
    if not raw_value.isdigit():
        logger.error("Invalid selection {}", raw_value or "<empty>")
        return None
    index = int(raw_value)
    if index < 1 or index > len(rows):
        logger.error("Selection {} is out of range", index)
        return None
    return rows[index - 1]


def _print_selection_rows(rows: list[CloudInstanceInventoryRow]) -> None:
    for index, row in enumerate(rows, start=1):
        sys.stdout.write(f"{index}. {row.name} ({row.alias}, {row.role}, {row.zone})\n")


def _build_ssh_command(target: CloudInstanceInventoryRow) -> list[str]:
    cmd = [
        "gcloud",
        "compute",
        "ssh",
        target.name,
        f"--project={target.project}",
        f"--zone={target.zone}",
    ]
    if target.ssh_via_iap:
        cmd.append("--tunnel-through-iap")
    identity_file = _detect_ssh_key_file()
    if identity_file is not None:
        cmd.append(f"--ssh-key-file={identity_file}")
    return cmd


def _detect_ssh_key_file() -> Path | None:
    configured = _configured_gcloud_ssh_key_file()
    if configured is not None:
        return configured

    default_identity = Path.home() / ".ssh" / "google_compute_engine"
    if default_identity.is_file():
        return default_identity

    candidates = sorted(
        Path("/tmp").glob("crsbench-oslogin-*/id_ed25519"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def _configured_gcloud_ssh_key_file() -> Path | None:
    result = subprocess.run(
        ["gcloud", "config", "get-value", "compute/ssh_key_file"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    if not value or value == "(unset)":
        return None
    candidate = Path(value).expanduser()
    return candidate if candidate.is_file() else None
