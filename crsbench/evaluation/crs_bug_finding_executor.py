"""CRS Bug Finding executor implementation.

This module implements the CRSBugFindingExecutor class which integrates with
oss-crs CLI for bug finding CRS execution using pre-cloned source repositories.
"""

import json
from crsbench.utils.logger import get_logger
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any, Set

from crsbench.evaluation.crs_executor import CRSExecutor, CRSResult
from crsbench.evaluation.results import POVResult
from crsbench.validation.schemas import HarnessFile

logger = get_logger(__name__)


class ExecutorError(Exception):
    """Raised when executor encounters an error."""

    pass


class CRSBugFindingExecutor(CRSExecutor):
    """CRS executor for bug finding using oss-crs CLI.

    This executor uses oss-crs's interface with pre-cloned source repositories
    via repository manager integration. Output location is auto-determined by
    oss-crs as {{ build_dir }}/out/{{ crs_name }}/{{ project }}/.
    """

    def __init__(
        self,
        crs_config_name: str,
        oss_fuzz_path: Path,
        registry_dir: Path,
        benchmarks_root: Path
    ):
        """Initialize bug finding executor.

        Args:
            crs_config_name: CRS configuration name (e.g., "ensemble-c")
            oss_fuzz_path: Path to oss-fuzz repository
            registry_dir: Path to CRS registry directory (e.g., crses/ or oss-crs-registry/)
            benchmarks_root: Path to benchmarks directory (for repo manager)
        """
        self.crs_config_name = crs_config_name
        self.oss_fuzz_path = oss_fuzz_path
        self.registry_dir = registry_dir
        self.benchmarks_root = benchmarks_root
        self.config: Dict[str, Any] = {}
        self.built_projects: Set[str] = set()

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
        self.config.setdefault("run_timeout", 7200)    # 2 hours
        self.config.setdefault("hints_enabled", False)
        self.config.setdefault("hints_corpus_level", "1h")

        logger.info(f"Configured CRS Bug Finding executor with: {config}")

    def run_crs(
        self,
        benchmark_path: Path,
        harness: HarnessFile,
        trial_output_dir: Path
    ) -> CRSResult:
        """Run CRS on a specific harness.

        Args:
            benchmark_path: Path to benchmark directory
            harness: Harness configuration
            trial_output_dir: Trial directory (from TrialDirectoryPreparer)

        Returns:
            CRSResult with execution details

        Note:
            Source code is already prepared at the correct commit by
            TrialDirectoryPreparer. The executor does not need commit
            information - it simply runs CRS on pre-prepared directories.

            Output location is auto-determined by oss-crs as:
            {{ build_dir }}/out/{{ crs_name }}/{{ project }}/
        """
        start_time = time.time()
        project_name = self._extract_project_name(benchmark_path)

        logger.info(f"Running Bug Finding CRS for project '{project_name}', harness '{harness.name}'")

        try:
            # 1. Prepare trial-specific build directory
            trial_build_dir = trial_output_dir / "build"
            trial_build_dir.mkdir(parents=True, exist_ok=True)

            # 2. Build CRS Docker image (this also clones the repository)
            self._build_crs_if_needed(benchmark_path, project_name, trial_build_dir)

            # 3. Verify source path exists after build
            source_path = self._find_source_path(trial_build_dir, project_name)
            if not source_path.exists():
                raise ExecutorError(
                    f"Source path not found: {source_path}. "
                    "Repository cloning or build preparation failed."
                )

            # 4. Prepare hints if enabled
            harness_name = Path(harness.name).stem
            hints_path = self._prepare_hints(benchmark_path, harness_name, trial_output_dir)

            # 5. Run CRS bug finding campaign
            cmd = self._construct_run_command(
                project_name=project_name,
                harness_name=harness_name,
                trial_build_dir=trial_build_dir,
                hints_path=hints_path
            )

            logger.info(f"Executing: {' '.join(cmd)}")
            logger.debug(f"Command: {cmd}")
            logger.debug(f"Working directory: {trial_output_dir}")

            # Get expected output location
            expected_output_dir = self._get_crs_output_dir(trial_build_dir, project_name)
            logger.info(f"Expected output at: {expected_output_dir}")

            timeout = self.config.get("run_timeout", 7200)
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                cwd=str(trial_output_dir)
            )

            execution_time = time.time() - start_time

            # 6. Store execution metadata
            self._store_execution_metadata(
                trial_output_dir=trial_output_dir,
                harness=harness,
                cmd=cmd,
                hints_path=hints_path,
                execution_time=execution_time,
                returncode=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr
            )

            # 7. Return result
            success = result.returncode == 0
            if not success:
                logger.warning(f"CRS execution returned non-zero code: {result.returncode}")
                logger.debug(f"stdout: {result.stdout}")
                logger.debug(f"stderr: {result.stderr}")

            return CRSResult(
                harness_name=harness.name,
                execution_time=execution_time,
                success=success,
                output=result.stdout,
                error=result.stderr if not success else None
            )

        except subprocess.TimeoutExpired as e:
            execution_time = time.time() - start_time
            logger.warning(f"CRS execution timeout after {execution_time}s")
            return CRSResult(
                harness_name=harness.name,
                execution_time=execution_time,
                success=False,
                output=e.stdout.decode() if e.stdout else "",
                error=f"Timeout after {execution_time}s"
            )

        except Exception as e:
            execution_time = time.time() - start_time
            logger.error(f"CRS execution failed: {e}", exc_info=True)
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
        """Process CRS results.

        Note: For bug finding executor, this is a stub.
        POV validation is handled by the snapshot module separately.

        Args:
            crs_result: CRS execution result
            harness: Harness configuration
            trial_output_dir: Trial directory

        Returns:
            Empty list (POV validation done by snapshot module)
        """
        logger.debug(
            f"Bug finding executor does not process POV results. "
            f"POV validation handled by snapshot module for {harness.name}"
        )
        return []

    def _build_crs_if_needed(
        self,
        benchmark_path: Path,
        project_name: str,
        trial_build_dir: Path
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

        logger.info(f"Building CRS for {build_key}")

        # Use repository manager to ensure source code exists
        from crsbench.migration.repo_manager import ensure_project_repository

        # Clone to trial-specific build directory
        source_dest = trial_build_dir / "src" / project_name

        source_path = ensure_project_repository(
            benchmark_dir=str(benchmark_path),
            project_dir=str(source_dest),
            verbose=self.config.get("verbose", False)
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
            "oss-crs", "build",
            "--build-dir", str(trial_build_dir),
            "--oss-fuzz-dir", str(self.oss_fuzz_path),
            "--registry-dir", str(self.registry_dir),
            "--project-path", str(benchmark_path),
            str(crs_config_dir), project_name, str(source_path)
        ]

        logger.info(f"Build command: {' '.join(cmd)}")
        logger.debug(f"Command: {cmd}")
        logger.debug(f"Working directory: {trial_build_dir}")

        timeout = self.config.get("build_timeout", 3600)

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                cwd=str(trial_build_dir)
            )

            if result.returncode != 0:
                logger.error(f"Build failed with code {result.returncode}")
                logger.error(f"Build stdout: {result.stdout}")
                logger.error(f"Build stderr: {result.stderr}")
                raise ExecutorError(f"CRS build failed: {result.stderr}")

            logger.info(f"Successfully built CRS for {build_key}")
            self.built_projects.add(build_key)

        except subprocess.TimeoutExpired as e:
            logger.error(f"Build timeout after {timeout}s")
            raise ExecutorError(f"Build timeout after {timeout}s") from e

    def _construct_run_command(
        self,
        project_name: str,
        harness_name: str,
        trial_build_dir: Path,
        hints_path: Optional[Path]
    ) -> List[str]:
        """Construct oss-crs run command.

        Args:
            project_name: Project name
            harness_name: Harness name
            trial_build_dir: Trial-specific build directory
            hints_path: Optional path to hints directory

        Returns:
            Command as list of strings

        Note:
            NO --output parameter. Output location is auto-determined by oss-crs as:
            {{ build_dir }}/out/{{ crs_name }}/{{ project }}/
        """
        crs_config_dir = self._resolve_crs_config_dir()

        cmd = [
            "oss-crs", "run",
            "--build-dir", str(trial_build_dir),
            "--oss-fuzz-dir", str(self.oss_fuzz_path),
            "--registry-dir", str(self.registry_dir),
            str(crs_config_dir), project_name, harness_name
        ]

        # Add hints if available
        if hints_path and hints_path.exists():
            cmd.extend(["--hints", str(hints_path)])
            logger.info(f"Using hints from: {hints_path}")
        else:
            logger.info("Running without hints")

        return cmd

    def _resolve_crs_config_dir(self) -> Path:
        """Resolve CRS configuration directory path.

        Searches for CRS configuration in crses/ directory, which follows
        the same format as oss-crs/example_configs/.

        Note:
            This does NOT search in oss-crs-registry/. The registry is only
            used via --registry-dir parameter for oss-crs CLI.

        Returns:
            Path to CRS config directory (absolute)

        Raises:
            ExecutorError: If config directory not found

        Example:
            crs_name: "ensemble-c"
            crses_dir: /path/to/CRSBench/crses
            Returns: /path/to/CRSBench/crses/ensemble-c/
        """
        # Check if full path provided
        config_path = Path(self.crs_config_name)
        if config_path.is_absolute() and config_path.exists():
            return config_path

        # Resolve from crses/ directory (NOT oss-crs-registry/)
        crses_dir = Path(__file__).parent.parent.parent / "crses"
        crs_config_dir = crses_dir / self.crs_config_name

        if not crs_config_dir.exists():
            available = [d.name for d in crses_dir.iterdir() if d.is_dir()] if crses_dir.exists() else []
            raise ExecutorError(
                f"CRS config directory not found: {crs_config_dir}\n"
                f"Available in {crses_dir}: {available}"
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

    def _get_crs_output_dir(self, build_dir: Path, benchmark_name: str) -> Path:
        """Get CRS output directory path.

        The output directory is auto-determined by oss-crs as:
        {{ build_dir }}/out/{{ crs_name }}/{{ project }}/

        Args:
            build_dir: Build directory
            benchmark_name: Benchmark name

        Returns:
            Path to CRS output directory

        Note:
            This directory is created and populated by oss-crs during execution.
            It contains subdirectories: povs/, corpus/, crs-data/
        """
        return build_dir / "out" / self.crs_config_name / benchmark_name

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
            logger.info(f"Copied {len(corpus_files)} corpus files from {corpus_level} level")
        else:
            logger.warning(f"Corpus directory not found: {source_corpus}")

        logger.info(f"Prepared hints at: {hints_dir}")
        return hints_dir

    def _store_execution_metadata(
        self,
        trial_output_dir: Path,
        harness: HarnessFile,
        cmd: List[str],
        hints_path: Optional[Path],
        execution_time: float,
        returncode: int,
        stdout: str,
        stderr: str
    ) -> None:
        """Store execution metadata to trial directory.

        Args:
            trial_output_dir: Trial directory
            harness: Harness configuration
            cmd: Command executed
            hints_path: Path to prepared hints (or None)
            execution_time: Total execution time
            returncode: Process exit code
            stdout: Process stdout
            stderr: Process stderr
        """
        build_dir = trial_output_dir / "build"

        # Get actual output directory (auto-determined by oss-crs)
        crs_output_dir = self._get_crs_output_dir(build_dir, harness.name)

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
                "timeout": returncode == 124
            },
            "hints": {
                "enabled": hints_path is not None,
                "path": str(hints_path) if hints_path else None,
                "corpus_level": self.config.get("hints_corpus_level") if hints_path else None,
                "sarif_count": len(list((hints_path / "sarif").glob("*.sarif"))) if hints_path and (hints_path / "sarif").exists() else 0,
                "corpus_count": len(list((hints_path / "corpus").iterdir())) if hints_path and (hints_path / "corpus").exists() else 0,
            },
            "outputs": {
                "crs_output_dir": str(crs_output_dir),
                "build_dir": str(build_dir),
                "note": "CRS output is at {{ build_dir }}/out/{{ crs_name }}/{{ project }}/"
            },
            "result": {
                "stdout_length": len(stdout),
                "stderr_length": len(stderr),
                "has_output": bool(stdout or stderr)
            }
        }

        metadata_file = trial_output_dir / "execution.json"
        with open(metadata_file, "w") as f:
            json.dump(metadata, f, indent=2)

        logger.debug(f"Stored execution metadata to {metadata_file}")
