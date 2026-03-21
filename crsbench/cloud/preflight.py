"""Provider-neutral operator-side launch preflight data."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from crsbench.cloud.models import CloudLaunchPlan


@dataclass(frozen=True)
class CloudLaunchPreflight:
    """Resolved launch inputs for provisioning plus redacted persistence copies."""

    resolved_plan: CloudLaunchPlan
    redacted_worker_fleets: list = field(default_factory=list)
    redacted_evaluator_fleets: list = field(default_factory=list)
    orchestrator_env: dict[str, str] = field(default_factory=dict)
    worker_placement_envs: list[dict[str, str]] = field(default_factory=list)
    evaluator_placement_envs: list[dict[str, str]] = field(default_factory=list)
