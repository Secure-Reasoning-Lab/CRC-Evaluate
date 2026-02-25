"""Patch verification manager for real-time patch tracking during CRS evaluation.

This module provides PatchVerificationManager, which monitors patch output during
bug-fixing CRS evaluation and tracks patch discovery progress.

Threading Model:
- Main thread: Runs CRS subprocess
- Manager: Event-based patch tracking via on_snapshot callback

Integration:
- Works with SnapshotManager for synchronized patch tracking snapshots
- Tracks patches in both supported layouts:
  - output/patches/{cpv_id}/patch.diff
  - output/patches/*.diff (flat; mapped to trial target CPV by runner)

Note: Follows POVVerificationManager pattern. Unlike POVVerificationManager,
this manager only tracks patch discovery - full verification (build, POV test,
unit test) is done post-evaluation by PatchVerificationEngine.
"""

import json
import threading
import time
from pathlib import Path
from typing import Optional

from crsbench.evaluation.verification.patch.models import (
    PatchDiscoveryReport,
    PatchSnapshot,
)
from crsbench.utils.logger import get_logger

logger = get_logger(__name__)


class PatchVerificationManager:
    """Manages real-time patch tracking during bug-fixing CRS evaluation.

    This class monitors patch output directories and tracks patch discovery
    progress. Unlike POVVerificationManager, it does not perform real-time
    verification - that is done post-evaluation by PatchVerificationEngine.

    Follows POVVerificationManager pattern for consistency.

    TODO: Currently this manager only counts patch files without verification.
    Consider whether real-time patch verification (build, POV test) should be
    added here, similar to how POVVerificationManager verifies POVs in real-time.
    This would enable early-stop conditions (e.g., stop when valid patch found).

    Attributes:
        trial_dir: Trial output directory containing CRS outputs
        patch_output_dir: Directory where CRS writes patches (output/patches/)
        harness_name: Name of the harness being evaluated
        benchmark_id: Benchmark identifier
        input_cpvs_total: Number of input CPVs to generate patches for
    """

    def __init__(
        self,
        trial_dir: Path,
        patch_output_dir: Path,
        harness_name: str,
        benchmark_id: str,
        input_cpvs_total: int,
        *,
        trial_start_time: Optional[float] = None,
        exchange_patch_dir: Optional[Path] = None,
        target_cpv_id: Optional[str] = None,
    ):
        """Initialize patch verification manager.

        Args:
            trial_dir: Trial output directory (must exist)
            patch_output_dir: Directory where CRS writes patches
            harness_name: Name of the harness being evaluated
            benchmark_id: Benchmark identifier
            input_cpvs_total: Number of input CPVs to generate patches for
            trial_start_time: Trial start timestamp (defaults to current time)
            exchange_patch_dir: Pre-resolved EXCHANGE_DIR patches path for real-time scanning
            target_cpv_id: Target CPV ID for flat patch layouts (per-CPV trials)

        Raises:
            ValueError: If trial_dir doesn't exist
        """
        if not trial_dir.exists():
            raise ValueError(f"trial_dir does not exist: {trial_dir}")

        self.trial_dir = trial_dir
        self.patch_output_dir = patch_output_dir
        self.harness_name = harness_name
        self.benchmark_id = benchmark_id
        self.input_cpvs_total = input_cpvs_total
        self.trial_start_time = trial_start_time or time.time()
        self._target_cpv_id = target_cpv_id

        # Thread-safe access to state
        self._lock = threading.Lock()

        # Tracking state
        self._discovered_cpv_ids: set[str] = set()  # CPV IDs with patches
        self._discovered_patch_keys: set[str] = set()  # Logical patch keys
        self._patches_total = 0
        self._snapshot_count = 0
        self._latest_snapshot: Optional[PatchSnapshot] = None

        # EXCHANGE_DIR scanning for real-time patch discovery during CRS execution
        self._exchange_patch_dir = exchange_patch_dir

        logger.info(
            f"PatchVerificationManager initialized: trial_dir={trial_dir}, "
            f"harness={harness_name}, benchmark={benchmark_id}, "
            f"input_cpvs={input_cpvs_total}"
        )

    @property
    def cpvs_with_patches(self) -> list[str]:
        """Get list of CPV IDs with patches discovered."""
        with self._lock:
            return sorted(self._discovered_cpv_ids)

    @property
    def patches_total(self) -> int:
        """Get total number of patches discovered."""
        with self._lock:
            return self._patches_total

    @staticmethod
    def _scan_patch_directory(
        directory: Path,
        target_cpv_id: Optional[str],
    ) -> list[tuple[str, str, Path]]:
        """Scan a directory for patch files.

        Supports:
        - Structured layout: {cpv_id}/patch.diff
        - Flat layout: *.diff (mapped to target_cpv_id when available)

        Args:
            directory: Directory to scan for patch subdirectories.
            target_cpv_id: Target CPV for flat patch files.

        Returns:
            List of (cpv_id, logical_patch_key, patch_file_path) tuples.
        """
        if not directory.exists():
            return []

        results: list[tuple[str, str, Path]] = []

        # Flat layout: output/patches/*.diff
        flat_files = sorted(
            entry
            for entry in directory.iterdir()
            if entry.is_file() and entry.suffix == ".diff"
        )
        if flat_files:
            flat_cpv_id = target_cpv_id or "unknown"
            for patch_file in flat_files:
                # Use filename as logical key so exchange/output duplicates dedupe.
                results.append((flat_cpv_id, patch_file.name, patch_file))

        # Structured layout: output/patches/{cpv_id}/patch.diff
        for cpv_dir in directory.iterdir():
            if not cpv_dir.is_dir():
                continue
            patch_file = cpv_dir / "patch.diff"
            if patch_file.exists():
                # Logical key by relative path to dedupe exchange/output duplicates.
                results.append((cpv_dir.name, f"{cpv_dir.name}/patch.diff", patch_file))
        return results

    def _discover_new_patches(self) -> tuple[list[str], int]:
        """Discover new patches in output and EXCHANGE_DIR directories.

        Scans both the primary ``patch_output_dir`` (populated by
        ``collect_results()`` after CRS execution) and the EXCHANGE_DIR
        ``patch/`` subdirectory (populated in real-time by the exchange
        sidecar during CRS execution).

        Patches may be organized as:
        - {dir}/{cpv_id}/patch.diff
        - {dir}/*.diff

        Returns:
            Tuple of (new_cpv_ids, new_patches_count)
        """
        # Collect patches from both directories
        patch_entries = self._scan_patch_directory(
            self.patch_output_dir, self._target_cpv_id
        )

        if self._exchange_patch_dir is not None:
            patch_entries.extend(
                self._scan_patch_directory(
                    self._exchange_patch_dir, self._target_cpv_id
                )
            )

        new_cpv_ids = []
        new_patches_count = 0

        for cpv_id, patch_key, _patch_file in patch_entries:
            with self._lock:
                if patch_key in self._discovered_patch_keys:
                    continue

                self._discovered_patch_keys.add(patch_key)
                if cpv_id not in self._discovered_cpv_ids:
                    self._discovered_cpv_ids.add(cpv_id)
                    new_cpv_ids.append(cpv_id)

                    self._patches_total += 1
                else:
                    self._patches_total += 1
                new_patches_count += 1

        return new_cpv_ids, new_patches_count

    def on_snapshot(self, cycle: int) -> PatchSnapshot:
        """Create patch tracking snapshot for a given cycle.

        This method is called by SnapshotManager before creating a snapshot
        archive. It captures the current patch discovery state.

        Thread-safe: Uses internal lock to protect snapshot state.

        Args:
            cycle: Snapshot cycle number (1-indexed)

        Returns:
            PatchSnapshot with current discovery state
        """
        timestamp = time.time()
        elapsed_time = timestamp - self.trial_start_time

        # Discover new patches
        new_cpv_ids, patches_new = self._discover_new_patches()

        # Log new discoveries
        for cpv_id in new_cpv_ids:
            logger.info(f"Patch discovered: cpv_id={cpv_id}")

        # Create snapshot
        with self._lock:
            snapshot = PatchSnapshot(
                cycle=cycle,
                timestamp=timestamp,
                elapsed_time=elapsed_time,
                harness_name=self.harness_name,
                cpvs_with_patches=sorted(self._discovered_cpv_ids),
                patches_total=self._patches_total,
                patches_new=patches_new,
                input_cpvs_total=self.input_cpvs_total,
            )
            self._snapshot_count += 1
            self._latest_snapshot = snapshot

        # Save snapshot to file
        self._save_snapshot_file(snapshot)
        self._save_snapshot_history()

        logger.info(
            f"Patch snapshot {cycle}: "
            f"patches={self._patches_total}/{self.input_cpvs_total} "
            f"(+{patches_new})"
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
                "input_cpvs_total": self.input_cpvs_total,
                "cpvs_with_patches": sorted(self._discovered_cpv_ids),
                "patches_total": self._patches_total,
                "snapshot_count": self._snapshot_count,
            }

    def get_report(self) -> PatchDiscoveryReport:
        """Generate final patch discovery report.

        Returns:
            PatchDiscoveryReport with final discovery results
        """
        total_duration = time.time() - self.trial_start_time

        with self._lock:
            return PatchDiscoveryReport(
                benchmark_id=self.benchmark_id,
                harness_name=self.harness_name,
                input_cpvs_total=self.input_cpvs_total,
                cpvs_with_patches=sorted(self._discovered_cpv_ids),
                patches_total=self._patches_total,
                total_duration_seconds=total_duration,
                snapshot_count=self._snapshot_count,
                latest_snapshot=self._latest_snapshot,
            )

    def _save_snapshot_file(self, snapshot: PatchSnapshot) -> None:
        """Save individual snapshot to file.

        Args:
            snapshot: PatchSnapshot to save
        """
        try:
            snapshots_dir = self.trial_dir / "patches" / "snapshots"
            snapshots_dir.mkdir(parents=True, exist_ok=True)

            snapshot_file = snapshots_dir / f"snapshot-{snapshot.cycle:03d}.json"
            snapshot_data = {
                "cycle": snapshot.cycle,
                "timestamp": snapshot.timestamp,
                "elapsed_time": snapshot.elapsed_time,
                "harness_name": snapshot.harness_name,
                "cpvs_with_patches": snapshot.cpvs_with_patches,
                "patches_total": snapshot.patches_total,
                "patches_new": snapshot.patches_new,
                "input_cpvs_total": snapshot.input_cpvs_total,
            }

            snapshot_file.write_text(json.dumps(snapshot_data, indent=2))
            logger.debug(f"Saved patch snapshot to {snapshot_file}")
        except Exception as e:
            logger.warning(f"Failed to save patch snapshot file: {e}")

    def _save_snapshot_history(self) -> None:
        """Save snapshot summary to trial directory."""
        try:
            history_file = self.trial_dir / "patches" / "snapshot_history.json"
            history_file.parent.mkdir(parents=True, exist_ok=True)

            with self._lock:
                latest = self._latest_snapshot
                history_data = {
                    "harness_name": self.harness_name,
                    "input_cpvs_total": self.input_cpvs_total,
                    "snapshot_count": self._snapshot_count,
                    "latest_snapshot": {
                        "cycle": latest.cycle,
                        "elapsed_time": latest.elapsed_time,
                        "cpvs_with_patches": latest.cpvs_with_patches,
                        "patches_total": latest.patches_total,
                        "patches_new": latest.patches_new,
                    }
                    if latest
                    else None,
                }

            history_file.write_text(json.dumps(history_data, indent=2))
            logger.debug(f"Saved patch snapshot history to {history_file}")
        except Exception as e:
            logger.warning(f"Failed to save patch snapshot history: {e}")
