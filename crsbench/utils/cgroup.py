"""Cgroup v2 management for resource control.

This module provides functions to create and manage cgroup v2 hierarchies
for Docker container resource management using cgroup-parent.

Functions:
- Preflight: check_cgroup_v2_available, check_docker_cgroup_driver,
             check_cgroup_delegation, run_preflight_checks
- Lifecycle: get_user_cgroup_base, setup_cgroup_hierarchy, create_cgroup
- Cleanup: cleanup_cgroup, cleanup_stale_cgroups
- Helpers: cgroup_path_for_docker, format_setup_instructions,
           format_docker_cgroup_driver_instructions
"""

import errno
import os
import signal
import subprocess
import time
from pathlib import Path

from crsbench.utils.logger import get_logger

logger = get_logger(__name__)

CGROUP_FS_ROOT = "/sys/fs/cgroup"


class CgroupError(Exception):
    """Raised when cgroup operations fail with actionable context."""


# ---------------------------------------------------------------------------
# Preflight checks
# ---------------------------------------------------------------------------


def check_cgroup_v2_available() -> bool:
    """Check if cgroup v2 is available on the system.

    Returns:
        True if cgroup v2 unified hierarchy is available.
    """
    cgroup_mount = Path(CGROUP_FS_ROOT)
    if not cgroup_mount.exists():
        return False

    controllers_file = cgroup_mount / "cgroup.controllers"
    return controllers_file.exists()


