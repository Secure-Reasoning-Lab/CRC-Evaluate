"""Tests for cgroup v2 utility module."""

import errno
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from crsbench.utils.cgroup import (
    CGROUP_FS_ROOT,
    CgroupError,
    cgroup_path_for_docker,
    check_cgroup_delegation,
    check_cgroup_v2_available,
    check_docker_cgroup_driver,
    cleanup_cgroup,
    cleanup_stale_cgroups,
    create_cgroup,
    ensure_controllers_enabled,
    format_docker_cgroup_driver_instructions,
    format_setup_instructions,
    get_user_cgroup_base,
    run_preflight_checks,
    setup_cgroup_hierarchy,
)

# ---------------------------------------------------------------------------
# check_cgroup_v2_available
# ---------------------------------------------------------------------------


@patch("crsbench.utils.cgroup.Path")
def test_cgroup_v2_available_when_present(mock_path_cls):
    """Cgroup v2 detected when mount and controllers file both exist."""
    mock_mount = MagicMock()
    mock_mount.exists.return_value = True
    mock_controllers = MagicMock()
    mock_controllers.exists.return_value = True

    # Path(CGROUP_FS_ROOT) for the mount check, then / for controllers
    mock_mount.__truediv__ = MagicMock(return_value=mock_controllers)
    mock_path_cls.return_value = mock_mount

    assert check_cgroup_v2_available() is True


@patch("crsbench.utils.cgroup.Path")
def test_cgroup_v2_not_available_no_mount(mock_path_cls):
    """Cgroup v2 not available when /sys/fs/cgroup does not exist."""
    mock_mount = MagicMock()
    mock_mount.exists.return_value = False
    mock_path_cls.return_value = mock_mount

    assert check_cgroup_v2_available() is False


@patch("crsbench.utils.cgroup.Path")
def test_cgroup_v2_not_available_no_controllers(mock_path_cls):
    """Cgroup v2 not available when controllers file is missing."""
    mock_mount = MagicMock()
    mock_mount.exists.return_value = True
    mock_controllers = MagicMock()
    mock_controllers.exists.return_value = False
    mock_mount.__truediv__ = MagicMock(return_value=mock_controllers)
    mock_path_cls.return_value = mock_mount

    assert check_cgroup_v2_available() is False


# ---------------------------------------------------------------------------
# check_docker_cgroup_driver
# ---------------------------------------------------------------------------


@patch("crsbench.utils.cgroup.subprocess.run")
def test_docker_cgroupfs_driver(mock_run):
    """Returns (True, 'cgroupfs') when Docker uses cgroupfs driver."""
    mock_run.return_value = MagicMock(stdout="cgroupfs\n")
    is_cgroupfs, driver = check_docker_cgroup_driver()
    assert is_cgroupfs is True
    assert driver == "cgroupfs"


@patch("crsbench.utils.cgroup.subprocess.run")
def test_docker_systemd_driver(mock_run):
    """Returns (False, 'systemd') when Docker uses systemd driver."""
    mock_run.return_value = MagicMock(stdout="systemd\n")
    is_cgroupfs, driver = check_docker_cgroup_driver()
    assert is_cgroupfs is False
    assert driver == "systemd"


@patch("crsbench.utils.cgroup.subprocess.run")
def test_docker_command_fails(mock_run):
    """Returns (False, 'unknown') when docker info command fails."""
    mock_run.side_effect = subprocess.CalledProcessError(1, "docker")
    is_cgroupfs, driver = check_docker_cgroup_driver()
    assert is_cgroupfs is False
    assert driver == "unknown"


@patch("crsbench.utils.cgroup.subprocess.run")
def test_docker_timeout(mock_run):
    """Returns (False, 'unknown') when docker info times out."""
    mock_run.side_effect = subprocess.TimeoutExpired("docker", 10)
    is_cgroupfs, driver = check_docker_cgroup_driver()
    assert is_cgroupfs is False
    assert driver == "unknown"


# ---------------------------------------------------------------------------
# check_cgroup_delegation
# ---------------------------------------------------------------------------


def test_delegation_valid():
    """Returns (True, []) when required controllers are delegated."""
    base = MagicMock(spec=Path)
    base.exists.return_value = True
    parent = MagicMock(spec=Path)
    base.parent = parent

    subtree_file = MagicMock(spec=Path)
    subtree_file.exists.return_value = True
    subtree_file.read_text.return_value = "cpuset memory io"
    parent.__truediv__ = MagicMock(return_value=subtree_file)

    is_valid, missing = check_cgroup_delegation(base)
    assert is_valid is True
    assert missing == []


