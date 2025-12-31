"""CLI commands for POV and patch verification."""

from crsbench.evaluation.verification.cli.patch_verify_command import (
    add_patch_verify_subparser,
    run_patch_verify,
)
from crsbench.evaluation.verification.cli.pov_verify_command import (
    add_verify_subparser,
    run_verify,
)

__all__ = [
    # POV verification
    "add_verify_subparser",
    "run_verify",
    # Patch verification
    "add_patch_verify_subparser",
    "run_patch_verify",
]
