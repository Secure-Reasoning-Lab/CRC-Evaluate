"""Tests for CPU pool manager utility."""

import pytest
from crsbench.utils.cpu_pool import CPUPool, cpuset_count, format_cpuset, parse_cpuset

# ---------------------------------------------------------------------------
# parse_cpuset
# ---------------------------------------------------------------------------


def test_parse_cpuset_range():
    """Parse a simple range."""
    assert parse_cpuset("0-3") == [0, 1, 2, 3]


def test_parse_cpuset_mixed():
    """Parse a mix of ranges and individual CPUs."""
    assert parse_cpuset("0-3,8-11") == [0, 1, 2, 3, 8, 9, 10, 11]


def test_parse_cpuset_individual():
    """Parse individual CPUs separated by commas."""
    assert parse_cpuset("0,2,4,6") == [0, 2, 4, 6]


# ---------------------------------------------------------------------------
# format_cpuset
# ---------------------------------------------------------------------------


def test_format_cpuset_range():
    """Format consecutive CPUs as a range."""
    assert format_cpuset([0, 1, 2, 3]) == "0-3"


def test_format_cpuset_mixed():
    """Format non-contiguous CPUs as multiple ranges."""
    assert format_cpuset([0, 1, 2, 3, 8, 9, 10, 11]) == "0-3,8-11"


def test_format_cpuset_empty():
    """Format an empty list as an empty string."""
    assert format_cpuset([]) == ""


# ---------------------------------------------------------------------------
# cpuset_count
# ---------------------------------------------------------------------------


def test_cpuset_count():
    """Count CPUs in a cpuset string."""
    assert cpuset_count("0-15") == 16


# ---------------------------------------------------------------------------
# CPUPool -- existing behaviour (regression)
# ---------------------------------------------------------------------------


def test_cpupool_default():
    """Default pool with total_cpus has correct count and allocates lowest."""
    pool = CPUPool(total_cpus=8)
    assert pool.available_count() == 8
    cpus = pool.allocate(4)
    assert cpus == [0, 1, 2, 3]


def test_cpupool_allocate_release():
    """Allocate and release restores the pool to its original size."""
    pool = CPUPool(total_cpus=8)
    cpus = pool.allocate(4)
    assert cpus is not None
    assert pool.available_count() == 4
    pool.release(cpus)
    assert pool.available_count() == 8


# ---------------------------------------------------------------------------
# CPUPool -- skip_cpus parameter
# ---------------------------------------------------------------------------


def test_cpupool_skip_cpus():
    """skip_cpus removes specified cores from the pool."""
    pool = CPUPool(total_cpus=16, skip_cpus="0-3")
    assert pool.available_count() == 12
    assert pool.available == set(range(4, 16))


def test_cpupool_skip_cpus_allocate():
    """After skip, allocate returns the lowest available cores."""
    pool = CPUPool(total_cpus=16, skip_cpus="0-3")
    cpus = pool.allocate(4)
    assert cpus == [4, 5, 6, 7]


def test_cpupool_skip_cpus_noncontiguous():
    """Skipping non-contiguous ranges works correctly."""
    pool = CPUPool(total_cpus=16, skip_cpus="0-3,12-15")
    assert pool.available_count() == 8
    assert pool.available == set(range(4, 12))


def test_cpupool_skip_all_raises():
    """Skipping all cores raises ValueError."""
    with pytest.raises(ValueError, match="No CPUs available"):
        CPUPool(total_cpus=4, skip_cpus="0-3")


# ---------------------------------------------------------------------------
# CPUPool -- cores parameter (string cpuset)
# ---------------------------------------------------------------------------


def test_cpupool_cores_string():
    """cores as cpuset string restricts the pool to specified range."""
    pool = CPUPool(cores="16-47")
    assert pool.available_count() == 32
    assert pool.available == set(range(16, 48))


def test_cpupool_cores_string_allocate():
    """Allocate from a string-specified core set returns lowest cores."""
    pool = CPUPool(cores="16-47")
    cpus = pool.allocate(4)
    assert cpus == [16, 17, 18, 19]


def test_cpupool_cores_noncontiguous():
    """Non-contiguous cores string works correctly."""
    pool = CPUPool(cores="0-3,16-19")
    assert pool.available_count() == 8
    assert pool.available == {0, 1, 2, 3, 16, 17, 18, 19}


# ---------------------------------------------------------------------------
# CPUPool -- cores parameter (integer count)
# ---------------------------------------------------------------------------


def test_cpupool_cores_int():
    """cores as integer count creates pool from first N cores."""
    pool = CPUPool(cores=32)
    assert pool.available_count() == 32
    assert pool.available == set(range(32))


def test_cpupool_cores_int_is_range():
    """Integer cores behaves like range(N) and allocates lowest."""
    pool = CPUPool(cores=4)
    cpus = pool.allocate(2)
    assert cpus == [0, 1]


# ---------------------------------------------------------------------------
# CPUPool -- combined skip_cpus + cores
# ---------------------------------------------------------------------------


def test_cpupool_cores_and_skip():
    """Combining cores and skip_cpus subtracts skip from core set."""
    pool = CPUPool(cores="0-15", skip_cpus="0-3")
    assert pool.available_count() == 12
    assert pool.available == set(range(4, 16))


def test_cpupool_cores_and_skip_allocate():
    """Allocate from combined core+skip pool returns lowest available."""
    pool = CPUPool(cores="0-15", skip_cpus="0-3")
    cpus = pool.allocate(4)
    assert cpus == [4, 5, 6, 7]


def test_cpupool_cores_and_skip_all_raises():
    """Skipping all specified cores raises ValueError."""
    with pytest.raises(ValueError, match="No CPUs available"):
        CPUPool(cores="0-3", skip_cpus="0-3")


def test_cpupool_skip_outside_cores_ignored():
    """Skipping cores outside the cores range has no effect."""
    pool = CPUPool(cores="4-7", skip_cpus="0-3")
    assert pool.available_count() == 4
    assert pool.available == {4, 5, 6, 7}


# ---------------------------------------------------------------------------
# CPUPool -- precedence
# ---------------------------------------------------------------------------


def test_cpupool_cores_overrides_total_cpus():
    """cores takes precedence over total_cpus."""
    pool = CPUPool(total_cpus=64, cores="0-7")
    assert pool.available_count() == 8
    assert pool.available == set(range(8))