def test_delegation_missing_controllers():
    """Reports missing controllers when only some are delegated."""
    base = MagicMock(spec=Path)
    base.exists.return_value = True
    parent = MagicMock(spec=Path)
    base.parent = parent

    subtree_file = MagicMock(spec=Path)
    subtree_file.exists.return_value = True
    subtree_file.read_text.return_value = "memory"
    parent.__truediv__ = MagicMock(return_value=subtree_file)

    is_valid, missing = check_cgroup_delegation(base)
    assert is_valid is False
    assert missing == ["cpuset"]


def test_delegation_base_not_exists():
    """Reports all controllers missing when base path does not exist."""
    base = MagicMock(spec=Path)
    base.exists.return_value = False

    is_valid, missing = check_cgroup_delegation(base)
    assert is_valid is False
    assert "cpuset" in missing
    assert "memory" in missing


# ---------------------------------------------------------------------------
# run_preflight_checks
# ---------------------------------------------------------------------------


@patch("crsbench.utils.cgroup.check_cgroup_delegation")
@patch("crsbench.utils.cgroup.get_user_cgroup_base")
@patch("crsbench.utils.cgroup.check_docker_cgroup_driver")
@patch("crsbench.utils.cgroup.check_cgroup_v2_available")
def test_preflight_all_pass(mock_v2, mock_docker, mock_base, mock_deleg):
    """Returns base path when all preflight checks pass."""
    mock_v2.return_value = True
    mock_docker.return_value = (True, "cgroupfs")
    expected = Path("/sys/fs/cgroup/user.slice/crsbench")
    mock_base.return_value = expected
    mock_deleg.return_value = (True, [])

    result = run_preflight_checks()
    assert result == expected


@patch("crsbench.utils.cgroup.check_cgroup_v2_available")
def test_preflight_no_cgroup_v2(mock_v2):
    """Raises CgroupError when cgroup v2 is not available."""
    mock_v2.return_value = False

    with pytest.raises(CgroupError, match="cgroup v2"):
        run_preflight_checks()


@patch("crsbench.utils.cgroup.check_docker_cgroup_driver")
@patch("crsbench.utils.cgroup.check_cgroup_v2_available")
def test_preflight_wrong_docker_driver(mock_v2, mock_docker):
    """Raises CgroupError when Docker driver is not cgroupfs."""
    mock_v2.return_value = True
    mock_docker.return_value = (False, "systemd")

    with pytest.raises(CgroupError, match="daemon.json"):
        run_preflight_checks()


@patch("crsbench.utils.cgroup.check_cgroup_delegation")
@patch("crsbench.utils.cgroup.get_user_cgroup_base")
@patch("crsbench.utils.cgroup.check_docker_cgroup_driver")
@patch("crsbench.utils.cgroup.check_cgroup_v2_available")
def test_preflight_no_delegation(mock_v2, mock_docker, mock_base, mock_deleg):
    """Raises CgroupError when cgroup delegation is not configured."""
    mock_v2.return_value = True
    mock_docker.return_value = (True, "cgroupfs")
    mock_base.return_value = Path("/sys/fs/cgroup/user.slice/crsbench")
    mock_deleg.return_value = (False, ["cpuset"])

    with pytest.raises(CgroupError, match="(?i)delegate"):
        run_preflight_checks()


# ---------------------------------------------------------------------------
# get_user_cgroup_base
# ---------------------------------------------------------------------------


def test_user_cgroup_base():
    """Constructs path with current user's uid."""
    with patch("crsbench.utils.cgroup.os.getuid", return_value=1000):
        result = get_user_cgroup_base()
    expected = Path(
        "/sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/crsbench"
    )
    assert result == expected


# ---------------------------------------------------------------------------
# ensure_controllers_enabled
# ---------------------------------------------------------------------------


def test_enable_missing_controllers():
    """Writes only the missing controllers to subtree_control."""
    cgroup = MagicMock(spec=Path)
    subtree = MagicMock(spec=Path)
    subtree.read_text.return_value = "memory"
    cgroup.__truediv__ = MagicMock(return_value=subtree)

    ensure_controllers_enabled(cgroup, ["cpuset", "memory"])

    subtree.write_text.assert_called_once_with("+cpuset")


