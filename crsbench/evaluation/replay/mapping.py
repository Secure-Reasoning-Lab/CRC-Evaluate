"""Packaged benchmark-to-OSS-Fuzz project mapping helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from importlib import resources
from typing import Literal

MappingReason = Literal["mapped", "unsupported_mapping", "missing_mapping"]


@dataclass(frozen=True)
class MappingResolution:
    """Resolution result for one benchmark name."""

    benchmark: str
    mapped_project: str | None
    reason: MappingReason


def load_benchmark_project_mapping() -> dict[str, str | None]:
    """Load the packaged benchmark-to-project mapping JSON resource."""
    payload = (
        resources.files("crsbench.evaluation.replay")
        .joinpath("benchmark_to_oss_fuzz_project.json")
        .read_text(encoding="utf-8")
    )
    return json.loads(payload)


def resolve_mapped_project(
    benchmark: str, mapping: dict[str, str | None]
) -> MappingResolution:
    """Resolve one benchmark name to an OSS-Fuzz project mapping outcome."""
    if benchmark not in mapping:
        return MappingResolution(
            benchmark=benchmark,
            mapped_project=None,
            reason="missing_mapping",
        )

    mapped_project = mapping[benchmark]
    if mapped_project is None:
        return MappingResolution(
            benchmark=benchmark,
            mapped_project=None,
            reason="unsupported_mapping",
        )

    return MappingResolution(
        benchmark=benchmark,
        mapped_project=mapped_project,
        reason="mapped",
    )
