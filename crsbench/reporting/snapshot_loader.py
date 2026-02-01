"""Snapshot loading and trial discovery for the reporting module."""

import json
import re
import tarfile
import tempfile
from pathlib import Path

from pydantic import ValidationError

from crsbench.reporting.errors import SnapshotLoadError
from crsbench.reporting.models import (
    LLMUsageFile,
    SnapshotData,
    SnapshotMetadataFile,
    TrialInfo,
    TrialMetadataFile,
)
from crsbench.utils.logger import get_logger

logger = get_logger(__name__)


class SnapshotLoader:
    """Load and parse snapshot archives from trial directories.

    This class handles:
    - Discovering complete snapshots in trial directories
    - Extracting tar.gz archives
    - Parsing snapshot metadata and contents
    - Building SnapshotData objects

    Example:
        loader = SnapshotLoader()
        snapshots = loader.load_trial_snapshots(trial_dir)
        for snap in snapshots:
            print(f"Cycle {snap.cycle}: {snap.pov_count} POVs")
    """

    def load_trial_snapshots(self, trial_dir: Path) -> list[SnapshotData]:
        """Load all complete snapshots for a trial.

        Args:
            trial_dir: Path to trial directory containing snapshots

        Returns:
            List of SnapshotData objects sorted by cycle number
        """
        # Discover and load complete snapshots
        snapshots = []
        for archive_path in self._discover_complete_snapshots(trial_dir):
            try:
                snapshot_data = self.load_snapshot(archive_path, trial_dir)
                snapshots.append(snapshot_data)
            except SnapshotLoadError as e:
                logger.warning(f"Failed to load snapshot {archive_path}: {e}")

        # Sort by cycle number
        snapshots.sort(key=lambda s: s.cycle)

        logger.info(f"Loaded {len(snapshots)} snapshots from {trial_dir}")
        return snapshots

    def load_snapshot(self, archive_path: Path, trial_dir: Path) -> SnapshotData:
        """Load and parse a single snapshot archive.

        Args:
            archive_path: Path to snapshot tar.gz file
            trial_dir: Parent trial directory

        Returns:
            Parsed SnapshotData object

        Raises:
            SnapshotLoadError: If snapshot cannot be loaded or parsed
        """
        if not archive_path.exists():
            raise SnapshotLoadError(f"Snapshot archive not found: {archive_path}")

        # Extract to temp directory
        with tempfile.TemporaryDirectory() as tmpdir:
            extract_dir = Path(tmpdir)
            return self._extract_and_parse(archive_path, extract_dir, trial_dir)

    def _discover_complete_snapshots(self, trial_dir: Path) -> list[Path]:
        """Discover complete snapshots in trial directory.

        Args:
            trial_dir: Path to trial directory

        Returns:
            List of snapshot archive paths (only complete ones)
        """
        if not trial_dir.exists():
            return []

        snapshots = []
        for archive_path in sorted(trial_dir.glob("snapshot-*.tar.gz")):
            # Skip symlinks (e.g., snapshot-latest.tar.gz, snapshot-final.tar.gz)
            if archive_path.is_symlink():
                continue

            # Extract cycle number
            # Skip special snapshot files (final/latest are symlink-like markers)
            if archive_path.name in ("snapshot-final.tar.gz", "snapshot-latest.tar.gz"):
                continue

            try:
                cycle = self._extract_cycle_from_filename(archive_path.name)
            except ValueError:
                logger.warning(f"Invalid snapshot filename: {archive_path.name}")
                continue

            # Check completion marker
            marker = trial_dir / f"snapshot-{cycle:04d}.complete"
            if not marker.exists():
                logger.debug(f"Skipping incomplete snapshot: {archive_path}")
                continue

            snapshots.append(archive_path)

        return snapshots

    def _extract_cycle_from_filename(self, filename: str) -> int:
        """Extract cycle number from snapshot filename.

        Args:
            filename: Snapshot filename (e.g., "snapshot-0001.tar.gz")

        Returns:
            Cycle number

        Raises:
            ValueError: If filename format is invalid
        """
        match = re.match(r"snapshot-(\d+)\.tar\.gz", filename)
        if not match:
            raise ValueError(f"Invalid snapshot filename format: {filename}")
        return int(match.group(1))

    def _extract_and_parse(
        self,
        archive_path: Path,
        extract_dir: Path,
        trial_dir: Path,
    ) -> SnapshotData:
        """Extract archive and parse contents.

        Args:
            archive_path: Path to snapshot tar.gz file
            extract_dir: Directory to extract files to
            trial_dir: Parent trial directory

        Returns:
            Parsed SnapshotData object
        """
        try:
            with tarfile.open(archive_path, "r:gz") as tar:
                tar.extractall(extract_dir, filter="data")
        except Exception as e:
            raise SnapshotLoadError(
                f"Failed to extract snapshot {archive_path}: {e}"
            ) from e

        # Parse snapshot metadata
        metadata_path = extract_dir / "metadata.json"
        if not metadata_path.exists():
            raise SnapshotLoadError(
                f"Missing metadata.json in snapshot: {archive_path}"
            )

        try:
            metadata_dict = json.loads(metadata_path.read_text())
            metadata = SnapshotMetadataFile.model_validate(metadata_dict)
        except (json.JSONDecodeError, ValidationError) as e:
            raise SnapshotLoadError(
                f"Invalid metadata.json in snapshot {archive_path}: {e}"
            ) from e

        # Count and collect POV names
        povs_dir = extract_dir / "povs"
        pov_names: list[str] = []
        if povs_dir.exists():
            # POVs can be files or directories
            for item in povs_dir.iterdir():
                if not item.name.startswith("."):
                    pov_names.append(item.name)

        # Count and collect patch names
        patches_dir = extract_dir / "patches"
        patch_names: list[str] = []
        if patches_dir.exists():
            # Patches are organized by POV ID: patches/<pov_id>/patch.diff
            for item in patches_dir.iterdir():
                if item.is_dir() and not item.name.startswith("."):
                    patch_file = item / "patch.diff"
                    if patch_file.exists():
                        patch_names.append(item.name)

        # Count corpus files
        corpus_dir = extract_dir / "seeds"
        corpus_count = 0
        if corpus_dir.exists():
            corpus_count = len([f for f in corpus_dir.iterdir() if f.is_file()])

        # Parse LLM usage
        llm_usage = LLMUsageFile()
        llm_usage_path = extract_dir / "llm-usage.json"
        if llm_usage_path.exists():
            try:
                llm_dict = json.loads(llm_usage_path.read_text())
                llm_usage = LLMUsageFile.model_validate(llm_dict)
            except (json.JSONDecodeError, ValidationError) as e:
                logger.warning(f"Invalid llm-usage.json in snapshot: {e}")

        # Parse POV verification data
        cpvs_found: list[str] = []
        cpvs_remaining: list[str] = []
        early_stop_triggered = False

        pov_verification_path = extract_dir / "pov_verification.json"
        if pov_verification_path.exists():
            try:
                pov_data = json.loads(pov_verification_path.read_text())
                cpvs_found = pov_data.get("cpvs_found", [])
                cpvs_remaining = pov_data.get("cpvs_remaining", [])
                early_stop_triggered = pov_data.get("early_stop_triggered", False)
            except (json.JSONDecodeError, ValidationError) as e:
                logger.warning(f"Invalid pov_verification.json in snapshot: {e}")

        # Check for optional files
        has_config = (extract_dir / "config.yaml").exists()
        has_execution = (extract_dir / "execution.json").exists()
        has_crs_log = (extract_dir / "crs-output.log").exists()
        has_coverage = (extract_dir / "coverage.json").exists()

        return SnapshotData(
            trial_dir=trial_dir,
            cycle=metadata.cycle,
            timestamp=metadata.timestamp,
            elapsed_time=metadata.elapsed_time,
            snapshot_period=metadata.snapshot_period,
            running_elapsed_time=metadata.running_elapsed_time,
            pov_count=len(pov_names),
            patch_count=len(patch_names),
            corpus_count=corpus_count,
            pov_names=pov_names,
            patch_names=patch_names,
            llm_usage=llm_usage,
            cpvs_found=cpvs_found,
            cpvs_remaining=cpvs_remaining,
            early_stop_triggered=early_stop_triggered,
            has_config=has_config,
            has_execution_metadata=has_execution,
            has_crs_log=has_crs_log,
            has_coverage=has_coverage,
        )


