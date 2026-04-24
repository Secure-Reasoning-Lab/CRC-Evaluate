"""Experiment-scoped queue cleanup helpers.

Supports both queue models:
- flat (shared queues)
- per-experiment (legacy dedicated queues)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, cast

from crsbench.distributed.job_lifecycle import JobLifecycleStore, LifecycleRedisProtocol
from crsbench.distributed.queue import (
    clear_experiment_jobs,
    get_existing_trial_jobs,
    has_potentially_live_started_jobs,
    resolve_queue_names,
    validate_queue_name_component,
)
from crsbench.distributed.registry import RegistryClient
from crsbench.utils.logger import get_logger

try:
    import rq

    RQ_AVAILABLE = True
except ImportError:
    rq = None  # type: ignore[assignment]
    RQ_AVAILABLE = False

logger = get_logger(__name__)

QUEUE_SCOPE_MAP = {
    "trial": 0,
    "build": 1,
    "verify": 2,
}


@dataclass(frozen=True)
class QueueCleanupResult:
    """Summary of experiment-scoped cleanup work."""

    queue_names: tuple[str, ...]
    removed_jobs: int
    matched_jobs: int
    removed_registry_entry: bool
    removed_lock: bool


def _normalize_scopes(scopes: Iterable[str]) -> tuple[str, ...]:
    normalized = []
    for scope in scopes:
        lowered = scope.strip().lower()
        if lowered not in QUEUE_SCOPE_MAP:
            raise ValueError(
                f"Unsupported queue scope {scope!r}. Allowed: trial, build, verify"
            )
        normalized.append(lowered)
    if not normalized:
        normalized = ["trial", "build", "verify"]
    return tuple(dict.fromkeys(normalized))


def resolve_scope_queue_names(
    experiment_name: str, scopes: Iterable[str]
) -> tuple[str, ...]:
    """Resolve queue names for selected scopes under current queue model."""
    validate_queue_name_component(experiment_name)
    trial_queue, build_queue, verify_queue = resolve_queue_names(experiment_name)
    names = (trial_queue, build_queue, verify_queue)
    selected = []
    for scope in _normalize_scopes(scopes):
        selected.append(names[QUEUE_SCOPE_MAP[scope]])
    return tuple(dict.fromkeys(selected))


def _normalize_rq_queue_name(name: object) -> str | None:
    if isinstance(name, bytes):
        return name.decode("utf-8", errors="ignore")
    if isinstance(name, str):
        return name
    return None


def resolve_experiment_queue_names_for_cleanup(
    redis_conn,
    *,
    experiment_name: str,
    scopes: Iterable[str],
) -> tuple[str, ...]:
    """Resolve all queue names that may hold experiment build/verify work."""
    normalized_scopes = _normalize_scopes(scopes)
    names = list(resolve_scope_queue_names(experiment_name, normalized_scopes))
    if not {"build", "verify"} & set(normalized_scopes):
        return tuple(names)

    queue_prefix = f"crsbench_{experiment_name}_"
    seen = set(names)
    queue_all = getattr(rq.Queue, "all", None)  # type: ignore[attr-defined]
    if not callable(queue_all):
        return tuple(names)

    for queue in queue_all(connection=redis_conn):
        queue_name = _normalize_rq_queue_name(getattr(queue, "name", None))
        if queue_name is None or queue_name in seen:
            continue
        if not queue_name.startswith(queue_prefix):
            continue
        if "build" in normalized_scopes and queue_name.endswith("_build"):
            names.append(queue_name)
            seen.add(queue_name)
            continue
        if "verify" in normalized_scopes and queue_name.endswith("_verify"):
            names.append(queue_name)
            seen.add(queue_name)
    return tuple(names)


def resolve_dispatcher_state_keys_for_cleanup(
    *, experiment_name: str, scopes: Iterable[str]
) -> tuple[str, ...]:
    """Resolve dispatcher Redis hashes that belong to one experiment."""
    validate_queue_name_component(experiment_name)
    normalized_scopes = _normalize_scopes(scopes)
    if not {"build", "verify"} <= set(normalized_scopes):
        return ()

    prefix = f"crsbench:dispatcher:{experiment_name}"
    return (
        f"{prefix}:build_requests",
        f"{prefix}:build_results",
        f"{prefix}:build_attempts",
        f"{prefix}:lineages",
        f"{prefix}:verify_requests",
        f"{prefix}:verify_results",
        f"{prefix}:verify_attempts",
        f"{prefix}:evaluators",
        f"{prefix}:lease",
    )


def clean_experiment_queues(
    redis_conn,
    *,
    experiment_name: str,
    scopes: Iterable[str] = ("trial", "build", "verify"),
    include_registry: bool = True,
    include_lock: bool = True,
    dry_run: bool = False,
) -> QueueCleanupResult:
    """Clean jobs for a single experiment from selected queues."""
    if not RQ_AVAILABLE:
        raise RuntimeError("Redis and RQ packages are required")

    normalized_scopes = _normalize_scopes(scopes)
    queue_names = resolve_experiment_queue_names_for_cleanup(
        redis_conn,
        experiment_name=experiment_name,
        scopes=normalized_scopes,
    )
    total_removed = 0
    total_matched = 0
    saw_started_trial_jobs = False

    for queue_name in queue_names:
        queue = rq.Queue(queue_name, connection=redis_conn)  # type: ignore[attr-defined]
        existing = get_existing_trial_jobs(queue, experiment_name=experiment_name)
        matched = sum(len(v) for v in existing.values())
        if queue_name == resolve_queue_names(experiment_name)[0] and (
            has_potentially_live_started_jobs(existing.get("started", []))
        ):
            saw_started_trial_jobs = True
        total_matched += matched
        if dry_run:
            continue
        total_removed += clear_experiment_jobs(queue, experiment_name)

    if not dry_run and "trial" in normalized_scopes:
        trial_queue_name, _, _ = resolve_queue_names(experiment_name)
        trial_queue = rq.Queue(trial_queue_name, connection=redis_conn)  # type: ignore[attr-defined]
        remaining_trial_jobs = get_existing_trial_jobs(
            trial_queue,
            experiment_name=experiment_name,
        )
        if not saw_started_trial_jobs and not any(remaining_trial_jobs.values()):
            try:
                lifecycle_connection = cast("LifecycleRedisProtocol", redis_conn)
                JobLifecycleStore(lifecycle_connection).clear_experiment(
                    experiment_name
                )
            except Exception as exc:
                logger.warning(
                    "Failed to clear lifecycle state for experiment={}: {}",
                    experiment_name,
                    exc,
                )

    if not dry_run:
        dispatcher_state_keys = resolve_dispatcher_state_keys_for_cleanup(
            experiment_name=experiment_name,
            scopes=normalized_scopes,
        )
        if dispatcher_state_keys:
            redis_conn.delete(*dispatcher_state_keys)

    removed_registry_entry = False
    removed_lock = False
    if not dry_run and include_registry:
        registry = RegistryClient(redis_conn)
        registry.deregister(experiment_name)
        removed_registry_entry = True
    if not dry_run and include_lock:
        lock_key = f"crsbench:lock:{experiment_name}"
        redis_conn.delete(lock_key)
        removed_lock = True

    return QueueCleanupResult(
        queue_names=queue_names,
        removed_jobs=total_removed,
        matched_jobs=total_matched,
        removed_registry_entry=removed_registry_entry,
        removed_lock=removed_lock,
    )
