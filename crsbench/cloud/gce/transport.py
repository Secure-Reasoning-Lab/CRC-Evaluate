"""GCE operator transport helpers behind the shared cloud transport API."""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from crsbench.utils.logger import get_logger

if TYPE_CHECKING:
    from crsbench.cloud.transport import CloudSshTarget

logger = get_logger(__name__)


class GceCloudTransport:
    """Build GCE-specific SSH and IAP operator commands."""

    def build_ssh_command(
        self,
        target: CloudSshTarget,
        *,
        remote_command: list[str] | None = None,
        tty: bool = False,
    ) -> list[str]:
        cmd = [
            "gcloud",
            "compute",
            "ssh",
            target.name,
            f"--project={target.project}",
            f"--zone={target.zone}",
        ]
        if target.ssh_via_iap:
            cmd.append("--tunnel-through-iap")
        identity_file = self.detect_ssh_key_file()
        if identity_file is not None:
            cmd.append(f"--ssh-key-file={identity_file}")
        if tty:
            cmd.append("--ssh-flag=-t")
        if remote_command:
            cmd.append(f"--command={shlex.join(remote_command)}")
        return cmd

    def build_iap_tunnel_command(
        self,
        *,
        instance_name: str,
        project: str,
        zone: str,
        local_port: int,
        remote_port: int | str = 22,
    ) -> list[str]:
        return [
            "gcloud",
            "compute",
            "start-iap-tunnel",
            instance_name,
            str(remote_port),
            f"--project={project}",
            f"--zone={zone}",
            f"--local-host-port=127.0.0.1:{local_port}",
        ]

    def build_local_forward_command(
        self,
        *,
        project: str,
        remote_host: str,
        local_port: int,
        remote_bind: str,
        ssh_via_iap: bool,
        known_hosts_path: Path,
        ssh_user: str | None = None,
        host_key_alias: str | None = None,
        iap_ssh_port: int | None = None,
    ) -> list[str]:
        forward = f"{local_port}:{remote_bind}"
        resolved_ssh_user = ssh_user or self.resolve_direct_ssh_user(project)
        cmd = self._base_ssh_command(
            ssh_via_iap=ssh_via_iap,
            known_hosts_path=known_hosts_path,
            host_key_alias=host_key_alias,
            local_port=iap_ssh_port,
            prefer_gcloud_config=False,
        )
        cmd.extend(["-N", "-L", forward, f"{resolved_ssh_user}@{remote_host}"])
        return cmd

    def build_local_ssh_command(
        self,
        *,
        project: str,
        remote_host: str,
        ssh_via_iap: bool,
        known_hosts_path: Path | None,
        ssh_user: str | None = None,
        local_port: int | None = None,
        host_key_alias: str | None = None,
        remote_command: str | None = None,
    ) -> list[str]:
        resolved_user = ssh_user or self.resolve_direct_ssh_user(project)
        cmd = self._base_ssh_command(
            ssh_via_iap=ssh_via_iap,
            known_hosts_path=known_hosts_path,
            host_key_alias=host_key_alias,
            local_port=local_port,
            prefer_gcloud_config=False,
        )
        if ssh_via_iap:
            cmd.extend(["-l", resolved_user, remote_host])
        else:
            cmd.append(f"{resolved_user}@{remote_host}")
        if remote_command is not None:
            cmd.append(remote_command)
        return cmd

    def build_rsync_ssh_command(
        self,
        *,
        project: str,
        ssh_via_iap: bool,
        known_hosts_path: Path | None,
        ssh_user: str | None = None,
        local_port: int | None = None,
        host_key_alias: str | None = None,
    ) -> str:
        del project
        parts = self._base_ssh_command(
            ssh_via_iap=ssh_via_iap,
            known_hosts_path=known_hosts_path,
            host_key_alias=host_key_alias,
            local_port=local_port,
            prefer_gcloud_config=False,
        )
        if ssh_via_iap and ssh_user is not None:
            parts.extend(["-l", shlex.quote(ssh_user)])
        return " ".join(parts)

    def resolve_direct_ssh_user(self, project: str) -> str:
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
            raise RuntimeError(
                f"Unable to resolve OS Login username for project {project}"
            )
        return username

    def prepare_known_hosts(
        self,
        *,
        known_hosts_path: Path,
        remote_host: str,
    ) -> Path:
        self._ensure_known_hosts_file(known_hosts_path)
        subprocess.run(
            ["ssh-keygen", "-R", remote_host, "-f", str(known_hosts_path)],
            check=False,
            capture_output=True,
            text=True,
        )
        keyscan = subprocess.run(
            ["ssh-keyscan", "-T", "5", "-t", "ed25519", remote_host],
            check=True,
            capture_output=True,
            text=True,
        )
        if not keyscan.stdout.strip():
            raise RuntimeError(f"ssh-keyscan returned no host key for {remote_host}")
        with known_hosts_path.open("a", encoding="utf-8") as handle:
            handle.write(keyscan.stdout)
        return known_hosts_path

    def prepare_iap_known_hosts(
        self,
        *,
        known_hosts_path: Path,
        host_key_alias: str,
    ) -> Path:
        self._ensure_known_hosts_file(known_hosts_path)
        subprocess.run(
            ["ssh-keygen", "-R", host_key_alias, "-f", str(known_hosts_path)],
            check=False,
            capture_output=True,
            text=True,
        )
        return known_hosts_path

    def detect_ssh_key_file(self) -> Path | None:
        return self._detect_ssh_key_file(prefer_gcloud_config=True)

    def _detect_ssh_key_file(self, *, prefer_gcloud_config: bool) -> Path | None:
        configured = (
            self._configured_gcloud_ssh_key_file() if prefer_gcloud_config else None
        )
        if configured is not None:
            return configured

        default_identity = Path.home() / ".ssh" / "google_compute_engine"
        if default_identity.is_file():
            return default_identity

        candidates = sorted(
            Path("/tmp").glob("crsbench-oslogin-*/id_ed25519"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        return candidates[0] if candidates else None

    def _configured_gcloud_ssh_key_file(self) -> Path | None:
        result = subprocess.run(
            ["gcloud", "config", "get-value", "compute/ssh_key_file"],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return None
        value = result.stdout.strip()
        if not value or value == "(unset)":
            return None
        candidate = Path(value).expanduser()
        return candidate if candidate.is_file() else None

    def _base_ssh_command(
        self,
        *,
        ssh_via_iap: bool,
        known_hosts_path: Path | None,
        host_key_alias: str | None = None,
        local_port: int | None = None,
        prefer_gcloud_config: bool,
    ) -> list[str]:
        parts = ["ssh", "-o", "BatchMode=yes"]
        if ssh_via_iap:
            parts.extend(
                [
                    "-o",
                    "StrictHostKeyChecking=no",
                    "-o",
                    "CheckHostIP=no",
                ]
            )
            if host_key_alias is not None:
                parts.extend(["-o", f"HostKeyAlias={host_key_alias}"])
        else:
            parts.extend(["-o", "StrictHostKeyChecking=yes"])
        if known_hosts_path is not None:
            parts.extend(["-o", f"UserKnownHostsFile={known_hosts_path}"])
        parts.extend(["-o", "ConnectTimeout=10"])
        if local_port is not None:
            parts.extend(["-p", str(local_port)])
        identity_file = self._detect_ssh_key_file(
            prefer_gcloud_config=prefer_gcloud_config
        )
        if identity_file is not None:
            parts.extend(["-i", str(identity_file), "-o", "IdentitiesOnly=yes"])
        return parts

    def _ensure_known_hosts_file(self, known_hosts_path: Path) -> None:
        known_hosts_path.parent.mkdir(parents=True, exist_ok=True)
        known_hosts_path.touch(exist_ok=True)
        known_hosts_path.chmod(0o600)
