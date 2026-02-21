"""Unified OSS CRS adapter for bug-finding and bug-fixing.

Implements the oss-crs interface for both modes. Orchestrates the
3-phase lifecycle (prepare, build-target, run) and collects artifacts
from SUBMIT_DIR after execution.
"""

from __future__ import annotations

import fcntl
import os
import re
import shutil
import time
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

import yaml

from crsbench.evaluation.adapter.compose_common import (
    docker_compose_down_cleanup,
    generate_run_id,
    read_crs_source_from_registry,
    run_oss_crs_artifacts,
    run_oss_crs_build_target,
    run_oss_crs_prepare,
    run_oss_crs_run,
)
from crsbench.evaluation.adapter.config_gen import (
    CrsComposeCrsEntry,
    CrsComposeInfra,
    CrsComposeLlmConfig,
    CrsComposeYaml,
)
from crsbench.evaluation.results import CRSExecutionResult
from crsbench.utils.crs_helper import get_all_crs_registry_names
from crsbench.utils.logger import get_logger

if TYPE_CHECKING:
    import threading
    from collections.abc import Callable

    from crsbench.validation.schemas import ExperimentConfig, HarnessFile

logger = get_logger(__name__)


def _normalize_optional_text(value: Any) -> Optional[str]:
    """Normalize optional config values.

    Returns None for unset/sentinel values ("none", "null", empty),
    otherwise returns stripped string.
    """
    if value is None:
        return None
    text = str(value).strip()
    if text == "" or text.lower() in {"none", "null"}:
        return None
    return text


