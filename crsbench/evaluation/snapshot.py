"""Snapshot data structures and utilities for CRSBench.

This module provides data structures and utility functions for working with
trial snapshots - periodic captures of CRS trial progress.

Snapshots enable:
- Progress monitoring during long-running trials
- Early debugging and issue detection
- Resource usage tracking over time
- Partial result recovery from interrupted trials
"""

from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Optional, Any
import json
import tarfile
import logging

logger = logging.getLogger(__name__)


@dataclass
class SnapshotMetadata:
    """Metadata for a single snapshot.

    Attributes:
        cycle: Snapshot cycle number (1-indexed)
        timestamp: Unix timestamp when snapshot was captured
        elapsed_time: Seconds elapsed since trial start
        snapshot_period: Configured snapshot interval in seconds
    """
    cycle: int
    timestamp: float
    elapsed_time: float
    snapshot_period: int

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'SnapshotMetadata':
        """Create from dictionary loaded from JSON."""
        return cls(
            cycle=data['cycle'],
            timestamp=data['timestamp'],
            elapsed_time=data['elapsed_time'],
            snapshot_period=data['snapshot_period']
        )

    def to_json(self) -> str:
        """Serialize to JSON string."""
        return json.dumps(self.to_dict(), indent=2)

    @classmethod
    def from_json(cls, json_str: str) -> 'SnapshotMetadata':
        """Deserialize from JSON string."""
        return cls.from_dict(json.loads(json_str))


@dataclass
class SnapshotSummary:
    """Summary information about a snapshot archive.

    Attributes:
        cycle: Snapshot cycle number
        archive_path: Path to snapshot tar.gz file
        is_complete: Whether snapshot has completion marker
        file_count: Number of files in snapshot (None if not inspected)
        archive_size_bytes: Size of tar.gz archive in bytes
        metadata: Snapshot metadata (None if not loaded)
    """
    cycle: int
    archive_path: Path
    is_complete: bool
    file_count: Optional[int] = None
    archive_size_bytes: Optional[int] = None
    metadata: Optional[SnapshotMetadata] = None


def is_snapshot_complete(trial_dir: Path, cycle: int) -> bool:
    """Check if a snapshot has a completion marker.

    Args:
        trial_dir: Trial output directory
        cycle: Snapshot cycle number

    Returns:
        True if completion marker exists, False otherwise
    """
    marker_path = trial_dir / f"snapshot-{cycle:04d}.complete"
    return marker_path.exists()


def get_snapshot_archive_path(trial_dir: Path, cycle: int) -> Path:
    """Get path to snapshot archive for given cycle.

    Args:
        trial_dir: Trial output directory
        cycle: Snapshot cycle number

    Returns:
        Path to snapshot tar.gz file
    """
    return trial_dir / f"snapshot-{cycle:04d}.tar.gz"


def get_completion_marker_path(trial_dir: Path, cycle: int) -> Path:
    """Get path to completion marker for given cycle.

    Args:
        trial_dir: Trial output directory
        cycle: Snapshot cycle number

    Returns:
        Path to completion marker file
    """
    return trial_dir / f"snapshot-{cycle:04d}.complete"


def list_snapshots(trial_dir: Path) -> List[SnapshotSummary]:
    """List all snapshots in a trial directory.

    Args:
        trial_dir: Trial output directory

    Returns:
        List of SnapshotSummary objects, sorted by cycle number
    """
    if not trial_dir.exists():
        return []

    snapshots = []

    # Find all snapshot archives
    for archive_path in sorted(trial_dir.glob("snapshot-*.tar.gz")):
        # Extract cycle number from filename
        try:
            # snapshot-0001.tar.gz -> 0001
            name_without_gz = archive_path.name.replace('.tar.gz', '')
            cycle = int(Path(name_without_gz).stem.split('-')[1])
        except (IndexError, ValueError) as e:
            logger.warning(f"Invalid snapshot filename: {archive_path.name}: {e}")
            continue

        # Check completion status
        is_complete = is_snapshot_complete(trial_dir, cycle)

        # Get file size
        archive_size = archive_path.stat().st_size if archive_path.exists() else None

        snapshots.append(SnapshotSummary(
            cycle=cycle,
            archive_path=archive_path,
            is_complete=is_complete,
            archive_size_bytes=archive_size
        ))

    return snapshots


