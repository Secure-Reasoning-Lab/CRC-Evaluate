"""CRS Bug Finding executor implementation.

This module implements the CRSBugFindingExecutor class which integrates with
oss-bugfind-crs CLI for bug finding CRS execution using pre-cloned source repositories.
"""

import json
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from crsbench.evaluation.crs_executor import CRSExecutionResult, CRSExecutor
from crsbench.evaluation.process_utils import run_with_graceful_timeout
from crsbench.utils.crs_helper import get_crs_registry_name
from crsbench.utils.logger import get_logger
from crsbench.utils.repo_manager import USE_GITCACHE
from crsbench.validation.schemas import HarnessFile

logger = get_logger(__name__)


class ExecutorError(Exception):
    """Raised when executor encounters an error."""


class CRSBugFindingExecutor(CRSExecutor):
    """CRS executor for bug finding using oss-bugfind-crs CLI.

    This executor uses oss-bugfind-crs's interface with pre-cloned source repositories
    via repository manager integration. Output location is auto-determined by
    oss-crs as {{ build_dir }}/artifacts/{{ crs_name }}/{{ project }}/run/{{ harness_name }}/.
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
        """Initialize bug finding executor.

        Args:
            crs_config_name: CRS configuration name (e.g., "ensemble-c")
            oss_fuzz_path: Path to oss-fuzz repository
            registry_dir: Path to CRS registry directory (e.g., crses/registry/ or oss-crs-registry/)
            benchmarks_root: Path to benchmarks directory (for repo manager)
            crs_configs_dir: Path to CRS configs directory
            litellm_mode: LiteLLM mode ('passthrough' or 'proxy', default: 'passthrough')
        """
        self.crs_config_name = crs_config_name
        self.oss_fuzz_path = oss_fuzz_path
        self.registry_dir = registry_dir
        self.benchmarks_root = benchmarks_root
        self.crs_configs_dir = crs_configs_dir
        self.litellm_mode = litellm_mode
        self.config: Dict[str, Any] = {}
        self.built_projects: Set[str] = set()

    @property
    def actual_crs_name(self) -> str:
        """Get actual CRS name from config-resource.yaml (cached)."""
        if not hasattr(self, "_actual_crs_name"):
            self._actual_crs_name = get_crs_registry_name(
                self.crs_config_name, self.crs_configs_dir
            )
        return self._actual_crs_name

    def configure_crs(self, config: Dict[str, Any]) -> None:
        """Configure the CRS with given parameters.

        Args:
            config: CRS configuration parameters
                - build_timeout: Build timeout in seconds (default: 3600)
                - run_timeout: Run timeout in seconds (default: 7200)
                - hints_enabled: Whether to provide hints (default: False)
                - hints_corpus_level: Corpus level for hints ("1h" or "1d", default: "1h")
        """
        self.config = config.copy()

        # Set defaults
        self.config.setdefault("build_timeout", 3600)  # 1 hour
        self.config.setdefault("run_timeout", 7200)  # 2 hours
        self.config.setdefault("hints_enabled", False)
        self.config.setdefault("hints_corpus_level", "1h")

        logger.info(f"Configured CRS Bug Finding executor with: {config}")

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
        stop_event: Optional[threading.Event] = None,
    ) -> CRSExecutionResult:
        """Run CRS on a specific harness.

        Args:
            benchmark_path: Path to benchmark directory
            harness: Harness configuration
            trial_output_dir: Trial directory (from TrialDirectoryPreparer)
            on_run_start: Callback invoked when CRS run starts (after build)
            stop_event: Optional event to signal early termination (e.g., coverage saturation)

        Returns:
            CRSExecutionResult with execution details

        Note:
            Source code is already prepared at the correct commit by
            TrialDirectoryPreparer. The executor does not need commit
            information - it simply runs CRS on pre-prepared directories.

            Output location is auto-determined by oss-bugfind-crs as:
            {{ build_dir }}/artifacts/{{ crs_name }}/{{ project }}/run/{{ harness_name }}/
        """
        start_time = time.time()
        project_name = self._extract_project_name(benchmark_path)

        logger.info(
            f"Running Bug Finding CRS for project '{project_name}', harness '{harness.name}'"
        )

        try:
            # 1. Prepare trial-specific build directory
            trial_build_dir = trial_output_dir / "crs-build"
            trial_build_dir.mkdir(parents=True, exist_ok=True)

            # 2. Build CRS Docker image (this also clones the repository)
            self._build_crs_if_needed(benchmark_path, project_name, trial_build_dir)

            # Signal that CRS run is starting (after build)
            if on_run_start:
                on_run_start()

            # 3. Verify source path exists after build
            source_path = self._find_source_path(trial_build_dir, project_name)
            if not source_path.exists():
                raise ExecutorError(
                    f"Source path not found: {source_path}. "
                    "Repository cloning or build preparation failed."
                )

            # 4. Prepare hints if enabled
            harness_name = Path(harness.name).stem
            hints_path = self._prepare_hints(
                benchmark_path, harness_name, trial_output_dir
            )

            # Detect ref.diff for delta mode
            diff_path = None
            if hints_path and (hints_path / "ref.diff").exists():
                diff_path = hints_path / "ref.diff"

            # 5. Run CRS bug finding campaign
            cmd = self._construct_run_command(
                project_name=project_name,
                harness_name=harness_name,
                trial_build_dir=trial_build_dir,
                hints_path=hints_path,
                diff_path=diff_path,
            )

            logger.info(f"Run command: {' '.join(cmd)}")
            logger.debug(f"Command: {cmd}")
            logger.debug(f"Working directory: {trial_output_dir}")

            # Get expected output location
            expected_output_dir = self._get_crs_output_dir(
                trial_build_dir, project_name, harness_name
            )
            logger.info(f"Expected output at: {expected_output_dir}")

            # Create relative symlink for easy access to output
            symlink_path = trial_output_dir / "output"
            if not symlink_path.exists():
                # Compute relative path from symlink location to target
                relative_target = (
                    Path("crs-build")
                    / "artifacts"
                    / self.actual_crs_name  # # TODO: ensemble settings
                    / project_name
                    / "run"
                    / harness_name
                )
                symlink_path.symlink_to(relative_target)
                logger.debug(
                    f"Created output symlink: {symlink_path} -> {relative_target}"
                )

            # Get LiteLLM environment variables
            import os

            litellm_env = self._get_litellm_env()
            env = os.environ.copy()
            env.update(litellm_env)

            timeout = self.config.get("run_timeout", 7200)
            grace_period = self.config.get("graceful_timeout", 60)

            # Run with graceful timeout handling
            stdout, stderr, returncode, timed_out = run_with_graceful_timeout(
                cmd=cmd,
                timeout=timeout,
                grace_period=grace_period,
                cwd=trial_output_dir,
                env=env,
                stop_event=stop_event,
            )

            execution_time = time.time() - start_time

            # 6. Store execution metadata
            self._store_execution_metadata(
                trial_output_dir=trial_output_dir,
                project_name=project_name,
                harness=harness,
                cmd=cmd,
                hints_path=hints_path,
                diff_path=diff_path,
                execution_time=execution_time,
                returncode=returncode,
                stdout=stdout,
                stderr=stderr,
            )

            # 7. Return result
            # Timeout is considered success (CRS ran for full specified time)
            # Only other errors count as failure
            success = returncode == 0 or timed_out
            if timed_out:
                logger.info(
                    f"CRS execution completed full timeout period {execution_time}s (returncode: {returncode})"
                )
            elif not success:
                logger.warning(f"CRS execution returned non-zero code: {returncode}")
                logger.debug(f"stdout: {stdout}")
                logger.debug(f"stderr: {stderr}")

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
            logger.error(f"CRS execution failed: {e}", exc_info=True)
            return CRSExecutionResult(
                harness_name=harness.name,
                execution_time=execution_time,
                success=False,
                output="",
                error=str(e),
                timed_out=False,
            )

    def _get_litellm_env(self) -> Dict[str, str]:
        """Get LiteLLM environment variables based on configured mode.

        Returns:
            Dictionary of environment variables for CRS subprocess

        Raises:
            ExecutorError: If required environment variables are not set
        """
        import os

        if self.litellm_mode is None:
            # No external LiteLLM - CRS deploys its own
            return {}

        env = {}

        if self.litellm_mode == "passthrough":
            # Use external LiteLLM with UPSTREAM_LITELLM_BASE_URL and LITELLM_API_KEY
            url = os.environ.get("UPSTREAM_LITELLM_BASE_URL")
            key = os.environ.get("LITELLM_API_KEY")

            if not url:
                raise ExecutorError(
                    "UPSTREAM_LITELLM_BASE_URL not set (required for passthrough mode)"
                )
            if not key:
                raise ExecutorError(
                    "LITELLM_API_KEY not set (required for passthrough mode)"
                )

            env["LITELLM_URL"] = url
            env["LITELLM_KEY"] = key
            logger.info(f"Using passthrough LiteLLM mode with URL: {url}")

        elif self.litellm_mode == "proxy":
            # Use self-hosted LiteLLM proxy with LITELLM_BASE_URL and LITELLM_MASTER_KEY
            url = os.environ.get("LITELLM_BASE_URL")
            key = os.environ.get("LITELLM_MASTER_KEY")

            if not url:
                raise ExecutorError(
                    "LITELLM_BASE_URL not set (required for proxy mode)"
                )
            if not key:
                raise ExecutorError(
                    "LITELLM_MASTER_KEY not set (required for proxy mode)"
                )

            env["LITELLM_URL"] = url
            env["LITELLM_KEY"] = key
            logger.info(f"Using proxy LiteLLM mode with URL: {url}")

        return env

    def _build_crs_if_needed(
        self, benchmark_path: Path, project_name: str, trial_build_dir: Path
    ) -> None:
        """Build CRS Docker image if not already built.

        Args:
            benchmark_path: Path to benchmark directory
            project_name: Project name for caching
            trial_build_dir: Trial-specific build directory
        """
        build_key = f"{self.crs_config_name}:{project_name}"

        if build_key in self.built_projects:
            logger.info(f"CRS already built for {build_key}, skipping build")
            return

        build_start_time = time.time()
        logger.info(f"Building CRS for {build_key}")

        # Use repository manager to ensure source code exists
        from crsbench.utils.repo_manager import ensure_project_repository

        # Clone to trial-specific build directory
        source_dest = trial_build_dir / "src" / project_name

        source_path = ensure_project_repository(
            benchmark_dir=str(benchmark_path),
            project_dir=str(source_dest),
            mode=self.config.get("mode"),
            verbose=self.config.get("verbose", False),
        )

        if not source_path:
            raise ExecutorError(
                f"Failed to obtain source code for {project_name}. "
                "Check that project.yaml has valid main_repo or provide source manually."
            )

        logger.info(f"Using source from: {source_path}")

        # Resolve CRS config directory
        crs_config_dir = self._resolve_crs_config_dir()

        # Construct build command
        cmd = [
            "oss-bugfind-crs",
            "build",
            "--build-dir",
            str(trial_build_dir),
            "--oss-fuzz-dir",
            str(self.oss_fuzz_path),
            "--registry-dir",
            str(self.registry_dir),
            "--project-path",
            str(benchmark_path),
            "--project-image-prefix",
            self.config.get("project_image_prefix", "aixcc-afc"),
            str(crs_config_dir),
            project_name,
            str(source_path),
        ]

        # Add external LiteLLM flag if using external LiteLLM
        if self.litellm_mode is not None:
            cmd.append("--external-litellm")

        # Add gitcache flag if enabled
        if USE_GITCACHE:
            cmd.append("--gitcache")

        logger.info(f"Build command: {' '.join(cmd)}")
        logger.debug(f"Command: {cmd}")
        logger.debug(f"Working directory: {trial_build_dir}")

        # Get LiteLLM environment variables
        import os

        litellm_env = self._get_litellm_env()
        env = os.environ.copy()
        env.update(litellm_env)

        timeout = self.config.get("build_timeout", 3600)

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                cwd=str(trial_build_dir),
                env=env,
            )

            if result.returncode != 0:
                logger.error(f"Build failed with code {result.returncode}")
                logger.error(f"Build stdout: {result.stdout}")
                logger.error(f"Build stderr: {result.stderr}")
                raise ExecutorError(f"CRS build failed: {result.stderr}")

            build_time = time.time() - build_start_time
            logger.info(f"Successfully built CRS for {build_key} in {build_time:.1f}s")
            self.built_projects.add(build_key)

        except subprocess.TimeoutExpired as e:
            logger.error(f"Build timeout after {timeout}s")
            raise ExecutorError(f"Build timeout after {timeout}s") from e

    def _construct_run_command(
        self,
        project_name: str,
        harness_name: str,
        trial_build_dir: Path,
        hints_path: Optional[Path],
        diff_path: Optional[Path] = None,
    ) -> List[str]:
        """Construct oss-bugfind-crs run command.

        Args:
            project_name: Project name
            harness_name: Harness name
            trial_build_dir: Trial-specific build directory
            hints_path: Optional path to hints directory
            diff_path: Optional path to diff file (delta mode)

        Returns:
            Command as list of strings

        Note:
            NO --output parameter. Output location is auto-determined by oss-bugfind-crs as:
            {{ build_dir }}/artifacts/{{ crs_name }}/{{ project }}/run/{{ harness_name }}/
        """
        crs_config_dir = self._resolve_crs_config_dir()

        cmd = [
            "oss-bugfind-crs",
            "run",
            "--build-dir",
            str(trial_build_dir),
            "--oss-fuzz-dir",
            str(self.oss_fuzz_path),
            "--registry-dir",
            str(self.registry_dir),
            str(crs_config_dir),
            project_name,
            harness_name,
        ]

        # Add hints if available
        if hints_path and hints_path.exists():
            cmd.extend(["--hints", str(hints_path)])
            logger.info(f"Using hints from: {hints_path}")
        else:
            logger.info("Running without hints")

        # Add diff path if available (delta mode)
        if diff_path and diff_path.exists():
            cmd.extend(["--diff", str(diff_path)])
            logger.info(f"Using diff for delta mode: {diff_path}")

        # Add external LiteLLM flag if using external LiteLLM
        if self.litellm_mode is not None:
            cmd.append("--external-litellm")

        # Add gitcache flag if enabled
        if USE_GITCACHE:
            cmd.append("--gitcache")

        return cmd

    def _resolve_crs_config_dir(self) -> Path:
        """Resolve CRS configuration directory path.

        Searches for CRS configuration in configurable configs directory,
        which follows the same format as oss-crs/example_configs/.

        Note:
            This does NOT search in oss-crs-registry/. The registry is only
            used via --registry-dir parameter for oss-bugfind-crs CLI.

        Returns:
            Path to CRS config directory (absolute)

        Raises:
            ExecutorError: If config directory not found

        Example:
            crs_name: "ensemble-c"
            configs_dir: /path/to/CRSBench/crses/configs
            Returns: /path/to/CRSBench/crses/configs/ensemble-c/
        """
        # Check if full path provided
        config_path = Path(self.crs_config_name)
        if config_path.is_absolute() and config_path.exists():
            return config_path

        # Resolve from configs directory
        configs_dir = Path(self.crs_configs_dir)

        crs_config_dir = configs_dir / self.crs_config_name

        if not crs_config_dir.exists():
            available = (
                [d.name for d in configs_dir.iterdir() if d.is_dir()]
                if configs_dir.exists()
                else []
            )
            raise ExecutorError(
                f"CRS config directory not found: {crs_config_dir}\n"
                f"Available in {configs_dir}: {available}"
            )

        return crs_config_dir.absolute()

    def _extract_project_name(self, benchmark_path: Path) -> str:
        """Extract OSS-Fuzz project name from benchmark path.

        Args:
            benchmark_path: Path to benchmark directory

        Returns:
            Project name

        Example:
            benchmarks/json-c-delta-01 → json-c-delta-01
        """
        return benchmark_path.name

    def _find_source_path(self, build_dir: Path, benchmark_name: str) -> Path:
        """Find source path in build directory.

        The source is at: build/src/<benchmark-name>/

        Args:
            build_dir: Build directory
            benchmark_name: Benchmark name

        Returns:
            Path to source directory
        """
        return build_dir / "src" / benchmark_name

    def _get_crs_output_dir(
        self, build_dir: Path, benchmark_name: str, harness_name: str
    ) -> Path:
        """Get CRS output directory path.

        The output directory is auto-determined by oss-bugfind-crs as:
        {{ build_dir }}/artifacts/{{ crs_name }}/{{ project }}/run/{{ harness_name }}/

        Args:
            build_dir: Build directory
            benchmark_name: Benchmark name
            harness_name: Harness name

        Returns:
            Path to CRS output directory

        Note:
            This directory is created and populated by oss-bugfind-crs during execution.
            It contains subdirectories: povs/, corpus/, crs-data/
        """
        return (
            build_dir
            / "artifacts"
            / self.actual_crs_name  # TODO: ensemble settings
            / benchmark_name
            / "run"
            / harness_name
        )

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

        Example structure created:
            trial_output_dir/hints/
            ├── sarif/
            │   ├── codeql.sarif
            │   └── semgrep.sarif
            └── corpus/
                ├── input-001
                └── input-002
        """
        if not self.config.get("hints_enabled", False):
            logger.debug("Hints disabled, skipping preparation")
            return None

        # Source hints from benchmark
        source_hints = benchmark_path / ".aixcc" / harness_name / "hints"
        if not source_hints.exists():
            logger.warning(f"Hints directory not found: {source_hints}")
            return None

        # Create trial-specific hints directory
        hints_dir = trial_output_dir / "hints"
        hints_dir.mkdir(parents=True, exist_ok=True)

        # Copy SARIF files
        source_sarif = source_hints / "sarif"
        if source_sarif.exists():
            dest_sarif = hints_dir / "sarif"
            dest_sarif.mkdir(exist_ok=True)
            for sarif_file in source_sarif.glob("*.sarif"):
                shutil.copy2(sarif_file, dest_sarif)
                logger.debug(f"Copied SARIF file: {sarif_file.name}")

        # Copy corpus based on configured level (1h or 1d)
        corpus_level = self.config.get("hints_corpus_level", "1h")
        source_corpus = source_hints / "corpus" / corpus_level
        if source_corpus.exists():
            dest_corpus = hints_dir / "corpus"
            dest_corpus.mkdir(exist_ok=True)
            corpus_files = list(source_corpus.iterdir())
            for corpus_file in corpus_files:
                if corpus_file.is_file():
                    shutil.copy2(corpus_file, dest_corpus)
            logger.info(
                f"Copied {len(corpus_files)} corpus files from {corpus_level} level"
            )
        else:
            logger.warning(f"Corpus directory not found: {source_corpus}")

        logger.info(f"Prepared hints at: {hints_dir}")
        return hints_dir

    def _store_execution_metadata(
        self,
        trial_output_dir: Path,
        project_name: str,
        harness: HarnessFile,
        cmd: List[str],
        hints_path: Optional[Path],
        diff_path: Optional[Path],
        execution_time: float,
        returncode: int,
        stdout: str,
        stderr: str,
    ) -> None:
        """Store execution metadata to trial directory.

        Args:
            trial_output_dir: Trial directory
            project_name: Project name
            harness: Harness configuration
            cmd: Command executed
            hints_path: Path to prepared hints (or None)
            diff_path: Path to diff file (or None)
            execution_time: Total execution time
            returncode: Process exit code
            stdout: Process stdout
            stderr: Process stderr
        """
        build_dir = trial_output_dir / "crs-build"

        # Get actual output directory (auto-determined by oss-bugfind-crs)
        crs_output_dir = self._get_crs_output_dir(build_dir, project_name, harness.name)

        metadata = {
            "timestamp": datetime.now().isoformat(),
            "executor": "CRSBugFindingExecutor",
            "crs_config": self.crs_config_name,
            "harness": harness.name,
            "execution_time": execution_time,
            "command": " ".join(cmd),
            "execution": {
                "returncode": returncode,
                "success": returncode == 0,
                "timeout": returncode == 124,
            },
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
                "has_diff": diff_path is not None,
                "diff_path": str(diff_path) if diff_path else None,
            },
            "outputs": {
                "crs_output_dir": str(crs_output_dir),
                "build_dir": str(build_dir),
                "note": "CRS output is at {{ build_dir }}/artifacts/{{ crs_name }}/{{ project }}/run/{{ harness_name }}/",
            },
            "result": {
                "stdout_length": len(stdout),
                "stderr_length": len(stderr),
                "has_output": bool(stdout or stderr),
            },
        }

        metadata_file = trial_output_dir / "execution.json"
        with metadata_file.open("w") as f:
            json.dump(metadata, f, indent=2)

        logger.debug(f"Stored execution metadata to {metadata_file}")

        # Always write stdout/stderr to files for debugging
        if stdout:
            stdout_file = trial_output_dir / "crs_stdout.log"
            with stdout_file.open("w") as f:
                f.write(stdout)
            logger.debug(f"Stored CRS stdout to {stdout_file}")

        if stderr:
            stderr_file = trial_output_dir / "crs_stderr.log"
            with stderr_file.open("w") as f:
                f.write(stderr)
            logger.debug(f"Stored CRS stderr to {stderr_file}")
