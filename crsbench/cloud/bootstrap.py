"""Shared bootstrap helpers for cloud-managed CRSBench VMs."""

from __future__ import annotations

import os
import subprocess
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal, TypeVar, cast

from crsbench.benchmark.discovery import auto_generate_meta_yaml
from crsbench.dataset.download import download_dataset, download_suite
from crsbench.dataset.registry import resolve_prefix
from crsbench.utils.cpu_pool import CPUPool, format_cpuset, resolve_parallel_job_plan
from crsbench.validation.schemas import (
    ExperimentConfig,
    _normalize_benchmark_selector_list,
)

PrepareMode = Literal["full", "skip_base_images"]
DownloadBenchmarksMode = Literal["auto", "always", "never"]
BenchmarkSelectorValue = list[str] | dict[str, list[str]]
BenchmarkSelectorInput = str | dict[str, BenchmarkSelectorValue]
BenchmarkSelectorList = list[BenchmarkSelectorInput]

CRSBENCH_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BENCHMARKS_ROOT = Path("benchmarks")
DEFAULT_BENCHMARK_SUITES_ROOT = Path("benchmark-suites")
MANAGED_OSS_FUZZ_ROOT = Path("third_party/oss-fuzz")
DEFAULT_DISCOVERY_BUILD_TIMEOUT = 3600
CRSBENCH_DOWNLOAD_DELAY_SEC_ENV = "CRSBENCH_DOWNLOAD_DELAY_SEC"
_DOWNLOAD_DELAY_WINDOW_SEC = 300
_DOWNLOAD_DELAY_SPACING_SEC = 10
_DOWNLOAD_DELAY_WAVE_SIZE = 3
T = TypeVar("T")
R = TypeVar("R")


@dataclass(frozen=True)
class CloudBenchmarkSelector:
    """Benchmark selector and roots needed by VM bootstrap."""

    benchmark_suite: str | None = None
    benchmarks: BenchmarkSelectorList | None = None
    benchmarks_root: Path | None = None
    benchmark_suites_root: Path | None = None

    @classmethod
    def from_inputs(
        cls,
        inputs: CloudVmBootstrapInputs,
    ) -> CloudBenchmarkSelector:
        """Normalize raw bootstrap inputs into one benchmark selector contract."""
        benchmark_suite = _normalize_optional_string(inputs.benchmark_suite)
        benchmarks = _normalize_benchmark_selector_list(inputs.benchmarks, "benchmarks")
        if benchmark_suite is not None and benchmarks is not None:
            raise ValueError(
                "Cloud VM bootstrap inputs cannot define both benchmark_suite and benchmarks"
            )
        if benchmark_suite is None and benchmarks is None:
            raise ValueError(
                "Cloud VM bootstrap inputs require benchmark_suite or benchmarks"
            )
        return cls(
            benchmark_suite=benchmark_suite,
            benchmarks=benchmarks,
            benchmarks_root=_normalize_override_path(
                inputs.benchmarks_root,
                default_path=DEFAULT_BENCHMARKS_ROOT,
            ),
            benchmark_suites_root=(
                _normalize_override_path(
                    inputs.benchmark_suites_root,
                    default_path=DEFAULT_BENCHMARK_SUITES_ROOT,
                )
                if benchmark_suite is not None
                else None
            ),
        )

    def effective_benchmarks_root(self) -> Path:
        """Return the root path used by VM-side benchmark download."""
        return self.benchmarks_root or DEFAULT_BENCHMARKS_ROOT

    def effective_benchmark_suites_root(self) -> Path:
        """Return the suite root path used by suite-backed downloads."""
        return self.benchmark_suites_root or DEFAULT_BENCHMARK_SUITES_ROOT

    def benchmark_names(self) -> list[str]:
        """Extract benchmark names from raw benchmark selectors."""
        if self.benchmarks is None:
            return []

        names: list[str] = []
        for benchmark in self.benchmarks:
            if isinstance(benchmark, str):
                names.append(benchmark)
                continue
            names.append(next(iter(benchmark)))
        return names


