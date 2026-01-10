"""Snapshot manager for periodic CRS trial progress capture.

This module implements SnapshotManager, which runs in a separate thread
during CRS execution to periodically capture trial progress.

Threading model:
- Main thread: Runs CRS subprocess
- Snapshot thread: Periodic snapshot capture (sleep-based timing)

The snapshot thread is daemon=True, ensuring Python doesn't wait for it
indefinitely if shutdown takes too long.
"""

from __future__ import annotations

import json
import shutil
import tarfile
import threading
import time
from pathlib import Path  # noqa: TC003 - used at runtime for file operations
from typing import TYPE_CHECKING, Optional

from crsbench.evaluation.snapshot import Snapshot, SnapshotMetadata
from crsbench.utils.logger import get_logger

if TYPE_CHECKING:
    from crsbench.evaluation.coverage.manager import CoverageManager
    from crsbench.evaluation.verification.patch.manager import PatchVerificationManager
    from crsbench.evaluation.litellm_tracker import LiteLLMTracker
    from crsbench.evaluation.verification.pov.manager import POVVerificationManager

logger = get_logger(__name__)


class SnapshotManager:
    """Manages periodic snapshot capture during CRS trial execution.

    This class runs in a separate thread and periodically captures the state
    of a CRS trial, including POVs, patches, corpus, logs, and LLM metrics.

    Attributes:
        trial_dir: Trial output directory containing CRS outputs
        snapshot_period: Interval between snapshots in seconds
        trial_start_time: Unix timestamp when trial started
    """

    def __init__(
        self,
        trial_dir: Path,
        snapshot_period: int,
        trial_start_time: Optional[float] = None,
        *,
        coverage_manager: Optional["CoverageManager"] = None,
        pov_verification_manager: Optional["POVVerificationManager"] = None,
        patch_verification_manager: Optional["PatchVerificationManager"] = None,
        llm_tracker: Optional["LiteLLMTracker"] = None,
        llm_api_key: Optional[str] = None,
        llm_trial_id: Optional[str] = None,
    ):
        """Initialize snapshot manager.

        Args:
            trial_dir: Trial output directory (must exist)
            snapshot_period: Snapshot interval in seconds (must be > 0)
            trial_start_time: Trial start timestamp (defaults to current time)
            coverage_manager: Optional CoverageManager for collecting coverage snapshots
            pov_verification_manager: Optional POVVerificationManager for POV verification
                (bug-finding CRS)
            patch_verification_manager: Optional PatchVerificationManager for patch tracking
                (bug-fixing CRS)
            llm_tracker: Optional LiteLLMTracker for querying LLM usage
            llm_api_key: Optional trial-specific API key for LLM tracking
            llm_trial_id: Optional trial identifier for LLM usage file

        Raises:
            ValueError: If snapshot_period <= 0 or trial_dir doesn't exist
        """
        if snapshot_period <= 0:
            raise ValueError(f"snapshot_period must be > 0, got {snapshot_period}")

        if not trial_dir.exists():
            raise ValueError(f"trial_dir does not exist: {trial_dir}")

        self.trial_dir = trial_dir
        self.snapshot_period = snapshot_period
        self.trial_start_time = trial_start_time or time.time()
        self.crs_run_start_time: Optional[float] = None
        self.coverage_manager = coverage_manager
        self.pov_verification_manager = pov_verification_manager
        self.patch_verification_manager = patch_verification_manager

        # LLM tracking
        self.llm_tracker = llm_tracker
        self.llm_api_key = llm_api_key
        self.llm_trial_id = llm_trial_id

        # State tracking
        self.cycle = 0
        self.running = False
        self.shutdown_event = threading.Event()

        logger.info(
            f"SnapshotManager initialized: period={snapshot_period}s, trial_dir={trial_dir}"
        )
        if llm_tracker and llm_api_key:
            logger.info("LLM tracking enabled for snapshots")

    def run(self):
        """Main snapshot loop (runs in separate thread).



        This method should be called in a separate thread via:
            thread = threading.Thread(target=manager.run, daemon=True)
            thread.start()

        The loop will:
        1. Sleep until next snapshot time
        2. Capture snapshot
        3. Repeat until stopped

        The loop respects shutdown_event for quick termination.
        """
        self.running = True
        logger.info("Snapshot thread started")

        try:
            while self.running:
                # Sleep until next snapshot (with quick shutdown checks)
                if self._sleep_until_next_snapshot():
                    break  # Shutdown requested

                if not self.running:
                    break

                # Capture snapshot
                try:
                    self.capture_snapshot()
                except Exception as e:
                    # Log error but don't crash - continue to next snapshot
                    logger.error(
                        f"Snapshot {self.cycle + 1} failed: {e}", exc_info=True
                    )

        finally:
            self.running = False
            self._create_final_symlink()
            logger.info(f"Snapshot thread stopped (captured {self.cycle} snapshots)")

    def stop(self):
        """Signal snapshot thread to stop.

        This method should be called from the main thread when CRS completes.
        After calling stop(), join the thread with a timeout:
            manager.stop()
            thread.join(timeout=5.0)
        """
        logger.info("Stopping snapshot manager")
        self.running = False
        self.shutdown_event.set()

    def set_crs_run_start_time(self, start_time: float) -> None:
        """Set CRS run start time (called after build completes).

        Args:
            start_time: Unix timestamp when CRS run started
        """
        self.crs_run_start_time = start_time
        logger.info(f"CRS run start time set: {start_time}")

    def capture_snapshot(self) -> Snapshot:
        """Capture a single snapshot.

        This method:
        1. Creates temporary directory for snapshot contents
        2. Captures metadata, config, logs, POVs, patches, corpus
        3. Compresses to tar.gz archive
        4. Writes completion marker
        5. Cleans up temporary directory

        Returns:
            Snapshot object with captured content information

        Errors are logged but not raised (allows continuing to next snapshot).
        """
        self.cycle += 1
        snapshot_timestamp = time.time()
        elapsed_time = snapshot_timestamp - self.trial_start_time
        running_elapsed_time = (
            snapshot_timestamp - self.crs_run_start_time
            if self.crs_run_start_time
            else None
        )

        # Log running time if CRS has started, otherwise log total elapsed time
        if running_elapsed_time is not None:
            logger.info(
                f"Capturing snapshot {self.cycle} (running: {running_elapsed_time:.1f}s)"
            )
        else:
            logger.info(
                f"Capturing snapshot {self.cycle} (elapsed: {elapsed_time:.1f}s)"
            )

        # Create temp directory for snapshot contents
        temp_dir = self.trial_dir / f".snapshot-{self.cycle:04d}"
        temp_dir.mkdir(exist_ok=True)

        # Track captured content flags for return value
        has_config = False
        has_execution_metadata = False
        has_llm_usage = False
        has_crs_log = False
        has_povs = False
        has_patches = False
        has_corpus = False
        has_crs_data = False

        try:
            # Capture all snapshot data (tracking what was captured)
            self._capture_metadata(temp_dir, elapsed_time, running_elapsed_time)

            has_config = self._capture_config(temp_dir)
            has_execution_metadata = self._capture_execution_metadata(temp_dir)
            has_llm_usage = self._capture_llm_usage(temp_dir)
            has_crs_log = self._capture_crs_log(temp_dir)

            has_povs = self._capture_povs(temp_dir)
            has_patches = self._capture_patches(temp_dir)
            has_corpus = self._capture_corpus(temp_dir)
            has_crs_data = self._capture_crs_data(temp_dir)

            # Capture coverage snapshot if coverage manager is available
            coverage_snapshot = None
            if self.coverage_manager:
                try:
                    coverage_snapshot = self.coverage_manager.on_snapshot(self.cycle)
                    self._capture_coverage(temp_dir, coverage_snapshot)
                except Exception as e:
                    logger.warning(f"Failed to capture coverage snapshot: {e}")

            # Capture POV verification snapshot if POV verification manager is available
            if self.pov_verification_manager:
                try:
                    pov_snapshot = self.pov_verification_manager.on_snapshot(self.cycle)
                    self._capture_pov_verification(temp_dir, pov_snapshot)
                except Exception as e:
                    logger.warning(f"Failed to capture POV verification snapshot: {e}")

            # Capture patch verification snapshot if patch verification manager is available
            if self.patch_verification_manager:
                try:
                    patch_snapshot = self.patch_verification_manager.on_snapshot(
                        self.cycle
                    )
                    self._capture_patch_verification(temp_dir, patch_snapshot)
                except Exception as e:
                    logger.warning(
                        f"Failed to capture patch verification snapshot: {e}"
                    )

            # Compress to tar.gz
            archive_path = self.trial_dir / f"snapshot-{self.cycle:04d}.tar.gz"
            self._create_tar_gz(temp_dir, archive_path)

            # Update snapshot-latest symlink
            self._update_latest_symlink(archive_path)

            # Mark complete
            marker_path = self.trial_dir / f"snapshot-{self.cycle:04d}.complete"
            marker_path.touch()

            # Update LLM logs file in trial directory (not in snapshot)
            self._update_llm_logs()

            logger.info(f"Snapshot {self.cycle} completed: {archive_path.name}")

            # Create and return Snapshot object
            return Snapshot(
                cycle=self.cycle,
                timestamp=snapshot_timestamp,
                elapsed_time=elapsed_time,
                snapshot_period=self.snapshot_period,
                running_elapsed_time=running_elapsed_time,
                archive_path=archive_path,
                is_complete=True,
                has_config=has_config,
                has_execution_metadata=has_execution_metadata,
                has_llm_usage=has_llm_usage,
                has_crs_log=has_crs_log,
                has_povs=has_povs,
                has_patches=has_patches,
                has_corpus=has_corpus,
                has_crs_data=has_crs_data,
            )

        finally:
            # Always cleanup temp directory
            if temp_dir.exists():
                shutil.rmtree(temp_dir, ignore_errors=True)

    def _sleep_until_next_snapshot(self) -> bool:
        """Sleep until next snapshot period.

        Uses quick shutdown checks (every 1 second) to enable responsive shutdown.

        Returns:
            True if shutdown was requested during sleep, False otherwise
        """
        remaining = self.snapshot_period

        while remaining > 0 and self.running:
            # Sleep in 1-second chunks for responsive shutdown
            sleep_time = min(1.0, remaining)
            if self.shutdown_event.wait(sleep_time):
                return True  # Shutdown requested
            remaining -= sleep_time

        return not self.running

    def _get_crs_output_dir(self) -> Path:
        """Get the CRS output directory.

        Returns:
            Path to CRS output directory (trial_dir/output/)
        """
        return self.trial_dir / "output"

    def _capture_metadata(
        self, temp_dir: Path, elapsed_time: float, running_elapsed_time: Optional[float]
    ):
        """Capture snapshot metadata."""
        metadata = SnapshotMetadata(
            cycle=self.cycle,
            timestamp=time.time(),
            elapsed_time=elapsed_time,
            snapshot_period=self.snapshot_period,
            running_elapsed_time=running_elapsed_time,
        )

        metadata_path = temp_dir / "metadata.json"
        metadata_path.write_text(metadata.model_dump_json())

    def _capture_config(self, temp_dir: Path) -> bool:
        """Capture experiment config (full - static).

        Returns:
            True if config was captured, False otherwise
        """
        config_path = self.trial_dir / "config.yaml"
        if config_path.exists():
            shutil.copy2(config_path, temp_dir / "config.yaml")
            return True
        return False

    def _capture_execution_metadata(self, temp_dir: Path) -> bool:
        """Capture execution metadata (full - static).

        Returns:
            True if execution metadata was captured, False otherwise
        """
        exec_path = self.trial_dir / "execution.json"
        if exec_path.exists():
            shutil.copy2(exec_path, temp_dir / "execution.json")
            return True
        return False

    def _capture_llm_usage(self, temp_dir: Path) -> bool:
        """Capture LLM usage log (full - cumulative).

        If LLM tracker is configured, queries the LiteLLM API directly to get
        real-time usage data. Otherwise, falls back to copying existing file.

        Returns:
            True if LLM usage was captured, False otherwise
        """
        # If LLM tracker is available, query API directly
        if self.llm_tracker and self.llm_api_key and self.llm_trial_id:
            try:
                output_path = temp_dir / "llm-usage.json"
                self.llm_tracker.write_llm_usage_file(
                    api_key=self.llm_api_key,
                    trial_id=self.llm_trial_id,
                    output_path=output_path,
                )
                logger.debug("Captured LLM usage from API")
                return True
            except Exception as e:
                logger.warning(f"Failed to capture LLM usage from API: {e}")
                # Fall through to try file-based capture

        # Fall back to copying existing file
        llm_path = self.trial_dir / "llm-usage.json"
        if llm_path.exists():
            try:
                shutil.copy2(llm_path, temp_dir / "llm-usage.json")
                return True
            except Exception as e:
                logger.warning(f"Failed to capture llm-usage.json: {e}")
                return False
        return False

    def _update_llm_logs(self) -> None:
        """Update LLM logs file in trial directory.

        This writes all LLM request/response logs to a single file in the
        trial directory. Called on each snapshot to keep logs up-to-date.
        Unlike llm-usage.json which is included in snapshots, this file
        is maintained separately for dashboard viewing.
        """
        if not (self.llm_tracker and self.llm_api_key and self.llm_trial_id):
            return

        try:
            output_path = self.trial_dir / "llm-logs.json"
            self.llm_tracker.write_llm_logs_file(
                api_key=self.llm_api_key,
                trial_id=self.llm_trial_id,
                output_path=output_path,
            )
            logger.debug("Updated LLM logs file")
        except Exception as e:
            logger.warning(f"Failed to update LLM logs: {e}")

    def _capture_crs_log(self, temp_dir: Path) -> bool:
        """Capture CRS output log (full - complete log).

        Returns:
            True if CRS log was captured, False otherwise
        """
        log_path = self.trial_dir / "crs-output.log"
        if log_path.exists():
            try:
                shutil.copy2(log_path, temp_dir / "crs-output.log")
                return True
            except Exception as e:
                logger.warning(f"Failed to capture crs-output.log: {e}")
                return False
        return False

    def _capture_povs(self, temp_dir: Path) -> bool:
        """Capture all POVs (files and directories).

        Returns:
            True if POVs were captured, False otherwise
        """
        output_dir = self._get_crs_output_dir()
        pov_dir = output_dir / "povs"

        if not pov_dir.exists():
            return False

        # Copy entire POVs directory (both files and directories)
        snapshot_pov_dir = temp_dir / "povs"

        try:
            shutil.copytree(pov_dir, snapshot_pov_dir, dirs_exist_ok=True)
            return True
        except Exception as e:
            logger.warning(f"Failed to capture POVs: {e}")
            return False

    def _capture_patches(self, temp_dir: Path) -> bool:
        """Capture all patches (organized by POV ID).

        Returns:
            True if patches were captured, False otherwise
        """
        output_dir = self._get_crs_output_dir()
        patches_dir = output_dir / "patches"

        if not patches_dir.exists():
            return False

        # Copy entire patches directory (both files and directories)
        snapshot_patches_dir = temp_dir / "patches"

        try:
            shutil.copytree(patches_dir, snapshot_patches_dir, dirs_exist_ok=True)
            return True
        except Exception as e:
            logger.warning(f"Failed to capture patches: {e}")
            return False

    def _capture_corpus(self, temp_dir: Path) -> bool:
        """Capture all corpus contents.

        Returns:
            True if corpus was captured, False otherwise
        """
        output_dir = self._get_crs_output_dir()
        corpus_dir = output_dir / "corpus"

        if not corpus_dir.exists():
            return False

        # Copy entire corpus directory (both files and directories)
        snapshot_corpus_dir = temp_dir / "corpus"

        try:
            shutil.copytree(corpus_dir, snapshot_corpus_dir, dirs_exist_ok=True)
            return True
        except Exception as e:
            logger.warning(f"Failed to capture corpus: {e}")
            return False

    def _capture_crs_data(self, temp_dir: Path) -> bool:
        """Capture CRS-specific data (incremental - by mtime, optional).

        Returns:
            True if CRS data was captured, False otherwise
        """
        output_dir = self._get_crs_output_dir()
        crs_data_dir = output_dir / "crs-data"

        if not crs_data_dir.exists():
            return False

        # Copy entire crs-data directory (CRS-specific, no deduplication)
        snapshot_crs_data_dir = temp_dir / "crs-data"

        try:
            shutil.copytree(crs_data_dir, snapshot_crs_data_dir, dirs_exist_ok=True)
            logger.debug("Captured crs-data directory")
            return True
        except Exception as e:
            logger.warning(f"Failed to capture crs-data: {e}")
            return False

    def _create_tar_gz(self, source_dir: Path, archive_path: Path):
        """Compress snapshot directory to tar.gz.

        Args:
            source_dir: Directory containing snapshot files
            archive_path: Path to output tar.gz file
        """
        with tarfile.open(archive_path, "w:gz") as tar:
            for item in source_dir.rglob("*"):
                if item.is_file():
                    arcname = item.relative_to(source_dir)
                    tar.add(item, arcname=arcname)

    def _update_latest_symlink(self, archive_path: Path):
        """Update snapshot-latest.tar.gz symlink to point to current snapshot.

        Args:
            archive_path: Path to the current snapshot archive
        """
        latest_link = self.trial_dir / "snapshot-latest.tar.gz"

        # Remove existing symlink if it exists
        if latest_link.exists() or latest_link.is_symlink():
            latest_link.unlink()

        # Create symlink to current snapshot
        latest_link.symlink_to(archive_path.name)
        logger.debug(f"Updated snapshot-latest.tar.gz -> {archive_path.name}")

    def _create_final_symlink(self):
        """Create snapshot-final.tar.gz symlink to the last captured snapshot."""
        if self.cycle == 0:
            return  # No snapshots captured

        final_link = self.trial_dir / "snapshot-final.tar.gz"
        final_archive_name = f"snapshot-{self.cycle:04d}.tar.gz"
        final_archive_path = self.trial_dir / final_archive_name

        # Only create symlink if the archive actually exists
        if not final_archive_path.exists():
            logger.debug(
                f"Skipping final symlink - archive {final_archive_name} does not exist"
            )
            return

        # Remove existing symlink if it exists
        if final_link.exists() or final_link.is_symlink():
            final_link.unlink()

        # Create symlink to final snapshot
        final_link.symlink_to(final_archive_name)
        logger.info(f"Created snapshot-final.tar.gz -> {final_archive_name}")

    def _capture_coverage(self, temp_dir: Path, coverage_snapshot) -> bool:
        """Capture coverage snapshot data.

        Args:
            temp_dir: Temporary directory for snapshot contents
            coverage_snapshot: CoverageSnapshot from coverage manager

        Returns:
            True if coverage was captured, False otherwise
        """
        if coverage_snapshot is None:
            return False

        try:
            coverage_data = {
                "cycle": coverage_snapshot.cycle,
                "timestamp": coverage_snapshot.timestamp,
                "elapsed_time": coverage_snapshot.elapsed_time,
                "harness_name": coverage_snapshot.harness_name,
                "saturation_detected": coverage_snapshot.saturation_detected,
                "new_corpus_count": coverage_snapshot.new_corpus_count,
                "new_lines_count": coverage_snapshot.new_lines_count,
                "summary": coverage_snapshot.summary.model_dump(),
            }

            coverage_path = temp_dir / "coverage.json"
            coverage_path.write_text(json.dumps(coverage_data, indent=2))
            logger.debug(
                f"Captured coverage: lines={coverage_snapshot.summary.lines_covered}, "
                f"saturation={coverage_snapshot.saturation_detected}"
            )
            return True
        except Exception as e:
            logger.warning(f"Failed to write coverage.json: {e}")
            return False

    def _capture_pov_verification(self, temp_dir: Path, pov_snapshot) -> bool:
        """Capture POV verification snapshot data.

        Args:
            temp_dir: Temporary directory for snapshot contents
            pov_snapshot: POVSnapshot from POV verification manager

        Returns:
            True if POV verification data was captured, False otherwise
        """
        if pov_snapshot is None:
            return False

        try:
            pov_data = {
                "cycle": pov_snapshot.cycle,
                "timestamp": pov_snapshot.timestamp,
                "elapsed_time": pov_snapshot.elapsed_time,
                "harness_name": pov_snapshot.harness_name,
                "cpvs_found": pov_snapshot.cpvs_found,
                "cpvs_remaining": pov_snapshot.cpvs_remaining,
                "povs_total": pov_snapshot.povs_total,
                "povs_new": pov_snapshot.povs_new,
                "duplicates_skipped": pov_snapshot.duplicates_skipped,
                "zerodays_count": pov_snapshot.zerodays_count,
                "early_stop_triggered": pov_snapshot.early_stop_triggered,
            }

            pov_path = temp_dir / "pov_verification.json"
            pov_path.write_text(json.dumps(pov_data, indent=2))
            logger.debug(
                f"Captured POV verification: cpvs={len(pov_snapshot.cpvs_found)}, "
                f"povs={pov_snapshot.povs_total}, "
                f"early_stop={pov_snapshot.early_stop_triggered}"
            )
            return True
        except Exception as e:
            logger.warning(f"Failed to write pov_verification.json: {e}")
            return False

    def _capture_patch_verification(self, temp_dir: Path, patch_snapshot) -> bool:
        """Capture patch verification snapshot data.

        Args:
            temp_dir: Temporary directory for snapshot contents
            patch_snapshot: PatchSnapshot from patch verification manager

        Returns:
            True if patch verification data was captured, False otherwise
        """
        if patch_snapshot is None:
            return False

        try:
            patch_data = {
                "cycle": patch_snapshot.cycle,
                "timestamp": patch_snapshot.timestamp,
                "elapsed_time": patch_snapshot.elapsed_time,
                "harness_name": patch_snapshot.harness_name,
                "cpvs_with_patches": patch_snapshot.cpvs_with_patches,
                "patches_total": patch_snapshot.patches_total,
                "patches_new": patch_snapshot.patches_new,
                "input_cpvs_total": patch_snapshot.input_cpvs_total,
            }

            patch_path = temp_dir / "patch_verification.json"
            patch_path.write_text(json.dumps(patch_data, indent=2))
            logger.debug(
                f"Captured patch verification: patches={patch_snapshot.patches_total}, "
                f"new={patch_snapshot.patches_new}"
            )
            return True
        except Exception as e:
            logger.warning(f"Failed to write patch_verification.json: {e}")
            return False
