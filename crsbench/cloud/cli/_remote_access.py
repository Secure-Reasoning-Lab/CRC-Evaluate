"""Shared remote target selection and provider-owned SSH helpers."""

from __future__ import annotations

import sys

from crsbench.cloud.cli._instance_inventory import (
    CloudInstanceInventoryRow,
    list_inventory_selector_matches,
    resolve_inventory_selector,
)
from crsbench.cloud.transport import transport_for_provider
from crsbench.utils.logger import get_logger

logger = get_logger(__name__)


def select_target(
    rows: list[CloudInstanceInventoryRow],
    selector: str | None,
) -> CloudInstanceInventoryRow | None:
    """Resolve one target row from a live inventory list."""
    if selector:
        matches = list_inventory_selector_matches(rows, selector)
        selected = resolve_inventory_selector(rows, selector)
        if selected is None:
            if matches:
                print_selection_rows(matches)
                if not sys.stdin.isatty():
                    logger.error(
                        "Selector {} matched multiple live cloud instances; "
                        "choose a more specific selector when stdin is not interactive",
                        selector,
                    )
                    return None
                logger.warning(
                    "Selector {} matched multiple live cloud instances; choose one",
                    selector,
                )
                return _prompt_for_target(
                    matches,
                    prompt=f"Select {selector} instance number: ",
                )
            logger.error("No live cloud instance matched selector {}", selector)
            print_selection_rows(rows)
            return None
        return selected

    print_selection_rows(rows)
    return _prompt_for_target(rows)


def _prompt_for_target(
    rows: list[CloudInstanceInventoryRow],
    *,
    prompt: str = "Select instance number: ",
) -> CloudInstanceInventoryRow | None:
    """Prompt the operator to choose one row from a printed inventory list."""
    if not sys.stdin.isatty():
        logger.error("Specify an instance name when stdin is not interactive")
        return None

    raw_value = input(prompt).strip()
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
    tty: bool = False,
) -> list[str]:
    """Build a provider-specific operator SSH command for one live target."""
    transport = transport_for_provider(target.provider)
    return transport.build_ssh_command(
        target,
        remote_command=remote_command,
        tty=tty,
    )


def build_serial_command(
    target: CloudInstanceInventoryRow,
    *,
    port: int = 1,
) -> list[str]:
    """Build a provider-specific operator serial-console command."""
    transport = transport_for_provider(target.provider)
    return transport.build_serial_command(
        target,
        port=port,
    )