@dataclass(frozen=True)
class CloudVmBootstrapInputs:
    """Provider-neutral bootstrap inputs for one cloud VM."""

    prepare_mode: PrepareMode = "full"
    download_benchmarks: DownloadBenchmarksMode = "auto"
    gitcache: bool = False
    build_timeout: int = DEFAULT_DISCOVERY_BUILD_TIMEOUT
    benchmark_init_jobs: int | None = None
    benchmark_init_cores_per_job: int | None = None
    benchmark_suite: str | None = None
    benchmarks: BenchmarkSelectorList | None = None
    benchmarks_root: Path | str = DEFAULT_BENCHMARKS_ROOT
    benchmark_suites_root: Path | str = DEFAULT_BENCHMARK_SUITES_ROOT
    oss_fuzz_path: Path | str = MANAGED_OSS_FUZZ_ROOT

    @property
    def selector(self) -> CloudBenchmarkSelector:
        """Return the normalized selector for these raw bootstrap inputs."""
        return CloudBenchmarkSelector.from_inputs(self)

    @classmethod
    def from_experiment_config(
        cls,
        config: ExperimentConfig,
    ) -> CloudVmBootstrapInputs:
        bootstrap = config.cloud.bootstrap if config.cloud is not None else None
        benchmark_init_jobs, benchmark_init_cores_per_job = (
            _derive_benchmark_init_parallelism_from_experiment_config(config)
        )
        return cls(
            prepare_mode=bootstrap.prepare_mode if bootstrap is not None else "full",
            download_benchmarks=(
                bootstrap.download_benchmarks if bootstrap is not None else "auto"
            ),
            gitcache=bootstrap.gitcache if bootstrap is not None else False,
            build_timeout=config.build_timeout,
            benchmark_init_jobs=benchmark_init_jobs,
            benchmark_init_cores_per_job=benchmark_init_cores_per_job,
            benchmark_suite=config.benchmark_suite,
            benchmarks=config.benchmarks,
            benchmarks_root=_restore_repo_relative_path(
                Path(config.benchmarks_root),
                default_path=DEFAULT_BENCHMARKS_ROOT,
            ),
            benchmark_suites_root=_restore_repo_relative_path(
                Path(config.benchmark_suites_root),
                default_path=DEFAULT_BENCHMARK_SUITES_ROOT,
            ),
            oss_fuzz_path=_restore_repo_relative_path(
                Path(config.oss_fuzz_path),
                default_path=MANAGED_OSS_FUZZ_ROOT,
            ),
        )


def bootstrap_inputs_from_payload(payload: dict[str, Any]) -> CloudVmBootstrapInputs:
    """Decode VM bootstrap inputs from the worker metadata payload."""
    return CloudVmBootstrapInputs(
        prepare_mode=_coerce_prepare_mode(payload.get("prepare_mode")),
        download_benchmarks=_coerce_download_benchmarks_mode(
            payload.get("download_benchmarks")
        ),
        gitcache=bool(payload.get("gitcache", False)),
        build_timeout=_coerce_optional_positive_int(
            payload.get("build_timeout", DEFAULT_DISCOVERY_BUILD_TIMEOUT),
            field_name="build_timeout",
        )
        or DEFAULT_DISCOVERY_BUILD_TIMEOUT,
        benchmark_init_jobs=_coerce_optional_positive_int(
            payload.get("benchmark_init_jobs"),
            field_name="benchmark_init_jobs",
        ),
        benchmark_init_cores_per_job=_coerce_optional_positive_int(
            payload.get("benchmark_init_cores_per_job"),
            field_name="benchmark_init_cores_per_job",
        ),
        benchmark_suite=_normalize_optional_string(
            _coerce_optional_string(payload.get("benchmark_suite"))
        ),
        benchmarks=payload.get("benchmarks"),
        benchmarks_root=_coerce_root_path(
            payload.get("benchmarks_root"),
            default_path=DEFAULT_BENCHMARKS_ROOT,
        ),
        benchmark_suites_root=_coerce_root_path(
            payload.get("benchmark_suites_root"),
            default_path=DEFAULT_BENCHMARK_SUITES_ROOT,
        ),
        oss_fuzz_path=_coerce_root_path(
            payload.get("oss_fuzz_path"),
            default_path=MANAGED_OSS_FUZZ_ROOT,
        ),
    )


def should_download_benchmarks(inputs: CloudVmBootstrapInputs) -> bool:
    """Resolve the effective benchmark-download policy for a VM bootstrap."""
    policy = inputs.download_benchmarks
    if policy == "always":
        return True
    if policy == "never":
        return False
    if inputs.selector.benchmark_suite == "sanity":
        return False
    return True


