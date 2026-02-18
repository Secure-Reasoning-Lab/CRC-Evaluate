"""Resource context manager for cgroup lifecycle around trial execution.

Wraps adapter execution with cgroup v2 creation/cleanup, setting the
environment variables that Docker containers inherit for resource isolation.
Degrades gracefully when cgroup v2 is unavailable.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Optional

from crsbench.utils.logger import get_logger

if TYPE_CHECKING:
    from pathlib import Path

logger = get_logger(__name__)


class ResourceContext:
    """Context manager wrapping trial execution with cgroup lifecycle.

    Creates a cgroup on entry, sets ``OSS_FUZZ_CGROUP_PARENT`` and
    ``OSS_FUZZ_CPUSET_CPUS`` environment variables, and cleans up both
    on exit. Degrades gracefully when cgroup v2 is unavailable.

    Args:
        trial_name: Unique name for the cgroup (e.g., trial-id).
        cpuset: Allocated CPU set (e.g., ``"0-3,8-11"``).
        memory_bytes: Memory limit in bytes. 0 means unlimited.
        use_cgroups: Whether to create a cgroup. When False, the
            context manager is a no-op (beyond env-var cleanup).
    """

    def __init__(
        self,
        trial_name: str,
        cpuset: Optional[str] = None,
        memory_bytes: int = 0,
        *,
        use_cgroups: bool = False,
    ) -> None:
        self._trial_name = trial_name
        self._cpuset = cpuset
        self._memory_bytes = memory_bytes
        self._use_cgroups = use_cgroups
        self._cgroup_path: Optional[Path] = None
        self._env_vars: dict[str, str] = {}

    @property
    def cgroup_path(self) -> Optional[Path]:
        """The created cgroup path, or None if cgroups were not used."""
        return self._cgroup_path

    @property
    def env_vars(self) -> dict[str, str]:
        """Environment variables set by this context (for subprocess.Popen)."""
        return dict(self._env_vars)

    def __enter__(self) -> ResourceContext:
        if not self._use_cgroups:
            return self

        if self._cpuset is None:
            logger.warning(
                "Cgroup creation requires cpuset; skipping cgroup enforcement"
            )
            return self

        try:
            from crsbench.utils.cgroup import (
                CgroupError,
                cgroup_path_for_docker,
                create_cgroup,
                run_preflight_checks,
                setup_cgroup_hierarchy,
            )

            cgroup_base = run_preflight_checks()
            setup_cgroup_hierarchy(cgroup_base)
            cgroup_path = create_cgroup(
                cgroup_base,
                self._trial_name,
                cpuset=self._cpuset,
                memory_bytes=self._memory_bytes,
            )
            self._cgroup_path = cgroup_path

            docker_parent = cgroup_path_for_docker(cgroup_path)
            os.environ["OSS_FUZZ_CGROUP_PARENT"] = docker_parent
            os.environ["OSS_FUZZ_CPUSET_CPUS"] = self._cpuset
            self._env_vars = {
                "OSS_FUZZ_CGROUP_PARENT": docker_parent,
                "OSS_FUZZ_CPUSET_CPUS": self._cpuset,
            }

            logger.info(
                "ResourceContext: cgroup created for %s (cpuset=%s, mem=%s)",
                self._trial_name,
                self._cpuset,
                "unlimited" if self._memory_bytes == 0 else self._memory_bytes,
            )
        except CgroupError as exc:
            logger.warning(
                "Cgroup v2 unavailable, continuing without enforcement: %s", exc
            )
            self._cgroup_path = None

        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:  # noqa: ANN001
        # Always clean up env vars, even if cgroup creation was skipped
        os.environ.pop("OSS_FUZZ_CGROUP_PARENT", None)
        os.environ.pop("OSS_FUZZ_CPUSET_CPUS", None)
        self._env_vars.clear()

        if self._cgroup_path is not None:
            from crsbench.utils.cgroup import cleanup_cgroup

            cleanup_cgroup(self._cgroup_path, force=True)
            logger.info("ResourceContext: cleaned up cgroup %s", self._cgroup_path)

        return False
