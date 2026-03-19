"""Temporary local Redis tunnel to a launched remote orchestrator."""

from __future__ import annotations

import shutil
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from crsbench.cloud.launch_state import CloudLaunchState, cloud_state_dir
from crsbench.utils.logger import get_logger

logger = get_logger(__name__)

_REMOTE_REDIS_BIND = "127.0.0.1:6379"
_TUNNEL_STARTUP_TIMEOUT_SEC = 30.0
_TUNNEL_STARTUP_ATTEMPT_TIMEOUT_SEC = 5.0
_TUNNEL_STARTUP_RETRY_DELAY_SEC = 1.0
_IAP_REMOTE_SSH_PORT = "22"


class OrchestratorTunnelError(RuntimeError):
    """Raised when the orchestrator Redis tunnel cannot be established."""


def resolve_direct_ssh_user(project: str) -> str:
    """Return the local OS Login username for direct GCE SSH."""
    result = subprocess.run(
        [
            "gcloud",
            "compute",
            "os-login",
            "describe-profile",
            f"--project={project}",
            "--format=value(posixAccounts[0].username)",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    username = result.stdout.strip()
    if not username:
        raise OrchestratorTunnelError(
            f"Unable to resolve OS Login username for project {project}"
        )
    return username


def prepare_known_hosts(base_path: Path | str, remote_host: str) -> Path:
    """Seed direct-SSH host trust in a config-adjacent known_hosts file."""
    known_hosts_path = cloud_state_dir(base_path) / "known_hosts"
    known_hosts_path.parent.mkdir(parents=True, exist_ok=True)
    known_hosts_path.touch(exist_ok=True)
    known_hosts_path.chmod(0o600)

    subprocess.run(
        [
            "ssh-keygen",
            "-R",
            remote_host,
            "-f",
            str(known_hosts_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    keyscan = subprocess.run(
        [
            "ssh-keyscan",
            "-T",
            "5",
            "-t",
            "ed25519",
            remote_host,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    if not keyscan.stdout.strip():
        raise OrchestratorTunnelError(
            f"ssh-keyscan returned no host key for {remote_host}"
        )
    with known_hosts_path.open("a", encoding="utf-8") as handle:
        handle.write(keyscan.stdout)
    return known_hosts_path


def prepare_iap_known_hosts(base_path: Path | str, host_key_alias: str) -> Path:
    """Prepare the localhost-tunnel known_hosts file used for IAP-backed SSH."""
    known_hosts_path = cloud_state_dir(base_path) / "known_hosts_iap"
    known_hosts_path.parent.mkdir(parents=True, exist_ok=True)
    known_hosts_path.touch(exist_ok=True)
    known_hosts_path.chmod(0o600)

    subprocess.run(
        [
            "ssh-keygen",
            "-R",
            host_key_alias,
            "-f",
            str(known_hosts_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    return known_hosts_path


def build_iap_tunnel_command(
    launch_state: CloudLaunchState,
    *,
    local_port: int,
) -> list[str]:
    """Build the gcloud command that opens an IAP tunnel to remote SSH."""
    return [
        "gcloud",
        "compute",
        "start-iap-tunnel",
        launch_state.orchestrator_name,
        _IAP_REMOTE_SSH_PORT,
        f"--project={launch_state.orchestrator_project}",
        f"--zone={launch_state.orchestrator_zone}",
        f"--local-host-port=127.0.0.1:{local_port}",
    ]


def build_tunnel_command(
    base_path: Path | str,
    launch_state: CloudLaunchState,
    *,
    local_port: int,
    iap_ssh_port: int | None = None,
) -> list[str]:
    """Build the subprocess command used for the local Redis forward."""
    forward = f"{local_port}:{_REMOTE_REDIS_BIND}"

    if launch_state.orchestrator_ssh_via_iap:
        if iap_ssh_port is None:
            raise OrchestratorTunnelError(
                "IAP-backed orchestrator tunnel requires a local forwarded SSH port"
            )
        known_hosts_path = prepare_iap_known_hosts(
            base_path,
            launch_state.orchestrator_name,
        )
        ssh_user = resolve_direct_ssh_user(launch_state.orchestrator_project)
        cmd = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "CheckHostIP=no",
            "-o",
            f"HostKeyAlias={launch_state.orchestrator_name}",
            "-o",
            f"UserKnownHostsFile={known_hosts_path}",
            "-o",
            "ConnectTimeout=10",
            "-p",
            str(iap_ssh_port),
        ]
        identity_file = Path.home() / ".ssh" / "google_compute_engine"
        if identity_file.is_file():
            cmd.extend(["-i", str(identity_file), "-o", "IdentitiesOnly=yes"])
        cmd.extend(["-N", "-L", forward, f"{ssh_user}@127.0.0.1"])
        return cmd

    remote_host = (
        launch_state.orchestrator_external_ip
        or launch_state.orchestrator_internal_ip
        or launch_state.orchestrator_name
    )
    known_hosts_path = prepare_known_hosts(base_path, remote_host)
    ssh_user = resolve_direct_ssh_user(launch_state.orchestrator_project)
    cmd = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        f"UserKnownHostsFile={known_hosts_path}",
    ]
    identity_file = Path.home() / ".ssh" / "google_compute_engine"
    if identity_file.is_file():
        cmd.extend(["-i", str(identity_file), "-o", "IdentitiesOnly=yes"])
    cmd.extend(["-N", "-L", forward, f"{ssh_user}@{remote_host}"])
    return cmd


def wait_for_local_port(
    host: str,
    port: int,
    *,
    timeout: float = 10.0,
    process: subprocess.Popen | None = None,
    process_label: str = "tunnel process",
) -> None:
    """Wait until a local forwarded port starts accepting TCP connections."""
    deadline = time.monotonic() + timeout
    last_error: OSError | None = None
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return
        except OSError as exc:
            last_error = exc
            if process is not None:
                return_code = process.poll()
                if return_code is not None:
                    raise OrchestratorTunnelError(
                        f"{process_label} exited with code {return_code} before local tunnel "
                        f"{host}:{port} became ready"
                    ) from exc
            time.sleep(0.1)
    raise OrchestratorTunnelError(
        f"Timed out waiting for local tunnel {host}:{port} to become ready: {last_error}"
    )


def allocate_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        return int(sock.getsockname()[1])


@dataclass
class OrchestratorRedisTunnel:
    """Context manager that owns the orchestrator Redis forward process."""

    base_path: Path
    launch_state: CloudLaunchState
    local_port: int | None = None
    startup_timeout: float = _TUNNEL_STARTUP_TIMEOUT_SEC
    process: subprocess.Popen | None = None
    iap_process: subprocess.Popen | None = None
    iap_local_port: int | None = None

    @classmethod
    def from_launch_state(
        cls,
        base_path: Path | str,
        launch_state: CloudLaunchState,
        *,
        local_port: int | None = None,
        startup_timeout: float = _TUNNEL_STARTUP_TIMEOUT_SEC,
    ) -> "OrchestratorRedisTunnel":
        return cls(
            base_path=Path(base_path),
            launch_state=launch_state,
            local_port=local_port,
            startup_timeout=startup_timeout,
        )

    @property
    def redis_host(self) -> str:
        if self.local_port is None:
            raise OrchestratorTunnelError("Tunnel has not been started yet")
        return f"127.0.0.1:{self.local_port}"

    def __enter__(self) -> "OrchestratorRedisTunnel":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()

    def start(self) -> None:
        """Start the forward process and wait for the local port."""
        if self.process is not None:
            return

        auto_local_port = self.local_port is None
        required_commands = ["ssh"]
        if self.launch_state.orchestrator_ssh_via_iap:
            required_commands.append("gcloud")
        for transport_command in required_commands:
            if shutil.which(transport_command) is None:
                raise OrchestratorTunnelError(
                    f"Required transport command not found: {transport_command}"
                )

        deadline = time.monotonic() + self.startup_timeout
        last_error: Exception | None = None

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if last_error is not None:
                    raise last_error
                raise OrchestratorTunnelError(
                    f"Timed out starting orchestrator tunnel after {self.startup_timeout}s"
                )

            if auto_local_port or self.local_port is None:
                self.local_port = allocate_local_port()
            if self.launch_state.orchestrator_ssh_via_iap:
                self.iap_local_port = allocate_local_port()
                self.iap_process = subprocess.Popen(
                    build_iap_tunnel_command(
                        self.launch_state,
                        local_port=self.iap_local_port,
                    ),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    text=True,
                )
                try:
                    wait_for_local_port(
                        "127.0.0.1",
                        self.iap_local_port,
                        timeout=min(remaining, _TUNNEL_STARTUP_ATTEMPT_TIMEOUT_SEC),
                        process=self.iap_process,
                        process_label="orchestrator IAP tunnel process",
                    )
                except Exception:
                    self.stop()
                    if auto_local_port:
                        self.local_port = None
                    self.iap_local_port = None
                    raise
            self.process = subprocess.Popen(
                build_tunnel_command(
                    self.base_path,
                    self.launch_state,
                    local_port=self.local_port,
                    iap_ssh_port=self.iap_local_port,
                ),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            try:
                wait_for_local_port(
                    "127.0.0.1",
                    self.local_port,
                    timeout=min(remaining, _TUNNEL_STARTUP_ATTEMPT_TIMEOUT_SEC),
                    process=self.process,
                    process_label="orchestrator redis tunnel process",
                )
                return
            except Exception as exc:
                last_error = exc
                self.stop()
                if auto_local_port:
                    self.local_port = None
                self.iap_local_port = None
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise
                time.sleep(min(_TUNNEL_STARTUP_RETRY_DELAY_SEC, remaining))

    def stop(self) -> None:
        """Terminate the forward process if it is still running."""
        if self.process is None:
            process = None
        else:
            process = self.process

        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5.0)
        self.process = None

        if self.iap_process is None:
            return
        if self.iap_process.poll() is None:
            self.iap_process.terminate()
            try:
                self.iap_process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                self.iap_process.kill()
                self.iap_process.wait(timeout=5.0)
        self.iap_process = None
