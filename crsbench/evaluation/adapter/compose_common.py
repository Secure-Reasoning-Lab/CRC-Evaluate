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
        local_path=source.get("local_path"),
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
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        logger.warning("oss-crs prepare timed out after {}s", timeout)
        force_cleanup_work_dir_containers(work_dir)
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
    incremental_build: bool = False,
) -> tuple[str, str, int]:
    """Run ``oss-crs build-target`` to compile the target.

    Args:
        compose_file: Path to the crs-compose.yaml file.
        work_dir: Working directory for oss-crs.
        target_proj_path: Path to the benchmark project directory.
        oss_crs_cmd: Path to the oss-crs executable.
        timeout: Maximum time in seconds.
        incremental_build: Enable incremental build snapshots for RTS.

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
        "--fuzz-proj-path",
        str(target_proj_path),
    ]
    if incremental_build:
        cmd.append("--incremental-build")

    logger.debug(f"Running oss-crs build-target: {' '.join(cmd)}")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        logger.warning("oss-crs build-target timed out after {}s", timeout)
        force_cleanup_work_dir_containers(work_dir)
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
    run_id: Optional[str] = None,
    grace_period: int = 60,
    stop_event: Optional[threading.Event] = None,
    pov: Optional[Path] = None,
    pov_dir: Optional[Path] = None,
    diff: Optional[Path] = None,
    seed_dir: Optional[Path] = None,
    bug_candidate: Optional[Path] = None,
    bug_candidate_dir: Optional[Path] = None,
    incremental_build: bool = False,
    build_id: Optional[str] = None,
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
        run_id: Unique run identifier for deterministic path resolution.
        grace_period: Seconds to wait after SIGTERM before SIGKILL.
        stop_event: Threading event for early termination.
        pov: Path to a single POV file (bug-fixing).
        pov_dir: Path to a directory of POVs (bug-fixing).
        diff: Path to a reference diff file (bug-fixing).
        seed_dir: Path to seed corpus directory.
        bug_candidate: Path to a single bug-candidate report file (e.g. SARIF).
        bug_candidate_dir: Path to a directory of bug-candidate reports.
    Returns:
        Tuple of (stdout, stderr, returncode, timed_out).
    """
    if bug_candidate is not None and bug_candidate_dir is not None:
        raise ValueError(
            "bug_candidate and bug_candidate_dir are mutually exclusive; provide only one"
        )

    cmd = [
        oss_crs_cmd,
        "run",
        "--compose-file",
        str(compose_file),
        "--work-dir",
        str(work_dir),
        "--fuzz-proj-path",
        str(target_proj_path),
        "--target-harness",
        target_harness,
        "--timeout",
        str(timeout),
    ]

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
    if bug_candidate is not None:
        cmd.extend(["--bug-candidate", str(bug_candidate)])
    if bug_candidate_dir is not None:
        cmd.extend(["--bug-candidate-dir", str(bug_candidate_dir)])
    if incremental_build:
        cmd.append("--incremental-build")
    if build_id is not None:
        cmd.extend(["--build-id", build_id])

    logger.debug(f"Running oss-crs run: {' '.join(cmd)}")

    return run_with_graceful_timeout(
        cmd,
        timeout=timeout,
        grace_period=grace_period,
        stop_event=stop_event,
        env=None,
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
                proc = subprocess.run(
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
                    errors="replace",
                    timeout=60,
                )
                if proc.returncode != 0:
                    detail = proc.stderr or proc.stdout or "<no output>"
                    logger.warning(
                        f"docker compose down failed for {compose_file} "
                        f"(rc={proc.returncode}): {detail}"
                    )
            except (
                subprocess.TimeoutExpired,
                subprocess.SubprocessError,
                OSError,
            ) as e:
                logger.warning(
                    f"Failed to run docker compose down for {compose_file}: {e}"
                )

        # Intentionally avoid daemon-wide ``docker network prune`` here.
        # Global pruning can race with other concurrent trials that are
        # creating/using compose networks on the same Docker host.
    except Exception:
        logger.warning(f"Failed during Docker cleanup for work_dir {work_dir}")


def force_cleanup_work_dir_containers(work_dir: Path) -> None:
    """Force-kill any Docker container whose mounts reference ``work_dir``.

    Called on subprocess timeout of any oss-crs phase to ensure no
    daemon-owned container survives the Python subprocess kill. Complements
    ``docker_compose_down_cleanup`` which only enumerates compose files.

    Never raises -- this is cleanup code. Logs at warning level on failure.

    Root cause: GitHub issue #182 -- ``subprocess.run(timeout=...)`` only
    kills the Python child; Docker containers launched by oss-crs survive
    and leak file descriptors until the host EMFILEs out.
    """
    try:
        work_dir_resolved = str(work_dir.resolve())
    except OSError as exc:
        logger.warning(
            "force_cleanup: unable to resolve work_dir {}: {}", work_dir, exc
        )
        return

    # First pass: tear down any compose projects rooted in work_dir.
    # This reuses the existing belt-and-suspenders path. We cannot pass
    # --timeout 0 through the helper without rewriting it, and the helper's
    # own 30s compose-down timeout is acceptable here because it runs after
    # the subprocess is already declared dead.
    try:
        docker_compose_down_cleanup(work_dir)
    except Exception as exc:  # noqa: BLE001 -- cleanup code, never raises
        logger.warning("force_cleanup: compose-down pass failed: {}", exc)

    # Second pass: enumerate all containers (running + exited) and docker
    # kill any whose mount set references the absolute work_dir path.
    try:
        ps = subprocess.run(
            ["docker", "ps", "-aq"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        logger.warning("force_cleanup: docker ps failed: {}", exc)
        return

    container_ids = [cid for cid in ps.stdout.splitlines() if cid.strip()]
    if not container_ids:
        return

    # docker inspect -f on a list of ids; filter locally by mount source.
    try:
        inspect = subprocess.run(
            [
                "docker",
                "inspect",
                "-f",
                "{{.Id}} {{range .Mounts}}{{.Source}} {{end}}",
                *container_ids,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        logger.warning("force_cleanup: docker inspect failed: {}", exc)
        return

    to_kill: list[str] = []
    work_dir_with_sep = work_dir_resolved.rstrip("/") + "/"
    for line in inspect.stdout.splitlines():
        parts = line.strip().split()
        if len(parts) < 2:
            continue
        cid, mount_sources = parts[0], parts[1:]
        # Whole-path segment match: container mounts work_dir itself, or a
        # descendant of it. Substring matching would cross-kill sibling
        # trials (e.g. `/data/work/trial-1` into `/data/work/trial-10`).
        if any(
            src == work_dir_resolved or src.startswith(work_dir_with_sep)
            for src in mount_sources
        ):
            to_kill.append(cid)

    if not to_kill:
        return

    logger.warning(
        "force_cleanup: killing {} container(s) referencing work_dir {}",
        len(to_kill),
        work_dir_resolved,
    )
    # docker kill first, then docker rm -f to drop anything exited.
    try:
        subprocess.run(
            ["docker", "kill", *to_kill],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=30,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        logger.warning("force_cleanup: docker kill failed: {}", exc)

    try:
        subprocess.run(
            ["docker", "rm", "-f", *to_kill],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=30,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        logger.warning("force_cleanup: docker rm -f failed: {}", exc)

    _cleanup_orphaned_networks(work_dir_resolved)


def _cleanup_orphaned_networks(work_dir_resolved: str) -> None:
    """Remove Docker networks created by compose projects under *work_dir*.

    Scoped by work_dir path segment so concurrent trials on the same host
    are never affected.  Only removes networks with zero attached containers.
    """
    try:
        ls = subprocess.run(
            ["docker", "network", "ls", "--format", "{{.ID}} {{.Name}}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        logger.warning("force_cleanup: docker network ls failed: {}", exc)
        return

    work_dir_slug = work_dir_resolved.rstrip("/").replace("/", "-").lstrip("-")

    to_remove: list[str] = []
    for line in ls.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        net_id, net_name = parts
        if net_name in ("bridge", "host", "none"):
            continue
        if work_dir_slug not in net_name:
            continue
        try:
            info = subprocess.run(
                [
                    "docker",
                    "network",
                    "inspect",
                    net_id,
                    "--format",
                    "{{len .Containers}}",
                ],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=10,
            )
            if info.stdout.strip() == "0":
                to_remove.append(net_id)
        except (subprocess.SubprocessError, OSError):
            continue

    if not to_remove:
        return

    logger.warning(
        "force_cleanup: removing {} orphaned network(s) for work_dir {}",
        len(to_remove),
        work_dir_resolved,
    )
    for net_id in to_remove:
        try:
            subprocess.run(
                ["docker", "network", "rm", net_id],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=10,
            )
        except (subprocess.SubprocessError, OSError):
            pass


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
        "--fuzz-proj-path",
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
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
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