def check_docker_cgroup_driver() -> tuple[bool, str]:
    """Check if Docker is using the cgroupfs driver.

    Returns:
        Tuple of (is_cgroupfs, driver_name).
        On failure returns (False, "unknown") -- failing closed is safer
        than assuming cgroupfs when Docker is unreachable.
    """
    try:
        result = subprocess.run(
            ["docker", "info", "--format", "{{.CgroupDriver}}"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        driver = result.stdout.strip()
        return driver == "cgroupfs", driver
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        FileNotFoundError,
    ):
        return False, "unknown"


def check_cgroup_delegation(base_path: Path) -> tuple[bool, list[str]]:
    """Verify that required cgroup controllers are delegated.

    Args:
        base_path: Base cgroup directory path (e.g. .../crsbench).

    Returns:
        Tuple of (is_valid, missing_controllers).
    """
    required_controllers = {"cpuset", "memory"}

    if not base_path.exists():
        return False, sorted(required_controllers)

    parent_path = base_path.parent
    subtree_control_file = parent_path / "cgroup.subtree_control"

    if not subtree_control_file.exists():
        return False, sorted(required_controllers)

    try:
        enabled = set(subtree_control_file.read_text().strip().split())
        missing = required_controllers - enabled
        return len(missing) == 0, sorted(missing)
    except (OSError, PermissionError):
        return False, sorted(required_controllers)


def run_preflight_checks() -> Path:
    """Run all cgroup v2 preflight checks and return the base path.

    This is the single entry point that supervisors call at startup.
    Raises CgroupError with actionable instructions on any failure.

    Returns:
        The user cgroup base path ready for use.

    Raises:
        CgroupError: If any preflight check fails.
    """
    if not check_cgroup_v2_available():
        raise CgroupError(
            "Cgroup v2 (unified hierarchy) is not available on this system.\n"
            "CRSBench requires cgroup v2 for CPU pinning.\n\n"
            "Check your system:\n"
            "  stat /sys/fs/cgroup/cgroup.controllers\n\n"
            "Cgroup v2 requires Linux 4.15+ and must be enabled as the "
            "default hierarchy.\n"
            "On older systems, add 'systemd.unified_cgroup_hierarchy=1' "
            "to the kernel command line."
        )

    is_cgroupfs, driver = check_docker_cgroup_driver()
    if not is_cgroupfs:
        raise CgroupError(
            f"Docker cgroup driver is '{driver}', but cgroupfs is required.\n\n"
            + format_docker_cgroup_driver_instructions()
        )

    base_path = get_user_cgroup_base()

    is_delegated, missing = check_cgroup_delegation(base_path)
    if not is_delegated:
        raise CgroupError(
            f"Cgroup delegation not configured. Missing controllers: "
            f"{', '.join(missing)}\n\n" + format_setup_instructions(base_path)
        )

    logger.info(f"Cgroup v2 preflight passed: driver={driver}, base={base_path}")
    return base_path


# ---------------------------------------------------------------------------
# Lifecycle management
# ---------------------------------------------------------------------------


def get_user_cgroup_base() -> Path:
    """Get the base cgroup path for the current user.

    Returns:
        Path to ``/sys/fs/cgroup/user.slice/user-<uid>.slice/
        user@<uid>.service/crsbench``.
    """
    uid = os.getuid()
    return Path(
        f"{CGROUP_FS_ROOT}/user.slice/user-{uid}.slice/user@{uid}.service/crsbench"
    )


def ensure_controllers_enabled(cgroup_path: Path, controllers: list[str]) -> None:
    """Enable controllers in cgroup.subtree_control if not already enabled.

    Args:
        cgroup_path: Path to cgroup directory.
        controllers: Controller names to enable (e.g. ["cpuset", "memory"]).

    Raises:
        OSError: If unable to write to subtree_control.
    """
    subtree_control = cgroup_path / "cgroup.subtree_control"

    try:
        current = set(subtree_control.read_text().strip().split())
    except FileNotFoundError:
        current = set()

    to_add = set(controllers) - current
    if not to_add:
        return

    add_str = " ".join(f"+{c}" for c in sorted(to_add))
    subtree_control.write_text(add_str)
    logger.info(f"Enabled controllers {sorted(to_add)} in {subtree_control}")


def setup_cgroup_hierarchy(base_path: Path) -> None:
    """Set up cgroup hierarchy with required controllers.

    Creates the crsbench directory and enables cpuset+memory controllers at
    the parent (user@<uid>.service) and crsbench levels.

    Args:
        base_path: Path to the crsbench cgroup directory.

    Raises:
        OSError: If setup fails.
    """
    required_controllers = ["cpuset", "memory"]

    parent_path = base_path.parent
    ensure_controllers_enabled(parent_path, required_controllers)

    base_path.mkdir(parents=True, exist_ok=True)

    ensure_controllers_enabled(base_path, required_controllers)


def create_cgroup(
    base_path: Path,
    name: str,
    cpuset: str,
    memory_bytes: int = 0,
) -> Path:
    """Create a cgroup with specified resource limits.

    Args:
        base_path: Base cgroup directory.
        name: Unique cgroup name for this trial.
        cpuset: CPU set specification (e.g. "0-15").
        memory_bytes: Memory limit in bytes. 0 means no limit.

    Returns:
        Path to the created cgroup directory.

    Raises:
        OSError: If cgroup creation or configuration fails.
    """
    cgroup_path = base_path / name
    cgroup_path.mkdir(parents=True, exist_ok=True)

    cpuset_file = cgroup_path / "cpuset.cpus"
    cpuset_file.write_text(cpuset)

    memory_file = cgroup_path / "memory.max"
    if memory_bytes > 0:
        memory_file.write_text(str(memory_bytes))
    else:
        memory_file.write_text("max")

    logger.info(
        f"Created cgroup {cgroup_path}: cpuset={cpuset}, "
        f"memory={'unlimited' if memory_bytes == 0 else memory_bytes}"
    )
    return cgroup_path


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


def _kill_cgroup_processes(cgroup_path: Path) -> bool:
    """Kill all processes inside a cgroup (and its children) before removal.

    Tries the atomic ``cgroup.kill`` interface first (Linux 5.14+),
    which recursively kills all processes in the entire subtree.
    Falls back to reading ``cgroup.procs`` and sending SIGKILL
    to each PID individually.

    Args:
        cgroup_path: Path to the cgroup directory.

    Returns:
        True if any processes were killed (or cgroup.kill succeeded),
        False if the cgroup was empty or does not exist.
    """
    if not cgroup_path.exists():
        return False

    # Primary: atomic cgroup.kill (Linux 5.14+) — kills entire subtree
    kill_file = cgroup_path / "cgroup.kill"
    try:
        kill_file.write_text("1")
        logger.debug(f"Sent cgroup.kill to {cgroup_path}")
        time.sleep(0.5)
        return True
    except OSError:
        # ENOENT on older kernels, EPERM on permission issues — fall through
        pass

    # Fallback: read cgroup.procs and kill each PID
    procs_file = cgroup_path / "cgroup.procs"
    try:
        content = procs_file.read_text().strip()
    except OSError:
        return False

    if not content:
        return False

    killed_any = False
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            pid = int(line)
            os.kill(pid, signal.SIGKILL)
            logger.debug(f"Killed PID {pid} in cgroup {cgroup_path}")
            killed_any = True
        except ProcessLookupError:
            # Process already exited between read and kill
            killed_any = True
        except (ValueError, OSError):
            pass

    if killed_any:
        time.sleep(0.5)
    return killed_any


def _remove_cgroup_children(cgroup_path: Path) -> None:
    """Recursively remove empty child cgroup directories (bottom-up).

    Docker creates nested cgroup hierarchies (e.g.,
    ``worker-1/docker/<container-id>/``). The kernel refuses to remove
    a cgroup that still has children (returns EBUSY), so we must walk
    the tree depth-first and remove leaves before parents.

    Only removes empty cgroups (no processes). If a child cgroup still
    has live processes, its ``rmdir`` will fail with EBUSY and it is
    silently skipped. This makes the function safe to call even when
    other supervisors have active workers.

    Args:
        cgroup_path: Root cgroup directory whose children should be removed.
    """
    if not cgroup_path.exists():
        return

    # Collect all child cgroup directories (only dirs, skip cgroup control files)
    children = [p for p in cgroup_path.iterdir() if p.is_dir()]
    for child in children:
        # Recurse first (depth-first, remove leaves before parents)
        _remove_cgroup_children(child)
        try:
            child.rmdir()
            logger.debug(f"Removed child cgroup {child}")
        except OSError as e:
            logger.debug(f"Could not remove child cgroup {child}: {e}")


def cleanup_cgroup(
    cgroup_path: Path,
    *,
    max_retries: int = 3,
    retry_delay: float = 2.0,
    force: bool = False,
) -> bool:
    """Remove a cgroup directory, retrying on EBUSY.

    Args:
        cgroup_path: Path to the cgroup to remove.
        max_retries: Maximum number of retry attempts for EBUSY errors.
        retry_delay: Seconds to wait between retries.
        force: If True, kill all processes before removal. Only use when
            the cgroup is owned by the caller (e.g., our worker finished
            or Ctrl+C shutdown). When False (default), only removes empty
            child directories — safe for stale cleanup when other
            supervisors may have active workers.

    Returns:
        True if the cgroup was removed (or did not exist), False otherwise.
    """
    if not cgroup_path.exists():
        return True

    for attempt in range(1, max_retries + 1):
        try:
            cgroup_path.rmdir()
            return True
        except OSError as e:
            if e.errno == errno.EBUSY:
                if force:
                    _kill_cgroup_processes(cgroup_path)
                # Always safe: only removes empty child dirs
                _remove_cgroup_children(cgroup_path)
                # Retry rmdir immediately after cleanup
                try:
                    cgroup_path.rmdir()
                    return True
                except OSError:
                    pass
                if attempt < max_retries:
                    time.sleep(retry_delay)
                    continue
                if force:
                    logger.warning(
                        f"Could not remove cgroup {cgroup_path} after "
                        f"{max_retries} retries, may need manual cleanup"
                    )
                return False
            # Non-EBUSY error: no point retrying
            logger.error(f"Failed to remove cgroup {cgroup_path}: {e}")
            return False

    return False  # pragma: no cover


def cleanup_stale_cgroups(base_path: Path) -> int:
    """Remove stale cgroup directories left over from previous crashes.

    Iterates all child directories under *base_path* and attempts to remove
    each one. Uses fewer retries than normal cleanup since stale cgroups are
    less likely to be transiently busy.

    Args:
        base_path: The crsbench base cgroup directory.

    Returns:
        Number of successfully removed directories.
    """
    if not base_path.exists():
        return 0

    removed = 0
    for child in base_path.iterdir():
        if child.is_dir():
            if cleanup_cgroup(child, max_retries=1, retry_delay=1.0):
                removed += 1

    if removed > 0:
        logger.info(f"Cleaned up {removed} stale cgroup(s) from {base_path}")
    return removed


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def cgroup_path_for_docker(cgroup_path: Path) -> str:
    """Convert a cgroup filesystem path to Docker ``--cgroup-parent`` format.

    Strips the ``/sys/fs/cgroup`` prefix so the path is suitable for
    Docker's cgroupfs driver.

    Args:
        cgroup_path: Full filesystem path to the cgroup.

    Returns:
        Relative cgroup path for Docker.
    """
    path_str = str(cgroup_path)
    if path_str.startswith(CGROUP_FS_ROOT):
        return path_str[len(CGROUP_FS_ROOT) :]
    return path_str


def format_setup_instructions(base_path: Path) -> str:
    """Format instructions for setting up cgroup delegation.

    Args:
        base_path: The base cgroup path that needs setup.

    Returns:
        Multi-line instruction string.
    """
    uid = os.getuid()
    gid = os.getgid()
    parent_path = base_path.parent

    return (
        "To enable cgroup-parent mode, set up delegation:\n\n"
        "1. Create systemd delegate configuration:\n"
        "   sudo mkdir -p /etc/systemd/system/user@.service.d\n"
        "   sudo tee /etc/systemd/system/user@.service.d/delegate.conf "
        "<<EOF\n"
        "   [Service]\n"
        "   Delegate=cpu cpuset io memory pids\n"
        "   EOF\n"
        "   sudo systemctl daemon-reload\n\n"
        f"2. Create the cgroup directory:\n"
        f"   sudo mkdir -p {base_path}\n"
        f"   sudo chown {uid}:{gid} {base_path}\n\n"
        f"3. Enable controllers:\n"
        f'   echo "+cpuset +memory" | sudo tee '
        f"{parent_path}/cgroup.subtree_control\n"
    )


def format_docker_cgroup_driver_instructions() -> str:
    """Format instructions for configuring Docker to use cgroupfs driver.

    Returns:
        Multi-line instruction string.
    """
    return (
        "Docker must use the cgroupfs cgroup driver for cgroup-parent mode.\n\n"
        "To configure Docker to use cgroupfs driver:\n\n"
        "1. Edit or create /etc/docker/daemon.json:\n"
        "   {\n"
        '     "exec-opts": ["native.cgroupdriver=cgroupfs"]\n'
        "   }\n\n"
        "2. Restart Docker:\n"
        "   sudo systemctl restart docker\n\n"
        "3. Verify the change:\n"
        '   docker info | grep "Cgroup Driver"\n'
        '   (should show "cgroupfs")\n'
    )
