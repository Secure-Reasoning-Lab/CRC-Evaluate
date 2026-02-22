"""Shared subprocess wrappers for oss-crs CLI and registry reader.

Provides functions for all three oss-crs lifecycle phases
(prepare, build-target, run), Docker cleanup, CRS source resolution
from the registry, and artifact path resolution via ``oss-crs artifacts``.
"""

from __future__ import annotations

import json
import random
import string
import subprocess
import time
from typing import TYPE_CHECKING, Any, Optional

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
    """Read CRS source URL and ref from registry entry.

    Expected registry layout:
    ``<registry_dir>/<crs_name>.yaml`` (oss-crs format)

    Args:
        registry_dir: Path to CRS registry directory.
        crs_name: Name of the CRS to look up.

    Returns:
        CrsComposeSource with url and ref populated from registry entry.

    Raises:
        FileNotFoundError: If registry entry does not exist.
        ValueError: If the ``source`` key is missing from registry entry.
    """
    oss_crs_yaml = registry_dir / f"{crs_name}.yaml"
    if not oss_crs_yaml.exists():
        msg = (
            f"CRS registry entry not found for '{crs_name}' in {registry_dir}. "
            f"Expected: {oss_crs_yaml}"
        )
        raise FileNotFoundError(msg)

    with oss_crs_yaml.open("r") as f:
        pkg_data = yaml.safe_load(f)

    source = pkg_data.get("source")
    if not source:
        msg = f"'source' key not found in {oss_crs_yaml}"
        raise ValueError(msg)

    return CrsComposeSource(
        url=source.get("url"),
        ref=source.get("ref"),
    )


def run_oss_crs_prepare(
    compose_file: Path,
    work_dir: Path,
    *,
    oss_crs_cmd: str = "oss-crs",
    timeout: int = 3600,
) -> tuple[str, str, int]:
    """Run ``oss-crs prepare`` to build CRS Docker images.

    Args:
        compose_file: Path to the crs-compose.yaml file.
        work_dir: Working directory for oss-crs.
        oss_crs_cmd: Path to the oss-crs executable.
        timeout: Maximum time in seconds.

    Returns:
        Tuple of (stdout, stderr, returncode).

    Raises:
        RuntimeError: If oss-crs executable is not found.
    """
    cmd = [
        oss_crs_cmd,
        "prepare",
        "--compose-file",
        str(compose_file),
        "--work-dir",
        str(work_dir),
    ]
    logger.debug(f"Running oss-crs prepare: {' '.join(cmd)}")

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        logger.warning(f"oss-crs prepare timed out after {timeout}s")
        return ("", f"oss-crs prepare timed out after {timeout}s", -1)
    except FileNotFoundError as exc:
        msg = (
            f"oss-crs executable not found: '{oss_crs_cmd}'. "
            "Set oss_crs_cmd in crs_compose config to the full path."
        )
        raise RuntimeError(msg) from exc

    return result.stdout, result.stderr, result.returncode


