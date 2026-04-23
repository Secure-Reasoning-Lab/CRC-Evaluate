"""Owner-key and fair-selection helpers for evaluator scheduling."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

SCHEDULER_OWNER_KEY_META = "scheduler_owner_key"


def _normalize_component(value: object) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return text.replace("::", "_") if text else ""


def _build_non_trial_owner(
    *,
    experiment_name: str,
    components: list[object],
    fallback_job_id: str,
) -> str:
    normalized = [_normalize_component(component) for component in components]
    suffix_parts = [component for component in normalized if component]
    suffix = (
        "::".join(suffix_parts)
        if suffix_parts
        else _normalize_component(fallback_job_id)
    )
    return f"unit::{_normalize_component(experiment_name)}::{suffix}"


def build_scheduler_experiment_name_from_queue_name(queue_name: object) -> str:
    """Derive a stable experiment namespace from a routed queue name."""
    normalized = _normalize_component(queue_name) or "unknown-experiment"
    for suffix in ("_build", "_verify", "-build", "-verify"):
        if normalized.endswith(suffix):
            stripped = normalized[: -len(suffix)]
            if stripped:
                return stripped
    return normalized


def build_scheduler_owner_key_from_payload(
    payload: dict[str, object],
    *,
    fallback_job_id: str,
    queue_name: str,
) -> str:
    """Return the scheduler owner key for a serialized evaluator payload."""
    payload_experiment_name = _normalize_component(payload.get("experiment_name"))
    experiment_name = (
        payload_experiment_name
        or build_scheduler_experiment_name_from_queue_name(queue_name)
    )
    trial_id = payload.get("trial_id")
    if isinstance(trial_id, str) and trial_id.strip():
        return f"trial::{experiment_name}::{trial_id.strip()}"

    return _build_non_trial_owner(
        experiment_name=experiment_name,
        components=[
            payload.get("benchmark"),
            payload.get("benchmark_name"),
            payload.get("harness"),
            payload.get("cpv_id"),
            payload.get("sanitizer"),
            payload.get("job_type"),
        ],
        fallback_job_id=fallback_job_id,
    )


def build_scheduler_owner_key_for_ci_job(job: Any, *, experiment_name: str) -> str:
    """Return the scheduler owner key for a CI job object."""
    trial_id = getattr(job, "trial_id", None)
    if isinstance(trial_id, str) and trial_id.strip():
        return f"trial::{_normalize_component(experiment_name)}::{trial_id.strip()}"

    return _build_non_trial_owner(
        experiment_name=experiment_name,
        components=[
            getattr(job, "benchmark_name", None),
            getattr(job, "benchmark", None),
            getattr(job, "harness", None),
            getattr(job, "cpv_id", None),
            getattr(job, "sanitizer", None),
            getattr(job, "job_type", None),
        ],
        fallback_job_id=getattr(job, "job_id", ""),
    )


def should_adopt_scheduler_owner(
    *, existing_owner: str | None, new_owner: str | None
) -> bool:
    """Return whether a duplicate-reused job should adopt the new owner key."""
    if not new_owner:
        return False
    if not existing_owner:
        return True
    return existing_owner.startswith("unit::") and new_owner.startswith("trial::")


def adopt_scheduler_owner_if_needed(job: Any, *, new_owner: str | None) -> bool:
    """Update a reused job's owner metadata when the new owner is stronger."""
    existing_owner = None
    if isinstance(getattr(job, "meta", None), dict):
        existing_owner = job.meta.get(SCHEDULER_OWNER_KEY_META)
    if not should_adopt_scheduler_owner(
        existing_owner=existing_owner,
        new_owner=new_owner,
    ):
        return False
    if not isinstance(getattr(job, "meta", None), dict):
        job.meta = {}
    job.meta[SCHEDULER_OWNER_KEY_META] = new_owner
    job.save_meta()
    return True


@dataclass(frozen=True)
class QueuedCandidate:
    """Queued job candidate used for fair owner rotation."""

    queue_name: str
    job_id: str
    owner_key: str


