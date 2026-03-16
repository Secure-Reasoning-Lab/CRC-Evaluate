"""OSS-Fuzz infrastructure utilities for the builder module.

This module provides OSSFuzzInfrastructure, which wraps OSS-Fuzz's helper.py
for building fuzzers and reproducing crashes.
"""

import fcntl
import hashlib
import json
import os
import posixpath
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from crsbench.builder.replay_policy import (
    REPLAY_MODE_ENFORCE,
    replay_install_script,
    resolve_replay_policy,
)
from crsbench.builder.types import (
    BUILD_METADATA_FILE,
    BuildConfig,
    BuildMetadata,
    FuzzerBuildResult,
    ReproduceOutput,
)
from crsbench.utils.docker import docker_rmtree, fix_docker_ownership
from crsbench.utils.image_names import (
    DEFAULT_LOCAL_IMAGE_PREFIX,
    DEFAULT_REGISTRY,
    helper_inc_image_name,
    helper_latest_image_name,
    local_inc_image_name,
    registry_inc_image_name,
)
from crsbench.utils.logger import get_logger
from crsbench.utils.repo_manager import clone_or_copy_cached_repo
from crsbench.utils.subprocess_utils import run_with_timeout

logger = get_logger(__name__)
# Separate logger for crash reproduction results
reproduce_logger = get_logger("reproduce")

# Exit code constants from helper.py
EXIT_CODE_TIMEOUT = 124  # Subprocess timeout in helper.py
DEFAULT_CONTAINER_LOCALE = "C.UTF-8"

_LEAK_MARKERS = (
    "LeakSanitizer:",
    "detected memory leaks",
)
_CRASH_MARKERS = (
    "AddressSanitizer:DEADLYSIGNAL",
    "AddressSanitizer: CHECK failed",
    "ERROR: AddressSanitizer:",
    "UndefinedBehaviorSanitizer",
    "MemorySanitizer",
    "ThreadSanitizer",
    "libFuzzer: deadly signal",
    "runtime error:",
    "ERROR: AddressSanitizer",
    "== Java Exception:",
    "Exception in thread",
)


def _coverage_lock_token(value: str) -> str:
    token = re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip("-")
    return token or "unknown"


def _is_leak_only_exit(output: str) -> bool:
    """Check if reproduce output is a LeakSanitizer-only exit (not a real crash).

    LeakSanitizer exits with non-zero code but memory leaks are diagnostic,
    not POV-triggered crashes. We pass -detect_leaks=0 to libfuzzer, but the
    Docker base image's ASAN_OPTIONS=detect_leaks=1 can override it.

    In practice, real crashes (SEGV/ABRT) kill the process before ASAN's
    atexit LeakSanitizer handler runs, so leak markers and real crashes
    are mutually exclusive.
    """
    has_leak_markers = any(marker in output for marker in _LEAK_MARKERS)
    return has_leak_markers and not _has_crash_signature(output)


def _has_crash_signature(output: str) -> bool:
    """Return True when reproduce output contains crash/sanitizer markers."""
    return any(marker in output for marker in _CRASH_MARKERS)


# Track initialized OSS-Fuzz paths to avoid redundant setup
_initialized_oss_fuzz_paths: set[Path] = set()

# Regex to match unified diff hunk header: @@ -start,count +start,count @@
_HUNK_HEADER_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@")


@dataclass
class PatchHunk:
    """Represents a single hunk from a unified diff patch.

    A hunk is one contiguous change within a file, starting with @@ header.
    """

    file_path: str  # The file being patched (from +++ line)
    header: str  # The @@ line
    content: list[str]  # Lines of the hunk (context, additions, deletions)

    def hash_key(self) -> str:
        """Return a hash key for deduplication.

        Uses file path and normalized content (without line numbers) to detect
        identical hunks even if they appear at different positions.
        """
        # Normalize: join content lines, strip trailing whitespace
        normalized = "\n".join(line.rstrip() for line in self.content)
        content_to_hash = f"{self.file_path}\n{normalized}"
        return hashlib.sha256(content_to_hash.encode()).hexdigest()


@dataclass
class PatchFile:
    """Represents a parsed unified diff patch file."""

    file_path: str  # The file being patched
    old_path: str  # --- line
    new_path: str  # +++ line
    hunks: list[PatchHunk]

    def to_diff(self) -> str:
        """Convert back to unified diff format."""
        lines = [
            f"diff --git a/{self.file_path} b/{self.file_path}",
            self.old_path,
            self.new_path,
        ]
        for hunk in self.hunks:
            lines.append(hunk.header)
            lines.extend(hunk.content)
        return "\n".join(lines)


def parse_patch_into_hunks(patch_content: str) -> list[PatchFile]:
    """Parse a unified diff patch into individual file patches with hunks.

    Args:
        patch_content: The full patch file content

    Returns:
        List of PatchFile objects, each containing hunks for one file
    """
    lines = patch_content.splitlines()
    patch_files: list[PatchFile] = []

    current_file: Optional[PatchFile] = None
    current_hunk: Optional[PatchHunk] = None
    old_path = ""
    new_path = ""
    file_path = ""

    i = 0
    while i < len(lines):
        line = lines[i]

        # Start of a new file diff
        if line.startswith("diff --git"):
            # Save previous hunk and file
            if current_hunk and current_file:
                current_file.hunks.append(current_hunk)
            if current_file and current_file.hunks:
                patch_files.append(current_file)

            current_hunk = None
            current_file = None
            file_path = ""
            old_path = ""
            new_path = ""
            i += 1
            continue

        # Old file path
        if line.startswith("--- "):
            old_path = line
            i += 1
            continue

        # New file path
        if line.startswith("+++ "):
            new_path = line
            # Extract file path from +++ b/path or +++ path
            if line.startswith("+++ b/"):
                file_path = line[6:]
            elif line.startswith("+++ a/"):
                file_path = line[6:]
            else:
                file_path = line[4:].strip()

            current_file = PatchFile(
                file_path=file_path,
                old_path=old_path,
                new_path=new_path,
                hunks=[],
            )
            i += 1
            continue

        # Hunk header
        if _HUNK_HEADER_RE.match(line):
            # Save previous hunk
            if current_hunk and current_file:
                current_file.hunks.append(current_hunk)

            current_hunk = PatchHunk(
                file_path=file_path,
                header=line,
                content=[],
            )
            i += 1
            continue

        # Hunk content (context, addition, deletion, or no-newline marker)
        # Empty lines represent empty context lines (unchanged blank lines
        # in source) — git diff omits the leading space for these.
        if current_hunk is not None and (
            line == "" or line.startswith((" ", "+", "-", "\\"))
        ):
            current_hunk.content.append(line)
            i += 1
            continue

        # Skip other lines (index, mode changes, etc.)
        i += 1

    # Don't forget the last hunk and file
    if current_hunk and current_file:
        current_file.hunks.append(current_hunk)
    if current_file and current_file.hunks:
        patch_files.append(current_file)

    return patch_files


def deduplicate_hunks(
    patch_files: list[PatchFile],
    applied_hashes: set[str],
) -> tuple[list[PatchFile], set[str]]:
    """Remove duplicate hunks from patch files.

    Args:
        patch_files: List of parsed patch files
        applied_hashes: Set of already-applied hunk hashes

    Returns:
        Tuple of (deduplicated patch files, updated applied hashes)
    """
    result: list[PatchFile] = []
    new_hashes = set(applied_hashes)

    for pf in patch_files:
        unique_hunks = []
        for hunk in pf.hunks:
            hunk_hash = hunk.hash_key()
            if hunk_hash not in new_hashes:
                unique_hunks.append(hunk)
                new_hashes.add(hunk_hash)

        if unique_hunks:
            result.append(
                PatchFile(
                    file_path=pf.file_path,
                    old_path=pf.old_path,
                    new_path=pf.new_path,
                    hunks=unique_hunks,
                )
            )

    return result, new_hashes


@dataclass
class _RedisLockLease:
    """Token-bound distributed lock lease."""

    key: str
    token: str


def write_patch_files_to_diff(patch_files: list[PatchFile]) -> str:
    """Convert list of PatchFile objects back to unified diff format."""
    return "\n".join(pf.to_diff() for pf in patch_files) + "\n"


def ensure_oss_fuzz_ready(oss_fuzz_path: Path) -> None:
    """Ensure OSS-Fuzz directory is ready for parallel builds.

    Creates the build directory upfront to avoid race condition in helper.py
    when multiple parallel builds try to create it simultaneously.

    This function is idempotent. Concurrent calls are safe because
    mkdir(exist_ok=True) handles races gracefully.

    Note: Path tracking is per-process. With multiprocessing, each process
    maintains its own cache, but this is harmless since mkdir is idempotent.

    Args:
        oss_fuzz_path: Path to oss-fuzz directory
    """
    path = Path(oss_fuzz_path).resolve()
    if path in _initialized_oss_fuzz_paths:
        return

    # Create build directory to prevent TOCTOU race in helper.py
    # where it checks existence then creates, causing FileExistsError
    (path / "build").mkdir(exist_ok=True)
    _initialized_oss_fuzz_paths.add(path)
    logger.debug(f"Initialized OSS-Fuzz build directory: {path / 'build'}")


