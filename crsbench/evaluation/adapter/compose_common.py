"""Shared subprocess wrappers for crs-compose CLI and registry reader.

Provides functions for all three crs-compose lifecycle phases
(prepare, build-target, run), Docker cleanup, CRS source resolution
from the registry, and artifact directory discovery.
"""

from __future__ import annotations

import os
import subprocess
from typing import TYPE_CHECKING, Optional

import yaml

from crsbench.evaluation.adapter.config_gen import CrsComposeSource
from crsbench.evaluation.process_utils import run_with_graceful_timeout
from crsbench.utils.logger import get_logger

if TYPE_CHECKING:
    import threading
    from pathlib import Path

logger = get_logger(__name__)


def read_crs_source_from_registry(
    registry_dir: Path,
    crs_name: str,
) -> CrsComposeSource:
    """Read CRS source URL and ref from registry pkg.yaml.

    Follows the same pattern as ``get_crs_type()`` in
    ``crsbench/distributed/jobs.py``.

    Args:
        registry_dir: Path to CRS registry directory.
        crs_name: Name of the CRS to look up.

    Returns:
        CrsComposeSource with url and ref populated from pkg.yaml.

    Raises:
        FileNotFoundError: If pkg.yaml does not exist.
        ValueError: If the ``source`` key is missing from pkg.yaml.
    """
    pkg_yaml_path = registry_dir / crs_name / "pkg.yaml"

    if not pkg_yaml_path.exists():
        msg = f"CRS package file not found: {pkg_yaml_path}"
        raise FileNotFoundError(msg)

    with pkg_yaml_path.open("r") as f:
        pkg_data = yaml.safe_load(f)

    source = pkg_data.get("source")
    if not source:
        msg = f"'source' key not found in {pkg_yaml_path}"
        raise ValueError(msg)

    return CrsComposeSource(
        url=source.get("url"),
        ref=source.get("ref"),
    )


def run_crs_compose_prepare(
    compose_file: Path,
    work_dir: Path,
    *,
    crs_compose_cmd: str = "crs-compose",
    timeout: int = 3600,
) -> tuple[str, str, int]:
    """Run ``crs-compose prepare`` to build CRS Docker images.

    Args:
        compose_file: Path to the crs-compose.yaml file.
        work_dir: Working directory for crs-compose.
        crs_compose_cmd: Path to the crs-compose executable.
        timeout: Maximum time in seconds.

    Returns:
        Tuple of (stdout, stderr, returncode).

    Raises:
        RuntimeError: If crs-compose executable is not found.
    """
    cmd = [
        crs_compose_cmd,
        "prepare",
        "--compose-file",
        str(compose_file),
        "--work-dir",
        str(work_dir),
    ]
    logger.debug(f"Running crs-compose prepare: {' '.join(cmd)}")

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        logger.warning(f"crs-compose prepare timed out after {timeout}s")
        return ("", f"crs-compose prepare timed out after {timeout}s", -1)
    except FileNotFoundError as exc:
        msg = (
            f"crs-compose executable not found: '{crs_compose_cmd}'. "
            "Set crs_compose_cmd in crs_compose config to the full path."
        )
        raise RuntimeError(msg) from exc

    return result.stdout, result.stderr, result.returncode


def run_crs_compose_build_target(
    compose_file: Path,
    work_dir: Path,
    target_proj_path: Path,
    *,
    crs_compose_cmd: str = "crs-compose",
    timeout: int = 3600,
    target_repo_path: Optional[Path] = None,
) -> tuple[str, str, int]:
    """Run ``crs-compose build-target`` to compile the target.

    Args:
        compose_file: Path to the crs-compose.yaml file.
        work_dir: Working directory for crs-compose.
        target_proj_path: Path to the benchmark project directory.
        crs_compose_cmd: Path to the crs-compose executable.
        timeout: Maximum time in seconds.
        target_repo_path: Path to pre-prepared source repository.

    Returns:
        Tuple of (stdout, stderr, returncode).

    Raises:
        RuntimeError: If crs-compose executable is not found.
    """
    cmd = [
        crs_compose_cmd,
        "build-target",
        "--compose-file",
        str(compose_file),
        "--work-dir",
        str(work_dir),
        "--target-proj-path",
        str(target_proj_path),
    ]

    if target_repo_path is not None:
        cmd.extend(["--target-repo-path", str(target_repo_path)])

    logger.debug(f"Running crs-compose build-target: {' '.join(cmd)}")

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        logger.warning(f"crs-compose build-target timed out after {timeout}s")
        return ("", f"crs-compose build-target timed out after {timeout}s", -1)
    except FileNotFoundError as exc:
        msg = (
            f"crs-compose executable not found: '{crs_compose_cmd}'. "
            "Set crs_compose_cmd in crs_compose config to the full path."
        )
        raise RuntimeError(msg) from exc

    return result.stdout, result.stderr, result.returncode


