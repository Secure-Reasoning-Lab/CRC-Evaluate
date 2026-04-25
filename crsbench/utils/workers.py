"""Worker configuration utilities.

Build workers use a simple priority hierarchy: CLI > config > default.
Verify workers additionally inherit the supervisor-assigned cpuset width when
running inside a pinned child process and no explicit override was provided.
"""

import os
from typing import Optional

from crsbench.utils.cpu_pool import cpuset_count

DEFAULT_WORKERS = 4


def resolve_build_workers(
    cli_workers: Optional[int] = None,
    config_workers: Optional[int] = None,
) -> int:
    """Resolve the number of parallel build jobs.

    Priority (highest to lowest):
    1. Explicit caller value (highest priority)
    2. Fallback config value
    3. Default value (4)

    Args:
        cli_workers: Worker count (highest priority)
        config_workers: Fallback worker count

    Returns:
        Resolved worker count (always >= 1)
    """
    return _resolve_workers(cli_workers, config_workers)


def resolve_verify_workers(
    cli_workers: Optional[int] = None,
    config_workers: Optional[int] = None,
) -> int:
    """Resolve the number of parallel verification jobs.

    Priority (highest to lowest):
    1. Explicit caller value (highest priority)
    2. Fallback config value
    3. Active cpuset width inherited from the supervisor
    4. Default value (4)

    Args:
        cli_workers: Worker count (highest priority)
        config_workers: Fallback worker count

    Returns:
        Resolved worker count (always >= 1)
    """
    return _resolve_workers(
        cli_workers,
        config_workers,
        default_workers=_resolve_cpuset_verify_workers(),
    )


def _resolve_workers(
    cli_workers: Optional[int],
    config_workers: Optional[int],
    *,
    default_workers: Optional[int] = None,
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
    if default_workers is not None and default_workers >= 1:
        return default_workers

    return DEFAULT_WORKERS


def _resolve_cpuset_verify_workers() -> Optional[int]:
    """Return supervisor-assigned verify CPU width when running in a cpuset child."""
    cpuset = os.environ.get("OSS_FUZZ_CPUSET_CPUS", "").strip()
    if not cpuset:
        return None
    try:
        return cpuset_count(cpuset)
    except ValueError:
        return None
