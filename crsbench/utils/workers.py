"""Worker configuration utilities.

This module provides utilities for resolving worker count configuration
with a clear priority hierarchy: CLI > Environment > Config > Default.
"""

import os
from typing import Optional

DEFAULT_WORKERS = 4
BUILD_WORKERS_ENV_VAR = "CRSBENCH_BUILD_WORKERS"
VERIFY_WORKERS_ENV_VAR = "CRSBENCH_VERIFY_WORKERS"


def resolve_build_workers(
    cli_workers: Optional[int] = None,
    config_workers: Optional[int] = None,
) -> int:
    """Resolve the number of workers for building variants.

    Priority (highest to lowest):
    1. CLI argument (--build-workers)
    2. Environment variable (CRSBENCH_BUILD_WORKERS)
    3. Config file value (build_workers field)
    4. Default value (4)

    Args:
        cli_workers: Worker count from CLI argument (highest priority)
        config_workers: Worker count from config file

    Returns:
        Resolved worker count (always >= 1)
    """
    return _resolve_workers(cli_workers, config_workers, BUILD_WORKERS_ENV_VAR)


def resolve_verify_workers(
    cli_workers: Optional[int] = None,
    config_workers: Optional[int] = None,
) -> int:
    """Resolve the number of workers for POV/patch verification.

    Priority (highest to lowest):
    1. CLI argument (--verify-workers)
    2. Environment variable (CRSBENCH_VERIFY_WORKERS)
    3. Config file value (verify_workers field)
    4. Default value (4)

    Args:
        cli_workers: Worker count from CLI argument (highest priority)
        config_workers: Worker count from config file

    Returns:
        Resolved worker count (always >= 1)
    """
    return _resolve_workers(cli_workers, config_workers, VERIFY_WORKERS_ENV_VAR)


def _resolve_workers(
    cli_workers: Optional[int],
    config_workers: Optional[int],
    env_var: str,
) -> int:
    """Internal helper to resolve worker count.

    Args:
        cli_workers: Worker count from CLI argument (highest priority)
        config_workers: Worker count from config file
        env_var: Environment variable name to check

    Returns:
        Resolved worker count (always >= 1)
    """
    # Priority 1: CLI argument
    if cli_workers is not None and cli_workers >= 1:
        return cli_workers

    # Priority 2: Environment variable
    env_value = os.environ.get(env_var)
    if env_value:
        try:
            env_workers = int(env_value)
            if env_workers >= 1:
                return env_workers
        except ValueError:
            pass  # Invalid env value, continue to next priority

    # Priority 3: Config file
    if config_workers is not None and config_workers >= 1:
        return config_workers

    # Priority 4: Default
    return DEFAULT_WORKERS


# Backwards compatibility alias
def resolve_workers(
    cli_workers: Optional[int] = None,
    config_workers: Optional[int] = None,
) -> int:
    """Resolve worker count (deprecated, use resolve_build_workers instead)."""
    return resolve_build_workers(cli_workers, config_workers)