def run_crs_compose_run(
    compose_file: Path,
    work_dir: Path,
    target_proj_path: Path,
    target_harness: str,
    *,
    timeout: int,
    crs_compose_cmd: str = "crs-compose",
    grace_period: int = 60,
    stop_event: Optional[threading.Event] = None,
    pov: Optional[Path] = None,
    pov_dir: Optional[Path] = None,
    diff: Optional[Path] = None,
    corpus_dir: Optional[Path] = None,
    external_litellm: bool = False,
    litellm_url: Optional[str] = None,
    litellm_api_key: Optional[str] = None,
) -> tuple[str, str, int, bool]:
    """Run ``crs-compose run`` with timeout and graceful shutdown.

    Delegates to ``run_with_graceful_timeout()`` for SIGTERM/SIGKILL
    lifecycle management.

    Args:
        compose_file: Path to the crs-compose.yaml file.
        work_dir: Working directory for crs-compose.
        target_proj_path: Path to the benchmark project directory.
        target_harness: Name of the harness to run.
        timeout: Maximum time in seconds for the run phase.
        crs_compose_cmd: Path to the crs-compose executable.
        grace_period: Seconds to wait after SIGTERM before SIGKILL.
        stop_event: Threading event for early termination.
        pov: Path to a single POV file (bug-fixing).
        pov_dir: Path to a directory of POVs (bug-fixing).
        diff: Path to a reference diff file (bug-fixing).
        corpus_dir: Path to seed corpus directory.
        external_litellm: When True, pass ``--external-litellm`` flag and
            inject ``LITELLM_URL`` / ``LITELLM_KEY`` env vars so the CRS
            routes LLM traffic through CRSBench's upstream proxy.
        litellm_url: Upstream LiteLLM proxy URL.
        litellm_api_key: Per-trial LiteLLM API key.

    Returns:
        Tuple of (stdout, stderr, returncode, timed_out).
    """
    cmd = [
        crs_compose_cmd,
        "run",
        "--compose-file",
        str(compose_file),
        "--work-dir",
        str(work_dir),
        "--target-proj-path",
        str(target_proj_path),
        "--target-harness",
        target_harness,
        "--timeout",
        str(timeout),
    ]

    if pov is not None:
        cmd.extend(["--pov", str(pov)])
    if pov_dir is not None:
        cmd.extend(["--pov-dir", str(pov_dir)])
    if diff is not None:
        cmd.extend(["--diff", str(diff)])
    if corpus_dir is not None:
        cmd.extend(["--corpus", str(corpus_dir)])

    # Build subprocess environment for external LiteLLM proxy routing
    run_env: Optional[dict[str, str]] = None
    if external_litellm:
        cmd.append("--external-litellm")
        if litellm_url and litellm_api_key:
            run_env = {
                **os.environ,
                "LITELLM_URL": litellm_url,
                "LITELLM_KEY": litellm_api_key,
            }
            key_suffix = litellm_api_key[-4:] if len(litellm_api_key) > 4 else "****"
            logger.debug(
                f"External LiteLLM enabled: URL={litellm_url}, key=...{key_suffix}"
            )

    logger.debug(f"Running crs-compose run: {' '.join(cmd)}")

    return run_with_graceful_timeout(
        cmd,
        timeout=timeout,
        grace_period=grace_period,
        stop_event=stop_event,
        env=run_env,
    )


def docker_compose_down_cleanup(work_dir: Path) -> None:
    """Belt-and-suspenders Docker cleanup after crs-compose execution.

    crs-compose handles its own cleanup via TmpDockerCompose, but if the
    process is killed before cleanup completes, containers may remain.
    This function scans the work directory for docker-compose files and
    runs ``docker compose down --remove-orphans`` on each.

    Never raises exceptions -- this is cleanup code.
    """
    try:
        compose_files = list(work_dir.rglob("docker-compose.yaml")) + list(
            work_dir.rglob("docker-compose.yml")
        )

        for compose_file in compose_files:
            try:
                logger.debug(f"Running docker compose down for {compose_file}")
                subprocess.run(
                    [
                        "docker",
                        "compose",
                        "-f",
                        str(compose_file),
                        "down",
                        "--remove-orphans",
                        "--timeout",
                        "30",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
            except (
                subprocess.TimeoutExpired,
                subprocess.SubprocessError,
                OSError,
            ):
                logger.warning(f"Failed to run docker compose down for {compose_file}")

        # Prune dangling Docker networks left by killed crs-compose processes
        try:
            subprocess.run(
                ["docker", "network", "prune", "-f"],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (subprocess.TimeoutExpired, subprocess.SubprocessError, OSError):
            logger.debug("Failed to prune Docker networks")
    except Exception:
        logger.warning(f"Failed during Docker cleanup for work_dir {work_dir}")


def find_submit_dir(
    work_dir: Path,
    crs_name: str,
    harness_name: str,
) -> Optional[Path]:
    """Locate the SUBMIT_DIR for a CRS and harness in the work directory.

    Matches oss-crs's ``CRS.get_submit_dir()`` convention::

        <work_dir>/crs_compose/<config_hash>/<sanitizer>/runs/<run_id>/crs/<crs_name>/<target_key>/SUBMIT_DIR/<harness>/

    Each ``*`` maps to a single unknown segment (hash, sanitizer, run-id,
    target-key).  This is intentionally explicit rather than ``**`` so that
    a layout change in oss-crs causes a visible failure instead of silently
    matching the wrong path.

    Args:
        work_dir: crs-compose working directory.
        crs_name: Name of the CRS.
        harness_name: Name of the harness.

    Returns:
        Path to the SUBMIT_DIR/<harness> directory, or None if not found.
    """
    pattern = f"crs_compose/*/*/runs/*/crs/{crs_name}/*/SUBMIT_DIR/{harness_name}"
    matches = list(work_dir.glob(pattern))

    if not matches:
        logger.warning(
            f"No SUBMIT_DIR found for CRS '{crs_name}', harness '{harness_name}' in {work_dir}"
        )
        return None

    if len(matches) > 1:
        logger.warning(
            f"Multiple SUBMIT_DIRs found for CRS '{crs_name}', harness '{harness_name}': {matches}. Using first."
        )

    return matches[0]
