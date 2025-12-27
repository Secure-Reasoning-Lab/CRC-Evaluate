"""Coverage CLI module for CRSBench."""

from crsbench.evaluation.coverage.cli.coverage_command import (
    add_coverage_subparser,
    run_coverage,
)

__all__ = ["add_coverage_subparser", "run_coverage"]