class OSSFuzzInfrastructure:
    """Infrastructure for building OSS-Fuzz projects.

    Wraps OSS-Fuzz's helper.py to provide a unified interface for:
    - Building validation variants (with address sanitizer)
    - Building coverage variants (with coverage sanitizer)
    - Managing variant project directories
    - Caching and build detection

    Supports two modes:
    1. Shared mode (default): Uses oss_fuzz_path directly for everything
    2. Isolated mode: Builds output to work_dir, with symlinks from oss_fuzz_path
       so helper.py can find them

    Attributes:
        oss_fuzz_path: Path to oss-fuzz directory
        projects_base: Path to oss-fuzz/projects directory
        work_dir: Working directory for isolated builds (None = shared mode)
    """

    def __init__(
        self,
        oss_fuzz_path: Path,
        work_dir: Optional[Path] = None,
        *,
        inc_image_policy: Optional[str] = None,
        inc_image_registry: Optional[str] = None,
        inc_image_max_pull_bytes: Optional[int] = None,
        inc_image_pull_timeout: Optional[int] = None,
        snapshot_commit_timeout: Optional[int] = None,
        inc_image_retry_interval_sec: Optional[int] = None,
        local_image_prefix: Optional[str] = None,
    ):
        """Initialize the OSS-Fuzz infrastructure.

        Args:
            oss_fuzz_path: Path to oss-fuzz directory
            work_dir: Optional working directory for isolated builds.
                If provided, builds output to work_dir/{variant}/ with symlinks
                from oss-fuzz/build/out/{variant} for helper.py compatibility.

        Raises:
            FileNotFoundError: If helper.py is not found
        """
        self.oss_fuzz_path = Path(oss_fuzz_path).resolve()
        self.work_dir = Path(work_dir).resolve() if work_dir else None
        self.projects_base = self.oss_fuzz_path / "projects"
        self._helper_script = self.oss_fuzz_path / "infra" / "helper.py"
        self._templates_dir = Path(__file__).resolve().parent / "templates"
        self.inc_image_policy = inc_image_policy or os.getenv(
            "CRSBENCH_INC_IMAGE_POLICY", "auto"
        )
        self.inc_image_registry = inc_image_registry or os.getenv(
            "CRSBENCH_INC_IMAGE_REGISTRY", DEFAULT_REGISTRY
        )
        max_pull_env = os.getenv("CRSBENCH_INC_IMAGE_MAX_PULL_BYTES")
        self.inc_image_max_pull_bytes = (
            inc_image_max_pull_bytes
            if inc_image_max_pull_bytes is not None
            else (int(max_pull_env) if max_pull_env else None)
        )
        pull_timeout_env = os.getenv("CRSBENCH_INC_IMAGE_PULL_TIMEOUT")
        self.inc_image_pull_timeout = (
            inc_image_pull_timeout
            if inc_image_pull_timeout is not None
            else (int(pull_timeout_env) if pull_timeout_env else 300)
        )
        snapshot_commit_timeout_env = os.getenv("CRSBENCH_SNAPSHOT_COMMIT_TIMEOUT")
        self.snapshot_commit_timeout = (
            snapshot_commit_timeout
            if snapshot_commit_timeout is not None
            else (
                int(snapshot_commit_timeout_env)
                if snapshot_commit_timeout_env
                else 1800
            )
        )
        retry_interval_env = os.getenv("CRSBENCH_INC_IMAGE_RETRY_INTERVAL_SEC")
        self.inc_image_retry_interval_sec = (
            inc_image_retry_interval_sec
            if inc_image_retry_interval_sec is not None
            else (int(retry_interval_env) if retry_interval_env else 120)
        )
        self.local_image_prefix = local_image_prefix or os.getenv(
            "CRSBENCH_LOCAL_IMAGE_PREFIX", DEFAULT_LOCAL_IMAGE_PREFIX
        )

        # Cache for successful inc-build image availability (project:sanitizer -> True).
        self._inc_image_cache: dict[str, bool] = {}
        # Last failed availability check timestamp by cache key.
        # Failures are retried after inc_image_retry_interval_sec.
        self._inc_image_last_failure: dict[str, float] = {}
        # Per-project locks to allow parallel pulls for different projects
        self._inc_image_locks: dict[str, threading.Lock] = {}
        self._inc_image_locks_lock = (
            threading.Lock()
        )  # Meta-lock for creating per-project locks
        self.inc_image_dist_lock_mode = os.getenv(
            "CRSBENCH_INC_IMAGE_DIST_LOCK", "auto"
        ).strip()
        self.inc_image_dist_lock_ttl_sec = int(
            os.getenv("CRSBENCH_INC_IMAGE_DIST_LOCK_TTL_SEC", "1800")
        )
        self.inc_image_dist_lock_wait_sec = int(
            os.getenv("CRSBENCH_INC_IMAGE_DIST_LOCK_WAIT_SEC", "600")
        )
        self.inc_image_dist_lock_poll_sec = max(
            1, int(os.getenv("CRSBENCH_INC_IMAGE_DIST_LOCK_POLL_SEC", "2"))
        )
        self.inc_image_dist_lock_renew_interval_sec = max(
            1, int(os.getenv("CRSBENCH_INC_IMAGE_DIST_LOCK_RENEW_SEC", "30"))
        )
        self._inc_image_dist_lock_conn = None
        self._inc_image_dist_lock_conn_lock = threading.Lock()
        self._inc_image_dist_lock_warned = False
        # Cache image tags that have replay hooks installed, keyed by resolved
        # image id. Tags can be retargeted to a different image id, so tag-only
        # caching is unsafe.
        self._replay_hooked_images: dict[str, str] = {}

        # Ensure OSS-Fuzz is ready for parallel builds
        ensure_oss_fuzz_ready(self.oss_fuzz_path)

        if not self._helper_script.exists():
            raise FileNotFoundError(
                f"OSS-Fuzz helper.py not found: {self._helper_script}"
            )

    def _coverage_lock_file_path(self, project_name: str) -> Path:
        lock_dir = Path(
            os.environ.get("CRSBENCH_COVERAGE_LOCK_DIR", "/tmp")
        ).expanduser()
        project = _coverage_lock_token(project_name)
        return lock_dir / f"crsbench-coverage-{project}.lock"

    @contextmanager
    def _acquire_coverage_lock(self, project_name: str):
        lock_path = self._coverage_lock_file_path(project_name)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("w") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _ensure_replay_hooks_in_image(self, image_name: str, policy) -> bool:
        """Install replay hook files into an image via docker run+commit."""
        if not policy.enabled:
            return True
        current_image_id = self._get_local_image_id(image_name)
        cached_image_id = self._replay_hooked_images.get(image_name)
        if (
            current_image_id is not None
            and cached_image_id is not None
            and cached_image_id == current_image_id
        ):
            return True

        original_entrypoint: Optional[list[str]] = None
        original_cmd: Optional[list[str]] = None
        try:
            inspect_result = run_with_timeout(
                ["docker", "image", "inspect", image_name],
                timeout=30,
            )
            if inspect_result.returncode == 0 and inspect_result.stdout:
                payload = json.loads(inspect_result.stdout)
                if payload and isinstance(payload, list):
                    config = payload[0].get("Config") or {}
                    entry = config.get("Entrypoint")
                    cmd = config.get("Cmd")
                    if isinstance(entry, list):
                        original_entrypoint = [str(v) for v in entry]
                    if isinstance(cmd, list):
                        original_cmd = [str(v) for v in cmd]
        except Exception as e:
            logger.debug(
                f"Failed to inspect original image config for replay hook ({image_name}): {e}"
            )

        container_name = (
            f"crsbench-replay-hooks-{os.getpid()}-{int(time.time() * 1000)}"
        )
        hook_cmd = replay_install_script(policy)

        run_cmd = [
            "docker",
            "run",
            "--name",
            container_name,
        ]
        run_cmd.extend(self._locale_docker_run_args())
        run_cmd.extend(self._docker_resource_run_args())
        run_cmd.extend(
            [
                image_name,
                "/bin/bash",
                "-lc",
                hook_cmd,
            ]
        )
        commit_cmd = ["docker", "commit"]
        if original_entrypoint is not None:
            commit_cmd.extend(
                ["--change", f"ENTRYPOINT {json.dumps(original_entrypoint)}"]
            )
        if original_cmd is not None:
            commit_cmd.extend(["--change", f"CMD {json.dumps(original_cmd)}"])
        commit_cmd.extend([container_name, image_name])
        rm_cmd = ["docker", "rm", "-f", container_name]

        try:
            run_result = run_with_timeout(run_cmd, timeout=300)
            if run_result.returncode != 0:
                logger.error(
                    "Failed to install replay hooks into {}: {}",
                    image_name,
                    (run_result.stderr or run_result.stdout or "").strip()[:500],
                )
                return False
            commit_result = run_with_timeout(commit_cmd, timeout=120)
            if commit_result.returncode != 0:
                logger.error(
                    "Failed to commit replay-hooked image {}: {}",
                    image_name,
                    (commit_result.stderr or "").strip()[:500],
                )
                return False
            hooked_image_id = self._get_local_image_id(image_name)
            if hooked_image_id is not None:
                self._replay_hooked_images[image_name] = hooked_image_id
            return True
        finally:
            try:
                run_with_timeout(rm_cmd, timeout=30)
            except Exception:
                pass

    @staticmethod
    def _docker_resource_run_args() -> list[str]:
        """Return docker run resource args propagated from supervisor env.

        Supervisor sets these for worker processes so helper.py-driven docker runs
        are constrained. Direct docker run paths must forward them explicitly.
        """
        args: list[str] = []
        cpuset = os.environ.get("OSS_FUZZ_CPUSET_CPUS", "").strip()
        cgroup_parent = os.environ.get("OSS_FUZZ_CGROUP_PARENT", "").strip()
        docker_network = os.environ.get("OSS_FUZZ_DOCKER_NETWORK", "").strip()
        if cpuset:
            args.extend(["--cpuset-cpus", cpuset])
        if cgroup_parent:
            args.extend(["--cgroup-parent", cgroup_parent])
        if docker_network:
            args.extend(["--network", docker_network])
        return args

    @staticmethod
    def _locale_docker_run_args() -> list[str]:
        """Return locale env args for direct docker run invocations."""
        return [
            "-e",
            f"LANG={DEFAULT_CONTAINER_LOCALE}",
            "-e",
            f"LC_ALL={DEFAULT_CONTAINER_LOCALE}",
        ]

    @staticmethod
    def _locale_helper_env_args() -> list[str]:
        """Return helper.py environment args for locale consistency."""
        return [
            "-e",
            f"LANG={DEFAULT_CONTAINER_LOCALE}",
            "-e",
            f"LC_ALL={DEFAULT_CONTAINER_LOCALE}",
        ]

    @staticmethod
    def _validate_variant_name(variant_name: str) -> str:
        """Validate variant name for safe filesystem usage."""
        if not variant_name:
            raise ValueError("Variant name cannot be empty")
        if variant_name in (".", ".."):
            raise ValueError(f"Invalid variant name: {variant_name!r}")
        if "/" in variant_name or "\\" in variant_name:
            raise ValueError(
                f"Variant name cannot contain path separators: {variant_name!r}"
            )
        return variant_name

    def get_build_output_path(self, variant_name: str) -> Path:
        """Get the build output path for a variant (in oss-fuzz).

        This is the path helper.py expects to find builds.
        In isolated mode, this is a symlink target.

        Args:
            variant_name: Variant name (e.g., "benchmark-deltabase")

        Returns:
            Path to build output directory in oss-fuzz
        """
        safe_variant = self._validate_variant_name(variant_name)
        return self.oss_fuzz_path / "build" / "out" / safe_variant

    def get_isolated_build_path(self, variant_name: str) -> Path:
        """Get the actual build output path (isolated or shared).

        In isolated mode, returns path in work_dir.
        In shared mode, returns the oss-fuzz path.

        Args:
            variant_name: Variant name

        Returns:
            Path to actual build output directory
        """
        if self.work_dir:
            safe_variant = self._validate_variant_name(variant_name)
            return self.work_dir / safe_variant / "build" / "out"
        return self.get_build_output_path(variant_name)

    def get_isolated_work_path(self, variant_name: str) -> Path:
        """Get the work directory path (isolated or shared).

        Args:
            variant_name: Variant name

        Returns:
            Path to work directory
        """
        if self.work_dir:
            safe_variant = self._validate_variant_name(variant_name)
            return self.work_dir / safe_variant / "build" / "work"
        safe_variant = self._validate_variant_name(variant_name)
        return self.oss_fuzz_path / "build" / "work" / safe_variant

    def copy_build_output(
        self,
        source_variant: str,
        target_variant: str,
    ) -> bool:
        """Copy build output from source variant to target variant.

        This is needed for unit tests which run in a separate variant to avoid
        overwriting the ASAN binary from build_fuzzers. The test variant needs
        the binaries from the build variant to execute tests.

        Args:
            source_variant: Source variant name (has the binaries)
            target_variant: Target variant name (needs the binaries)

        Returns:
            True if copy succeeded, False otherwise
        """
        import shutil

        source_path = self.get_build_output_path(source_variant)
        target_path = self.get_build_output_path(target_variant)

        if not source_path.exists():
            logger.warning(f"Source build output not found: {source_path}")
            return False

        # Clean stale target — may have root-owned files from a previous
        # Docker run (e.g. run_tests) that wasn't fully ownership-fixed.
        if target_path.exists():
            docker_rmtree(target_path)

        target_path.mkdir(parents=True, exist_ok=True)

        # Copy all files from source to target
        try:
            for item in source_path.iterdir():
                if item.is_file():
                    shutil.copy2(item, target_path / item.name)
                elif item.is_dir():
                    dest = target_path / item.name
                    if dest.exists():
                        shutil.rmtree(dest)
                    shutil.copytree(item, dest)
            logger.debug(f"Copied build output: {source_variant} -> {target_variant}")
            return True
        except OSError as e:
            logger.warning(f"Failed to copy build output: {e}")
            return False

    def get_isolated_src_path(self, variant_name: str) -> Path:
        """Get the source directory path (isolated or shared).

        Args:
            variant_name: Variant name

        Returns:
            Path to source directory
        """
        safe_variant = self._validate_variant_name(variant_name)
        if self.work_dir:
            return self.work_dir / safe_variant / "src"
        return self.oss_fuzz_path / "build" / "src" / safe_variant

    def is_isolated(self) -> bool:
        """Check if running in isolated mode.

        Returns:
            True if work_dir is set (isolated mode)
        """
        return self.work_dir is not None

    def setup_build_symlink(self, variant_name: str) -> bool:
        """Create symlink from oss-fuzz/build/out/{variant} to isolated build.

        This allows helper.py to find builds in isolated directories.
        Only needed in isolated mode.

        Args:
            variant_name: Variant name

        Returns:
            True if symlink created or not needed
        """
        if not self.work_dir:
            return True  # Not needed in shared mode

        isolated_path = self.get_isolated_build_path(variant_name)
        ossfuzz_path = self.get_build_output_path(variant_name)

        if not isolated_path.exists():
            logger.warning(f"Isolated build path not found: {isolated_path}")
            return False

        # Check if correct symlink already exists
        if ossfuzz_path.is_symlink():
            if ossfuzz_path.resolve() == isolated_path.resolve():
                logger.debug(f"Symlink already exists: {ossfuzz_path}")
                return True
            ossfuzz_path.unlink()
        elif ossfuzz_path.exists():
            logger.warning(f"Removing existing dir for symlink: {ossfuzz_path}")
            shutil.rmtree(ossfuzz_path)

        ossfuzz_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            ossfuzz_path.symlink_to(isolated_path)
            logger.debug(f"Created symlink: {ossfuzz_path} -> {isolated_path}")
            return True
        except FileExistsError:
            # Race condition: another worker created it
            if (
                ossfuzz_path.is_symlink()
                and ossfuzz_path.resolve() == isolated_path.resolve()
            ):
                logger.debug(f"Symlink created by another worker: {ossfuzz_path}")
                return True
            logger.error(f"Symlink exists with wrong target: {ossfuzz_path}")
            return False
        except Exception as e:
            logger.error(f"Failed to create symlink: {e}")
            return False

    def cleanup_isolated_build(self, variant_name: str) -> None:
        """Clean up isolated build for a variant.

        Removes:
        - Symlink from oss-fuzz/build/out/{variant}
        - Isolated build directory in work_dir/{variant}/

        Args:
            variant_name: Variant name
        """
        # Remove symlink from oss-fuzz
        ossfuzz_path = self.get_build_output_path(variant_name)
        if ossfuzz_path.is_symlink():
            ossfuzz_path.unlink()
            logger.debug(f"Removed symlink: {ossfuzz_path}")

        # Remove isolated directory
        safe_variant = self._validate_variant_name(variant_name)
        if self.work_dir:
            isolated_dir = self.work_dir / safe_variant
            if isolated_dir.exists():
                try:
                    shutil.rmtree(isolated_dir)
                    logger.debug(f"Removed isolated build: {isolated_dir}")
                except Exception as e:
                    logger.warning(f"Failed to remove isolated build: {e}")

    def is_variant_built(
        self,
        variant_name: str,
        *,
        require_inc_build: Optional[bool] = None,
        require_source_image: bool = False,
    ) -> bool:
        """Check if a variant has been built successfully.

        Requires `.build-meta.json` to exist as proof that the build
        completed.  Interrupted builds leave partial files but no
        metadata, so they are correctly rejected.

        Args:
            variant_name: Variant name
            require_inc_build: If specified, also verify the cached build
                matches the requested inc-build mode. None means don't check.
                When True, also rejects builds that used fallback.
            require_source_image: If True, also require the corresponding
                source image tag to exist locally:
                - :inc when require_inc_build=True
                - :latest otherwise

        Returns:
            True if built fuzzers exist (and match required inc_build mode if specified)
        """
        build_path = self.get_build_output_path(variant_name)
        safe_variant = self._validate_variant_name(variant_name)
        project_path = self.projects_base / safe_variant

        # Both project directory and build output must exist
        if not build_path.exists() or not project_path.exists():
            return False

        # Check that build output has actual files
        if not any(build_path.iterdir()):
            return False

        # Metadata must exist — it is written only after a successful build.
        # A missing metadata file means the build was interrupted or incomplete.
        metadata = self.read_build_metadata(variant_name)
        if metadata is None:
            logger.debug(
                f"No build metadata for {variant_name}: "
                "build may be incomplete, treating as not built"
            )
            return False

        # If require_inc_build is specified, verify the cached build matches
        if require_inc_build is not None:
            if metadata.inc_build != require_inc_build:
                logger.debug(
                    f"Cache mismatch for {variant_name}: "
                    f"cached inc_build={metadata.inc_build}, "
                    f"required={require_inc_build}"
                )
                return False

            # If inc-build is required, reject builds that used fallback
            if require_inc_build and metadata.fallback_used:
                logger.debug(
                    f"Cache mismatch for {variant_name}: "
                    f"cached build used fallback, but pure inc-build required"
                )
                return False

        if require_source_image:
            image_tag = "inc" if require_inc_build else "latest"
            image_name = f"gcr.io/oss-fuzz/{variant_name}:{image_tag}"
            if not self._docker_image_exists(image_name):
                logger.debug(
                    "Cache incomplete for %s: missing source image %s",
                    variant_name,
                    image_name,
                )
                return False

        return True

    def write_build_metadata(
        self,
        variant_name: str,
        *,
        inc_build: bool = False,
        sanitizer: str = "address",
        fallback_used: bool = False,
    ) -> None:
        """Write build metadata to the build output directory.

        Uses atomic write (write to temp file, then rename) to prevent
        race conditions when multiple workers might access the same file.

        Args:
            variant_name: Variant name
            inc_build: Whether this was an incremental build
            sanitizer: Sanitizer used for the build
            fallback_used: Whether fallback to clean build was used
        """
        from datetime import datetime

        build_path = self.get_build_output_path(variant_name)
        if not build_path.exists():
            logger.warning(f"Build path does not exist: {build_path}")
            return

        metadata = BuildMetadata(
            inc_build=inc_build,
            sanitizer=sanitizer,
            timestamp=datetime.now().isoformat(),
            fallback_used=fallback_used,
        )

        metadata_path = build_path / BUILD_METADATA_FILE
        temp_path = metadata_path.with_suffix(".tmp")
        try:
            # Atomic write: write to temp file, then rename
            with temp_path.open("w") as f:
                json.dump(metadata.to_dict(), f, indent=2)
            temp_path.replace(metadata_path)  # Atomic on POSIX
            logger.debug(f"Wrote build metadata: {metadata_path}")
        except Exception as e:
            logger.warning(f"Failed to write build metadata: {e}")
            # Clean up temp file if it exists
            if temp_path.exists():
                temp_path.unlink(missing_ok=True)

    def read_build_metadata(self, variant_name: str) -> Optional[BuildMetadata]:
        """Read build metadata from the build output directory.

        Args:
            variant_name: Variant name

        Returns:
            BuildMetadata if found, None otherwise
        """
        build_path = self.get_build_output_path(variant_name)
        metadata_path = build_path / BUILD_METADATA_FILE

        if not metadata_path.exists():
            return None

        try:
            with metadata_path.open() as f:
                data = json.load(f)
            return BuildMetadata.from_dict(data)
        except Exception as e:
            logger.warning(f"Failed to read build metadata: {e}")
            return None

    def has_harness(self, variant_name: str, harness_name: str) -> bool:
        """Check if a specific harness exists in the build output.

        Args:
            variant_name: Variant name
            harness_name: Name of the harness executable

        Returns:
            True if the harness executable exists in build output
        """
        build_path = self.get_build_output_path(variant_name)
        if not build_path.exists():
            return False

        harness_path = build_path / harness_name
        return harness_path.exists() and harness_path.is_file()

    # =========================================================================
    # Cleanup methods (shared by POV, patch, coverage verification)
    # =========================================================================

    def cleanup_build_outputs(self, variant_name: str) -> None:
        """Clean up build outputs only (for force rebuild or fallback retry).

        Removes:
        - Build output directory (or symlink) at oss-fuzz/build/out/{variant}
        - Isolated build directory if work_dir is set
        - Work directory at oss-fuzz/build/work/{variant}

        Does NOT remove:
        - Project symlink (read-only, reusable)
        - Docker images (use cleanup_docker_images separately for force rebuild)

        Args:
            variant_name: Variant name
        """
        # Remove build output (symlink or directory)
        build_path = self.get_build_output_path(variant_name)
        if build_path.is_symlink():
            build_path.unlink()
            logger.debug(f"Removed build symlink: {build_path}")
        elif build_path.exists():
            docker_rmtree(build_path)

        # Remove isolated build directory if using work_dir
        if self.work_dir:
            isolated_build = self.get_isolated_build_path(variant_name)
            if isolated_build.exists():
                docker_rmtree(isolated_build)

        # Remove work directory
        work_path = self.get_isolated_work_path(variant_name)
        if work_path.exists():
            docker_rmtree(work_path)

    def cleanup_docker_images(self, variant_name: str) -> None:
        """Remove Docker images for a variant to force full rebuild.

        Removes both the normal build image and the inc-build image.
        Missing images are skipped silently. Failed removals are logged as
        warnings but do not raise exceptions (cleanup failures should not
        block the build).

        Args:
            variant_name: Variant name (e.g., "benchmark-asan-deltaref")
        """
        image_names = [
            f"gcr.io/oss-fuzz/{variant_name}:latest",
            f"gcr.io/oss-fuzz/{variant_name}:inc",
        ]

        for image_name in image_names:
            if not self._docker_image_exists(image_name):
                logger.debug(f"Image not found, skipping: {image_name}")
                continue

            try:
                result = subprocess.run(
                    ["docker", "rmi", image_name],
                    capture_output=True,
                    timeout=60,
                    stdin=subprocess.DEVNULL,
                )
                if result.returncode == 0:
                    logger.debug(f"Removed Docker image: {image_name}")
                else:
                    logger.warning(
                        f"Failed to remove Docker image {image_name}: "
                        f"exit code {result.returncode}"
                    )
            except Exception as e:
                logger.warning(f"Error removing Docker image {image_name}: {e}")

    def cleanup_project_symlink(self, variant_name: str) -> None:
        """Clean up project symlink only.

        Removes:
        - Project symlink at oss-fuzz/projects/{variant}

        Args:
            variant_name: Variant name
        """
        safe_variant = self._validate_variant_name(variant_name)
        variant_path = self.projects_base / safe_variant
        if variant_path.is_symlink():
            variant_path.unlink()
            logger.debug(f"Removed project symlink: {variant_path}")
        elif variant_path.exists():
            # Should not happen with symlink-based approach, but handle it
            try:
                shutil.rmtree(variant_path)
                logger.debug(f"Removed project dir: {variant_path}")
            except Exception as e:
                logger.warning(f"Failed to remove project dir: {e}")

    def cleanup_source(self, variant_name: str) -> None:
        """Clean up source directory only.

        Removes:
        - Source directory at work_dir/{variant}/src or build/src/{variant}

        Args:
            variant_name: Variant name
        """
        src_path = self.get_isolated_src_path(variant_name)
        if src_path.exists():
            docker_rmtree(src_path)

    def create_variant_project(
        self,
        benchmark_path: Path,
        variant_name: str,
    ) -> Optional[Path]:
        """Create a variant project directory as symlink to benchmark.

        The project directory (Dockerfile, build.sh, etc.) is read-only during
        build/run, so we can safely symlink instead of copying.

        Thread-safe: handles race conditions when multiple workers try to
        create the same symlink simultaneously.

        Args:
            benchmark_path: Path to original benchmark directory
            variant_name: Name for the variant project

        Returns:
            Path to variant project symlink, or None on failure
        """
        safe_variant = self._validate_variant_name(variant_name)
        variant_path = self.projects_base / safe_variant
        target_path = benchmark_path.resolve()

        if variant_path.is_symlink():
            # Already exists - verify it points to the right target
            if variant_path.resolve() == target_path:
                logger.debug(f"Variant project already exists: {variant_path}")
                return variant_path
            # Wrong target - remove and recreate
            variant_path.unlink()
        elif variant_path.exists():
            logger.debug(f"Variant project already exists: {variant_path}")
            return variant_path

        if not benchmark_path.exists():
            logger.error(f"Benchmark path not found: {benchmark_path}")
            return None

        try:
            variant_path.parent.mkdir(parents=True, exist_ok=True)
            variant_path.symlink_to(target_path)
            logger.debug(f"Linked variant project: {safe_variant} -> {benchmark_path}")
            return variant_path
        except FileExistsError:
            # Race condition: another worker created it first
            # Check if it's correct and return success
            if variant_path.is_symlink() and variant_path.resolve() == target_path:
                logger.debug(
                    f"Variant project created by another worker: {variant_path}"
                )
                return variant_path
            logger.error(f"Variant project exists with wrong target: {variant_path}")
            return None
        except Exception as e:
            logger.error(f"Failed to create variant project: {e}")
            return None

    def link_variant_project(
        self,
        source_project: str,
        variant_name: str,
    ) -> Optional[Path]:
        """Create a variant project as symlink to existing project.

        Use this when the variant shares the same project config (Dockerfile,
        build.sh, etc.) as an existing project. Avoids duplicate copies.

        Args:
            source_project: Name of existing project in oss-fuzz/projects/
            variant_name: Name for the variant project

        Returns:
            Path to variant project symlink, or None on failure
        """
        safe_variant = self._validate_variant_name(variant_name)
        safe_source = self._validate_variant_name(source_project)
        variant_path = self.projects_base / safe_variant
        source_path = self.projects_base / safe_source

        # Check if correct symlink already exists
        if variant_path.is_symlink():
            if variant_path.resolve() == source_path.resolve():
                logger.debug(f"Variant project already exists: {variant_path}")
                return variant_path
            # Wrong target - remove and recreate
            variant_path.unlink()
        elif variant_path.exists():
            logger.debug(f"Variant project already exists: {variant_path}")
            return variant_path

        if not source_path.exists():
            logger.error(f"Source project not found: {source_path}")
            return None

        try:
            variant_path.symlink_to(source_path)
            logger.debug(f"Linked variant project: {safe_variant} -> {safe_source}")
            return variant_path
        except FileExistsError:
            # Race condition: another worker created it
            if (
                variant_path.is_symlink()
                and variant_path.resolve() == source_path.resolve()
            ):
                logger.debug(
                    f"Variant project created by another worker: {variant_path}"
                )
                return variant_path
            logger.error(f"Variant project exists with wrong target: {variant_path}")
            return None
        except Exception as e:
            logger.error(f"Failed to link variant project: {e}")
            return None

    def clone_source(
        self,
        config: BuildConfig,
        dest_dir: Path,
    ) -> Optional[Path]:
        """Clone and checkout source repository.

        Args:
            config: Build configuration
            dest_dir: Destination directory for clone

        Returns:
            Path to cloned repository, or None on failure
        """
        repo_path = dest_dir / "repo"

        result = clone_or_copy_cached_repo(
            repo_url=config.main_repo,
            commit=config.commit,
            target_dir=str(repo_path),
            repo_name=config.repo_name,
            verbose=True,
        )

        if not result:
            logger.error(f"Failed to clone repository: {config.main_repo}")
            return None

        return repo_path

    def build_fuzzers(
        self,
        config: BuildConfig,
        src_path: Optional[Path] = None,
        *,
        use_inc_image: bool = False,
        snapshot_fallback: bool = False,
    ) -> FuzzerBuildResult:
        """Build fuzzers for a variant.

        Args:
            config: Build configuration
            src_path: Path to source repository. If None, uses bundled source in Docker.
            use_inc_image: Use pre-built inc-build image for faster builds
            snapshot_fallback: Fallback to clean build from /src.
                Use when incremental/replay build fails due to incompatibility.

        Returns:
            FuzzerBuildResult with success status and fallback tracking.
            When snapshot_fallback=True is used, fallback_used will be True to indicate
            that the build MAY have used fallback (we can't detect if it was actually
            triggered, but we track the intent for correct cache metadata).
        """
        variant_name = config.variant_name
        logger.debug(f"Building {variant_name} ({config.language})")

        # Map language to OSS-Fuzz FUZZING_LANGUAGE format
        fuzzing_language = config.language if config.language != "c" else "c++"

        # Build command
        cmd = [
            "python3",
            str(self._helper_script),
            "build_fuzzers",
            "--sanitizer",
            config.sanitizer,
            "-e",
            f"FUZZING_LANGUAGE={fuzzing_language}",
        ]
        cmd.extend(self._locale_helper_env_args())

        # Use inc-build image for faster incremental builds.
        # In inc-build mode we prepare variant :latest from inc image and must
        # avoid helper.py image rebuilds that would overwrite replay hooks.
        replay_policy = resolve_replay_policy(
            inc_build=(use_inc_image and not snapshot_fallback)
        )
        replay_shims_ready = False
        if use_inc_image:
            cmd.append("--no-build-image")
            replay_build = "0" if snapshot_fallback else "1"
            cmd.extend(["-e", f"REPLAY_BUILD={replay_build}"])
            if src_path and replay_policy.enabled:
                image_for_hooks = helper_latest_image_name(variant_name)
                if not self._ensure_replay_hooks_in_image(
                    image_for_hooks, replay_policy
                ):
                    logger.error(
                        "Replay hook install failed for {} image {}",
                        variant_name,
                        image_for_hooks,
                    )
                    return FuzzerBuildResult(
                        success=False,
                        fallback_used=False,
                        stdout="",
                        stderr="Replay hook installation failed",
                    )
                cmd.extend(
                    [
                        "-e",
                        f"CRSBENCH_REPLAY_POLICY={REPLAY_MODE_ENFORCE}",
                        "-e",
                        f"CRSBENCH_REPLAY_POLICY_SOURCE={replay_policy.source}",
                        "-e",
                        f"CRSBENCH_REPLAY_POLICY_TOOLS={replay_policy.tool_hooks_csv()}",
                    ]
                )
                replay_shims_ready = True
            elif replay_policy.enabled and not src_path:
                logger.error(
                    "Replay policy requires source path for {} but src_path is None",
                    variant_name,
                )
                return FuzzerBuildResult(
                    success=False,
                    fallback_used=False,
                    stdout="",
                    stderr="Replay policy requires src_path in inc-build mode",
                )

        cmd.append(variant_name)
        # source_path mounts local extracted source into the container via
        # helper.py's read-only mount + rsync. When None, the build uses
        # in-image source from COPY pkgs/ (default CMD ["compile"]).
        if src_path:
            cmd.append(str(src_path))

        logger.debug(f"Building {variant_name} with {config.sanitizer} sanitizer...")
        logger.debug(f"Build command: {' '.join(cmd)}")
        if replay_shims_ready:
            logger.debug(
                "Replay policy active for {}: source={} tools={}",
                variant_name,
                replay_policy.source,
                replay_policy.tool_hooks_csv(),
            )

        try:
            result = run_with_timeout(
                cmd,
                timeout=config.timeout,
                cwd=self.oss_fuzz_path,
            )

            if result.returncode == 0:
                logger.debug(f"Build succeeded for {variant_name}")
                # Log build output for debugging
                if result.stdout:
                    logger.debug(f"Build stdout: {result.stdout[:1000]}")
                fix_docker_ownership(self.get_build_output_path(variant_name))
                # Track if fallback may have been used
                # Only relevant when inc-build was attempted (use_inc_image=True)
                # and fallback was enabled (snapshot_fallback=True)
                actual_fallback = use_inc_image and snapshot_fallback
                return FuzzerBuildResult(
                    success=True,
                    fallback_used=actual_fallback,
                    stdout=result.stdout or "",
                    stderr=result.stderr or "",
                )

            logger.error(
                f"Build failed for {variant_name} (exit code {result.returncode})"
            )
            if result.stdout:
                logger.debug(f"Build stdout: {result.stdout[:2000]}...")
            if result.stderr:
                logger.debug(f"Build stderr: {result.stderr[:2000]}...")
            return FuzzerBuildResult(
                success=False,
                fallback_used=False,
                stdout=result.stdout or "",
                stderr=result.stderr or "",
            )

        except subprocess.TimeoutExpired:
            logger.error(f"Build timed out for {variant_name} ({config.timeout}s)")
            return FuzzerBuildResult(success=False, fallback_used=False)
        except Exception as e:
            logger.error(f"Build error for {variant_name}: {e}")
            return FuzzerBuildResult(success=False, fallback_used=False)

    def get_patch_superset_map(self, benchmark_path: Path) -> dict[int, int]:
        """Get mapping of CPV subset to superset relationships from meta.yaml.

        This delegates to MetaYamlAdapter for reusability.
        When a CPV has patch_superset set, it means that CPV's patch is a subset
        of another CPV's patch (the superset).

        Example: cpv_1 has patch_superset: cpv_7
        - cpv_1's patch is a subset of cpv_7's patch
        - When applying all patches, cpv_1 should be skipped (cpv_7 covers it)
        - When excluding cpv_1, cpv_7 should also be excluded (contains cpv_1's fix)

        Args:
            benchmark_path: Path to benchmark directory

        Returns:
            Dict mapping subset CPV number to superset CPV number.
            Example: {1: 7} means cpv_1 is subset of cpv_7.
        """
        from crsbench.validation.meta_adapter import MetaYamlAdapter

        adapter = MetaYamlAdapter.from_benchmark_path(benchmark_path)
        if adapter is None:
            return {}

        return adapter.get_patch_superset_map()

    def get_cpv_patches(self, benchmark_path: Path) -> dict[int, list[Path]]:
        """Get all CPV patches from a benchmark's .aixcc directory.

        CRSBench structure: .aixcc/{harness}/cpv_{N}/patches/patch_*.diff

        Args:
            benchmark_path: Path to benchmark directory

        Returns:
            Dict mapping CPV number to list of patch files
        """
        aixcc_dir = benchmark_path / ".aixcc"
        if not aixcc_dir.exists():
            logger.warning(f".aixcc directory not found: {aixcc_dir}")
            return {}

        cpv_patches: dict[int, list[Path]] = {}

        for harness_dir in aixcc_dir.iterdir():
            if not harness_dir.is_dir() or harness_dir.name in ("tests", "povs"):
                continue

            for cpv_dir in harness_dir.glob("cpv_*"):
                if not cpv_dir.is_dir():
                    continue

                try:
                    cpv_num = int(cpv_dir.name.split("_")[1])
                except (IndexError, ValueError):
                    continue

                patches_dir = cpv_dir / "patches"
                if patches_dir.exists():
                    for patch_file in sorted(patches_dir.glob("*.diff")):
                        if cpv_num not in cpv_patches:
                            cpv_patches[cpv_num] = []
                        cpv_patches[cpv_num].append(patch_file)

        return cpv_patches

    def get_all_patches(self, benchmark_path: Path) -> list[Path]:
        """Get all CPV patches as a flat list, excluding subset CPVs.

        When a CPV has a patch_superset relationship (e.g., cpv_1 ⊂ cpv_7),
        the subset CPV's patch is skipped because the superset's patch
        already includes the fix.

        Args:
            benchmark_path: Path to benchmark directory

        Returns:
            Sorted list of all patch files (excluding subsets)
        """
        superset_map = self.get_patch_superset_map(benchmark_path)
        cpv_patches = self.get_cpv_patches(benchmark_path)

        # Subset CPVs should be skipped (superset includes their fix)
        subsets = set(superset_map.keys())

        all_patches = []
        for cpv_num in sorted(cpv_patches.keys()):
            if cpv_num in subsets:
                superset_cpv = superset_map[cpv_num]
                logger.debug(
                    f"Skipping cpv_{cpv_num} patches (subset of cpv_{superset_cpv})"
                )
                continue
            all_patches.extend(cpv_patches[cpv_num])

        return all_patches

    def get_patches_except(self, benchmark_path: Path, exclude_cpv: int) -> list[Path]:
        """Get all CPV patches except for a specific CPV.

        Handles patch superset relationships:
        - If exclude_cpv is a subset (has patch_superset), its superset is also excluded
        - If exclude_cpv is a superset, only it is excluded (subsets can be applied)
        - Subset patches are always skipped unless their superset is excluded

        Also excludes patches from other CPVs that have identical content
        (same SHA-256 hash) to prevent unintended patching when multiple
        CPVs share the same vulnerability fix.

        Args:
            benchmark_path: Path to benchmark directory
            exclude_cpv: CPV number to exclude

        Returns:
            List of patch files excluding the specified CPV and related patches
        """
        superset_map = self.get_patch_superset_map(benchmark_path)
        cpv_patches = self.get_cpv_patches(benchmark_path)

        # Build set of CPVs to exclude
        cpvs_to_exclude = {exclude_cpv}

        # If exclude_cpv is a subset, also exclude its superset
        # (testing subset vulnerability requires removing both patches)
        if exclude_cpv in superset_map:
            superset_cpv = superset_map[exclude_cpv]
            cpvs_to_exclude.add(superset_cpv)
            logger.debug(
                f"Excluding cpv_{superset_cpv} (superset of excluded cpv_{exclude_cpv})"
            )

        # Determine which supersets are excluded
        supersets_excluded = set()
        for _subset_cpv, superset_cpv in superset_map.items():
            if superset_cpv in cpvs_to_exclude:
                supersets_excluded.add(superset_cpv)

        # Calculate hashes of excluded CPV patches for duplicate detection
        excluded_hashes: set[str] = set()
        for excluded_cpv in cpvs_to_exclude:
            if excluded_cpv in cpv_patches:
                for patch_file in cpv_patches[excluded_cpv]:
                    patch_hash = hashlib.sha256(patch_file.read_bytes()).hexdigest()
                    excluded_hashes.add(patch_hash)

        # Collect patches
        patches = []
        for cpv_num in sorted(cpv_patches.keys()):
            if cpv_num in cpvs_to_exclude:
                continue

            # Handle subset CPVs
            if cpv_num in superset_map:
                superset_cpv = superset_map[cpv_num]
                if superset_cpv in supersets_excluded:
                    # Superset is excluded, so apply the subset
                    logger.debug(
                        f"Including cpv_{cpv_num} patches "
                        f"(superset cpv_{superset_cpv} is excluded)"
                    )
                else:
                    # Superset will be applied, skip the subset
                    logger.debug(
                        f"Skipping cpv_{cpv_num} patches (subset of cpv_{superset_cpv})"
                    )
                    continue

            # Add patches, checking for duplicates
            for patch_file in cpv_patches[cpv_num]:
                patch_hash = hashlib.sha256(patch_file.read_bytes()).hexdigest()
                if patch_hash not in excluded_hashes:
                    patches.append(patch_file)
                else:
                    logger.debug(
                        f"Skipping duplicate patch {patch_file.name} "
                        f"(same content as excluded CPV)"
                    )

        return patches

    def apply_patch(self, repo_path: Path, patch_file: Path) -> bool:
        """Apply a single patch file to a repository.

        Args:
            repo_path: Path to repository
            patch_file: Path to patch file

        Returns:
            True if patch applied successfully
        """
        return self._apply_single_patch(repo_path, patch_file)

    def apply_patches_from_list(self, repo_path: Path, patches: list[Path]) -> bool:
        """Apply a list of patches to a repository with hunk-level deduplication.

        Uses hunk-level deduplication to handle cases where multiple CPV patches
        share identical hunks (e.g., same utility function added in multiple
        version-specific files). This prevents duplicate code from being applied
        when using fuzz mode.

        When applying multiple patches, uses fuzz mode for second and subsequent
        patches to handle context changes from earlier patches.

        Args:
            repo_path: Path to repository
            patches: List of patch files to apply

        Returns:
            True if all patches applied successfully
        """
        success = True
        applied_hunk_hashes: set[str] = set()
        applied_count = 0

        # Debug: log all patches to be applied
        patch_cpvs = []
        for p in patches:
            cpv_parts = [x for x in p.parts if x.startswith("cpv_")]
            cpv_name = cpv_parts[0] if cpv_parts else "unknown"
            patch_cpvs.append(cpv_name)
        logger.debug(f"Applying patches to {repo_path}: {patch_cpvs}")

        for patch_file in patches:
            # Parse patch into hunks
            patch_content = patch_file.read_text()
            patch_files = parse_patch_into_hunks(patch_content)

            if not patch_files:
                logger.debug(f"No hunks found in patch: {patch_file.name}")
                continue

            # Deduplicate hunks
            unique_patch_files, applied_hunk_hashes = deduplicate_hunks(
                patch_files, applied_hunk_hashes
            )

            if not unique_patch_files:
                logger.debug(f"All hunks already applied, skipping: {patch_file.name}")
                continue

            # Count unique hunks for logging
            unique_hunk_count = sum(len(pf.hunks) for pf in unique_patch_files)
            total_hunk_count = sum(len(pf.hunks) for pf in patch_files)
            if unique_hunk_count < total_hunk_count:
                logger.debug(
                    f"Patch {patch_file.name}: {unique_hunk_count}/{total_hunk_count} "
                    "unique hunks (duplicates skipped)"
                )

            # Write unique hunks to temp file
            unique_diff = write_patch_files_to_diff(unique_patch_files)

            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".diff", delete=False
            ) as tmp:
                tmp.write(unique_diff)
                tmp_path = Path(tmp.name)

            try:
                # Use fuzz for second and subsequent patches
                use_fuzz = applied_count > 0

                if not self._apply_single_patch(repo_path, tmp_path, use_fuzz=use_fuzz):
                    logger.warning(f"Failed to apply patch: {patch_file}")
                    success = False
                else:
                    logger.debug(f"Applied patch: {patch_file.name}")
                    applied_count += 1
            finally:
                tmp_path.unlink(missing_ok=True)

        return success

    def apply_patches(
        self,
        repo_path: Path,
        aixcc_dir: Path,
        exclude_cpv: Optional[int] = None,
    ) -> None:
        """Apply CPV patches from CRSBench structure.

        DEPRECATED: Use apply_patches_from_list() with get_all_patches() or
        get_patches_except() instead.

        CRSBench structure: .aixcc/{harness}/cpv_{N}/patches/patch_*.diff

        Args:
            repo_path: Path to the repository
            aixcc_dir: Path to .aixcc directory
            exclude_cpv: CPV number to exclude (skip its patches)
        """
        if not aixcc_dir.exists():
            logger.warning(f".aixcc directory not found: {aixcc_dir}")
            return

        # Find all cpv_* directories across all harnesses
        cpv_patches: dict[int, list[Path]] = {}

        for harness_dir in aixcc_dir.iterdir():
            if not harness_dir.is_dir() or harness_dir.name in ("tests", "povs"):
                continue

            for cpv_dir in harness_dir.glob("cpv_*"):
                if not cpv_dir.is_dir():
                    continue

                try:
                    cpv_num = int(cpv_dir.name.split("_")[1])
                except (IndexError, ValueError):
                    continue

                patches_dir = cpv_dir / "patches"
                if patches_dir.exists():
                    for patch_file in patches_dir.glob("*.diff"):
                        if cpv_num not in cpv_patches:
                            cpv_patches[cpv_num] = []
                        cpv_patches[cpv_num].append(patch_file)

        # Apply patches, excluding the specified CPV
        for cpv_num, patch_files in sorted(cpv_patches.items()):
            if exclude_cpv is not None and cpv_num == exclude_cpv:
                logger.debug(f"Skipping patches for cpv_{cpv_num}")
                continue

            for patch_file in patch_files:
                if not self._apply_single_patch(repo_path, patch_file):
                    logger.warning(f"Failed to apply patch: {patch_file}")
                else:
                    logger.debug(f"Applied patch: {patch_file.name} (cpv_{cpv_num})")

    def _apply_single_patch(
        self,
        repo_path: Path,
        patch_file: Path,
        *,
        use_fuzz: bool = False,
    ) -> bool:
        """Apply a single patch file.

        Uses --whitespace=nowarn instead of --3way because bundled tarballs
        have fresh git init with different blob hashes than the original
        repository where patches were created. The --3way option requires
        matching index hashes which won't exist.

        Falls back to the patch binary if git apply fails.

        Args:
            repo_path: Path to repository
            patch_file: Path to patch file
            use_fuzz: If True, use lenient context matching (git apply -C1,
                patch --fuzz=3). Useful when applying multiple patches where
                earlier patches may have changed the context lines.

        Returns:
            True if successful
        """
        try:
            patch_file_abs = patch_file.resolve()

            # Build git apply command
            git_cmd = [
                "git",
                "apply",
                "--whitespace=nowarn",
                "--verbose",
            ]
            if use_fuzz:
                # Use -C1 to require only 1 line of context (more lenient)
                git_cmd.append("-C1")
            git_cmd.append(str(patch_file_abs))

            # Try git apply first
            result = subprocess.run(
                git_cmd,
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=60,
                stdin=subprocess.DEVNULL,
            )
            if result.returncode == 0:
                return True

            # git apply failed, try patch binary as fallback
            logger.debug(
                f"git apply failed, trying patch binary: {patch_file.name}\n"
                f"  stderr: {result.stderr}"
            )

            # Build patch command
            patch_cmd = ["patch", "-p1", "-i", str(patch_file_abs)]
            if use_fuzz:
                # Use --fuzz=3 for lenient matching with patch binary
                patch_cmd.append("--fuzz=3")

            fallback_result = subprocess.run(
                patch_cmd,
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=60,
                stdin=subprocess.DEVNULL,
            )
            if fallback_result.returncode == 0:
                logger.debug(f"Patch applied via fallback: {patch_file.name}")
                return True

            # Both methods failed
            logger.warning(
                f"Patch apply failed (both git apply and patch):\n"
                f"  repo: {repo_path}\n"
                f"  patch: {patch_file_abs}\n"
                f"  git stderr: {result.stderr}\n"
                f"  patch stderr: {fallback_result.stderr}"
            )
            return False
        except Exception as e:
            logger.error(f"Patch error: {e}")
            return False

    def cleanup_variant(self, variant_name: str) -> None:
        """Remove all variant artifacts (full cleanup).

        Convenience method that removes everything:
        - Project symlink
        - Build outputs
        - Source directory (if isolated)

        For granular cleanup, use:
        - cleanup_project_symlink() - project symlink only
        - cleanup_build_outputs() - build outputs only
        - cleanup_source() - source directory only

        Args:
            variant_name: Variant name
        """
        self.cleanup_project_symlink(variant_name)
        self.cleanup_build_outputs(variant_name)
        self.cleanup_source(variant_name)

    def reproduce(
        self,
        project_name: str,
        harness: str,
        pov_data: bytes,
        timeout: int = 180,
        request_id: Optional[int] = None,
        pov_id: Optional[str] = None,
    ) -> "ReproduceOutput":
        """Reproduce a crash using OSS-Fuzz helper.py.

        Uses official OSS-Fuzz helper.py reproduce invocation.

        Exit handling:
        - 0: No crash
        - 124: Timeout (treated as no crash)
        - Other non-zero: Crash/error (subject to leak-only filtering)

        Note: sanitizer is embedded in project_name (e.g., project-asan-deltaref).
        helper.py reproduce doesn't accept --sanitizer flag.

        Args:
            project_name: Variant name (e.g., "benchmark-asan-deltaref")
            harness: Harness name to run
            pov_data: Raw POV/testcase bytes
            timeout: Timeout for reproduce operation in seconds (default: 180s
                to handle large projects like wireshark)
            request_id: Optional request ID for logging
            pov_id: Optional POV identifier for logging

        Returns:
            ReproduceOutput with crashed status and stdout/stderr
        """
        from crsbench.builder.types import ReproduceOutput

        # Write POV data to temporary file
        with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as f:
            f.write(pov_data)
            testcase_path = Path(f.name)

        req_prefix = f"[Request #{request_id}] " if request_id else ""
        pov_prefix = f"[{pov_id}] " if pov_id else ""

        try:
            # Note: sanitizer is embedded in project_name (e.g., project-asan-deltaref)
            # helper.py reproduce doesn't accept --sanitizer flag
            cmd = [
                "python3",
                str(self._helper_script),
                "reproduce",
                *self._locale_helper_env_args(),
                project_name,
                harness,
                str(testcase_path),
                "--",
                "-detect_leaks=0",
            ]

            logger.debug(f"{req_prefix}Reproducing: {' '.join(cmd)}")

            # Use a longer outer timeout than the helper/libFuzzer timeout path
            # so helper.py can return its native reproduce exit semantics
            # (e.g., non-zero crash code or 124 timeout) instead of being cut off
            # by our subprocess guard under load.
            helper_timeout = timeout + 10
            # Use binary mode to handle fuzzer output that may contain non-UTF-8 bytes
            result = run_with_timeout(
                cmd,
                timeout=helper_timeout,
                cwd=self.oss_fuzz_path,
                text=False,
            )

            # Decode output with error handling for binary content
            stdout = result.stdout.decode("utf-8", errors="replace")
            stderr = result.stderr.decode("utf-8", errors="replace")

            # Handle exit codes explicitly
            if result.returncode == 0:
                reproduce_logger.debug(
                    f"{req_prefix}{pov_prefix}{project_name}/{harness} did not crash"
                )
                return ReproduceOutput(
                    crashed=False,
                    stdout=stdout,
                    stderr=stderr,
                    exit_code=0,
                )
            if result.returncode == EXIT_CODE_TIMEOUT:
                reproduce_logger.debug(
                    f"{req_prefix}{pov_prefix}{project_name}/{harness} "
                    "timed out (exit code 124)"
                )
                return ReproduceOutput(
                    crashed=False,
                    stdout=stdout,
                    stderr=stderr,
                    exit_code=124,
                )

            combined_output = f"{stdout}\n{stderr}"

            # Check for LeakSanitizer-only exit (not a real crash)
            # -detect_leaks=0 flag may not suppress ASAN_OPTIONS=detect_leaks=1
            if _is_leak_only_exit(combined_output):
                reproduce_logger.debug(
                    f"{req_prefix}{pov_prefix}{project_name}/{harness} "
                    f"LeakSanitizer only (exit code {result.returncode}), not a crash"
                )
                return ReproduceOutput(
                    crashed=False,
                    stdout=stdout,
                    stderr=stderr,
                    exit_code=result.returncode,
                )

            # Keep origin/main semantics:
            # any non-zero exit (except 124 and leak-only) is treated as crash.
            logger.debug(
                f"{req_prefix}{pov_prefix}{project_name}/{harness} crashed "
                f"(exit code {result.returncode})"
            )

            return ReproduceOutput(
                crashed=True,
                stdout=stdout,
                stderr=stderr,
                exit_code=result.returncode,
            )

        except subprocess.TimeoutExpired:
            # Our subprocess timeout (shouldn't happen with grace period)
            logger.warning(
                f"{req_prefix}Subprocess timeout for {project_name}/{harness}"
            )
            return ReproduceOutput(crashed=False)
        except Exception as e:
            logger.error(
                f"{req_prefix}Reproduce error for {project_name}/{harness}: {e}"
            )
            return ReproduceOutput(crashed=False)
        finally:
            # Clean up temporary file
            testcase_path.unlink(missing_ok=True)

    def run_coverage(
        self,
        project_name: str,
        harness: str,
        corpus_dir: Path,
        output_dir: Path,
        timeout: int = 300,
    ) -> tuple[bool, Path]:
        """Run coverage collection using OSS-Fuzz helper.py.

        Uses OSS-Fuzz's native coverage command, which writes coverage artifacts
        into the project's build output directory. Those artifacts are then
        copied into ``output_dir`` so callers have a stable per-run location.

        Args:
            project_name: Coverage variant name (e.g., "benchmark-coverage")
            harness: Harness/fuzz target name
            corpus_dir: Directory containing corpus files
            output_dir: Directory for coverage output (dumps, report, etc.)
            timeout: Timeout in seconds

        Returns:
            Tuple of (success: bool, output_dir: Path)
            The caller should parse the appropriate file based on language:
            - C/C++: output_dir/report/linux/summary.json
            - Java: output_dir/report/linux/jacoco.xml
        """
        output_dir = Path(output_dir)
        project_output_dir = self.oss_fuzz_path / "build" / "out" / project_name
        report_src = project_output_dir / "report"
        dumps_src = project_output_dir / "dumps"
        with self._acquire_coverage_lock(project_name):
            # helper.py coverage writes report/ and dumps/ directly into the shared
            # project build output. Clear any previous coverage artifacts so each
            # run sees isolated results before we copy them into output_dir.
            docker_rmtree(report_src)
            docker_rmtree(dumps_src)
            if output_dir.exists():
                shutil.rmtree(output_dir)
            output_dir.mkdir(parents=True, exist_ok=True)

            cmd = [
                "python3",
                str(self._helper_script),
                "coverage",
                "--corpus-dir",
                str(corpus_dir),
                "--fuzz-target",
                harness,
                "--no-serve",
                "--port",
                "",
                project_name,
            ]

            logger.debug(f"Running coverage: {' '.join(cmd)}")

            try:
                result = run_with_timeout(
                    cmd,
                    timeout=timeout,
                    cwd=self.oss_fuzz_path,
                )

                if result.returncode != 0:
                    logger.warning(
                        f"Coverage failed for {project_name}/{harness}: "
                        f"{result.stderr[:500]}"
                    )
                    return False, output_dir

                # Docker writes coverage artifacts into the project build output as
                # root-owned files. Normalize ownership before copying them out.
                fix_docker_ownership(report_src)
                fix_docker_ownership(dumps_src)

                # Copy OSS-Fuzz coverage artifacts from the project's build output
                # into the requested per-run output location.
                if report_src.exists():
                    shutil.copytree(report_src, output_dir / "report")
                if dumps_src.exists():
                    shutil.copytree(dumps_src, output_dir / "dumps")

                # Verify output was generated (check report dir exists)
                report_dir = output_dir / "report" / "linux"
                if not report_dir.exists():
                    logger.warning(f"Coverage report not found: {report_dir}")
                    return False, output_dir

                # Fix ownership of coverage output (Docker creates root-owned files)
                # Note: build output ownership is already fixed at build time
                fix_docker_ownership(output_dir)

                return True, output_dir

            except subprocess.TimeoutExpired:
                logger.warning(f"Coverage timeout for {project_name}/{harness}")
                fix_docker_ownership(output_dir)
                return False, output_dir
            except Exception as e:
                logger.error(f"Coverage error for {project_name}/{harness}: {e}")
                fix_docker_ownership(output_dir)
                return False, output_dir

    # =========================================================================
    # Inc-build image support for patch verification
    # =========================================================================

    def get_inc_build_image_name(
        self,
        project_name: str,
        sanitizer: str = "address",
        registry: str = DEFAULT_REGISTRY,
    ) -> str:
        """Get inc-build Docker image name.

        Args:
            project_name: OSS-Fuzz project name
            sanitizer: Sanitizer type (address, undefined, memory)
            registry: Docker registry

        Returns:
            Full Docker image name (e.g., ghcr.io/team-atlanta/crsbench/proj-asan:inc)
        """
        return registry_inc_image_name(project_name, sanitizer, registry=registry)

    def get_local_inc_image_name(
        self,
        project_name: str,
        sanitizer: str = "address",
        local_prefix: str = DEFAULT_LOCAL_IMAGE_PREFIX,
    ) -> str:
        """Get local CRSBench-managed inc-build image name."""
        return local_inc_image_name(project_name, sanitizer, local_prefix=local_prefix)

    def has_inc_build_image(
        self,
        project_name: str,
        sanitizer: str = "address",
        registry: str = DEFAULT_REGISTRY,
    ) -> bool:
        """Check if inc-build Docker image exists.

        Args:
            project_name: OSS-Fuzz project name
            sanitizer: Sanitizer type
            registry: Docker registry

        Returns:
            True if inc-build image exists
        """
        image_name = self.get_inc_build_image_name(project_name, sanitizer, registry)
        return self._docker_image_exists(image_name)

    def get_ossfuzz_image_name(
        self,
        project_name: str,
        sanitizer: str = "address",
    ) -> str:
        """Get OSS-Fuzz helper-compatible Docker image name."""
        return helper_inc_image_name(project_name, sanitizer)

    def is_inc_image_available(
        self,
        project_name: str,
        sanitizer: str = "address",
        registry: str = DEFAULT_REGISTRY,
        local_prefix: str = DEFAULT_LOCAL_IMAGE_PREFIX,
    ) -> bool:
        """Check if inc-build image exists locally and ensure OSS-Fuzz format.

        If image exists in registry format but not in OSS-Fuzz format,
        automatically retags it for OSS-Fuzz compatibility.

        Args:
            project_name: OSS-Fuzz project name
            sanitizer: Sanitizer type
            registry: Docker registry

        Returns:
            True if image exists locally (in OSS-Fuzz format)
        """
        helper_image = self.get_ossfuzz_image_name(project_name, sanitizer)
        local_image = self.get_local_inc_image_name(
            project_name, sanitizer, local_prefix
        )
        remote_image = self.get_inc_build_image_name(project_name, sanitizer, registry)

        # Helper-compatible image first.
        if self._docker_image_exists(helper_image):
            return True

        # Local CRSBench namespace image.
        if self._docker_image_exists(local_image):
            return self._retag_for_ossfuzz(local_image, helper_image)

        # Remote-namespaced image present locally.
        if self._docker_image_exists(remote_image):
            return self._retag_for_ossfuzz(
                remote_image, local_image
            ) and self._retag_for_ossfuzz(local_image, helper_image)

        return False

    def _get_remote_image_size(self, image_name: str) -> Optional[int]:
        """Get remote image size in bytes using docker manifest inspect."""
        try:
            result = subprocess.run(
                ["docker", "manifest", "inspect", image_name, "--verbose"],
                capture_output=True,
                text=True,
                timeout=60,
                stdin=subprocess.DEVNULL,
            )
            if result.returncode != 0:
                return None
            data = json.loads(result.stdout)
            if isinstance(data, list):
                total = 0
                for entry in data:
                    size = entry.get("Descriptor", {}).get("size")
                    if isinstance(size, int):
                        total += size
                return total or None
            if isinstance(data, dict):
                size = data.get("Descriptor", {}).get("size")
                if isinstance(size, int):
                    return size
            return None
        except Exception as e:
            logger.debug(f"Failed to read remote size for {image_name}: {e}")
            return None

    def _docker_image_exists(self, image_name: str) -> bool:
        """Check if a Docker image exists locally."""
        try:
            result = subprocess.run(
                ["docker", "image", "inspect", image_name],
                capture_output=True,
                timeout=30,
                stdin=subprocess.DEVNULL,
            )
            return result.returncode == 0
        except Exception as e:
            logger.debug(f"Error checking image {image_name}: {e}")
            return False

    def pull_inc_build_image(
        self,
        project_name: str,
        sanitizer: str = "address",
        registry: str = DEFAULT_REGISTRY,
        *,
        local_prefix: str = DEFAULT_LOCAL_IMAGE_PREFIX,
        timeout: int = 300,
    ) -> bool:
        """Pull inc-build image from registry and retag for OSS-Fuzz.

        Args:
            project_name: OSS-Fuzz project name
            sanitizer: Sanitizer type
            registry: Docker registry

        Returns:
            True if pull succeeded
        """
        helper_image = self.get_ossfuzz_image_name(project_name, sanitizer)
        local_image = self.get_local_inc_image_name(
            project_name, sanitizer, local_prefix
        )

        # Check if already available
        if self._docker_image_exists(helper_image):
            logger.debug(f"OSS-Fuzz compatible image already exists: {helper_image}")
            return True

        inc_image = self.get_inc_build_image_name(project_name, sanitizer, registry)

        # Check if source image exists locally
        if self._docker_image_exists(inc_image):
            logger.debug(f"Inc-build image available locally: {inc_image}")
            return self._retag_for_ossfuzz(
                inc_image, local_image
            ) and self._retag_for_ossfuzz(local_image, helper_image)

        # Pull from registry
        logger.debug(f"Pulling inc-build image: {inc_image}")
        try:
            result = run_with_timeout(
                ["docker", "pull", inc_image],
                timeout=timeout,
            )

            if result.returncode == 0:
                logger.debug(f"Successfully pulled: {inc_image}")
                return self._retag_for_ossfuzz(
                    inc_image, local_image
                ) and self._retag_for_ossfuzz(local_image, helper_image)

            logger.debug(f"Inc-build image not available: {inc_image}")
            return False
        except subprocess.TimeoutExpired:
            logger.error(f"Timeout pulling {inc_image}")
            return False
        except Exception as e:
            logger.error(f"Error pulling {inc_image}: {e}")
            return False

    def build_inc_build_image(
        self,
        project_name: str,
        sanitizer: str = "address",
        *,
        benchmark_path: Optional[Path] = None,
        local_prefix: str = DEFAULT_LOCAL_IMAGE_PREFIX,
    ) -> bool:
        """Build an inc image locally and map to local/helper tags.

        Local snapshot creation flow:
        1. Build base helper image via `helper.py build_image`.
        2. If benchmark_path is provided, bake benchmark project files into
           `/CRSBENCH_PROJECT`, run `compile` in-container to capture
           intermediate build state.
        3. Tag the baked image to local/helper `:inc`.
        """
        helper_latest = helper_latest_image_name(project_name)
        local_image = self.get_local_inc_image_name(
            project_name, sanitizer, local_prefix
        )
        helper_inc = self.get_ossfuzz_image_name(project_name, sanitizer)

        result = run_with_timeout(
            [
                "python3",
                str(self._helper_script),
                "build_image",
                "--no-pull",
                project_name,
            ],
            timeout=1200,
            cwd=self.oss_fuzz_path,
        )
        if result.returncode != 0:
            logger.warning(
                f"Failed local inc image build for {project_name}: "
                f"{(result.stderr or '').strip()[:400]}"
            )
            return False

        if benchmark_path is not None:
            if not self._bake_snapshot_state(
                project_name=project_name,
                sanitizer=sanitizer,
                benchmark_path=benchmark_path,
                image_name=helper_latest,
            ):
                return False

        return self._retag_for_ossfuzz(
            helper_latest, local_image
        ) and self._retag_for_ossfuzz(local_image, helper_inc)

    def _bake_snapshot_state(
        self,
        *,
        project_name: str,
        sanitizer: str,
        benchmark_path: Path,
        image_name: str,
    ) -> bool:
        """Bake source/project state + compile intermediates into image."""
        benchmark_path = Path(benchmark_path).resolve()
        if not benchmark_path.exists():
            logger.warning(
                f"Snapshot bake skipped for {project_name}: "
                f"benchmark path not found: {benchmark_path}"
            )
            return False

        workdir = self._resolve_test_workdir(image_name, benchmark_path)
        container_name = (
            f"crsbench-snapshot-{project_name}-{os.getpid()}-{int(time.time() * 1000)}"
        )
        project_mount = "/CRSBENCH_PROJ_PATH"
        fuzzing_language = self._resolve_fuzzing_language(benchmark_path)
        bake_script = self._render_template(
            "bake_snapshot.sh.tmpl",
            {
                "@@PROJECT_MOUNT@@": shlex.quote(project_mount),
                "@@WORKDIR@@": shlex.quote(workdir),
                "@@SANITIZER@@": shlex.quote(sanitizer),
                "@@PROJECT_NAME@@": shlex.quote(project_name),
                "@@FUZZING_LANGUAGE@@": shlex.quote(fuzzing_language),
            },
        )

        run_cmd = [
            "docker",
            "run",
            "--name",
            container_name,
            "--user",
            "0",
            "-v",
            f"{benchmark_path}:{project_mount}:ro",
        ]
        run_cmd.extend(self._locale_docker_run_args())
        run_cmd.extend(self._docker_resource_run_args())
        run_cmd.extend(
            [
                image_name,
                "/bin/bash",
                "-lc",
                bake_script,
            ]
        )

        commit_cmd = [
            "docker",
            "commit",
            container_name,
            image_name,
        ]
        rm_cmd = ["docker", "rm", "-f", container_name]

        try:
            run_result = run_with_timeout(run_cmd, timeout=3600)
            if run_result.returncode != 0:
                stdout_tail = (run_result.stdout or "").strip()[-1200:]
                stderr_tail = (run_result.stderr or "").strip()[-1200:]
                combined = "\n".join(
                    part
                    for part in (
                        f"[stdout tail]\n{stdout_tail}" if stdout_tail else "",
                        f"[stderr tail]\n{stderr_tail}" if stderr_tail else "",
                    )
                    if part
                )
                logger.warning(
                    f"Snapshot bake failed for {project_name}: "
                    f"{combined or '(no output)'}"
                )
                return False

            commit_result = run_with_timeout(
                commit_cmd, timeout=self.snapshot_commit_timeout
            )
            if commit_result.returncode != 0:
                logger.warning(
                    f"Snapshot commit failed for {project_name}: "
                    f"{(commit_result.stderr or '').strip()[:400]}"
                )
                return False

            logger.debug(f"Baked snapshot state into {image_name} for {project_name}")
            return True
        finally:
            try:
                run_with_timeout(rm_cmd, timeout=60)
            except Exception:
                pass

    def _resolve_fuzzing_language(self, benchmark_path: Path) -> str:
        """Resolve OSS-Fuzz FUZZING_LANGUAGE from project.yaml with safe default."""
        project_yaml = benchmark_path / "project.yaml"
        if not project_yaml.exists():
            return "c++"
        try:
            import yaml

            with project_yaml.open(encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            lang = str(data.get("language", "c")).strip().lower()
            return "c++" if lang == "c" else lang
        except Exception:
            return "c++"

    def _render_template(self, template_name: str, values: dict[str, str]) -> str:
        """Render a small shell template using exact placeholder replacement."""
        template_path = self._templates_dir / template_name
        if not template_path.exists():
            raise FileNotFoundError(f"Template not found: {template_path}")
        rendered = template_path.read_text(encoding="utf-8")
        for placeholder, value in values.items():
            rendered = rendered.replace(placeholder, value)
        return rendered

    def _retag_for_ossfuzz(self, src_image: str, dst_image: str) -> bool:
        """Retag an image for OSS-Fuzz compatibility."""
        try:
            result = subprocess.run(
                ["docker", "tag", src_image, dst_image],
                capture_output=True,
                text=True,
                timeout=30,
                stdin=subprocess.DEVNULL,
            )
            if result.returncode == 0:
                logger.debug(f"Retagged {src_image} -> {dst_image}")
                # Destination tag may now point to a different image id.
                self._replay_hooked_images.pop(dst_image, None)
                return True
            logger.error(f"Failed to retag: {result.stderr}")
            return False
        except Exception as e:
            logger.error(f"Error retagging image: {e}")
            return False

    def _get_local_image_id(self, image_name: str) -> Optional[str]:
        """Return local Docker image id for tag, or None if unavailable."""
        try:
            result = subprocess.run(
                ["docker", "image", "inspect", "--format", "{{.Id}}", image_name],
                capture_output=True,
                text=True,
                timeout=30,
                stdin=subprocess.DEVNULL,
            )
            if result.returncode != 0:
                return None
            image_id = (result.stdout or "").strip()
            return image_id if image_id else None
        except Exception:
            return None

    def _remove_local_image_if_exists(self, image_name: str) -> None:
        """Remove a local Docker image tag if present."""
        if not self._docker_image_exists(image_name):
            return
        try:
            result = subprocess.run(
                ["docker", "rmi", image_name],
                capture_output=True,
                text=True,
                timeout=120,
                stdin=subprocess.DEVNULL,
            )
            if result.returncode != 0:
                logger.debug(
                    f"Failed to remove image {image_name}: {(result.stderr or '').strip()[:200]}"
                )
        except Exception as e:
            logger.debug(f"Error removing image {image_name}: {e}")

    def prepare_inc_image_for_variant(
        self,
        project_name: str,
        variant_name: str,
        sanitizer: str = "address",
    ) -> bool:
        """Prepare inc-build image for a variant by retagging.

        This allows parallel test execution with proper path isolation.
        The inc-build image is retagged from project name to variant name.

        NOTE: Caller must ensure source image is available (via _ensure_inc_build_image)
        before calling this method.

        Args:
            project_name: Original project name (source image)
            variant_name: Variant name (target image)
            sanitizer: Sanitizer type

        Returns:
            True if image is ready for use
        """
        src_image = self.get_ossfuzz_image_name(project_name, sanitizer)
        # Variant runtime image names already include sanitizer in variant_name
        # (e.g., ...-asan-...), so keep :inc tag stable without appending
        # another sanitizer suffix.
        dst_image = f"gcr.io/oss-fuzz/{variant_name}:inc"
        dst_latest = helper_latest_image_name(variant_name)

        if self._docker_image_exists(dst_image) and self._docker_image_exists(
            dst_latest
        ):
            logger.debug(
                f"Variant inc-build image already exists: {dst_image} and {dst_latest}"
            )
            return True

        if not self._docker_image_exists(src_image):
            logger.warning(f"Source inc-build image not available: {src_image}")
            return False

        return self._retag_for_ossfuzz(
            src_image, dst_image
        ) and self._retag_for_ossfuzz(src_image, dst_latest)

    def prepare_image_for_variant(
        self,
        source_variant: str,
        target_variant: str,
        docker_tag: str = "latest",
    ) -> bool:
        """Prepare Docker image for a derived variant by retagging.

        This allows test execution with a separate variant name without rebuilding.
        The source image is retagged to the target variant name.

        Args:
            source_variant: Source variant name (e.g., base patched variant)
            target_variant: Target variant name (e.g., with -unittest suffix)
            docker_tag: Docker image tag (default: "latest")

        Returns:
            True if image is ready for use
        """
        src_image = f"gcr.io/oss-fuzz/{source_variant}:{docker_tag}"
        dst_image = f"gcr.io/oss-fuzz/{target_variant}:{docker_tag}"

        if self._docker_image_exists(dst_image):
            logger.debug(f"Target variant image already exists: {dst_image}")
            return True

        if not self._docker_image_exists(src_image):
            logger.warning(f"Source variant image not found: {src_image}")
            return False

        return self._retag_for_ossfuzz(src_image, dst_image)

    def _get_inc_image_lock(self, cache_key: str) -> threading.Lock:
        """Get or create a per-project lock for inc-image operations.

        This allows parallel pulls for different projects while preventing
        concurrent pulls for the same project.

        Args:
            cache_key: Cache key (project_name:sanitizer)

        Returns:
            Lock for the specific project/sanitizer combination
        """
        with self._inc_image_locks_lock:
            if cache_key not in self._inc_image_locks:
                self._inc_image_locks[cache_key] = threading.Lock()
            return self._inc_image_locks[cache_key]

    def _inc_image_cache_key(
        self, project_name: str, sanitizer: str, registry: str, local_prefix: str
    ) -> str:
        """Build cache key for inc image lookups, scoped by namespace settings."""
        return f"{registry}|{local_prefix}|{project_name}:{sanitizer}"

    def _distributed_inc_lock_enabled(self) -> bool:
        """Return whether Redis-based distributed inc-image lock should be used."""
        mode = (self.inc_image_dist_lock_mode or "auto").strip().lower()
        if mode in {"1", "true", "yes", "on"}:
            return True
        if mode in {"0", "false", "no", "off"}:
            return False
        # Auto mode: enable when a Redis connection is actually available.
        # This covers RQ worker jobs, evaluator subprocesses, and any runtime
        # where distributed queues are active even without explicit env hints.
        return self._get_inc_image_dist_lock_connection() is not None

    def _get_inc_image_dist_lock_connection(self):
        """Get Redis connection for distributed inc-image lock, if available."""
        if self._inc_image_dist_lock_conn is not None:
            return self._inc_image_dist_lock_conn
        with self._inc_image_dist_lock_conn_lock:
            if self._inc_image_dist_lock_conn is not None:
                return self._inc_image_dist_lock_conn
            try:
                # Prefer the active RQ worker/job connection when available.
                import rq

                current_job = rq.get_current_job()
                if current_job is not None and current_job.connection is not None:
                    self._inc_image_dist_lock_conn = current_job.connection
                    return self._inc_image_dist_lock_conn
            except Exception:
                pass
            try:
                from crsbench.distributed.queue import create_redis_connection

                redis_host = os.getenv("CRSBENCH_REDIS_HOST", "localhost")
                self._inc_image_dist_lock_conn = create_redis_connection(redis_host)
                return self._inc_image_dist_lock_conn
            except Exception as exc:
                if not self._inc_image_dist_lock_warned:
                    logger.warning(
                        "Distributed inc-image lock disabled for this process "
                        f"(Redis unavailable): {exc}"
                    )
                    self._inc_image_dist_lock_warned = True
                return None

    @staticmethod
    def _inc_image_dist_lock_key(cache_key: str) -> str:
        key_hash = hashlib.sha256(cache_key.encode()).hexdigest()[:24]
        return f"crsbench:inc-image-lock:v1:{key_hash}"

    def _acquire_inc_image_dist_lock(
        self, lock_key: str, wait_timeout_sec: int
    ) -> Optional[_RedisLockLease]:
        """Acquire Redis distributed lock for inc-image prepare with bounded wait."""
        conn = self._get_inc_image_dist_lock_connection()
        if conn is None:
            return None
        token = str(uuid.uuid4())
        deadline = time.monotonic() + max(0, wait_timeout_sec)
        while True:
            try:
                acquired = conn.set(
                    lock_key,
                    token,
                    nx=True,
                    ex=self.inc_image_dist_lock_ttl_sec,
                )
            except Exception as exc:
                logger.warning(
                    f"Failed to acquire distributed inc-image lock {lock_key}: {exc}"
                )
                return None
            if acquired:
                return _RedisLockLease(key=lock_key, token=token)
            if time.monotonic() >= deadline:
                return None
            time.sleep(self.inc_image_dist_lock_poll_sec)

    def _renew_inc_image_dist_lock(self, lease: _RedisLockLease) -> bool:
        """Renew lock TTL only if token still matches."""
        conn = self._get_inc_image_dist_lock_connection()
        if conn is None:
            return False
        script = (
            "if redis.call('get', KEYS[1]) == ARGV[1] then "
            "  return redis.call('expire', KEYS[1], tonumber(ARGV[2])) "
            "else return 0 end"
        )
        try:
            result = conn.eval(
                script,
                1,
                lease.key,
                lease.token,
                str(self.inc_image_dist_lock_ttl_sec),
            )
        except Exception:
            return False
        return bool(result)

    def _release_inc_image_dist_lock(self, lease: _RedisLockLease) -> bool:
        """Release lock only if token still matches owner token."""
        conn = self._get_inc_image_dist_lock_connection()
        if conn is None:
            return False
        script = (
            "if redis.call('get', KEYS[1]) == ARGV[1] then "
            "  return redis.call('del', KEYS[1]) "
            "else return 0 end"
        )
        try:
            result = conn.eval(script, 1, lease.key, lease.token)
        except Exception:
            return False
        return bool(result)

    def ensure_inc_image(
        self,
        project_name: str,
        sanitizer: str = "address",
        registry: Optional[str] = None,
        *,
        benchmark_path: Optional[Path] = None,
        policy: Optional[str] = None,
        max_pull_bytes: Optional[int] = None,
        pull_timeout: Optional[int] = None,
        local_prefix: Optional[str] = None,
        force_rebuild: bool = False,
    ) -> bool:
        """Ensure inc-build image is available for use.

        This is a convenience method that:
        1. Checks if inc-build image exists locally (in OSS-Fuzz format)
        2. If not found locally, tries to pull from registry
        3. Returns True if image is available, False otherwise

        Use this method to prepare inc-build images before building variants.
        If this method returns False, callers should fall back to standard builds.

        Uses per-project locks to allow parallel pulls for different projects
        while preventing redundant pulls for the same project.

        Args:
            project_name: OSS-Fuzz project name
            sanitizer: Sanitizer type (default: "address")
            registry: Docker registry (default: ghcr.io/team-atlanta/crsbench)

        Returns:
            True if inc-build image is available and ready for use
        """
        registry = registry or self.inc_image_registry
        policy = policy or self.inc_image_policy
        if max_pull_bytes is None:
            max_pull_bytes = self.inc_image_max_pull_bytes
        if pull_timeout is None:
            pull_timeout = self.inc_image_pull_timeout
        local_prefix = local_prefix or self.local_image_prefix
        cache_key = self._inc_image_cache_key(
            project_name, sanitizer, registry, local_prefix
        )

        if force_rebuild:
            self._inc_image_cache.pop(cache_key, None)
            self._inc_image_last_failure.pop(cache_key, None)
            self._replay_hooked_images.pop(
                self.get_ossfuzz_image_name(project_name, sanitizer), None
            )
            self._replay_hooked_images.pop(helper_latest_image_name(project_name), None)
            self._remove_local_image_if_exists(
                self.get_ossfuzz_image_name(project_name, sanitizer)
            )
            self._remove_local_image_if_exists(helper_latest_image_name(project_name))
            self._remove_local_image_if_exists(
                self.get_local_inc_image_name(project_name, sanitizer, local_prefix)
            )
            self._remove_local_image_if_exists(
                self.get_inc_build_image_name(project_name, sanitizer, registry)
            )

        # Quick check without lock - if already cached, return immediately
        if cache_key in self._inc_image_cache:
            return self._inc_image_cache[cache_key]

        # Avoid retry storms after transient failures; retry after cooldown.
        last_failure = self._inc_image_last_failure.get(cache_key)
        now = time.monotonic()
        if (
            last_failure is not None
            and now - last_failure < self.inc_image_retry_interval_sec
        ):
            return False

        # Get per-project lock (allows parallel pulls for different projects)
        project_lock = self._get_inc_image_lock(cache_key)

        with project_lock:
            # Double-check after acquiring lock (another thread may have completed)
            if cache_key in self._inc_image_cache:
                return self._inc_image_cache[cache_key]
            last_failure = self._inc_image_last_failure.get(cache_key)
            now = time.monotonic()
            if (
                last_failure is not None
                and now - last_failure < self.inc_image_retry_interval_sec
            ):
                return False

            lease: Optional[_RedisLockLease] = None
            renew_stop = threading.Event()
            lease_lost = threading.Event()
            renew_thread: Optional[threading.Thread] = None
            if self._distributed_inc_lock_enabled():
                lock_key = self._inc_image_dist_lock_key(cache_key)
                lease = self._acquire_inc_image_dist_lock(
                    lock_key, self.inc_image_dist_lock_wait_sec
                )
                if lease is None:
                    # Another worker may have completed while we waited.
                    if self.is_inc_image_available(
                        project_name,
                        sanitizer,
                        registry,
                        local_prefix=local_prefix,
                    ):
                        self._inc_image_cache[cache_key] = True
                        self._inc_image_last_failure.pop(cache_key, None)
                        return True
                    logger.warning(
                        "Timed out waiting for distributed inc-image lock for "
                        f"{project_name}:{sanitizer}; falling back to standard build"
                    )
                    return False

                def _renew_loop() -> None:
                    while not renew_stop.wait(
                        self.inc_image_dist_lock_renew_interval_sec
                    ):
                        if not self._renew_inc_image_dist_lock(lease):
                            logger.warning(
                                "Lost distributed inc-image lock lease for "
                                f"{project_name}:{sanitizer}"
                            )
                            lease_lost.set()
                            break

                renew_thread = threading.Thread(
                    target=_renew_loop,
                    name=f"inc-image-lock-renew-{project_name}-{sanitizer}",
                    daemon=True,
                )
                renew_thread.start()

            try:
                # Check if already available locally.
                if self.is_inc_image_available(
                    project_name,
                    sanitizer,
                    registry,
                    local_prefix=local_prefix,
                ):
                    logger.debug(f"Inc-build image available for {project_name}")
                    self._inc_image_cache[cache_key] = True
                    self._inc_image_last_failure.pop(cache_key, None)
                    return True
                if lease_lost.is_set():
                    logger.warning(
                        "Skipping inc-image prepare after distributed lock lease loss "
                        f"for {project_name}:{sanitizer}"
                    )
                    return False

                allow_pull = policy in ("auto", "pull_only")
                allow_build = policy in ("auto", "build_only")

                # Pull path with optional remote size gate.
                if allow_pull:
                    remote_image = self.get_inc_build_image_name(
                        project_name, sanitizer, registry
                    )
                    if max_pull_bytes is not None:
                        remote_size = self._get_remote_image_size(remote_image)
                        if remote_size is not None and remote_size > max_pull_bytes:
                            logger.info(
                                f"Skipping pull for {project_name}:{sanitizer} "
                                f"(remote size {remote_size} > cap {max_pull_bytes})"
                            )
                        else:
                            logger.debug(
                                "Inc-build image not found locally, trying pull: "
                                f"{project_name}"
                            )
                            if self.pull_inc_build_image(
                                project_name,
                                sanitizer,
                                registry,
                                local_prefix=local_prefix,
                                timeout=pull_timeout,
                            ):
                                logger.debug(
                                    f"Successfully pulled inc-build image for {project_name}"
                                )
                                self._inc_image_cache[cache_key] = True
                                self._inc_image_last_failure.pop(cache_key, None)
                                return True
                            if lease_lost.is_set():
                                logger.warning(
                                    "Distributed lock lease lost during inc-image pull "
                                    f"for {project_name}:{sanitizer}"
                                )
                                return False
                    else:
                        logger.info(
                            "Inc-build image not found locally, trying pull: "
                            f"{project_name}"
                        )
                        if self.pull_inc_build_image(
                            project_name,
                            sanitizer,
                            registry,
                            local_prefix=local_prefix,
                            timeout=pull_timeout,
                        ):
                            logger.debug(
                                f"Successfully pulled inc-build image for {project_name}"
                            )
                            self._inc_image_cache[cache_key] = True
                            self._inc_image_last_failure.pop(cache_key, None)
                            return True
                        if lease_lost.is_set():
                            logger.warning(
                                "Distributed lock lease lost during inc-image pull "
                                f"for {project_name}:{sanitizer}"
                            )
                            return False

                # Local-build path.
                if allow_build:
                    if lease_lost.is_set():
                        logger.warning(
                            "Skipping local inc-image build after distributed lock "
                            f"lease loss for {project_name}:{sanitizer}"
                        )
                        return False
                    logger.info(
                        f"Preparing local inc image for {project_name}:{sanitizer} "
                        "(on-demand fallback)"
                    )
                    if self.build_inc_build_image(
                        project_name,
                        sanitizer,
                        benchmark_path=benchmark_path,
                        local_prefix=local_prefix,
                    ):
                        self._inc_image_cache[cache_key] = True
                        self._inc_image_last_failure.pop(cache_key, None)
                        return True
                    if lease_lost.is_set():
                        logger.warning(
                            "Distributed lock lease lost during local inc-image build "
                            f"for {project_name}:{sanitizer}"
                        )
                        return False

                logger.warning(
                    f"Inc-build image not available for {project_name}, "
                    "will fall back to standard build"
                )
                self._inc_image_last_failure[cache_key] = time.monotonic()
                return False
            finally:
                if lease is not None:
                    renew_stop.set()
                    if renew_thread is not None:
                        renew_thread.join(timeout=1)
                    if not self._release_inc_image_dist_lock(lease):
                        logger.warning(
                            "Failed to release distributed inc-image lock for "
                            f"{project_name}:{sanitizer}"
                        )

    # =========================================================================
    # Unit test support for patch verification
    # =========================================================================

    @staticmethod
    def _normalize_container_workdir(raw_workdir: str) -> str:
        """Normalize container WORKDIR with conservative defaults."""
        workdir = (raw_workdir or "").strip()
        if not workdir:
            return "/src"
        workdir = workdir.replace("${SRC}", "/src").replace("$SRC", "/src")
        parts_raw = [part for part in workdir.split("/") if part]
        if ".." in parts_raw:
            return "/src"
        if not workdir.startswith("/"):
            workdir = f"/src/{workdir}"
        normalized = posixpath.normpath(workdir)
        if not normalized.startswith("/"):
            normalized = f"/{normalized}"
        # Guard against unsafe or unexpected sync targets.
        if normalized in ("", "/", "/."):
            return "/src"
        # Test-source overlays are supported only under /src semantics.
        if normalized == "/src":
            return normalized
        if not normalized.startswith("/src/"):
            return "/src"
        return normalized

    @staticmethod
    def _parse_workdir_from_dockerfile(project_dir: Path) -> str:
        """Parse Dockerfile WORKDIR with cumulative relative semantics."""
        dockerfile = project_dir / "Dockerfile"
        if not dockerfile.exists():
            return "/src"
        workdir = "/src"
        pattern = re.compile(r"^\s*WORKDIR\s+(.+?)\s*$")
        for raw_line in dockerfile.read_text(
            encoding="utf-8", errors="ignore"
        ).splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.upper().startswith("FROM "):
                workdir = "/src"
                continue
            match = pattern.match(line)
            if not match:
                continue
            value = match.group(1).split("#", 1)[0].strip().strip("\"'")
            value = value.replace("${SRC}", "/src").replace("$SRC", "/src")
            if value.startswith("/"):
                workdir = posixpath.normpath(value)
            else:
                workdir = posixpath.normpath(posixpath.join(workdir, value))
        return workdir or "/src"

    def _resolve_test_workdir(self, image: str, project_dir: Path) -> str:
        """Resolve runtime WORKDIR for test execution."""
        inspect_cmd = [
            "docker",
            "image",
            "inspect",
            "--format",
            "{{.Config.WorkingDir}}",
            image,
        ]
        try:
            inspect_result = run_with_timeout(inspect_cmd, timeout=15)
            if inspect_result.returncode == 0:
                return self._normalize_container_workdir(inspect_result.stdout.strip())
            logger.debug(
                "Failed to inspect image working dir for {} (code={}, stderr={})",
                image,
                inspect_result.returncode,
                inspect_result.stderr.strip(),
            )
        except Exception as e:
            logger.debug(f"Image inspect failed for {image}: {e}")

        try:
            parsed = self._parse_workdir_from_dockerfile(project_dir)
            return self._normalize_container_workdir(parsed)
        except Exception as e:
            logger.debug(f"Dockerfile WORKDIR fallback failed for {project_dir}: {e}")
            return "/src"

    def run_tests(
        self,
        project_name: str,
        src_path: Path,
        sanitizer: str = "address",
        timeout: int = 1800,
        *,
        rts_mode: bool = False,
        docker_tag: str = "latest",
    ) -> tuple[bool, str, str]:
        """Run unit tests by executing benchmark test script inside builder image.

        Args:
            project_name: OSS-Fuzz project name
            src_path: Path to source code
            sanitizer: Sanitizer type
            timeout: Timeout in seconds
            rts_mode: If True, run only patch-affected tests (RTS)
            docker_tag: Docker image tag to use (e.g., "latest", "inc")

        Returns:
            Tuple of (passed, stdout, stderr)
        """
        project_dir = self.projects_base / project_name
        run_tests_sh = project_dir / "run_tests.sh"
        src_path = src_path.resolve()
        project_mount = "/CRSBENCH_PROJ_PATH"
        patched_src_container = "/CRSBENCH_PATCHED_SRC"
        image = f"gcr.io/oss-fuzz/{project_name}:{docker_tag}"
        test_workdir = self._resolve_test_workdir(image, project_dir)
        replay_policy = resolve_replay_policy(inc_build=(docker_tag == "inc"))
        if replay_policy.enabled:
            if not self._ensure_replay_hooks_in_image(image, replay_policy):
                return False, "", "Replay hook installation failed"
        logger.info(
            "Resolved test workdir for {}: image={} workdir={}",
            project_name,
            image,
            test_workdir,
        )
        test_cmd = (
            f'if [ -n "${{CRSBENCH_REPLAY_BIN:-}}" ] && [ -d "${{CRSBENCH_REPLAY_BIN}}" ]; then '
            'case ":$PATH:" in *":${CRSBENCH_REPLAY_BIN}:"*) ;; '
            '*) export PATH="${CRSBENCH_REPLAY_BIN}:$PATH" ;; esac; fi; '
            f"project_path={shlex.quote(project_mount)}; "
            f"patched_src={shlex.quote(patched_src_container)}; "
            f"workdir={shlex.quote(test_workdir)}; "
            'case "$workdir" in ""|"/") echo "Unsafe workdir: $workdir"; exit 2;; esac; '
            'if [ -L "$workdir" ]; then echo "Unsafe symlink workdir: $workdir"; exit 2; fi; '
            'mkdir -p "$workdir"; '
            'resolved_workdir="$workdir"; '
            "if command -v realpath >/dev/null 2>&1; then "
            'resolved_workdir=$(realpath -m "$workdir" 2>/dev/null || echo "$workdir"); '
            "else "
            'echo "realpath unavailable; refusing unsafe sync"; exit 2; '
            "fi; "
            'case "$resolved_workdir" in /src|/src/*) ;; '
            '*) echo "Unsafe resolved workdir: $resolved_workdir"; exit 2;; esac; '
            'if [ -d "$patched_src" ]; then '
            'if [ "$workdir" = "/src" ]; then '
            "preserve_list=$(mktemp); "
            "for env_var in MVN MAVEN_HOME M2_HOME GRADLE_HOME; do "
            'env_val="${!env_var:-}"; '
            'case "$env_val" in '
            '/src/*) top="${env_val#/src/}"; top="${top%%/*}"; [ -n "$top" ] && echo "$top" >> "$preserve_list" ;; '
            "esac; "
            "done; "
            'manual_preserve="${CRSBENCH_TEST_PRESERVE_TOPLEVEL:-}"; '
            'if [ -n "$manual_preserve" ]; then '
            'for top in ${manual_preserve//,/ }; do [ -n "$top" ] && echo "$top" >> "$preserve_list"; done; '
            "fi; "
            "for path_entry in ${PATH//:/ }; do "
            'case "$path_entry" in '
            '/src/*) top="${path_entry#/src/}"; top="${top%%/*}"; [ -n "$top" ] && echo "$top" >> "$preserve_list" ;; '
            "esac; "
            "done; "
            'sort -u -o "$preserve_list" "$preserve_list" 2>/dev/null || true; '
            "for p in /src/* /src/.[!.]* /src/..?*; do "
            '[ -e "$p" ] || continue; '
            'base=$(basename "$p"); '
            'if ! grep -Fxq "$base" "$preserve_list"; then rm -rf "$p"; fi; '
            "done; "
            'cp -a "$patched_src/." "$workdir/"; '
            'rm -f "$preserve_list"; '
            "else "
            "if command -v rsync >/dev/null 2>&1; then "
            'rsync -a --delete "$patched_src/" "$workdir/"; '
            "else "
            'find "$workdir" -mindepth 1 -maxdepth 1 -exec rm -rf {} +; '
            'cp -a "$patched_src/." "$workdir/"; '
            "fi; "
            "fi; "
            "fi; "
            'if [ -f "$project_path/test.sh" ]; then '
            'cd "$workdir" && /bin/bash "$project_path/test.sh"; '
            'elif [ -f "$project_path/run_tests.sh" ]; then '
            'cd "$workdir" && /bin/bash "$project_path/run_tests.sh"; '
            "else echo 'No test.sh or run_tests.sh found, skipping'; exit 0; fi"
        )
        if rts_mode:
            test_cmd = f"export RTS_MODE=1; {test_cmd}"

        out_dir = self.get_build_output_path(project_name)
        work_dir = self.get_isolated_work_path(project_name)
        out_dir.mkdir(parents=True, exist_ok=True)
        work_dir.mkdir(parents=True, exist_ok=True)

        cmd = [
            "docker",
            "run",
            "--rm",
            "--user",
            "0",
            "-e",
            f"SANITIZER={sanitizer}",
            "-e",
            f"FUZZING_LANGUAGE={self._resolve_fuzzing_language(project_dir)}",
            "-e",
            f"PROJECT_NAME={project_name}",
            "-e",
            f"CRSBENCH_PATCHED_SRC={patched_src_container}",
            "-e",
            f"CRSBENCH_PROJ_PATH={project_mount}",
            "-e",
            f"CRSBENCH_EFFECTIVE_WORKDIR={test_workdir}",
            "-v",
            f"{src_path}:{patched_src_container}:ro",
            "-v",
            f"{project_dir.resolve()}:{project_mount}:ro",
            "-v",
            f"{out_dir.resolve()}:/out",
            "-v",
            f"{work_dir.resolve()}:/work",
        ]
        cmd[5:5] = self._locale_docker_run_args()
        cmd.extend(self._docker_resource_run_args())
        cmd.extend(
            [
                image,
                "/bin/bash",
                "-lc",
                test_cmd,
            ]
        )
        if replay_policy.enabled:
            cmd[cmd.index(image) : cmd.index(image)] = [
                "-e",
                f"CRSBENCH_REPLAY_POLICY_SOURCE={replay_policy.source}",
                "-e",
                f"CRSBENCH_REPLAY_POLICY_TOOLS={replay_policy.tool_hooks_csv()}",
            ]
        if run_tests_sh.exists():
            cmd[cmd.index(image) : cmd.index(image)] = [
                "-v",
                f"{run_tests_sh.resolve()}:/src/run_tests.sh:ro",
            ]

        logger.debug(
            f"Running unit tests for {project_name} "
            f"(rts={rts_mode}, sanitizer={sanitizer}, tag={docker_tag})"
        )
        logger.debug(f"Command: {' '.join(cmd)}")

        try:
            result = run_with_timeout(
                cmd,
                timeout=timeout,
            )

            passed = result.returncode == 0
            if passed:
                logger.debug(f"Unit tests passed for {project_name}")
            else:
                logger.warning(
                    f"Unit tests failed for {project_name} "
                    f"(exit code: {result.returncode})"
                )

            fix_docker_ownership(self.get_build_output_path(project_name))
            fix_docker_ownership(work_dir)
            fix_docker_ownership(src_path)
            return passed, result.stdout, result.stderr

        except subprocess.TimeoutExpired:
            logger.error(f"Test timeout for {project_name}")
            fix_docker_ownership(self.get_build_output_path(project_name))
            fix_docker_ownership(work_dir)
            fix_docker_ownership(src_path)
            return False, "", "Test execution timed out"
        except Exception as e:
            logger.error(f"Test execution error for {project_name}: {e}")
            fix_docker_ownership(self.get_build_output_path(project_name))
            fix_docker_ownership(work_dir)
            fix_docker_ownership(src_path)
            return False, "", str(e)

    def is_tests_available(self, project_name: str) -> bool:
        """Check if test.sh exists for the project.

        Args:
            project_name: OSS-Fuzz project name

        Returns:
            True if test.sh exists
        """
        project_dir = self.projects_base / project_name
        return (project_dir / "test.sh").exists() or (
            project_dir / "run_tests.sh"
        ).exists()
