"""Coverage manager for periodic coverage collection during CRS trials.

This module provides CoverageManager, which runs in a separate thread
during CRS execution to periodically collect and track code coverage.

Threading Model:
- Main thread: Runs CRS subprocess
- Manager thread: Periodic coverage collection (event-based)

The manager thread is daemon=True, ensuring Python doesn't wait for it
indefinitely if shutdown takes too long.

Integration:
- Works with SnapshotManager for synchronized coverage snapshots
- Uses CoverageCollector for actual coverage data collection
- Stores coverage data using CoverageStore
"""

import threading
import time
from pathlib import Path
from typing import Optional

from crsbench.evaluation.coverage.collector import CoverageCollector
from crsbench.evaluation.coverage.models import (
    CoverageConfig,
    CoverageSnapshot,
    CoverageSummary,
)
from crsbench.evaluation.coverage.store import CoverageStore
from crsbench.utils.logger import get_logger

logger = get_logger(__name__)


class CoverageManager:
    """Manages periodic coverage collection during CRS trial execution.

    This class runs in a separate thread and periodically collects
    coverage data, creates snapshots, and detects coverage saturation.

    Attributes:
        trial_dir: Trial output directory containing CRS outputs
        collector: Coverage collector for processing corpus files
        config: Coverage configuration
        snapshots: List of captured coverage snapshots
    """

    def __init__(
        self,
        trial_dir: Path,
        collector: CoverageCollector,
        config: CoverageConfig,
        harness_name: str,
        corpus_dir: Path,
        *,
        trial_start_time: Optional[float] = None,
        store: Optional[CoverageStore] = None,
    ):
        """Initialize coverage manager.

        Args:
            trial_dir: Trial output directory (must exist)
            collector: Coverage collector instance
            config: Coverage configuration
            harness_name: Name of the harness being fuzzed
            corpus_dir: Directory containing corpus files to monitor
            trial_start_time: Trial start timestamp (defaults to current time)
            store: Optional CoverageStore for persistence (creates new if None)

        Raises:
            ValueError: If trial_dir doesn't exist
        """
        if not trial_dir.exists():
            raise ValueError(f"trial_dir does not exist: {trial_dir}")

        self.trial_dir = trial_dir
        self.collector = collector
        self.config = config
        self.harness_name = harness_name
        self.corpus_dir = corpus_dir
        self.trial_start_time = trial_start_time or time.time()

        # Coverage store for persistence
        coverage_store_dir = trial_dir / "coverage"
        self.store = store or CoverageStore(coverage_store_dir)

        # State tracking
        self.snapshots: list[CoverageSnapshot] = []
        self.running = False
        self.shutdown_event = threading.Event()

        # Time-based saturation detection state
        self._last_new_coverage_time = self.trial_start_time
        self._last_lines_covered = 0
        self._saturation_detected = False

        logger.info(
            f"CoverageManager initialized: trial_dir={trial_dir}, "
            f"harness={harness_name}, corpus_dir={corpus_dir}"
        )

    def run(self):
        """Main coverage collection loop (runs in separate thread).

        This method should be called in a separate thread via:
            thread = threading.Thread(target=manager.run, daemon=True)
            thread.start()

        The loop will:
        1. Wait for next collection cycle
        2. Process new corpus files
        3. Collect coverage data
        4. Repeat until stopped

        The loop respects shutdown_event for quick termination.
        """
        self.running = True
        logger.info("Coverage manager thread started")

        try:
            while self.running:
                # Check for shutdown
                if self.shutdown_event.is_set():
                    break

                # Process new corpus files
                try:
                    new_count = self.collector.process_new_corpus(self.corpus_dir)
                    if new_count > 0:
                        logger.debug(f"Processed {new_count} new corpus files")
                except Exception as e:
                    logger.error(f"Coverage collection failed: {e}", exc_info=True)

                # Sleep briefly to avoid busy-waiting (100ms check interval)
                if self.shutdown_event.wait(0.1):
                    break

        finally:
            self.running = False
            self._save_final_state()
            logger.info(
                f"Coverage manager thread stopped "
                f"(captured {len(self.snapshots)} snapshots)"
            )

    def stop(self):
        """Signal coverage manager thread to stop.

        This method should be called from the main thread when CRS completes.
        After calling stop(), join the thread with a timeout:
            manager.stop()
            thread.join(timeout=5.0)
        """
        logger.info("Stopping coverage manager")
        self.running = False
        self.shutdown_event.set()

    def on_snapshot(self, cycle: int) -> CoverageSnapshot:
        """Create coverage snapshot for a given cycle.

        This method is called by SnapshotManager before creating a snapshot
        archive. It captures the current coverage state and checks for
        saturation using time-based detection.

        Args:
            cycle: Snapshot cycle number (1-indexed)

        Returns:
            CoverageSnapshot with current coverage state
        """
        timestamp = time.time()
        elapsed_time = timestamp - self.trial_start_time

        # Get current summary from collector
        try:
            summary = self.collector.get_summary()
        except Exception as e:
            logger.warning(f"Failed to get coverage summary: {e}")
            summary = CoverageSummary(metric=self.config.metric)

        # Calculate new lines since last snapshot
        new_lines_count = summary.lines_covered - self._last_lines_covered

        # Calculate new corpus since last snapshot
        last_corpus_count = (
            self.snapshots[-1].summary.corpus_total if self.snapshots else 0
        )
        new_corpus_count = summary.corpus_total - last_corpus_count

        # Update time-based saturation tracking
        if new_lines_count > 0:
            self._last_new_coverage_time = timestamp

        # Check for saturation using time-based detection
        saturation_detected = self.check_saturation(timestamp)

        # Create snapshot
        snapshot = CoverageSnapshot(
            cycle=cycle,
            timestamp=timestamp,
            elapsed_time=elapsed_time,
            harness_name=self.harness_name,
            summary=summary,
            saturation_detected=saturation_detected,
            new_corpus_count=max(0, new_corpus_count),
            new_lines_count=max(0, new_lines_count),
        )

        # Update state
        self._last_lines_covered = summary.lines_covered
        self.snapshots.append(snapshot)

        logger.info(
            f"Coverage snapshot {cycle}: "
            f"lines={summary.lines_covered}/{summary.lines_total} "
            f"({summary.lines_percent:.1f}%), "
            f"new_lines={new_lines_count}, "
            f"saturation={saturation_detected}"
        )

        return snapshot

    def check_saturation(self, current_time: Optional[float] = None) -> bool:
        """Check if coverage has saturated based on time without new coverage.

        Saturation is detected when no new lines have been covered
        for saturation_time seconds.

        Args:
            current_time: Current timestamp (defaults to time.time())

        Returns:
            True if saturation is detected, False otherwise
        """
        if self._saturation_detected:
            return True

        if current_time is None:
            current_time = time.time()

        # Calculate time since last new coverage
        time_since_new_coverage = current_time - self._last_new_coverage_time

        if time_since_new_coverage >= self.config.saturation_time:
            logger.info(
                f"Coverage saturation detected: "
                f"no new coverage for {time_since_new_coverage:.0f}s "
                f"(threshold={self.config.saturation_time}s)"
            )
            self._saturation_detected = True
            return True

        return False

    def get_current_summary(self) -> CoverageSummary:
        """Get current coverage summary.

        Returns:
            Current CoverageSummary from collector, or empty summary on error
        """
        try:
            return self.collector.get_summary()
        except Exception as e:
            logger.warning(f"Failed to get coverage summary: {e}")
            return CoverageSummary(metric=self.config.metric)

    def is_saturated(self) -> bool:
        """Check if coverage saturation has been detected.

        Returns:
            True if saturation was detected, False otherwise
        """
        return self._saturation_detected

    def _save_final_state(self):
        """Save final coverage state to disk."""
        try:
            coverage_file = self.trial_dir / "coverage" / "coverage_store.json"
            self.store.save(coverage_file)
            logger.debug(f"Saved coverage store to {coverage_file}")
        except Exception as e:
            logger.warning(f"Failed to save coverage store: {e}")
