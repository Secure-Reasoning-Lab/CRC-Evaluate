"""CLI commands for POV verification."""

from crsbench.evaluation.verification.cli.verify_command import (
    add_verify_subparser,
    run_verify,
)

__all__ = ["add_verify_subparser", "run_verify"]
