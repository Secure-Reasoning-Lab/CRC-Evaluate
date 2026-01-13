"""CWE pillar mapping utilities.

Maps CWE IDs to their root pillars (top-level categories) using
MITRE's View 1000 (Research Concepts) hierarchy.

Also provides 2025 CWE Top 25 Most Dangerous Software Weaknesses data.
"""

import json
from functools import lru_cache
from pathlib import Path

# Path to the CWE pillar mapping JSON file
_MAPPING_FILE = Path(__file__).parent / "cwe_pillar_mapping.json"

# 2025 CWE Top 25 Most Dangerous Software Weaknesses
# Source: https://cwe.mitre.org/top25/archive/2025/2025_top25_list.html
CWE_TOP_25_2025: list[tuple[str, str]] = [
    ("79", "Cross-site Scripting (XSS)"),
    ("89", "SQL Injection"),
    ("352", "Cross-Site Request Forgery (CSRF)"),
    ("862", "Missing Authorization"),
    ("787", "Out-of-bounds Write"),
    ("22", "Path Traversal"),
    ("416", "Use After Free"),
    ("125", "Out-of-bounds Read"),
    ("78", "OS Command Injection"),
    ("94", "Code Injection"),
    ("120", "Classic Buffer Overflow"),
    ("434", "Unrestricted Upload of File with Dangerous Type"),
    ("476", "NULL Pointer Dereference"),
    ("121", "Stack-based Buffer Overflow"),
    ("502", "Deserialization of Untrusted Data"),
    ("122", "Heap-based Buffer Overflow"),
    ("863", "Incorrect Authorization"),
    ("20", "Improper Input Validation"),
    ("284", "Improper Access Control"),
    ("200", "Exposure of Sensitive Information"),
    ("306", "Missing Authentication for Critical Function"),
    ("918", "Server-Side Request Forgery (SSRF)"),
    ("77", "Command Injection"),
    ("639", "Authorization Bypass Through User-Controlled Key"),
    ("770", "Allocation of Resources Without Limits"),
]


@lru_cache(maxsize=1)
def load_pillar_mapping() -> tuple[dict[str, str], dict[str, str]]:
    """Load CWE pillar mapping from JSON file.

    Returns:
        Tuple of (pillars dict, mapping dict) where:
        - pillars: pillar_id -> pillar_name
        - mapping: cwe_id -> pillar_id
    """
    if not _MAPPING_FILE.exists():
        return {}, {}

    with _MAPPING_FILE.open() as f:
        data = json.load(f)

    return data.get("pillars", {}), data.get("mapping", {})


def get_pillar_for_cwe(cwe_id: str) -> str | None:
    """Get the root pillar ID for a CWE.

    Args:
        cwe_id: CWE ID, with or without 'CWE-' prefix (e.g., "CWE-119" or "119")

    Returns:
        Pillar CWE ID (without prefix), or None if not found
    """
    pillars, mapping = load_pillar_mapping()

    # Normalize: remove 'CWE-' prefix if present and strip leading zeros
    normalized = cwe_id.replace("CWE-", "").replace("cwe-", "").lstrip("0") or "0"

    return mapping.get(normalized)


def get_pillar_name(pillar_id: str) -> str:
    """Get the name of a pillar by its ID.

    Args:
        pillar_id: Pillar CWE ID (without 'CWE-' prefix)

    Returns:
        Pillar name, or "Unknown" if not found
    """
    pillars, _ = load_pillar_mapping()
    return pillars.get(pillar_id, "Unknown")


def get_all_pillars() -> dict[str, str]:
    """Get all pillar IDs and names.

    Returns:
        Dict of pillar_id -> pillar_name
    """
    pillars, _ = load_pillar_mapping()
    return pillars


def aggregate_cwes_by_pillar(cwe_counts: dict[str, int]) -> dict[str, int]:
    """Aggregate CWE counts by their root pillar.

    Args:
        cwe_counts: Dict of CWE ID -> count (can include 'CWE-' prefix)

    Returns:
        Dict of pillar_id -> aggregated count
    """
    pillar_counts: dict[str, int] = {}

    for cwe_id, count in cwe_counts.items():
        pillar_id = get_pillar_for_cwe(cwe_id)
        if pillar_id:
            pillar_counts[pillar_id] = pillar_counts.get(pillar_id, 0) + count
        else:
            # Track unmapped CWEs under "Other"
            pillar_counts["Other"] = pillar_counts.get("Other", 0) + count

    return pillar_counts


def normalize_cwe_id(cwe_id: str) -> str:
    """Normalize a CWE ID by removing prefix and leading zeros.

    Args:
        cwe_id: CWE ID with or without 'CWE-' prefix (e.g., "CWE-079", "79")

    Returns:
        Normalized CWE ID (e.g., "79")
    """
    return cwe_id.replace("CWE-", "").replace("cwe-", "").lstrip("0") or "0"


def calculate_top25_coverage(cwe_counts: dict[str, int]) -> dict[str, int]:
    """Calculate coverage of 2025 CWE Top 25 from vulnerability counts.

    Args:
        cwe_counts: Dict of CWE ID -> count (can include 'CWE-' prefix)

    Returns:
        Dict of normalized CWE ID -> count for Top 25 CWEs only
    """
    # Normalize input CWE counts
    normalized_counts: dict[str, int] = {}
    for cwe_id, count in cwe_counts.items():
        normalized = normalize_cwe_id(cwe_id)
        normalized_counts[normalized] = normalized_counts.get(normalized, 0) + count

    # Get counts for Top 25 CWEs
    top25_ids = {cwe_id for cwe_id, _ in CWE_TOP_25_2025}
    return {cwe_id: normalized_counts.get(cwe_id, 0) for cwe_id in top25_ids}


def get_top25_name(cwe_id: str) -> str:
    """Get the name of a Top 25 CWE by its ID.

    Args:
        cwe_id: CWE ID (without 'CWE-' prefix)

    Returns:
        CWE name, or empty string if not in Top 25
    """
    for top_id, name in CWE_TOP_25_2025:
        if top_id == cwe_id:
            return name
    return ""


def get_top25_rank(cwe_id: str) -> int:
    """Get the rank of a CWE in the Top 25 list.

    Args:
        cwe_id: CWE ID (without 'CWE-' prefix)

    Returns:
        Rank (1-25), or 0 if not in Top 25
    """
    for i, (top_id, _) in enumerate(CWE_TOP_25_2025, 1):
        if top_id == cwe_id:
            return i
    return 0