def prepare_command_args(prepare_mode: PrepareMode) -> list[str]:
    """Return the `crsbench prepare` command for the selected mode."""
    cmd = ["crsbench", "prepare"]
    if prepare_mode == "full":
        return cmd
    if prepare_mode == "skip_base_images":
        return [*cmd, "--skip-base-images"]
    raise ValueError(f"Unsupported prepare mode: {prepare_mode}")


def run_prepare(
    prepare_mode: PrepareMode,
    *,
    cwd: Path | None = None,
    runner: Callable[..., object] | None = None,
) -> None:
    """Run `crsbench prepare` from a cloud VM checkout."""
    cmd = prepare_command_args(prepare_mode)
    if runner is None:
        if cwd is None:
            subprocess.run(cmd, check=True)
            return
        subprocess.run(cmd, cwd=cwd, check=True)
        return
    if cwd is None:
        runner(cmd, check=True)
        return
    runner(cmd, cwd=cwd, check=True)


def run_benchmark_download(
    selector: CloudBenchmarkSelector,
    *,
    download_suite_fn: Callable[..., list[Path]] | None = None,
    download_dataset_fn: Callable[..., Path] | None = None,
    cwd: Path | None = None,
    oss_fuzz_path: Path | str = MANAGED_OSS_FUZZ_ROOT,
    build_timeout: int = DEFAULT_DISCOVERY_BUILD_TIMEOUT,
    benchmark_init_jobs: int | None = None,
    benchmark_init_cores_per_job: int | None = None,
) -> list[Path]:
    """Download benchmarks required by one cloud VM bootstrap."""
    selected_download_suite = (
        download_suite if download_suite_fn is None else download_suite_fn
    )
    selected_download_dataset = (
        download_dataset if download_dataset_fn is None else download_dataset_fn
    )
    if selector.benchmark_suite:
        return selected_download_suite(
            selector.benchmark_suite,
            selector.effective_benchmarks_root(),
            selector.effective_benchmark_suites_root(),
            no_ground_truth=False,
        )
    external_paths = _prepare_external_benchmarks(
        selector,
        cwd=cwd,
        oss_fuzz_path=oss_fuzz_path,
        build_timeout=build_timeout,
        benchmark_init_jobs=benchmark_init_jobs,
        benchmark_init_cores_per_job=benchmark_init_cores_per_job,
    )
    if external_paths is not None:
        return external_paths
    if selector.benchmarks:
        results: list[Path] = []
        for dataset, names in _group_benchmarks_by_dataset(
            selector.benchmark_names()
        ).items():
            results.append(
                selected_download_dataset(
                    dataset,
                    selector.effective_benchmarks_root(),
                    benchmarks=names,
                    no_ground_truth=False,
                )
            )
        return results
    raise ValueError("Cloud VM bootstrap requires a benchmark selector")


