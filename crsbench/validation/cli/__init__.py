"""CLI module for POV validation commands."""

from crsbench.validation.cli.validate_command import (
    add_validate_subparser,
    run_validate,
)

__all__ = [
    "add_validate_subparser",
    "run_validate",
]