def run_oss_crs_build_target(
    compose_file: Path,
    work_dir: Path,
    target_proj_path: Path,
    *,
    oss_crs_cmd: str = "oss-crs",
    timeout: int = 3600,
    target_repo_path: Optional[Path] = None,
    sanitizer: Optional[str] = None,
) -> tuple[str, str, int]:
    """Run ``oss-crs build-target`` to compile the target.

    Args:
        compose_file: Path to the crs-compose.yaml file.
        work_dir: Working directory for oss-crs.
        target_proj_path: Path to the benchmark project directory.
        oss_crs_cmd: Path to the oss-crs executable.
        timeout: Maximum time in seconds.
        target_repo_path: Path to pre-prepared source repository.
        sanitizer: Sanitizer to use (e.g., "address", "undefined").

    Returns:
        Tuple of (stdout, stderr, returncode).

    Raises:
        RuntimeError: If oss-crs executable is not found.
    """
    cmd = [
        oss_crs_cmd,
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
    if sanitizer is not None:
        cmd.extend(["--sanitizer", sanitizer])

    logger.debug(f"Running oss-crs build-target: {' '.join(cmd)}")

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        logger.warning(f"oss-crs build-target timed out after {timeout}s")
        return ("", f"oss-crs build-target timed out after {timeout}s", -1)
    except FileNotFoundError as exc:
        msg = (
            f"oss-crs executable not found: '{oss_crs_cmd}'. "
            "Set oss_crs_cmd in crs_compose config to the full path."
        )
        raise RuntimeError(msg) from exc

    return result.stdout, result.stderr, result.returncode


def run_oss_crs_run(
    compose_file: Path,
    work_dir: Path,
    target_proj_path: Path,
    target_harness: str,
    *,
    timeout: int,
    oss_crs_cmd: str = "oss-crs",
    sanitizer: Optional[str] = None,
    run_id: Optional[str] = None,
    grace_period: int = 60,
    stop_event: Optional[threading.Event] = None,
    pov: Optional[Path] = None,
    pov_dir: Optional[Path] = None,
    diff: Optional[Path] = None,
    seed_dir: Optional[Path] = None,
    external_litellm: bool = False,
    litellm_url: Optional[str] = None,
    litellm_api_key: Optional[str] = None,
) -> tuple[str, str, int, bool]:
    """Run ``oss-crs run`` with timeout and graceful shutdown.

    Delegates to ``run_with_graceful_timeout()`` for SIGTERM/SIGKILL
    lifecycle management.

    Args:
        compose_file: Path to the crs-compose.yaml file.
        work_dir: Working directory for oss-crs.
        target_proj_path: Path to the benchmark project directory.
        target_harness: Name of the harness to run.
        timeout: Maximum time in seconds for the run phase.
        oss_crs_cmd: Path to the oss-crs executable.
        sanitizer: Sanitizer to use (e.g., "address", "undefined").
        run_id: Unique run identifier for deterministic path resolution.
        grace_period: Seconds to wait after SIGTERM before SIGKILL.
        stop_event: Threading event for early termination.
        pov: Path to a single POV file (bug-fixing).
        pov_dir: Path to a directory of POVs (bug-fixing).
        diff: Path to a reference diff file (bug-fixing).
        seed_dir: Path to seed corpus directory.
        external_litellm: When True, configure runtime for external LiteLLM.
        litellm_url: Upstream LiteLLM proxy URL.
        litellm_api_key: Per-trial LiteLLM API key.

    Returns:
        Tuple of (stdout, stderr, returncode, timed_out).
    """
    cmd = [
        oss_crs_cmd,
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

    if sanitizer is not None:
        cmd.extend(["--sanitizer", sanitizer])
    if run_id is not None:
        cmd.extend(["--run-id", run_id])
    if pov is not None:
        cmd.extend(["--pov", str(pov)])
    if pov_dir is not None:
        cmd.extend(["--pov-dir", str(pov_dir)])
    if diff is not None:
        cmd.extend(["--diff", str(diff)])
    if seed_dir is not None:
        cmd.extend(["--seed-dir", str(seed_dir)])

    # New oss-crs LiteLLM integration is compose-driven.
    # Keep logging context for traceability; do not pass CLI flags.
    run_env: Optional[dict[str, str]] = None
    if external_litellm and litellm_url and litellm_api_key:
        key_suffix = litellm_api_key[-4:] if len(litellm_api_key) > 4 else "****"
        logger.debug(
            f"External LiteLLM configured: URL={litellm_url}, key=...{key_suffix}"
        )

    logger.debug(f"Running oss-crs run: {' '.join(cmd)}")

    return run_with_graceful_timeout(
        cmd,
        timeout=timeout,
        grace_period=grace_period,
        stop_event=stop_event,
        env=run_env,
    )


def docker_compose_down_cleanup(work_dir: Path) -> None:
    """Belt-and-suspenders Docker cleanup after oss-crs execution.

    oss-crs handles its own cleanup via TmpDockerCompose, but if the
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

        # Prune dangling Docker networks left by killed oss-crs processes
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


def generate_run_id() -> str:
    """Generate a unique run identifier for oss-crs.

    Uses timestamp plus a random suffix to avoid collisions when
    multiple runs start within the same second.
    """
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
    return f"run-{int(time.time())}-{suffix}"


def run_oss_crs_artifacts(
    compose_file: Path,
    work_dir: Path,
    target_proj_path: Path,
    target_harness: str,
    run_id: str,
    *,
    oss_crs_cmd: str = "oss-crs",
    sanitizer: str = "address",
) -> dict[str, Any]:
    """Resolve artifact paths via ``oss-crs artifacts``.

    Calls the oss-crs artifacts command to pre-resolve SUBMIT_DIR,
    EXCHANGE_DIR, and other artifact paths for a given run.  This
    replaces brittle glob-based path discovery.

    Args:
        compose_file: Path to the crs-compose.yaml file.
        work_dir: Working directory for oss-crs.
        target_proj_path: Path to the benchmark project directory.
        target_harness: Name of the harness.
        run_id: Unique run identifier (same as passed to ``oss-crs run``).
        oss_crs_cmd: Path to the oss-crs executable.
        sanitizer: Sanitizer name (e.g., "address").

    Returns:
        Parsed JSON dict from oss-crs artifacts output.

    Raises:
        RuntimeError: If the command fails or output cannot be parsed.
    """
    cmd = [
        oss_crs_cmd,
        "artifacts",
        "--compose-file",
        str(compose_file),
        "--work-dir",
        str(work_dir),
        "--target-proj-path",
        str(target_proj_path),
        "--target-harness",
        target_harness,
        "--run-id",
        run_id,
        "--sanitizer",
        sanitizer,
    ]

    logger.debug(f"Running oss-crs artifacts: {' '.join(cmd)}")

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except FileNotFoundError as exc:
        msg = (
            f"oss-crs executable not found: '{oss_crs_cmd}'. "
            "Set oss_crs_cmd in crs_compose config to the full path."
        )
        raise RuntimeError(msg) from exc
    except subprocess.TimeoutExpired as exc:
        msg = "oss-crs artifacts timed out after 60s"
        raise RuntimeError(msg) from exc

    if result.returncode != 0:
        detail = result.stderr or result.stdout
        msg = f"oss-crs artifacts failed (rc={result.returncode}): {detail}"
        raise RuntimeError(msg)

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        msg = f"Failed to parse oss-crs artifacts JSON: {exc}"
        raise RuntimeError(msg) from exc
