"""Rsync-based artifact collector for GCE workers.

Implements a stage-then-publish pattern:
1. rsync trial artifacts from worker into a staging directory
2. verify the staged tree contains valid trials (metadata.json sentinel)
3. publish by merging staging into the final experiment_filestore path
4. clean up staging

Covers ARTF-01 through ARTF-04.
"""

from __future__ import annotations

import json
import math
import os
import posixpath
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath  # noqa: TC003
from typing import TYPE_CHECKING, Iterator, Protocol

import tenacity

from crsbench.cloud.launch_state import cloud_state_dir, remote_logs_dir
from crsbench.cloud.orchestrator_tunnel import allocate_local_port, wait_for_local_port
from crsbench.cloud.transport import CloudTransport, transport_for_provider
from crsbench.utils.logger import get_logger

if TYPE_CHECKING:
    from crsbench.cloud.records import CloudInstanceLike


class SshTransportConfig(Protocol):
    """Minimal transport settings needed for rsync/SSH collection."""

    project: str
    zone: str | None
    ssh_via_iap: bool


logger = get_logger(__name__)

_IAP_TUNNEL_PORT = 22
_IAP_TUNNEL_STARTUP_TIMEOUT_SEC = 30.0
_COLLECT_MARKER_FILENAME = ".crsbench-collect.json"
_ARTIFACT_RSYNC_EXCLUDES: tuple[str, ...] = (
    "oss-crs-workdir/",
    "output/logs/",
)
_REHYDRATE_EXCLUDED_TOPLEVEL_DIRS = frozenset({"oss-crs-workdir"})
_DROP_EXCLUDED_TOPLEVEL_DIRS = frozenset({"staged"})
_REPORT_LOG_RSYNC_EXCLUDES: tuple[str, ...] = ("oss-crs-workdir/",)
_LOG_RSYNC_EXCLUDES: tuple[str, ...] = ()
_REPORT_LOG_RSYNC_INCLUDES: tuple[str, ...] = (
    "output/logs/services/*_patcher.stdout.log",
    "output/logs/services/*inc-builder-*.stdout.log",
    "output/logs/crs/*/log_dir/verify_patch_timing.json",
    "output/logs/crs/**/*_patcher.stdout.log",
    "output/logs/crs/**/*inc-builder-*.stdout.log",
)
_TRIAL_DIR_NAME_RE = re.compile(r"^trial-\d+$")
_TRIAL_ROOT_MODES = frozenset({"delta", "full", "all"})
_TRIAL_ROOT_SANITIZERS = frozenset({"address", "memory", "undefined"})
_FAILED_TRIAL_ROOT_KEEP_FILENAMES = frozenset({"metadata.json", "worker.log", ".fail"})
_REEVAL_SUBMISSION_ARTIFACT_DIRNAME = ".cloud-reeval"
_REEVAL_TERMINAL_STATES = frozenset({"succeeded", "failed"})
_REEVAL_REMOTE_TEXT_ARTIFACTS: tuple[tuple[str, str], ...] = (
    ("summary.json", "summary.json"),
    ("runner.log", "runner.log"),
    ("bundle/manifest.json", "manifest.json"),
)


def _rsync_exact_path_excludes(relpaths: list[Path]) -> list[str]:
    """Return rsync exclude patterns that block exact paths and any descendants."""
    patterns: list[str] = []
    for relpath in relpaths:
        posix = relpath.as_posix()
        patterns.append(posix)
        patterns.append(f"{posix}/***")
    return patterns


def _is_report_log_file(relpath: str) -> bool:
    """Return whether one logical path is part of the kept report-log subset."""
    parts = PurePosixPath(relpath).parts
    for index in range(len(parts) - 1):
        if parts[index : index + 2] != ("output", "logs"):
            continue
        tail = parts[index + 2 :]
        if len(tail) == 2 and tail[0] == "services":
            name = tail[-1]
            return name.endswith("_patcher.stdout.log") or "inc-builder-" in name
        if len(tail) >= 2 and tail[0] == "crs":
            name = tail[-1]
            if name.endswith("_patcher.stdout.log") or "inc-builder-" in name:
                return True
            if tail[-2] == "log_dir" and name == "verify_patch_timing.json":
                return True
    return False


def _is_trial_root_name(name: str) -> bool:
    """Return whether one directory name matches the canonical trial-root pattern."""
    return _TRIAL_DIR_NAME_RE.fullmatch(name) is not None


def _trial_relpath_matches_layout(relpath: PurePosixPath) -> bool:
    """Return whether one relative path matches the canonical trial layout."""
    parts = relpath.parts
    if len(parts) == 6:
        mode_index = 3
        sanitizer_index = 4
    elif len(parts) == 7:
        mode_index = 4
        sanitizer_index = 5
    else:
        return False
    return (
        _is_trial_root_name(parts[-1])
        and parts[mode_index] in _TRIAL_ROOT_MODES
        and parts[sanitizer_index] in _TRIAL_ROOT_SANITIZERS
    )


def _load_trial_root_identity(path: Path) -> tuple[str, str, str, str] | None:
    """Return trial metadata fields needed to validate one canonical trial path."""
    metadata_path = path / "metadata.json"
    if not metadata_path.is_file():
        return None
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    crs = payload.get("crs")
    benchmark = payload.get("benchmark")
    harness = payload.get("harness")
    trial_num = payload.get("trial_num")
    if (
        not isinstance(crs, str)
        or not crs
        or not isinstance(benchmark, str)
        or not benchmark
        or not isinstance(harness, str)
        or not harness
        or isinstance(trial_num, bool)
    ):
        return None
    try:
        trial_name = f"trial-{int(trial_num)}"
    except (TypeError, ValueError):
        return None
    return crs, benchmark, harness, trial_name


def _trial_relpath_matches_metadata(
    relpath: PurePosixPath,
    *,
    trial_identity: tuple[str, str, str, str],
) -> bool:
    """Return whether one relative path agrees with metadata-derived identity."""
    crs, benchmark, harness, trial_name = trial_identity
    parts = relpath.parts
    return (
        parts[-1] == trial_name
        and len(parts) in {6, 7}
        and parts[:3] == (crs, benchmark, harness)
    )


def _is_trial_root_dir(path: Path, *, root: Path) -> bool:
    """Return whether one directory is a real trial root rather than payload content."""
    if not path.is_dir():
        return False
    try:
        relpath = PurePosixPath(path.relative_to(root).as_posix())
    except ValueError:
        return False
    if not _trial_relpath_matches_layout(relpath):
        return False
    trial_identity = _load_trial_root_identity(path)
    if trial_identity is None:
        return (path / ".success").exists() or (path / ".fail").exists()
    return _trial_relpath_matches_metadata(relpath, trial_identity=trial_identity)


def _iter_trial_dirs(root: Path) -> Iterator[Path]:
    """Yield real trial roots under ``root`` and prune descent once one is found."""
    yield from _iter_trial_dirs_under(root, root=root)


def _iter_trial_dirs_under(path: Path, *, root: Path) -> Iterator[Path]:
    """Yield real trial roots while carrying the experiment-root context."""
    if _is_trial_root_dir(path, root=root):
        yield path
        return

    try:
        iterator = os.scandir(path)
    except OSError:
        return

    child_dirs: list[Path] = []
    with iterator as entries:
        for entry in entries:
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                continue
            if not is_dir:
                continue
            child_dirs.append(Path(entry.path))

    for child_dir in sorted(child_dirs):
        yield from _iter_trial_dirs_under(child_dir, root=root)


_TRIAL_ROOT_DISCOVERY_SCRIPT = r"""
import json
import os
import pathlib
import re
import sys


TRIAL_DIR_RE = re.compile(r"^trial-\d+$")
TRIAL_ROOT_MODES = {"delta", "full", "all"}
TRIAL_ROOT_SANITIZERS = {"address", "memory", "undefined"}


def _matches_layout(rel_parts: tuple[str, ...]) -> bool:
    if len(rel_parts) == 6:
        mode_index = 3
        sanitizer_index = 4
    elif len(rel_parts) == 7:
        mode_index = 4
        sanitizer_index = 5
    else:
        return False
    return (
        TRIAL_DIR_RE.fullmatch(rel_parts[-1])
        and rel_parts[mode_index] in TRIAL_ROOT_MODES
        and rel_parts[sanitizer_index] in TRIAL_ROOT_SANITIZERS
    )


def _load_trial_identity(path: pathlib.Path) -> tuple[str, str, str, str] | None:
    metadata_path = path / "metadata.json"
    if not metadata_path.is_file():
        return None
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    crs = payload.get("crs")
    benchmark = payload.get("benchmark")
    harness = payload.get("harness")
    trial_num = payload.get("trial_num")
    if (
        not isinstance(crs, str)
        or not crs
        or not isinstance(benchmark, str)
        or not benchmark
        or not isinstance(harness, str)
        or not harness
        or isinstance(trial_num, bool)
    ):
        return None
    try:
        trial_name = f"trial-{int(trial_num)}"
    except (TypeError, ValueError):
        return None
    return crs, benchmark, harness, trial_name


def _matches_metadata(
    rel_parts: tuple[str, ...],
    *,
    trial_identity: tuple[str, str, str, str],
) -> bool:
    crs, benchmark, harness, trial_name = trial_identity
    return (
        rel_parts[-1] == trial_name
        and len(rel_parts) in {6, 7}
        and rel_parts[:3] == (crs, benchmark, harness)
    )


def _is_trial_root(path: pathlib.Path, rel_parts: tuple[str, ...]) -> bool:
    if not path.is_dir() or not _matches_layout(rel_parts):
        return False
    trial_identity = _load_trial_identity(path)
    if trial_identity is None:
        return (path / ".success").exists() or (path / ".fail").exists()
    return _matches_metadata(rel_parts, trial_identity=trial_identity)


def _scan(path: pathlib.Path, rel_parts: tuple[str, ...], out: list[str]) -> None:
    try:
        iterator = os.scandir(path)
    except OSError:
        return

    child_dirs: list[str] = []
    with iterator as entries:
        for entry in entries:
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                continue
            if is_dir:
                child_dirs.append(entry.name)

    for name in sorted(child_dirs):
        child = path / name
        child_rel_parts = (*rel_parts, name)
        if _is_trial_root(child, child_rel_parts):
            out.append("/".join(child_rel_parts))
            continue
        _scan(child, child_rel_parts, out)


root = pathlib.Path(sys.argv[1])
trial_dirs: list[str] = []
_scan(root, (), trial_dirs)
print(json.dumps(trial_dirs))
"""


