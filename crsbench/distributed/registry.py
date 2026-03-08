"""Redis experiment registry for configless worker/evaluator discovery.

When ``crsbench run`` starts a distributed experiment it publishes a
``RuntimeRegistration`` to a shared Redis hash.  Workers and evaluators
that boot *without* an ``--experiment-config`` file read this registry
to discover which queues to listen on and what resources to allocate.

Key Redis structures:
    ``crsbench:registry:experiments``  – HASH: experiment_name → JSON
    ``crsbench:registry:events``       – PUB/SUB channel for live updates
    ``crsbench:lock:{name}``           – STRING with NX+EX: distributed experiment lock
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Generator, Optional, cast

from pydantic import BaseModel, ConfigDict

from crsbench.distributed.queue import resolve_queue_names
from crsbench.utils.logger import get_logger

if TYPE_CHECKING:
    from redis import Redis

logger = get_logger(__name__)

REGISTRY_KEY = "crsbench:registry:experiments"
EVENTS_CHANNEL = "crsbench:registry:events"
LOCK_KEY_PREFIX = "crsbench:lock:"
LOCK_TTL = 600  # 10 minutes; renewed by the monitor loop every 60s


class RuntimeRegistration(BaseModel):
    """Metadata published by the orchestrator for a single experiment."""

    model_config = ConfigDict(extra="ignore")

    # Schema versioning for forward compatibility
    version: int = 1

    # Experiment identity
    experiment: str

    # Queue names (fully qualified)
    trial_queue: str = ""
    build_queue: str = ""
    verify_queue: str = ""

    # Resource admission
    cores_per_trial: int = 4
    memory_per_trial: Optional[str] = None
    cpu_tag: Optional[str] = None
    worker_jobs: Optional[int] = None
    worker_cores_per_job: Optional[int] = None
    worker_cores: Optional[str] = None
    worker_skip_cpus: Optional[str] = None
    worker_cpu_tag: Optional[str] = None
    evaluator_build_jobs: Optional[int] = None
    evaluator_build_cores_per_job: Optional[int] = None
    evaluator_verify_jobs: Optional[int] = None
    evaluator_verify_cores_per_job: Optional[int] = None
    evaluator_cores: Optional[str] = None
    evaluator_skip_cpus: Optional[str] = None
    evaluator_idle_timeout: Optional[int] = None
    evaluator_cpu_tag: Optional[str] = None

    # Evaluator pre-build hints
    benchmarks: list[str] = []
    sanitizers: list[str] = []
    modes: list[str] = []

    # Paths (workers verify these exist locally)
    benchmarks_root: str = "benchmarks"
    source_mode: str = "pkgs"
    inc_image_policy: str = "auto"
    inc_image_registry: str = "ghcr.io/team-atlanta/crsbench"
    inc_image_max_pull_bytes: Optional[int] = 10 * 1024 * 1024 * 1024
    inc_image_pull_timeout_sec: int = 300
    local_image_prefix: str = "crsbench"

    # Timeouts
    max_total_time: int = 7200
    build_timeout: int = 3600
    per_pov_verify_timeout: int = 180

    # Provenance
    registered_at: str = ""
    config_hash: str = ""

    @classmethod
    def from_experiment_config(cls, config) -> RuntimeRegistration:
        """Build a registration from an ``ExperimentConfig`` instance.

        Args:
            config: An ``ExperimentConfig`` (from crsbench.validation.schemas).

        Returns:
            Populated ``RuntimeRegistration`` ready to publish.
        """
        experiment = config.experiment

        # Compute stable hash of the serialized config
        config_bytes = json.dumps(
            config.model_dump(), sort_keys=True, default=str
        ).encode()
        config_hash = hashlib.sha256(config_bytes).hexdigest()

        benchmarks = config.get_benchmark_list()
        sanitizers = [s.value for s in config.sanitizers]
        modes = [config.mode.value]

        resources = getattr(config, "resources", None)
        cores_per_trial = resources.cores_per_trial if resources else 4
        memory_per_trial = resources.memory_per_trial if resources else None
        cpu_tag = getattr(resources, "cpu_tag", None) if resources else None
        if cpu_tag is not None and not isinstance(cpu_tag, str):
            cpu_tag = None

        # Keep fallback behavior for tests/mocks: treat non-BaseModel dynamic attrs
        # as absent instead of trying to serialize MagicMock values.
        worker_cfg = getattr(config, "worker", None)
        if not isinstance(worker_cfg, BaseModel):
            worker_cfg = None
        evaluator_cfg = getattr(config, "evaluator", None)
        if not isinstance(evaluator_cfg, BaseModel):
            evaluator_cfg = None

        worker_jobs = worker_cfg.jobs if worker_cfg else None
        worker_cores_per_job = (
            worker_cfg.cores_per_job
            if worker_cfg and worker_cfg.cores_per_job
            else cores_per_trial
        )

        trial_queue, build_queue, verify_queue = resolve_queue_names(experiment)

        inc_image_policy = getattr(config, "inc_image_policy", "auto")
        if not isinstance(inc_image_policy, str) or not inc_image_policy:
            inc_image_policy = "auto"

        inc_image_registry = getattr(
            config, "inc_image_registry", "ghcr.io/team-atlanta/crsbench"
        )
        if not isinstance(inc_image_registry, str) or not inc_image_registry:
            inc_image_registry = "ghcr.io/team-atlanta/crsbench"

        inc_image_max_pull_bytes = getattr(
            config, "inc_image_max_pull_bytes", 10 * 1024 * 1024 * 1024
        )
        if not isinstance(inc_image_max_pull_bytes, int):
            inc_image_max_pull_bytes = 10 * 1024 * 1024 * 1024

        inc_image_pull_timeout_sec = getattr(config, "inc_image_pull_timeout_sec", 300)
        if (
            not isinstance(inc_image_pull_timeout_sec, int)
            or inc_image_pull_timeout_sec <= 0
        ):
            inc_image_pull_timeout_sec = 300

        local_image_prefix = getattr(config, "project_image_prefix", "crsbench")
        if not isinstance(local_image_prefix, str) or not local_image_prefix:
            local_image_prefix = "crsbench"

        return cls(
            experiment=experiment,
            trial_queue=trial_queue,
            build_queue=build_queue,
            verify_queue=verify_queue,
            cores_per_trial=cores_per_trial,
            memory_per_trial=memory_per_trial,
            cpu_tag=cpu_tag,
            worker_jobs=worker_jobs,
            worker_cores_per_job=worker_cores_per_job,
            worker_cores=worker_cfg.cores if worker_cfg else None,
            worker_skip_cpus=worker_cfg.skip_cpus if worker_cfg else None,
            worker_cpu_tag=worker_cfg.cpu_tag if worker_cfg else None,
            evaluator_build_jobs=evaluator_cfg.build_jobs if evaluator_cfg else None,
            evaluator_build_cores_per_job=(
                evaluator_cfg.build_cores_per_job if evaluator_cfg else None
            ),
            evaluator_verify_jobs=evaluator_cfg.verify_jobs if evaluator_cfg else None,
            evaluator_verify_cores_per_job=(
                evaluator_cfg.verify_cores_per_job if evaluator_cfg else None
            ),
            evaluator_cores=evaluator_cfg.cores if evaluator_cfg else None,
            evaluator_skip_cpus=evaluator_cfg.skip_cpus if evaluator_cfg else None,
            evaluator_idle_timeout=evaluator_cfg.idle_timeout
            if evaluator_cfg
            else None,
            evaluator_cpu_tag=evaluator_cfg.cpu_tag if evaluator_cfg else None,
            benchmarks=benchmarks,
            sanitizers=sanitizers,
            modes=modes,
            benchmarks_root=str(config.benchmarks_root),
            source_mode=config.source_mode,
            inc_image_policy=inc_image_policy,
            inc_image_registry=inc_image_registry,
            inc_image_max_pull_bytes=inc_image_max_pull_bytes,
            inc_image_pull_timeout_sec=inc_image_pull_timeout_sec,
            local_image_prefix=local_image_prefix,
            max_total_time=config.max_total_time,
            build_timeout=config.build_timeout,
            per_pov_verify_timeout=config.per_pov_verify_timeout,
            registered_at=datetime.now(timezone.utc).isoformat(),
            config_hash=config_hash,
        )


class RegistryClient:
    """Thin wrapper around Redis for experiment registration CRUD."""

    def __init__(self, connection: Redis) -> None:
        self._conn = connection

    # ---- locking ----

    def lock(self, experiment: str) -> bool:
        """Acquire a distributed lock for *experiment*.

        Uses ``SET NX EX`` so the operation is atomic.  The TTL acts as a
        crash-recovery safety net and is renewed periodically by the
        monitor loop via :meth:`renew`.

        Returns:
            ``True`` if the lock was acquired, ``False`` if already held.
        """
        key = f"{LOCK_KEY_PREFIX}{experiment}"
        acquired = self._conn.set(key, "locked", nx=True, ex=LOCK_TTL)
        if acquired:
            logger.info(f"Acquired lock for experiment '{experiment}'")
        return bool(acquired)

    def renew(self, experiment: str) -> bool:
        """Extend the lock TTL.  Called periodically by the monitor loop.

        Returns:
            ``True`` if the key exists and TTL was extended,
            ``False`` if the key no longer exists (lock lost).
        """
        key = f"{LOCK_KEY_PREFIX}{experiment}"
        result = self._conn.expire(key, LOCK_TTL)
        if not result:
            logger.warning(
                f"Lock renewal failed for '{experiment}' — lock no longer exists"
            )
        return bool(result)

    def unlock(self, experiment: str) -> None:
        """Release the distributed lock for *experiment*."""
        key = f"{LOCK_KEY_PREFIX}{experiment}"
        self._conn.delete(key)
        logger.info(f"Released lock for experiment '{experiment}'")

    # ---- write operations ----

    def register(self, registration: RuntimeRegistration) -> None:
        """Publish a registration to the registry hash and event channel."""
        payload = registration.model_dump_json()
        self._conn.hset(REGISTRY_KEY, registration.experiment, payload)
        self._conn.publish(
            EVENTS_CHANNEL,
            json.dumps({"event": "register", "experiment": registration.experiment}),
        )
        logger.info(f"Registered experiment '{registration.experiment}' in registry")

    def deregister(self, experiment: str) -> None:
        """Remove an experiment from the registry."""
        self._conn.hdel(REGISTRY_KEY, experiment)
        self._conn.publish(
            EVENTS_CHANNEL,
            json.dumps({"event": "deregister", "experiment": experiment}),
        )
        logger.info(f"Deregistered experiment '{experiment}' from registry")

    # ---- read operations ----

    def list_experiments(self) -> dict[str, RuntimeRegistration]:
        """Return all registered experiments."""
        raw_result = self._conn.hgetall(REGISTRY_KEY)
        if isinstance(raw_result, Awaitable):
            raise RuntimeError(
                "Registry Redis client returned awaitable from hgetall(); "
                "expected synchronous Redis client."
            )
        raw = cast("dict[bytes | str, bytes | str]", raw_result)
        results: dict[str, RuntimeRegistration] = {}
        for name, payload in raw.items():
            key = name.decode() if isinstance(name, bytes) else name
            val = payload.decode() if isinstance(payload, bytes) else payload
            try:
                results[key] = RuntimeRegistration.model_validate_json(val)
            except Exception:
                logger.warning(f"Skipping corrupt registry entry for '{key}'")
        return results

    def get_experiment(self, name: str) -> RuntimeRegistration | None:
        """Fetch a single experiment registration (or ``None``)."""
        raw_result = self._conn.hget(REGISTRY_KEY, name)
        if isinstance(raw_result, Awaitable):
            raise RuntimeError(
                "Registry Redis client returned awaitable from hget(); "
                "expected synchronous Redis client."
            )
        raw = cast("bytes | str | None", raw_result)
        if raw is None:
            return None
        val = raw.decode() if isinstance(raw, bytes) else raw
        try:
            return RuntimeRegistration.model_validate_json(val)
        except Exception:
            logger.warning(f"Corrupt registry entry for '{name}'")
            return None

    # ---- live events ----

    def subscribe(self) -> Generator[tuple[str, str], None, None]:
        """Yield ``(event_type, experiment_name)`` from the event channel.

        This is a blocking generator.  Callers typically run it in a
        background thread.
        """
        pubsub = self._conn.pubsub()
        pubsub.subscribe(EVENTS_CHANNEL)
        try:
            for message in pubsub.listen():
                if message["type"] != "message":
                    continue
                data = message["data"]
                text = data.decode() if isinstance(data, bytes) else data
                try:
                    parsed = json.loads(text)
                    yield parsed["event"], parsed["experiment"]
                except (json.JSONDecodeError, KeyError) as e:
                    logger.warning(
                        f"Ignoring malformed registry event payload: {e} payload={text!r}"
                    )
                    continue
        finally:
            try:
                pubsub.unsubscribe(EVENTS_CHANNEL)
                pubsub.close()
            except Exception:
                pass


@dataclass
class RegistryLease:
    """Manages registry lock + registration lifecycle for one experiment."""

    client: RegistryClient
    experiment: str
    lock_acquired: bool = False
    registration_published: bool = False

    def acquire_lock(self) -> bool:
        """Acquire experiment lock once."""
        if self.lock_acquired:
            return True
        self.lock_acquired = self.client.lock(self.experiment)
        return self.lock_acquired

    def register(self, registration: RuntimeRegistration) -> None:
        """Publish registration and track cleanup responsibility."""
        if registration.experiment != self.experiment:
            raise ValueError(
                "Registration experiment mismatch: "
                f"expected '{self.experiment}', got '{registration.experiment}'"
            )
        self.client.register(registration)
        self.registration_published = True

    def cleanup(self) -> None:
        """Best-effort cleanup of published registration and lock."""
        if self.registration_published:
            try:
                self.client.deregister(self.experiment)
            except Exception as exc:
                logger.warning(f"Failed to deregister experiment: {exc}")
            else:
                self.registration_published = False
        if self.lock_acquired:
            try:
                self.client.unlock(self.experiment)
            except Exception as exc:
                logger.warning(f"Failed to unlock experiment: {exc}")
            else:
                self.lock_acquired = False