def discover_trials(experiment_dir: Path) -> list[TrialInfo]:
    """Discover trials by scanning experiment directory structure.

    Supports multiple directory structures:
    1. Flat: experiment_dir/{benchmark}__{crs}/trial-{N}/
    2. Deep: experiment_dir/{crs}/{benchmark}/{harness}/{mode}/trial-{N}/

    Each trial directory contains:
        - metadata.json (trial metadata)
        - snapshot-*.tar.gz (periodic snapshots)
        - snapshot-*.complete (completion markers)

    Args:
        experiment_dir: Path to experiment directory

    Returns:
        List of TrialInfo objects with validation status
    """
    if not experiment_dir.exists():
        logger.warning(f"Experiment directory not found: {experiment_dir}")
        return []

    discovered_trials: list[TrialInfo] = []

    # Use rglob to find all trial-N directories at any depth
    for trial_dir in sorted(experiment_dir.rglob("trial-*")):
        if not trial_dir.is_dir():
            continue

        # Check for trial-N pattern
        match = re.match(r"trial-(\d+)", trial_dir.name)
        if not match:
            continue

        trial_num = int(match.group(1))
        trial_info = _load_trial_info(trial_dir, trial_num)
        discovered_trials.append(trial_info)

    logger.info(f"Discovered {len(discovered_trials)} trials in {experiment_dir}")
    return discovered_trials


