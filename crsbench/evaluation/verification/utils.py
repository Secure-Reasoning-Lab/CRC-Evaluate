"""Shared utility functions for verification.

This module provides utility functions used across verification modules:
- compute_content_hash: Compute SHA256 hash of file content
"""

import hashlib
from pathlib import Path


def compute_content_hash(path: Path) -> str:
    """Compute SHA256 hash of file content.

    Uses 16 hex characters (64 bits) for reduced collision probability.
    With 64 bits, collision probability stays negligible even with millions of files.

    Args:
        path: Path to file

    Returns:
        First 16 hex characters of SHA256 hash
    """
    if not path.exists():
        # Return hash of empty content for non-existent files
        return hashlib.sha256(b"").hexdigest()[:16]

    sha256 = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(8192):
            sha256.update(chunk)
    return sha256.hexdigest()[:16]
