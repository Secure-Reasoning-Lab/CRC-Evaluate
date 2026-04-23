"""POV verification manager for real-time POV verification during CRS evaluation.

This module provides POVVerificationManager, which monitors POV output during
CRS evaluation, verifies discovered POVs against ground truth CPVs, and
supports early termination when all CPVs for a harness are found.

Threading Model:
- Main thread: Runs CRS subprocess
- Manager: Event-based POV verification via on_snapshot callback

Integration:
- Works with SnapshotManager for synchronized POV verification snapshots
- Uses VerificationEngine for actual POV verification
- Stores POV data using POVStore

Note: Follows CoverageManager pattern - state is tracked directly in manager
and store, with no separate state class.
"""

import json
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from crsbench.validation.meta_adapter import MetaYamlAdapter

from crsbench.evaluation.verification.models import (
    PovVerificationRequest,
    PovVerificationResult,
    PovVerificationStatus,
)
from crsbench.evaluation.verification.pov.config import POVVerificationConfig
from crsbench.evaluation.verification.pov.engine import VerificationEngine
from crsbench.evaluation.verification.pov.models import (
    POVSnapshot,
    POVVerificationReport,
)
from crsbench.evaluation.verification.pov.store import POVStore
from crsbench.utils.logger import get_logger

logger = get_logger(__name__)


