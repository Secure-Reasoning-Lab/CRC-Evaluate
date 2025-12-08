"""CRS Patch CRS executor implementation.

This module implements the CRSPatchExecutor class which integrates with
CRS Patch's interface for patch generation using pre-cloned source repositories.
"""

import json
from crsbench.utils.logger import get_logger
import os
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any, Set

from crsbench.evaluation.crs_executor import CRSExecutor, CRSResult
from crsbench.evaluation.results import POVResult, POVStatus
from crsbench.validation.schemas import HarnessFile, POV

logger = get_logger(__name__)


class CRSPatchExecutor(CRSExecutor):
    """CRS executor for patch generation using CRS Patch interface.

    This executor uses CRS Patch's alternative build method with pre-cloned
    source repositories via repository manager integration.
    """

    def __init__(
        self,
        crs_config_name: str,
        crs_patch_path: Path,
        oss_fuzz_path: Path,
        litellm_base: str,
        litellm_key: str,
        benchmarks_root: Path
    ):
        """Initialize CRS Patch executor.

        Args:
            crs_config_name: CRS configuration name (e.g., "multi-retrieval")
            crs_patch_path: Path to crs-patch repository
            oss_fuzz_path: Path to oss-fuzz repository (required for infrastructure)
            litellm_base: LiteLLM API base URL
            litellm_key: LiteLLM API key
            benchmarks_root: Path to benchmarks directory (for finding benchmark dirs)
        """
        self.crs_config_name = crs_config_name
        self.crs_patch_path = crs_patch_path
        self.oss_fuzz_path = oss_fuzz_path
        self.litellm_base = litellm_base
        self.litellm_key = litellm_key
        self.benchmarks_root = benchmarks_root
        self.config: Dict[str, Any] = {}
        self.built_projects: Set[str] = set()

    def configure_crs(self, config: Dict[str, Any]) -> None:
        """Configure the CRS with given parameters.

        Args:
            config: CRS configuration parameters
        """
        self.config = config.copy()
        logger.info(f"Configured CRS Patch executor with: {config}")

    def run_crs(
        self,
        benchmark_path: Path,
        harness: HarnessFile,
        trial_output_dir: Path,
        base_commit: str,
        ref_commit: Optional[str] = None
    ) -> CRSResult:
        """Run patch generation CRS.

        Args:
            benchmark_path: Path to benchmark directory
            harness: Harness configuration
            trial_output_dir: Directory for this trial's outputs
            base_commit: Base commit for evaluation
            ref_commit: Optional reference commit

        Returns:
            CRSResult with execution details
        """
        project_name = self._extract_project_name(benchmark_path)
        logger.info(f"Running CRS Patch CRS for project '{project_name}', harness '{harness.name}'")

        # Build if needed (pass benchmark_path for repo manager integration)
        self._build_crs_if_needed(benchmark_path, project_name)

        # Prepare base output directory (CRS creates subdirectories)
        self._prepare_output_directory(trial_output_dir)

        harness_name = Path(harness.name).stem

        # Build command
        cmd = [
            "oss-bugfix-crs", "run",
            self.crs_config_name, project_name,
            "--harness", harness_name,
            "--output", str(trial_output_dir / "output"),
            "--litellm-base", self.litellm_base,
            "--litellm-key", self.litellm_key
        ]

        # Prepare and add POVs directory
        povs_path = self._prepare_povs(benchmark_path, harness_name, trial_output_dir)
        if povs_path:
            cmd.extend(["--povs", str(povs_path)])
            logger.info(f"Using prepared POVs from {povs_path}")

        # Prepare and add hints if enabled
        hints_path = self._prepare_hints(benchmark_path, harness_name, trial_output_dir)
        if hints_path:
            cmd.extend(["--hints", str(hints_path)])
            logger.info(f"Using prepared hints from {hints_path}")

        logger.info(f"Executing: {' '.join(cmd)}")

        start_time = time.time()
        try:
            result = subprocess.run(
                cmd,
                cwd=self.crs_patch_path,
                capture_output=True,
                text=True,
                timeout=self.config.get("run_timeout", 3600)
            )
            execution_time = time.time() - start_time

            # Store execution metadata for reproducibility
            self._store_execution_metadata(
                trial_output_dir=trial_output_dir,
                cmd=cmd,
                hints_path=hints_path,
                povs_path=povs_path,
                execution_time=execution_time,
                returncode=result.returncode
            )

            return CRSResult(
                harness_name=harness.name,
                execution_time=execution_time,
                success=(result.returncode == 0),
                output=result.stdout,
                error=result.stderr if result.returncode != 0 else None
            )

        except subprocess.TimeoutExpired as e:
            execution_time = time.time() - start_time
            logger.error(f"CRS execution timed out after {execution_time:.1f}s")

            self._store_execution_metadata(
                trial_output_dir=trial_output_dir,
                cmd=cmd,
                hints_path=hints_path,
                povs_path=povs_path,
                execution_time=execution_time,
                returncode=-1
            )

            return CRSResult(
                harness_name=harness.name,
                execution_time=execution_time,
                success=False,
                output=str(e.stdout) if e.stdout else "",
                error=f"Execution timed out after {execution_time:.1f}s"
            )

        except Exception as e:
            execution_time = time.time() - start_time
            logger.error(f"CRS execution failed: {e}")

            return CRSResult(
                harness_name=harness.name,
                execution_time=execution_time,
                success=False,
                output="",
                error=str(e)
            )

    def process_pov_results(
        self,
        crs_result: CRSResult,
        harness: HarnessFile,
        trial_output_dir: Path
    ) -> List[POVResult]:
        """Process patch generation results.

        Args:
            crs_result: CRS execution result
            harness: Harness configuration
            trial_output_dir: Directory containing CRS outputs

        Returns:
            List of POVResult objects for each expected POV
        """
        pov_results = []

        if not harness.povs:
            return pov_results

        if not crs_result.success:
            # Mark all POVs as ERROR if CRS execution failed
            for pov in harness.povs:
                pov_results.append(POVResult(
                    name=pov.id,
                    harness_name=harness.name,
                    sanitizer=pov.sanitizer,
                    error_token=pov.error_token,
                    status=POVStatus.ERROR,
                    error_message=crs_result.error,
                    crs_output=crs_result.output
                ))
            return pov_results

        # Collect generated patches from output directory
        patches = self._collect_patches(trial_output_dir)

        # For each POV, check if patch was generated
        for pov in harness.povs:
            # Check if patch exists for this POV
            # Note: Full validation would require applying patch and re-testing
            # For now, we check if patch file exists
            has_patch = pov.id in patches

            status = POVStatus.FOUND if has_patch else POVStatus.MISSED

            pov_results.append(POVResult(
                name=pov.id,
                harness_name=harness.name,
                sanitizer=pov.sanitizer,
                error_token=pov.error_token,
                status=status,
                execution_time=crs_result.execution_time / len(harness.povs),
                crs_output=crs_result.output
            ))

        return pov_results

    def _build_crs_if_needed(self, benchmark_path: Path, project_name: str) -> None:
        """Build patch generation CRS if not already built.

        Args:
            benchmark_path: Path to benchmark directory (contains project.yaml)
            project_name: Project name for caching
        """
        build_key = f"{self.crs_config_name}:{project_name}"

        if build_key in self.built_projects:
            logger.info(f"CRS already built for {build_key}")
            return

        # Use repository manager to ensure source code exists
        from crsbench.utils.repo_manager import ensure_project_repository

        logger.info(f"Ensuring source repository for {project_name}...")
        source_path = ensure_project_repository(
            benchmark_dir=str(benchmark_path),
            verbose=self.config.get("verbose", False)
        )

        if not source_path:
            raise RuntimeError(
                f"Failed to obtain source code for {project_name}. "
                "Check that project.yaml has valid main_repo or provide source manually."
            )

        logger.info(f"Using source from: {source_path}")

        cmd = [
            "oss-bugfix-crs", "build",
            self.crs_config_name, project_name,
            "--oss-fuzz", str(self.oss_fuzz_path),
            "--project-path", str(benchmark_path),  # Benchmark dir (OSS-Fuzz compatible)
            "--source-path", str(source_path)        # Pre-cloned source from repo manager
        ]

        env = os.environ.copy()
        env["OSS_FUZZ_HOME"] = str(self.oss_fuzz_path)

        logger.info(f"Building CRS: {' '.join(cmd)}")

        try:
            result = subprocess.run(
                cmd,
                cwd=self.crs_patch_path,
                env=env,
                capture_output=True,
                text=True,
                timeout=self.config.get("build_timeout", 600)
            )

            if result.returncode != 0:
                logger.error(f"CRS build failed: {result.stderr}")
                raise RuntimeError(f"Patch CRS build failed: {result.stderr}")

            self.built_projects.add(build_key)
            logger.info(f"Successfully built CRS for {project_name}")

        except subprocess.TimeoutExpired:
            logger.error("CRS build timed out")
            raise RuntimeError("CRS build timed out")

    def _prepare_output_directory(self, trial_output_dir: Path) -> None:
        """Prepare output directory before CRS execution.

        Args:
            trial_output_dir: Trial-specific output directory

        Note:
            Only creates the base output directory. CRS is responsible for
            creating subdirectories (povs/, patches/, corpus/, crs-data/)
            according to the naming convention agreement.
        """
        output_dir = trial_output_dir / "output"
        output_dir.mkdir(parents=True, exist_ok=True)
        logger.debug(f"Prepared output directory: {output_dir}")

    def _prepare_povs(
        self,
        benchmark_path: Path,
        harness_name: str,
        trial_output_dir: Path
    ) -> Optional[Path]:
        """Prepare POVs directory with filtered POVs from benchmark.

        Creates trial-specific POVs directory and copies selected POV blobs
        based on experiment configuration.

        Args:
            benchmark_path: Path to benchmark directory
            harness_name: Name of the harness
            trial_output_dir: Trial-specific output directory

        Returns:
            Path to prepared POVs directory, or None if no POVs available

        Process:
            1. Create trial_output_dir/povs/ directory
            2. Find all POV blobs from benchmark .aixcc/<harness>/cpv_*/blobs/
            3. Flatten structure (pov_0, pov_1, pov_2 directly in povs/)
            4. Filter based on experiment config (target_povs list)
        """
        # Source POVs from benchmark
        source_harness_dir = benchmark_path / ".aixcc" / harness_name
        if not source_harness_dir.exists():
            logger.warning(f"No .aixcc directory found for harness {harness_name}")
            return None

        # Create trial-specific POVs directory
        povs_dir = trial_output_dir / "povs"
        povs_dir.mkdir(parents=True, exist_ok=True)

        # Collect POVs from all cpv_* directories
        pov_count = 0
        for cpv_dir in sorted(source_harness_dir.glob("cpv_*")):
            blobs_dir = cpv_dir / "blobs"
            if not blobs_dir.exists():
                continue

            for pov_blob in sorted(blobs_dir.glob("*.blob")):
                # Filter based on config if specified
                if self.config.get("target_povs"):
                    if pov_blob.stem not in self.config["target_povs"]:
                        continue

                # Copy and flatten: pov_0.blob -> povs/pov_0
                dest_name = pov_blob.stem  # Remove .blob extension
                shutil.copy2(pov_blob, povs_dir / dest_name)
                pov_count += 1

        if pov_count == 0:
            logger.warning(f"No POV blobs found for harness {harness_name}")
            return None

        logger.info(f"Prepared {pov_count} POVs for {harness_name}")
        return povs_dir

    def _prepare_hints(
        self,
        benchmark_path: Path,
        harness_name: str,
        trial_output_dir: Path
    ) -> Optional[Path]:
        """Prepare hints directory with filtered content from benchmark.

        Creates trial-specific hints directory and copies selected content
        based on experiment configuration.

        Args:
            benchmark_path: Path to benchmark directory
            harness_name: Name of the harness
            trial_output_dir: Trial-specific output directory

        Returns:
            Path to prepared hints directory, or None if hints not available

        Process:
            1. Create trial_output_dir/hints/ directory
            2. Copy SARIF files from benchmark .aixcc/<harness>/hints/sarif/
            3. Copy corpus based on config (1h or 1d) from .aixcc/<harness>/hints/corpus/{1h,1d}/
            4. Filter based on experiment configuration
        """
        if not self.config.get("hints_enabled", False):
            return None

        # Source hints from benchmark
        source_hints = benchmark_path / ".aixcc" / harness_name / "hints"
        if not source_hints.exists():
            logger.warning(f"No hints directory found for harness {harness_name}")
            return None

        # Create trial-specific hints directory
        hints_dir = trial_output_dir / "hints"
        hints_dir.mkdir(parents=True, exist_ok=True)

        # Copy SARIF files
        source_sarif = source_hints / "sarif"
        if source_sarif.exists():
            dest_sarif = hints_dir / "sarif"
            dest_sarif.mkdir(exist_ok=True)
            sarif_count = 0
            for sarif_file in source_sarif.glob("*.sarif"):
                shutil.copy2(sarif_file, dest_sarif)
                sarif_count += 1
            logger.debug(f"Copied {sarif_count} SARIF files")

        # Copy corpus based on configured level (1h or 1d)
        corpus_level = self.config.get("hints_corpus_level", "1h")
        source_corpus = source_hints / "corpus" / corpus_level
        if source_corpus.exists():
            dest_corpus = hints_dir / "corpus"
            dest_corpus.mkdir(exist_ok=True)
            corpus_count = 0
            for corpus_file in source_corpus.iterdir():
                shutil.copy2(corpus_file, dest_corpus)
                corpus_count += 1
            logger.debug(f"Copied {corpus_count} corpus files from {corpus_level}")

        logger.info(f"Prepared hints for {harness_name}")
        return hints_dir

    def _store_execution_metadata(
        self,
        trial_output_dir: Path,
        cmd: List[str],
        hints_path: Optional[Path],
        povs_path: Optional[Path],
        execution_time: float,
        returncode: int
    ) -> None:
        """Store execution metadata for reproducibility.

        Args:
            trial_output_dir: Trial-specific output directory
            cmd: Command executed
            hints_path: Path to prepared hints (or None)
            povs_path: Path to prepared POVs (or None)
            execution_time: Execution duration in seconds
            returncode: Process exit code

        Writes execution.json with:
            - Timestamp
            - Exact command run
            - Hints preparation details (enabled, path, corpus level, file counts)
            - POVs preparation details (provided, path, count)
            - Execution timing and result
            - CRS configuration
        """
        metadata = {
            "timestamp": datetime.now().isoformat(),
            "command": cmd,
            "crs_config": self.config.copy(),
            "hints": {
                "enabled": hints_path is not None,
                "path": str(hints_path) if hints_path else None,
                "corpus_level": self.config.get("hints_corpus_level") if hints_path else None,
                "sarif_count": len(list((hints_path / "sarif").glob("*.sarif"))) if hints_path and (hints_path / "sarif").exists() else 0,
                "corpus_count": len(list((hints_path / "corpus").iterdir())) if hints_path and (hints_path / "corpus").exists() else 0,
            },
            "povs": {
                "provided": povs_path is not None,
                "path": str(povs_path) if povs_path else None,
                "count": len(list(povs_path.iterdir())) if povs_path and povs_path.exists() else 0,
            },
            "execution": {
                "duration_seconds": execution_time,
                "returncode": returncode,
                "success": returncode == 0,
            },
        }

        execution_file = trial_output_dir / "execution.json"
        with open(execution_file, "w") as f:
            json.dump(metadata, f, indent=2)

        logger.debug(f"Stored execution metadata to {execution_file}")

    def _collect_patches(self, trial_output_dir: Path) -> Dict[str, str]:
        """Collect generated patches from output directory.

        Args:
            trial_output_dir: Trial-specific output directory

        Returns:
            Dict mapping POV ID to patch content
        """
        patches_dir = trial_output_dir / "output" / "patches"

        patches = {}
        if patches_dir.exists():
            # Collect patches organized by POV ID: patches/<pov_id>/patch.diff
            for pov_dir in patches_dir.iterdir():
                if pov_dir.is_dir():
                    patch_file = pov_dir / "patch.diff"
                    if patch_file.exists():
                        patches[pov_dir.name] = patch_file.read_text()
                        logger.debug(f"Collected patch for {pov_dir.name}")

        logger.info(f"Collected {len(patches)} patches from {patches_dir}")
        return patches

    def _extract_project_name(self, benchmark_path: Path) -> str:
        """Extract OSS-Fuzz project name from benchmark path.

        Args:
            benchmark_path: Path to benchmark directory

        Returns:
            Project name

        Example:
            benchmarks/json-c -> json-c
        """
        return benchmark_path.name

    def _resolve_crs_config_dir(self) -> Path:
        """Resolve CRS configuration directory.

        Returns:
            Path to CRS config directory (absolute)

        Raises:
            RuntimeError: If config directory not found
        """
        # Check if full path provided
        config_path = Path(self.crs_config_name)
        if config_path.is_absolute() and config_path.exists():
            return config_path

        # Look in crses/ directory
        crses_dir = Path(__file__).parent.parent.parent / "crses"
        config_dir = crses_dir / "configs" / self.crs_config_name

        if not config_dir.exists():
            raise RuntimeError(f"CRS config not found: {self.crs_config_name}")

        return config_dir.absolute()
