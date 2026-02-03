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
        self._verify_queue: Optional[object] = None  # Lazy-initialized RQ Queue
        self._pending_job_ids: list[str] = []  # Job IDs awaiting results
        self._pov_hash_to_path: dict[str, Path] = {}  # hash → local file path

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
        from crsbench.distributed.verify_queue import enqueue_single_pov

        queue = self._get_verify_queue()
        if queue is None:
            logger.warning("Verify queue not available, skipping async enqueue")
            return None

        pov_data = pov_path.read_bytes()
        self._pov_hash_to_path[pov_hash] = pov_path
        pov_id = f"{pov_path.name}:{pov_hash}"
        return enqueue_single_pov(
            verify_queue=queue,  # type: ignore[arg-type]
            experiment_name=self._experiment_name or "",
            trial_id=self._trial_id or "",
            benchmark=self.benchmark_id,
            harness=self.harness_name,
            pov_id=pov_id,
            pov_data=pov_data,
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
            self._redis_host, self._pending_job_ids
        )
        self._pending_job_ids = remaining

        from crsbench.distributed.evaluator_jobs import SinglePovResult

        for result_dict in completed:
            try:
                result = SinglePovResult.from_dict(result_dict)
                # Use explicit status from verdict (handles all states correctly)
                status = PovVerificationStatus(result.verdict.status)
                cpv_matched = result.verdict.cpv_matches

                # Add to store directly (no POV path — it was enqueued by content)
                self.store.add_pov_by_id(result.verdict.pov_id, status, cpv_matched)

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

    def _discover_new_povs(self) -> list[tuple[Path, str]]:
        """Discover new POV files in the output directory.

        POV files can have various formats:
        - No extension (hex hash like '47107064ecc2b03b')
        - .blob, .bin, .pov extensions

        Excludes hidden files (starting with '.'), directories, and symlinks.

        Returns:
            List of (path, hash) tuples for new POV files not yet tested
        """
        if not self.pov_output_dir.exists():
            return []

        # Match all files, exclude hidden files, directories, and symlinks
        pov_files = [
            f
            for f in self.pov_output_dir.glob("*")
            if f.is_file() and not f.name.startswith(".") and not f.is_symlink()
        ]

        # Filter out already tested POVs and return with hashes
        new_povs = []
        for pov_path in pov_files:
            pov_hash, is_tested = self.store.check_pov_hash(pov_path)
            if not is_tested:
                new_povs.append((pov_path, pov_hash))
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

            # Verify the POV
            return self._engine.verify_pov(request, self._adapter)
        except Exception as e:
            logger.error(f"POV verification failed for {pov_path}: {e}", exc_info=True)
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

        # Add to store with the verification status directly (no mapping needed)
        self.store.add_pov(
            pov_path, result.status, result.cpv_matched, pov_hash=pov_hash
        )

        # Store per-variant crash logs for ALL statuses (not just CPV)
        if result.crash_info and "logs" in result.crash_info:
            crash_logs = result.crash_info["logs"]
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
                verification_errors=self._errors_count,
                verification_timeouts=0,  # Not tracked separately
                early_stopped=self._early_stop_triggered,
                early_stop_time=self._early_stop_time,
                total_duration_seconds=total_duration,
            )

    def drain_pending(self, timeout: float = 300.0, poll_interval: float = 2.0) -> None:
        """Block until all pending async verdicts complete.

        In async (Redis) mode, POV verification jobs may still be running.
        This method polls until all pending jobs are resolved or timeout expires.

        In inline mode, this is a no-op since all verifications are synchronous.

        Args:
            timeout: Maximum seconds to wait for pending results
            poll_interval: Seconds between poll attempts
        """
        if not self._pending_job_ids or not self._async_mode:
            return

        deadline = time.time() + timeout
        logger.info(
            f"Draining {len(self._pending_job_ids)} pending async verdicts "
            f"(timeout={timeout}s)"
        )

        while self._pending_job_ids and time.time() < deadline:
            self._poll_pending_verdicts()
            if self._pending_job_ids:
                time.sleep(poll_interval)

        if self._pending_job_ids:
            logger.warning(
                f"Drain timeout: {len(self._pending_job_ids)} verdicts still pending"
            )
        else:
            logger.info("All pending async verdicts drained successfully")

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
