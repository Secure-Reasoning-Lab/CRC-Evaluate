"""Snapshot loading and trial discovery for the reporting module."""

import json
import re
import tarfile
import tempfile
from pathlib import Path

from pydantic import ValidationError

from crsbench.evaluation.trial_paths import (
    TrialDir,
)
from crsbench.evaluation.verification.patch.models import PatchSnapshot
from crsbench.reporting.errors import SnapshotLoadError
from crsbench.reporting.models import (
    LLMUsageFile,
    SnapshotData,
    SnapshotMetadataFile,
    TrialInfo,
    TrialMetadataFile,
)
from crsbench.utils.logger import get_logger
from crsbench.validation.schemas import TrialMode

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

        logger.debug(f"Loaded {len(snapshots)} snapshots from {trial_dir}")
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

    def _trial_has_post_trial_coverage(self, trial_dir: Path) -> bool:
        return any(
            path.exists()
            for path in (
                trial_dir / "coverage" / "coverage_timeline.json",
                trial_dir / "final_coverage.json",
            )
        )

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

        # Count and collect patch identities using the same layouts accepted by patch verification: top-level flat *.diff files and CPV-scoped patches/<cpv_id>/*.diff files.
        patches_dir = extract_dir / "patches"
        patch_names: list[str] = []
        if patches_dir.exists():
            for patch_file in sorted(patches_dir.glob("*.diff")):
                if patch_file.is_file() and not patch_file.name.startswith("."):
                    patch_names.append(patch_file.name)

            for cpv_dir in sorted(patches_dir.iterdir()):
                if not cpv_dir.is_dir() or cpv_dir.name.startswith("."):
                    continue
                for patch_file in sorted(cpv_dir.glob("*.diff")):
                    if patch_file.is_file() and not patch_file.name.startswith("."):
                        patch_names.append(
                            patch_file.relative_to(patches_dir).as_posix()
                        )

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

        # Parse patch discovery data. The patch files may not have reached the collected output directory yet when a periodic snapshot is captured, so this metadata is authoritative for cumulative discovery counts.
        patches_total: int | None = None
        patches_new: int | None = None
        cpvs_with_patches: list[str] = []
        input_cpvs_total: int | None = None

        patch_verification_path = extract_dir / "patch_verification.json"
        if patch_verification_path.exists():
            try:
                patch_data = json.loads(patch_verification_path.read_text())
                patch_snapshot = PatchSnapshot.model_validate(patch_data)
                patches_total = patch_snapshot.patches_total
                patches_new = patch_snapshot.patches_new
                cpvs_with_patches = patch_snapshot.cpvs_with_patches
                input_cpvs_total = patch_snapshot.input_cpvs_total
            except (json.JSONDecodeError, ValidationError) as e:
                logger.warning(f"Invalid patch_verification.json in snapshot: {e}")

        # Check for optional files
        has_config = (extract_dir / "config.yaml").exists()
        has_execution = (extract_dir / "execution.json").exists()
        has_crs_log = (extract_dir / "crs-output.log").exists()
        has_coverage = (extract_dir / "coverage.json").exists() or (
            self._trial_has_post_trial_coverage(trial_dir)
        )

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
            patches_total=patches_total,
            patches_new=patches_new,
            cpvs_with_patches=cpvs_with_patches,
            input_cpvs_total=input_cpvs_total,
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
    has_success_marker = (trial_dir / ".success").exists()
    has_fail_marker = (trial_dir / ".fail").exists()
    execution_status = (
        "success"
        if has_success_marker
        else ("failed" if has_fail_marker else "incomplete")
    )

    if not metadata_path.exists():
        return TrialInfo(
            trial_dir=trial_dir,
            trial_num=trial_num,
            status="missing_metadata",
            error="metadata.json not found",
            has_success_marker=has_success_marker,
            has_fail_marker=has_fail_marker,
            execution_status=execution_status,
            snapshot_count=snapshot_count,
            complete_snapshot_count=complete_count,
        )

    try:
        metadata_dict = json.loads(metadata_path.read_text())
        metadata = TrialMetadataFile.model_validate(metadata_dict)
        reeval_ready, reeval_reason = _evaluate_reeval_readiness(
            trial_dir, metadata.mode
        )

        return TrialInfo(
            trial_dir=trial_dir,
            trial_num=trial_num,
            crs=metadata.crs,
            benchmark=metadata.benchmark,
            harness=metadata.harness,
            mode=metadata.mode,
            status="valid",
            metadata=metadata,
            has_success_marker=has_success_marker,
            has_fail_marker=has_fail_marker,
            execution_status=execution_status,
            reeval_ready=reeval_ready,
            reeval_reason=reeval_reason,
            snapshot_count=snapshot_count,
            complete_snapshot_count=complete_count,
        )

    except json.JSONDecodeError as e:
        return TrialInfo(
            trial_dir=trial_dir,
            trial_num=trial_num,
            status="invalid_metadata",
            error=f"Invalid JSON in metadata.json: {e}",
            has_success_marker=has_success_marker,
            has_fail_marker=has_fail_marker,
            execution_status=execution_status,
            snapshot_count=snapshot_count,
            complete_snapshot_count=complete_count,
        )

    except ValidationError as e:
        return TrialInfo(
            trial_dir=trial_dir,
            trial_num=trial_num,
            status="invalid_metadata",
            error=f"Invalid metadata format: {e}",
            has_success_marker=has_success_marker,
            has_fail_marker=has_fail_marker,
            execution_status=execution_status,
            snapshot_count=snapshot_count,
            complete_snapshot_count=complete_count,
        )


def _evaluate_reeval_readiness(trial_dir: Path, mode: TrialMode) -> tuple[bool, str]:
    """Return whether trial has required outputs for re-eval.

    Readiness checks are mode-specific and intentionally shared from trial
    discovery so downstream tools (re-eval/reporting) can use a single
    classification result.
    """
    output_dir = trial_dir / "output"
    if not output_dir.exists():
        return False, "missing output directory"

    if mode == TrialMode.bug_finding:
        pov_dir = TrialDir(trial_dir).output_povs
        if not pov_dir.exists():
            return False, "missing output/povs directory"

        has_pov_file = any(
            p.is_file() and not p.name.startswith(".") for p in pov_dir.iterdir()
        )
        if not has_pov_file:
            return False, "no POV files in output/povs"
        return True, "ready"

    if mode == TrialMode.patch_generation:
        patch_dir = TrialDir(trial_dir).output_patches
        if not patch_dir.exists():
            return False, "missing output/patches directory"

        has_patch = any(
            p.is_file() and not p.name.startswith(".")
            for p in patch_dir.rglob("*.diff")
        )
        if not has_patch:
            return False, "no patch diff files in output/patches"

        input_povs_dir = TrialDir(trial_dir).input_povs
        if not input_povs_dir.exists():
            return False, "missing crs-input/povs directory"
        if TrialDir(trial_dir).count_visible_input_povs() == 0:
            return False, "no POV files in crs-input/povs"
        return True, "ready"

    # Unknown mode: treat as not re-evaluable by default.
    return False, f"unsupported trial mode: {mode.value}"
