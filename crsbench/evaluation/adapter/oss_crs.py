"""Unified OSS CRS adapter for bug-finding and bug-fixing.

Implements the crs-compose interface for both modes. Orchestrates the
3-phase lifecycle (prepare, build-target, run) and collects artifacts
from SUBMIT_DIR after execution.
"""

from __future__ import annotations

import shutil
import time
from typing import TYPE_CHECKING, Any, Optional

from crsbench.evaluation.adapter.compose_common import (
    docker_compose_down_cleanup,
    find_submit_dir,
    read_crs_source_from_registry,
    run_crs_compose_build_target,
    run_crs_compose_prepare,
    run_crs_compose_run,
)
from crsbench.evaluation.adapter.config_gen import (
    CrsComposeCrsEntry,
    CrsComposeInfra,
    CrsComposeYaml,
)
from crsbench.evaluation.results import CRSExecutionResult
from crsbench.utils.logger import get_logger

if TYPE_CHECKING:
    import threading
    from collections.abc import Callable
    from pathlib import Path

    from crsbench.validation.schemas import ExperimentConfig, HarnessFile

logger = get_logger(__name__)


class OssCrsAdapter:
    """Adapter for crs-compose interface supporting both bug-finding and bug-fixing.

    Orchestrates CRS through crs-compose prepare/build-target/run phases,
    then collects POVs (bug-finding) or patches (bug-fixing) from SUBMIT_DIR.
    The ``mode`` parameter selects the lifecycle variant.
    """

    def __init__(
        self,
        crs_config_name: str,
        oss_fuzz_path: Path,
        registry_dir: Path,
        benchmarks_root: Path,
        crs_configs_dir: Path,
        *,
        litellm_mode: str = "passthrough",
        mode: str = "bug-finding",
    ) -> None:
        self._crs_config_name = crs_config_name
        self._oss_fuzz_path = oss_fuzz_path
        self._registry_dir = registry_dir
        self._benchmarks_root = benchmarks_root
        self._crs_configs_dir = crs_configs_dir
        self._litellm_mode = litellm_mode
        self._mode = mode
        self._built_projects: set[str] = set()

        self._compose_file: Optional[Path] = None
        self._work_dir: Optional[Path] = None
        self._crs_compose_cmd: str = "crs-compose"
        self._docker_registry: str = ""
        self._oss_crs_infra_cpuset: str = "0-3"
        self._oss_crs_infra_memory: str = "8G"
        self._build_timeout: int = 3600
        self._run_timeout: int = 7200
        self._external_litellm: bool = False
        self._litellm_url: str = ""
        self._litellm_api_key: str = ""

    @property
    def mode(self) -> str:
        """Return the adapter mode (bug-finding or bug-fixing)."""
        return self._mode

    @property
    def built_projects(self) -> set[str]:
        """Track which projects have been built (stateful)."""
        return self._built_projects

    def configure(self, config: dict[str, Any]) -> None:
        """Configure the adapter with experiment parameters.

        Extracts standard fields (build_timeout, run_timeout) and
        compose-specific fields (docker_registry, crs_compose_cmd, etc.)
        from the flat config dict passed by the caller.
        """
        if "build_timeout" in config:
            self._build_timeout = int(config["build_timeout"])
        if "run_timeout" in config:
            self._run_timeout = int(config["run_timeout"])
        if "crs_compose_cmd" in config:
            self._crs_compose_cmd = str(config["crs_compose_cmd"])
        if "docker_registry" in config:
            self._docker_registry = str(config["docker_registry"])
        if "oss_crs_infra_cpuset" in config:
            self._oss_crs_infra_cpuset = str(config["oss_crs_infra_cpuset"])
        if "oss_crs_infra_memory" in config:
            self._oss_crs_infra_memory = str(config["oss_crs_infra_memory"])
        if "work_dir" in config and config["work_dir"] is not None:
            from pathlib import Path as _Path

            self._work_dir = _Path(config["work_dir"])
        if "external_litellm" in config:
            self._external_litellm = bool(config["external_litellm"])
        if "litellm_url" in config:
            self._litellm_url = str(config["litellm_url"])
        if "litellm_api_key" in config:
            self._litellm_api_key = str(config["litellm_api_key"])

    def _stage_benchmark(self, benchmark_path: Path, trial_output_dir: Path) -> Path:
        """Create a staging directory that excludes dotfiles/dotdirs.

        This prevents ground truth (e.g. ``.aixcc/``, ``.agent/``) from
        leaking into Docker build context.  Uses symlinks to avoid copying
        large benchmark files.  A ``.dockerignore`` is written as
        defense-in-depth.
        """
        staged = trial_output_dir / "staged-benchmark"
        if staged.exists():
            shutil.rmtree(staged)
        staged.mkdir(parents=True, exist_ok=True)

        for entry in benchmark_path.iterdir():
            if entry.name.startswith("."):
                continue
            (staged / entry.name).symlink_to(entry.resolve())

        # Defense-in-depth: prevent accidental COPY of ground truth
        (staged / ".dockerignore").write_text(".aixcc\n**/.aixcc\n.agent\n**/.agent\n")
        return staged

    def _generate_compose_yaml(self, trial_output_dir: Path) -> Path:
        """Generate crs-compose.yaml for this adapter's CRS.

        Reads CRS source info from registry and creates the YAML config
        file in the trial output directory.
        """
        source = read_crs_source_from_registry(
            self._registry_dir, self._crs_config_name
        )

        compose_yaml = CrsComposeYaml(
            docker_registry=self._docker_registry,
            oss_crs_infra=CrsComposeInfra(
                cpuset=self._oss_crs_infra_cpuset,
                memory=self._oss_crs_infra_memory,
            ),
            crs_entries={
                self._crs_config_name: CrsComposeCrsEntry(
                    source=source,
                    cpuset=self._oss_crs_infra_cpuset,
                    memory=self._oss_crs_infra_memory,
                ),
            },
        )

        compose_path = trial_output_dir / "crs-compose.yaml"
        trial_output_dir.mkdir(parents=True, exist_ok=True)
        compose_yaml.to_yaml(compose_path)

        self._compose_file = compose_path
        logger.info(f"Generated crs-compose.yaml at {compose_path}")
        return compose_path

    def _ensure_compose_state(self) -> tuple[Path, Path]:
        """Return (compose_file, work_dir), raising if not initialized."""
        if self._compose_file is None or self._work_dir is None:
            msg = "build() must be called before run()"
            raise RuntimeError(msg)
        return self._compose_file, self._work_dir

    def build(self, benchmark_path: Path, trial_output_dir: Path) -> None:
        """Build CRS for the given benchmark via crs-compose prepare + build-target."""
        project_name = benchmark_path.name
        if project_name in self._built_projects:
            logger.debug(f"Project {project_name} already built, skipping")
            return

        if self._compose_file is None:
            self._generate_compose_yaml(trial_output_dir)

        if self._work_dir is None:
            self._work_dir = trial_output_dir / "crs-compose-workdir"
        self._work_dir.mkdir(parents=True, exist_ok=True)

        compose_file, work_dir = self._ensure_compose_state()

        # Stage benchmark to exclude ground truth dotfiles
        staged_path = self._stage_benchmark(benchmark_path, trial_output_dir)

        # Phase 1: prepare (build CRS Docker images)
        logger.info(f"crs-compose prepare for {project_name}")
        stdout, stderr, rc = run_crs_compose_prepare(
            compose_file,
            work_dir,
            crs_compose_cmd=self._crs_compose_cmd,
            timeout=self._build_timeout,
        )
        if rc != 0:
            # crs-compose outputs errors via rich console to stdout
            detail = stderr or stdout
            msg = f"crs-compose prepare failed (rc={rc}): {detail}"
            raise RuntimeError(msg)

        # Phase 2: build-target (compile the target project)
        logger.info(f"crs-compose build-target for {project_name}")
        stdout, stderr, rc = run_crs_compose_build_target(
            compose_file,
            work_dir,
            staged_path,
            crs_compose_cmd=self._crs_compose_cmd,
            timeout=self._build_timeout,
        )
        if rc != 0:
            # crs-compose outputs errors via rich console to stdout
            detail = stderr or stdout
            msg = f"crs-compose build-target failed (rc={rc}): {detail}"
            raise RuntimeError(msg)

        self._built_projects.add(project_name)
        logger.info(f"Build complete for {project_name}")

    def _find_pov_dir(self, trial_output_dir: Path) -> Optional[Path]:
        """Locate POV directory in trial output (povs/ or pov/)."""
        for name in ("povs", "pov"):
            candidate = trial_output_dir / name
            if candidate.exists():
                return candidate
        return None

    def run(
        self,
        benchmark_path: Path,
        harness: HarnessFile,
        trial_output_dir: Path,
        *,
        on_build_start: Optional[Callable[[], None]] = None,
        on_run_start: Optional[Callable[[], None]] = None,
        stop_event: Optional[threading.Event] = None,
    ) -> CRSExecutionResult:
        """Execute CRS against a harness via crs-compose run.

        For bug-finding: runs without pov_dir/diff/corpus_dir.
        For bug-fixing: locates pov_dir, diff, and corpus_dir from
        trial_output_dir before running.
        """
        compose_file, work_dir = self._ensure_compose_state()

        if on_build_start is not None:
            on_build_start()
        if on_run_start is not None:
            on_run_start()

        # Stage benchmark to exclude ground truth dotfiles
        staged_path = self._stage_benchmark(benchmark_path, trial_output_dir)

        # Bug-fixing inputs (only used when mode is bug-fixing)
        pov_dir: Optional[Path] = None
        diff: Optional[Path] = None
        corpus_dir: Optional[Path] = None

        if self._mode == "bug-fixing":
            pov_dir = self._find_pov_dir(trial_output_dir)
            diff_path = trial_output_dir / "ref.diff"
            diff = diff_path if diff_path.exists() else None
            corpus_path = trial_output_dir / "corpus"
            corpus_dir = corpus_path if corpus_path.exists() else None

        start_time = time.time()
        stdout = ""
        stderr = ""
        rc = -1
        timed_out = False

        try:
            stdout, stderr, rc, timed_out = run_crs_compose_run(
                compose_file,
                work_dir,
                staged_path,
                harness.name,
                timeout=self._run_timeout,
                crs_compose_cmd=self._crs_compose_cmd,
                stop_event=stop_event,
                pov_dir=pov_dir,
                diff=diff,
                corpus_dir=corpus_dir,
                external_litellm=self._external_litellm,
                litellm_url=self._litellm_url,
                litellm_api_key=self._litellm_api_key,
            )
        finally:
            docker_compose_down_cleanup(work_dir)

        execution_time = time.time() - start_time

        return CRSExecutionResult(
            harness_name=harness.name,
            execution_time=execution_time,
            success=(rc == 0),
            output=stdout,
            error=stderr if rc != 0 else None,
            timed_out=timed_out,
        )

    def collect_results(
        self,
        trial_output_dir: Path,
        harness_name: str,
    ) -> dict[str, Any]:
        """Collect artifacts from SUBMIT_DIR after execution.

        For bug-finding: copies POVs and seeds.
        For bug-fixing: copies patches and POVs.
        """
        output_dir = trial_output_dir / "output"
        output_dir.mkdir(parents=True, exist_ok=True)

        if self._work_dir is None:
            logger.warning("work_dir not set, cannot collect results")
            return {
                "type": self._mode,
                "output_dir": str(output_dir),
                "harness": harness_name,
                "submit_dir": None,
            }

        submit_dir = find_submit_dir(
            self._work_dir, self._crs_config_name, harness_name
        )

        if self._mode == "bug-finding":
            return self._collect_bugfind_results(submit_dir, output_dir, harness_name)
        return self._collect_bugfix_results(submit_dir, output_dir, harness_name)

    def _collect_bugfind_results(
        self,
        submit_dir: Optional[Path],
        output_dir: Path,
        harness_name: str,
    ) -> dict[str, Any]:
        """Collect bug-finding artifacts (POVs and seeds)."""
        if submit_dir is not None:
            pov_src = submit_dir / "pov"
            if pov_src.exists():
                shutil.copytree(pov_src, output_dir / "povs", dirs_exist_ok=True)
                logger.info(f"Copied POVs from {pov_src}")

            seed_src = submit_dir / "seed"
            if seed_src.exists():
                shutil.copytree(seed_src, output_dir / "seed", dirs_exist_ok=True)
                logger.info(f"Copied seeds from {seed_src}")

        return {
            "type": "bug-finding",
            "output_dir": str(output_dir),
            "harness": harness_name,
            "submit_dir": str(submit_dir) if submit_dir else None,
        }

    def _collect_bugfix_results(
        self,
        submit_dir: Optional[Path],
        output_dir: Path,
        harness_name: str,
    ) -> dict[str, Any]:
        """Collect bug-fixing artifacts (patches and POVs)."""
        if submit_dir is not None:
            # Copy patches (primary artifact for bug-fixing)
            patch_src = submit_dir / "patch"
            if patch_src.exists():
                shutil.copytree(patch_src, output_dir / "patches", dirs_exist_ok=True)
                logger.info(f"Copied patches from {patch_src}")

            # Also copy POVs if present (CRS may find new vulnerabilities)
            pov_src = submit_dir / "pov"
            if pov_src.exists():
                shutil.copytree(pov_src, output_dir / "povs", dirs_exist_ok=True)
                logger.info(f"Copied POVs from {pov_src}")

        # List collected patches
        patch_output = output_dir / "patches"
        patches = (
            [str(p) for p in patch_output.iterdir()] if patch_output.exists() else []
        )

        return {
            "type": "bug-fixing",
            "output_dir": str(output_dir),
            "harness": harness_name,
            "patches": patches,
            "submit_dir": str(submit_dir) if submit_dir else None,
        }


def create_adapter(
    config: ExperimentConfig,
    crs_config_name: str,
    oss_fuzz_path: Path,
    registry_dir: Path,
    benchmarks_root: Path,
    crs_configs_dir: Path,
    *,
    mode: str = "bug-finding",
) -> OssCrsAdapter:
    """Create an OssCrsAdapter from experiment configuration.

    Single entry point for adapter construction. Used by runner.py,
    distributed/jobs.py, and experiment/jobs/crs_run.py.
    """
    return OssCrsAdapter(
        crs_config_name=crs_config_name,
        oss_fuzz_path=oss_fuzz_path,
        registry_dir=registry_dir,
        benchmarks_root=benchmarks_root,
        crs_configs_dir=crs_configs_dir,
        litellm_mode=config.litellm_mode or "passthrough",
        mode=mode,
    )
