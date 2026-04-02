"""List live cloud instances for one experiment config."""

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING

from crsbench.cloud.cli._config_reconnect import (
    resolve_cloud_context,
    resolve_effective_experiment_name,
)
from crsbench.cloud.cli._instance_inventory import list_cloud_instances
from crsbench.cloud.providers import provisioner_for_context

if TYPE_CHECKING:
    import argparse


def run_list(args: argparse.Namespace) -> int:
    """Print live cloud instances discovered from config plus saved launch state."""
    experiment_name = resolve_effective_experiment_name(args.config, None)
    context = resolve_cloud_context(args.config, experiment_name)
    provisioner = provisioner_for_context(context)
    rows = list_cloud_instances(context, experiment_name, provisioner)

    if args.json_output:
        sys.stdout.write(json.dumps([row.as_dict() for row in rows], indent=2) + "\n")
        return 0

    if not rows:
        sys.stdout.write(
            f"No live instances found for experiment '{experiment_name}'.\n"
        )
        return 0

    _print_rows(rows)
    return 0


def _print_rows(rows) -> None:
    headers = [
        ("ALIAS", "alias"),
        ("NAME", "name"),
        ("ROLE", "role"),
        ("SOURCE", "placement_source"),
        ("ZONE", "zone"),
        ("STATUS", "status"),
        ("INTERNAL_IP", "internal_ip"),
        ("EXTERNAL_IP", "external_ip"),
    ]
    widths: dict[str, int] = {}
    for label, field in headers:
        widths[field] = max(
            len(label),
            *(len(str(getattr(row, field) or "")) for row in rows),
        )

    header_line = "  ".join(label.ljust(widths[field]) for label, field in headers)
    sys.stdout.write(header_line + "\n")
    for row in rows:
        sys.stdout.write(
            "  ".join(
                str(getattr(row, field) or "").ljust(widths[field])
                for _, field in headers
            )
            + "\n"
        )
