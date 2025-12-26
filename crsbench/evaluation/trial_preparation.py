"""Trial directory preparation for CRS execution.

This module provides functionality to create isolated directory structures
for each trial execution, including:
- Trial directory structure creation
- Source code preparation (cloning at specific commits)
- Hints directory preparation with filtering
- POVs directory preparation for patch generation
- Metadata generation and storage
"""

import json
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

from crsbench.utils import run_git
from crsbench.utils.logger import get_logger

logger = get_logger(__name__)


class TrialPreparationError(Exception):
    """Raised when trial preparation fails."""


class SourceCloneError(TrialPreparationError):
    """Raised when source code cloning fails."""


class HintsPreparationError(TrialPreparationError):
    """Raised when hints preparation fails."""


class POVsPreparationError(TrialPreparationError):
    """Raised when POVs preparation fails."""


@dataclass
class TrialPreparationResult:
    """Result of trial directory preparation."""

    trial_dir: Optional[Path]
    build_dir: Optional[Path]
    source_path: Optional[Path]
    output_dir: Optional[Path]
    hints_dir: Optional[Path]
    povs_dir: Optional[Path]
    metadata: Dict[str, Any]
    success: bool
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert result to dictionary."""
        return {
            "trial_dir": str(self.trial_dir) if self.trial_dir else None,
            "build_dir": str(self.build_dir) if self.build_dir else None,
            "source_path": str(self.source_path) if self.source_path else None,
            "output_dir": str(self.output_dir) if self.output_dir else None,
            "hints_dir": str(self.hints_dir) if self.hints_dir else None,
            "povs_dir": str(self.povs_dir) if self.povs_dir else None,
            "metadata": self.metadata,
            "success": self.success,
            "error": self.error,
        }


class TrialDirectoryPreparer:
    """Prepares isolated directory structure for CRS trial execution."""

    def __init__(
        self,
        experiment_dir: Path,
        benchmarks_root: Path,
        oss_fuzz_dir: Path,
        config: Dict[str, Any],
    ):
        """
        Initialize trial directory preparer.

        Args:
            experiment_dir: Root directory for experiment
            benchmarks_root: Path to benchmarks directory
            oss_fuzz_dir: Path to oss-fuzz submodule
            config: Experiment configuration
        """
        self.experiment_dir = Path(experiment_dir)
        self.benchmarks_root = Path(benchmarks_root)
        self.oss_fuzz_dir = Path(oss_fuzz_dir)
        self.config = config

    def prepare_trial(
        self,
        crs: str,
        benchmark: str,
        harness: str,
        trial_num: int,
        mode: str = "bug_finding",
    ) -> TrialPreparationResult:
        """
        Prepare complete trial directory structure.

        Args:
            crs: CRS name
            benchmark: Benchmark name
            harness: Harness name
            trial_num: Trial number
            mode: "bug_finding" or "patch_generation"

        Returns:
            TrialPreparationResult with all prepared paths
        """
        logger.info(f"Preparing trial {trial_num} for {crs} on {benchmark}/{harness}")

        try:
            # Create trial root
            trial_dir = self._create_trial_directory(trial_num)

            # Create directory structure
            build_dir = trial_dir / "crs-build"
            output_dir = trial_dir / "output"
            build_dir.mkdir(parents=True, exist_ok=True)
            output_dir.mkdir(parents=True, exist_ok=True)

            # Prepare source code
            source_path = self._prepare_source_code(benchmark, build_dir)

            # Prepare hints (if enabled)
            hints_dir = self._prepare_hints(benchmark, trial_dir)

            # Prepare POVs (if patch generation mode)
            povs_dir = None
            if mode == "patch_generation":
                povs_dir = self._prepare_povs(benchmark, harness, trial_dir)

            # Store preparation metadata
            metadata = self._create_metadata(
                crs=crs,
                benchmark=benchmark,
                harness=harness,
                trial_num=trial_num,
                mode=mode,
                source_path=source_path,
                hints_dir=hints_dir,
                povs_dir=povs_dir,
            )
            self._write_metadata(trial_dir, metadata)

            logger.info(f"Trial preparation complete: {trial_dir}")

            return TrialPreparationResult(
                trial_dir=trial_dir,
                build_dir=build_dir,
                source_path=source_path,
                output_dir=output_dir,
                hints_dir=hints_dir,
                povs_dir=povs_dir,
                metadata=metadata,
                success=True,
            )

        except Exception as e:
            logger.error(f"Trial preparation failed: {e}", exc_info=True)
            return TrialPreparationResult(
                trial_dir=None,
                build_dir=None,
                source_path=None,
                output_dir=None,
                hints_dir=None,
                povs_dir=None,
                metadata={},
                success=False,
                error=str(e),
            )

    def prepare_trial_safe(
        self,
        crs: str,
        benchmark: str,
        harness: str,
        trial_num: int,
        mode: str = "bug_finding",
    ) -> TrialPreparationResult:
        """
        Safe version of prepare_trial that catches exceptions.

        Returns:
            TrialPreparationResult with success=False on error
        """
        return self.prepare_trial(crs, benchmark, harness, trial_num, mode)

    def _create_trial_directory(self, trial_num: int) -> Path:
        """
        Create trial root directory.

        Args:
            trial_num: Trial number

        Returns:
            Path to trial directory
        """
        # Generate trial directory name
        trial_name = f"trial-{trial_num}"

        # Create trial directory
        trial_dir = self.experiment_dir / trial_name
        trial_dir.mkdir(parents=True, exist_ok=True)

        logger.info(f"Created trial directory: {trial_dir}")
        return trial_dir

    def _prepare_source_code(self, benchmark: str, build_dir: Path) -> Path:
        """
        Clone source code at commit specified in meta.yaml.

        Args:
            benchmark: Benchmark name
            build_dir: Build directory

        Returns:
            Path to cloned source code

        Raises:
            SourceCloneError: If source cloning fails
        """
        from crsbench.utils.repo_manager import ensure_project_repository

        benchmark_dir = self.benchmarks_root / benchmark

        if not benchmark_dir.exists():
            raise SourceCloneError(f"Benchmark directory not found: {benchmark_dir}")

        # Use repository manager to clone source
        source_dest = build_dir / "src" / benchmark

        try:
            source_path = ensure_project_repository(
                benchmark_dir=str(benchmark_dir),
                project_dir=str(source_dest),
                verbose=self.config.get("verbose", False),
            )

            if not source_path:
                raise SourceCloneError(
                    f"Failed to clone source for {benchmark}. "
                    "Check project.yaml main_repo and meta.yaml commits."
                )

            logger.info(f"Prepared source code at: {source_path}")
            return Path(source_path)

        except Exception as e:
            raise SourceCloneError(f"Source preparation failed: {e}") from e

    def _prepare_hints(self, benchmark: str, trial_dir: Path) -> Optional[Path]:
        """
        Prepare hints directory by aggregating hints from all CPVs.

        New behavior:
        - Loads meta.yaml to discover ALL harnesses and CPVs
        - Aggregates SARIF hints from level_N.sarif files based on hint_sarif_level
        - Uses sequential numeric names (0.sarif, 1.sarif, ...) to avoid leaking CPV info
        - Corpus support is placeholder for future implementation

        Args:
            benchmark: Benchmark name
            trial_dir: Trial directory

        Returns:
            Path to prepared hints directory, or None if not enabled
        """
        if not self.config.get("hints_enabled", False):
            logger.debug("Hints not enabled, skipping")
            return None

        sarif_level = self.config.get("hint_sarif_level")
        corpus_level = self.config.get("hint_corpus_level")

        # Must have at least one hint type configured
        if sarif_level is None and corpus_level is None:
            logger.warning("hints_enabled=True but no hint levels configured")
            return None

        benchmark_dir = self.benchmarks_root / benchmark
        if not benchmark_dir.exists():
            logger.warning(f"Benchmark directory not found: {benchmark_dir}")
            return None

        # Load meta.yaml to discover all harnesses and CPVs
        meta_yaml_path = benchmark_dir / ".aixcc" / "meta.yaml"
        if not meta_yaml_path.exists():
            logger.warning(f"No meta.yaml found for {benchmark}")
            return None

        try:
            with meta_yaml_path.open("r") as f:
                meta = yaml.safe_load(f)
        except Exception as e:
            logger.error(f"Failed to load meta.yaml for {benchmark}: {e}")
            return None

        # Create trial hints directory
        hints_dir = trial_dir / "hints"
        hints_dir.mkdir(parents=True, exist_ok=True)

        # Aggregate SARIF hints from all CPVs
        sarif_copied = False
        if sarif_level is not None:
            sarif_copied = self._aggregate_sarif_hints(
                benchmark_dir=benchmark_dir,
                meta=meta,
                sarif_level=sarif_level,
                hints_dir=hints_dir,
            )

        # Aggregate corpus hints (not yet implemented)
        corpus_copied = False
        if corpus_level is not None:
            logger.warning("Corpus hints not yet implemented")

        if sarif_copied or corpus_copied:
            logger.info(f"Prepared hints at: {hints_dir}")
            return hints_dir
        logger.warning(f"No hints content aggregated for {benchmark}")
        return None

    def _aggregate_sarif_hints(
        self,
        benchmark_dir: Path,
        meta: Dict[str, Any],
        sarif_level: int,
        hints_dir: Path,
    ) -> bool:
        """
        Aggregate SARIF hints from all CPVs across all harnesses.

        Args:
            benchmark_dir: Benchmark directory path
            meta: Parsed meta.yaml content
            sarif_level: SARIF level to select (1-5)
            hints_dir: Destination hints directory

        Returns:
            True if any SARIF files were copied
        """
        sarif_index = 0
        harness_files = meta.get("harness_files", [])

        if not harness_files:
            logger.warning("No harness_files found in meta.yaml")
            return False

        for harness in harness_files:
            harness_name = harness.get("name")
            if not harness_name:
                logger.warning("Harness missing 'name' field, skipping")
                continue

            vulns = harness.get("vulns", [])
            for vuln in vulns:
                cpv_keyword = vuln.get("vuln_keyword")
                if not cpv_keyword:
                    logger.warning(
                        f"Vulnerability missing 'vuln_keyword' in {harness_name}, skipping"
                    )
                    continue

                # Construct path to CPV hints
                cpv_hints_dir = (
                    benchmark_dir / ".aixcc" / harness_name / cpv_keyword / "hints"
                )
                sarif_file = cpv_hints_dir / f"level_{sarif_level}.sarif"

                if sarif_file.exists():
                    # Copy with non-leaky sequential name
                    dest = hints_dir / f"{sarif_index}.sarif"
                    shutil.copy2(sarif_file, dest)
                    logger.debug(f"Copied {sarif_file} -> {dest}")
                    sarif_index += 1
                else:
                    logger.debug(f"SARIF hint not found: {sarif_file}")

        if sarif_index > 0:
            logger.info(f"Aggregated {sarif_index} SARIF hints at level {sarif_level}")
            return True
        logger.warning(f"No SARIF hints found at level {sarif_level}")
        return False

    def _prepare_povs(
        self, benchmark: str, harness: str, trial_dir: Path
    ) -> Optional[Path]:
        """
        Prepare POVs directory for patch generation.

        Args:
            benchmark: Benchmark name
            harness: Harness name
            trial_dir: Trial directory

        Returns:
            Path to prepared POVs directory, or None if no POVs
        """
        benchmark_dir = self.benchmarks_root / benchmark
        source_harness_dir = benchmark_dir / ".aixcc" / harness

        if not source_harness_dir.exists():
            logger.warning(f"No harness directory for {benchmark}/{harness}")
            return None

        # Create trial POVs directory
        povs_dir = trial_dir / "povs"
        povs_dir.mkdir(parents=True, exist_ok=True)

        try:
            # Collect POVs from all cpv_* directories
            pov_count = 0
            for cpv_dir in sorted(source_harness_dir.glob("cpv_*")):
                blobs_dir = cpv_dir / "blobs"
                if not blobs_dir.exists():
                    continue

                for pov_blob in sorted(blobs_dir.glob("*.blob")):
                    # Filter based on config
                    if self._should_include_pov(pov_blob.stem):
                        # Copy and flatten: pov_0.blob -> povs/pov_0
                        dest_name = pov_blob.stem  # Remove .blob extension
                        shutil.copy2(pov_blob, povs_dir / dest_name)
                        pov_count += 1

            if pov_count > 0:
                logger.info(f"Prepared {pov_count} POVs at: {povs_dir}")
                return povs_dir
            logger.warning(f"No POVs found for {benchmark}/{harness}")
            return None

        except Exception as e:
            logger.error(f"POVs preparation failed: {e}", exc_info=True)
            raise POVsPreparationError(f"Failed to prepare POVs: {e}") from e

    def _should_include_pov(self, pov_name: str) -> bool:
        """
        Check if POV should be included based on config.

        Args:
            pov_name: POV name (e.g., "pov_0")

        Returns:
            True if POV should be included
        """
        target_povs = self.config.get("target_povs")

        if not target_povs:
            # No filter, include all POVs
            return True

        # Check if POV is in target list
        return pov_name in target_povs

    def _create_metadata(
        self,
        crs: str,
        benchmark: str,
        harness: str,
        trial_num: int,
        mode: str,
        source_path: Path,
        hints_dir: Optional[Path],
        povs_dir: Optional[Path],
    ) -> Dict[str, Any]:
        """
        Create trial preparation metadata.

        Args:
            crs: CRS name
            benchmark: Benchmark name
            harness: Harness name
            trial_num: Trial number
            mode: Execution mode
            source_path: Path to source code
            hints_dir: Path to hints (or None)
            povs_dir: Path to POVs (or None)

        Returns:
            Metadata dictionary
        """
        # Get source commit from git
        source_commit = self._get_git_commit(source_path)

        # Count files in hints/povs
        hints_stats = self._get_hints_stats(hints_dir) if hints_dir else None
        povs_stats = self._get_povs_stats(povs_dir) if povs_dir else None

        return {
            "timestamp": datetime.now().isoformat(),
            "trial_num": trial_num,
            "crs": crs,
            "benchmark": benchmark,
            "harness": harness,
            "mode": mode,
            "source": {"path": str(source_path), "commit": source_commit},
            "hints": hints_stats,
            "povs": povs_stats,
            "config": {
                "hints_enabled": self.config.get("hints_enabled", False),
                "hints_corpus_level": self.config.get("hints_corpus_level"),
                "target_povs": self.config.get("target_povs"),
            },
        }

    def _get_git_commit(self, source_path: Path) -> Optional[str]:
        """Get current commit hash from git repository."""
        try:
            result = run_git(
                ["rev-parse", "HEAD"],
                cwd=str(source_path),
                capture_output=True,
                text=True,
                timeout=10,
            )
            return result.stdout.strip()
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            logger.warning(f"Failed to get git commit: {e}")
            return None

    def _get_hints_stats(self, hints_dir: Path) -> Dict[str, Any]:
        """Get statistics about prepared hints."""
        # Count SARIF files directly in hints_dir (no sarif/ subdir in new structure)
        sarif_count = len(list(hints_dir.glob("*.sarif")))

        # Corpus support placeholder
        corpus_dir = hints_dir / "corpus"
        corpus_count = len(list(corpus_dir.iterdir())) if corpus_dir.exists() else 0

        return {
            "path": str(hints_dir),
            "sarif_count": sarif_count,
            "corpus_count": corpus_count,
        }

    def _get_povs_stats(self, povs_dir: Path) -> Dict[str, Any]:
        """Get statistics about prepared POVs."""
        return {"path": str(povs_dir), "pov_count": len(list(povs_dir.iterdir()))}

    def _write_metadata(self, trial_dir: Path, metadata: Dict[str, Any]) -> None:
        """Write metadata to trial directory."""
        metadata_file = trial_dir / "metadata.json"
        with metadata_file.open("w") as f:
            json.dump(metadata, f, indent=2)

        logger.debug(f"Wrote trial metadata to {metadata_file}")
