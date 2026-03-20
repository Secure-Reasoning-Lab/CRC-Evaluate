"""Shared remote target selection and SSH command helpers for cloud CLI tools."""

from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path

from crsbench.cloud.cli._instance_inventory import (
    CloudInstanceInventoryRow,
    resolve_inventory_selector,
)
from crsbench.utils.logger import get_logger

logger = get_logger(__name__)


def select_target(
    rows: list[CloudInstanceInventoryRow],
    selector: str | None,
) -> CloudInstanceInventoryRow | None:
    """Resolve one target row from a live inventory list."""
    if selector:
        selected = resolve_inventory_selector(rows, selector)
        if selected is None:
            logger.error("No live cloud instance matched selector {}", selector)
            print_selection_rows(rows)
            return None
        return selected

    print_selection_rows(rows)
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


def print_selection_rows(rows: list[CloudInstanceInventoryRow]) -> None:
    """Print numbered instance rows for operator selection."""
    for index, row in enumerate(rows, start=1):
        sys.stdout.write(f"{index}. {row.name} ({row.alias}, {row.role}, {row.zone})\n")


def build_ssh_command(
    target: CloudInstanceInventoryRow,
    *,
    remote_command: list[str] | None = None,
) -> list[str]:
    """Build a gcloud compute ssh command for one live target."""
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
    identity_file = detect_ssh_key_file()
    if identity_file is not None:
        cmd.append(f"--ssh-key-file={identity_file}")
    if remote_command:
        cmd.append(f"--command={shlex.join(remote_command)}")
    return cmd


def detect_ssh_key_file() -> Path | None:
    """Best-effort detection of the local SSH identity used for GCE access."""
    configured = configured_gcloud_ssh_key_file()
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


def configured_gcloud_ssh_key_file() -> Path | None:
    """Read compute/ssh_key_file from gcloud config when it points to a file."""
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