def load_snapshot_metadata(archive_path: Path) -> Optional[SnapshotMetadata]:
    """Load metadata from a snapshot archive.

    Args:
        archive_path: Path to snapshot tar.gz file

    Returns:
        SnapshotMetadata object, or None if metadata cannot be loaded
    """
    try:
        with tarfile.open(archive_path, 'r:gz') as tar:
            # Try to extract metadata.json
            try:
                metadata_member = tar.getmember("metadata.json")
                metadata_file = tar.extractfile(metadata_member)
                if metadata_file:
                    metadata_json = metadata_file.read().decode('utf-8')
                    return SnapshotMetadata.from_json(metadata_json)
            except KeyError:
                logger.warning(f"No metadata.json in snapshot: {archive_path}")
                return None
    except Exception as e:
        logger.error(f"Failed to load metadata from {archive_path}: {e}")
        return None


def inspect_snapshot(archive_path: Path) -> Optional[SnapshotSummary]:
    """Inspect a snapshot archive and return detailed summary.

    Args:
        archive_path: Path to snapshot tar.gz file

    Returns:
        SnapshotSummary with file count and metadata, or None on error
    """
    if not archive_path.exists():
        return None

    try:
        # Extract cycle from filename
        name_without_gz = archive_path.name.replace('.tar.gz', '')
        cycle = int(Path(name_without_gz).stem.split('-')[1])
    except (IndexError, ValueError) as e:
        logger.error(f"Invalid snapshot filename: {archive_path.name}: {e}")
        return None

    trial_dir = archive_path.parent
    is_complete = is_snapshot_complete(trial_dir, cycle)
    archive_size = archive_path.stat().st_size

    # Load metadata
    metadata = load_snapshot_metadata(archive_path)

    # Count files in archive
    file_count = None
    try:
        with tarfile.open(archive_path, 'r:gz') as tar:
            members = tar.getmembers()
            # Count only files, not directories
            file_count = len([m for m in members if m.isfile()])
    except Exception as e:
        logger.warning(f"Failed to count files in {archive_path}: {e}")

    return SnapshotSummary(
        cycle=cycle,
        archive_path=archive_path,
        is_complete=is_complete,
        file_count=file_count,
        archive_size_bytes=archive_size,
        metadata=metadata
    )


def extract_snapshot(archive_path: Path, extract_dir: Path) -> bool:
    """Extract a snapshot archive to a directory.

    Args:
        archive_path: Path to snapshot tar.gz file
        extract_dir: Directory to extract files to

    Returns:
        True if extraction succeeded, False otherwise
    """
    try:
        extract_dir.mkdir(parents=True, exist_ok=True)

        with tarfile.open(archive_path, 'r:gz') as tar:
            tar.extractall(path=extract_dir)

        logger.info(f"Extracted snapshot to {extract_dir}")
        return True

    except Exception as e:
        logger.error(f"Failed to extract snapshot {archive_path}: {e}")
        return False


def validate_snapshot_structure(archive_path: Path) -> tuple[bool, List[str]]:
    """Validate that a snapshot archive has required structure.

    Args:
        archive_path: Path to snapshot tar.gz file

    Returns:
        Tuple of (is_valid, error_messages)
    """
    errors = []

    if not archive_path.exists():
        errors.append(f"Snapshot archive does not exist: {archive_path}")
        return False, errors

    try:
        with tarfile.open(archive_path, 'r:gz') as tar:
            member_names = [m.name for m in tar.getmembers()]

            # Check for required files
            required_files = ["metadata.json"]
            for req_file in required_files:
                if req_file not in member_names:
                    errors.append(f"Missing required file: {req_file}")

            # Validate metadata if present
            if "metadata.json" in member_names:
                metadata = load_snapshot_metadata(archive_path)
                if metadata is None:
                    errors.append("Failed to parse metadata.json")
                else:
                    # Validate metadata fields
                    if metadata.cycle < 1:
                        errors.append(f"Invalid cycle number: {metadata.cycle}")
                    if metadata.elapsed_time < 0:
                        errors.append(f"Invalid elapsed_time: {metadata.elapsed_time}")
                    if metadata.snapshot_period < 60 and metadata.snapshot_period != 0:
                        errors.append(f"Invalid snapshot_period: {metadata.snapshot_period}")

    except Exception as e:
        errors.append(f"Failed to read archive: {e}")

    return len(errors) == 0, errors
