from __future__ import annotations

from typing import Any, Mapping

from crsbench.validation.schemas import ExperimentConfig


def validate_grouped_config(grouped_config: Mapping[str, Any]) -> ExperimentConfig:
    return ExperimentConfig(**dict(grouped_config))
