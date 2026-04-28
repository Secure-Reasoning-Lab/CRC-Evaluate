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


def sanitizer_short_name(sanitizer: str) -> str:
    """Convert sanitizer name to short form for variant names.

    Args:
        sanitizer: Full sanitizer name (e.g., "address", "undefined")

    Returns:
        Short form for use in variant names (e.g., "asan", "ubsan")
    """
    mapping = {
        "address": "asan",
        "undefined": "ubsan",
        "memory": "msan",
        "thread": "tsan",
        "coverage": "cov",
    }
    return mapping.get(sanitizer, sanitizer)


def sanitizer_to_inc_tag(san_short: str) -> str:
    """Convert short sanitizer name back to full name for inc-build image tags.

    Inc-build images use full sanitizer names as Docker tags.
    Example: curl:address (not curl:asan)

    Args:
        san_short: Short sanitizer name from variant name (e.g., "asan")

    Returns:
        Full sanitizer name for Docker tag (e.g., "address")
    """
    mapping = {
        "asan": "address",
        "ubsan": "undefined",
        "msan": "memory",
        "tsan": "thread",
        "coverage": "coverage",
    }
    return mapping.get(san_short, san_short)


class VariantType(Enum):
    """Type of variant to build.

    Validation variants (for POV verification):
    - FULL_BASE: Base commit for full mode (vulnerable version)
    - DELTA_REF: Reference commit for delta mode (vulnerable)
    - ALL_PATCHED: All CPV patches applied (should not crash)
    - CPV: All patches except one CPV (crashes if POV triggers that CPV)

    Patch verification variant:
    - PATCHED: CRS-generated patch applied (for patch verification)

    Coverage variant:
    - COVERAGE: Coverage-instrumented build for coverage collection
    """

    FULL_BASE = "fullbase"
    DELTA_REF = "deltaref"
    ALL_PATCHED = "allpatched"
    CPV = "cpv"
    PATCHED = "patched"
    COVERAGE = "coverage"

    def is_validation_variant(self) -> bool:
        """Check if this is a POV validation variant."""
        return self not in (VariantType.COVERAGE, VariantType.PATCHED)

    def is_patch_variant(self) -> bool:
        """Check if this is a patch verification variant."""
        return self == VariantType.PATCHED

    def is_inc_build_target(self) -> bool:
        """Whether this variant uses the inc-build image's commit.

        Inc-build images are built for ref_commit (delta mode) or base_commit
        (full mode). COVERAGE uses different instrumentation, not inc-build.
        """
        return self != VariantType.COVERAGE

    def supports_inc_build(self) -> bool:
        """Check if this variant type supports incremental builds.

        Inc-build uses pre-built Docker images with cached dependencies,
        enabling faster builds by skipping dependency downloads.

        Supported: All validation variants + PATCHED
        Not supported: COVERAGE (requires different instrumentation)

        Returns:
            True if inc-build is supported for this variant type.
        """
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
        patch_id: Patch identifier for PATCHED variants (e.g., "patch_0")
        pov_id: Source POV/CPV identifier for PATCHED variants (e.g., "pov_0", "cpv_0")
        use_inc_build: Whether to use incremental build (for patch verification)
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
    patch_id: Optional[str] = None
    pov_id: Optional[str] = None
    use_inc_build: bool = False
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

        # PATCHED variants require patch_id and pov_id
        if self.variant_type == VariantType.PATCHED:
            if self.patch_id is None:
                raise ValueError("patch_id is required for PATCHED variants")
            if self.pov_id is None:
                raise ValueError("pov_id is required for PATCHED variants")

        # Coverage variants use coverage sanitizer
        if self.variant_type == VariantType.COVERAGE:
            self.sanitizer = "coverage"

    @property
    def variant_name(self) -> str:
        """Get the variant name for this build config.

        Naming convention (includes sanitizer for multi-sanitizer support):
        - Format: {benchmark}-{san_short}-{variant_suffix}
        - Base variants: {benchmark}-{san_short}-{type} (e.g., afc-curl-asan-deltabase)
        - Ref variants: {benchmark}-{san_short}-deltaref
        - Shared variants: {benchmark}-{san_short}-{mode}-{type} (e.g., afc-curl-asan-delta-cpv0)
        - Patch variants: {benchmark}-{san_short}-{mode}-patched-{pov_id}-{patch_id}

        Returns:
            Variant name (e.g., "benchmark-asan-deltabase", "benchmark-ubsan-delta-cpv0")
        """
        san_short = sanitizer_short_name(self.sanitizer)

        # Base and ref variants already have mode in type name
        if self.variant_type in (
            VariantType.FULL_BASE,
            VariantType.DELTA_REF,
        ):
            return f"{self.benchmark_name}-{san_short}-{self.variant_type.value}"

        # Shared variants need mode prefix to avoid conflicts
        mode_prefix = self.mode.value if self.mode else "delta"

        if self.variant_type == VariantType.CPV:
            return f"{self.benchmark_name}-{san_short}-{mode_prefix}-cpv{self.cpv_num}"

        # Patch variants include pov_id and patch_id for isolation
        if self.variant_type == VariantType.PATCHED:
            return f"{self.benchmark_name}-{san_short}-{mode_prefix}-patched-{self.pov_id}-{self.patch_id}"

        return (
            f"{self.benchmark_name}-{san_short}-{mode_prefix}-{self.variant_type.value}"
        )

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
        fallback_used: Whether inc-build fell back to standard build
        stdout: Build stdout output
        stderr: Build stderr output
    """

    config: BuildConfig
    success: bool
    variant_name: str
    build_path: Optional[Path] = None
    error: Optional[str] = None
    elapsed_seconds: float = 0.0
    cached: bool = False
    fallback_used: bool = False
    stdout: str = ""
    stderr: str = ""

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


# Marker file name for build metadata
BUILD_METADATA_FILE = ".build-meta.json"


@dataclass
class BuildMetadata:
    """Metadata about a build stored in the build output directory.

    Used to track how a build was created, enabling cache validation
    when switching between build modes (e.g., standard vs inc-build).

    Attributes:
        inc_build: Whether this was an incremental build
        sanitizer: Sanitizer used for the build
        timestamp: Build timestamp (ISO format)
        fallback_used: Whether fallback to clean build was used (inc-build failed)
        project_fingerprint: Deterministic fingerprint for the source project tree
            used by plain OSS-Fuzz project builds
    """

    inc_build: bool = False
    sanitizer: str = "address"
    timestamp: str = ""
    fallback_used: bool = False
    project_fingerprint: str = ""

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "inc_build": self.inc_build,
            "sanitizer": self.sanitizer,
            "timestamp": self.timestamp,
            "fallback_used": self.fallback_used,
            "project_fingerprint": self.project_fingerprint,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "BuildMetadata":
        """Create from dictionary."""
        return cls(
            inc_build=data.get("inc_build", False),
            sanitizer=data.get("sanitizer", "address"),
            timestamp=data.get("timestamp", ""),
            fallback_used=data.get("fallback_used", False),
            project_fingerprint=data.get("project_fingerprint", ""),
        )


@dataclass
class FuzzerBuildResult:
    """Result of build_fuzzers operation.

    Tracks whether the build succeeded and if fallback was used.

    Attributes:
        success: Whether the build succeeded
        fallback_used: Whether fallback to clean build was used (inc-build failed)
        stdout: Build stdout output
        stderr: Build stderr output
    """

    success: bool
    fallback_used: bool = False
    stdout: str = ""
    stderr: str = ""


@dataclass
class BuildPlan:
    """Plan for building multiple variants.

    Tracks which variants need to be built vs are already cached.
    Encapsulates inc-build configuration at the plan level.

    Attributes:
        benchmark_name: Name of the benchmark
        configs: List of build configurations
        cached_variants: Set of variant names already cached
        use_inc_build: Whether to use incremental builds (plan-level setting)
    """

    benchmark_name: str
    configs: list[BuildConfig] = field(default_factory=list)
    cached_variants: set[str] = field(default_factory=set)
    use_inc_build: bool = False

    def add_config(self, config: BuildConfig) -> None:
        """Add a pre-configured BuildConfig to the plan.

        Use this when you need full control over the config.
        For automatic inc-build handling, use add_variant() instead.
        """
        self.configs.append(config)

    def add_variant(
        self,
        variant_type: "VariantType",
        commit: str,
        main_repo: str,
        benchmark_path: Path,
        mode: Optional["BenchmarkMode"] = None,
        patches: Optional[list[Path]] = None,
        language: str = "c",
        repo_name: Optional[str] = None,
        cpv_num: Optional[int] = None,
        patch_id: Optional[str] = None,
        pov_id: Optional[str] = None,
        sanitizer: str = "address",
    ) -> None:
        """Add a variant to the plan with automatic inc-build configuration.

        Uses self.benchmark_name for the variant's benchmark name.
        The use_inc_build is automatically determined from:
        1. Plan-level use_inc_build setting
        2. VariantType.supports_inc_build()
        """
        # Determine use_inc_build for this variant
        variant_use_inc = self.use_inc_build and variant_type.supports_inc_build()

        config = BuildConfig(
            benchmark_name=self.benchmark_name,
            variant_type=variant_type,
            commit=commit,
            main_repo=main_repo,
            benchmark_path=benchmark_path,
            mode=mode,
            patches=patches or [],
            language=language,
            repo_name=repo_name,
            cpv_num=cpv_num,
            patch_id=patch_id,
            pov_id=pov_id,
            use_inc_build=variant_use_inc,
            sanitizer=sanitizer,
        )
        self.add_config(config)

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


@dataclass
class ReproduceOutput:
    """Output from reproduce operation.

    Attributes:
        crashed: True if the POV caused a crash
        stdout: Standard output from reproduce (contains crash log if crashed)
        stderr: Standard error from reproduce
        exit_code: Exit code from reproduce command
    """

    crashed: bool
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
