"""Provider-neutral transport resolver for operator remote access."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from crsbench.cloud.types import CloudProvider, coerce_cloud_provider

if TYPE_CHECKING:
    from pathlib import Path


class CloudSshTarget(Protocol):
    """Minimal live-instance shape needed for interactive remote access."""

    name: str
    project: str
    zone: str
    ssh_via_iap: bool


class CloudTransport(Protocol):
    """Provider-owned operator transport operations."""

    def build_serial_command(
        self,
        target: CloudSshTarget,
        *,
        port: int = 1,
    ) -> list[str]: ...

    def build_ssh_command(
        self,
        target: CloudSshTarget,
        *,
        remote_command: list[str] | None = None,
        tty: bool = False,
    ) -> list[str]: ...

    def build_iap_tunnel_command(
        self,
        *,
        instance_name: str,
        project: str,
        zone: str,
        local_port: int,
        remote_port: int | str = 22,
    ) -> list[str]: ...

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
    ) -> list[str]: ...

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
    ) -> list[str]: ...

    def build_rsync_ssh_command(
        self,
        *,
        project: str,
        ssh_via_iap: bool,
        known_hosts_path: Path | None,
        ssh_user: str | None = None,
        local_port: int | None = None,
        host_key_alias: str | None = None,
    ) -> str: ...

    def resolve_direct_ssh_user(self, project: str) -> str: ...

    def prepare_known_hosts(
        self,
        *,
        known_hosts_path: Path,
        remote_host: str,
    ) -> Path: ...

    def prepare_iap_known_hosts(
        self,
        *,
        known_hosts_path: Path,
        host_key_alias: str,
    ) -> Path: ...


def transport_for_provider(provider: CloudProvider | str) -> CloudTransport:
    """Return the provider transport implementation for one cloud provider."""
    resolved = coerce_cloud_provider(provider)
    if resolved is CloudProvider.GCE:
        from crsbench.cloud.gce.transport import GceCloudTransport

        return GceCloudTransport()
    raise NotImplementedError(
        f"Cloud transport is not implemented for provider {resolved.value}"
    )