class OssCrsAdapter:
    """Adapter for oss-crs interface supporting both bug-finding and bug-fixing.

    Orchestrates CRS through oss-crs prepare/build-target/run phases,
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
        self._oss_crs_cmd: str = "oss-crs"
        self._docker_registry: str = ""
        self._oss_crs_infra_cpuset: str = "0-3"
        self._oss_crs_infra_memory: str = "8G"
        self._build_timeout: int = 3600
        self._run_timeout: int = 7200
        self._sanitizer: str = "address"
        self._external_litellm: bool = False
        self._litellm_url: str = ""
        self._litellm_api_key: str = ""
        self._litellm_config_path: str = ""
        self._additional_env_overrides: dict[str, str] = {}

        # Artifacts resolved via `oss-crs artifacts`
        self._resolved_artifacts: Optional[dict[str, Any]] = None
        self._run_id: Optional[str] = None

    @staticmethod
    def _lock_token(value: str) -> str:
        """Convert arbitrary text to a filesystem-safe lock token."""
        token = re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip("-")
        return token or "unknown"

    def _build_lock_file_path(self, project_name: str) -> Path:
        """Return host-local lock file path for build-target serialization.

        This is a temporary workaround for docker image tag races when multiple
        trials build the same CRS/project/sanitizer concurrently on one host.
        """
        lock_dir = Path(
            os.environ.get("CRSBENCH_OSS_CRS_BUILD_LOCK_DIR", "/tmp")
        ).expanduser()
        crs = self._lock_token(self._crs_config_name)
        project = self._lock_token(project_name)
        sanitizer = self._lock_token(self._sanitizer)
        return lock_dir / f"crsbench-oss-crs-build-{crs}-{project}-{sanitizer}.lock"

    @contextmanager
    def _acquire_build_lock(self, project_name: str):
        """Acquire an exclusive host-local lock for this build key."""
        lock_path = self._build_lock_file_path(project_name)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("w") as lock_file:
            logger.info(
                f"Acquiring build lock for {self._crs_config_name}/{project_name} "
                f"({self._sanitizer}): {lock_path}"
            )
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    @property
    def mode(self) -> str:
        """Return the adapter mode (bug-finding or bug-fixing)."""
        return self._mode

    @property
    def work_dir(self) -> Optional[Path]:
        """Return the oss-crs working directory, or None if not yet set."""
        return self._work_dir

    @property
    def built_projects(self) -> set[str]:
        """Track which projects have been built (stateful)."""
        return self._built_projects

    def _get_crs_artifact_path(self, key: str) -> Optional[Path]:
        """Look up a single path from resolved artifacts for the current CRS.

        Returns ``None`` when artifacts haven't been resolved or the key is
        absent in the CRS entry.
        """
        if self._resolved_artifacts is None:
            return None
        crs_arts = self._resolved_artifacts.get("crs", {}).get(
            self._crs_config_name, {}
        )
        value = crs_arts.get(key)
        return Path(value) if value else None

    def _get_exchange_path(self, key: str) -> Optional[Path]:
        """Look up a path from the top-level ``exchange_dir`` in artifacts.

        Returns ``None`` when artifacts haven't been resolved or the key is
        absent in the ``exchange_dir`` entry.
        """
        if self._resolved_artifacts is None:
            return None
        exchange = self._resolved_artifacts.get("exchange_dir")
        if exchange is None:
            return None
        value = exchange.get(key)
        return Path(value) if value else None

    @property
    def exchange_base_dir(self) -> Optional[Path]:
        """Return pre-resolved EXCHANGE_DIR base path, or None."""
        return self._get_exchange_path("base")

    @property
    def exchange_pov_dir(self) -> Optional[Path]:
        """Return pre-resolved EXCHANGE_DIR POV path, or None."""
        return self._get_exchange_path("pov")

    @property
    def exchange_seed_dir(self) -> Optional[Path]:
        """Return pre-resolved EXCHANGE_DIR seed path, or None."""
        return self._get_exchange_path("seed")

    @property
    def exchange_bug_candidate_dir(self) -> Optional[Path]:
        """Return pre-resolved EXCHANGE_DIR bug-candidate path, or None."""
        return self._get_exchange_path("bug_candidate")

    @property
    def exchange_patch_dir(self) -> Optional[Path]:
        """Return pre-resolved EXCHANGE_DIR patch path, or None."""
        return self._get_exchange_path("patch")

    @property
    def exchange_diff_dir(self) -> Optional[Path]:
        """Return pre-resolved EXCHANGE_DIR diff path, or None."""
        return self._get_exchange_path("diff")

    def configure(self, config: dict[str, Any]) -> None:
        """Configure the adapter with experiment parameters.

        Extracts standard fields (build_timeout, run_timeout) and
        oss-crs-specific fields (docker_registry, oss_crs_cmd, etc.)
        from the flat config dict passed by the caller.
        """
        if "build_timeout" in config:
            self._build_timeout = int(config["build_timeout"])
        if "run_timeout" in config:
            self._run_timeout = int(config["run_timeout"])
        if "oss_crs_cmd" in config:
            normalized = _normalize_optional_text(config["oss_crs_cmd"])
            if normalized is not None:
                self._oss_crs_cmd = normalized
        if "docker_registry" in config:
            normalized = _normalize_optional_text(config["docker_registry"])
            if normalized is not None:
                self._docker_registry = normalized
        if "oss_crs_infra_cpuset" in config:
            normalized = _normalize_optional_text(config["oss_crs_infra_cpuset"])
            if normalized is not None:
                self._oss_crs_infra_cpuset = normalized
            else:
                logger.debug(
                    "Ignoring empty oss_crs_infra_cpuset; keeping existing value "
                    f"'{self._oss_crs_infra_cpuset}'"
                )
        if "oss_crs_infra_memory" in config:
            normalized = _normalize_optional_text(config["oss_crs_infra_memory"])
            if normalized is not None:
                self._oss_crs_infra_memory = normalized
            else:
                logger.debug(
                    "Ignoring empty oss_crs_infra_memory; keeping existing value "
                    f"'{self._oss_crs_infra_memory}'"
                )
        if "work_dir" in config and config["work_dir"] is not None:
            self._work_dir = Path(config["work_dir"])
        if "sanitizer" in config:
            self._sanitizer = str(config["sanitizer"])
        if "external_litellm" in config:
            self._external_litellm = bool(config["external_litellm"])
        if "litellm_url" in config:
            self._litellm_url = str(config["litellm_url"])
        if "litellm_api_key" in config:
            self._litellm_api_key = str(config["litellm_api_key"])
        if "litellm_config_path" in config:
            normalized = _normalize_optional_text(config["litellm_config_path"])
            if normalized is not None:
                self._litellm_config_path = normalized
        if "additional_env" in config and config["additional_env"] is not None:
            self._additional_env_overrides = {
                str(k): str(v) for k, v in dict(config["additional_env"]).items()
            }

    def _collect_required_llm_aliases(self) -> tuple[list[str], list[str]]:
        """Collect required LLM aliases from registry metadata.

        For ensemble CRS configs, unions requirements from all registry members
        listed in ``crses/configs/<config>/config-resource.yaml``.
        """
        registry_names = get_all_crs_registry_names(
            self._crs_config_name, self._crs_configs_dir
        )
        required: set[str] = set()

        for registry_name in registry_names:
            registry_yaml = self._registry_dir / f"{registry_name}.yaml"
            if not registry_yaml.exists():
                raise FileNotFoundError(
                    f"CRS registry entry not found for required_llms lookup: {registry_yaml}"
                )
            with registry_yaml.open("r") as f:
                data = yaml.safe_load(f) or {}
            raw_required = data.get("required_llms")
            if raw_required is None:
                continue
            if not isinstance(raw_required, list) or not all(
                isinstance(x, str) for x in raw_required
            ):
                raise ValueError(
                    f"Invalid required_llms in {registry_yaml}; expected list[str]"
                )
            required.update(x.strip() for x in raw_required if x and x.strip())

        return sorted(required), registry_names

    @staticmethod
    def _load_litellm_aliases(litellm_config_path: Path) -> set[str]:
        """Load LiteLLM model aliases from config file."""
        with litellm_config_path.open("r") as f:
            cfg = yaml.safe_load(f) or {}
        model_list = cfg.get("model_list", [])
        aliases: set[str] = set()
        if isinstance(model_list, list):
            for model in model_list:
                if isinstance(model, dict):
                    alias = model.get("model_name")
                    if isinstance(alias, str) and alias.strip():
                        aliases.add(alias.strip())
        return aliases

    @staticmethod
    def _ignore_dotfiles(_directory: str, contents: list[str]) -> list[str]:
        """Return dotfile/dotdir names to exclude from copytree."""
        return [c for c in contents if c.startswith(".")]

    def _stage_benchmark(self, benchmark_path: Path, trial_output_dir: Path) -> Path:
        """Create a staging directory that excludes dotfiles/dotdirs.

        This prevents ground truth (e.g. ``.aixcc/``, ``.agent/``) from
        leaking into Docker build context.  Uses ``copytree`` because
        Docker buildx does not follow symlinks in build context.
        A ``.dockerignore`` is written as defense-in-depth.
        """
        staged = trial_output_dir / "staged" / benchmark_path.name
        if staged.exists():
            shutil.rmtree(staged)

        shutil.copytree(benchmark_path, staged, ignore=self._ignore_dotfiles)

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
        required_llms, required_sources = self._collect_required_llm_aliases()
        litellm_config_path = (
            Path(self._litellm_config_path)
            if self._litellm_config_path
            else self._crs_configs_dir / self._crs_config_name / "config-litellm.yaml"
        )
        llm_config: Optional[CrsComposeLlmConfig] = None
        if litellm_config_path.exists():
            llm_config = CrsComposeLlmConfig(litellm_config=str(litellm_config_path))
            logger.info(
                f"Using LiteLLM config for '{self._crs_config_name}': {litellm_config_path}"
            )
        elif self._litellm_config_path:
            raise RuntimeError(
                f"Configured litellm_config_path does not exist: {litellm_config_path}"
            )
        else:
            logger.info(
                f"No LiteLLM config found for '{self._crs_config_name}' at default path: {litellm_config_path}"
            )

        # Preflight contract validation: required_llms from CRS metadata must
        # be present in selected LiteLLM config.
        if required_llms:
            if llm_config is None:
                raise RuntimeError(
                    "Missing LiteLLM config for CRS required_llms validation. "
                    f"Required aliases: {', '.join(sorted(required_llms))}. "
                    f"Registry sources: {', '.join(required_sources)}"
                )
            available = self._load_litellm_aliases(litellm_config_path)
            missing = sorted(alias for alias in required_llms if alias not in available)
            if missing:
                raise RuntimeError(
                    "LiteLLM config missing required aliases for CRS: "
                    f"{', '.join(missing)}. "
                    f"Config: {litellm_config_path}. "
                    f"Registry sources: {', '.join(required_sources)}"
                )
            logger.info(
                f"Validated required_llms for '{self._crs_config_name}' "
                f"(aliases={len(required_llms)}, sources={','.join(required_sources)})"
            )

        additional_env: dict[str, str] = dict(self._additional_env_overrides)
        if self._external_litellm:
            if not self._litellm_url or not self._litellm_api_key:
                raise RuntimeError(
                    "external_litellm is enabled but litellm_url or "
                    "litellm_api_key is missing"
                )
            additional_env["OSS_CRS_LLM_API_URL"] = self._litellm_url
            additional_env["OSS_CRS_LLM_API_KEY"] = self._litellm_api_key

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
                    additional_env=additional_env or None,
                ),
            },
            llm_config=llm_config,
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
        """Build CRS for the given benchmark via oss-crs prepare + build-target."""
        project_name = benchmark_path.name
        if project_name in self._built_projects:
            logger.debug(f"Project {project_name} already built, skipping")
            return

        if self._compose_file is None:
            self._generate_compose_yaml(trial_output_dir)

        if self._work_dir is None:
            self._work_dir = trial_output_dir / "oss-crs-workdir"
        self._work_dir.mkdir(parents=True, exist_ok=True)

        compose_file, work_dir = self._ensure_compose_state()

        # Stage benchmark to exclude ground truth dotfiles
        staged_path = self._stage_benchmark(benchmark_path, trial_output_dir)

        with self._acquire_build_lock(project_name):
            # Re-check after lock in case another actor finished first.
            if project_name in self._built_projects:
                logger.debug(f"Project {project_name} already built, skipping")
                return

            # Phase 1: prepare (build CRS Docker images)
            logger.info(f"oss-crs prepare for {project_name}")
            stdout, stderr, rc = run_oss_crs_prepare(
                compose_file,
                work_dir,
                oss_crs_cmd=self._oss_crs_cmd,
                timeout=self._build_timeout,
            )
            if rc != 0:
                # oss-crs outputs errors via rich console to stdout
                detail = stderr or stdout
                msg = f"oss-crs prepare failed (rc={rc}): {detail}"
                raise RuntimeError(msg)

            # Phase 2: build-target (compile the target project)
            logger.info(f"oss-crs build-target for {project_name}")
            stdout, stderr, rc = run_oss_crs_build_target(
                compose_file,
                work_dir,
                staged_path,
                oss_crs_cmd=self._oss_crs_cmd,
                timeout=self._build_timeout,
                sanitizer=self._sanitizer,
            )
            if rc != 0:
                # oss-crs outputs errors via rich console to stdout
                detail = stderr or stdout
                msg = f"oss-crs build-target failed (rc={rc}): {detail}"
                raise RuntimeError(msg)

            self._built_projects.add(project_name)
            logger.info(f"Build complete for {project_name}")

    def _find_pov_dir(self, trial_output_dir: Path) -> Optional[Path]:
        """Locate POV directory in trial output."""
        candidate = trial_output_dir / "povs"
        if candidate.exists():
            return candidate
        return None

    def resolve_artifacts(
        self,
        benchmark_path: Path,
        harness_name: str,
        trial_output_dir: Path,
    ) -> None:
        """Pre-resolve artifact paths via ``oss-crs artifacts``.

        Must be called after ``build()`` and before ``run()``.  Generates a
        ``run_id`` and queries ``oss-crs artifacts`` for deterministic
        SUBMIT_DIR / EXCHANGE_DIR paths.  The same ``run_id`` is later
        forwarded to ``oss-crs run`` so on-disk paths match.

        Call this before creating verification managers so that
        ``exchange_dir`` returns a real path instead of ``None``.
        """
        compose_file, work_dir = self._ensure_compose_state()
        # Reuse staged path created by build() — same deterministic location
        staged_path = trial_output_dir / "staged" / benchmark_path.name

        self._run_id = generate_run_id()
        try:
            self._resolved_artifacts = run_oss_crs_artifacts(
                compose_file,
                work_dir,
                staged_path,
                harness_name,
                self._run_id,
                oss_crs_cmd=self._oss_crs_cmd,
                sanitizer=self._sanitizer,
            )
            logger.info(f"Resolved artifacts for run_id={self._run_id}")
        except RuntimeError:
            logger.warning(
                "oss-crs artifacts failed, falling back to None paths",
                exc_info=True,
            )
            self._resolved_artifacts = None

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
        """Execute CRS against a harness via oss-crs run.

        For bug-finding: runs without pov_dir/diff/seed_dir.
        For bug-fixing: locates pov_dir, diff, and seed_dir from
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
        seed_dir: Optional[Path] = None

        if self._mode == "bug-fixing":
            pov_dir = self._find_pov_dir(trial_output_dir)
            diff_path = trial_output_dir / "ref.diff"
            diff = diff_path if diff_path.exists() else None
            seed_path = trial_output_dir / "seeds"
            seed_dir = seed_path if seed_path.exists() else None

        start_time = time.time()
        stdout = ""
        stderr = ""
        rc = -1
        timed_out = False

        try:
            stdout, stderr, rc, timed_out = run_oss_crs_run(
                compose_file,
                work_dir,
                staged_path,
                harness.name,
                timeout=self._run_timeout,
                oss_crs_cmd=self._oss_crs_cmd,
                sanitizer=self._sanitizer,
                run_id=self._run_id,
                stop_event=stop_event,
                pov_dir=pov_dir,
                diff=diff,
                seed_dir=seed_dir,
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

        # Resolve SUBMIT_DIR from pre-resolved artifacts
        submit_dir = self._get_crs_artifact_path("submit_dir")
        if submit_dir is None:
            if self._resolved_artifacts is None:
                logger.warning("No resolved artifacts, cannot locate SUBMIT_DIR")
            else:
                logger.warning(
                    f"CRS '{self._crs_config_name}' not found in artifacts output"
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
            pov_src = submit_dir / "povs"
            if pov_src.exists():
                shutil.copytree(pov_src, output_dir / "povs", dirs_exist_ok=True)
                logger.info(f"Copied POVs from {pov_src}")

            seed_src = submit_dir / "seeds"
            if seed_src.exists():
                shutil.copytree(seed_src, output_dir / "seeds", dirs_exist_ok=True)
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
            patch_src = submit_dir / "patches"
            if patch_src.exists():
                shutil.copytree(patch_src, output_dir / "patches", dirs_exist_ok=True)
                logger.info(f"Copied patches from {patch_src}")

            # Also copy POVs if present (CRS may find new vulnerabilities)
            pov_src = submit_dir / "povs"
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
