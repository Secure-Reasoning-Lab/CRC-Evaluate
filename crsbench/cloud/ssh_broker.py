"""Shared SSH/IAP transport helpers for cloud rsync operations.

Both :class:`crsbench.cloud.collection.ArtifactCollector` (pulls results from
workers) and :class:`crsbench.cloud.collection.ArtifactPusher` (pushes
``from_experiment`` bundles to VMs before launch) need the same low-level
plumbing: resolving OS Login usernames, seeding known_hosts, opening IAP
tunnels, and building ssh command strings for rsync's ``-e`` flag.

The broker centralises that plumbing so the collector and pusher stay focused
on their own dataflow.
"""

from __future__ import annotations

import subprocess
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Iterator, Protocol

import tenacity

from crsbench.cloud.launch_state import cloud_state_dir
from crsbench.cloud.orchestrator_tunnel import allocate_local_port, wait_for_local_port
from crsbench.cloud.transport import CloudTransport, transport_for_provider
from crsbench.utils.logger import get_logger

if TYPE_CHECKING:
    from crsbench.cloud.records import CloudInstanceLike


logger = get_logger(__name__)

_IAP_TUNNEL_PORT = 22
_IAP_TUNNEL_STARTUP_TIMEOUT_SEC = 30.0


class SshTransportConfig(Protocol):
    """Minimal transport settings needed for rsync/SSH connections."""

    project: str
    zone: str | None
    ssh_via_iap: bool


class SshBrokerError(RuntimeError):
    """Raised when SSH access preparation or remote execution fails."""


def run_rsync_with_retry(cmd: list[str]) -> None:
    """Run an rsync command with exponential-backoff retry on failure."""

    @tenacity.retry(
        retry=tenacity.retry_if_exception_type(subprocess.CalledProcessError),
        wait=tenacity.wait_exponential(multiplier=1, min=2, max=30),
        stop=tenacity.stop_after_attempt(3),
        reraise=True,
    )
    def _run() -> None:
        subprocess.run(cmd, check=True)

    _run()


