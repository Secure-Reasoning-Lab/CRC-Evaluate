"""Shared distributed runtime session bootstrapping for CLI orchestrators."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional, cast

from crsbench.cloud.readiness import CloudReadinessStore, ReadinessRedisProtocol
from crsbench.distributed.job_lifecycle import (
    JobLifecycleStore,
    JobState,
    LifecycleRedisProtocol,
)
from crsbench.distributed.job_monitor import JobMonitorLoop
from crsbench.distributed.patch_queue import initialize_patch_queues
from crsbench.distributed.queue import create_redis_connection, initialize_queue
from crsbench.distributed.registry import RegistryClient, RegistryLease
from crsbench.distributed.verify_queue import initialize_verify_queue

if TYPE_CHECKING:
    from collections.abc import Callable

    import rq
    from redis import Redis

    from crsbench.distributed.registry import RuntimeRegistration


class LockContentionError(RuntimeError):
    """Raised when a distributed experiment lock cannot be acquired."""


@dataclass
class DistributedRuntimeSession:
    """Holds queue/registry state for distributed orchestration commands."""

    experiment_name: str
    redis_host: str
    redis_conn: "Redis"
    registry: RegistryClient
    lease: RegistryLease
    trial_queue: Optional["rq.Queue"] = None
    build_queue: Optional["rq.Queue"] = None
    verify_queue: Optional["rq.Queue"] = None
    cloud_readiness: Optional[CloudReadinessStore] = None
    lifecycle_store: Optional[JobLifecycleStore] = None
    _monitor: Optional[JobMonitorLoop] = field(default=None, repr=False)

    @classmethod
    def for_run(
        cls,
        *,
        redis_host: str,
        experiment_name: str,
    ) -> Optional["DistributedRuntimeSession"]:
        """Create session for `crsbench run` distributed trial queue usage."""
        trial_queue = initialize_queue(redis_host, experiment_name)
        if trial_queue is None:
            return None

        redis_conn = trial_queue.connection
        registry = RegistryClient(redis_conn)
        lease = RegistryLease(registry, experiment_name)
        return cls(
            experiment_name=experiment_name,
            redis_host=redis_host,
            redis_conn=redis_conn,
            registry=registry,
            lease=lease,
            trial_queue=trial_queue,
            cloud_readiness=CloudReadinessStore(
                cast("ReadinessRedisProtocol", redis_conn)
            ),
            lifecycle_store=JobLifecycleStore(
                cast("LifecycleRedisProtocol", redis_conn)
            ),
        )

    @classmethod
    def for_reeval(
        cls,
        *,
        redis_host: str,
        experiment_name: str,
    ) -> Optional["DistributedRuntimeSession"]:
        """Create session for `crsbench re-eval` async verify/patch queues."""
        verify_queue = initialize_verify_queue(redis_host, experiment_name)
        if verify_queue is None:
            return None

        build_queue, patch_verify_queue = initialize_patch_queues(
            redis_host, experiment_name
        )
        if build_queue is None or patch_verify_queue is None:
            return None

        redis_conn = create_redis_connection(redis_host)
        registry = RegistryClient(redis_conn)
        lease = RegistryLease(registry, experiment_name)
        return cls(
            experiment_name=experiment_name,
            redis_host=redis_host,
            redis_conn=redis_conn,
            registry=registry,
            lease=lease,
            build_queue=build_queue,
            verify_queue=patch_verify_queue,
            trial_queue=verify_queue,
            cloud_readiness=CloudReadinessStore(
                cast("ReadinessRedisProtocol", redis_conn)
            ),
        )

    def register_or_raise(self, registration: "RuntimeRegistration") -> None:
        """Acquire lock and publish registration; raise on lock contention."""
        if not self.lease.acquire_lock():
            raise LockContentionError(
                f"Experiment '{self.experiment_name}' is already locked."
            )
        self.lease.register(registration)

    def ensure_registered_if_missing(self, registration: "RuntimeRegistration") -> bool:
        """Register only when experiment is absent in registry.

        Returns True when this session published the registration.
        """
        if self.registry.get_experiment(self.experiment_name) is not None:
            return False
        self.register_or_raise(registration)
        return True

    def start_monitor(
        self,
        cloud_liveness_checker: "Callable[[str], bool]",
        artifact_checker: "Callable[[str], JobState | None]",
        scan_interval: float = 90.0,
    ) -> None:
        """Start the job monitor background thread.

        Args:
            cloud_liveness_checker: callable(instance_name) -> bool.
            artifact_checker: callable(trial_key) -> terminal lifecycle state or None.
            scan_interval: Seconds between scan cycles.
        """
        if self.lifecycle_store is None:
            raise RuntimeError(
                "lifecycle_store is not initialized — cannot start monitor"
            )
        monitor = JobMonitorLoop(
            lifecycle_store=self.lifecycle_store,
            experiment_name=self.experiment_name,
            connection=cast("LifecycleRedisProtocol", self.redis_conn),
            cloud_liveness_checker=cloud_liveness_checker,
            artifact_checker=artifact_checker,
            scan_interval=scan_interval,
        )
        monitor.start()
        self._monitor = monitor

    def stop_monitor(self) -> None:
        """Stop the job monitor background thread if running."""
        if self._monitor is not None:
            self._monitor.stop()
            self._monitor = None

    def resume_or_raise(
        self,
        *,
        artifact_checker: "Callable[[str], JobState | None] | None" = None,
        registration: "RuntimeRegistration | None" = None,
    ) -> list[str]:
        """Take over a stale experiment lock and resume monitoring after a restart.

        Calls try_resume_lock() and, if the monitor is already running,
        reconciles uncollected jobs.

        Returns:
            List of job_ids needing collection attention (from reconcile_on_resume).

        Raises:
            LockContentionError: If the lock is actively held and cannot be taken.
        """
        if not self.lease.try_resume_lock():
            raise LockContentionError(
                f"Experiment '{self.experiment_name}' lock is actively held — cannot resume."
            )
        existing_registration = self.registry.get_experiment(self.experiment_name)
        if existing_registration is None:
            if registration is not None:
                self.lease.register(registration)
        else:
            self.lease.registration_published = True
        needs_collection: list[str] = []
        if self.lifecycle_store is not None:
            monitor = self._monitor or JobMonitorLoop(
                lifecycle_store=self.lifecycle_store,
                experiment_name=self.experiment_name,
                connection=cast("LifecycleRedisProtocol", self.redis_conn),
                artifact_checker=artifact_checker,
            )
            needs_collection = monitor.reconcile_on_resume()
        return needs_collection

    def cleanup(self) -> None:
        """Best-effort registry cleanup for resources owned by this session."""
        self.stop_monitor()
        self.lease.cleanup()
