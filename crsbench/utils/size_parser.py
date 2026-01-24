"""Human-readable size parsing utilities."""

import re


def parse_size_to_bytes(size_str: str) -> int:
    """Parse human-readable size string to bytes.

    Args:
        size_str: Size string like "200GB", "100MB", "1TB", "500G"

    Returns:
        Size in bytes

    Raises:
        ValueError: If format is invalid

    Examples:
        >>> parse_size_to_bytes("200GB")
        214748364800
        >>> parse_size_to_bytes("100MB")
        104857600
        >>> parse_size_to_bytes("1TB")
        1099511627776
    """
    units = {
        "B": 1,
        "KB": 1024,
        "MB": 1024**2,
        "GB": 1024**3,
        "TB": 1024**4,
        "K": 1024,
        "M": 1024**2,
        "G": 1024**3,
        "T": 1024**4,
    }

    match = re.match(r"^\s*(\d+(?:\.\d+)?)\s*([A-Za-z]+)\s*$", size_str)
    if not match:
        raise ValueError(f"Invalid size format: {size_str}")

    value, unit = match.groups()
    unit_upper = unit.upper()

    if unit_upper not in units:
        raise ValueError(f"Unknown unit: {unit}")

    return int(float(value) * units[unit_upper])
