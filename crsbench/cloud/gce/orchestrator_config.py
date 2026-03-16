"""Helpers for mutating experiment config on the remote orchestrator VM."""

from __future__ import annotations

from pathlib import Path

import yaml


def patch_experiment_config_for_local_redis(
    config_path: str | Path,
    *,
    redis_host: str,
) -> None:
    """Rewrite compatibility and grouped Redis config to the orchestrator-local host."""
    path = Path(config_path)
    raw_config = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw_config, dict):
        raise ValueError(
            f"Experiment config at {path} must deserialize to a mapping, got "
            f"{type(raw_config).__name__}"
        )

    raw_config["redis_host"] = redis_host

    runtime = raw_config.get("runtime")
    if runtime is None:
        runtime = {}
        raw_config["runtime"] = runtime
    if not isinstance(runtime, dict):
        raise ValueError("Experiment config 'runtime' section must be a mapping")

    runtime_redis = runtime.get("redis")
    if runtime_redis is None:
        runtime_redis = {}
        runtime["redis"] = runtime_redis
    if not isinstance(runtime_redis, dict):
        raise ValueError("Experiment config 'runtime.redis' section must be a mapping")

    runtime_redis["host"] = redis_host
    path.write_text(
        yaml.safe_dump(raw_config, sort_keys=False),
        encoding="utf-8",
    )
