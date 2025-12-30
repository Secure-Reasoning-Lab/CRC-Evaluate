"""Type definitions for the builder module.

This module defines core data structures for the unified OSS-Fuzz builder:
- VariantType: Enum for different variant types (deltabase, coverage, etc.)
- BuildConfig: Configuration for a single build
- BuildResult: Result of a build operation
"""

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional


class VariantType(Enum):
    """Type of variant to build.

    Validation variants (for POV verification):
    - FULL_BASE: Base commit for full mode (vulnerable version)
    - DELTA_BASE: Base commit for delta mode (pre-vulnerability)
    - DELTA_REF: Reference commit for delta mode (vulnerable)
    - ALL_PATCHED: All CPV patches applied (should not crash)
    - CPV: All patches except one CPV (crashes if POV triggers that CPV)

    Coverage variant:
    - COVERAGE: Coverage-instrumented build for coverage collection
    """

    FULL_BASE = "fullbase"
    DELTA_BASE = "deltabase"
    DELTA_REF = "deltaref"
    ALL_PATCHED = "allpatched"
    CPV = "cpv"
    COVERAGE = "coverage"

    def is_validation_variant(self) -> bool:
        """Check if this is a validation variant (not coverage)."""
        return self != VariantType.COVERAGE


class BenchmarkMode(Enum):
    """Benchmark testing mode."""

    FULL = "full"
    DELTA = "delta"


@dataclass
class BuildConfig:
    """Configuration for building a single variant.

    The core insight: patches are the primitive, not variant types.
    All builds follow: clone repo → checkout commit → apply patches → build

    Attributes:
        benchmark_name: Name of the benchmark (e.g., "sanity-mock-c-delta-01")
        variant_type: Type of variant (for naming only)
        commit: Git commit hash to checkout
        main_repo: Main repository URL
        benchmark_path: Path to benchmark directory
        mode: Benchmark mode (FULL or DELTA) - used in variant naming
        patches: List of patch files to apply (empty = no patches)
        output_dir: Output directory (None = shared oss-fuzz/build/, Path = per-trial)
        language: Programming language ("c", "cpp", "jvm")
        cpv_num: CPV number for CPV variants (None otherwise)
        sanitizer: Sanitizer to use (default: "address", coverage uses "coverage")
        engine: Fuzzing engine (default: "libfuzzer")
        timeout: Build timeout in seconds (default: 3600)
        repo_name: Optional repository name for caching
    """

    benchmark_name: str
    variant_type: VariantType
    commit: str
    main_repo: str
    benchmark_path: Path
    mode: Optional["BenchmarkMode"] = None
    patches: list[Path] = field(default_factory=list)
    output_dir: Optional[Path] = None
    language: str = "c"
    cpv_num: Optional[int] = None
    sanitizer: str = "address"
    engine: str = "libfuzzer"
    timeout: int = 3600
    repo_name: Optional[str] = None

    def __post_init__(self) -> None:
        """Validate and normalize configuration."""
        # Ensure benchmark_path is a Path
        if isinstance(self.benchmark_path, str):
            self.benchmark_path = Path(self.benchmark_path)

        # Ensure output_dir is a Path if provided
        if self.output_dir is not None and isinstance(self.output_dir, str):
            self.output_dir = Path(self.output_dir)

        # CPV variants require cpv_num
        if self.variant_type == VariantType.CPV and self.cpv_num is None:
            raise ValueError("cpv_num is required for CPV variants")

        # Coverage variants use coverage sanitizer
        if self.variant_type == VariantType.COVERAGE:
            self.sanitizer = "coverage"

    @property
    def variant_name(self) -> str:
        """Get the variant name for this build config.

        Naming convention:
        - Base variants include mode in type: {benchmark}-deltabase, {benchmark}-fullbase
        - Ref variants are mode-specific: {benchmark}-deltaref
        - Shared variants need mode prefix: {benchmark}-delta-allpatched, {benchmark}-full-cpv0

        Returns:
            Variant name (e.g., "benchmark-deltabase", "benchmark-delta-cpv0")
        """
        # Base and ref variants already have mode in type name
        if self.variant_type in (
            VariantType.FULL_BASE,
            VariantType.DELTA_BASE,
            VariantType.DELTA_REF,
        ):
            return f"{self.benchmark_name}-{self.variant_type.value}"

        # Shared variants need mode prefix to avoid conflicts
        mode_prefix = self.mode.value if self.mode else "delta"

        if self.variant_type == VariantType.CPV:
            return f"{self.benchmark_name}-{mode_prefix}-cpv{self.cpv_num}"

        return f"{self.benchmark_name}-{mode_prefix}-{self.variant_type.value}"

    @property
    def is_shared(self) -> bool:
        """Check if this is a shared build (not per-trial).

        Returns:
            True if output goes to shared oss-fuzz/build/ location
        """
        return self.output_dir is None


@dataclass
class BuildResult:
    """Result of a build operation.

    Attributes:
        config: The build configuration used
        success: Whether the build succeeded
        variant_name: Name of the variant (e.g., "benchmark-deltabase")
        build_path: Path to build output (if successful)
        error: Error message (if failed)
        elapsed_seconds: Time taken for the build
        cached: Whether the result was from cache
    """

    config: BuildConfig
    success: bool
    variant_name: str
    build_path: Optional[Path] = None
    error: Optional[str] = None
    elapsed_seconds: float = 0.0
    cached: bool = False

    @classmethod
    def from_cache(cls, config: BuildConfig, build_path: Path) -> "BuildResult":
        """Create a successful result from cache.

        Args:
            config: Build configuration
            build_path: Path to cached build output

        Returns:
            BuildResult indicating a cache hit
        """
        return cls(
            config=config,
            success=True,
            variant_name=config.variant_name,
            build_path=build_path,
            cached=True,
        )

    @classmethod
    def from_error(
        cls,
        config: BuildConfig,
        error: str,
        elapsed_seconds: float = 0.0,
    ) -> "BuildResult":
        """Create a failed result.

        Args:
            config: Build configuration
            error: Error message
            elapsed_seconds: Time taken before failure

        Returns:
            BuildResult indicating failure
        """
        return cls(
            config=config,
            success=False,
            variant_name=config.variant_name,
            error=error,
            elapsed_seconds=elapsed_seconds,
        )


@dataclass
class BuildPlan:
    """Plan for building multiple variants.

    Tracks which variants need to be built vs are already cached.

    Attributes:
        benchmark_name: Name of the benchmark
        configs: List of build configurations
        cached_variants: Set of variant names already cached
    """

    benchmark_name: str
    configs: list[BuildConfig] = field(default_factory=list)
    cached_variants: set[str] = field(default_factory=set)

    def add_config(self, config: BuildConfig) -> None:
        """Add a build configuration to the plan."""
        self.configs.append(config)

    def mark_cached(self, variant_name: str) -> None:
        """Mark a variant as already cached."""
        self.cached_variants.add(variant_name)

    @property
    def configs_to_build(self) -> list[BuildConfig]:
        """Get configs that need to be built (not cached)."""
        return [c for c in self.configs if c.variant_name not in self.cached_variants]

    @property
    def total_count(self) -> int:
        """Total number of variants in the plan."""
        return len(self.configs)

    @property
    def cached_count(self) -> int:
        """Number of variants already cached."""
        return len(self.cached_variants)

    @property
    def build_count(self) -> int:
        """Number of variants that need to be built."""
        return len(self.configs_to_build)
