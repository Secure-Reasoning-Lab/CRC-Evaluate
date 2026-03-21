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
import shlex
import shutil
import subprocess
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path  # noqa: TC003
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
    marker_path.write_text(json.dumps(payload), encoding="utf-8")


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
    if not isinstance(raw, str):
        return None

    normalized = raw.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
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
        self._transport = transport or transport_for_provider("gce")

    def collect(
        self,
        worker: CloudInstanceLike,
        fleet: SshTransportConfig,
        experiment_name: str,
        experiment_filestore: Path,
        remote_experiment_dir: str,
        start_time_observations: list[tuple[str | None, str]] | None = None,
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
        )

        # Verify before publishing
        self._verify_staging(staging_dir)
        if start_time_observations is not None:
            start_time_observations.append(
                discover_experiment_start_time_from_staging([staging_dir])
            )

        final_dir = experiment_filestore / experiment_name
        self._publish(staging_dir, final_dir)

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
            self._run_log_rsync(
                worker=worker,
                fleet=fleet,
                remote_experiment_dir=remote_experiment_dir,
                staging_dir=instance_logs_dir / "trial-artifacts",
                experiment_filestore=experiment_filestore,
                known_hosts_path=known_hosts_path,
                ssh_user=ssh_user,
            )

        logger.info(
            "Remote log collection complete: worker={} experiment={} logs_dir={}",
            worker.name,
            experiment_name,
            instance_logs_dir,
        )
        return logs_root

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
    ) -> list[str]:
        """Return the full rsync command as a list of strings.

        Flags used:
        - ``-a``: archive mode (preserves mtimes, permissions, symlinks — ARTF-02)
        - ``--mkpath``: create destination dirs as needed (rsync 3.2+)
        - ``--partial-dir=.rsync-partial``: job-local partial dir (no cross-job collisions)
        - ``--delay-updates``: stage all files before renaming into place
        - ``--delete-delay``: remove remote-deleted files after transfer completes
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

        return [
            "rsync",
            "-a",
            "--mkpath",
            "--partial-dir=.rsync-partial",
            "--delay-updates",
            "--delete-delay",
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
            "--include=*/",
            "--include=trial_matrix.json",
            "--include=metadata.json",
            "--include=worker.log",
            "--include=.success",
            "--include=.failure",
            "--exclude=*",
            "-e",
            ssh_cmd,
            source,
            dest,
        ]

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

    def _known_hosts_path(self, experiment_filestore: Path) -> Path:
        """Return the local known_hosts file used for direct-IP collection."""
        return self._state_dir(experiment_filestore) / "known_hosts"

    def _iap_known_hosts_path(self, experiment_filestore: Path) -> Path:
        """Return the localhost tunnel known_hosts file used for IAP rsync."""
        return self._state_dir(experiment_filestore) / "known_hosts_iap"

    def _prepare_iap_known_hosts(
        self,
        *,
        experiment_filestore: Path,
        host_key_alias: str,
    ) -> Path:
        """Clear any stale stable-name host key before reconnecting over an IAP SSH tunnel."""
        known_hosts_path = self._iap_known_hosts_path(experiment_filestore)
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

        known_hosts_path = self._known_hosts_path(experiment_filestore)
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
        )
        self._run_rsync_with_retry(cmd)

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

    def _publish(self, staging_dir: Path, final_dir: Path) -> None:
        """Merge *staging_dir* contents into *final_dir* and remove staging.

        Uses ``shutil.copytree`` with ``dirs_exist_ok=True`` so that incremental
        collections (multiple workers, multiple runs) merge correctly into the
        same final experiment directory.
        """
        final_dir.mkdir(parents=True, exist_ok=True)
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
