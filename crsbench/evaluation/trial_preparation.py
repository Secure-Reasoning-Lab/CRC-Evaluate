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
import logging
import shutil
import subprocess
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, List

logger = logging.getLogger(__name__)


class TrialPreparationError(Exception):
    """Raised when trial preparation fails."""
    pass


class SourceCloneError(TrialPreparationError):
    """Raised when source code cloning fails."""
    pass


class HintsPreparationError(TrialPreparationError):
    """Raised when hints preparation fails."""
    pass


class POVsPreparationError(TrialPreparationError):
    """Raised when POVs preparation fails."""
    pass


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
            "error": self.error
        }


class TrialDirectoryPreparer:
    """Prepares isolated directory structure for CRS trial execution."""

    def __init__(
        self,
        experiment_dir: Path,
        benchmarks_root: Path,
        oss_fuzz_dir: Path,
        config: Dict[str, Any]
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
        mode: str = "bug_finding"
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
            trial_dir = self._create_trial_directory(crs, benchmark, trial_num)

            # Create directory structure
            build_dir = trial_dir / "build"
            output_dir = trial_dir / "output"
            build_dir.mkdir(parents=True, exist_ok=True)
            output_dir.mkdir(parents=True, exist_ok=True)

            # Prepare source code
            source_path = self._prepare_source_code(benchmark, build_dir)

            # Prepare hints (if enabled)
            hints_dir = self._prepare_hints(benchmark, harness, trial_dir)

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
                povs_dir=povs_dir
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
                success=True
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
                error=str(e)
            )

    def prepare_trial_safe(
        self,
        crs: str,
        benchmark: str,
        harness: str,
        trial_num: int,
        mode: str = "bug_finding"
    ) -> TrialPreparationResult:
        """
        Safe version of prepare_trial that catches exceptions.

        Returns:
            TrialPreparationResult with success=False on error
        """
        return self.prepare_trial(crs, benchmark, harness, trial_num, mode)

    def _create_trial_directory(
        self,
        crs: str,
        benchmark: str,
        trial_num: int
    ) -> Path:
        """
        Create trial root directory.

        Args:
            crs: CRS name
            benchmark: Benchmark name
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

    def _prepare_source_code(
        self,
        benchmark: str,
        build_dir: Path
    ) -> Path:
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
        from crsbench.migration.repo_manager import ensure_project_repository

        benchmark_dir = self.benchmarks_root / benchmark

        if not benchmark_dir.exists():
            raise SourceCloneError(f"Benchmark directory not found: {benchmark_dir}")

        # Use repository manager to clone source
        source_dest = build_dir / "src" / benchmark

        try:
            source_path = ensure_project_repository(
                benchmark_dir=str(benchmark_dir),
                project_dir=str(source_dest),
                verbose=self.config.get("verbose", False)
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

    def _prepare_hints(
        self,
        benchmark: str,
        harness: str,
        trial_dir: Path
    ) -> Optional[Path]:
        """
        Prepare hints directory with filtered content.

        Args:
            benchmark: Benchmark name
            harness: Harness name
            trial_dir: Trial directory

        Returns:
            Path to prepared hints directory, or None if not enabled
        """
        if not self.config.get("hints_enabled", False):
            logger.debug("Hints not enabled, skipping")
            return None

        benchmark_dir = self.benchmarks_root / benchmark
        source_hints = benchmark_dir / ".aixcc" / harness / "hints"

        if not source_hints.exists():
            logger.warning(f"No hints found for {benchmark}/{harness}")
            return None

        # Create trial hints directory
        hints_dir = trial_dir / "hints"
        hints_dir.mkdir(parents=True, exist_ok=True)

        try:
            # Copy SARIF files
            sarif_copied = self._copy_sarif_files(source_hints, hints_dir)

            # Copy corpus based on level
            corpus_copied = self._copy_corpus_files(source_hints, hints_dir)

            if sarif_copied or corpus_copied:
                logger.info(f"Prepared hints at: {hints_dir}")
                return hints_dir
            else:
                logger.warning(f"No hints content copied for {benchmark}/{harness}")
                return None

        except Exception as e:
            logger.error(f"Hints preparation failed: {e}", exc_info=True)
            raise HintsPreparationError(f"Failed to prepare hints: {e}") from e

    def _copy_sarif_files(
        self,
        source_hints: Path,
        hints_dir: Path
    ) -> bool:
        """
        Copy SARIF files from source hints.

        Args:
            source_hints: Source hints directory
            hints_dir: Destination hints directory

        Returns:
            True if any files copied
        """
        source_sarif = source_hints / "sarif"
        if not source_sarif.exists():
            logger.debug("No SARIF directory found in hints")
            return False

        dest_sarif = hints_dir / "sarif"
        dest_sarif.mkdir(exist_ok=True)

        copied = 0
        for sarif_file in source_sarif.glob("*.sarif"):
            shutil.copy2(sarif_file, dest_sarif)
            copied += 1

        if copied > 0:
            logger.info(f"Copied {copied} SARIF files")
        return copied > 0

    def _copy_corpus_files(
        self,
        source_hints: Path,
        hints_dir: Path
    ) -> bool:
        """
        Copy corpus files from source hints based on config level.

        Args:
            source_hints: Source hints directory
            hints_dir: Destination hints directory

        Returns:
            True if any files copied
        """
        corpus_level = self.config.get("hints_corpus_level", "1h")
        source_corpus = source_hints / "corpus" / corpus_level

        if not source_corpus.exists():
            logger.warning(f"Corpus level '{corpus_level}' not found in hints")
            return False

        dest_corpus = hints_dir / "corpus"
        dest_corpus.mkdir(exist_ok=True)

        copied = 0
        for corpus_file in source_corpus.iterdir():
            if corpus_file.is_file():
                shutil.copy2(corpus_file, dest_corpus)
                copied += 1

        if copied > 0:
            logger.info(f"Copied {copied} corpus files (level: {corpus_level})")
        return copied > 0

    def _prepare_povs(
        self,
        benchmark: str,
        harness: str,
        trial_dir: Path
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
            else:
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
        povs_dir: Optional[Path]
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
            "source": {
                "path": str(source_path),
                "commit": source_commit
            },
            "hints": hints_stats,
            "povs": povs_stats,
            "config": {
                "hints_enabled": self.config.get("hints_enabled", False),
                "hints_corpus_level": self.config.get("hints_corpus_level"),
                "target_povs": self.config.get("target_povs")
            }
        }

    def _get_git_commit(self, source_path: Path) -> Optional[str]:
        """Get current commit hash from git repository."""
        try:
            result = subprocess.run(
                ["git", "-C", str(source_path), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                check=True,
                timeout=10
            )
            return result.stdout.strip()
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            logger.warning(f"Failed to get git commit: {e}")
            return None

    def _get_hints_stats(self, hints_dir: Path) -> Dict[str, Any]:
        """Get statistics about prepared hints."""
        sarif_dir = hints_dir / "sarif"
        corpus_dir = hints_dir / "corpus"

        return {
            "path": str(hints_dir),
            "sarif_count": len(list(sarif_dir.glob("*.sarif"))) if sarif_dir.exists() else 0,
            "corpus_count": len(list(corpus_dir.iterdir())) if corpus_dir.exists() else 0
        }

    def _get_povs_stats(self, povs_dir: Path) -> Dict[str, Any]:
        """Get statistics about prepared POVs."""
        return {
            "path": str(povs_dir),
            "pov_count": len(list(povs_dir.iterdir()))
        }

    def _write_metadata(self, trial_dir: Path, metadata: Dict[str, Any]) -> None:
        """Write metadata to trial directory."""
        metadata_file = trial_dir / "metadata.json"
        with open(metadata_file, "w") as f:
            json.dump(metadata, f, indent=2)

        logger.debug(f"Wrote trial metadata to {metadata_file}")
