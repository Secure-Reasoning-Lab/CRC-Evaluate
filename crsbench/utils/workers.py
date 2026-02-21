"""Worker configuration utilities.

This module provides utilities for resolving worker count configuration
with a clear priority hierarchy: CLI > Config > Default.
"""

from typing import Optional

DEFAULT_WORKERS = 4


def resolve_build_workers(
    cli_workers: Optional[int] = None,
    config_workers: Optional[int] = None,
) -> int:
    """Resolve the number of workers for building variants.

    Priority (highest to lowest):
    1. CLI argument (--build-workers)
    2. Config file value (build_workers field)
    3. Default value (4)

    Args:
        cli_workers: Worker count from CLI argument (highest priority)
        config_workers: Worker count from config file

    Returns:
        Resolved worker count (always >= 1)
    """
    return _resolve_workers(cli_workers, config_workers)


def resolve_verify_workers(
    cli_workers: Optional[int] = None,
    config_workers: Optional[int] = None,
) -> int:
    """Resolve the number of workers for POV/patch verification.

    Priority (highest to lowest):
    1. CLI argument (--verify-workers)
    2. Config file value (verify_workers field)
    3. Default value (4)

    Args:
        cli_workers: Worker count from CLI argument (highest priority)
        config_workers: Worker count from config file

    Returns:
        Resolved worker count (always >= 1)
    """
    return _resolve_workers(cli_workers, config_workers)


def _resolve_workers(
    cli_workers: Optional[int],
    config_workers: Optional[int],
) -> int:
    """Internal helper to resolve worker count.

    Args:
        cli_workers: Worker count from CLI argument (highest priority)
        config_workers: Worker count from config file

    Returns:
        Resolved worker count (always >= 1)
    """
    # Priority 1: CLI argument
    if cli_workers is not None and cli_workers >= 1:
        return cli_workers

    # Priority 2: Config file
    if config_workers is not None and config_workers >= 1:
        return config_workers

    # Priority 3: Default
    return DEFAULT_WORKERS