_COPY_LINK_FILELIST_DISCOVERY_SCRIPT = r"""
import json
import pathlib
import sys


def _path_under(path: pathlib.PurePosixPath, prefix: pathlib.PurePosixPath) -> bool:
    prefix_parts = prefix.parts
    return path.parts[: len(prefix_parts)] == prefix_parts


def _excluded(
    logical_rel: pathlib.PurePosixPath,
    exclude_prefixes: list[pathlib.PurePosixPath],
) -> bool:
    return any(_path_under(logical_rel, prefix) for prefix in exclude_prefixes)


def _actual_under(path: pathlib.Path, prefix: pathlib.Path) -> bool:
    try:
        path.relative_to(prefix)
        return True
    except ValueError:
        return False


def _actual_excluded(
    actual: pathlib.Path,
    exclude_actual_prefixes: list[pathlib.Path],
) -> bool:
    try:
        resolved_actual = actual.resolve(strict=True)
    except OSError:
        return True
    return any(
        _actual_under(resolved_actual, prefix) for prefix in exclude_actual_prefixes
    )


def _walk(
    *,
    full_logical: pathlib.PurePosixPath,
    logical_rel: pathlib.PurePosixPath,
    actual: pathlib.Path,
    active_dirs: set[str],
    exclude_prefixes: list[pathlib.PurePosixPath],
    exclude_actual_prefixes: list[pathlib.Path],
    directories: list[str],
    files: list[str],
) -> None:
    if _excluded(logical_rel, exclude_prefixes) or _actual_excluded(
        actual, exclude_actual_prefixes
    ):
        return

    if actual.is_dir():
        real_dir = actual.resolve(strict=True)
        real_key = str(real_dir)
        if real_key in active_dirs:
            return
        next_active = set(active_dirs)
        next_active.add(real_key)
        directories.append(full_logical.as_posix())
        for child in sorted(actual.iterdir(), key=lambda item: item.name):
            child_full = full_logical / child.name
            child_rel = logical_rel / child.name
            if child.is_symlink():
                try:
                    child_target = child.resolve(strict=True)
                except OSError:
                    continue
                _walk(
                    full_logical=child_full,
                    logical_rel=child_rel,
                    actual=child_target,
                    active_dirs=next_active,
                    exclude_prefixes=exclude_prefixes,
                    exclude_actual_prefixes=exclude_actual_prefixes,
                    directories=directories,
                    files=files,
                )
                continue
            _walk(
                full_logical=child_full,
                logical_rel=child_rel,
                actual=child,
                active_dirs=next_active,
                exclude_prefixes=exclude_prefixes,
                exclude_actual_prefixes=exclude_actual_prefixes,
                directories=directories,
                files=files,
            )
        return

    files.append(full_logical.as_posix())


remote_root = pathlib.Path(sys.argv[1])
specs = json.loads(sys.argv[2])
directories: list[str] = []
files: list[str] = []

for spec in specs:
    root = pathlib.PurePosixPath(spec["root"])
    exclude_prefixes = [
        pathlib.PurePosixPath(prefix) for prefix in spec["exclude_prefixes"]
    ]
    exclude_actual_prefixes = []
    for prefix in spec.get("exclude_actual_prefixes", []):
        try:
            exclude_actual_prefixes.append(
                (remote_root / pathlib.PurePosixPath(prefix)).resolve(strict=True)
            )
        except OSError:
            continue
    try:
        actual_root = (remote_root / root).resolve(strict=True)
    except OSError as exc:
        raise SystemExit(f"failed to resolve {root.as_posix()}: {exc}")
    if actual_root.is_dir():
        _walk(
            full_logical=root,
            logical_rel=pathlib.PurePosixPath("."),
            actual=actual_root,
            active_dirs=set(),
            exclude_prefixes=exclude_prefixes,
            exclude_actual_prefixes=exclude_actual_prefixes,
            directories=directories,
            files=files,
        )
        continue
    if _actual_excluded(actual_root, exclude_actual_prefixes):
        continue
    files.append(root.as_posix())

print(
    json.dumps(
        {
            "directories": sorted(set(directories)),
            "files": sorted(set(files)),
        }
    )
)
"""


def collect_marker_path(destination: Path) -> Path:
    """Return the metadata marker path for *destination*."""
    return destination / _COLLECT_MARKER_FILENAME


def read_collect_marker(destination: Path) -> dict[str, object] | None:
    """Read and parse collect marker JSON payload from *destination*.

    Returns ``None`` when marker is missing or malformed.
    """
    marker_path = collect_marker_path(destination)
    if not marker_path.exists():
        return None

    try:
        payload = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def write_collect_marker(destination: Path, payload: dict[str, object]) -> None:
    """Persist *payload* to the hidden collect marker at *destination*."""
    marker_path = collect_marker_path(destination)
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=marker_path.parent,
            prefix=f"{marker_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            json.dump(payload, tmp)
            temp_path = Path(tmp.name)
        temp_path.replace(marker_path)
    except Exception:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        raise


def discover_experiment_start_time_from_staging(
    staging_dirs: list[Path],
) -> tuple[str | None, str]:
    """Discover the experiment start time and source from one or more staging trees."""
    timestamp_starts: list[tuple[datetime, str]] = []
    legacy_timestamps: list[tuple[datetime, str]] = []

    for staging_dir in staging_dirs:
        if not staging_dir.exists():
            continue
        for metadata_path in staging_dir.rglob("metadata.json"):
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(metadata, dict):
                continue

            timestamp_start = _parse_metadata_timestamp(metadata, "timestamp_start")
            if timestamp_start is not None:
                timestamp_starts.append(timestamp_start)
                continue

            legacy_timestamp = _parse_metadata_timestamp(metadata, "timestamp")
            if legacy_timestamp is not None:
                legacy_timestamps.append(legacy_timestamp)

    if timestamp_starts:
        _, start_time = min(timestamp_starts, key=lambda item: item[0])
        return start_time, "earliest_trial_timestamp_start"
    if legacy_timestamps:
        _, start_time = min(legacy_timestamps, key=lambda item: item[0])
        return start_time, "earliest_trial_timestamp"
    return None, "unknown"


def merge_experiment_start_time(
    current: tuple[str | None, str], prior: dict[str, object] | None
) -> tuple[str | None, str]:
    """Return prior experiment start time/source when current run has no start time."""
    if current[0] is not None:
        return current

    if not isinstance(prior, dict):
        return current

    prior_start_time = prior.get("experiment_start_time")
    prior_source = prior.get("experiment_start_time_source")
    if isinstance(prior_start_time, str) and isinstance(prior_source, str):
        return prior_start_time, prior_source
    return current


