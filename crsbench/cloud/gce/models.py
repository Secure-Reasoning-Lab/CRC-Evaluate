"""Typed request and status models for GCE worker fleet lifecycle."""

from __future__ import annotations

from dataclasses import dataclass, field

DEFAULT_SERVICE_ACCOUNT_SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/cloud-platform",
)


@dataclass(frozen=True)
class GceInstanceRequest:
    """Rendered GCE instance request for one CRSBench worker VM."""

    project: str
    zone: str | None
    name: str
    labels: dict[str, str]
    metadata: dict[str, str]
    service_account_email: str
    ssh_via_iap: bool
    assign_external_ip: bool
    machine_type: str | None = None
    boot_disk_size_gb: int | None = None
    image: str | None = None
    instance_template: str | None = None
    network: str | None = None
    subnetwork: str | None = None
    service_account_scopes: tuple[str, ...] = field(
        default_factory=lambda: DEFAULT_SERVICE_ACCOUNT_SCOPES
    )

    def to_instance_resource(self) -> dict[str, object]:
        """Convert the request into the GCE instance resource shape."""
        resource: dict[str, object] = {
            "name": self.name,
            "labels": dict(self.labels),
            "metadata": {
                "items": [
                    {"key": key, "value": value}
                    for key, value in sorted(self.metadata.items())
                ]
            },
            "service_accounts": [
                {
                    "email": self.service_account_email,
                    "scopes": list(self.service_account_scopes),
                }
            ],
        }

        if not self.instance_template:
            if self.machine_type is None or self.image is None:
                raise ValueError(
                    "machine_type and image are required when no instance template is set"
                )
            if self.zone is None:
                raise ValueError("zone is required for zonal instance resources")
            resource["machine_type"] = (
                f"zones/{self.zone}/machineTypes/{self.machine_type}"
            )
            resource["disks"] = [
                {
                    "boot": True,
                    "auto_delete": True,
                    "initialize_params": {
                        "source_image": self.image,
                        "disk_size_gb": str(self.boot_disk_size_gb),
                    },
                }
            ]

        network_interface: dict[str, object] = {}
        if self.network:
            network_interface["network"] = self.network
        if self.subnetwork:
            network_interface["subnetwork"] = self.subnetwork
        if self.assign_external_ip:
            network_interface["access_configs"] = [
                {"name": "External NAT", "type": "ONE_TO_ONE_NAT"}
            ]
        resource["network_interfaces"] = [network_interface]

        return resource

    def to_bulk_insert_instance_properties(self) -> dict[str, object]:
        """Convert the request into shared regional bulk-insert instance properties."""
        resource: dict[str, object] = {
            "labels": dict(self.labels),
            "metadata": {
                "items": [
                    {"key": key, "value": value}
                    for key, value in sorted(self.metadata.items())
                ]
            },
            "service_accounts": [
                {
                    "email": self.service_account_email,
                    "scopes": list(self.service_account_scopes),
                }
            ],
        }

        if not self.instance_template:
            if self.machine_type is None or self.image is None:
                raise ValueError(
                    "machine_type and image are required when no instance template is set"
                )
            resource["machine_type"] = self.machine_type
            resource["disks"] = [
                {
                    "boot": True,
                    "auto_delete": True,
                    "initialize_params": {
                        "source_image": self.image,
                        "disk_size_gb": str(self.boot_disk_size_gb),
                    },
                }
            ]

        network_interface: dict[str, object] = {}
        if self.network:
            network_interface["network"] = self.network
        if self.subnetwork:
            network_interface["subnetwork"] = self.subnetwork
        if self.assign_external_ip:
            network_interface["access_configs"] = [
                {"name": "External NAT", "type": "ONE_TO_ONE_NAT"}
            ]
        resource["network_interfaces"] = [network_interface]

        return resource


@dataclass(frozen=True)
class GceWorkerRecord:
    """Normalized GCE worker instance record returned to CRSBench."""

    name: str
    instance_id: str
    status: str
    zone: str
    internal_ip: str | None = None
    external_ip: str | None = None
    service_account_email: str | None = None
    labels: dict[str, str] = field(default_factory=dict)
    raw: dict[str, object] = field(default_factory=dict)
