"""Benchmark CI module for end-to-end testing of CRSBench benchmarks.

This module provides comprehensive testing for benchmarks including:
- File format validation
- Build verification
- POV reproduction testing
- Patch verification
- test.sh execution validation
"""

from crsbench.benchmark_ci.utils import (
    JobContext,
    ExecJobType,
    TaskMode,
    Task,
    Harness,
    Vulnerability,
    POV,
)
from crsbench.benchmark_ci.run_helper import (
    TestExitCode,
    TestExecutionError,
    DockerExecutionError,
    TestFailedError,
    FatalTestError,
)

__all__ = [
    'JobContext',
    'ExecJobType',
    'TaskMode',
    'Task',
    'Harness',
    'Vulnerability',
    'POV',
    # Test exit code handling
    'TestExitCode',
    'TestExecutionError',
    'DockerExecutionError',
    'TestFailedError',
    'FatalTestError',
]
