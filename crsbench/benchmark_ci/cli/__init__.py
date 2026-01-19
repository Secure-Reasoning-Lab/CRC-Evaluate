"""CLI module for benchmark CI testing."""

from crsbench.benchmark_ci.cli.main import (
    add_ci_subparser,
    main,
    run_ci,
    run_ci_parse,
)

__all__ = ["add_ci_subparser", "main", "run_ci", "run_ci_parse"]
