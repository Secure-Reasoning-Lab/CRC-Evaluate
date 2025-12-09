"""Snapshot manager for periodic CRS trial progress capture.

This module implements SnapshotManager, which runs in a separate thread
during CRS execution to periodically capture trial progress.

Threading model:
- Main thread: Runs CRS subprocess
- Snapshot thread: Periodic snapshot capture (sleep-based timing)

The snapshot thread is daemon=True, ensuring Python doesn't wait for it
indefinitely if shutdown takes too long.
"""

import json
from crsbench.utils.logger import get_logger
import shutil
import tarfile
import threading
import time
from pathlib import Path
from typing import Set, Optional

from crsbench.evaluation.snapshot import SnapshotMetadata

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

    def __init__(self, trial_dir: Path, snapshot_period: int, trial_start_time: Optional[float] = None, crs_output_dir: Optional[Path] = None):
        """Initialize snapshot manager.

        Args:
            trial_dir: Trial output directory (must exist)
            snapshot_period: Snapshot interval in seconds (must be > 0)
            trial_start_time: Trial start timestamp (defaults to current time)
            crs_output_dir: Optional CRS output directory (for oss-bugfind-crs workaround)

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
        self.crs_output_dir = crs_output_dir

        # State tracking
        self.cycle = 0
        self.running = False
        self.shutdown_event = threading.Event()

        # Incremental tracking sets
        self.captured_povs: Set[str] = set()
        self.captured_patches: Set[str] = set()
        self.last_corpus_mtime = 0.0

        logger.info(f"SnapshotManager initialized: period={snapshot_period}s, trial_dir={trial_dir}, crs_output_dir={crs_output_dir}")

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
                    logger.error(f"Snapshot {self.cycle + 1} failed: {e}", exc_info=True)

        finally:
            self.running = False
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

    def capture_snapshot(self):
        """Capture a single snapshot.

        This method:
        1. Creates temporary directory for snapshot contents
        2. Captures metadata, config, logs, POVs, patches, corpus
        3. Compresses to tar.gz archive
        4. Writes completion marker
        5. Cleans up temporary directory

        Errors are logged but not raised (allows continuing to next snapshot).
        """
        self.cycle += 1
        elapsed_time = time.time() - self.trial_start_time

        logger.info(f"Capturing snapshot {self.cycle} (elapsed: {elapsed_time:.1f}s)")

        # Create temp directory for snapshot contents
        temp_dir = self.trial_dir / f".snapshot-{self.cycle:04d}"
        temp_dir.mkdir(exist_ok=True)

        try:
            # Capture all snapshot data
            self._capture_metadata(temp_dir, elapsed_time)
            self._capture_config(temp_dir)
            self._capture_execution_metadata(temp_dir)
            self._capture_llm_usage(temp_dir)
            self._capture_crs_log(temp_dir)
            self._capture_povs(temp_dir)
            self._capture_patches(temp_dir)
            self._capture_corpus(temp_dir)
            self._capture_crs_data(temp_dir)

            # Compress to tar.gz
            archive_path = self.trial_dir / f"snapshot-{self.cycle:04d}.tar.gz"
            self._create_tar_gz(temp_dir, archive_path)

            # Mark complete
            marker_path = self.trial_dir / f"snapshot-{self.cycle:04d}.complete"
            marker_path.touch()

            logger.info(f"Snapshot {self.cycle} completed: {archive_path.name}")

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

    def _get_output_dir(self) -> Path:
        """Get the CRS output directory.

        Returns crs_output_dir if provided (for oss-bugfind-crs workaround),
        otherwise falls back to trial_dir/output/ (canonical location for future).

        Returns:
            Path to CRS output directory
        """
        if self.crs_output_dir and self.crs_output_dir.exists():
            return self.crs_output_dir
        return self.trial_dir / "output"

    def _capture_metadata(self, temp_dir: Path, elapsed_time: float):
        """Capture snapshot metadata."""
        metadata = SnapshotMetadata(
            cycle=self.cycle,
            timestamp=time.time(),
            elapsed_time=elapsed_time,
            snapshot_period=self.snapshot_period
        )

        metadata_path = temp_dir / "metadata.json"
        metadata_path.write_text(metadata.to_json())

    def _capture_config(self, temp_dir: Path):
        """Capture experiment config (full - static)."""
        config_path = self.trial_dir / "config.yaml"
        if config_path.exists():
            shutil.copy2(config_path, temp_dir / "config.yaml")

    def _capture_execution_metadata(self, temp_dir: Path):
        """Capture execution metadata (full - static)."""
        exec_path = self.trial_dir / "execution.json"
        if exec_path.exists():
            shutil.copy2(exec_path, temp_dir / "execution.json")

    def _capture_llm_usage(self, temp_dir: Path):
        """Capture LLM usage log (full - cumulative)."""
        llm_path = self.trial_dir / "llm-usage.json"
        if llm_path.exists():
            try:
                shutil.copy2(llm_path, temp_dir / "llm-usage.json")
            except Exception as e:
                logger.warning(f"Failed to capture llm-usage.json: {e}")

    def _capture_crs_log(self, temp_dir: Path):
        """Capture CRS output log (full - complete log)."""
        log_path = self.trial_dir / "crs-output.log"
        if log_path.exists():
            try:
                shutil.copy2(log_path, temp_dir / "crs-output.log")
            except Exception as e:
                logger.warning(f"Failed to capture crs-output.log: {e}")

    def _capture_povs(self, temp_dir: Path):
        """Capture POVs (incremental - only new POVs)."""
        output_dir = self._get_output_dir()
        pov_dir = output_dir / "povs"

        if not pov_dir.exists():
            return

        # Find new POVs (not yet captured)
        new_povs = []
        try:
            for pov_file in pov_dir.iterdir():
                if pov_file.is_file() and pov_file.name not in self.captured_povs:
                    new_povs.append(pov_file)
        except Exception as e:
            logger.warning(f"Failed to list POVs: {e}")
            return

        if not new_povs:
            return

        # Copy new POVs
        snapshot_pov_dir = temp_dir / "povs"
        snapshot_pov_dir.mkdir(exist_ok=True)

        for pov_file in new_povs:
            try:
                shutil.copy2(pov_file, snapshot_pov_dir / pov_file.name)
                self.captured_povs.add(pov_file.name)
            except Exception as e:
                logger.warning(f"Failed to capture POV {pov_file.name}: {e}")

        logger.debug(f"Captured {len(new_povs)} new POV(s)")

    def _capture_patches(self, temp_dir: Path):
        """Capture patches (incremental - only new patches, organized by POV ID)."""
        output_dir = self._get_output_dir()
        patches_dir = output_dir / "patches"

        if not patches_dir.exists():
            return

        # Find new patches (organized in pov_N/ subdirectories)
        new_patches = []
        try:
            for pov_subdir in patches_dir.iterdir():
                if not pov_subdir.is_dir():
                    continue

                for patch_file in pov_subdir.iterdir():
                    if patch_file.is_file():
                        # Track by relative path: pov_0/patch.diff
                        rel_path = f"{pov_subdir.name}/{patch_file.name}"
                        if rel_path not in self.captured_patches:
                            new_patches.append((pov_subdir.name, patch_file))
        except Exception as e:
            logger.warning(f"Failed to list patches: {e}")
            return

        if not new_patches:
            return

        # Copy new patches with directory structure
        snapshot_patches_dir = temp_dir / "patches"
        snapshot_patches_dir.mkdir(exist_ok=True)

        for pov_id, patch_file in new_patches:
            try:
                pov_patch_dir = snapshot_patches_dir / pov_id
                pov_patch_dir.mkdir(exist_ok=True)
                shutil.copy2(patch_file, pov_patch_dir / patch_file.name)
                self.captured_patches.add(f"{pov_id}/{patch_file.name}")
            except Exception as e:
                logger.warning(f"Failed to capture patch {pov_id}/{patch_file.name}: {e}")

        logger.debug(f"Captured {len(new_patches)} new patch(es)")

    def _capture_corpus(self, temp_dir: Path):
        """Capture corpus files (incremental - new/modified files by mtime)."""
        output_dir = self._get_output_dir()
        corpus_dir = output_dir / "corpus"

        if not corpus_dir.exists():
            return

        # Find new/modified corpus files (by modification time)
        new_corpus = []
        try:
            for corpus_file in corpus_dir.iterdir():
                if corpus_file.is_file():
                    if corpus_file.stat().st_mtime > self.last_corpus_mtime:
                        new_corpus.append(corpus_file)
        except Exception as e:
            logger.warning(f"Failed to list corpus: {e}")
            return

        if not new_corpus:
            return

        # Copy new corpus files
        snapshot_corpus_dir = temp_dir / "corpus"
        snapshot_corpus_dir.mkdir(exist_ok=True)

        for corpus_file in new_corpus:
            try:
                shutil.copy2(corpus_file, snapshot_corpus_dir / corpus_file.name)
                # Update last mtime
                file_mtime = corpus_file.stat().st_mtime
                if file_mtime > self.last_corpus_mtime:
                    self.last_corpus_mtime = file_mtime
            except Exception as e:
                logger.warning(f"Failed to capture corpus {corpus_file.name}: {e}")

        logger.debug(f"Captured {len(new_corpus)} corpus file(s)")

    def _capture_crs_data(self, temp_dir: Path):
        """Capture CRS-specific data (incremental - by mtime, optional)."""
        output_dir = self._get_output_dir()
        crs_data_dir = output_dir / "crs-data"

        if not crs_data_dir.exists():
            return

        # Copy entire crs-data directory (CRS-specific, no deduplication)
        snapshot_crs_data_dir = temp_dir / "crs-data"

        try:
            shutil.copytree(crs_data_dir, snapshot_crs_data_dir, dirs_exist_ok=True)
            logger.debug("Captured crs-data directory")
        except Exception as e:
            logger.warning(f"Failed to capture crs-data: {e}")

    def _create_tar_gz(self, source_dir: Path, archive_path: Path):
        """Compress snapshot directory to tar.gz.

        Args:
            source_dir: Directory containing snapshot files
            archive_path: Path to output tar.gz file
        """
        with tarfile.open(archive_path, 'w:gz') as tar:
            for item in source_dir.rglob('*'):
                if item.is_file():
                    arcname = item.relative_to(source_dir)
                    tar.add(item, arcname=arcname)