class POVVerificationManager:
    """Manages real-time POV verification during CRS evaluation.

    This class monitors POV output directories, verifies discovered POVs
    against ground truth CPVs, and supports early termination when all
    CPVs for a harness are found.

    Follows CoverageManager pattern: state is tracked directly in manager
    and store, with no separate state class.

    Attributes:
        trial_dir: Trial output directory containing CRS outputs
        pov_output_dir: Directory where CRS writes discovered POVs
        config: POV verification configuration
        harness_name: Name of the harness being evaluated
        benchmark_id: Benchmark identifier
        store: POV store for persistence
        expected_cpv_ids: Set of expected CPV IDs for this harness (e.g., {"cpv_0", "cpv_1"})
    """

    def __init__(
        self,
        trial_dir: Path,
        pov_output_dir: Path,
        config: POVVerificationConfig,
        harness_name: str,
        benchmark_id: str,
        expected_cpv_ids: list[str],
        *,
        trial_start_time: Optional[float] = None,
        store: Optional[POVStore] = None,
        engine: Optional[VerificationEngine] = None,
        stop_event: Optional[threading.Event] = None,
        adapter: Optional["MetaYamlAdapter"] = None,
        redis_host: Optional[str] = None,
        experiment_name: Optional[str] = None,
        trial_id: Optional[str] = None,
        exchange_pov_dir: Optional[Path] = None,
        sanitizer: Optional[str] = None,
    ):
        """Initialize POV verification manager.

        Args:
            trial_dir: Trial output directory (must exist)
            pov_output_dir: Directory where CRS writes POVs
            config: POV verification configuration
            harness_name: Name of the harness being evaluated
            benchmark_id: Benchmark identifier
            expected_cpv_ids: List of expected CPV IDs for this harness (e.g., ["cpv_0", "cpv_1"])
            trial_start_time: Trial start timestamp (defaults to current time)
            store: Optional POVStore for persistence (creates new if None)
            engine: Optional VerificationEngine (creates new if None)
            stop_event: Optional threading.Event for signaling early stop
            adapter: Optional MetaYamlAdapter for verification engine
            redis_host: Redis server hostname for async mode (None = inline mode)
            experiment_name: Experiment name for async verify queue naming
            trial_id: Trial identifier for async result correlation
            exchange_pov_dir: Pre-resolved EXCHANGE_DIR pov path for real-time scanning
            sanitizer: Trial sanitizer to scope async verification builds

        Raises:
            ValueError: If trial_dir doesn't exist
        """
        if not trial_dir.exists():
            raise ValueError(f"trial_dir does not exist: {trial_dir}")

        self.trial_dir = trial_dir
        self.pov_output_dir = pov_output_dir
        self.config = config
        self.harness_name = harness_name
        self.benchmark_id = benchmark_id
        self.expected_cpv_ids = set(expected_cpv_ids)
        self.trial_start_time = trial_start_time or time.time()
        self._adapter = adapter

        # POV store for persistence (pass trial_start_time as crs_run_start_time)
        pov_store_dir = trial_dir / "povs"
        self.store = store or POVStore(
            pov_store_dir, crs_run_start_time=self.trial_start_time
        )

        # Verification engine (lazy initialization)
        self._engine = engine

        # Stop event for signaling early termination
        self._stop_event = stop_event

        # Thread-safe access to state
        self._lock = threading.Lock()

        # Snapshot tracking: only keep count and latest (written to disk)
        self._snapshot_count = 0
        self._latest_snapshot: Optional[POVSnapshot] = None

        # Counter tracking (derived from store on demand for stats)
        self._duplicates_count = 0
        self._errors_count = 0
        self._unintended_crashes_count = 0

        # Early stop state
        self._early_stop_triggered = False
        self._early_stop_time: Optional[datetime] = None

        # Async verification via Redis (VU-02/03/04)
        self._redis_host = redis_host
        self._experiment_name = experiment_name
        self._trial_id = trial_id
        self._sanitizer = sanitizer
        self._verify_queue: Optional[object] = None  # Lazy-initialized RQ Queue
        self._build_queue: Optional[object] = None  # Lazy-initialized RQ Queue
        self._pending_job_ids: list[str] = []  # Job IDs awaiting results
        self._pov_hash_to_path: dict[str, Path] = {}  # hash → local file path
        self._job_to_pov_id: dict[str, str] = {}  # job_id → pov_id for timeout marking
        self._async_build_job_ids: list[str] = []
        self._async_build_dependencies: list[object] = []
        self._async_build_sanitizer: Optional[str] = None

        # EXCHANGE_DIR scanning for real-time POV discovery during CRS execution
        self._exchange_pov_dir = exchange_pov_dir

        async_mode = "async (Redis)" if redis_host else "inline"
        logger.info(
            f"POVVerificationManager initialized: trial_dir={trial_dir}, "
            f"harness={harness_name}, benchmark={benchmark_id}, "
            f"expected_cpvs={len(expected_cpv_ids)}, mode={async_mode}"
        )

    @property
    def found_cpvs(self) -> set[str]:
        """Get CPVs found so far (derived from store)."""
        return self.store.get_cpvs_found()

    @property
    def total_expected_cpvs(self) -> int:
        """Get total number of expected CPVs (for backward compatibility)."""
        return len(self.expected_cpv_ids)

    @property
    def all_cpvs_found(self) -> bool:
        """Check if all expected CPVs have been found."""
        return self.expected_cpv_ids <= self.found_cpvs

    def set_crs_run_start_time(self, start_time: float) -> None:
        """Update CRS run start time (called after build completes).

        Updates the POV store's crs_run_start_time so that relative_time
        calculations exclude build time. Does NOT update self.trial_start_time
        since that is used for elapsed_time in POVSnapshot (which should
        include total time since trial started).

        Args:
            start_time: Unix timestamp when CRS run actually started
        """
        self.store.set_crs_run_start_time(start_time)

    def _get_remaining_cpvs(self) -> list[str]:
        """Get list of CPV IDs not yet found.

        Returns:
            List of CPV identifiers that haven't been discovered yet, sorted.
        """
        return sorted(self.expected_cpv_ids - self.found_cpvs)

    @property
    def _async_mode(self) -> bool:
        """Whether async verification via Redis is enabled."""
        return self._redis_host is not None

    def _get_verify_queue(self) -> Optional[object]:
        """Get or create the Redis verify queue (lazy initialization)."""
        if self._verify_queue is not None:
            return self._verify_queue

        if not self._redis_host or not self._experiment_name:
            return None

        from crsbench.distributed.verify_queue import initialize_verify_queue

        self._verify_queue = initialize_verify_queue(
            self._redis_host, self._experiment_name
        )
        return self._verify_queue

    def _get_build_queue(self) -> Optional[object]:
        """Get or create the Redis build queue (lazy initialization)."""
        if self._build_queue is not None:
            return self._build_queue

        if not self._redis_host or not self._experiment_name:
            return None

        from crsbench.distributed.verify_queue import initialize_build_queue

        self._build_queue = initialize_build_queue(
            self._redis_host, self._experiment_name
        )
        return self._build_queue

    def _ensure_async_build_jobs(self) -> Optional[tuple[list[str], list[object]]]:
        """Ensure async POV verification has an explicit build DAG."""
        if self._async_build_job_ids:
            return self._async_build_job_ids, self._async_build_dependencies

        if self._engine is None:
            logger.warning("VerificationEngine not initialized, cannot enqueue builds")
            return None
        if self._adapter is None or self._adapter.benchmark_path is None:
            logger.warning(
                "MetaYamlAdapter missing benchmark path, cannot enqueue builds"
            )
            return None

        try:
            from crsbench.benchmark_ci.jobs.flat import (
                BuildSingleVariantJob,
                PrepareIncImageJob,
            )
            from crsbench.distributed.ci_jobs import serialize_ci_job
            from crsbench.distributed.queue import (
                ROUTING_MODEL_DISPATCHER,
                get_evaluator_routing_model,
            )
            from crsbench.distributed.verify_queue import (
                build_variant_rq_job_id,
                enqueue_ci_job,
                submit_async_build_requests,
            )

            adapter = self._adapter
            engine = self._engine
            benchmark_path = adapter.benchmark_path
            assert benchmark_path is not None
            source_mode = engine.builder.source_mode
            use_inc_build = bool(adapter.inc_build)
            sanitizer = self._sanitizer
            if sanitizer is None:
                sanitizers = adapter.get_all_cpv_sanitizers()
                sanitizer = sanitizers[0] if sanitizers else "address"

            plan = engine.builder.create_build_plan(
                benchmark_name=adapter.benchmark_name,
                benchmark_path=benchmark_path,
                main_repo=adapter.main_repo,
                mode=adapter.get_mode(),
                base_commit=adapter.get_base_commit(),
                ref_commit=adapter.get_ref_commit(),
                cpv_numbers=adapter.get_cpv_numbers(),
                language=adapter.lang,
                repo_name=adapter.repo_name,
                include_coverage=False,
                use_inc_build=use_inc_build,
                sanitizer=sanitizer,
            )

            if get_evaluator_routing_model() == ROUTING_MODEL_DISPATCHER:
                build_payloads: list[dict[str, object]] = []
                trial_id = self._trial_id or ""
                prepare_request_id = ""
                if use_inc_build:
                    prepare_job = PrepareIncImageJob(
                        benchmark_path=benchmark_path,
                        benchmark_name=adapter.benchmark_name,
                        sanitizer=sanitizer,
                        use_inc_build=True,
                        source_mode=source_mode,
                        inc_image_policy=engine.builder.infra.inc_image_policy,
                        inc_image_registry=engine.builder.infra.inc_image_registry,
                        inc_image_max_pull_bytes=engine.builder.infra.inc_image_max_pull_bytes,
                        inc_image_pull_timeout=engine.builder.infra.inc_image_pull_timeout,
                        local_image_prefix=engine.builder.infra.local_image_prefix,
                    )
                    build_payloads.append(serialize_ci_job(prepare_job))
                    prepare_request_id = f"build:{trial_id}:{adapter.benchmark_name}:0"

                for config in plan.configs:
                    build_job = BuildSingleVariantJob(
                        benchmark_path=config.benchmark_path,
                        benchmark_name=config.benchmark_name,
                        variant_type=config.variant_type,
                        commit=config.commit,
                        main_repo=config.main_repo,
                        mode=config.mode or adapter.get_mode(),
                        language=config.language,
                        cpv_num=config.cpv_num,
                        patch_id=config.patch_id,
                        pov_id=config.pov_id,
                        patches=config.patches,
                        use_inc_build=config.use_inc_build,
                        source_mode=source_mode,
                        sanitizer=config.sanitizer,
                        repo_name=config.repo_name,
                        prepare_inc_job_id=prepare_request_id,
                        inc_image_policy=engine.builder.infra.inc_image_policy,
                        inc_image_registry=engine.builder.infra.inc_image_registry,
                        inc_image_max_pull_bytes=engine.builder.infra.inc_image_max_pull_bytes,
                        inc_image_pull_timeout=engine.builder.infra.inc_image_pull_timeout,
                        local_image_prefix=engine.builder.infra.local_image_prefix,
                    )
                    build_payloads.append(serialize_ci_job(build_job))

                request_ids = submit_async_build_requests(
                    redis_host=self._redis_host,
                    experiment_name=self._experiment_name or "",
                    trial_id=trial_id,
                    benchmark=adapter.benchmark_name,
                    build_payloads=build_payloads,
                    sanitizer=sanitizer,
                    source_mode=source_mode,
                    use_inc_build=use_inc_build,
                )
                build_job_ids = request_ids[1:] if use_inc_build else request_ids

                self._async_build_job_ids = build_job_ids
                self._async_build_dependencies = []
                self._async_build_sanitizer = sanitizer
                logger.info(
                    "Prepared dispatcher async POV build requests for {}/{} "
                    "({} request(s), sanitizer={})",
                    self.benchmark_id,
                    self.harness_name,
                    len(build_job_ids),
                    sanitizer,
                )
                return self._async_build_job_ids, self._async_build_dependencies

            build_queue = self._get_build_queue()
            if build_queue is None:
                logger.warning("Build queue not available, skipping async POV enqueue")
                return None
            assert build_queue is not None

            prepare_dependency: list[object] = []
            if use_inc_build:
                prepare_job = PrepareIncImageJob(
                    benchmark_path=benchmark_path,
                    benchmark_name=adapter.benchmark_name,
                    sanitizer=sanitizer,
                    use_inc_build=True,
                    source_mode=source_mode,
                    inc_image_policy=engine.builder.infra.inc_image_policy,
                    inc_image_registry=engine.builder.infra.inc_image_registry,
                    inc_image_max_pull_bytes=engine.builder.infra.inc_image_max_pull_bytes,
                    inc_image_pull_timeout=engine.builder.infra.inc_image_pull_timeout,
                    local_image_prefix=engine.builder.infra.local_image_prefix,
                )
                prepare_rq_job = enqueue_ci_job(
                    build_queue, self._experiment_name or "", prepare_job
                )
                prepare_dependency = [prepare_rq_job]

            build_job_ids: list[str] = []
            build_dependencies: list[object] = []
            for config in plan.configs:
                build_job = BuildSingleVariantJob(
                    benchmark_path=config.benchmark_path,
                    benchmark_name=config.benchmark_name,
                    variant_type=config.variant_type,
                    commit=config.commit,
                    main_repo=config.main_repo,
                    mode=config.mode or adapter.get_mode(),
                    language=config.language,
                    cpv_num=config.cpv_num,
                    patch_id=config.patch_id,
                    pov_id=config.pov_id,
                    patches=config.patches,
                    use_inc_build=config.use_inc_build,
                    source_mode=source_mode,
                    sanitizer=config.sanitizer,
                    repo_name=config.repo_name,
                    prepare_inc_job_id=prepare_dependency[0].id
                    if prepare_dependency
                    else "",
                    inc_image_policy=engine.builder.infra.inc_image_policy,
                    inc_image_registry=engine.builder.infra.inc_image_registry,
                    inc_image_max_pull_bytes=engine.builder.infra.inc_image_max_pull_bytes,
                    inc_image_pull_timeout=engine.builder.infra.inc_image_pull_timeout,
                    local_image_prefix=engine.builder.infra.local_image_prefix,
                )
                rq_job_id = build_variant_rq_job_id(
                    benchmark=config.benchmark_name,
                    variant_name=config.variant_name,
                    source_mode=source_mode,
                    use_inc_build=config.use_inc_build,
                )
                build_rq_job = enqueue_ci_job(
                    build_queue,
                    self._experiment_name or "",
                    build_job,
                    depends_on=prepare_dependency or None,
                    job_id=rq_job_id,
                )
                build_job_ids.append(build_rq_job.id)
                build_dependencies.append(build_rq_job)

            self._async_build_job_ids = build_job_ids
            self._async_build_dependencies = build_dependencies
            self._async_build_sanitizer = sanitizer
            logger.info(
                "Prepared async POV build DAG for {}/{} ({} variant jobs, "
                "sanitizer={})",
                self.benchmark_id,
                self.harness_name,
                len(build_job_ids),
                sanitizer,
            )
            return self._async_build_job_ids, self._async_build_dependencies
        except Exception as e:
            logger.warning(f"Failed to enqueue async POV build DAG: {e}")
            return None

    def _enqueue_pov(self, pov_path: Path, pov_hash: str) -> Optional[str]:
        """Enqueue a single POV for async verification via Redis.

        The pov_id is formatted as ``{filename}:{hash}`` so the evaluator
        logs show the human-readable filename, while the hash suffix
        guarantees uniqueness even when the CRS reuses filenames
        (e.g., always writes to ``pov_0.blob``).

        Args:
            pov_path: Path to the POV file
            pov_hash: Content hash of the POV file

        Returns:
            Job ID if enqueued, None on error
        """
        from crsbench.distributed.queue import (
            ROUTING_MODEL_DISPATCHER,
            get_evaluator_routing_model,
        )
        from crsbench.distributed.verify_queue import enqueue_single_pov

        build_prereqs = self._ensure_async_build_jobs()
        if build_prereqs is None:
            return None
        build_job_ids, build_dependencies = build_prereqs

        pov_data = pov_path.read_bytes()
        self._pov_hash_to_path[pov_hash] = pov_path
        pov_id = f"{pov_path.name}:{pov_hash}"
        queue = None
        if get_evaluator_routing_model() != ROUTING_MODEL_DISPATCHER:
            queue = self._get_verify_queue()
            if queue is None:
                logger.warning("Verify queue not available, skipping async enqueue")
                return None

        return enqueue_single_pov(
            verify_queue=queue,  # type: ignore[arg-type]
            experiment_name=self._experiment_name or "",
            trial_id=self._trial_id or "",
            benchmark=self.benchmark_id,
            harness=self.harness_name,
            pov_id=pov_id,
            pov_data=pov_data,
            sanitizer=self._async_build_sanitizer or self._sanitizer,
            build_job_ids=build_job_ids,
            depends_on=build_dependencies or None,
            source_mode=self._engine.builder.source_mode if self._engine else "pkgs",
            use_inc_build=bool(self._adapter.inc_build) if self._adapter else True,
            redis_host=self._redis_host,
        )

    def _poll_pending_verdicts(self) -> None:
        """Poll Redis for completed async verification results.

        Updates internal state (store, counters) based on completed verdicts.
        Non-blocking: processes whatever results are available.
        """
        if not self._pending_job_ids or not self._redis_host:
            return

        from crsbench.distributed.verify_queue import poll_single_pov_verdicts

        completed, remaining = poll_single_pov_verdicts(
            self._redis_host,
            self._pending_job_ids,
            experiment_name=self._experiment_name,
        )
        self._pending_job_ids = remaining

        from crsbench.distributed.evaluator_jobs import SinglePovResult

        for result_dict in completed:
            try:
                result = SinglePovResult.from_dict(result_dict)
                # Use explicit status from verdict (handles all states correctly)
                status = PovVerificationStatus(result.verdict.status)
                cpv_matched = result.verdict.cpv_matches

                # Parse crash signature from async verdict crash logs
                sig_hash = self._extract_crash_sig_from_logs(result.verdict.crash_logs)

                # Add to store directly (no POV path — it was enqueued by content)
                self.store.add_pov_by_id(
                    result.verdict.pov_id,
                    status,
                    cpv_matched,
                    crash_signature=sig_hash,
                )

                # Store crash logs for ALL statuses (not just CPV)
                pov_hash = self.store._extract_hash(result.verdict.pov_id)
                for variant_name, crash_log in result.verdict.crash_logs.items():
                    self.store.store_crash_log(
                        pov_hash,
                        crash_log,
                        status,
                        cpv_matched,
                        variant_name=variant_name,
                    )

                if status == PovVerificationStatus.CPV:
                    # Store POV blob from the local file still on disk (CPV only)
                    pov_path = self._pov_hash_to_path.get(pov_hash)
                    if pov_path and pov_path.exists():
                        self.store.store_unique_pov(
                            pov_path, pov_hash, status, cpv_matched
                        )

                    for cpv_id in cpv_matched:
                        logger.info(
                            f"CPV found (async): cpv_id={cpv_id} "
                            f"pov={result.verdict.pov_id} "
                            f"found={len(self.found_cpvs)} "
                            f"total={self.total_expected_cpvs}"
                        )
                elif status == PovVerificationStatus.UNINTENDED_CRASH:
                    with self._lock:
                        self._unintended_crashes_count += 1
                    logger.info(
                        f"Unintended crash (async): pov={result.verdict.pov_id}"
                    )
                elif status == PovVerificationStatus.ERROR:
                    with self._lock:
                        self._errors_count += 1
            except Exception as e:
                logger.warning(f"Failed to process async verdict: {e}")
                with self._lock:
                    self._errors_count += 1

        if completed:
            logger.info(
                f"Processed {len(completed)} async verdicts, {len(remaining)} pending"
            )

    @staticmethod
    def _scan_pov_directory(directory: Path) -> list[Path]:
        """Scan a directory for POV files.

        Returns all regular files excluding hidden files, directories,
        and symlinks.

        Args:
            directory: Directory to scan for POV files.

        Returns:
            List of POV file paths found.
        """
        if not directory.exists():
            return []

        return [
            f
            for f in directory.glob("*")
            if f.is_file() and not f.name.startswith(".") and not f.is_symlink()
        ]

    def _discover_new_povs(self) -> list[tuple[Path, str]]:
        """Discover new POV files in output and EXCHANGE_DIR directories.

        Scans both the primary ``pov_output_dir`` (populated by
        ``collect_results()`` after CRS execution) and the EXCHANGE_DIR
        ``pov/`` subdirectory (populated in real-time by the exchange
        sidecar during CRS execution).

        POV files can have various formats:
        - No extension (hex hash like '47107064ecc2b03b')
        - .blob, .bin, .pov extensions

        Excludes hidden files (starting with '.'), directories, and symlinks.
        Deduplication is handled by the store's hash-based tracking.

        Returns:
            List of (path, hash) tuples for new POV files not yet tested
        """
        # Collect POV files from both directories
        pov_files = self._scan_pov_directory(self.pov_output_dir)

        if self._exchange_pov_dir is not None:
            pov_files.extend(self._scan_pov_directory(self._exchange_pov_dir))

        # Filter out already tested POVs and return with hashes.
        # Track seen hashes within this call to avoid returning the same
        # content twice when it exists in both directories.
        new_povs = []
        seen_hashes: set[str] = set()
        for pov_path in pov_files:
            pov_hash, is_tested = self.store.check_pov_hash(pov_path)
            if not is_tested and pov_hash not in seen_hashes:
                new_povs.append((pov_path, pov_hash))
                seen_hashes.add(pov_hash)
        return new_povs

    def _verify_pov(self, pov_path: Path) -> Optional[PovVerificationResult]:
        """Verify a single POV against ground truth CPVs.

        Args:
            pov_path: Path to the POV file

        Returns:
            PovVerificationResult if verification succeeds, None on error
        """
        if self._engine is None:
            logger.warning("VerificationEngine not initialized, skipping verification")
            return None

        if self._adapter is None:
            logger.warning("MetaYamlAdapter not initialized, skipping verification")
            return None

        try:
            # Read POV data
            pov_data = pov_path.read_bytes()

            # Create verification request
            request = PovVerificationRequest(
                pov_data=pov_data,
                harness=self.harness_name,
                benchmark=self.benchmark_id,
                pov_id=pov_path.name,
            )

            build_results = None
            if self._sanitizer:
                build_results = self._engine.get_or_build_results(
                    self._adapter,
                    sanitizer=self._sanitizer,
                )

            # Verify the POV
            return self._engine.verify_pov(
                request,
                self._adapter,
                build_results=build_results,
            )
        except Exception:
            logger.exception("POV verification failed for {}", pov_path)
            return None

    @staticmethod
    def _extract_crash_sig_from_logs(
        crash_logs: dict[str, str],
    ) -> Optional[str]:
        """Extract crash signature hash from a dict of variant crash logs.

        Used for async verdict processing where crash logs come as a
        plain dict rather than embedded in a PovVerificationResult.

        Args:
            crash_logs: Dict mapping variant_name -> crash_log text

        Returns:
            Crash signature hash (16-char hex) or None if not parseable
        """
        if not crash_logs:
            return None

        from crsbench.evaluation.verification.crash_signature import (
            parse_crash_signature,
        )

        # Iterate in key order for deterministic signature extraction.
        for variant_name in sorted(crash_logs):
            crash_log = crash_logs[variant_name]
            if crash_log:
                sig = parse_crash_signature(crash_log)
                if sig:
                    return sig.signature_hash
        return None

    @staticmethod
    def _extract_crash_signature(
        result: PovVerificationResult,
    ) -> Optional[str]:
        """Extract crash signature hash from a verification result's crash logs.

        Parses the first available crash log from the result's crash_info
        using the crash_signature module.

        Args:
            result: Verification result with optional crash_info

        Returns:
            Crash signature hash (16-char hex) or None if not parseable
        """
        if not result.crash_info:
            return None

        from crsbench.evaluation.verification.crash_signature import (
            parse_crash_signature,
        )

        # Try stdout logs (from verify_povs_parallel)
        stdout_logs = result.crash_info.get("stdout")
        if stdout_logs and isinstance(stdout_logs, dict):
            for variant_name in sorted(stdout_logs):
                crash_log = stdout_logs[variant_name]
                if crash_log:
                    sig = parse_crash_signature(crash_log)
                    if sig:
                        return sig.signature_hash

        # Try logs key (from verify_pov single-POV path)
        logs = result.crash_info.get("logs")
        if logs and isinstance(logs, dict):
            for variant_name in sorted(logs):
                crash_log = logs[variant_name]
                if crash_log:
                    sig = parse_crash_signature(crash_log)
                    if sig:
                        return sig.signature_hash

        return None

    def _update_state(
        self,
        pov_path: Path,
        result: Optional[PovVerificationResult],
        *,
        pov_hash: Optional[str] = None,
    ) -> None:
        """Update state after POV verification.

        Args:
            pov_path: Path to the verified POV
            result: Verification result (None if verification failed)
            pov_hash: Optional pre-computed hash (avoids recomputation)
        """
        if result is None:
            # Verification failed
            self.store.add_pov(
                pov_path, PovVerificationStatus.ERROR, [], pov_hash=pov_hash
            )
            with self._lock:
                self._errors_count += 1
            logger.warning(f"POV verification error: pov={pov_path.name}")
            return

        # Compute hash if not provided (needed for storing files)
        if pov_hash is None:
            from crsbench.evaluation.verification.utils import compute_content_hash

            pov_hash = compute_content_hash(pov_path)

        # Parse crash signature from crash logs
        sig_hash = self._extract_crash_signature(result)
        if sig_hash:
            result.crash_signature = sig_hash

        # Add to store with the verification status directly (no mapping needed)
        self.store.add_pov(
            pov_path,
            result.status,
            result.cpv_matched,
            pov_hash=pov_hash,
            crash_signature=sig_hash,
        )

        # Store per-variant crash logs for ALL statuses (not just CPV)
        if result.crash_info and "stdout" in result.crash_info:
            crash_logs = result.crash_info["stdout"]
            for variant_name, crash_log in crash_logs.items():
                self.store.store_crash_log(
                    pov_hash,
                    crash_log,
                    result.status,
                    result.cpv_matched,
                    variant_name=variant_name,
                )

        # Store POV blob for CPV matches only
        if result.status == PovVerificationStatus.CPV:
            self.store.store_unique_pov(
                pov_path, pov_hash, result.status, result.cpv_matched
            )

        # Update counters based on result
        if result.status == PovVerificationStatus.CPV:
            for cpv_id in result.cpv_matched:
                logger.info(
                    f"CPV found: cpv_id={cpv_id} pov={pov_path.name} "
                    f"found={len(self.found_cpvs)} "
                    f"total={self.total_expected_cpvs}"
                )
        elif result.status == PovVerificationStatus.UNINTENDED_CRASH:
            with self._lock:
                self._unintended_crashes_count += 1
            logger.info(f"Unintended crash: pov={pov_path.name}")
        elif result.status == PovVerificationStatus.NOT_VULNERABLE:
            logger.debug(f"POV not vulnerable: pov={pov_path.name}")
        else:
            with self._lock:
                self._errors_count += 1
            logger.warning(f"POV verification error: pov={pov_path.name}")

    def _should_terminate(self) -> bool:
        """Check if early termination condition is met.

        Returns:
            True if all CPVs found and early stop is enabled
        """
        if not self.config.early_stop_enabled:
            return False

        return self.all_cpvs_found

    def on_snapshot(self, cycle: int) -> POVSnapshot:
        """Create POV verification snapshot for a given cycle.

        This method is called by SnapshotManager before creating a snapshot
        archive. It captures the current POV verification state.

        Thread-safe: Uses internal lock to protect snapshot state.

        Args:
            cycle: Snapshot cycle number (1-indexed)

        Returns:
            POVSnapshot with current verification state
        """
        timestamp = time.time()
        elapsed_time = timestamp - self.trial_start_time

        # Discover new POVs (returns tuples of path, hash)
        new_povs = self._discover_new_povs()
        povs_new = 0

        if self._async_mode:
            # Async mode: enqueue new POVs to Redis, poll for results
            for pov_path, pov_hash in new_povs:
                job_id = self._enqueue_pov(pov_path, pov_hash)
                if job_id:
                    self._pending_job_ids.append(job_id)
                    pov_id = f"{pov_path.name}:{pov_hash}"
                    self._job_to_pov_id[job_id] = pov_id
                    # Capture file mtime now (POV creation time) before
                    # the async verdict overwrites it with poll time
                    stat = pov_path.stat()
                    self.store.mark_hash_tested(
                        pov_hash,
                        file_mtime=stat.st_mtime,
                        file_size=stat.st_size,
                    )
                povs_new += 1

            # Poll for completed async verdicts
            self._poll_pending_verdicts()
        else:
            # Inline mode: verify POVs synchronously
            for pov_path, pov_hash in new_povs:
                result = self._verify_pov(pov_path)
                self._update_state(pov_path, result, pov_hash=pov_hash)
                povs_new += 1

        # Check for early termination
        if self._should_terminate() and not self._early_stop_triggered:
            self._early_stop_triggered = True
            self._early_stop_time = datetime.now()

            logger.info(
                "=" * 60 + "\n"
                f"[EARLY TERMINATION] All CPVs found - triggering early stop\n"
                f"  harness: {self.harness_name}\n"
                f"  benchmark: {self.benchmark_id}\n"
                f"  cpvs_found: {len(self.found_cpvs)}/{self.total_expected_cpvs}\n"
                f"  cpv_ids: {sorted(self.found_cpvs)}\n"
                f"  elapsed_time: {elapsed_time:.1f}s\n" + "=" * 60
            )

            # Signal stop event if provided
            if self._stop_event is not None:
                logger.info("[EARLY TERMINATION] Signaling stop event for CRS process")
                self._stop_event.set()

        # Get remaining CPVs
        cpvs_remaining = self._get_remaining_cpvs()

        # Create snapshot
        with self._lock:
            snapshot = POVSnapshot(
                cycle=cycle,
                timestamp=timestamp,
                elapsed_time=elapsed_time,
                harness_name=self.harness_name,
                cpvs_found=list(self.found_cpvs),
                cpvs_remaining=cpvs_remaining,
                povs_total=len(self.store.povs),
                povs_new=povs_new,
                duplicates_skipped=0,  # Duplicates filtered in _discover_new_povs
                unintended_crashes_count=self._unintended_crashes_count,
                early_stop_triggered=self._early_stop_triggered,
            )
            self._snapshot_count += 1
            self._latest_snapshot = snapshot

        # Save snapshot to file
        self._save_snapshot_file(snapshot)
        self._save_snapshot_history()
        self.store.save()

        logger.info(
            f"POV snapshot {cycle}: "
            f"cpvs={len(self.found_cpvs)}/{self.total_expected_cpvs}, "
            f"povs={len(self.store.povs)} (+{povs_new}), "
            f"unintended_crashes={self._unintended_crashes_count}"
        )

        return snapshot

    def get_state(self) -> dict:
        """Get thread-safe snapshot of current state.

        Returns:
            Dictionary with current state data
        """
        with self._lock:
            return {
                "benchmark_id": self.benchmark_id,
                "harness_name": self.harness_name,
                "total_expected_cpvs": self.total_expected_cpvs,
                "found_cpvs": list(self.found_cpvs),
                "processed_hashes_count": len(self.store.povs),
                "duplicates_count": self._duplicates_count,
                "unintended_crashes_count": self._unintended_crashes_count,
                "errors_count": self._errors_count,
                "cpvs_remaining": self.total_expected_cpvs - len(self.found_cpvs),
                "all_cpvs_found": self.all_cpvs_found,
            }

    def get_report(self) -> POVVerificationReport:
        """Generate final POV verification report.

        Returns:
            POVVerificationReport with final verification results
        """
        total_duration = time.time() - self.trial_start_time

        # Get remaining CPVs
        cpvs_remaining = self._get_remaining_cpvs()

        # Count unique crash sites from store
        crash_summary = self.store.get_unintended_crash_summary()
        unique_crash_sites = len(crash_summary)

        with self._lock:
            return POVVerificationReport(
                benchmark_id=self.benchmark_id,
                harness_name=self.harness_name,
                total_expected_cpvs=self.total_expected_cpvs,
                cpvs_found=list(self.found_cpvs),
                cpvs_remaining=cpvs_remaining,
                total_povs_processed=len(self.store.povs),
                duplicates_skipped=self._duplicates_count,
                unintended_crashes=self._unintended_crashes_count,
                unique_unintended_crash_sites=unique_crash_sites,
                verification_errors=self._errors_count,
                verification_timeouts=0,  # Not tracked separately
                early_stopped=self._early_stop_triggered,
                early_stop_time=self._early_stop_time,
                total_duration_seconds=total_duration,
            )

    def drain_pending(
        self,
        per_pov_timeout: int = 180,
        verify_timeout: int = 7200,
        poll_interval: float = 2.0,
    ) -> None:
        """Block until all pending async verdicts complete.

        In async (Redis) mode, POV verification jobs may still be running.
        This method polls until all pending jobs are resolved or timeout expires.

        Async mode treats ``verify_timeout`` as the end-to-end verification
        phase budget, including any queued build prerequisites declared for
        pending POV jobs.

        In inline mode, this is a no-op since all verifications are synchronous.

        Args:
            per_pov_timeout: Max seconds a single POV verification takes
            verify_timeout: Overall verification phase budget in seconds
            poll_interval: Seconds between poll attempts
        """
        if not self._pending_job_ids or not self._async_mode:
            return

        n_pending = len(self._pending_job_ids)
        timeout = float(verify_timeout)

        deadline = time.time() + timeout
        logger.info(
            f"Draining {n_pending} pending async verdicts "
            f"(timeout={timeout:.0f}s, per_pov={per_pov_timeout}s)"
        )

        while self._pending_job_ids and time.time() < deadline:
            self._poll_pending_verdicts()
            if self._pending_job_ids:
                time.sleep(poll_interval)

        if self._pending_job_ids:
            logger.warning(
                f"Drain timeout: {len(self._pending_job_ids)} verdicts still pending"
            )
            self._mark_pending_as_error()
        else:
            logger.info("All pending async verdicts drained successfully")
            self.store.save()

    def _mark_pending_as_error(self) -> None:
        """Mark POVs with pending async verdicts as error after drain timeout."""
        for job_id in self._pending_job_ids:
            pov_id = self._job_to_pov_id.get(job_id)
            if pov_id:
                self.store.add_pov_by_id(
                    pov_id,
                    PovVerificationStatus.ERROR,
                    [],
                )
                logger.warning(f"Marked POV {pov_id} as error (async drain timeout)")
            else:
                logger.warning(f"Job {job_id} timed out but has no mapped pov_id")
            with self._lock:
                self._errors_count += 1
        self._pending_job_ids.clear()
        self.store.save()

    def get_verification_results(self) -> list[PovVerificationResult]:
        """Export stored POV verification data as PovVerificationResult list.

        Converts the internal POVStore entries into the same result format
        that VerificationEngine.verify_benchmark() returns. This allows
        the runner to use manager results directly without a separate
        verification pass.

        Returns:
            List of PovVerificationResult, one per verified POV
        """
        results: list[PovVerificationResult] = []
        with self._lock:
            for entry in self.store.povs.values():
                results.append(
                    PovVerificationResult(
                        status=entry.status,
                        benchmark=self.benchmark_id,
                        cpv_matched=list(entry.cpv_matched),
                        pov_id=entry.hash,
                        crash_signature=entry.crash_signature,
                    )
                )
        return results

    def _save_snapshot_file(self, snapshot: POVSnapshot) -> None:
        """Save individual snapshot to file.

        Args:
            snapshot: POVSnapshot to save
        """
        try:
            snapshots_dir = self.trial_dir / "povs" / "snapshots"
            snapshots_dir.mkdir(parents=True, exist_ok=True)

            snapshot_file = snapshots_dir / f"snapshot-{snapshot.cycle:03d}.json"
            snapshot_data = {
                "cycle": snapshot.cycle,
                "timestamp": snapshot.timestamp,
                "elapsed_time": snapshot.elapsed_time,
                "harness_name": snapshot.harness_name,
                "cpvs_found": snapshot.cpvs_found,
                "cpvs_remaining": snapshot.cpvs_remaining,
                "povs_total": snapshot.povs_total,
                "povs_new": snapshot.povs_new,
                "duplicates_skipped": snapshot.duplicates_skipped,
                "unintended_crashes_count": snapshot.unintended_crashes_count,
                "early_stop_triggered": snapshot.early_stop_triggered,
            }

            snapshot_file.write_text(json.dumps(snapshot_data, indent=2))
            logger.debug(f"Saved POV snapshot to {snapshot_file}")
        except Exception as e:
            logger.warning(f"Failed to save POV snapshot file: {e}")

    def _save_snapshot_history(self) -> None:
        """Save snapshot summary to trial directory.

        Note: Individual snapshots are saved in snapshot-{NNN}.json files.
        This file contains overall summary stats.
        """
        try:
            history_file = self.trial_dir / "povs" / "snapshot_history.json"
            history_file.parent.mkdir(parents=True, exist_ok=True)

            with self._lock:
                latest = self._latest_snapshot
                history_data = {
                    "harness_name": self.harness_name,
                    "total_expected_cpvs": self.total_expected_cpvs,
                    "expected_cpv_ids": sorted(self.expected_cpv_ids),
                    "early_stop_enabled": self.config.early_stop_enabled,
                    "early_stop_triggered": self._early_stop_triggered,
                    "snapshot_count": self._snapshot_count,
                    "latest_snapshot": {
                        "cycle": latest.cycle,
                        "elapsed_time": latest.elapsed_time,
                        "cpvs_found": latest.cpvs_found,
                        "cpvs_remaining": latest.cpvs_remaining,
                        "povs_total": latest.povs_total,
                        "povs_new": latest.povs_new,
                        "unintended_crashes_count": latest.unintended_crashes_count,
                        "early_stop_triggered": latest.early_stop_triggered,
                    }
                    if latest
                    else None,
                }

            history_file.write_text(json.dumps(history_data, indent=2))
            logger.debug(f"Saved POV snapshot history to {history_file}")
        except Exception as e:
            logger.warning(f"Failed to save POV snapshot history: {e}")
