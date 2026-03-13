"""Status sub-action for crsbench cloud CLI."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import argparse


def run_status(args: argparse.Namespace) -> int:
    """Show experiment fleet and job status (placeholder -- implemented in Task 2)."""
    raise NotImplementedError("run_status not yet implemented")
