"""File validation for benchmark format and structure."""

import os
from pathlib import Path
from typing import Any, Dict, List

import yaml

from crsbench.benchmark_ci.utils import (
    POV,
    Harness,
    Task,
    TaskMode,
    Vulnerability,
    get_benchmarks_root,
)
from crsbench.utils.logger import get_logger

logger = get_logger(__name__)


def check_benchmark_files(benchmark: str) -> None:
    """Check that benchmark has all required files with correct format.

    Args:
        benchmark: Benchmark name (e.g., "curl-delta-02")

    Raises:
        Exception: If any file validation fails
    """
    benchmarks_root = get_benchmarks_root()
    benchmark_dir = Path(benchmarks_root) / benchmark

    logger.info(f"Checking files for benchmark: {benchmark}")

    # Check benchmark directory exists
    if not benchmark_dir.exists():
        raise Exception(f"[Error] Benchmark directory not found: {benchmark_dir}")

    # Check .aixcc directory exists
    aixcc_dir = benchmark_dir / ".aixcc"
    if not aixcc_dir.exists():
        raise Exception(f"[Error] .aixcc directory not found: {aixcc_dir}")

    # Check meta.yaml exists
    meta_yaml_path = aixcc_dir / "meta.yaml"
    if not meta_yaml_path.exists():
        raise Exception(f"[Error] meta.yaml not found: {meta_yaml_path}")

    # Load and validate meta.yaml
    with meta_yaml_path.open() as f:
        meta_config = yaml.safe_load(f)

    _validate_meta_yaml_structure(meta_config, meta_yaml_path)

    # Check project.yaml exists
    project_yaml_path = benchmark_dir / "project.yaml"
    if not project_yaml_path.exists():
        raise Exception(f"[Error] project.yaml not found: {project_yaml_path}")

    # Load and validate project.yaml
    with project_yaml_path.open() as f:
        project_config = yaml.safe_load(f)

    _validate_project_yaml_structure(project_config, project_yaml_path)

    # Check harnesses and vulnerabilities
    _check_harness_files(meta_config, aixcc_dir)

    # Check test.sh exists and is executable
    test_sh_path = benchmark_dir / "test.sh"
    if test_sh_path.exists():
        if not os.access(test_sh_path, os.X_OK):
            logger.warning(f"test.sh exists but is not executable: {test_sh_path}")
    else:
        logger.warning(f"test.sh not found: {test_sh_path}")

    logger.info(f"File validation passed for {benchmark}")


def _validate_meta_yaml_structure(config: Dict[str, Any], path: Path) -> None:
    """Validate meta.yaml has required fields."""
    # Check harness_files exists
    if "harness_files" not in config:
        raise Exception(f"[Error] meta.yaml missing 'harness_files': {path}")

    if not isinstance(config["harness_files"], list):
        raise Exception(f"[Error] 'harness_files' must be a list: {path}")

    # Check delta_mode or full_mode exists
    has_delta = "delta_mode" in config
    has_full = "full_mode" in config

    if not has_delta and not has_full:
        raise Exception(
            f"[Error] meta.yaml must have 'delta_mode' or 'full_mode': {path}"
        )

    # Validate delta_mode structure
    if has_delta:
        delta_mode = config["delta_mode"]
        if "base_commit" not in delta_mode:
            raise Exception(f"[Error] delta_mode missing 'base_commit': {path}")
        if "ref_commit" not in delta_mode:
            raise Exception(f"[Error] delta_mode missing 'ref_commit': {path}")

    # Validate full_mode structure
    if has_full:
        full_mode = config["full_mode"]
        if "base_commit" not in full_mode:
            raise Exception(f"[Error] full_mode missing 'base_commit': {path}")


def _validate_project_yaml_structure(config: Dict[str, Any], path: Path) -> None:
    """Validate project.yaml has required fields."""
    required_fields = ["language", "main_repo"]

    for field in required_fields:
        if field not in config:
            raise Exception(f"[Error] project.yaml missing '{field}': {path}")

    # Check fuzzing_engines
    if "fuzzing_engines" in config:
        if not isinstance(config["fuzzing_engines"], list):
            raise Exception(f"[Error] 'fuzzing_engines' must be a list: {path}")

    # Check sanitizers
    if "sanitizers" in config:
        if not isinstance(config["sanitizers"], list):
            raise Exception(f"[Error] 'sanitizers' must be a list: {path}")