def test_all_controllers_already_enabled():
    """Skips writing when all controllers are already enabled."""
    cgroup = MagicMock(spec=Path)
    subtree = MagicMock(spec=Path)
    subtree.read_text.return_value = "cpuset memory"
    cgroup.__truediv__ = MagicMock(return_value=subtree)

    ensure_controllers_enabled(cgroup, ["cpuset", "memory"])

    subtree.write_text.assert_not_called()


def test_enable_from_empty():
    """Enables all controllers when subtree_control does not exist."""
    cgroup = MagicMock(spec=Path)
    subtree = MagicMock(spec=Path)
    subtree.read_text.side_effect = FileNotFoundError
    cgroup.__truediv__ = MagicMock(return_value=subtree)

    ensure_controllers_enabled(cgroup, ["cpuset", "memory"])

    subtree.write_text.assert_called_once_with("+cpuset +memory")


# ---------------------------------------------------------------------------
# setup_cgroup_hierarchy
# ---------------------------------------------------------------------------


@patch("crsbench.utils.cgroup.ensure_controllers_enabled")
def test_setup_hierarchy(mock_enable):
    """Sets up parent controllers, creates dir, enables at base level."""
    base = MagicMock(spec=Path)
    parent = MagicMock(spec=Path)
    base.parent = parent

    setup_cgroup_hierarchy(base)

    assert mock_enable.call_count == 2
    mock_enable.assert_any_call(parent, ["cpuset", "memory"])
    mock_enable.assert_any_call(base, ["cpuset", "memory"])
    base.mkdir.assert_called_once_with(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# create_cgroup
# ---------------------------------------------------------------------------


def test_create_cgroup_with_memory():
    """Writes cpuset.cpus and memory.max with byte value."""
    base = MagicMock(spec=Path)
    cgroup_dir = MagicMock(spec=Path)
    cpuset_file = MagicMock(spec=Path)
    memory_file = MagicMock(spec=Path)

    base.__truediv__ = MagicMock(return_value=cgroup_dir)
    cgroup_dir.__truediv__ = MagicMock(
        side_effect=lambda name: {
            "cpuset.cpus": cpuset_file,
            "memory.max": memory_file,
        }[name]
    )

    create_cgroup(base, "trial-1", "0-15", memory_bytes=34359738368)

    cgroup_dir.mkdir.assert_called_once_with(parents=True, exist_ok=True)
    cpuset_file.write_text.assert_called_once_with("0-15")
    memory_file.write_text.assert_called_once_with("34359738368")


def test_create_cgroup_no_memory_limit():
    """Writes 'max' to memory.max when memory_bytes is 0."""
    base = MagicMock(spec=Path)
    cgroup_dir = MagicMock(spec=Path)
    cpuset_file = MagicMock(spec=Path)
    memory_file = MagicMock(spec=Path)

    base.__truediv__ = MagicMock(return_value=cgroup_dir)
    cgroup_dir.__truediv__ = MagicMock(
        side_effect=lambda name: {
            "cpuset.cpus": cpuset_file,
            "memory.max": memory_file,
        }[name]
    )

    create_cgroup(base, "trial-2", "0-7")

    memory_file.write_text.assert_called_once_with("max")


def test_create_cgroup_returns_path():
    """Returns the created cgroup path."""
    base = MagicMock(spec=Path)
    cgroup_dir = MagicMock(spec=Path)
    base.__truediv__ = MagicMock(return_value=cgroup_dir)
    # Make sub-file lookups return mocks
    cgroup_dir.__truediv__ = MagicMock(return_value=MagicMock(spec=Path))

    result = create_cgroup(base, "trial-3", "0-3")
    assert result is cgroup_dir


# ---------------------------------------------------------------------------
# cleanup_cgroup
# ---------------------------------------------------------------------------


def test_cleanup_nonexistent():
    """Returns True immediately when path does not exist."""
    mock_path = MagicMock(spec=Path)
    mock_path.exists.return_value = False

    assert cleanup_cgroup(mock_path) is True
    mock_path.rmdir.assert_not_called()


def test_cleanup_success():
    """Returns True when rmdir succeeds on first attempt."""
    mock_path = MagicMock(spec=Path)
    mock_path.exists.return_value = True

    assert cleanup_cgroup(mock_path) is True
    mock_path.rmdir.assert_called_once()


@patch("crsbench.utils.cgroup.time.sleep")
def test_cleanup_ebusy_then_success(mock_sleep):
    """Retries on EBUSY and succeeds on second attempt."""
    mock_path = MagicMock(spec=Path)
    mock_path.exists.return_value = True

    ebusy = OSError(errno.EBUSY, "Device or resource busy")
    mock_path.rmdir.side_effect = [ebusy, None]

    assert cleanup_cgroup(mock_path) is True
    mock_sleep.assert_called_once_with(2.0)


@patch("crsbench.utils.cgroup.time.sleep")
def test_cleanup_ebusy_all_retries(mock_sleep):
    """Returns False after exhausting all retries on EBUSY."""
    mock_path = MagicMock(spec=Path)
    mock_path.exists.return_value = True

    ebusy = OSError(errno.EBUSY, "Device or resource busy")
    mock_path.rmdir.side_effect = [ebusy, ebusy, ebusy]

    assert cleanup_cgroup(mock_path) is False
    # sleep is called between retries, not after the last one
    assert mock_sleep.call_count == 2


@patch("crsbench.utils.cgroup.time.sleep")
def test_cleanup_permission_error(mock_sleep):
    """Returns False immediately on non-EBUSY OSError without retrying."""
    mock_path = MagicMock(spec=Path)
    mock_path.exists.return_value = True

    eperm = OSError(errno.EPERM, "Operation not permitted")
    mock_path.rmdir.side_effect = eperm

    assert cleanup_cgroup(mock_path) is False
    mock_sleep.assert_not_called()


# ---------------------------------------------------------------------------
# cleanup_stale_cgroups
# ---------------------------------------------------------------------------


def test_cleanup_stale_empty():
    """Returns 0 when base_path does not exist."""
    base = MagicMock(spec=Path)
    base.exists.return_value = False

    assert cleanup_stale_cgroups(base) == 0


@patch("crsbench.utils.cgroup.cleanup_cgroup")
def test_cleanup_stale_removes_children(mock_cleanup):
    """Removes all child directories and returns count."""
    base = MagicMock(spec=Path)
    base.exists.return_value = True

    children = [MagicMock(spec=Path) for _ in range(3)]
    for c in children:
        c.is_dir.return_value = True
    base.iterdir.return_value = children
    mock_cleanup.return_value = True

    assert cleanup_stale_cgroups(base) == 3
    assert mock_cleanup.call_count == 3


@patch("crsbench.utils.cgroup.cleanup_cgroup")
def test_cleanup_stale_partial_failure(mock_cleanup):
    """Counts only successfully removed directories."""
    base = MagicMock(spec=Path)
    base.exists.return_value = True

    children = [MagicMock(spec=Path) for _ in range(3)]
    for c in children:
        c.is_dir.return_value = True
    base.iterdir.return_value = children
    mock_cleanup.side_effect = [True, True, False]

    assert cleanup_stale_cgroups(base) == 2


# ---------------------------------------------------------------------------
# cgroup_path_for_docker
# ---------------------------------------------------------------------------


def test_strip_cgroup_prefix():
    """Strips /sys/fs/cgroup prefix for Docker."""
    path = Path("/sys/fs/cgroup/user.slice/crsbench/trial")
    assert cgroup_path_for_docker(path) == "/user.slice/crsbench/trial"


def test_no_prefix_passthrough():
    """Returns path as-is when prefix is not present."""
    path = Path("/other/path")
    assert cgroup_path_for_docker(path) == "/other/path"


# ---------------------------------------------------------------------------
# format functions
# ---------------------------------------------------------------------------


def test_format_setup_instructions():
    """Setup instructions contain delegate, mkdir, chown, subtree_control."""
    with (
        patch("crsbench.utils.cgroup.os.getuid", return_value=1000),
        patch("crsbench.utils.cgroup.os.getgid", return_value=1000),
    ):
        base = Path("/sys/fs/cgroup/user.slice/crsbench")
        output = format_setup_instructions(base)

    assert "delegate" in output.lower()
    assert "mkdir" in output
    assert "chown" in output
    assert "subtree_control" in output


def test_format_docker_instructions():
    """Docker instructions reference daemon.json, cgroupfs, and restart."""
    output = format_docker_cgroup_driver_instructions()

    assert "daemon.json" in output
    assert "cgroupfs" in output
    assert "systemctl restart docker" in output


# ---------------------------------------------------------------------------
# CGROUP_FS_ROOT constant
# ---------------------------------------------------------------------------


def test_cgroup_fs_root_value():
    """CGROUP_FS_ROOT is the expected path."""
    assert CGROUP_FS_ROOT == "/sys/fs/cgroup"
