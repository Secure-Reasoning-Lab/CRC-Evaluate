"""Atlantis/oss-crs coverage build helpers."""

from __future__ import annotations

import importlib
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

from crsbench.evaluation.adapter.compose_common import (
    run_oss_crs_build_target,
)
from crsbench.evaluation.adapter.config_gen import (
    CrsComposeCrsEntry,
    CrsComposeInfra,
    CrsComposeSource,
    CrsComposeYaml,
)
from crsbench.evaluation.adapter.oss_crs import _default_memory_limit
from crsbench.prepare.uniafl_backend import (
    default_uniafl_root,
    get_uniafl_prepare_readiness,
    prepare_image_refs,
    prepare_uniafl_backend,
)
from crsbench.utils.cpu_pool import format_cpuset
from crsbench.utils.docker import fix_docker_ownership

ATLANTIS_CRS_NAME = "atlantis-multilang-given_fuzzer"
DEFAULT_BUILD_TIMEOUT = 3600


@lru_cache(maxsize=1)
def _load_oss_crs_runtime_classes():
    # Atlantis contributes the source checkout used by prepare/build-target, but
    # build-id resolution must follow the active oss-crs CLI/package contract.
    # Prefer the ambient Python package and fall back to the repo-local checkout
    # when the current environment has not installed oss-crs as a module.
    try:
        compose_module = importlib.import_module("oss_crs.src.crs_compose")
        target_module = importlib.import_module("oss_crs.src.target")
    except ModuleNotFoundError:
        repo_root = Path(__file__).resolve().parents[3]
        oss_crs_root = repo_root / "oss-crs"
        if str(oss_crs_root) not in sys.path:
            sys.path.insert(0, str(oss_crs_root))
        compose_module = importlib.import_module("oss_crs.src.crs_compose")
        target_module = importlib.import_module("oss_crs.src.target")
    return compose_module.CRSCompose, target_module.Target


def _available_cpuset() -> str:
    try:
        cpus = sorted(os.sched_getaffinity(0))
    except AttributeError:
        total = os.cpu_count() or 1
        cpus = list(range(total))
    return format_cpuset(cpus) or "0"


def local_prepare_images_available(*, image_tag: str = "latest") -> bool:
    """Return whether the canonical Atlantis prepare images are present locally."""
    for image_ref in prepare_image_refs(image_tag=image_tag):
        inspect = subprocess.run(
            ["docker", "image", "inspect", image_ref],
            capture_output=True,
            text=True,
            errors="replace",
        )
        if inspect.returncode != 0:
            return False
    return True


def prepare_images_reusable(
    *,
    uniafl_root: Path | None = None,
    image_tag: str = "latest",
) -> bool:
    """Return whether the local Atlantis prepare image contract matches the checkout."""
    repo_root, issues = get_uniafl_prepare_readiness(
        uniafl_root or default_uniafl_root(),
        image_tag=image_tag,
    )
    del repo_root
    return not issues


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
        return
    if path.exists():
        shutil.rmtree(path)


COVERAGE_STAGE_IGNORES = frozenset({".aixcc", ".agent", ".git"})


def _read_project_sanitizer(project_dir: Path) -> str:
    """Read the primary sanitizer from a benchmark's project.yaml.

    Falls back to ``"address"`` when the file is missing or unparseable.
    """
    project_yaml = project_dir / "project.yaml"
    if not project_yaml.exists():
        return "address"
    try:
        data = yaml.safe_load(project_yaml.read_text())
        sanitizers = data.get("sanitizers") if isinstance(data, dict) else None
        if isinstance(sanitizers, list) and sanitizers:
            return str(sanitizers[0])
    except Exception:
        pass
    return "address"


def _ignore_coverage_metadata(_directory: str, contents: list[str]) -> list[str]:
    return [entry for entry in contents if entry in COVERAGE_STAGE_IGNORES]


def stage_benchmark_for_coverage(
    benchmark_path: Path, staged_project_dir: Path
) -> Path:
    """Stage benchmark files for oss-crs build-target without ground-truth dotdirs."""
    staged_project_dir = Path(staged_project_dir)
    _remove_path(staged_project_dir)
    staged_project_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        Path(benchmark_path),
        staged_project_dir,
        ignore=_ignore_coverage_metadata,
    )
    (staged_project_dir / ".dockerignore").write_text(
        ".aixcc\n**/.aixcc\n.agent\n**/.agent\n"
    )
    return staged_project_dir


def write_coverage_compose_yaml(
    *,
    compose_path: Path,
    uniafl_root: Path,
    cpuset: str | None = None,
    memory: str | None = None,
) -> Path:
    """Write a single-CRS oss-crs compose file bound to the local Atlantis checkout."""
    memory_limit = memory or _default_memory_limit()
    compose = CrsComposeYaml(
        docker_registry="",
        oss_crs_infra=CrsComposeInfra(
            cpuset=cpuset or _available_cpuset(),
            memory=memory_limit,
        ),
        crs_entries={
            ATLANTIS_CRS_NAME: CrsComposeCrsEntry(
                source=CrsComposeSource(local_path=str(Path(uniafl_root).resolve())),
                cpuset=cpuset or _available_cpuset(),
                memory=memory_limit,
            )
        },
    )
    compose_path.parent.mkdir(parents=True, exist_ok=True)
    compose.to_yaml(compose_path)
    return compose_path


