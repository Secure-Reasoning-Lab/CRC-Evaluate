"""Shared distributed runtime session bootstrapping for CLI orchestrators."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from crsbench.distributed.patch_queue import initialize_patch_queues
from crsbench.distributed.queue import create_redis_connection, initialize_queue
from crsbench.distributed.registry import RegistryClient, RegistryLease
from crsbench.distributed.verify_queue import initialize_verify_queue

if TYPE_CHECKING:
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

    def cleanup(self) -> None:
        """Best-effort registry cleanup for resources owned by this session."""
        self.lease.cleanup()
