"""Job classes for experiment execution.

This module provides job abstractions for CRS experiment trials:

- CRSRunJob: CRS execution with internal periodic verification
"""

from crsbench.experiment.jobs.crs_run import CRSRunJob

__all__ = [
    "CRSRunJob",
]
