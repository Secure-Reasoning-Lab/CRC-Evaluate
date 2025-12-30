"""CRS Patch CRS executor implementation.

This module implements the CRSPatchExecutor class which integrates with
CRS Patch's interface for patch generation using pre-cloned source repositories.
"""

import json
import os
import shutil
import subprocess
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from crsbench.evaluation.crs_executor import CRSExecutionResult, CRSExecutor
from crsbench.evaluation.process_utils import run_with_graceful_timeout
from crsbench.utils.logger import get_logger
from crsbench.utils.repo_manager import USE_GITCACHE
from crsbench.validation.schemas import HarnessFile

logger = get_logger(__name__)


class CRSPatchExecutor(CRSExecutor):
    """CRS executor for patch generation using CRS Patch interface.

    This executor uses CRS Patch's alternative build method with pre-cloned
    source repositories via repository manager integration.
    """

    def __init__(
        self,
        crs_config_name: str,
        oss_fuzz_path: Path,
        registry_dir: Path,
        benchmarks_root: Path,
        crs_configs_dir: Path,
        litellm_mode: Optional[str] = "passthrough",
    ):
        """Initialize CRS Patch executor.

        Args:
            crs_config_name: CRS configuration name (e.g., "multi-retrieval")
            oss_fuzz_path: Path to oss-fuzz repository (required for infrastructure)
            registry_dir: Path to CRS registry directory
            benchmarks_root: Path to benchmarks directory (for finding benchmark dirs)
            crs_configs_dir: Path to CRS configs directory
            litellm_mode: LiteLLM mode ('passthrough', 'proxy', or None for CRS-managed)
        """
        self.crs_config_name = crs_config_name
        self.oss_fuzz_path = oss_fuzz_path
        self.registry_dir = registry_dir
        self.litellm_mode = litellm_mode
        self.benchmarks_root = benchmarks_root
        self.crs_configs_dir = crs_configs_dir
        self.config: Dict[str, Any] = {}
        self.built_projects: Set[str] = set()

    def configure_crs(self, config: Dict[str, Any]) -> None:
        """Configure the CRS with given parameters.

        Args:
            config: CRS configuration parameters
        """
        self.config = config.copy()
        logger.info(f"Configured CRS Patch executor with: {config}")

    def _get_litellm_env(self) -> Dict[str, str]:
        """Get LiteLLM environment variables based on mode.

        Returns:
            Dict containing LITELLM_API_BASE and LITELLM_API_KEY for subprocess

        Raises:
            RuntimeError: If required environment variables are not set for the mode
        """
        if self.litellm_mode is None:
            # No external LiteLLM - CRS deploys its own
            return {}

        env = {}

        if self.litellm_mode == "passthrough":
            # Use external LiteLLM with UPSTREAM_LITELLM_BASE_URL and LITELLM_API_KEY
            url = os.environ.get("UPSTREAM_LITELLM_BASE_URL")
            key = os.environ.get("LITELLM_API_KEY")

            if not url:
                raise RuntimeError(
                    "UPSTREAM_LITELLM_BASE_URL not set (required for passthrough mode)"
                )
            if not key:
                raise RuntimeError(
                    "LITELLM_API_KEY not set (required for passthrough mode)"
                )

            env["LITELLM_API_BASE"] = url
            env["LITELLM_API_KEY"] = key
            logger.info(f"Using passthrough LiteLLM mode with URL: {url}")

        elif self.litellm_mode == "proxy":
            # Use self-hosted LiteLLM proxy with LITELLM_BASE_URL and LITELLM_MASTER_KEY
            url = os.environ.get("LITELLM_BASE_URL")
            key = os.environ.get("LITELLM_MASTER_KEY")

            if not url:
                raise RuntimeError("LITELLM_BASE_URL not set (required for proxy mode)")
            if not key:
                raise RuntimeError(
                    "LITELLM_MASTER_KEY not set (required for proxy mode)"
                )

            env["LITELLM_API_BASE"] = url
            env["LITELLM_API_KEY"] = key
            logger.info(f"Using proxy LiteLLM mode with URL: {url}")

        return env

    def build_crs(self, benchmark_path: Path, trial_output_dir: Path) -> None:
        """Pre-build CRS Docker image before running.

        Call this before starting snapshots to ensure build time
        is not included in snapshot period.

        Args:
            benchmark_path: Path to benchmark directory
            trial_output_dir: Trial directory (from TrialDirectoryPreparer)

        Note:
            This method is idempotent - it will skip building if already built
            for the same CRS config and project combination.
        """
        project_name = self._extract_project_name(benchmark_path)
        trial_build_dir = trial_output_dir / "crs-build"
        trial_build_dir.mkdir(parents=True, exist_ok=True)

        logger.info(f"Pre-building CRS for project '{project_name}'")
        self._build_crs_if_needed(benchmark_path, project_name, trial_build_dir)

    def run_crs(
        self,
        benchmark_path: Path,
        harness: HarnessFile,
        trial_output_dir: Path,
        *,
        on_run_start: Optional[Callable[[], None]] = None,
    ) -> CRSExecutionResult:
        """Run patch generation CRS.

        Args:
            benchmark_path: Path to benchmark directory
            harness: Harness configuration
            trial_output_dir: Directory for this trial's outputs
            on_run_start: Callback invoked when CRS run starts (after build)

        Returns:
            CRSExecutionResult with execution details
        """
        project_name = self._extract_project_name(benchmark_path)
        logger.info(
            f"Running CRS Patch CRS for project '{project_name}', harness '{harness.name}'"
        )

        # Prepare trial-specific build directory
        trial_build_dir = trial_output_dir / "crs-build"
        trial_build_dir.mkdir(parents=True, exist_ok=True)

        # Build if needed (pass benchmark_path for repo manager integration)
        self._build_crs_if_needed(benchmark_path, project_name, trial_build_dir)

        # Signal that CRS run is starting (after build)
        if on_run_start:
            on_run_start()

        # Prepare base output directory (CRS creates subdirectories)
        self._prepare_output_directory(trial_output_dir)

        harness_name = Path(harness.name).stem

        # Prepare POVs directory (required for patch generation)
        povs_path = self._prepare_povs(benchmark_path, harness_name, trial_output_dir)
        if not povs_path:
            raise RuntimeError(
                f"No POVs found for patch generation in harness {harness_name}"
            )

        # Build command
        cmd = [
            "oss-bugfix-crs",
            "run",
            self.crs_config_name,
            project_name,
            "--harness",
            harness_name,
            "--povs",
            str(povs_path),
            "--out",
            str(trial_output_dir / "output"),
            "--work-dir",
            str(trial_build_dir),
        ]

        # Prepare and add hints if enabled
        hints_path = self._prepare_hints(benchmark_path, harness_name, trial_output_dir)
        if hints_path:
            cmd.extend(["--hints", str(hints_path)])
            logger.info(f"Using prepared hints from {hints_path}")

        logger.info(f"Run command: {' '.join(cmd)}")
        logger.debug(f"Command: {cmd}")

        # Set up environment with LiteLLM configuration
        env = os.environ.copy()
        litellm_env = self._get_litellm_env()
        env.update(litellm_env)

        start_time = time.time()
        try:
            timeout = self.config.get("run_timeout", 3600)
            grace_period = self.config.get("graceful_timeout", 60)

            # Run with graceful timeout handling
            stdout, stderr, returncode, timed_out = run_with_graceful_timeout(
                cmd=cmd,
                timeout=timeout,
                grace_period=grace_period,
                cwd=trial_output_dir,
                env=env,
            )

            execution_time = time.time() - start_time

            # Store execution metadata for reproducibility
            self._store_execution_metadata(
                trial_output_dir=trial_output_dir,
                cmd=cmd,
                hints_path=hints_path,
                povs_path=povs_path,
                execution_time=execution_time,
                returncode=returncode,
            )

            # Timeout is considered success (CRS ran for full specified time)
            # Only other errors count as failure
            success = returncode == 0 or timed_out
            if timed_out:
                logger.info(
                    f"CRS execution completed full timeout period {execution_time:.1f}s (returncode: {returncode})"
                )

            return CRSExecutionResult(
                harness_name=harness.name,
                execution_time=execution_time,
                success=success,
                output=stdout,
                error=stderr if not success else None,
                timed_out=timed_out,
            )

        except Exception as e:
            execution_time = time.time() - start_time
            logger.error(f"CRS execution failed: {e}")

            return CRSExecutionResult(
                harness_name=harness.name,
                execution_time=execution_time,
                success=False,
                output="",
                error=str(e),
                timed_out=False,
            )

    def _build_crs_if_needed(
        self, benchmark_path: Path, project_name: str, trial_build_dir: Path
    ) -> None:
        """Build patch generation CRS if not already built.

        Args:
            benchmark_path: Path to benchmark directory (contains project.yaml)
            project_name: Project name for caching
            trial_build_dir: Trial-specific build directory
        """
        build_key = f"{self.crs_config_name}:{project_name}"

        if build_key in self.built_projects:
            logger.info(f"CRS already built for {build_key}")
            return

        build_start_time = time.time()

        # Use repository manager to ensure source code exists
        from crsbench.utils.repo_manager import ensure_project_repository

        logger.info(f"Ensuring source repository for {project_name}...")
        source_path = ensure_project_repository(
            benchmark_dir=str(benchmark_path), verbose=self.config.get("verbose", False)
        )

        if not source_path:
            raise RuntimeError(
                f"Failed to obtain source code for {project_name}. "
                "Check that project.yaml has valid main_repo or provide source manually."
            )

        logger.info(f"Using source from: {source_path}")

        cmd = [
            "oss-bugfix-crs",
            "build",
            self.crs_config_name,
            project_name,
            "--oss-fuzz",
            str(self.oss_fuzz_path),
            "--project-path",
            str(benchmark_path),  # Benchmark dir (OSS-Fuzz compatible)
            "--source-path",
            str(source_path),  # Pre-cloned source from repo manager
            "--registry",
            str(self.registry_dir),
            "--work-dir",
            str(trial_build_dir),
        ]

        # Add gitcache flag if enabled
        if USE_GITCACHE:
            cmd.append("--gitcache")

        env = os.environ.copy()
        env["OSS_FUZZ_HOME"] = str(self.oss_fuzz_path)

        logger.info(f"Build command: {' '.join(cmd)}")
        logger.debug(f"Command: {cmd}")
        logger.debug(f"Working directory: {trial_build_dir}")

        try:
            result = subprocess.run(
                cmd,
                env=env,
                capture_output=True,
                text=True,
                timeout=self.config.get("build_timeout", 600),
                cwd=str(trial_build_dir),
            )

            if result.returncode != 0:
                logger.error(f"CRS build failed: {result.stderr}")
                raise RuntimeError(f"Patch CRS build failed: {result.stderr}")

            build_time = time.time() - build_start_time
            self.built_projects.add(build_key)
            logger.info(
                f"Successfully built CRS for {project_name} in {build_time:.1f}s"
            )

        except subprocess.TimeoutExpired as e:
            logger.error("CRS build timed out")
            raise RuntimeError("CRS build timed out") from e

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
        self, benchmark_path: Path, harness_name: str, trial_output_dir: Path
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
        povs_dir = trial_output_dir / "crs-input" / "povs"
        povs_dir.mkdir(parents=True, exist_ok=True)

        # Collect POVs from directories containing blobs
        pov_count = 0
        for blobs_dir in sorted(source_harness_dir.glob("*/blobs")):
            if not blobs_dir.is_dir():
                continue

            # Get all files in blobs directory
            for pov_file in sorted(blobs_dir.iterdir()):
                if not pov_file.is_file():
                    continue

                # Filter based on config if specified
                if self.config.get("target_povs"):
                    if pov_file.stem not in self.config["target_povs"]:
                        continue

                # Copy with original filename
                shutil.copy2(pov_file, povs_dir / pov_file.name)
                pov_count += 1

        if pov_count == 0:
            logger.warning(f"No POV blobs found for harness {harness_name}")
            return None

        logger.info(f"Prepared {pov_count} POVs for {harness_name}")
        return povs_dir

    def _prepare_hints(
        self, benchmark_path: Path, harness_name: str, trial_output_dir: Path
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
        returncode: int,
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
                "corpus_level": self.config.get("hints_corpus_level")
                if hints_path
                else None,
                "sarif_count": len(list((hints_path / "sarif").glob("*.sarif")))
                if hints_path and (hints_path / "sarif").exists()
                else 0,
                "corpus_count": len(list((hints_path / "corpus").iterdir()))
                if hints_path and (hints_path / "corpus").exists()
                else 0,
            },
            "povs": {
                "provided": povs_path is not None,
                "path": str(povs_path) if povs_path else None,
                "count": len(list(povs_path.iterdir()))
                if povs_path and povs_path.exists()
                else 0,
            },
            "execution": {
                "duration_seconds": execution_time,
                "returncode": returncode,
                "success": returncode == 0,
            },
        }

        execution_file = trial_output_dir / "execution.json"
        with execution_file.open("w") as f:
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

        # Resolve from configs directory
        configs_dir = Path(self.crs_configs_dir)
        config_dir = configs_dir / self.crs_config_name

        if not config_dir.exists():
            raise RuntimeError(f"CRS config not found: {self.crs_config_name}")

        return config_dir.absolute()
