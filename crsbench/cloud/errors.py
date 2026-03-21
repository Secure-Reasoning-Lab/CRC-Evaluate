"""Shared cloud control-plane errors."""

from __future__ import annotations


class CloudProvisioningError(RuntimeError):
    """Raised when a cloud provider cannot satisfy a launch request."""