def build_download_delay_schedule(
    *,
    orchestrator_name: str,
    worker_names: list[str],
    evaluator_names: list[str],
) -> dict[str, int]:
    """Build a conservative benchmark-download stagger schedule for one launch."""
    ordered_names = [orchestrator_name]
    if worker_names:
        ordered_names.append(worker_names[0])
    if evaluator_names:
        ordered_names.append(evaluator_names[0])
    ordered_names.extend(evaluator_names[1:])
    ordered_names.extend(worker_names[1:])

    if len(set(ordered_names)) != len(ordered_names):
        raise ValueError("Cloud download delay schedule requires unique instance names")

    return {
        instance_name: (index // _DOWNLOAD_DELAY_WAVE_SIZE) * _DOWNLOAD_DELAY_WINDOW_SEC
        + (index % _DOWNLOAD_DELAY_WAVE_SIZE) * _DOWNLOAD_DELAY_SPACING_SEC
        for index, instance_name in enumerate(ordered_names)
    }


def run_benchmark_download_with_delay(
    selector: CloudBenchmarkSelector,
    *,
    download_delay_sec: int,
    download_fn: Callable[[CloudBenchmarkSelector], list[Path]] | None = None,
) -> list[Path]:
    """Sleep for the scheduled delay, then download the selected benchmarks."""
    if download_delay_sec < 0:
        raise ValueError(
            f"{CRSBENCH_DOWNLOAD_DELAY_SEC_ENV} must be non-negative, got {download_delay_sec}"
        )
    if download_delay_sec > 0:
        time.sleep(download_delay_sec)
    selected_download = run_benchmark_download if download_fn is None else download_fn
    return selected_download(selector)


def run_cloud_vm_bootstrap(
    inputs: CloudVmBootstrapInputs,
    *,
    cwd: Path,
    runner: Callable[..., object] | None = None,
    download_suite_fn: Callable[..., list[Path]] | None = None,
    download_dataset_fn: Callable[..., Path] | None = None,
    download_delay_sec: int | None = None,
) -> list[Path]:
    """Run the shared prepare/download bootstrap sequence for a cloud VM."""
    run_prepare(inputs.prepare_mode, cwd=cwd, runner=runner)
    resolved_download_delay_sec = _resolve_download_delay_sec(download_delay_sec)
    if not should_download_benchmarks(inputs):
        return []
    if _selector_has_external_benchmarks(inputs.selector):
        return run_benchmark_download_with_delay(
            inputs.selector,
            download_delay_sec=resolved_download_delay_sec,
            download_fn=lambda selector: _prepare_external_benchmarks(
                selector,
                cwd=cwd,
                oss_fuzz_path=inputs.oss_fuzz_path,
                build_timeout=inputs.build_timeout,
                benchmark_init_jobs=inputs.benchmark_init_jobs,
                benchmark_init_cores_per_job=inputs.benchmark_init_cores_per_job,
            )
            or [],
        )
    return run_benchmark_download_with_delay(
        inputs.selector,
        download_delay_sec=resolved_download_delay_sec,
        download_fn=lambda selector: run_benchmark_download(
            selector,
            download_suite_fn=download_suite_fn,
            download_dataset_fn=download_dataset_fn,
            cwd=cwd,
            oss_fuzz_path=inputs.oss_fuzz_path,
            build_timeout=inputs.build_timeout,
            benchmark_init_jobs=inputs.benchmark_init_jobs,
            benchmark_init_cores_per_job=inputs.benchmark_init_cores_per_job,
        ),
    )


def _group_benchmarks_by_dataset(benchmarks: list[str]) -> OrderedDict[str, list[str]]:
    grouped, unknown = _split_benchmarks_by_dataset(benchmarks)
    if unknown:
        raise ValueError(f"Unknown benchmark selector: {unknown[0]}")
    return grouped


def _split_benchmarks_by_dataset(
    benchmarks: list[str],
) -> tuple[OrderedDict[str, list[str]], list[str]]:
    grouped: OrderedDict[str, list[str]] = OrderedDict()
    external_benchmarks: list[str] = []
    for benchmark in benchmarks:
        dataset = resolve_prefix(benchmark)
        if dataset is None:
            external_benchmarks.append(benchmark)
            continue
        grouped.setdefault(dataset, []).append(benchmark)
    return grouped, external_benchmarks


def _prepare_external_benchmarks(
    selector: CloudBenchmarkSelector,
    *,
    cwd: Path | None,
    oss_fuzz_path: Path | str = MANAGED_OSS_FUZZ_ROOT,
    build_timeout: int = DEFAULT_DISCOVERY_BUILD_TIMEOUT,
    benchmark_init_jobs: int | None = None,
    benchmark_init_cores_per_job: int | None = None,
) -> list[Path] | None:
    grouped, external_benchmarks = _external_benchmark_groups(selector)
    if grouped is None:
        return None
    if not external_benchmarks:
        return None

    return _resolve_external_benchmark_paths(
        external_benchmarks,
        benchmarks_root=selector.effective_benchmarks_root(),
        cwd=cwd,
        oss_fuzz_path=oss_fuzz_path,
        build_timeout=build_timeout,
        benchmark_init_jobs=benchmark_init_jobs,
        benchmark_init_cores_per_job=benchmark_init_cores_per_job,
    )


def _selector_has_external_benchmarks(selector: CloudBenchmarkSelector) -> bool:
    _, external_benchmarks = _external_benchmark_groups(selector)
    return bool(external_benchmarks)


def _external_benchmark_groups(
    selector: CloudBenchmarkSelector,
) -> tuple[OrderedDict[str, list[str]] | None, list[str]]:
    if selector.benchmarks is None:
        return None, []

    grouped, external_benchmarks = _split_benchmarks_by_dataset(
        selector.benchmark_names()
    )
    if grouped and external_benchmarks:
        dataset_benchmarks = [
            benchmark_name for names in grouped.values() for benchmark_name in names
        ]
        raise ValueError(
            "Cloud VM bootstrap cannot mix CRSBench dataset benchmarks with "
            f"external benchmarks in one explicit benchmark list: "
            f"dataset={dataset_benchmarks!r}, "
            f"external={external_benchmarks!r}"
        )
    return grouped, external_benchmarks


def _resolve_external_benchmark_paths(
    benchmark_names: list[str],
    *,
    benchmarks_root: Path,
    cwd: Path | None,
    oss_fuzz_path: Path | str = MANAGED_OSS_FUZZ_ROOT,
    build_timeout: int = DEFAULT_DISCOVERY_BUILD_TIMEOUT,
    benchmark_init_jobs: int | None = None,
    benchmark_init_cores_per_job: int | None = None,
) -> list[Path]:
    resolved_root = _resolve_root_path(benchmarks_root, cwd=cwd)
    resolved_oss_fuzz_root = _resolve_root_path(oss_fuzz_path, cwd=cwd)
    if _is_managed_oss_fuzz_projects_root(
        benchmarks_root,
        cwd=cwd,
        oss_fuzz_path=oss_fuzz_path,
    ):
        if cwd is None:
            raise ValueError(
                "Cloud VM bootstrap requires cwd when benchmarks_root points to "
                "managed third_party/oss-fuzz/projects"
            )

        def ensure_managed_project(
            benchmark_name: str,
            cpuset_cpus: str | None,
        ) -> Path:
            return _ensure_managed_oss_fuzz_project(
                benchmark_name,
                oss_fuzz_root=resolved_oss_fuzz_root,
                cpuset_cpus=cpuset_cpus,
                build_timeout=build_timeout,
            )

        return _run_cpuset_bounded_benchmark_inits(
            benchmark_names,
            benchmark_init_jobs=benchmark_init_jobs,
            benchmark_init_cores_per_job=benchmark_init_cores_per_job,
            worker=ensure_managed_project,
        )

    benchmark_paths = [
        resolved_root / benchmark_name for benchmark_name in benchmark_names
    ]
    missing = [
        benchmark_name
        for benchmark_name, benchmark_path in zip(
            benchmark_names, benchmark_paths, strict=True
        )
        if not benchmark_path.is_dir()
    ]
    if missing:
        raise ValueError(
            "External benchmark directories are missing under unmanaged "
            f"benchmarks_root {resolved_root}: {missing!r}. Pre-create those "
            "directories on the VM, or use benchmarks_root=third_party/oss-fuzz/projects "
            "to materialize them from the managed OSS-Fuzz checkout."
        )

    if cwd is None:
        missing_meta = [
            benchmark_path.name
            for benchmark_path in benchmark_paths
            if not (benchmark_path / ".aixcc" / "meta.yaml").is_file()
        ]
        if missing_meta:
            raise ValueError(
                "Cloud VM bootstrap requires cwd to auto-generate "
                f".aixcc/meta.yaml for external benchmarks: {missing_meta!r}"
            )
        return benchmark_paths

    def ensure_benchmark_path(benchmark_path: Path, cpuset_cpus: str | None) -> Path:
        return _ensure_external_meta_yaml_and_return_path(
            benchmark_path,
            oss_fuzz_root=resolved_oss_fuzz_root,
            cpuset_cpus=cpuset_cpus,
            build_timeout=build_timeout,
        )

    return _run_cpuset_bounded_benchmark_inits(
        benchmark_paths,
        benchmark_init_jobs=benchmark_init_jobs,
        benchmark_init_cores_per_job=benchmark_init_cores_per_job,
        worker=ensure_benchmark_path,
    )


def _ensure_managed_oss_fuzz_project(
    benchmark_name: str,
    *,
    oss_fuzz_root: Path,
    cpuset_cpus: str | None = None,
    build_timeout: int = DEFAULT_DISCOVERY_BUILD_TIMEOUT,
) -> Path:
    project_dir = oss_fuzz_root / "projects" / benchmark_name
    if not project_dir.is_dir():
        project_dir = _materialize_managed_oss_fuzz_project(
            benchmark_name,
            oss_fuzz_root=oss_fuzz_root,
        )
    if not project_dir.is_dir():
        raise ValueError(
            f"Managed OSS-Fuzz checkout did not produce projects/{benchmark_name}: "
            f"{project_dir}"
        )
    _ensure_external_meta_yaml(
        project_dir,
        oss_fuzz_root=oss_fuzz_root,
        cpuset_cpus=cpuset_cpus,
        build_timeout=build_timeout,
    )
    return project_dir


def _materialize_managed_oss_fuzz_project(
    benchmark_name: str,
    *,
    oss_fuzz_root: Path,
) -> Path:
    project_prefix = f"projects/{benchmark_name}"
    archive_cmd = [
        "git",
        "-C",
        str(oss_fuzz_root),
        "archive",
        "--format=tar",
        "HEAD",
        project_prefix,
    ]
    archive = subprocess.Popen(
        archive_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert archive.stdout is not None
    extract = subprocess.Popen(
        ["tar", "-xf", "-"],
        cwd=oss_fuzz_root,
        stdin=archive.stdout,
        stderr=subprocess.PIPE,
    )
    archive.stdout.close()
    _, archive_stderr = archive.communicate()
    _, extract_stderr = extract.communicate()
    if archive.returncode != 0 or extract.returncode != 0:
        archive_error = archive_stderr.decode("utf-8", errors="replace").strip()
        extract_error = extract_stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"Failed to materialize OSS-Fuzz project {benchmark_name!r} from "
            f"{oss_fuzz_root}: git archive rc={archive.returncode}, tar rc={extract.returncode}, "
            f"git stderr={archive_error!r}, tar stderr={extract_error!r}"
        )

    project_dir = oss_fuzz_root / project_prefix
    if not project_dir.is_dir():
        raise ValueError(
            f"Managed OSS-Fuzz checkout does not contain {project_prefix}. "
            "Verify the benchmark name matches an OSS-Fuzz project in third_party/oss-fuzz."
        )
    return project_dir


def _ensure_external_meta_yaml(
    benchmark_path: Path,
    *,
    oss_fuzz_root: Path,
    cpuset_cpus: str | None = None,
    build_timeout: int = DEFAULT_DISCOVERY_BUILD_TIMEOUT,
) -> Path:
    meta_yaml_path = benchmark_path / ".aixcc" / "meta.yaml"
    if meta_yaml_path.is_file():
        return meta_yaml_path
    return auto_generate_meta_yaml(
        benchmark_path,
        oss_fuzz_root,
        cpuset_cpus=cpuset_cpus,
        build_timeout=build_timeout,
    )


def _ensure_external_meta_yaml_and_return_path(
    benchmark_path: Path,
    *,
    oss_fuzz_root: Path,
    cpuset_cpus: str | None = None,
    build_timeout: int = DEFAULT_DISCOVERY_BUILD_TIMEOUT,
) -> Path:
    _ensure_external_meta_yaml(
        benchmark_path,
        oss_fuzz_root=oss_fuzz_root,
        cpuset_cpus=cpuset_cpus,
        build_timeout=build_timeout,
    )
    return benchmark_path


def _run_cpuset_bounded_benchmark_inits(
    items: list[T],
    *,
    benchmark_init_jobs: int | None,
    benchmark_init_cores_per_job: int | None,
    worker: Callable[[T, str | None], R],
) -> list[R]:
    if not items:
        return []

    plan = resolve_parallel_job_plan(
        len(items),
        requested_jobs=benchmark_init_jobs,
        requested_cores_per_job=benchmark_init_cores_per_job,
    )
    if plan is None:
        return [worker(item, None) for item in items]

    max_parallel_jobs, cores_per_job = plan
    cpu_pool = CPUPool()

    def run_with_allocated_cpus(item: T) -> R:
        allocated_cpus = cpu_pool.allocate(cores_per_job)
        if allocated_cpus is None:
            raise RuntimeError(
                "cloud bootstrap CPU allocation failed despite bounded executor sizing"
            )
        assigned_cpuset = format_cpuset(allocated_cpus)
        try:
            return worker(item, assigned_cpuset)
        finally:
            cpu_pool.release(allocated_cpus)

    if max_parallel_jobs == 1:
        return [run_with_allocated_cpus(item) for item in items]

    results: list[R | None] = [None] * len(items)
    with ThreadPoolExecutor(max_workers=max_parallel_jobs) as executor:
        futures = {
            executor.submit(run_with_allocated_cpus, item): index
            for index, item in enumerate(items)
        }
        for future in as_completed(futures):
            results[futures[future]] = future.result()
    return cast("list[R]", results)


def _resolve_root_path(path: Path | str, *, cwd: Path | None) -> Path:
    root = Path(path)
    if root.is_absolute() or cwd is None:
        return root
    return cwd / root


def _is_managed_oss_fuzz_projects_root(
    path: Path,
    *,
    cwd: Path | None,
    oss_fuzz_path: Path | str = MANAGED_OSS_FUZZ_ROOT,
) -> bool:
    return _resolve_root_path(path, cwd=cwd) == (
        _resolve_root_path(oss_fuzz_path, cwd=cwd) / "projects"
    )


def _derive_benchmark_init_parallelism_from_experiment_config(
    config: ExperimentConfig,
) -> tuple[int | None, int | None]:
    from crsbench.distributed.registry import RuntimeRegistration

    registration = RuntimeRegistration.from_experiment_config(config)
    return _derive_benchmark_init_parallelism_from_registration(registration)


def _derive_benchmark_init_parallelism_from_registration(
    registration: Any,
) -> tuple[int | None, int | None]:
    worker_jobs = getattr(registration, "worker_jobs", None)
    worker_cores_per_job = getattr(registration, "worker_cores_per_job", None)
    cores_per_trial = getattr(registration, "cores_per_trial", None)
    if (
        worker_jobs is not None
        or worker_cores_per_job is not None
        or cores_per_trial is not None
    ):
        return worker_jobs or 1, worker_cores_per_job or cores_per_trial

    evaluator_build_jobs = getattr(registration, "evaluator_build_jobs", None)
    evaluator_build_cores_per_job = getattr(
        registration,
        "evaluator_build_cores_per_job",
        None,
    )
    if evaluator_build_jobs is not None or evaluator_build_cores_per_job is not None:
        return evaluator_build_jobs or 1, evaluator_build_cores_per_job

    return None, None


def _resolve_download_delay_sec(download_delay_sec: int | None) -> int:
    if download_delay_sec is not None:
        return download_delay_sec

    raw_value = os.environ.get(CRSBENCH_DOWNLOAD_DELAY_SEC_ENV, "")
    if raw_value == "":
        return 0
    try:
        parsed = int(raw_value)
    except ValueError as exc:
        raise ValueError(
            f"{CRSBENCH_DOWNLOAD_DELAY_SEC_ENV} must be an integer number of seconds, got {raw_value!r}"
        ) from exc
    if parsed < 0:
        raise ValueError(
            f"{CRSBENCH_DOWNLOAD_DELAY_SEC_ENV} must be non-negative, got {parsed}"
        )
    return parsed


def _normalize_optional_string(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _coerce_optional_string(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _coerce_optional_positive_int(value: Any, *, field_name: str) -> int | None:
    if value is None:
        return None
    parsed = int(value)
    if parsed <= 0:
        raise ValueError(f"{field_name} must be > 0, got {parsed}")
    return parsed


def _coerce_root_path(value: Any, *, default_path: Path) -> Path:
    if value is None:
        return default_path
    return Path(str(value))


def _coerce_prepare_mode(value: Any) -> PrepareMode:
    mode = str(value or "full")
    if mode not in ("full", "skip_base_images"):
        raise ValueError(f"Unsupported prepare mode in cloud bootstrap payload: {mode}")
    return cast("PrepareMode", mode)


def _coerce_download_benchmarks_mode(value: Any) -> DownloadBenchmarksMode:
    mode = str(value or "auto")
    if mode not in ("auto", "always", "never"):
        raise ValueError(
            f"Unsupported benchmark download mode in cloud bootstrap payload: {mode}"
        )
    return cast("DownloadBenchmarksMode", mode)


def _normalize_override_path(
    value: Path | str,
    *,
    default_path: Path,
) -> Path | None:
    path = Path(value)
    if path == default_path:
        return None
    return path


def _restore_repo_relative_path(path: Path, *, default_path: Path) -> Path:
    if not path.is_absolute():
        return default_path if path == default_path else path

    try:
        resolved_path = path.resolve()
        resolved_default = (CRSBENCH_REPO_ROOT / default_path).resolve()
        if resolved_path == resolved_default:
            return default_path
        return resolved_path.relative_to(CRSBENCH_REPO_ROOT)
    except (OSError, ValueError):
        return path
