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


def build_tunnel_command(
    base_path: Path | str,
    launch_state: CloudLaunchState,
    *,
    local_port: int,
) -> list[str]:
    """Build the subprocess command used for the local Redis forward."""
    forward = f"{local_port}:{_REMOTE_REDIS_BIND}"

    if launch_state.orchestrator_ssh_via_iap:
        return [
            "gcloud",
            "compute",
            "ssh",
            launch_state.orchestrator_name,
            f"--project={launch_state.orchestrator_project}",
            f"--zone={launch_state.orchestrator_zone}",
            "--tunnel-through-iap",
            "--",
            "-N",
            "-L",
            forward,
        ]

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


def wait_for_local_port(host: str, port: int, *, timeout: float = 10.0) -> None:
    """Wait until a local forwarded port starts accepting TCP connections."""
    deadline = time.monotonic() + timeout
    last_error: OSError | None = None
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return
        except OSError as exc:
            last_error = exc
            time.sleep(0.1)
    raise OrchestratorTunnelError(
        f"Timed out waiting for local tunnel {host}:{port} to become ready: {last_error}"
    )


def _allocate_local_port() -> int:
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
    startup_timeout: float = 10.0
    process: subprocess.Popen | None = None

    @classmethod
    def from_launch_state(
        cls,
        base_path: Path | str,
        launch_state: CloudLaunchState,
        *,
        local_port: int | None = None,
        startup_timeout: float = 10.0,
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

        if self.local_port is None:
            self.local_port = _allocate_local_port()
        cmd = build_tunnel_command(
            self.base_path,
            self.launch_state,
            local_port=self.local_port,
        )
        if shutil.which(cmd[0]) is None:
            raise OrchestratorTunnelError(
                f"Required transport command not found: {cmd[0]}"
            )

        self.process = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        try:
            wait_for_local_port(
                "127.0.0.1", self.local_port, timeout=self.startup_timeout
            )
        except Exception:
            self.stop()
            raise

    def stop(self) -> None:
        """Terminate the forward process if it is still running."""
        if self.process is None:
            return

        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5.0)
        self.process = None
