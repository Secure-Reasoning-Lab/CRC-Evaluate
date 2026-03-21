"""CPU pool manager for distributed worker CPU affinity.

This module provides utilities for:
1. Parsing cpuset strings (e.g., "0-3,8-11")
2. Managing a pool of available CPUs for dynamic allocation
3. Thread-safe CPU allocation/release for worker processes
"""

import os
import threading
from typing import Optional, Union


def parse_cpuset(cpuset_str: str) -> list[int]:
    """Parse cpuset string to list of CPU IDs.

    Args:
        cpuset_str: CPU set string in taskset format

    Returns:
        Sorted list of CPU IDs

    Examples:
        >>> parse_cpuset("0-3")
        [0, 1, 2, 3]
        >>> parse_cpuset("0-3,8-11")
        [0, 1, 2, 3, 8, 9, 10, 11]
        >>> parse_cpuset("0,2,4,6")
        [0, 2, 4, 6]
    """
    cpus = []
    for part in cpuset_str.split(","):
        part = part.strip()
        if "-" in part:
            start, end = part.split("-")
            cpus.extend(range(int(start), int(end) + 1))
        else:
            cpus.append(int(part))
    return sorted(cpus)


def cpuset_count(cpuset_str: str) -> int:
    """Get number of CPUs from cpuset string.

    Args:
        cpuset_str: CPU set string in taskset format

    Returns:
        Number of CPUs

    Examples:
        >>> cpuset_count("0-15")
        16
        >>> cpuset_count("0-3,8-11")
        8
    """
    return len(parse_cpuset(cpuset_str))


def format_cpuset(cpus: list[int]) -> str:
    """Format list of CPU IDs as cpuset string.

    Converts a list of CPU IDs into a compact cpuset string format,
    using ranges where possible.

    Args:
        cpus: List of CPU IDs

    Returns:
        Cpuset string in taskset format

    Examples:
        >>> format_cpuset([0, 1, 2, 3])
        '0-3'
        >>> format_cpuset([0, 1, 2, 3, 8, 9, 10, 11])
        '0-3,8-11'
        >>> format_cpuset([0, 2, 4, 6])
        '0,2,4,6'
    """
    if not cpus:
        return ""

    # Sort CPUs
    sorted_cpus = sorted(cpus)

    # Group consecutive CPUs into ranges
    ranges = []
    start = sorted_cpus[0]
    end = sorted_cpus[0]

    for cpu in sorted_cpus[1:]:
        if cpu == end + 1:
            # Extend current range
            end = cpu
        else:
            # Close current range and start new one
            if start == end:
                ranges.append(str(start))
            else:
                ranges.append(f"{start}-{end}")
            start = cpu
            end = cpu

    # Add final range
    if start == end:
        ranges.append(str(start))
    else:
        ranges.append(f"{start}-{end}")

    return ",".join(ranges)


def visible_cpu_ids(
    total_cpus: Optional[int] = None,
    *,
    skip_cpus: Optional[str] = None,
    cores: Optional[Union[str, int]] = None,
) -> list[int]:
    """Return CPUs visible to the current runtime after applying overrides."""
    if isinstance(cores, str):
        base_set = set(parse_cpuset(cores))
    elif isinstance(cores, int):
        base_set = set(range(cores))
    elif total_cpus is not None:
        base_set = set(range(total_cpus))
    else:
        try:
            base_set = set(os.sched_getaffinity(0))
        except AttributeError:
            base_set = set(range(os.cpu_count() or 1))

    if skip_cpus is not None:
        skip_set = set(parse_cpuset(skip_cpus))
        base_set -= skip_set

    if not base_set:
        raise ValueError("No CPUs available after applying skip_cpus exclusion")

    return sorted(base_set)


def visible_cpu_count(
    total_cpus: Optional[int] = None,
    *,
    skip_cpus: Optional[str] = None,
    cores: Optional[Union[str, int]] = None,
) -> int:
    """Return the count of CPUs visible to the current runtime."""
    return len(visible_cpu_ids(total_cpus=total_cpus, skip_cpus=skip_cpus, cores=cores))


def auto_cores_per_job(
    jobs: int,
    *,
    skip_cpus: Optional[str] = None,
    cores: Optional[Union[str, int]] = None,
) -> int:
    """Derive a per-job CPU width from the visible CPU envelope."""
    total = visible_cpu_count(skip_cpus=skip_cpus, cores=cores)
    return max(1, total // max(jobs, 1))


class CPUPool:
    """Thread-safe CPU pool for dynamic allocation to workers.

    Manages a pool of available CPU cores that can be allocated to workers
    and released when workers finish. Provides thread-safe operations for
    concurrent allocation/release.

    Example:
        >>> pool = CPUPool(total_cpus=16)
        >>> cpus = pool.allocate(4)  # Get 4 CPUs
        >>> cpus
        [0, 1, 2, 3]
        >>> pool.available_count()
        12
        >>> pool.release(cpus)  # Return CPUs to pool
        >>> pool.available_count()
        16
        >>> pool = CPUPool(cores="16-47")  # Only use cores 16-47
        >>> pool = CPUPool(skip_cpus="0-3")  # Skip system cores
        >>> pool = CPUPool(cores="0-47", skip_cpus="0-3")  # Cores 4-47
        >>> pool = CPUPool(cores=32)  # First 32 cores (0-31)
    """

    def __init__(
        self,
        total_cpus: Optional[int] = None,
        *,
        skip_cpus: Optional[str] = None,
        cores: Optional[Union[str, int]] = None,
    ):
        """Initialize CPU pool.

        Args:
            total_cpus: Total number of CPUs (default: from os.cpu_count()).
                Ignored if ``cores`` is provided.
            skip_cpus: CPUs to exclude from allocation (cpuset format,
                e.g., ``"0-3,8-11"``).
            cores: Restrict pool to these cores. Accepts a cpuset string
                (e.g., ``"16-47"``) or an integer count (first N cores).
                Takes precedence over ``total_cpus``.
        """
        # 1. Determine the visible CPU envelope
        base_set = set(
            visible_cpu_ids(total_cpus=total_cpus, skip_cpus=skip_cpus, cores=cores)
        )

        # 2. Assign to instance variables
        self.total_cpus = len(base_set)
        self.available = base_set
        self.lock = threading.Lock()

    def allocate(self, count: int) -> Optional[list[int]]:
        """Try to allocate `count` CPUs from the pool.

        Args:
            count: Number of CPUs to allocate

        Returns:
            List of allocated CPU IDs, or None if not enough CPUs available

        Thread-safe: Yes
        """
        with self.lock:
            if len(self.available) < count:
                return None

            # Allocate lowest-numbered CPUs for better locality
            cpus = sorted(self.available)[:count]
            self.available -= set(cpus)
            return cpus

    def release(self, cpus: list[int]) -> None:
        """Return CPUs to the pool.

        Args:
            cpus: List of CPU IDs to release

        Thread-safe: Yes
        """
        with self.lock:
            self.available.update(cpus)

    def available_count(self) -> int:
        """Get number of available CPUs.

        Returns:
            Number of CPUs currently available for allocation

        Thread-safe: Yes
        """
        with self.lock:
            return len(self.available)
