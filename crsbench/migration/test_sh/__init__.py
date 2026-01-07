"""
test.sh generation package for CRSBench benchmarks.

This package uses Claude Agent SDK to automatically generate test.sh functional
test scripts for benchmarks that don't have them. It analyzes project repositories,
discovers unit tests, and generates appropriate test.sh scripts.
"""

from crsbench.migration.test_sh.generator import ShTestGenerator
from crsbench.migration.test_sh.utils import (
    add_generation_header,
    copy_benchmark_files_to_project,
    generate_test_sh_for_benchmark,
)

# Backward compatibility aliases
_add_generation_header = add_generation_header
_copy_benchmark_files_to_project = copy_benchmark_files_to_project

__all__ = [
    "ShTestGenerator",
    "generate_test_sh_for_benchmark",
    "add_generation_header",
    "copy_benchmark_files_to_project",
    # Backward compatibility
    "_add_generation_header",
    "_copy_benchmark_files_to_project",
]