@dataclass
class FairSchedulerState:
    """Mutable fair-selection state for the evaluator supervisor."""

    next_queue_class: str = "verify"
    last_owner_by_class: dict[str, str | None] = field(
        default_factory=lambda: {"build": None, "verify": None}
    )
    next_queue_index_by_class: dict[str, int] = field(
        default_factory=lambda: {"build": 0, "verify": 0}
    )


def clone_fair_scheduler_state(state: FairSchedulerState) -> FairSchedulerState:
    """Return a detached copy of mutable fair-scheduler state."""
    return FairSchedulerState(
        next_queue_class=state.next_queue_class,
        last_owner_by_class=dict(state.last_owner_by_class),
        next_queue_index_by_class=dict(state.next_queue_index_by_class),
    )


def restore_fair_scheduler_state(
    state: FairSchedulerState,
    snapshot: FairSchedulerState,
) -> None:
    """Restore mutable fair-scheduler state from a prior snapshot."""
    state.next_queue_class = snapshot.next_queue_class
    state.last_owner_by_class = dict(snapshot.last_owner_by_class)
    state.next_queue_index_by_class = dict(snapshot.next_queue_index_by_class)


def _interleave_candidates(
    queue_candidates: list[list[QueuedCandidate]],
    *,
    start_index: int,
) -> list[QueuedCandidate]:
    if not queue_candidates:
        return []

    merged: list[QueuedCandidate] = []
    queue_count = len(queue_candidates)
    max_depth = max(len(candidates) for candidates in queue_candidates)
    ordered_indexes = [
        (start_index + offset) % queue_count for offset in range(queue_count)
    ]
    for depth in range(max_depth):
        for queue_index in ordered_indexes:
            if depth < len(queue_candidates[queue_index]):
                merged.append(queue_candidates[queue_index][depth])
    return merged


def _next_owner(
    owner_order: list[str],
    *,
    last_owner: str | None,
) -> str:
    if not owner_order:
        raise ValueError("owner_order must not be empty")
    if last_owner not in owner_order or len(owner_order) == 1:
        return owner_order[0]
    owner_index = owner_order.index(last_owner)
    return owner_order[(owner_index + 1) % len(owner_order)]


def choose_next_fair_candidate(
    queue_candidates: list[list[QueuedCandidate]],
    *,
    queue_class: str,
    state: FairSchedulerState,
) -> QueuedCandidate | None:
    """Return the next queued candidate for a fair round-robin owner turn."""
    interleaved = _interleave_candidates(
        queue_candidates,
        start_index=state.next_queue_index_by_class.get(queue_class, 0),
    )
    if not interleaved:
        return None

    owner_order: list[str] = []
    for candidate in interleaved:
        if candidate.owner_key not in owner_order:
            owner_order.append(candidate.owner_key)

    next_owner = _next_owner(
        owner_order,
        last_owner=state.last_owner_by_class.get(queue_class),
    )
    chosen = next(
        candidate for candidate in interleaved if candidate.owner_key == next_owner
    )
    state.last_owner_by_class[queue_class] = next_owner
    chosen_queue_index = next(
        index
        for index, candidates in enumerate(queue_candidates)
        if chosen in candidates
    )
    state.next_queue_index_by_class[queue_class] = (chosen_queue_index + 1) % max(
        len(queue_candidates), 1
    )
    return chosen


def choose_next_queue_class(
    *,
    build_has_capacity: bool,
    verify_has_capacity: bool,
    build_has_work: bool,
    verify_has_work: bool,
    state: FairSchedulerState,
) -> str | None:
    """Return the next queue class to serve based on capacity and alternation."""
    build_ready = build_has_capacity and build_has_work
    verify_ready = verify_has_capacity and verify_has_work

    if build_ready and verify_ready:
        chosen = state.next_queue_class
        state.next_queue_class = "verify" if chosen == "build" else "build"
        return chosen
    if build_ready:
        return "build"
    if verify_ready:
        return "verify"
    return None