def _load_trial_info(trial_dir: Path, trial_num: int) -> TrialInfo:
    """Load trial information from a trial directory.

    Requires metadata.json to be present. Returns invalid status if missing.

    Args:
        trial_dir: Path to trial directory
        trial_num: Trial number extracted from directory name

    Returns:
        TrialInfo with metadata and validation status
    """
    # Count snapshots
    all_snapshots = list(trial_dir.glob("snapshot-*.tar.gz"))
    # Filter out symlinks
    all_snapshots = [p for p in all_snapshots if not p.is_symlink()]
    snapshot_count = len(all_snapshots)

    # Count complete snapshots
    complete_count = len(list(trial_dir.glob("snapshot-*.complete")))

    # Load metadata.json (required)
    metadata_path = trial_dir / "metadata.json"

    if not metadata_path.exists():
        return TrialInfo(
            trial_dir=trial_dir,
            trial_num=trial_num,
            status="missing_metadata",
            error="metadata.json not found",
            snapshot_count=snapshot_count,
            complete_snapshot_count=complete_count,
        )

    try:
        metadata_dict = json.loads(metadata_path.read_text())
        metadata = TrialMetadataFile.model_validate(metadata_dict)

        return TrialInfo(
            trial_dir=trial_dir,
            trial_num=trial_num,
            crs=metadata.crs,
            benchmark=metadata.benchmark,
            harness=metadata.harness,
            mode=metadata.mode,
            status="valid",
            metadata=metadata,
            snapshot_count=snapshot_count,
            complete_snapshot_count=complete_count,
        )

    except json.JSONDecodeError as e:
        return TrialInfo(
            trial_dir=trial_dir,
            trial_num=trial_num,
            status="invalid_metadata",
            error=f"Invalid JSON in metadata.json: {e}",
            snapshot_count=snapshot_count,
            complete_snapshot_count=complete_count,
        )

    except ValidationError as e:
        return TrialInfo(
            trial_dir=trial_dir,
            trial_num=trial_num,
            status="invalid_metadata",
            error=f"Invalid metadata format: {e}",
            snapshot_count=snapshot_count,
            complete_snapshot_count=complete_count,
        )
