from __future__ import annotations

import re
from typing import Any

_MEMORY_PATTERN = re.compile(
    r"^\d+(\.\d+)?\s*(B|K|KB|M|MB|G|GB|T|TB)$",
    re.IGNORECASE,
)

_MEMORY_FIELDS = {
    ("resources", "memory_per_trial"),
    ("crs_compose", "service_mem_limit"),
    ("crs_compose", "infra_mem_limit"),
}


def validate_field_value(section: str, key: str, value: Any) -> None:
    if (section, key) in _MEMORY_FIELDS:
        if value is None:
            return
        text = str(value).strip()
        if not text:
            return
        if not _MEMORY_PATTERN.match(text):
            raise ValueError("Invalid memory format. Try: 8G, 16GB, 1024M, 2048MB")