def materialize_atlantis_build_output(
    *,
    atlantis_build_output_dir: Path,
    normalized_build_output_dir: Path,
) -> Path:
    """Expose Atlantis build outputs in the legacy `/out` layout expected by runtime code."""
    atlantis_build_output_dir = Path(atlantis_build_output_dir)
    normalized_build_output_dir = Path(normalized_build_output_dir)

    uniafl_build_dir = atlantis_build_output_dir / "uniafl" / "build"
    source_repo_dir = atlantis_build_output_dir / "uniafl" / "src"
    coverage_build_dir = atlantis_build_output_dir / "coverage" / "build"
    coverage_skip_file = atlantis_build_output_dir / "coverage" / ".build.skip"

    if not uniafl_build_dir.is_dir():
        raise FileNotFoundError(
            f"Missing Atlantis UniAFL build output: {uniafl_build_dir}"
        )
    if not source_repo_dir.is_dir():
        raise FileNotFoundError(f"Missing Atlantis source output: {source_repo_dir}")

    fix_docker_ownership(atlantis_build_output_dir)
    normalized_build_output_dir.mkdir(parents=True, exist_ok=True)
    fix_docker_ownership(normalized_build_output_dir)
    for child in list(normalized_build_output_dir.iterdir()):
        _remove_path(child)

    for artifact in sorted(uniafl_build_dir.iterdir()):
        destination = normalized_build_output_dir / artifact.name
        if artifact.is_dir():
            shutil.copytree(artifact, destination)
        else:
            shutil.copy2(artifact, destination)

    repo_link = normalized_build_output_dir / ".crsbench-repo"
    repo_link.symlink_to(source_repo_dir.resolve(), target_is_directory=True)

    if coverage_build_dir.exists():
        shutil.copytree(
            coverage_build_dir,
            normalized_build_output_dir / "coverage-out",
        )
    elif not coverage_skip_file.exists():
        raise FileNotFoundError(
            f"Missing Atlantis coverage build output: {coverage_build_dir}"
        )

    return repo_link


@dataclass(frozen=True)
class AtlantisCoverageBuild:
    """Resolved Atlantis build artifacts for one coverage variant."""

    compose_file: Path
    control_root: Path
    staged_project_dir: Path
    atlantis_build_output_dir: Path
    build_id: str
    source_repo_dir: Path
    normalized_build_output_dir: Path


def _resolve_atlantis_build_output(
    *,
    compose_file: Path,
    work_dir: Path,
    staged_project_dir: Path,
    sanitizer: str = "address",
    crs_name: str = ATLANTIS_CRS_NAME,
) -> tuple[Path, str]:
    crs_compose_cls, target_cls = _load_oss_crs_runtime_classes()
    compose = crs_compose_cls.from_yaml_file(
        compose_file,
        work_dir,
        skip_crs_init=True,
    )
    target = target_cls(work_dir, staged_project_dir, None)
    build_id = compose.get_latest_build_id(target, sanitizer)
    if build_id is None:
        raise RuntimeError(
            f"oss-crs did not produce a build id for {staged_project_dir.name}"
        )
    build_output_dir = compose.work_dir.get_build_output_dir(
        crs_name,
        target,
        build_id,
        sanitizer,
        create=False,
    )
    if not build_output_dir.exists():
        raise RuntimeError(
            f"Resolved Atlantis build output does not exist: {build_output_dir}"
        )
    return build_output_dir, build_id


def build_atlantis_coverage_artifacts(
    *,
    benchmark_path: Path,
    normalized_build_output_dir: Path,
    control_root: Path,
    uniafl_root: Path | None = None,
    oss_crs_cmd: str = "oss-crs",
    build_timeout: int = DEFAULT_BUILD_TIMEOUT,
) -> AtlantisCoverageBuild:
    """Run Atlantis oss-crs prepare/build-target and normalize outputs for runtime use."""
    control_root = Path(control_root)
    compose_file = control_root / "crs-compose.yaml"
    work_dir = control_root / "oss-crs-workdir"
    staged_project_dir = control_root / "staged" / Path(benchmark_path).name
    resolved_uniafl_root = Path(uniafl_root or default_uniafl_root()).resolve()

    write_coverage_compose_yaml(
        compose_path=compose_file,
        uniafl_root=resolved_uniafl_root,
    )
    stage_benchmark_for_coverage(benchmark_path, staged_project_dir)

    if not prepare_images_reusable(uniafl_root=resolved_uniafl_root):
        prepare_uniafl_backend(
            resolved_uniafl_root,
            oss_crs_cmd=oss_crs_cmd,
            timeout=build_timeout,
        )

    stdout, stderr, returncode = run_oss_crs_build_target(
        compose_file,
        work_dir,
        staged_project_dir,
        oss_crs_cmd=oss_crs_cmd,
        timeout=build_timeout,
    )
    if returncode != 0:
        detail = stderr or stdout
        raise RuntimeError(f"oss-crs build-target failed (rc={returncode}): {detail}")

    sanitizer = _read_project_sanitizer(staged_project_dir)
    atlantis_build_output_dir, build_id = _resolve_atlantis_build_output(
        compose_file=compose_file,
        work_dir=work_dir,
        staged_project_dir=staged_project_dir,
        sanitizer=sanitizer,
    )
    source_repo_dir = materialize_atlantis_build_output(
        atlantis_build_output_dir=atlantis_build_output_dir,
        normalized_build_output_dir=normalized_build_output_dir,
    )

    return AtlantisCoverageBuild(
        compose_file=compose_file,
        control_root=control_root,
        staged_project_dir=staged_project_dir,
        atlantis_build_output_dir=atlantis_build_output_dir,
        build_id=build_id,
        source_repo_dir=source_repo_dir,
        normalized_build_output_dir=normalized_build_output_dir,
    )
