"""CPU pool manager for distributed worker CPU affinity.

This module provides utilities for:
1. Parsing cpuset strings (e.g., "0-3,8-11")
2. Managing a pool of available CPUs for dynamic allocation
3. Thread-safe CPU allocation/release for worker processes
"""

import os
import threading
from typing import Optional


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
    """

    def __init__(self, total_cpus: Optional[int] = None):
        """Initialize CPU pool.

        Args:
            total_cpus: Total number of CPUs (default: from os.cpu_count())
        """
        self.total_cpus = total_cpus or os.cpu_count() or 1
        self.available = set(range(self.total_cpus))
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
