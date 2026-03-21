"""Shared read-only launch conflict checks."""

from __future__ import annotations

from pathlib import Path

from crsbench.cloud.launch_state import launch_state_path, load_launch_state


def find_launch_target_conflicts(
    *,
    config_path: Path,
    experiment_name: str,
    adapter,
    plan,
) -> list[str]:
    """Return user-facing launch conflicts without mutating any launch state."""
    conflicts: list[str] = []

    existing_state = load_launch_state(config_path, experiment_name)
    if existing_state is not None:
        conflicts.append(
            f"saved launch state exists at {launch_state_path(config_path, experiment_name)}"
        )

    live_instances = [
        *adapter.list_orchestrators(plan=plan),
        *adapter.list_workers(plan=plan),
        *adapter.list_evaluators(plan=plan),
    ]
    if live_instances:
        live_names = ", ".join(sorted({instance.name for instance in live_instances}))
        conflicts.append(f"live cloud instances already exist: {live_names}")

    return conflicts
