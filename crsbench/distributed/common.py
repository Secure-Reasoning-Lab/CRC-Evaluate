"""Shared helpers for configless distributed worker/evaluator runtime."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Optional

from crsbench.distributed.queue import create_redis_connection
from crsbench.distributed.registry import RegistryClient, RuntimeRegistration
from crsbench.utils.logger import get_logger

if TYPE_CHECKING:
    from redis import Redis

logger = get_logger(__name__)


def normalize_cpu_tag(value: Optional[str]) -> Optional[str]:
    """Normalize cpu_tag values (trim whitespace, empty -> None)."""
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def discover_registered_experiments(
    redis_host: str,
    *,
    max_poll_attempts: Optional[int] = None,
    poll_interval_seconds: int = 5,
) -> tuple["Redis", dict[str, RuntimeRegistration]]:
    """Poll registry until at least one experiment is discovered.

    Args:
        redis_host: Redis host for registry access.
        max_poll_attempts: Optional cap for polling attempts. ``None`` means
            poll indefinitely.
        poll_interval_seconds: Sleep interval between failed polls.

    Returns:
        Tuple of ``(redis_connection, experiments)`` where experiments is a map
        from experiment name to ``RuntimeRegistration``.

    Raises:
        RuntimeError: If max polling attempts are reached without experiments.
    """
    redis_conn = create_redis_connection(redis_host)
    registry = RegistryClient(redis_conn)

    attempts = 0
    experiments: dict[str, RuntimeRegistration] = {}
    while not experiments:
        try:
            experiments = registry.list_experiments()
        except Exception as exc:
            logger.warning(f"Registry poll failed: {exc}")

        if experiments:
            break

        attempts += 1
        if max_poll_attempts is not None and attempts >= max_poll_attempts:
            raise RuntimeError(
                f"No experiments found after {attempts} attempts in registry"
            )
        logger.info(
            f"No experiments registered yet, waiting {poll_interval_seconds}s..."
        )
        time.sleep(poll_interval_seconds)

    return redis_conn, experiments


def resolve_cli_or_first_metadata(
    *,
    cli_value: Optional[str],
    metadata_values: list[str],
    field_name: str,
) -> Optional[str]:
    """Resolve a string setting with CLI precedence and deterministic fallback.

    Resolution order:
    1. CLI value (if provided)
    2. First metadata value in caller-provided order
    3. ``None``

    If multiple distinct metadata values are found, logs a warning and keeps
    the first value from the provided list. Callers must provide a stable
    ordering (for example sorted by experiment name).
    """
    if cli_value is not None:
        return cli_value

    if not metadata_values:
        return None

    resolved = metadata_values[0]
    if len(set(metadata_values)) > 1:
        logger.warning(
            f"Multiple {field_name} values found across experiments; "
            f"using first value in stable order: {resolved}"
        )
    return resolved


def collect_validated_int_metadata(
    *,
    registrations: list[RuntimeRegistration],
    attr_name: str,
    field_name: str,
    minimum: int,
) -> list[int]:
    """Collect integer metadata values and validate minimum constraints.

    Args:
        registrations: Ordered registrations to scan.
        attr_name: RuntimeRegistration integer field name.
        field_name: Human-readable field name used in error messages.
        minimum: Minimum allowed value (inclusive).

    Returns:
        A list of validated integer values for non-None metadata entries.

    Raises:
        RuntimeError: If metadata contains non-integer or too-small values.
    """
    values: list[int] = []
    for reg in registrations:
        value: Any = getattr(reg, attr_name)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int):
            raise RuntimeError(
                f"Invalid {field_name} metadata for experiment "
                f"'{reg.experiment}': {value!r} (expected int >= {minimum})"
            )
        if value < minimum:
            raise RuntimeError(
                f"Invalid {field_name} metadata for experiment "
                f"'{reg.experiment}': {value} (must be >= {minimum})"
            )
        values.append(value)
    return values


def validate_optional_int_override(
    *,
    value: Optional[int],
    field_name: str,
    minimum: int,
) -> Optional[int]:
    """Validate optional integer override inputs from runtime entrypoints."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"Invalid {field_name}: {value!r} (must be int >= {minimum})")
    return value
