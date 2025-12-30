"""Variant models for POV validation.

This module defines the core data structures for managing benchmark variants
used in POV (Proof of Vulnerability) validation.
"""

from dataclasses import dataclass
from typing import Optional

from crsbench.builder.types import BenchmarkMode, VariantType


@dataclass
class BuildVersion:
    """Represents a built variant of a benchmark.

    Attributes:
        benchmark_name: Name of the benchmark (e.g., "afc-curl-delta-01")
        lang: Programming language (e.g., "c", "jvm")
        mode: FULL or DELTA mode
        variant_type: Type of variant (FULL_BASE, DELTA_REF, CPV, etc.)
        commit: Git commit hash used for this build
        variant_project_name: Full variant name (e.g., "afc-curl-delta-01-cpv0")
        cpv_num: CPV number if variant_type is CPV, None otherwise
    """

    benchmark_name: str
    lang: str
    mode: BenchmarkMode
    variant_type: VariantType
    commit: str
    variant_project_name: str
    cpv_num: Optional[int] = None

    @property
    def project_path(self) -> str:
        """Return the OSS-Fuzz project path for this variant."""
        return self.variant_project_name

    def __str__(self) -> str:
        if self.variant_type == VariantType.CPV:
            return f"{self.benchmark_name}:{self.variant_type.value}{self.cpv_num}"
        return f"{self.benchmark_name}:{self.variant_type.value}"
