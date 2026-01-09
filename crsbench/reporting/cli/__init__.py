"""CLI commands for the reporting module."""

from crsbench.reporting.cli.dashboard_command import (
    add_dashboard_subparser,
    run_dashboard,
)
from crsbench.reporting.cli.report_command import (
    add_report_subparser,
    run_report,
)

__all__ = [
    "add_report_subparser",
    "run_report",
    "add_dashboard_subparser",
    "run_dashboard",
]