def _parse_metadata_timestamp(
    metadata: dict[str, object], key: str
) -> tuple[datetime, str] | None:
    """Parse a timestamp field from metadata JSON, returning ``(parsed_dt, raw)``."""
    raw = metadata.get(key)
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        if isinstance(raw, float) and not math.isfinite(raw):
            return None
        try:
            parsed = datetime.fromtimestamp(raw, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
        return parsed, parsed.isoformat()
    if isinstance(raw, str):
        normalized = raw.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    else:
        parsed = parsed.astimezone(timezone.utc)
    return parsed, raw


class ArtifactCollectionError(Exception):
    """Raised when artifact collection fails verification or publication."""


class ArtifactCollector:
    """Collect trial artifacts from a GCE worker via rsync.

    The collector is stateless; all parameters are passed directly to methods.
    """

    def __init__(
        self,
        *,
        base_path: Path | str | None = None,
        journal_lines: int = 2000,
        transport: CloudTransport | None = None,
    ) -> None:
        self._base_path = Path(base_path) if base_path is not None else None
        self._journal_lines = journal_lines
        self._ssh_users_by_project: dict[str, str] = {}
        self._ssh_user_lock = threading.Lock()
        self._iap_known_hosts_lock = threading.Lock()
        self._publish_lock = threading.Lock()
        self._transport = transport or transport_for_provider("gce")

    def collect(
        self,
        worker: CloudInstanceLike,
        fleet: SshTransportConfig,
        experiment_name: str,
        experiment_filestore: Path,
        remote_experiment_dir: str,
        start_time_observations: list[tuple[str | None, str]] | None = None,
        destination: Path | None = None,
    ) -> Path:
        """Rsync trial artifacts from *worker* and publish them to *experiment_filestore*.

        Args:
            worker: GCE worker instance record (provides name, zone, IP).
            fleet: Fleet configuration (provides project, ssh_via_iap).
            experiment_name: Name of the experiment (used to build the final path).
            experiment_filestore: Orchestrator-local directory where experiments live.
            remote_experiment_dir: Absolute path on the worker containing the experiment tree.

        Returns:
            Path to the final experiment directory (``experiment_filestore / experiment_name``).

        Raises:
            ArtifactCollectionError: If the staged tree fails verification or rsync fails.
        """
        known_hosts_path = self._prepare_ssh_access(
            worker=worker,
            fleet=fleet,
            experiment_filestore=experiment_filestore,
        )
        ssh_user = self._direct_ssh_user(fleet)
        staging_dir = (
            experiment_filestore / ".collect-staging" / worker.name / experiment_name
        )
        excluded_trial_staged_relpaths = self._discover_remote_trial_staged_relpaths(
            worker=worker,
            fleet=fleet,
            remote_experiment_dir=remote_experiment_dir,
            experiment_filestore=experiment_filestore,
            known_hosts_path=known_hosts_path,
            ssh_user=ssh_user,
        )

        # Clear any stale staging from a prior interrupted run
        if staging_dir.exists():
            logger.debug("Removing stale staging dir: {}", staging_dir)
            shutil.rmtree(staging_dir)
        staging_dir.mkdir(parents=True, exist_ok=True)

        self._run_artifact_rsync(
            worker=worker,
            fleet=fleet,
            remote_experiment_dir=remote_experiment_dir,
            staging_dir=staging_dir,
            experiment_filestore=experiment_filestore,
            known_hosts_path=known_hosts_path,
            ssh_user=ssh_user,
            excluded_relpaths=excluded_trial_staged_relpaths,
        )
        self._drop_trial_staged_dirs(staging_dir)
        rehydrate_relpaths, drop_relpaths = self._partition_excluded_symlink_entries(
            staging_dir,
            remote_experiment_dir=remote_experiment_dir,
        )
        if drop_relpaths:
            self._drop_excluded_symlink_entries(
                staging_dir=staging_dir,
                symlink_relpaths=drop_relpaths,
            )
        if rehydrate_relpaths:
            self._rehydrate_excluded_symlink_entries(
                worker=worker,
                fleet=fleet,
                remote_experiment_dir=remote_experiment_dir,
                staging_dir=staging_dir,
                experiment_filestore=experiment_filestore,
                known_hosts_path=known_hosts_path,
                ssh_user=ssh_user,
                symlink_relpaths=rehydrate_relpaths,
            )
        self._prune_staged_output_logs(staging_dir)
        report_log_output_relpaths = [
            output_dir.relative_to(staging_dir)
            for trial_dir in _iter_trial_dirs(staging_dir)
            for output_dir in [trial_dir / "output"]
            if output_dir.exists() or output_dir.is_symlink()
        ]
        self._rehydrate_report_logs_from_output_symlinks(
            worker=worker,
            fleet=fleet,
            remote_experiment_dir=remote_experiment_dir,
            staging_dir=staging_dir,
            experiment_filestore=experiment_filestore,
            known_hosts_path=known_hosts_path,
            ssh_user=ssh_user,
            symlink_relpaths=report_log_output_relpaths,
        )
        failed_trial_relpaths = self._compact_failed_trials_to_diagnostics(staging_dir)

        # Verify before publishing
        self._verify_staging(staging_dir)
        final_dir = destination or (experiment_filestore / experiment_name)
        with self._publish_lock:
            if start_time_observations is not None:
                start_time_observations.append(
                    discover_experiment_start_time_from_staging([staging_dir])
                )
            self._publish(
                staging_dir,
                final_dir,
                replace_trial_dirs=failed_trial_relpaths,
            )

        # Clean up the per-worker staging parent
        worker_staging = experiment_filestore / ".collect-staging" / worker.name
        if worker_staging.exists():
            shutil.rmtree(worker_staging, ignore_errors=True)

        logger.info(
            "Artifact collection complete: worker={} experiment={} final_dir={}",
            worker.name,
            experiment_name,
            final_dir,
        )
        return final_dir

    def collect_logs(
        self,
        worker: CloudInstanceLike,
        fleet: SshTransportConfig,
        experiment_name: str,
        experiment_filestore: Path,
        remote_experiment_dir: str,
    ) -> Path:
        """Collect remote service journals and in-progress trial logs for one VM."""
        logs_root = self._remote_logs_dir(experiment_filestore, experiment_name)
        instance_logs_dir = logs_root / worker.name
        instance_logs_dir.mkdir(parents=True, exist_ok=True)

        known_hosts_path = self._prepare_ssh_access(
            worker=worker,
            fleet=fleet,
            experiment_filestore=experiment_filestore,
        )
        ssh_user = self._direct_ssh_user(fleet)

        for destination, command in self._log_commands(worker).items():
            destination = logs_root / destination
            destination.parent.mkdir(parents=True, exist_ok=True)
            output = self._run_remote_command(
                worker=worker,
                fleet=fleet,
                known_hosts_path=known_hosts_path,
                ssh_user=ssh_user,
                command=command,
                experiment_filestore=experiment_filestore,
            )
            if output.returncode != 0:
                raise ArtifactCollectionError(
                    f"remote log command failed for {worker.name}: {command}"
                )
            combined = output.stdout
            if output.stderr:
                combined = f"{combined}\n{output.stderr}".strip() + "\n"
            destination.write_text(combined, encoding="utf-8")

        if self._remote_path_exists(
            worker=worker,
            fleet=fleet,
            known_hosts_path=known_hosts_path,
            ssh_user=ssh_user,
            remote_path=remote_experiment_dir,
            experiment_filestore=experiment_filestore,
        ):
            trial_artifacts_dir = instance_logs_dir / "trial-artifacts"
            trial_artifacts_staging = instance_logs_dir / ".trial-artifacts-staging"
            if trial_artifacts_staging.exists():
                shutil.rmtree(trial_artifacts_staging, ignore_errors=True)
            excluded_trial_staged_relpaths = (
                self._discover_remote_trial_staged_relpaths(
                    worker=worker,
                    fleet=fleet,
                    remote_experiment_dir=remote_experiment_dir,
                    experiment_filestore=experiment_filestore,
                    known_hosts_path=known_hosts_path,
                    ssh_user=ssh_user,
                )
            )
            self._run_log_rsync(
                worker=worker,
                fleet=fleet,
                remote_experiment_dir=remote_experiment_dir,
                staging_dir=trial_artifacts_staging,
                experiment_filestore=experiment_filestore,
                known_hosts_path=known_hosts_path,
                ssh_user=ssh_user,
                excluded_relpaths=excluded_trial_staged_relpaths,
            )
            self._drop_trial_staged_dirs(trial_artifacts_staging)
            self._drop_excluded_top_level_trial_symlinks(
                trial_artifacts_staging,
                remote_experiment_dir=remote_experiment_dir,
            )
            self._publish(trial_artifacts_staging, trial_artifacts_dir)

        logger.info(
            "Remote log collection complete: worker={} experiment={} logs_dir={}",
            worker.name,
            experiment_name,
            instance_logs_dir,
        )
        return logs_root

    def collect_reeval_submission_artifacts(
        self,
        *,
        worker: CloudInstanceLike,
        fleet: SshTransportConfig,
        experiment_name: str,
        experiment_filestore: Path,
        remote_submission_dir: str,
        destination: Path,
    ) -> Path:
        """Collect authoritative cloud re-eval wrapper artifacts from the orchestrator."""
        known_hosts_path = self._prepare_ssh_access(
            worker=worker,
            fleet=fleet,
            experiment_filestore=experiment_filestore,
        )
        ssh_user = self._direct_ssh_user(fleet)

        submission_state = self._read_remote_text_file(
            worker=worker,
            fleet=fleet,
            known_hosts_path=known_hosts_path,
            ssh_user=ssh_user,
            remote_path=posixpath.join(remote_submission_dir, "submission.json"),
            experiment_filestore=experiment_filestore,
        )
        try:
            submission_payload = json.loads(submission_state)
        except json.JSONDecodeError as exc:
            raise ArtifactCollectionError(
                "Remote cloud re-eval submission state is not valid JSON"
            ) from exc
        if not isinstance(submission_payload, dict):
            raise ArtifactCollectionError(
                "Remote cloud re-eval submission state is not a JSON object"
            )

        remote_state = submission_payload.get("state")
        if remote_state not in _REEVAL_TERMINAL_STATES:
            raise ArtifactCollectionError(
                f"Remote cloud re-eval submission for {experiment_name} is in "
                f"non-terminal state {remote_state!r}"
            )

        submission_dir = destination / _REEVAL_SUBMISSION_ARTIFACT_DIRNAME
        submission_dir.mkdir(parents=True, exist_ok=True)
        (submission_dir / "submission.json").write_text(
            submission_state,
            encoding="utf-8",
        )
        for remote_name, local_name in _REEVAL_REMOTE_TEXT_ARTIFACTS:
            payload = self._read_remote_text_file(
                worker=worker,
                fleet=fleet,
                known_hosts_path=known_hosts_path,
                ssh_user=ssh_user,
                remote_path=posixpath.join(remote_submission_dir, remote_name),
                experiment_filestore=experiment_filestore,
            )
            (submission_dir / local_name).write_text(payload, encoding="utf-8")

        logger.info(
            "Cloud re-eval submission artifact collection complete: "
            "worker={} experiment={} submission_dir={}",
            worker.name,
            experiment_name,
            submission_dir,
        )
        return submission_dir

    def _run_rsync_with_retry(self, cmd: list[str]) -> None:
        """Run the rsync command with exponential-backoff retry."""

        @tenacity.retry(
            retry=tenacity.retry_if_exception_type(subprocess.CalledProcessError),
            wait=tenacity.wait_exponential(multiplier=1, min=2, max=30),
            stop=tenacity.stop_after_attempt(3),
            reraise=True,
        )
        def _run() -> None:
            subprocess.run(cmd, check=True)

        _run()

    def _discover_copy_link_filelist(
        self,
        *,
        worker: CloudInstanceLike,
        fleet: SshTransportConfig,
        remote_experiment_dir: str,
        experiment_filestore: Path,
        known_hosts_path: Path | None,
        ssh_user: str | None,
        symlink_relpaths: list[Path],
        ssh_command: str | None = None,
        remote_host: str | None = None,
    ) -> tuple[list[Path], list[str]]:
        """Return cycle-safe rsync file-list entries for directory symlink rehydration."""
        specs = []
        for relpath in symlink_relpaths:
            exclude_actual_prefixes = [
                (relpath.parent / name).as_posix()
                for name in _DROP_EXCLUDED_TOPLEVEL_DIRS
            ]
            exclude_actual_prefixes.append(
                (relpath.parent / "output" / "logs").as_posix()
            )
            specs.append(
                {
                    "root": relpath.as_posix(),
                    "exclude_prefixes": ["logs"] if relpath.name == "output" else [],
                    "exclude_actual_prefixes": exclude_actual_prefixes,
                }
            )
        return self._discover_remote_filelist(
            worker=worker,
            fleet=fleet,
            remote_experiment_dir=remote_experiment_dir,
            experiment_filestore=experiment_filestore,
            known_hosts_path=known_hosts_path,
            ssh_user=ssh_user,
            specs=specs,
            ssh_command=ssh_command,
            remote_host=remote_host,
        )

    def _discover_remote_filelist(
        self,
        *,
        worker: CloudInstanceLike,
        fleet: SshTransportConfig,
        remote_experiment_dir: str,
        experiment_filestore: Path,
        known_hosts_path: Path | None,
        ssh_user: str | None,
        specs: list[dict[str, object]],
        ssh_command: str | None = None,
        remote_host: str | None = None,
    ) -> tuple[list[Path], list[str]]:
        """Run the remote manifest walker and return discovered directories/files."""
        command = self._build_remote_python_command(
            _COPY_LINK_FILELIST_DISCOVERY_SCRIPT,
            remote_experiment_dir,
            json.dumps(specs),
            use_sudo=True,
        )
        if ssh_command is not None and remote_host is not None:
            result = self._run_remote_command_via_ssh(
                ssh_command=ssh_command,
                remote_host=remote_host,
                ssh_user=ssh_user,
                command=command,
            )
        else:
            result = self._run_remote_command(
                worker=worker,
                fleet=fleet,
                known_hosts_path=known_hosts_path,
                ssh_user=ssh_user,
                command=command,
                experiment_filestore=experiment_filestore,
            )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
            raise ArtifactCollectionError(
                f"failed to enumerate dereferenced artifact paths from {worker.name}: {detail}"
            )

        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ArtifactCollectionError(
                "failed to parse dereferenced artifact file list from remote output"
            ) from exc

        directories_raw = payload.get("directories", [])
        files_raw = payload.get("files", [])
        if not isinstance(directories_raw, list) or not isinstance(files_raw, list):
            raise ArtifactCollectionError(
                "remote dereferenced artifact enumeration returned malformed payload"
            )

        directories = [
            Path(value) for value in directories_raw if isinstance(value, str) and value
        ]
        files = [value for value in files_raw if isinstance(value, str) and value]
        return directories, files

    def _discover_remote_trial_root_relpaths(
        self,
        *,
        worker: CloudInstanceLike,
        fleet: SshTransportConfig,
        remote_experiment_dir: str,
        experiment_filestore: Path,
        known_hosts_path: Path | None,
        ssh_user: str | None,
    ) -> list[Path]:
        """Return every real trial root under the remote experiment directory."""
        command = self._build_remote_python_command(
            _TRIAL_ROOT_DISCOVERY_SCRIPT,
            remote_experiment_dir,
            use_sudo=True,
        )
        result = self._run_remote_command(
            worker=worker,
            fleet=fleet,
            known_hosts_path=known_hosts_path,
            ssh_user=ssh_user,
            command=command,
            experiment_filestore=experiment_filestore,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
            raise ArtifactCollectionError(
                f"failed to enumerate remote trial roots from {worker.name}: {detail}"
            )

        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ArtifactCollectionError(
                "failed to parse remote trial-root discovery output"
            ) from exc

        if not isinstance(payload, list) or any(
            not isinstance(item, str) for item in payload
        ):
            raise ArtifactCollectionError(
                "remote trial-root discovery returned a malformed payload"
            )

        return [Path(item) for item in payload]

    def _discover_remote_trial_staged_relpaths(
        self,
        *,
        worker: CloudInstanceLike,
        fleet: SshTransportConfig,
        remote_experiment_dir: str,
        experiment_filestore: Path,
        known_hosts_path: Path | None,
        ssh_user: str | None,
    ) -> list[Path]:
        """Return exact ``trial-*/staged`` paths for real remote trial roots only."""
        return [
            trial_relpath / "staged"
            for trial_relpath in self._discover_remote_trial_root_relpaths(
                worker=worker,
                fleet=fleet,
                remote_experiment_dir=remote_experiment_dir,
                experiment_filestore=experiment_filestore,
                known_hosts_path=known_hosts_path,
                ssh_user=ssh_user,
            )
        ]

    def _discover_report_log_filelist(
        self,
        *,
        worker: CloudInstanceLike,
        fleet: SshTransportConfig,
        remote_experiment_dir: str,
        experiment_filestore: Path,
        known_hosts_path: Path | None,
        ssh_user: str | None,
        output_relpath: Path,
        ssh_command: str | None = None,
        remote_host: str | None = None,
    ) -> list[str]:
        """Return report-log file paths for one rehydrated top-level output symlink."""
        try:
            _, files = self._discover_remote_filelist(
                worker=worker,
                fleet=fleet,
                remote_experiment_dir=remote_experiment_dir,
                experiment_filestore=experiment_filestore,
                known_hosts_path=known_hosts_path,
                ssh_user=ssh_user,
                specs=[
                    {
                        "root": (output_relpath / "logs").as_posix(),
                        "exclude_prefixes": [],
                        "exclude_actual_prefixes": [
                            (output_relpath.parent / name).as_posix()
                            for name in _DROP_EXCLUDED_TOPLEVEL_DIRS
                        ],
                    }
                ],
                ssh_command=ssh_command,
                remote_host=remote_host,
            )
        except ArtifactCollectionError as exc:
            if "failed to resolve" in str(exc):
                return []
            raise
        return [
            file_relpath for file_relpath in files if _is_report_log_file(file_relpath)
        ]

    def _run_copy_link_filelist_rsync(
        self,
        *,
        worker: CloudInstanceLike,
        fleet: SshTransportConfig,
        remote_experiment_dir: str,
        destination_root: Path,
        experiment_filestore: Path,
        manifest_relpaths: list[str],
        known_hosts_path: Path | None = None,
        ssh_user: str | None = None,
        ssh_command: str | None = None,
        remote_host: str | None = None,
    ) -> None:
        """Run a copy-links rsync constrained to an explicit manifest of paths."""
        if not manifest_relpaths:
            return

        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                prefix="crsbench-copy-links-",
                suffix=".txt",
                dir=experiment_filestore,
                delete=False,
            ) as tmp:
                temp_path = Path(tmp.name)
                tmp.write("\n".join(manifest_relpaths))
                tmp.write("\n")

            cmd = self._build_copy_link_filelist_rsync_cmd(
                worker=worker,
                fleet=fleet,
                remote_experiment_dir=remote_experiment_dir,
                destination_root=destination_root,
                files_from_path=temp_path,
                known_hosts_path=known_hosts_path,
                ssh_user=ssh_user,
                ssh_command=ssh_command,
                remote_host=remote_host,
            )
            self._run_rsync_with_retry(cmd)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

    def _build_rsync_cmd(
        self,
        worker: CloudInstanceLike,
        fleet: SshTransportConfig,
        remote_experiment_dir: str,
        staging_dir: Path,
        known_hosts_path: Path | None = None,
        ssh_user: str | None = None,
        ssh_command: str | None = None,
        remote_host: str | None = None,
        excluded_relpaths: list[Path] | None = None,
    ) -> list[str]:
        """Return the full rsync command as a list of strings.

        Flags used:
        - ``-a``: archive mode (preserves mtimes, permissions, symlinks — ARTF-02)
        - ``--mkpath``: create destination dirs as needed (rsync 3.2+)
        - ``--partial-dir=.rsync-partial``: job-local partial dir (no cross-job collisions)
        - ``--delay-updates``: stage all files before renaming into place
        - ``--delete-delay``: remove remote-deleted files after transfer completes
        - ``--exclude=oss-crs-workdir/``: skip trial-local oss-crs scratch state
        - exact ``trial-*/staged`` excludes: skip only real trial-local staged copies
        - ``--exclude=output/logs/``: skip bulky trial-local CRS/compose logs
        """
        if (
            known_hosts_path is None
            and not fleet.ssh_via_iap
            and self._base_path is not None
        ):
            known_hosts_path = cloud_state_dir(self._base_path) / "known_hosts"
        ssh_cmd = ssh_command or self._build_ssh_command(
            worker, fleet, known_hosts_path
        )

        resolved_remote_host = remote_host or self._remote_host(worker, fleet)
        if ssh_user is not None and remote_host is None and not fleet.ssh_via_iap:
            resolved_remote_host = f"{ssh_user}@{resolved_remote_host}"

        source = f"{resolved_remote_host}:{remote_experiment_dir}/"
        dest = str(staging_dir) + "/"

        cmd = [
            "rsync",
            "-a",
            "--mkpath",
            "--partial-dir=.rsync-partial",
            "--delay-updates",
            "--delete-delay",
        ]
        cmd.extend(f"--exclude={pattern}" for pattern in _ARTIFACT_RSYNC_EXCLUDES)
        cmd.extend(
            f"--exclude={pattern}"
            for pattern in _rsync_exact_path_excludes(excluded_relpaths or [])
        )
        cmd.extend(
            [
                "--rsync-path=sudo rsync",
                "-e",
                ssh_cmd,
                source,
                dest,
            ]
        )
        return cmd

    def _build_copy_link_filelist_rsync_cmd(
        self,
        worker: CloudInstanceLike,
        fleet: SshTransportConfig,
        remote_experiment_dir: str,
        destination_root: Path,
        files_from_path: Path,
        known_hosts_path: Path | None = None,
        ssh_user: str | None = None,
        ssh_command: str | None = None,
        remote_host: str | None = None,
    ) -> list[str]:
        """Return a cycle-safe rsync command that dereferences explicit source paths."""
        if (
            known_hosts_path is None
            and not fleet.ssh_via_iap
            and self._base_path is not None
        ):
            known_hosts_path = cloud_state_dir(self._base_path) / "known_hosts"
        ssh_cmd = ssh_command or self._build_ssh_command(
            worker, fleet, known_hosts_path
        )
        resolved_remote_host = remote_host or self._remote_host(worker, fleet)
        if ssh_user is not None and remote_host is None and not fleet.ssh_via_iap:
            resolved_remote_host = f"{ssh_user}@{resolved_remote_host}"

        remote_root = remote_experiment_dir.rstrip("/") or "/"
        source = f"{resolved_remote_host}:{remote_root}/"
        dest = str(destination_root) + "/"

        return [
            "rsync",
            "-a",
            "--mkpath",
            "--copy-links",
            "--ignore-missing-args",
            f"--files-from={files_from_path}",
            "--rsync-path=sudo rsync",
            "-e",
            ssh_cmd,
            source,
            dest,
        ]

    def _build_log_rsync_cmd(
        self,
        worker: CloudInstanceLike,
        fleet: SshTransportConfig,
        remote_experiment_dir: str,
        staging_dir: Path,
        known_hosts_path: Path | None = None,
        ssh_user: str | None = None,
        ssh_command: str | None = None,
        remote_host: str | None = None,
        excluded_relpaths: list[Path] | None = None,
    ) -> list[str]:
        """Return an rsync command that only copies lightweight trial-observability files."""
        if (
            known_hosts_path is None
            and not fleet.ssh_via_iap
            and self._base_path is not None
        ):
            known_hosts_path = cloud_state_dir(self._base_path) / "known_hosts"
        ssh_cmd = ssh_command or self._build_ssh_command(
            worker, fleet, known_hosts_path
        )
        resolved_remote_host = remote_host or self._remote_host(worker, fleet)
        if ssh_user is not None and remote_host is None and not fleet.ssh_via_iap:
            resolved_remote_host = f"{ssh_user}@{resolved_remote_host}"

        source = f"{resolved_remote_host}:{remote_experiment_dir}/"
        dest = str(staging_dir) + "/"

        return [
            "rsync",
            "-a",
            "--mkpath",
            "--prune-empty-dirs",
            *[f"--exclude={pattern}" for pattern in _LOG_RSYNC_EXCLUDES],
            *[
                f"--exclude={pattern}"
                for pattern in _rsync_exact_path_excludes(excluded_relpaths or [])
            ],
            "--include=*/",
            "--include=trial_matrix.json",
            "--include=metadata.json",
            "--include=worker.log",
            "--include=.success",
            "--include=.fail",
            "--exclude=*",
            "--rsync-path=sudo rsync",
            "-e",
            ssh_cmd,
            source,
            dest,
        ]

    @staticmethod
    def _build_remote_python_command(
        script: str,
        *args: str,
        use_sudo: bool = False,
    ) -> str:
        """Return a quoted remote shell command that runs one inline Python script."""
        cmd = f"python3 -c {shlex.quote(script)}"
        if args:
            cmd += " " + " ".join(shlex.quote(arg) for arg in args)
        if use_sudo:
            cmd = f"sudo {cmd}"
        return cmd

    def _build_report_log_rsync_cmd(
        self,
        worker: CloudInstanceLike,
        fleet: SshTransportConfig,
        remote_experiment_dir: str,
        staging_dir: Path,
        known_hosts_path: Path | None = None,
        ssh_user: str | None = None,
        ssh_command: str | None = None,
        remote_host: str | None = None,
        excluded_relpaths: list[Path] | None = None,
    ) -> list[str]:
        """Return an rsync command that keeps only report-critical trial logs."""
        if (
            known_hosts_path is None
            and not fleet.ssh_via_iap
            and self._base_path is not None
        ):
            known_hosts_path = cloud_state_dir(self._base_path) / "known_hosts"
        ssh_cmd = ssh_command or self._build_ssh_command(
            worker, fleet, known_hosts_path
        )
        resolved_remote_host = remote_host or self._remote_host(worker, fleet)
        if ssh_user is not None and remote_host is None and not fleet.ssh_via_iap:
            resolved_remote_host = f"{ssh_user}@{resolved_remote_host}"

        source = f"{resolved_remote_host}:{remote_experiment_dir}/"
        dest = str(staging_dir) + "/"

        cmd = [
            "rsync",
            "-a",
            "--mkpath",
            "--prune-empty-dirs",
        ]
        cmd.extend(f"--exclude={pattern}" for pattern in _REPORT_LOG_RSYNC_EXCLUDES)
        cmd.extend(
            f"--exclude={pattern}"
            for pattern in _rsync_exact_path_excludes(excluded_relpaths or [])
        )
        cmd.append("--include=*/")
        cmd.extend(f"--include={pattern}" for pattern in _REPORT_LOG_RSYNC_INCLUDES)
        cmd.extend(
            [
                "--exclude=*",
                "--rsync-path=sudo rsync",
                "-e",
                ssh_cmd,
                source,
                dest,
            ]
        )
        return cmd

    def _build_ssh_command(
        self,
        worker: CloudInstanceLike,
        fleet: SshTransportConfig,
        known_hosts_path: Path | None = None,
    ) -> str:
        """Return the ``-e`` argument string for direct-IP rsync SSH transport."""
        del worker
        return self._transport.build_rsync_ssh_command(
            project=fleet.project,
            ssh_via_iap=False,
            known_hosts_path=known_hosts_path,
        )

    def _build_iap_tunnel_command(
        self,
        *,
        worker: CloudInstanceLike,
        fleet: SshTransportConfig,
        local_port: int,
    ) -> list[str]:
        """Return the provider command that opens a local IAP tunnel to remote SSH."""
        zone = worker.zone or fleet.zone or ""
        return self._transport.build_iap_tunnel_command(
            instance_name=worker.name,
            project=fleet.project,
            zone=zone,
            local_port=local_port,
            remote_port=_IAP_TUNNEL_PORT,
        )

    def _build_iap_ssh_command(
        self,
        *,
        local_port: int,
        ssh_user: str,
        known_hosts_path: Path,
        host_key_alias: str,
    ) -> str:
        """Return a localhost-targeted ssh command for rsync over an active IAP tunnel."""
        return self._transport.build_rsync_ssh_command(
            project="",
            ssh_via_iap=True,
            known_hosts_path=known_hosts_path,
            ssh_user=ssh_user,
            local_port=local_port,
            host_key_alias=host_key_alias,
        )

    def _state_dir(self, experiment_filestore: Path) -> Path:
        """Return the local state directory used for SSH trust and log capture."""
        if self._base_path is not None:
            return cloud_state_dir(self._base_path)
        return experiment_filestore / ".crsbench-cloud"

    def _remote_logs_dir(
        self, experiment_filestore: Path, experiment_name: str
    ) -> Path:
        """Return the experiment-specific local remote-log directory."""
        if self._base_path is not None:
            return remote_logs_dir(self._base_path, experiment_name)
        return self._state_dir(experiment_filestore) / "remote-logs" / experiment_name

    def _known_hosts_path(
        self,
        experiment_filestore: Path,
        *,
        worker_name: str | None = None,
    ) -> Path:
        """Return the local known_hosts file used for direct-IP collection."""
        base_path = self._state_dir(experiment_filestore) / "known_hosts"
        if worker_name:
            return base_path / worker_name
        return base_path

    def _iap_known_hosts_path(
        self,
        experiment_filestore: Path,
        *,
        host_key_alias: str | None = None,
    ) -> Path:
        """Return the localhost tunnel known_hosts file used for IAP rsync."""
        base_path = self._state_dir(experiment_filestore) / "known_hosts_iap"
        del host_key_alias
        return base_path

    def _prepare_iap_known_hosts(
        self,
        *,
        experiment_filestore: Path,
        host_key_alias: str,
    ) -> Path:
        """Clear any stale stable-name host key before reconnecting over an IAP SSH tunnel."""
        known_hosts_path = self._iap_known_hosts_path(
            experiment_filestore,
            host_key_alias=host_key_alias,
        )
        known_hosts_path.parent.mkdir(parents=True, exist_ok=True)
        with self._iap_known_hosts_lock:
            return self._transport.prepare_iap_known_hosts(
                known_hosts_path=known_hosts_path,
                host_key_alias=host_key_alias,
            )

    def _remote_host(
        self,
        worker: CloudInstanceLike,
        fleet: SshTransportConfig,
    ) -> str:
        """Return the SSH/rsync host token for one worker."""
        if fleet.ssh_via_iap:
            return worker.name
        return worker.external_ip or worker.internal_ip or worker.name

    def _prepare_ssh_access(
        self,
        *,
        worker: CloudInstanceLike,
        fleet: SshTransportConfig,
        experiment_filestore: Path,
    ) -> Path | None:
        """Seed host trust for direct-IP SSH transport and return the known_hosts path."""
        if fleet.ssh_via_iap or self._base_path is None:
            return None

        known_hosts_path = self._known_hosts_path(
            experiment_filestore,
            worker_name=worker.name,
        )
        known_hosts_path.parent.mkdir(parents=True, exist_ok=True)
        remote_host = self._remote_host(worker, fleet)
        try:
            return self._transport.prepare_known_hosts(
                known_hosts_path=known_hosts_path,
                remote_host=remote_host,
            )
        except RuntimeError as exc:
            raise ArtifactCollectionError(str(exc)) from exc

    @contextmanager
    def _open_iap_tunnel(
        self,
        *,
        worker: CloudInstanceLike,
        fleet: SshTransportConfig,
    ) -> Iterator[int]:
        """Open a temporary local TCP tunnel to a worker's SSH port through IAP."""
        local_port = allocate_local_port()
        cmd = self._build_iap_tunnel_command(
            worker=worker,
            fleet=fleet,
            local_port=local_port,
        )
        try:
            process = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
            )
        except OSError as exc:
            raise ArtifactCollectionError(
                f"failed to start IAP tunnel for {worker.name}: {exc}"
            ) from exc

        try:
            wait_for_local_port(
                "127.0.0.1",
                local_port,
                timeout=_IAP_TUNNEL_STARTUP_TIMEOUT_SEC,
                process=process,
                process_label=f"IAP tunnel for {worker.name}",
            )
            yield local_port
        except Exception as exc:
            raise ArtifactCollectionError(
                f"failed to establish IAP tunnel for {worker.name}: {exc}"
            ) from exc
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5.0)

    def _run_artifact_rsync(
        self,
        *,
        worker: CloudInstanceLike,
        fleet: SshTransportConfig,
        remote_experiment_dir: str,
        staging_dir: Path,
        experiment_filestore: Path,
        known_hosts_path: Path | None,
        ssh_user: str | None,
        excluded_relpaths: list[Path] | None = None,
    ) -> None:
        """Run the full artifact rsync for one worker using the right transport."""
        if fleet.ssh_via_iap:
            if not ssh_user:
                raise ArtifactCollectionError(
                    f"Unable to resolve SSH user for IAP collection from {worker.name}"
                )
            iap_known_hosts_path = self._prepare_iap_known_hosts(
                experiment_filestore=experiment_filestore,
                host_key_alias=worker.name,
            )
            with self._open_iap_tunnel(worker=worker, fleet=fleet) as local_port:
                cmd = self._build_rsync_cmd(
                    worker=worker,
                    fleet=fleet,
                    remote_experiment_dir=remote_experiment_dir,
                    staging_dir=staging_dir,
                    ssh_command=self._build_iap_ssh_command(
                        local_port=local_port,
                        ssh_user=ssh_user,
                        known_hosts_path=iap_known_hosts_path,
                        host_key_alias=worker.name,
                    ),
                    remote_host="127.0.0.1",
                    excluded_relpaths=excluded_relpaths,
                )
                self._run_rsync_with_retry(cmd)
            return

        cmd = self._build_rsync_cmd(
            worker=worker,
            fleet=fleet,
            remote_experiment_dir=remote_experiment_dir,
            staging_dir=staging_dir,
            known_hosts_path=known_hosts_path,
            ssh_user=ssh_user,
            excluded_relpaths=excluded_relpaths,
        )
        self._run_rsync_with_retry(cmd)

    def _run_log_rsync(
        self,
        *,
        worker: CloudInstanceLike,
        fleet: SshTransportConfig,
        remote_experiment_dir: str,
        staging_dir: Path,
        experiment_filestore: Path,
        known_hosts_path: Path | None,
        ssh_user: str | None,
        excluded_relpaths: list[Path] | None = None,
    ) -> None:
        """Run the observability-only rsync for one worker using the right transport."""
        if fleet.ssh_via_iap:
            if not ssh_user:
                raise ArtifactCollectionError(
                    f"Unable to resolve SSH user for IAP log collection from {worker.name}"
                )
            iap_known_hosts_path = self._prepare_iap_known_hosts(
                experiment_filestore=experiment_filestore,
                host_key_alias=worker.name,
            )
            with self._open_iap_tunnel(worker=worker, fleet=fleet) as local_port:
                cmd = self._build_log_rsync_cmd(
                    worker=worker,
                    fleet=fleet,
                    remote_experiment_dir=remote_experiment_dir,
                    staging_dir=staging_dir,
                    ssh_command=self._build_iap_ssh_command(
                        local_port=local_port,
                        ssh_user=ssh_user,
                        known_hosts_path=iap_known_hosts_path,
                        host_key_alias=worker.name,
                    ),
                    remote_host="127.0.0.1",
                    excluded_relpaths=excluded_relpaths,
                )
                self._run_rsync_with_retry(cmd)
            return

        cmd = self._build_log_rsync_cmd(
            worker=worker,
            fleet=fleet,
            remote_experiment_dir=remote_experiment_dir,
            staging_dir=staging_dir,
            known_hosts_path=known_hosts_path,
            ssh_user=ssh_user,
            excluded_relpaths=excluded_relpaths,
        )
        self._run_rsync_with_retry(cmd)

    def _run_report_log_rsync(
        self,
        *,
        worker: CloudInstanceLike,
        fleet: SshTransportConfig,
        remote_experiment_dir: str,
        staging_dir: Path,
        experiment_filestore: Path,
        known_hosts_path: Path | None,
        ssh_user: str | None,
        excluded_relpaths: list[Path] | None = None,
    ) -> None:
        """Sync the minimal trial log subset needed by report generation."""
        if fleet.ssh_via_iap:
            if not ssh_user:
                raise ArtifactCollectionError(
                    f"Unable to resolve SSH user for IAP report-log collection from {worker.name}"
                )
            iap_known_hosts_path = self._prepare_iap_known_hosts(
                experiment_filestore=experiment_filestore,
                host_key_alias=worker.name,
            )
            with self._open_iap_tunnel(worker=worker, fleet=fleet) as local_port:
                cmd = self._build_report_log_rsync_cmd(
                    worker=worker,
                    fleet=fleet,
                    remote_experiment_dir=remote_experiment_dir,
                    staging_dir=staging_dir,
                    ssh_command=self._build_iap_ssh_command(
                        local_port=local_port,
                        ssh_user=ssh_user,
                        known_hosts_path=iap_known_hosts_path,
                        host_key_alias=worker.name,
                    ),
                    remote_host="127.0.0.1",
                    excluded_relpaths=excluded_relpaths,
                )
                self._run_rsync_with_retry(cmd)
            return

        cmd = self._build_report_log_rsync_cmd(
            worker=worker,
            fleet=fleet,
            remote_experiment_dir=remote_experiment_dir,
            staging_dir=staging_dir,
            known_hosts_path=known_hosts_path,
            ssh_user=ssh_user,
            excluded_relpaths=excluded_relpaths,
        )
        self._run_rsync_with_retry(cmd)

    def _rehydrate_report_logs_from_output_symlinks(
        self,
        *,
        worker: CloudInstanceLike,
        fleet: SshTransportConfig,
        remote_experiment_dir: str,
        staging_dir: Path,
        experiment_filestore: Path,
        known_hosts_path: Path | None,
        ssh_user: str | None,
        symlink_relpaths: list[Path],
    ) -> None:
        """Restore report-critical logs for top-level output symlinks rehydrated earlier."""
        output_relpaths = [
            relpath for relpath in symlink_relpaths if relpath.name == "output"
        ]
        if not output_relpaths:
            return

        if fleet.ssh_via_iap:
            if not ssh_user:
                raise ArtifactCollectionError(
                    f"Unable to resolve SSH user for IAP report-log rehydration from {worker.name}"
                )
            iap_known_hosts_path = self._prepare_iap_known_hosts(
                experiment_filestore=experiment_filestore,
                host_key_alias=worker.name,
            )
            with self._open_iap_tunnel(worker=worker, fleet=fleet) as local_port:
                ssh_command = self._build_iap_ssh_command(
                    local_port=local_port,
                    ssh_user=ssh_user,
                    known_hosts_path=iap_known_hosts_path,
                    host_key_alias=worker.name,
                )
                self._rehydrate_report_logs_from_output_symlinks_via_rsync(
                    worker=worker,
                    fleet=fleet,
                    remote_experiment_dir=remote_experiment_dir,
                    staging_dir=staging_dir,
                    experiment_filestore=experiment_filestore,
                    known_hosts_path=iap_known_hosts_path,
                    ssh_user=ssh_user,
                    output_relpaths=output_relpaths,
                    ssh_command=ssh_command,
                    remote_host="127.0.0.1",
                )
            return

        self._rehydrate_report_logs_from_output_symlinks_via_rsync(
            worker=worker,
            fleet=fleet,
            remote_experiment_dir=remote_experiment_dir,
            staging_dir=staging_dir,
            experiment_filestore=experiment_filestore,
            known_hosts_path=known_hosts_path,
            ssh_user=ssh_user,
            output_relpaths=output_relpaths,
        )

    def _rehydrate_report_logs_from_output_symlinks_via_rsync(
        self,
        *,
        worker: CloudInstanceLike,
        fleet: SshTransportConfig,
        remote_experiment_dir: str,
        staging_dir: Path,
        experiment_filestore: Path,
        known_hosts_path: Path | None,
        ssh_user: str | None,
        output_relpaths: list[Path],
        ssh_command: str | None = None,
        remote_host: str | None = None,
    ) -> None:
        """Restore report logs for top-level output symlinks using an explicit file manifest."""
        for output_relpath in output_relpaths:
            file_relpaths = self._discover_report_log_filelist(
                worker=worker,
                fleet=fleet,
                remote_experiment_dir=remote_experiment_dir,
                experiment_filestore=experiment_filestore,
                known_hosts_path=known_hosts_path,
                ssh_user=ssh_user,
                output_relpath=output_relpath,
                ssh_command=ssh_command,
                remote_host=remote_host,
            )
            if not file_relpaths:
                continue
            self._run_copy_link_filelist_rsync(
                worker=worker,
                fleet=fleet,
                remote_experiment_dir=remote_experiment_dir,
                destination_root=staging_dir,
                experiment_filestore=experiment_filestore,
                manifest_relpaths=file_relpaths,
                known_hosts_path=known_hosts_path,
                ssh_user=ssh_user,
                ssh_command=ssh_command,
                remote_host=remote_host,
            )
            self._verify_rehydrated_copy_link_manifest(
                staging_dir=staging_dir,
                symlink_relpath=output_relpath / "logs",
                directory_relpaths=[],
                file_relpaths=file_relpaths,
            )

    @staticmethod
    def _trial_symlink_target_parts(
        *,
        item: Path,
        trial_dir: Path,
        staging_dir: Path,
        remote_experiment_dir: str | None,
    ) -> tuple[str, ...] | None:
        """Return one symlink target as parts relative to *trial_dir*."""
        try:
            raw_target = item.readlink()
        except OSError:
            return None

        if raw_target.is_absolute():
            try:
                return item.resolve(strict=False).relative_to(trial_dir).parts
            except ValueError:
                pass
            if raw_target.exists():
                trial_parts = trial_dir.relative_to(staging_dir).parts
                target_parts = raw_target.parts
                for index in range(len(target_parts) - len(trial_parts) + 1):
                    if (
                        tuple(target_parts[index : index + len(trial_parts)])
                        != trial_parts
                    ):
                        continue
                    return tuple(target_parts[index + len(trial_parts) :])
            if remote_experiment_dir is None:
                return None
            trial_relpath = trial_dir.relative_to(staging_dir)
            try:
                return (
                    raw_target.relative_to(Path(remote_experiment_dir))
                    .relative_to(trial_relpath)
                    .parts
                )
            except ValueError:
                return None

        base_relpath = item.parent.relative_to(trial_dir)
        normalized = posixpath.normpath((base_relpath / raw_target).as_posix())
        if normalized in {"", "."}:
            return ()
        target_parts = Path(normalized).parts
        if target_parts and target_parts[0] == "..":
            return None
        return target_parts

    def _partition_excluded_symlink_entries(
        self,
        staging_dir: Path,
        *,
        remote_experiment_dir: str,
    ) -> tuple[list[Path], list[Path]]:
        """Return excluded top-level symlink entries split by rehydrate vs drop."""
        rehydrate_relpaths: list[Path] = []
        drop_relpaths: list[Path] = []

        for trial_dir in _iter_trial_dirs(staging_dir):
            for item in sorted(trial_dir.iterdir()):
                if not item.is_symlink():
                    continue
                target_parts = self._trial_symlink_target_parts(
                    item=item,
                    trial_dir=trial_dir,
                    staging_dir=staging_dir,
                    remote_experiment_dir=remote_experiment_dir,
                )
                if target_parts is None:
                    continue
                relpath = item.relative_to(staging_dir)
                if not target_parts:
                    continue
                top_level_target = target_parts[0]
                if top_level_target in _REHYDRATE_EXCLUDED_TOPLEVEL_DIRS:
                    rehydrate_relpaths.append(relpath)
                    continue
                if top_level_target in _DROP_EXCLUDED_TOPLEVEL_DIRS:
                    drop_relpaths.append(relpath)

        return rehydrate_relpaths, drop_relpaths

    @staticmethod
    def _remove_staged_path(path: Path) -> None:
        """Remove one staged file, dir, or symlink prior to rehydration."""
        if path.is_symlink() or path.is_file():
            path.unlink(missing_ok=True)
            return
        if path.is_dir():
            shutil.rmtree(path)

    def _prune_staged_output_logs(self, staging_dir: Path) -> None:
        """Drop any staged trial output/logs tree before restoring the keep-set."""
        for trial_dir in _iter_trial_dirs(staging_dir):
            logs_path = trial_dir / "output" / "logs"
            if logs_path.exists() or logs_path.is_symlink():
                self._remove_staged_path(logs_path)

    def _drop_trial_staged_dirs(self, staging_dir: Path) -> None:
        """Remove real trial-root ``staged`` dirs after collection as a backstop."""
        removed = 0
        for trial_dir in _iter_trial_dirs(staging_dir):
            staged_path = trial_dir / "staged"
            if not (staged_path.exists() or staged_path.is_symlink()):
                continue
            self._remove_staged_path(staged_path)
            removed += 1
        if removed:
            logger.info(
                "Dropped {} staged trial dir(s) from collected snapshots",
                removed,
            )

    def _drop_excluded_symlink_entries(
        self,
        *,
        staging_dir: Path,
        symlink_relpaths: list[Path],
    ) -> None:
        """Remove staged symlinks whose excluded targets should stay omitted."""
        logger.info(
            "Dropping {} staged symlinked artifact paths that resolve into omitted content",
            len(symlink_relpaths),
        )
        for relpath in symlink_relpaths:
            self._remove_staged_path(staging_dir / relpath)

    def _drop_excluded_top_level_trial_symlinks(
        self, staging_dir: Path, *, remote_experiment_dir: str
    ) -> None:
        """Remove top-level trial symlinks that resolve into omitted content."""
        removed = 0
        for trial_dir in _iter_trial_dirs(staging_dir):
            for item in sorted(trial_dir.iterdir()):
                if not item.is_symlink():
                    continue
                target_parts = self._trial_symlink_target_parts(
                    item=item,
                    trial_dir=trial_dir,
                    staging_dir=staging_dir,
                    remote_experiment_dir=remote_experiment_dir,
                )
                if (
                    not target_parts
                    or target_parts[0] not in _DROP_EXCLUDED_TOPLEVEL_DIRS
                ):
                    continue
                self._remove_staged_path(item)
                removed += 1
        if removed:
            logger.info(
                "Dropped {} top-level staged symlink(s) from collected trial snapshots",
                removed,
            )

    def _drop_excluded_report_log_symlinks(
        self, staging_dir: Path, *, remote_experiment_dir: str
    ) -> None:
        """Remove restored report-log symlinks that still resolve into omitted content."""
        removed = 0
        for trial_dir in _iter_trial_dirs(staging_dir):
            logs_root = trial_dir / "output" / "logs"
            if not logs_root.is_dir():
                continue
            for item in sorted(logs_root.rglob("*")):
                if not item.is_symlink():
                    continue
                target_parts = self._trial_symlink_target_parts(
                    item=item,
                    trial_dir=trial_dir,
                    staging_dir=staging_dir,
                    remote_experiment_dir=remote_experiment_dir,
                )
                if not target_parts:
                    continue
                if target_parts[0] in _DROP_EXCLUDED_TOPLEVEL_DIRS:
                    self._remove_staged_path(item)
                    removed += 1
        if removed:
            logger.info(
                "Dropped {} restored report-log symlink(s) that resolve into omitted content",
                removed,
            )

    def _compact_failed_trials_to_diagnostics(self, staging_dir: Path) -> list[Path]:
        """Reduce failed trials to marker/metadata/log diagnostics before publish."""
        compacted_trials: list[Path] = []

        for trial_dir in _iter_trial_dirs(staging_dir):
            if not self._is_failed_trial_dir(trial_dir):
                continue

            compacted_trials.append(trial_dir.relative_to(staging_dir))
            for item in sorted(trial_dir.iterdir()):
                if item.name in _FAILED_TRIAL_ROOT_KEEP_FILENAMES:
                    continue
                if item.name == "output":
                    self._compact_failed_trial_output(
                        trial_dir=trial_dir, output_dir=item
                    )
                    continue
                self._remove_staged_path(item)

        if compacted_trials:
            logger.info(
                "Compacted {} failed trial(s) to diagnostic-only artifacts",
                len(compacted_trials),
            )

        return compacted_trials

    @staticmethod
    def _is_failed_trial_dir(trial_dir: Path) -> bool:
        """Return whether *trial_dir* represents a failed trial collection target."""
        return (trial_dir / ".fail").exists() and not (trial_dir / ".success").exists()

    def _compact_failed_trial_output(
        self, *, trial_dir: Path, output_dir: Path
    ) -> None:
        """Preserve only the restored reporting-log subset under ``output/logs``."""
        if not (output_dir.exists() or output_dir.is_symlink()):
            return

        logs_path = output_dir / "logs"
        if not logs_path.exists():
            self._remove_staged_path(output_dir)
            return

        with tempfile.TemporaryDirectory(
            dir=trial_dir,
            prefix=".failed-trial-logs-",
        ) as temp_root:
            temp_logs_dir = Path(temp_root) / "logs"
            shutil.copytree(logs_path, temp_logs_dir)
            self._remove_staged_path(output_dir)

            restored_logs_dir = trial_dir / "output" / "logs"
            restored_logs_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(temp_logs_dir, restored_logs_dir)

    def _rehydrate_excluded_symlink_entries(
        self,
        *,
        worker: CloudInstanceLike,
        fleet: SshTransportConfig,
        remote_experiment_dir: str,
        staging_dir: Path,
        experiment_filestore: Path,
        known_hosts_path: Path | None,
        ssh_user: str | None,
        symlink_relpaths: list[Path],
    ) -> None:
        """Replace staged symlinks into excluded dirs with copied remote referents."""
        logger.info(
            "Rehydrating {} staged symlinked artifact paths excluded from main rsync",
            len(symlink_relpaths),
        )

        if fleet.ssh_via_iap:
            if not ssh_user:
                raise ArtifactCollectionError(
                    f"Unable to resolve SSH user for IAP collection from {worker.name}"
                )
            iap_known_hosts_path = self._prepare_iap_known_hosts(
                experiment_filestore=experiment_filestore,
                host_key_alias=worker.name,
            )
            with self._open_iap_tunnel(worker=worker, fleet=fleet) as local_port:
                ssh_command = self._build_iap_ssh_command(
                    local_port=local_port,
                    ssh_user=ssh_user,
                    known_hosts_path=iap_known_hosts_path,
                    host_key_alias=worker.name,
                )
                self._rehydrate_excluded_symlink_entries_via_rsync(
                    worker=worker,
                    fleet=fleet,
                    remote_experiment_dir=remote_experiment_dir,
                    staging_dir=staging_dir,
                    experiment_filestore=experiment_filestore,
                    symlink_relpaths=symlink_relpaths,
                    ssh_command=ssh_command,
                    remote_host="127.0.0.1",
                )
            return

        self._rehydrate_excluded_symlink_entries_via_rsync(
            worker=worker,
            fleet=fleet,
            remote_experiment_dir=remote_experiment_dir,
            staging_dir=staging_dir,
            experiment_filestore=experiment_filestore,
            symlink_relpaths=symlink_relpaths,
            known_hosts_path=known_hosts_path,
            ssh_user=ssh_user,
        )

    def _rehydrate_excluded_symlink_entries_via_rsync(
        self,
        *,
        worker: CloudInstanceLike,
        fleet: SshTransportConfig,
        remote_experiment_dir: str,
        staging_dir: Path,
        experiment_filestore: Path,
        symlink_relpaths: list[Path],
        known_hosts_path: Path | None = None,
        ssh_user: str | None = None,
        ssh_command: str | None = None,
        remote_host: str | None = None,
    ) -> None:
        """Run targeted rsync --copy-links transfers for staged excluded-dir symlinks."""
        for relpath in symlink_relpaths:
            local_path = staging_dir / relpath
            logger.debug("Rehydrating excluded symlink path: {}", local_path)
            directory_relpaths, file_relpaths = self._discover_copy_link_filelist(
                worker=worker,
                fleet=fleet,
                remote_experiment_dir=remote_experiment_dir,
                experiment_filestore=experiment_filestore,
                known_hosts_path=known_hosts_path,
                ssh_user=ssh_user,
                symlink_relpaths=[relpath],
                ssh_command=ssh_command,
                remote_host=remote_host,
            )
            manifest_relpaths = list(
                dict.fromkeys(
                    [
                        directory_relpath.as_posix()
                        for directory_relpath in directory_relpaths
                    ]
                    + file_relpaths
                )
            )
            self._remove_staged_path(local_path)
            local_path.parent.mkdir(parents=True, exist_ok=True)
            self._run_copy_link_filelist_rsync(
                worker=worker,
                fleet=fleet,
                remote_experiment_dir=remote_experiment_dir,
                destination_root=staging_dir,
                experiment_filestore=experiment_filestore,
                manifest_relpaths=manifest_relpaths,
                known_hosts_path=known_hosts_path,
                ssh_user=ssh_user,
                ssh_command=ssh_command,
                remote_host=remote_host,
            )
            self._verify_rehydrated_copy_link_manifest(
                staging_dir=staging_dir,
                symlink_relpath=relpath,
                directory_relpaths=directory_relpaths,
                file_relpaths=file_relpaths,
            )

    @staticmethod
    def _verify_rehydrated_copy_link_manifest(
        *,
        staging_dir: Path,
        symlink_relpath: Path,
        directory_relpaths: list[Path],
        file_relpaths: list[str],
    ) -> None:
        """Ensure manifest-discovered paths were materialized locally after rehydration."""
        missing_directories = [
            directory_relpath.as_posix()
            for directory_relpath in directory_relpaths
            if not (staging_dir / directory_relpath).is_dir()
        ]
        missing_files = [
            file_relpath
            for file_relpath in file_relpaths
            if not (staging_dir / file_relpath).is_file()
        ]
        if not missing_directories and not missing_files:
            return

        details: list[str] = []
        if missing_directories:
            details.append(
                "directories="
                + ", ".join(missing_directories[:5])
                + (
                    f" (+{len(missing_directories) - 5} more)"
                    if len(missing_directories) > 5
                    else ""
                )
            )
        if missing_files:
            details.append(
                "files="
                + ", ".join(missing_files[:5])
                + (
                    f" (+{len(missing_files) - 5} more)"
                    if len(missing_files) > 5
                    else ""
                )
            )
        raise ArtifactCollectionError(
            "failed to rehydrate excluded symlink path "
            f"{symlink_relpath.as_posix()}: manifest entries vanished during transfer "
            f"({'; '.join(details)})"
        )

    @staticmethod
    def _run_remote_command_via_ssh(
        *,
        ssh_command: str,
        remote_host: str,
        ssh_user: str | None,
        command: str,
    ) -> subprocess.CompletedProcess[str]:
        """Run one remote command through a prebuilt SSH transport."""
        destination = f"{ssh_user}@{remote_host}" if ssh_user else remote_host
        return subprocess.run(
            [*shlex.split(ssh_command), destination, command],
            check=False,
            capture_output=True,
            text=True,
        )

    def _run_remote_command(
        self,
        *,
        worker: CloudInstanceLike,
        fleet: SshTransportConfig,
        known_hosts_path: Path | None,
        ssh_user: str | None,
        command: str,
        experiment_filestore: Path,
    ) -> subprocess.CompletedProcess[str]:
        """Run one shell command on a remote VM and capture stdout/stderr."""
        if fleet.ssh_via_iap:
            if not ssh_user:
                raise ArtifactCollectionError(
                    f"Unable to resolve SSH user for IAP remote command on {worker.name}"
                )
            iap_known_hosts_path = self._prepare_iap_known_hosts(
                experiment_filestore=experiment_filestore,
                host_key_alias=worker.name,
            )
            with self._open_iap_tunnel(worker=worker, fleet=fleet) as local_port:
                cmd = self._transport.build_local_ssh_command(
                    project=fleet.project,
                    remote_host="127.0.0.1",
                    ssh_via_iap=True,
                    known_hosts_path=iap_known_hosts_path,
                    ssh_user=ssh_user,
                    local_port=local_port,
                    host_key_alias=worker.name,
                    remote_command=command,
                )
                return subprocess.run(
                    cmd,
                    check=False,
                    capture_output=True,
                    text=True,
                )
        else:
            remote_host = self._remote_host(worker, fleet)
            cmd = self._transport.build_local_ssh_command(
                project=fleet.project,
                remote_host=remote_host,
                ssh_via_iap=False,
                known_hosts_path=known_hosts_path,
                ssh_user=ssh_user,
                remote_command=command,
            )

        return subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
        )

    def _read_remote_text_file(
        self,
        *,
        worker: CloudInstanceLike,
        fleet: SshTransportConfig,
        known_hosts_path: Path | None,
        ssh_user: str | None,
        remote_path: str,
        experiment_filestore: Path,
    ) -> str:
        """Read one remote text file and return its contents."""
        result = self._run_remote_command(
            worker=worker,
            fleet=fleet,
            known_hosts_path=known_hosts_path,
            ssh_user=ssh_user,
            command=f"sudo cat {shlex.quote(remote_path)}",
            experiment_filestore=experiment_filestore,
        )
        if result.returncode != 0:
            detail = (result.stderr or "").strip() or (result.stdout or "").strip()
            raise ArtifactCollectionError(
                f"Failed to read remote file {remote_path}: "
                f"{detail or f'exit {result.returncode}'}"
            )
        return result.stdout

    def _remote_path_exists(
        self,
        *,
        worker: CloudInstanceLike,
        fleet: SshTransportConfig,
        known_hosts_path: Path | None,
        ssh_user: str | None,
        remote_path: str,
        experiment_filestore: Path,
    ) -> bool:
        """Return whether the remote experiment directory exists."""
        result = self._run_remote_command(
            worker=worker,
            fleet=fleet,
            known_hosts_path=known_hosts_path,
            ssh_user=ssh_user,
            command=f"test -d {shlex.quote(remote_path)}",
            experiment_filestore=experiment_filestore,
        )
        return result.returncode == 0

    def _direct_ssh_user(self, fleet: SshTransportConfig) -> str | None:
        """Return the local OS Login username for direct GCE SSH, if configured."""
        if self._base_path is None:
            return None

        project = fleet.project
        with self._ssh_user_lock:
            cached = self._ssh_users_by_project.get(project)
            if cached is not None:
                return cached

            try:
                username = self._transport.resolve_direct_ssh_user(project)
            except RuntimeError as exc:
                raise ArtifactCollectionError(str(exc)) from exc
            self._ssh_users_by_project[project] = username
            return username

    def _service_name(self, worker: CloudInstanceLike) -> str:
        """Return the CRSBench user service name expected on the target VM."""
        role = worker.labels.get("crsbench-role")
        if role == "orchestrator":
            return "crsbench-orchestrator.service"
        if role == "evaluator":
            return "crsbench-evaluator.service"
        return "crsbench-worker.service"

    def _log_commands(self, worker: CloudInstanceLike) -> dict[Path, str]:
        """Return remote commands for service journals and runtime summaries."""
        service_name = self._service_name(worker)
        service_log_name = service_name.removesuffix(".service")
        instance_dir = Path(worker.name)
        uid_expr = 'uid="$(id -u crsbench 2>/dev/null || echo 1001)"'

        commands = {
            instance_dir / "runtime-summary.txt": (
                "set -e; "
                f"{uid_expr}; "
                'echo "hostname=$(hostname)"; '
                'echo "instance_role='
                + (
                    "orchestrator"
                    if service_name.startswith("crsbench-orchestrator")
                    else "evaluator"
                    if service_name.startswith("crsbench-evaluator")
                    else "worker"
                )
                + '"; '
                "id crsbench || true; "
                "timedatectl show --property=Timezone --value || true; "
                "docker info --format '{{.CgroupDriver}}' || true; "
                'systemctl is-active "user@${uid}.service" || true; '
                'test -S "/run/user/${uid}/bus" && echo USER_BUS=present || echo USER_BUS=missing; '
                "test -f /var/lib/systemd/linger/crsbench && echo LINGER=present || echo LINGER=missing; "
                "ss -ltnp | grep 6379 || true"
            ),
            instance_dir / "google-startup-scripts.journal.log": (
                f"sudo journalctl -u google-startup-scripts.service -b -n {self._journal_lines} --no-pager || true"
            ),
            instance_dir / "google-guest-agent.journal.log": (
                f"sudo journalctl -u google-guest-agent.service -b -n {self._journal_lines} --no-pager || true"
            ),
            instance_dir / f"{service_log_name}.journal.log": (
                f"{uid_expr}; "
                "sudo -u crsbench env "
                'XDG_RUNTIME_DIR="/run/user/${uid}" '
                'DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/${uid}/bus" '
                f"journalctl --user -u {service_name} -b -n {self._journal_lines} --no-pager || true"
            ),
        }
        if service_name == "crsbench-orchestrator.service":
            commands[instance_dir / "orchestrator.log"] = (
                "sudo cat /var/lib/crsbench/orchestrator.log || true"
            )
        return commands

    def _verify_staging(self, staging_dir: Path) -> None:
        """Verify the staged tree contains at least one valid trial.

        Uses ``discover_trials`` as the sentinel check: a valid trial must have
        ``metadata.json`` present (status == "valid"). Raises
        ``ArtifactCollectionError`` if no valid trials are found.
        """
        from crsbench.reporting.snapshot_loader import discover_trials

        all_trials = discover_trials(staging_dir)
        valid_trials = [t for t in all_trials if t.status == "valid"]
        if not valid_trials:
            raise ArtifactCollectionError(
                f"No valid trials in staged tree: {staging_dir} "
                f"(found {len(all_trials)} trial dirs, none with valid metadata.json). "
                "Collection aborted — final path not written."
            )
        logger.info(
            "Staged tree verified: {} valid trial(s) found in {}",
            len(valid_trials),
            staging_dir,
        )

    def _publish(
        self,
        staging_dir: Path,
        final_dir: Path,
        *,
        replace_trial_dirs: list[Path] | None = None,
    ) -> None:
        """Merge *staging_dir* contents into *final_dir* and remove staging.

        Existing trial directories present in *staging_dir* are removed first so
        re-collects replace each trial exactly while still allowing unrelated
        trials from other workers or earlier runs to coexist under the same
        final experiment directory.
        """
        final_dir.mkdir(parents=True, exist_ok=True)
        staged_trial_dirs = [
            trial_dir.relative_to(staging_dir)
            for trial_dir in _iter_trial_dirs(staging_dir)
        ]
        relpaths_to_replace = list(
            dict.fromkeys([*staged_trial_dirs, *(replace_trial_dirs or [])])
        )
        for relpath in relpaths_to_replace:
            existing_trial_dir = final_dir / relpath
            if existing_trial_dir.exists() or existing_trial_dir.is_symlink():
                self._remove_staged_path(existing_trial_dir)
        subprocess.run(
            [
                "rsync",
                "-a",
                f"{staging_dir}/",
                f"{final_dir}/",
            ],
            check=True,
        )
        shutil.rmtree(staging_dir)
        logger.debug("Published staging {} -> {}", staging_dir, final_dir)
