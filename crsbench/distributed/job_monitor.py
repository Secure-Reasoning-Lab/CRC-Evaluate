"""Background monitor loop for stale job detection, recovery, and resume reconciliation.

Implements QUEUE-02: detects heartbeat-expired jobs, double-checks cloud API liveness,
applies grace-period gating, performs artifact-safe recovery, and supports controller
restart with state reconciliation.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Callable

from crsbench.distributed.job_lifecycle import (
    JobLifecycleRecord,
    JobLifecycleStore,
    JobState,
    LifecycleRedisProtocol,
    log_recovery_event,
)
from crsbench.utils.logger import get_logger

logger = get_logger(__name__)

# Active states subject to stale-detection scanning
_ACTIVE_STATES = {JobState.CLAIMED, JobState.RUNNING, JobState.SYNCING}


ArtifactTerminalStateChecker = Callable[[str], JobState | bool | None]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(ts: str) -> datetime:
    """Parse an ISO UTC timestamp string, adding UTC tzinfo if absent."""
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


class JobMonitorLoop:
    """Background thread that detects stale cloud worker leases and performs recovery.

    Args:
        lifecycle_store: JobLifecycleStore instance for state access.
        experiment_name: Experiment identifier (used for all Redis keys).
        connection: Redis connection (for recovery event logging).
        heartbeat_timeout_seconds: Age in seconds at which a heartbeat is considered stale.
        scan_interval: Seconds between scan cycles.
        max_retries: Maximum retry attempts before permanent failure.
        cloud_liveness_checker: callable(instance_name: str) -> bool. Injected for testing.
        artifact_checker: callable(trial_key: str) -> terminal lifecycle state if
            artifacts prove the job already finished, else None.
    """

    def __init__(
        self,
        lifecycle_store: JobLifecycleStore,
        experiment_name: str,
        connection: LifecycleRedisProtocol,
        heartbeat_timeout_seconds: int = 180,
        scan_interval: float = 90.0,
        max_retries: int = 3,
        cloud_liveness_checker: Callable[[str], bool] | None = None,
        artifact_checker: ArtifactTerminalStateChecker | None = None,
    ) -> None:
        self._store = lifecycle_store
        self._experiment = experiment_name
        self._conn = connection
        self._heartbeat_timeout = heartbeat_timeout_seconds
        self._scan_interval = scan_interval
        self._max_retries = max_retries
        self._cloud_liveness = cloud_liveness_checker or (lambda _: True)
        self._artifact_checker = artifact_checker or (lambda _: None)

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        # grace tracker: job_id -> number of consecutive stale scan hits
        self._grace_tracker: dict[str, int] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the background monitor thread."""
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        logger.info("JobMonitorLoop started for experiment '{}'", self._experiment)

    def stop(self, timeout: float = 10.0) -> None:
        """Signal the monitor thread to stop and wait for it to exit."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            logger.info("JobMonitorLoop stopped for experiment '{}'", self._experiment)

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def _loop(self) -> None:
        """Background loop: wait scan_interval, then run a scan cycle."""
        while not self._stop.wait(self._scan_interval):
            try:
                self._scan_and_recover()
            except Exception:
                logger.exception(
                    "Unhandled error in monitor scan for experiment '{}'",
                    self._experiment,
                )

    # ------------------------------------------------------------------
    # Scan and recover
    # ------------------------------------------------------------------

    def _scan_and_recover(self) -> None:
        """Scan all active jobs and apply recovery logic for stale ones."""
        jobs = self._store.list_jobs(self._experiment)
        now = _utc_now()

        for record in jobs:
            if record.state not in _ACTIVE_STATES:
                continue

            heartbeat_ts = self._store.get_heartbeat(self._experiment, record.job_id)
            if heartbeat_ts is not None:
                last_hb = _parse_ts(heartbeat_ts)
                age_seconds = (now - last_hb).total_seconds()
                if age_seconds <= self._heartbeat_timeout:
                    # Healthy: clear any grace count and skip
                    self._grace_tracker.pop(record.job_id, None)
                    continue

            # Heartbeat is stale or missing — check cloud liveness
            instance_name = record.claimed_by or ""
            if instance_name and self._cloud_liveness(instance_name):
                # Instance is alive (heartbeat lag or network glitch)
                self._grace_tracker.pop(record.job_id, None)
                continue

            # Instance appears dead: increment grace counter
            count = self._grace_tracker.get(record.job_id, 0) + 1
            self._grace_tracker[record.job_id] = count
            logger.debug("Job '{}' stale (grace count={})", record.job_id, count)

            if count < 2:
                # Grace period: flag but do not act yet
                continue

            # Grace period exhausted: perform recovery
            self._grace_tracker.pop(record.job_id, None)
            self._recover_orphaned_job(record)

    # ------------------------------------------------------------------
    # Recovery
    # ------------------------------------------------------------------

    def _recover_orphaned_job(self, record: JobLifecycleRecord) -> None:
        """Transition a job through orphan recovery based on artifact state and retry count."""
        job_id = record.job_id
        trial_key = record.trial_key

        logger.info(
            "Recovering orphaned job '{}' (trial={}, retries={})",
            job_id,
            trial_key,
            record.retry_count,
        )

        # Transition to ORPHANED
        self._store.transition(self._experiment, job_id, JobState.ORPHANED)
        log_recovery_event(
            self._conn,
            self._experiment,
            {"event": "orphan_detected", "job_id": job_id, "trial_key": trial_key},
        )

        # Check if artifacts already published and indicate a terminal state.
        terminal_state = self._resolve_terminal_artifact_state(
            self._artifact_checker(trial_key)
        )
        if terminal_state is not None:
            self._store.transition(self._experiment, job_id, terminal_state)
            event_name = (
                "completed_from_artifact"
                if terminal_state is JobState.COMPLETED
                else "failed_from_artifact"
            )
            log_recovery_event(
                self._conn,
                self._experiment,
                {
                    "event": event_name,
                    "job_id": job_id,
                    "state": terminal_state.value,
                },
            )
            logger.info(
                "Job '{}' marked {} from published artifacts — no requeue needed",
                job_id,
                terminal_state.value,
            )
            return

        # Refresh record to get latest retry_count after transition
        refreshed = self._store.get(self._experiment, job_id)
        retry_count = (
            refreshed.retry_count if refreshed is not None else record.retry_count
        )

        if retry_count >= self._max_retries:
            detail = "permanently failed: max retries exceeded"
            self._store.transition(
                self._experiment, job_id, JobState.FAILED, detail=detail
            )
            log_recovery_event(
                self._conn,
                self._experiment,
                {
                    "event": "permanently_failed",
                    "job_id": job_id,
                    "retry_count": retry_count,
                },
            )
            logger.warning(
                "Job '{}' permanently failed after {} retries", job_id, retry_count
            )
            return

        # Requeue the concrete RQ job first, then mark shadow state executable again.
        try:
            self._requeue_rq_job(job_id)
        except Exception as exc:
            detail = f"requeue failed after orphan recovery: {exc}"
            self._store.transition(
                self._experiment, job_id, JobState.FAILED, detail=detail
            )
            log_recovery_event(
                self._conn,
                self._experiment,
                {
                    "event": "requeue_failed",
                    "job_id": job_id,
                    "detail": str(exc),
                },
            )
            logger.warning("Failed to requeue orphaned job '%s': %s", job_id, exc)
            return

        self._store.increment_retry(self._experiment, job_id)
        self._store.transition(self._experiment, job_id, JobState.QUEUED)
        log_recovery_event(
            self._conn,
            self._experiment,
            {"event": "requeued", "job_id": job_id, "trial_key": trial_key},
        )
        logger.info(
            "Job '{}' requeued (retry {}/{})",
            job_id,
            retry_count + 1,
            self._max_retries,
        )

    def _requeue_rq_job(self, job_id: str) -> None:
        """Move a recovered orphaned job back into its concrete RQ queue."""
        import rq
        from rq.job import JobStatus

        job = rq.job.Job.fetch(job_id, connection=self._conn)  # type: ignore[attr-defined]
        if not job.origin:
            raise RuntimeError(f"job {job_id!r} is missing origin queue metadata")

        queue = rq.Queue(job.origin, connection=self._conn)  # type: ignore[attr-defined]
        job.set_status(JobStatus.FAILED)
        queue.enqueue_job(job)

    def _resolve_terminal_artifact_state(
        self, artifact_result: JobState | bool | None
    ) -> JobState | None:
        """Normalize artifact checker outputs for backward-compatible callers."""
        if artifact_result is True:
            return JobState.COMPLETED
        if artifact_result in {False, None}:
            return None
        return artifact_result

    # ------------------------------------------------------------------
    # Resume reconciliation
    # ------------------------------------------------------------------

    def reconcile_on_resume(self) -> list[str]:
        """Identify jobs needing collection attention after a controller restart.

        Returns:
            List of job_ids in SYNCING state that need collection follow-up.
        """
        jobs = self._store.list_jobs(self._experiment)
        needs_collection: list[str] = []

        for record in jobs:
            if record.state is JobState.SYNCING:
                needs_collection.append(record.job_id)
                log_recovery_event(
                    self._conn,
                    self._experiment,
                    {
                        "event": "resume_reconcile",
                        "job_id": record.job_id,
                        "state": record.state.value,
                    },
                )
                logger.info(
                    "Resume: job '{}' in {} state needs collection",
                    record.job_id,
                    record.state.value,
                )

        return needs_collection
