"""Worker configuration utilities.

This module provides utilities for resolving worker count configuration
with a clear priority hierarchy: CLI > Environment > Config > Default.
"""

import os
from typing import Optional

DEFAULT_WORKERS = 4
WORKERS_ENV_VAR = "CRSBENCH_WORKERS"


def resolve_workers(
    cli_workers: Optional[int] = None,
    config_workers: Optional[int] = None,
) -> int:
    """Resolve the number of workers to use.

    Priority (highest to lowest):
    1. CLI argument (--workers)
    2. Environment variable (CRSBENCH_WORKERS)
    3. Config file value (workers field)
    4. Default value (4)

    Args:
        cli_workers: Worker count from CLI argument (highest priority)
        config_workers: Worker count from config file

    Returns:
        Resolved worker count (always >= 1)
    """
    # Priority 1: CLI argument
    if cli_workers is not None and cli_workers >= 1:
        return cli_workers

    # Priority 2: Environment variable
    env_value = os.environ.get(WORKERS_ENV_VAR)
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
