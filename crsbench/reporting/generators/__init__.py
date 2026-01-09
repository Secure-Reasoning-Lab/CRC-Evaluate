"""Report generators for different output formats."""

from crsbench.reporting.generators.html import HTMLReportGenerator
from crsbench.reporting.generators.json import JSONReportGenerator

__all__ = [
    "HTMLReportGenerator",
    "JSONReportGenerator",
]
