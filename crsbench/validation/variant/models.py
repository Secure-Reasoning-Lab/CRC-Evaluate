"""Variant models for POV validation.

This module defines the core data structures for managing benchmark variants
used in POV (Proof of Vulnerability) validation.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class BenchmarkMode(Enum):
    """Benchmark testing mode."""
    FULL = "full"
    DELTA = "delta"


class BuildTag(Enum):
    """Build variant tag identifying the type of build.

    - FULL_BASE: Base commit for full mode (vulnerable version)
    - DELTA_BASE: Base commit for delta mode (pre-vulnerability version)
    - DELTA_REF: Reference commit for delta mode (vulnerable version)
    - ALL_PATCHED: All patches applied (should not crash)
    - CPV: Single CPV excluded (should crash if POV triggers that CPV)
    """
    FULL_BASE = "fullbase"
    DELTA_BASE = "deltabase"
    DELTA_REF = "deltaref"
    ALL_PATCHED = "allpatched"
    CPV = "cpv"


@dataclass
class BuildVersion:
    """Represents a built variant of a benchmark.

    Attributes:
        benchmark_name: Name of the benchmark (e.g., "afc-curl-delta-01")
        lang: Programming language (e.g., "c", "jvm")
        mode: FULL or DELTA mode
        build_tag: Type of variant (FULL_BASE, DELTA_REF, CPV, etc.)
        commit: Git commit hash used for this build
        variant_project_name: Full variant name (e.g., "afc-curl-delta-01-cpv0")
        cpv_num: CPV number if build_tag is CPV, None otherwise
    """
    benchmark_name: str
    lang: str
    mode: BenchmarkMode
    build_tag: BuildTag
    commit: str
    variant_project_name: str
    cpv_num: Optional[int] = None

    @property
    def project_path(self) -> str:
        """Return the OSS-Fuzz project path for this variant."""
        return f"aixcc/{self.lang}/{self.variant_project_name}"

    def __str__(self) -> str:
        if self.build_tag == BuildTag.CPV:
            return f"{self.benchmark_name}:{self.build_tag.value}{self.cpv_num}"
        return f"{self.benchmark_name}:{self.build_tag.value}"
