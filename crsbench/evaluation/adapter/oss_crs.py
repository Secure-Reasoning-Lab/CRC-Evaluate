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
    CrsComposeLiteLLMConfig,
    CrsComposeLiteLLMExternalConfig,
    CrsComposeLLMConfig,
    CrsComposeYaml,
)
from crsbench.evaluation.results import CRSExecutionResult
from crsbench.utils.cpu_pool import format_cpuset, parse_cpuset
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


def _default_memory_limit() -> str:
    """Return a conservative default memory string for oss-crs compose.

    Priority:
    1. ``CRSBENCH_OSS_CRS_DEFAULT_MEMORY`` (when set and non-empty)
    2. 90% of host MemTotal from ``/proc/meminfo`` (in MB)
    3. ``65536MB`` fallback
    """
    env_override = _normalize_optional_text(
        os.environ.get("CRSBENCH_OSS_CRS_DEFAULT_MEMORY")
    )
    if env_override:
        return env_override

    try:
        meminfo = Path("/proc/meminfo")
        if meminfo.exists():
            for line in meminfo.read_text().splitlines():
                if not line.startswith("MemTotal:"):
                    continue
                parts = line.split()
                if len(parts) < 2:
                    continue
                total_kb = int(parts[1])
                # Keep headroom for host + sibling processes.
                total_mb = max(1024, int(total_kb * 0.9 / 1024))
                return f"{total_mb}MB"
    except Exception:
        logger.debug(
            "Failed to derive host memory default from /proc/meminfo", exc_info=True
        )

    return "65536MB"


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
        litellm_mode: str = "external",
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
        self._allocated_cpus: Optional[str] = None
        self._fuzzing_language: str = "c"
        self._infra_num_cores: int = 0
        self._infra_shared: bool = True
        self._infra_mem_limit: Optional[str] = None
        self._infra_mem_explicit: bool = False
        self._crs_service_configs: dict[str, dict[str, Any]] = {
            self._crs_config_name: {
                "num_cores": 1,
                "mem_limit": None,
                "additional_env": {},
            }
        }
        self._build_timeout: int = 3600
        self._run_timeout: int = 7200
        self._sanitizer: str = "address"
        self._skip_litellm: bool = False
        self._litellm_runtime_url: str = ""
        self._litellm_runtime_api_key: str = ""
        self._litellm_config_path: str = ""

        # Artifacts resolved via `oss-crs artifacts`
        self._resolved_artifacts: Optional[dict[str, Any]] = None
        self._configured_run_id: Optional[str] = None
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

    def _get_all_crs_artifact_paths(self, key: str) -> dict[str, Path]:
        """Collect existing per-CRS artifact paths for ``key``.

        Iterates over all CRS entries returned by ``oss-crs artifacts`` and keeps
        only existing paths. This supports ensemble layouts where artifacts may
        be produced by multiple CRS containers.
        """
        if self._resolved_artifacts is None:
            return {}

        result: dict[str, Path] = {}
        crs_entries = self._resolved_artifacts.get("crs", {})
        if not isinstance(crs_entries, dict):
            return {}

        for crs_name, crs_info in crs_entries.items():
            if not isinstance(crs_info, dict):
                continue
            value = crs_info.get(key)
            if not value:
                continue
            path = Path(str(value))
            if path.exists():
                result[str(crs_name)] = path

        return result

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

    def _get_run_logs_path(self, key: str) -> Optional[Path]:
        """Look up a path from the top-level ``run_logs`` in artifacts."""
        if self._resolved_artifacts is None:
            return None
        run_logs = self._resolved_artifacts.get("run_logs")
        if run_logs is None:
            return None
        value = run_logs.get(key)
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

    @property
    def run_logs_base_dir(self) -> Optional[Path]:
        """Return top-level run logs base path, or None."""
        return self._get_run_logs_path("base")

    @property
    def compose_stdout_log(self) -> Optional[Path]:
        """Return compose stdout log path, or None."""
        return self._get_run_logs_path("compose_stdout_log")

    @property
    def compose_stderr_log(self) -> Optional[Path]:
        """Return compose stderr log path, or None."""
        return self._get_run_logs_path("compose_stderr_log")

    @property
    def service_logs_dir(self) -> Optional[Path]:
        """Return compose per-service logs directory, or None."""
        return self._get_run_logs_path("service_logs")

    def _collect_run_logs(
        self,
        output_dir: Path,
    ) -> dict[str, Any]:
        """Collect compose and per-CRS run logs from resolved artifacts."""
        logs_out = output_dir / "logs"
        logs_out.mkdir(parents=True, exist_ok=True)

        compose_stdout = self.compose_stdout_log
        compose_stderr = self.compose_stderr_log
        service_logs = self.service_logs_dir
        crs_run_logs_by_crs = self._get_all_crs_artifact_paths("run_logs")

        if compose_stdout and compose_stdout.exists():
            dst = logs_out / "docker-compose.stdout.log"
            shutil.copy2(compose_stdout, dst)
            logger.info(f"Copied compose stdout log from {compose_stdout}")

        if compose_stderr and compose_stderr.exists():
            dst = logs_out / "docker-compose.stderr.log"
            shutil.copy2(compose_stderr, dst)
            logger.info(f"Copied compose stderr log from {compose_stderr}")

        if service_logs and service_logs.exists():
            shutil.copytree(service_logs, logs_out / "services", dirs_exist_ok=True)
            logger.info(f"Copied service logs from {service_logs}")

        if crs_run_logs_by_crs:
            for crs_name, crs_log_dir in sorted(crs_run_logs_by_crs.items()):
                if not crs_log_dir.exists():
                    continue
                dst = logs_out / "crs" / crs_name
                shutil.copytree(crs_log_dir, dst, dirs_exist_ok=True)
                logger.info(f"Copied CRS run logs from {crs_log_dir}")

        return {
            "run_logs_base_dir": str(self.run_logs_base_dir)
            if self.run_logs_base_dir
            else None,
            "compose_stdout_log": str(compose_stdout) if compose_stdout else None,
            "compose_stderr_log": str(compose_stderr) if compose_stderr else None,
            "service_logs_dir": str(service_logs) if service_logs else None,
            "crs_run_logs_by_crs": {
                k: str(v) for k, v in sorted(crs_run_logs_by_crs.items())
            },
        }

    def configure(self, config: dict[str, Any]) -> None:
        """Configure the adapter with experiment parameters.

        Extracts standard fields (build_timeout, run_timeout) and
        oss-crs-specific fields from the config dict passed by the caller.
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
                logger.warning(
                    "Ignoring docker_registry override from CRSBench config (%s); "
                    "oss-crs registry config owns image resolution",
                    normalized,
                )
        if "oss_crs_infra_cpuset" in config:
            normalized = _normalize_optional_text(config["oss_crs_infra_cpuset"])
            if normalized is not None:
                self._oss_crs_infra_cpuset = normalized
        explicit_infra_memory = False
        if "oss_crs_infra_memory" in config:
            normalized = _normalize_optional_text(config["oss_crs_infra_memory"])
            self._infra_mem_limit = normalized
            explicit_infra_memory = normalized is not None
            self._infra_mem_explicit = explicit_infra_memory
        if "allocated_cpus" in config:
            self._allocated_cpus = _normalize_optional_text(config["allocated_cpus"])
        allocated_memory = _normalize_optional_text(config.get("allocated_memory"))
        if "fuzzing_language" in config:
            normalized = _normalize_optional_text(config["fuzzing_language"])
            if normalized is not None:
                self._fuzzing_language = normalized
        if "oss_crs_infra" in config and isinstance(config["oss_crs_infra"], dict):
            infra = dict(config["oss_crs_infra"])
            if "num_cores" in infra:
                self._infra_num_cores = int(infra["num_cores"])
                if "shared" not in infra:
                    # Backward-compatible interpretation: explicit num_cores means
                    # dedicated infra cores unless shared=true is explicitly set.
                    self._infra_shared = False
            if "shared" in infra:
                self._infra_shared = bool(infra["shared"])
            if "mem_limit" in infra:
                normalized_infra_mem = _normalize_optional_text(infra["mem_limit"])
                self._infra_mem_limit = normalized_infra_mem
                explicit_infra_memory = normalized_infra_mem is not None
                self._infra_mem_explicit = explicit_infra_memory
        if (
            allocated_memory is not None
            and not explicit_infra_memory
            and not self._infra_mem_explicit
        ):
            self._infra_mem_limit = allocated_memory
        raw_crs_services = None
        if "crs_services" in config and isinstance(config["crs_services"], dict):
            raw_crs_services = dict(config["crs_services"])
        else:
            reserved = {
                "build_timeout",
                "run_timeout",
                "oss_crs_cmd",
                "docker_registry",
                "oss_crs_infra_cpuset",
                "oss_crs_infra_memory",
                "allocated_cpus",
                "fuzzing_language",
                "oss_crs_infra",
                "additional_env",
                "work_dir",
                "sanitizer",
                "skip_litellm",
                "litellm_runtime_url",
                "litellm_runtime_api_key",
                "litellm_config_path",
                "run_id",
            }
            flat = {
                str(name): dict(raw_service)
                for name, raw_service in config.items()
                if name not in reserved and isinstance(raw_service, dict)
            }
            if flat:
                raw_crs_services = flat

        if raw_crs_services is not None:
            service_configs: dict[str, dict[str, Any]] = {}
            for name, raw_service in raw_crs_services.items():
                if not isinstance(raw_service, dict):
                    continue
                additional_env_raw = raw_service.get("additional_env")
                additional_env = (
                    {str(k): str(v) for k, v in additional_env_raw.items()}
                    if isinstance(additional_env_raw, dict)
                    else {}
                )
                service_configs[str(name)] = {
                    "num_cores": int(raw_service.get("num_cores", 1)),
                    "mem_limit": _normalize_optional_text(raw_service.get("mem_limit")),
                    "additional_env": additional_env,
                }
            if service_configs:
                if self._crs_config_name in service_configs:
                    self._crs_service_configs = {
                        self._crs_config_name: service_configs[self._crs_config_name]
                    }
                else:
                    raise RuntimeError(
                        "crs_services does not contain current trial CRS "
                        f"'{self._crs_config_name}'"
                    )
        if "additional_env" in config and isinstance(config["additional_env"], dict):
            # Backward-compatible adapter input: apply to current CRS service.
            merged = {str(k): str(v) for k, v in dict(config["additional_env"]).items()}
            service = self._crs_service_configs.get(self._crs_config_name)
            if service is None:
                self._crs_service_configs[self._crs_config_name] = {
                    "num_cores": 1,
                    "mem_limit": self._infra_mem_limit,
                    "additional_env": merged,
                }
            else:
                service_env = dict(service.get("additional_env", {}))
                service_env.update(merged)
                service["additional_env"] = service_env
        if "work_dir" in config and config["work_dir"] is not None:
            self._work_dir = Path(config["work_dir"])
        if "sanitizer" in config:
            new_sanitizer = str(config["sanitizer"])
            if new_sanitizer != self._sanitizer:
                self._sanitizer = new_sanitizer
                # Compose embeds SANITIZER via additional_env; changing sanitizer
                # requires regenerating compose/workdir and rebuilding targets.
                self._compose_file = None
                if "work_dir" not in config or config["work_dir"] is None:
                    self._work_dir = None
                self._resolved_artifacts = None
                self._built_projects.clear()
        if "skip_litellm" in config:
            self._skip_litellm = bool(config["skip_litellm"])
        if "litellm_runtime_url" in config:
            self._litellm_runtime_url = str(config["litellm_runtime_url"])
        if "litellm_runtime_api_key" in config:
            self._litellm_runtime_api_key = str(config["litellm_runtime_api_key"])
        if "litellm_config_path" in config:
            normalized = _normalize_optional_text(config["litellm_config_path"])
            if normalized is not None:
                self._litellm_config_path = normalized
        if "run_id" in config:
            self._configured_run_id = _normalize_optional_text(config["run_id"])
            # Keep adapter run-id aligned with explicit trial-scoped config.
            self._run_id = self._configured_run_id

    def _collect_required_llm_aliases(
        self, crs_names: list[str]
    ) -> tuple[list[str], list[str]]:
        """Collect required LLM aliases from registry metadata.

        Unions requirements from all CRS registry entries in ``crs_names``.
        """
        required: set[str] = set()

        for crs_name in crs_names:
            registry_yaml = self._registry_dir / f"{crs_name}.yaml"
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

        return sorted(required), sorted(crs_names)

    def _assign_cpusets(self) -> tuple[str, dict[str, str]]:
        """Assign cpusets for infra and each CRS service from allocated CPU pool."""
        service_names = list(self._crs_service_configs.keys())
        service_required = sum(
            int(cfg["num_cores"]) for cfg in self._crs_service_configs.values()
        )
        required = (
            service_required
            if self._infra_shared
            else self._infra_num_cores + service_required
        )
        if required < 1:
            raise RuntimeError(
                "Invalid CPU configuration: total required cores must be >= 1"
            )

        if self._allocated_cpus:
            pool = sorted(set(parse_cpuset(self._allocated_cpus)))
        elif _normalize_optional_text(self._oss_crs_infra_cpuset):
            pool = sorted(set(parse_cpuset(self._oss_crs_infra_cpuset)))
        else:
            pool = list(range(required))

        if len(pool) < required:
            service_summary = ", ".join(
                f"{name}:{self._crs_service_configs[name]['num_cores']}"
                for name in service_names
            )
            raise RuntimeError(
                "Insufficient allocated CPUs for oss-crs compose: "
                f"required={required} (infra={'shared' if self._infra_shared else self._infra_num_cores}, "
                f"services={{{service_summary}}}), "
                f"available={len(pool)} from '{self._allocated_cpus or format_cpuset(pool)}'"
            )

        cursor = 0
        infra_slice: list[int] = []
        if self._infra_num_cores > 0:
            infra_slice = pool[cursor : cursor + self._infra_num_cores]
            cursor += self._infra_num_cores

        service_cpusets: dict[str, str] = {}
        for name in service_names:
            count = int(self._crs_service_configs[name]["num_cores"])
            slice_ = pool[cursor : cursor + count]
            cursor += count
            service_cpusets[name] = format_cpuset(slice_)

        if self._infra_shared:
            if service_names:
                infra_cpus: set[int] = set()
                for cpuset in service_cpusets.values():
                    infra_cpus.update(parse_cpuset(cpuset))
                infra_cpuset = format_cpuset(sorted(infra_cpus))
            else:
                infra_cpuset = format_cpuset(pool[:1])
        elif self._infra_num_cores == 0:
            if service_names:
                infra_cpuset = service_cpusets[service_names[0]]
            else:
                infra_cpuset = format_cpuset(pool[:1])
        else:
            infra_cpuset = format_cpuset(infra_slice)

        return infra_cpuset, service_cpusets

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
        crs_names = list(self._crs_service_configs.keys())
        required_llms, required_sources = self._collect_required_llm_aliases(crs_names)
        llm_config: Optional[CrsComposeLLMConfig] = None
        litellm_config_path = (
            Path(self._litellm_config_path) if self._litellm_config_path else None
        )
        if self._skip_litellm:
            logger.info(
                f"skip_litellm=true; disabling LiteLLM for CRSs: {', '.join(crs_names)}"
            )
        else:
            if self._litellm_mode != "external":
                raise RuntimeError(
                    f"Unsupported litellm_mode='{self._litellm_mode}'. "
                    "CRSBench currently supports only litellm_mode='external'."
                )
            if litellm_config_path is not None:
                logger.info(
                    "litellm_config_path is ignored in external mode: "
                    f"{litellm_config_path}"
                )

            llm_config = CrsComposeLLMConfig(
                litellm=CrsComposeLiteLLMConfig(
                    mode="external",
                    model_check=True,
                    external=CrsComposeLiteLLMExternalConfig(
                        url=self._litellm_runtime_url,
                        key=self._litellm_runtime_api_key,
                    ),
                )
            )
            if not self._litellm_runtime_url or not self._litellm_runtime_api_key:
                raise RuntimeError(
                    "LiteLLM external mode requires explicit runtime URL/API key from "
                    "CRSBench (litellm_runtime_url, litellm_runtime_api_key)."
                )
            logger.info(
                "Configured oss-crs LiteLLM external mode for CRSs: "
                f"{', '.join(crs_names)}"
            )

        # Keep CRS-level required_llms information visible in logs.
        if required_llms and not self._skip_litellm:
            logger.info(
                "Required LLM aliases from CRS metadata "
                f"(validated by oss-crs in external mode): {', '.join(required_llms)} "
                f"[sources: {', '.join(required_sources)}]"
            )

        infra_cpuset, service_cpusets = self._assign_cpusets()
        infra_memory = (
            _normalize_optional_text(self._infra_mem_limit) or _default_memory_limit()
        )
        crs_entries: dict[str, CrsComposeCrsEntry] = {}
        for crs_name in crs_names:
            source = read_crs_source_from_registry(self._registry_dir, crs_name)
            service_cfg = self._crs_service_configs[crs_name]
            service_memory = (
                _normalize_optional_text(service_cfg.get("mem_limit")) or infra_memory
            )
            additional_env = {
                str(k): str(v)
                for k, v in dict(service_cfg.get("additional_env", {})).items()
            }
            if (
                "FUZZING_LANGUAGE" in additional_env
                and additional_env["FUZZING_LANGUAGE"] != self._fuzzing_language
            ):
                logger.warning(
                    "Overriding configured FUZZING_LANGUAGE=%s with benchmark language=%s for CRS=%s",
                    additional_env["FUZZING_LANGUAGE"],
                    self._fuzzing_language,
                    crs_name,
                )
            additional_env["SANITIZER"] = self._sanitizer
            additional_env["FUZZING_LANGUAGE"] = self._fuzzing_language
            crs_entries[crs_name] = CrsComposeCrsEntry(
                source=source,
                cpuset=service_cpusets[crs_name],
                memory=service_memory,
                additional_env=additional_env or None,
            )

        compose_yaml = CrsComposeYaml(
            docker_registry="",
            oss_crs_infra=CrsComposeInfra(
                cpuset=infra_cpuset,
                memory=infra_memory,
            ),
            crs_entries=crs_entries,
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
        # Reset lifecycle-scoped run state for each trial build invocation.
        self._run_id = self._configured_run_id
        self._resolved_artifacts = None

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

            build_succeeded = False
            try:
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
                )
                if rc != 0:
                    # oss-crs outputs errors via rich console to stdout
                    detail = stderr or stdout
                    msg = f"oss-crs build-target failed (rc={rc}): {detail}"
                    raise RuntimeError(msg)
                build_succeeded = True
            finally:
                if not build_succeeded:
                    docker_compose_down_cleanup(work_dir)

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

        if self._run_id is None:
            self._run_id = self._configured_run_id or generate_run_id()
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
        except RuntimeError as err:
            logger.info(
                "oss-crs artifacts pre-run unavailable for run_id=%s; "
                "continuing and will refresh post-run: %s",
                self._run_id,
                err,
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

        if self._run_id is None:
            self._run_id = self._configured_run_id or generate_run_id()

        try:
            stdout, stderr, rc, timed_out = run_oss_crs_run(
                compose_file,
                work_dir,
                staged_path,
                harness.name,
                timeout=self._run_timeout,
                oss_crs_cmd=self._oss_crs_cmd,
                run_id=self._run_id,
                stop_event=stop_event,
                pov_dir=pov_dir,
                diff=diff,
                seed_dir=seed_dir,
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

        # Refresh artifacts after run. Pre-run resolution can fail or return
        # incomplete paths before oss-crs materializes run outputs.
        self._refresh_artifacts_post_run(trial_output_dir, harness_name)

        # EXCHANGE_DIR is the source-of-truth for inter-container artifact transport.
        shared_dirs_by_crs = self._get_all_crs_artifact_paths("shared")
        exchange_pov_dir = self.exchange_pov_dir
        exchange_seed_dir = self.exchange_seed_dir
        exchange_patch_dir = self.exchange_patch_dir

        if (
            exchange_pov_dir is None
            and exchange_seed_dir is None
            and exchange_patch_dir is None
        ):
            logger.warning("No exchange_dir artifact paths available")

        if self._mode == "bug-finding":
            result = self._collect_bugfind_results(
                exchange_pov_dir=exchange_pov_dir,
                exchange_seed_dir=exchange_seed_dir,
                output_dir=output_dir,
                harness_name=harness_name,
            )
        else:
            result = self._collect_bugfix_results(
                exchange_patch_dir=exchange_patch_dir,
                exchange_pov_dir=exchange_pov_dir,
                output_dir=output_dir,
                harness_name=harness_name,
            )

        result.update(self._collect_run_logs(output_dir))
        result["shared_dirs_by_crs"] = {
            k: str(v) for k, v in sorted(shared_dirs_by_crs.items())
        }
        return result

    def _collect_bugfind_results(
        self,
        exchange_pov_dir: Optional[Path],
        exchange_seed_dir: Optional[Path],
        output_dir: Path,
        harness_name: str,
    ) -> dict[str, Any]:
        """Collect bug-finding artifacts (POVs and seeds)."""
        if exchange_pov_dir and exchange_pov_dir.exists():
            shutil.copytree(exchange_pov_dir, output_dir / "povs", dirs_exist_ok=True)
            logger.info(f"Copied POVs from {exchange_pov_dir}")

        if exchange_seed_dir and exchange_seed_dir.exists():
            shutil.copytree(exchange_seed_dir, output_dir / "seeds", dirs_exist_ok=True)
            logger.info(f"Copied seeds from {exchange_seed_dir}")

        return {
            "type": "bug-finding",
            "output_dir": str(output_dir),
            "harness": harness_name,
            "exchange_pov_dir": str(exchange_pov_dir) if exchange_pov_dir else None,
            "exchange_seed_dir": str(exchange_seed_dir) if exchange_seed_dir else None,
        }

    def _refresh_artifacts_post_run(
        self,
        trial_output_dir: Path,
        harness_name: str,
    ) -> None:
        """Refresh artifacts after run to capture finalized exchange/log paths."""
        if self._run_id is None or self._compose_file is None or self._work_dir is None:
            return

        staged_root = trial_output_dir / "staged"
        if not staged_root.exists():
            logger.warning(
                "Cannot refresh artifacts post-run: staged directory missing at "
                f"{staged_root}"
            )
            return

        staged_projects = [p for p in staged_root.iterdir() if p.is_dir()]
        if len(staged_projects) != 1:
            logger.warning(
                "Cannot refresh artifacts post-run: expected exactly one staged "
                f"project under {staged_root}, found {len(staged_projects)}"
            )
            return

        try:
            self._resolved_artifacts = run_oss_crs_artifacts(
                self._compose_file,
                self._work_dir,
                staged_projects[0],
                harness_name,
                self._run_id,
                oss_crs_cmd=self._oss_crs_cmd,
                sanitizer=self._sanitizer,
            )
            logger.info(f"Refreshed artifacts after run for run_id={self._run_id}")
        except RuntimeError:
            logger.warning(
                "Failed to refresh artifacts post-run",
                exc_info=True,
            )

    def _collect_bugfix_results(
        self,
        exchange_patch_dir: Optional[Path],
        exchange_pov_dir: Optional[Path],
        output_dir: Path,
        harness_name: str,
    ) -> dict[str, Any]:
        """Collect bug-fixing artifacts (patches and POVs)."""
        if exchange_patch_dir and exchange_patch_dir.exists():
            shutil.copytree(
                exchange_patch_dir, output_dir / "patches", dirs_exist_ok=True
            )
            logger.info(f"Copied patches from {exchange_patch_dir}")

        if exchange_pov_dir and exchange_pov_dir.exists():
            shutil.copytree(exchange_pov_dir, output_dir / "povs", dirs_exist_ok=True)
            logger.info(f"Copied POVs from {exchange_pov_dir}")

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
            "exchange_patch_dir": str(exchange_patch_dir)
            if exchange_patch_dir
            else None,
            "exchange_pov_dir": str(exchange_pov_dir) if exchange_pov_dir else None,
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
        litellm_mode=config.litellm_mode or "external",
        mode=mode,
    )