class SshBroker:
    """Shared SSH transport helper for direct-IP and IAP-tunneled rsync/ssh."""

    def __init__(
        self,
        *,
        base_path: Path | str | None = None,
        transport: CloudTransport | None = None,
    ) -> None:
        self._base_path = Path(base_path) if base_path is not None else None
        self._transport = transport or transport_for_provider("gce")
        self._ssh_users_by_project: dict[str, str] = {}
        self._ssh_user_lock = threading.Lock()
        self._iap_known_hosts_lock = threading.Lock()

    @property
    def transport(self) -> CloudTransport:
        return self._transport

    @property
    def base_path(self) -> Path | None:
        return self._base_path

    def state_dir(self, experiment_filestore: Path) -> Path:
        """Return the local state directory used for SSH trust and log capture."""
        if self._base_path is not None:
            return cloud_state_dir(self._base_path)
        return experiment_filestore / ".crsbench-cloud"

    def known_hosts_path(
        self,
        experiment_filestore: Path,
        *,
        worker_name: str | None = None,
    ) -> Path:
        """Return the local known_hosts file used for direct-IP transport."""
        base = self.state_dir(experiment_filestore) / "known_hosts"
        if worker_name:
            return base / worker_name
        return base

    def iap_known_hosts_path(self, experiment_filestore: Path) -> Path:
        """Return the localhost tunnel known_hosts file used for IAP transport."""
        return self.state_dir(experiment_filestore) / "known_hosts_iap"

    def remote_host(
        self,
        worker: CloudInstanceLike,
        fleet: SshTransportConfig,
    ) -> str:
        """Return the SSH/rsync host token for one worker."""
        if fleet.ssh_via_iap:
            return worker.name
        return worker.external_ip or worker.internal_ip or worker.name

    def direct_ssh_user(self, fleet: SshTransportConfig) -> str | None:
        """Return the local OS Login username for direct GCE SSH, if available."""
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
                raise SshBrokerError(str(exc)) from exc
            self._ssh_users_by_project[project] = username
            return username

    def prepare_direct_known_hosts(
        self,
        *,
        worker: CloudInstanceLike,
        fleet: SshTransportConfig,
        experiment_filestore: Path,
    ) -> Path | None:
        """Seed host trust for direct-IP SSH and return the known_hosts path."""
        if fleet.ssh_via_iap or self._base_path is None:
            return None

        known_hosts = self.known_hosts_path(
            experiment_filestore,
            worker_name=worker.name,
        )
        known_hosts.parent.mkdir(parents=True, exist_ok=True)
        try:
            return self._transport.prepare_known_hosts(
                known_hosts_path=known_hosts,
                remote_host=self.remote_host(worker, fleet),
            )
        except RuntimeError as exc:
            raise SshBrokerError(str(exc)) from exc

    def prepare_iap_known_hosts(
        self,
        *,
        experiment_filestore: Path,
        host_key_alias: str,
    ) -> Path:
        """Clear stale host keys for the localhost IAP tunnel and return the path."""
        known_hosts = self.iap_known_hosts_path(experiment_filestore)
        known_hosts.parent.mkdir(parents=True, exist_ok=True)
        with self._iap_known_hosts_lock:
            return self._transport.prepare_iap_known_hosts(
                known_hosts_path=known_hosts,
                host_key_alias=host_key_alias,
            )

    def build_direct_ssh_command(
        self,
        *,
        project: str,
        known_hosts_path: Path | None,
    ) -> str:
        """Return the ``-e`` argument for direct-IP rsync SSH transport."""
        return self._transport.build_rsync_ssh_command(
            project=project,
            ssh_via_iap=False,
            known_hosts_path=known_hosts_path,
        )

    def build_iap_ssh_command(
        self,
        *,
        local_port: int,
        ssh_user: str,
        known_hosts_path: Path,
        host_key_alias: str,
    ) -> str:
        """Return the ``-e`` argument for IAP-tunneled rsync SSH transport."""
        return self._transport.build_rsync_ssh_command(
            project="",
            ssh_via_iap=True,
            known_hosts_path=known_hosts_path,
            ssh_user=ssh_user,
            local_port=local_port,
            host_key_alias=host_key_alias,
        )

    def build_iap_tunnel_command(
        self,
        *,
        worker: CloudInstanceLike,
        fleet: SshTransportConfig,
        local_port: int,
    ) -> list[str]:
        """Return the provider command that opens an IAP tunnel to remote SSH."""
        zone = worker.zone or fleet.zone or ""
        return self._transport.build_iap_tunnel_command(
            instance_name=worker.name,
            project=fleet.project,
            zone=zone,
            local_port=local_port,
            remote_port=_IAP_TUNNEL_PORT,
        )

    @contextmanager
    def open_iap_tunnel(
        self,
        *,
        worker: CloudInstanceLike,
        fleet: SshTransportConfig,
    ) -> Iterator[int]:
        """Open a temporary local TCP tunnel to a worker's SSH port via IAP."""
        local_port = allocate_local_port()
        cmd = self.build_iap_tunnel_command(
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
            raise SshBrokerError(
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
            raise SshBrokerError(
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

    def run_remote_command(
        self,
        *,
        worker: CloudInstanceLike,
        fleet: SshTransportConfig,
        command: str,
        experiment_filestore: Path,
        known_hosts_path: Path | None = None,
        ssh_user: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run one shell command on a remote VM and capture stdout/stderr."""
        if fleet.ssh_via_iap:
            if not ssh_user:
                raise SshBrokerError(
                    f"Unable to resolve SSH user for IAP command on {worker.name}"
                )
            iap_known_hosts = self.prepare_iap_known_hosts(
                experiment_filestore=experiment_filestore,
                host_key_alias=worker.name,
            )
            with self.open_iap_tunnel(worker=worker, fleet=fleet) as local_port:
                cmd = self._transport.build_local_ssh_command(
                    project=fleet.project,
                    remote_host="127.0.0.1",
                    ssh_via_iap=True,
                    known_hosts_path=iap_known_hosts,
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
        remote_host = self.remote_host(worker, fleet)
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
