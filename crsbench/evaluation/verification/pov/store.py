"""In-memory POV store with JSON persistence.

This module provides POVStore, which tracks POV verification data,
supports deduplication based on content hash, and provides JSON persistence.

Thread Safety:
    All mutations are protected by a threading.Lock to ensure safe
    concurrent access from multiple threads (e.g., snapshot thread
    and main thread).
"""

import json
import shutil
import threading
import time
from pathlib import Path
from typing import Optional

from crsbench.evaluation.verification.models import PovVerificationStatus
from crsbench.evaluation.verification.pov.models import POVEntry
from crsbench.evaluation.verification.utils import compute_content_hash
from crsbench.utils.logger import get_logger

logger = get_logger(__name__)


class POVStore:
    """In-memory POV store with JSON persistence.

    Tracks POV verification data, determines which POVs are duplicates,
    and provides JSON persistence for saving/loading state.

    Directory Structure:
        store_dir/
        ├── pov_store.json               # Hash-to-POV mapping, CPV matches
        ├── cpvs/                        # Parent dir for CPV-specific POVs
        │   └── {cpv_id}/                # Per-CPV POV storage
        │       ├── blobs/{hash}.blob    # POV files that trigger this CPV
        │       └── crash_logs/{hash}-{variant}.log
        ├── unintended/                  # Unintended crashes (NOT_VULNERABLE, UNINTENDED_CRASH)
        │   ├── blobs/{hash}.blob
        │   └── crash_logs/{hash}-{variant}.log
        └── snapshots/                   # Per-snapshot summaries
            └── snapshot-{NNN}.json

    Thread Safety:
        All mutations are protected by threading.Lock.

    Attributes:
        store_dir: Directory for storing POV data (trial-N/povs/)
        povs: Hash-to-POVEntry mapping
        cpv_to_first_pov: Maps each CPV to discovery info {pov_hash, discovery_ts, relative_time}
        crs_run_start_time: Timestamp when CRS started running (for relative time calculation)
    """

    def __init__(self, store_dir: Path, *, crs_run_start_time: Optional[float] = None):
        """Initialize POV store.

        Args:
            store_dir: Directory for storing POV data.
                Will create subdirectories if they don't exist.
            crs_run_start_time: Timestamp when CRS started running.
                Used for calculating relative time (time since CRS start).
                Defaults to current time if not provided.
        """
        self.store_dir = store_dir
        self.povs: dict[str, POVEntry] = {}
        # Maps CPV ID to discovery info: {pov_hash, discovery_ts, relative_time}
        self.cpv_to_first_pov: dict[str, dict[str, str | float]] = {}
        self.crs_run_start_time: float = crs_run_start_time or time.time()
        self._lock = threading.Lock()

        # Create directory structure
        self._create_directories()

    def _create_directories(self) -> None:
        """Create store directory structure."""
        self.store_dir.mkdir(parents=True, exist_ok=True)
        # Create base directories
        (self.store_dir / "cpvs").mkdir(exist_ok=True)
        (self.store_dir / "unintended" / "blobs").mkdir(parents=True, exist_ok=True)
        (self.store_dir / "unintended" / "crash_logs").mkdir(exist_ok=True)
        (self.store_dir / "snapshots").mkdir(exist_ok=True)

    def _get_category_dir(
        self, status: PovVerificationStatus, cpv_matched: list[str]
    ) -> Path:
        """Get the directory for storing POV files based on status.

        Args:
            status: Verification status
            cpv_matched: List of matched CPV IDs (used for CPV status)

        Returns:
            Path to the category directory (e.g., cpvs/cpv_0, unintended)
        """
        if status == PovVerificationStatus.CPV and cpv_matched:
            # Store under first matched CPV
            cpv_id = cpv_matched[0]
            cpv_dir = self.store_dir / "cpvs" / cpv_id
            # Create CPV subdirectories if they don't exist
            (cpv_dir / "blobs").mkdir(parents=True, exist_ok=True)
            (cpv_dir / "crash_logs").mkdir(exist_ok=True)
            return cpv_dir

        # NOT_VULNERABLE, UNINTENDED_CRASH, ERROR, or other -> unintended
        return self.store_dir / "unintended"

    def add_pov(
        self,
        pov_path: Path,
        status: PovVerificationStatus,
        cpv_matched: list[str],
        verification_duration: float = 0.0,
        *,
        timestamp: Optional[float] = None,
        pov_hash: Optional[str] = None,
    ) -> tuple[str, bool]:
        """Add a POV with its verification result.

        Args:
            pov_path: Path to the POV file
            status: Verification result status
            cpv_matched: List of CPV identifiers matched
            verification_duration: Time taken to verify (seconds)
            timestamp: Optional timestamp for first_seen_ts (defaults to now)
            pov_hash: Optional pre-computed hash (avoids recomputation)

        Returns:
            Tuple of (hash, is_new) where:
                - hash: 16-character hex SHA256 hash of POV content
                - is_new: True if this POV was not previously seen
        """
        if pov_hash is None:
            pov_hash = compute_content_hash(pov_path)

        # Get file stats: size and mtime (CRS creation time)
        file_size = 0
        file_mtime: Optional[float] = None
        if pov_path.exists():
            stat = pov_path.stat()
            file_size = stat.st_size
            file_mtime = stat.st_mtime

        ts = timestamp if timestamp is not None else time.time()

        with self._lock:
            # Check if POV already exists
            if pov_hash in self.povs:
                existing = self.povs[pov_hash]
                # Keep earliest timestamp
                if ts < existing.first_seen_ts:
                    self.povs[pov_hash] = POVEntry(
                        hash=pov_hash,
                        first_seen_ts=ts,
                        file_mtime=existing.file_mtime,  # Keep original mtime
                        file_size=file_size,
                        status=existing.status,
                        cpv_matched=existing.cpv_matched,
                        crash_log_path=existing.crash_log_path,
                        verification_duration=existing.verification_duration,
                    )
                return pov_hash, False

            # Store POV entry
            self.povs[pov_hash] = POVEntry(
                hash=pov_hash,
                first_seen_ts=ts,
                file_mtime=file_mtime,
                file_size=file_size,
                status=status,
                cpv_matched=cpv_matched,
                verification_duration=verification_duration,
            )

            # Track first POV for each CPV with discovery timestamp
            for cpv_id in cpv_matched:
                if cpv_id not in self.cpv_to_first_pov:
                    self.cpv_to_first_pov[cpv_id] = {
                        "pov_hash": pov_hash,
                        "discovery_ts": ts,
                        "relative_time": ts - self.crs_run_start_time,
                    }

            # Log with relative time from CRS start
            if file_mtime is not None:
                relative_time = file_mtime - self.crs_run_start_time
                logger.debug(
                    f"Added POV {pov_hash}: status={status.value}, "
                    f"cpv_matched={cpv_matched}, "
                    f"file_mtime={file_mtime}, relative_time={relative_time:.2f}s"
                )
            else:
                logger.debug(
                    f"Added POV {pov_hash}: status={status.value}, "
                    f"cpv_matched={cpv_matched}"
                )

            return pov_hash, True

    def store_unique_pov(
        self,
        pov_path: Path,
        pov_hash: str,
        status: PovVerificationStatus,
        cpv_matched: list[str],
    ) -> Path:
        """Copy POV file to category-specific blobs directory.

        Args:
            pov_path: Path to source POV file
            pov_hash: Hash of the POV content
            status: Verification status (determines category)
            cpv_matched: List of matched CPV IDs

        Returns:
            Path to stored POV file
        """
        category_dir = self._get_category_dir(status, cpv_matched)
        dest_path = category_dir / "blobs" / f"{pov_hash}.blob"
        if not dest_path.exists():
            shutil.copy2(pov_path, dest_path)
            logger.debug(f"Stored unique POV: {dest_path}")
        return dest_path

    def store_crash_log(
        self,
        pov_hash: str,
        crash_log: str,
        status: PovVerificationStatus,
        cpv_matched: list[str],
        *,
        variant_name: Optional[str] = None,
    ) -> Path:
        """Save crash log to category-specific crash_logs directory.

        Args:
            pov_hash: Hash of the POV content
            crash_log: Crash log content
            status: Verification status (determines category)
            cpv_matched: List of matched CPV IDs
            variant_name: Optional variant name for per-variant logs

        Returns:
            Path to stored crash log file
        """
        category_dir = self._get_category_dir(status, cpv_matched)

        # Include variant name in filename if provided
        if variant_name:
            filename = f"{pov_hash}-{variant_name}.log"
        else:
            filename = f"{pov_hash}.log"

        dest_path = category_dir / "crash_logs" / filename
        dest_path.write_text(crash_log)

        # Update POV entry with crash log path (only for main log without variant)
        if not variant_name:
            # Compute relative path from store_dir
            rel_path = dest_path.relative_to(self.store_dir)
            with self._lock:
                if pov_hash in self.povs:
                    entry = self.povs[pov_hash]
                    self.povs[pov_hash] = POVEntry(
                        hash=entry.hash,
                        first_seen_ts=entry.first_seen_ts,
                        file_mtime=entry.file_mtime,
                        file_size=entry.file_size,
                        status=entry.status,
                        cpv_matched=entry.cpv_matched,
                        crash_log_path=str(rel_path),
                        verification_duration=entry.verification_duration,
                    )

        logger.debug(f"Stored crash log: {dest_path}")
        return dest_path

    def get_pov(self, pov_hash: str) -> Optional[POVEntry]:
        """Get POV entry by hash.

        Args:
            pov_hash: Hash of the POV content

        Returns:
            POVEntry or None if not found
        """
        with self._lock:
            return self.povs.get(pov_hash)

    def get_cpvs_found(self) -> set[str]:
        """Get set of CPV identifiers that have been discovered.

        Returns:
            Set of CPV identifiers
        """
        with self._lock:
            return set(self.cpv_to_first_pov.keys())

    def get_tested_hashes(self) -> set[str]:
        """Get set of POV hashes that have been tested.

        This is used for pre-verification deduplication via VerificationEngine's
        skip_hashes parameter.

        Returns:
            Set of POV content hashes
        """
        with self._lock:
            return set(self.povs.keys())

    def is_already_tested(self, pov_path: Path) -> bool:
        """Check if a POV has already been tested.

        Args:
            pov_path: Path to the POV file

        Returns:
            True if the POV hash is already in the store
        """
        _, is_tested = self.check_pov_hash(pov_path)
        return is_tested

    def check_pov_hash(self, pov_path: Path) -> tuple[str, bool]:
        """Compute hash and check if POV has already been tested.

        This method combines hash computation with the existence check
        to avoid computing the hash twice.

        Args:
            pov_path: Path to the POV file

        Returns:
            Tuple of (pov_hash, is_already_tested) where:
                - pov_hash: Content hash of the POV file
                - is_already_tested: True if POV is already in store
        """
        pov_hash = compute_content_hash(pov_path)
        with self._lock:
            return pov_hash, pov_hash in self.povs

    def get_stats(self) -> dict:
        """Get store statistics.

        Returns:
            Dictionary with statistics:
                - total_povs: Total unique POVs
                - cpvs_found: Number of CPVs discovered
                - unintended_crashes: Number of unintended crash POVs
                - errors: Number of error status POVs
        """
        with self._lock:
            stats = {
                "total_povs": len(self.povs),
                "cpvs_found": len(self.cpv_to_first_pov),
                "unintended_crashes": 0,
                "errors": 0,
            }
            for entry in self.povs.values():
                if entry.status == PovVerificationStatus.UNINTENDED_CRASH:
                    stats["unintended_crashes"] += 1
                elif entry.status == PovVerificationStatus.ERROR:
                    stats["errors"] += 1
            return stats

    def save(self, path: Optional[Path] = None) -> None:
        """Persist store state to JSON file.

        Args:
            path: Path to output JSON file. If None, uses default location.
        """
        if path is None:
            path = self.store_dir / "pov_store.json"

        with self._lock:
            data = {
                "crs_run_start_time": self.crs_run_start_time,
                "povs": {h: p.model_dump(mode="json") for h, p in self.povs.items()},
                "cpv_to_first_pov": dict(self.cpv_to_first_pov),
            }

        # Write outside lock to minimize lock duration
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2))
        logger.debug(f"Saved POV store to {path}")

    def load(self, path: Optional[Path] = None) -> None:
        """Load store state from JSON file.

        Args:
            path: Path to input JSON file. If None, uses default location.

        Raises:
            FileNotFoundError: If the file doesn't exist
            json.JSONDecodeError: If the file contains invalid JSON
        """
        if path is None:
            path = self.store_dir / "pov_store.json"

        data = json.loads(path.read_text())

        with self._lock:
            # Load CRS run start time
            if "crs_run_start_time" in data:
                self.crs_run_start_time = data["crs_run_start_time"]

            # Load POV entries
            self.povs = {}
            for h, p_data in data.get("povs", {}).items():
                self.povs[h] = POVEntry(**p_data)

            # Load CPV to first POV mapping
            self.cpv_to_first_pov = dict(data.get("cpv_to_first_pov", {}))

        logger.debug(f"Loaded POV store from {path}")
