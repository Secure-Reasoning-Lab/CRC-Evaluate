"""Experiment orchestration for CRS evaluation.

Provides:
- Upfront build orchestration with deduplication via DAGExecutor (EXP-01)
- CRSRunJob with internal SnapshotManager + POVVerificationManager (EXP-02/03/04)
- Post-trial analysis via flat jobs for coverage and patch verification (EXP-05)
"""

from crsbench.experiment.build_orchestrator import (
    create_upfront_build_jobs,
    execute_upfront_builds,
)
from crsbench.experiment.jobs import CRSRunJob
from crsbench.experiment.post_trial import (
    TrialResult,
    create_post_trial_jobs,
    execute_post_trial_analysis,
)

__all__ = [
    "CRSRunJob",
    "TrialResult",
    "create_post_trial_jobs",
    "create_upfront_build_jobs",
    "execute_post_trial_analysis",
    "execute_upfront_builds",
]