def _check_harness_files(meta_config: Dict[str, Any], aixcc_dir: Path) -> None:
    """Check that harness directories and files exist."""
    harness_files = meta_config.get("harness_files", [])

    for harness_entry in harness_files:
        harness_name = harness_entry.get("name")
        if not harness_name:
            raise Exception("[Error] Harness entry missing 'name' in meta.yaml")

        # Check harness directory in .aixcc
        harness_dir = aixcc_dir / harness_name

        # Check vulnerabilities if specified
        vulns = harness_entry.get("vulns", [])
        for vuln in vulns:
            vuln_keyword = vuln.get("vuln_keyword")
            if not vuln_keyword:
                raise Exception(
                    f"[Error] Vulnerability missing 'vuln_keyword' for harness {harness_name}"
                )

            vuln_dir = harness_dir / vuln_keyword
            if not vuln_dir.exists():
                logger.warning(f"Vulnerability directory not found: {vuln_dir}")
                continue

            # Check POVs
            povs = vuln.get("povs", [])
            for pov in povs:
                pov_id = pov.get("id")
                if not pov_id:
                    raise Exception(f"[Error] POV missing 'id' for vuln {vuln_keyword}")

                # Check POV blob exists
                pov_blob_path = vuln_dir / "blobs" / f"{pov_id}.blob"
                if not pov_blob_path.exists():
                    logger.warning(f"POV blob not found: {pov_blob_path}")

            # Check patch exists if present
            patches_dir = vuln_dir / "patches"
            if patches_dir.exists():
                patch_files = list(patches_dir.glob("*.diff"))
                if not patch_files:
                    logger.warning(
                        f"patches directory exists but no .diff files: {patches_dir}"
                    )


def get_tasks(benchmark: str) -> List[Task]:
    """Get tasks (delta/full mode) for a benchmark.

    Args:
        benchmark: Benchmark name

    Returns:
        List of Task objects
    """
    benchmarks_root = get_benchmarks_root()
    meta_yaml_path = Path(benchmarks_root) / benchmark / ".aixcc" / "meta.yaml"

    with meta_yaml_path.open() as f:
        meta_config = yaml.safe_load(f)

    tasks = []

    # Check for delta mode
    if "delta_mode" in meta_config:
        delta_mode = meta_config["delta_mode"]
        tasks.append(
            Task(
                mode=TaskMode.DELTA,
                base_commit=delta_mode["base_commit"],
                ref_commit=delta_mode["ref_commit"],
            )
        )

    # Check for full mode
    if "full_mode" in meta_config:
        full_mode = meta_config["full_mode"]
        tasks.append(Task(mode=TaskMode.FULL, base_commit=full_mode["base_commit"]))

    return tasks


def get_harnesses(benchmark: str) -> List[Harness]:
    """Get harnesses with vulnerabilities for a benchmark.

    Args:
        benchmark: Benchmark name

    Returns:
        List of Harness objects with vulnerabilities
    """
    benchmarks_root = get_benchmarks_root()
    benchmark_dir = Path(benchmarks_root) / benchmark
    meta_yaml_path = benchmark_dir / ".aixcc" / "meta.yaml"
    aixcc_dir = benchmark_dir / ".aixcc"

    with meta_yaml_path.open() as f:
        meta_config = yaml.safe_load(f)

    harnesses = []

    for harness_entry in meta_config.get("harness_files", []):
        harness_name = harness_entry["name"]
        harness_path = harness_entry.get("path", "")

        harness = Harness(name=harness_name, path=harness_path)

        # Parse vulnerabilities
        vulns = harness_entry.get("vulns", [])
        for vuln_entry in vulns:
            vuln_keyword = vuln_entry["vuln_keyword"]
            vuln_dir = aixcc_dir / harness_name / vuln_keyword

            # Load vuln.yaml if it exists
            vuln_yaml_path = vuln_dir / "vuln.yaml"
            vuln_name = vuln_keyword
            vuln_cwes = []

            if vuln_yaml_path.exists():
                with vuln_yaml_path.open() as f:
                    vuln_config = yaml.safe_load(f)
                    vuln_name = vuln_config.get("name", vuln_keyword)
                    vuln_cwes = vuln_config.get("cwes", [])

            vuln = Vulnerability(id=vuln_keyword, name=vuln_name, cwes=vuln_cwes)

            # Parse POVs
            povs = vuln_entry.get("povs", [])
            for pov_entry in povs:
                pov_id = pov_entry["id"]
                pov_sanitizer = pov_entry.get("sanitizer", "address")
                pov_error_token = pov_entry.get("error_token", "")

                pov_blob_path = vuln_dir / "blobs" / f"{pov_id}.blob"

                pov = POV(
                    id=pov_id,
                    sanitizer=pov_sanitizer,
                    error_token=pov_error_token,
                    blob_path=str(pov_blob_path),
                )
                vuln.povs.append(pov)

            # Check for patch file
            patches_dir = vuln_dir / "patches"
            if patches_dir.exists():
                patch_files = list(patches_dir.glob("patch_*.diff"))
                if patch_files:
                    vuln.patch_path = str(patch_files[0])

            harness.vulnerabilities.append(vuln)

        harnesses.append(harness)

    return harnesses


def get_project_config(benchmark: str) -> Dict[str, Any]:
    """Get project.yaml configuration for a benchmark.

    Args:
        benchmark: Benchmark name

    Returns:
        Project configuration dictionary
    """
    benchmarks_root = get_benchmarks_root()
    project_yaml_path = Path(benchmarks_root) / benchmark / "project.yaml"

    with project_yaml_path.open() as f:
        return yaml.safe_load(f)
