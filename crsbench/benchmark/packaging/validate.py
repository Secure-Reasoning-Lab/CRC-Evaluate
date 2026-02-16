"""Validate benchmark format and structure.

Ensures benchmarks have required files and valid pkgs/ structure.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from crsbench.benchmark.packaging.workdir_parser import get_expected_source_dir
from crsbench.builder.types import BenchmarkMode
from crsbench.utils.logger import get_logger
from crsbench.validation.meta_adapter import MetaYamlAdapter

logger = get_logger(__name__)


@dataclass
class ValidationResult:
    """Result of benchmark validation."""

    valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        """Human-readable validation result."""
        if self.valid and not self.warnings:
            return "Valid"
        parts = []
        if self.errors:
            parts.append(f"Errors: {', '.join(self.errors)}")
        if self.warnings:
            parts.append(f"Warnings: {', '.join(self.warnings)}")
        return "; ".join(parts) if parts else "Valid"


def validate_benchmark(benchmark_path: Path) -> ValidationResult:
    """Validate benchmark format.

    Checks:
    - Required files exist (Dockerfile, project.yaml)
    - .aixcc/meta.yaml exists with required fields
    - If pkgs/ exists, tarball matches WORKDIR
    - project.yaml has main_repo for cloning

    Args:
        benchmark_path: Path to benchmark directory

    Returns:
        ValidationResult with valid status, errors, and warnings
    """
    errors: list[str] = []
    warnings: list[str] = []

    # Check required files
    dockerfile = benchmark_path / "Dockerfile"
    project_yaml = benchmark_path / "project.yaml"
    meta_yaml = benchmark_path / ".aixcc" / "meta.yaml"

    if not benchmark_path.is_dir():
        errors.append(f"Not a directory: {benchmark_path}")
        return ValidationResult(valid=False, errors=errors)

    if not dockerfile.exists():
        errors.append("Missing Dockerfile")
    if not project_yaml.exists():
        errors.append("Missing project.yaml")
    if not meta_yaml.exists():
        errors.append("Missing .aixcc/meta.yaml")

    if errors:
        return ValidationResult(valid=False, errors=errors, warnings=warnings)

    # Validate project.yaml content
    project_errors, project_warnings = _validate_project_yaml(project_yaml)
    errors.extend(project_errors)
    warnings.extend(project_warnings)

    # Validate meta.yaml content
    meta_errors, meta_warnings = _validate_meta_yaml(meta_yaml)
    errors.extend(meta_errors)
    warnings.extend(meta_warnings)

    # VAL-01: Check Dockerfile has COPY/ADD pkgs/ pattern
    dockerfile_errors = _validate_dockerfile_pkgs_pattern(dockerfile)
    errors.extend(dockerfile_errors)

    # VAL-02: Check pkgs/ exists and has correct tarballs
    pkgs_dir = benchmark_path / "pkgs"
    if not pkgs_dir.exists():
        errors.append("Missing pkgs/ directory (bundled source tarballs required)")
    else:
        pkgs_errors, pkgs_warnings = _validate_pkgs_dir(pkgs_dir, dockerfile)
        errors.extend(pkgs_errors)
        warnings.extend(pkgs_warnings)

        # Also run strict tarball naming validation
        tarball_result = validate_tarball_naming(benchmark_path)
        if not tarball_result.valid:
            errors.extend(tarball_result.errors)
        warnings.extend(tarball_result.warnings)

    # Check ref.diff for delta mode benchmarks
    ref_diff_warnings = _validate_ref_diff(benchmark_path, meta_yaml)
    warnings.extend(ref_diff_warnings)

    return ValidationResult(
        valid=len(errors) == 0,
        errors=errors,
        warnings=warnings,
    )


def _validate_project_yaml(project_yaml: Path) -> tuple[list[str], list[str]]:
    """Validate project.yaml content."""
    errors: list[str] = []
    warnings: list[str] = []

    try:
        content = yaml.safe_load(project_yaml.read_text())
        if not content:
            errors.append("project.yaml is empty")
            return errors, warnings

        if "main_repo" not in content:
            warnings.append("project.yaml missing main_repo (needed for bundling)")

    except yaml.YAMLError as e:
        errors.append(f"Invalid project.yaml: {e}")

    return errors, warnings


def _validate_meta_yaml(meta_yaml: Path) -> tuple[list[str], list[str]]:
    """Validate .aixcc/meta.yaml content.

    Supports two formats:
    1. Nested format (current standard):
       delta_mode:
         base_commit: abc123
         ref_commit: def456
       full_mode:
         base_commit: def456

    2. Flat format (legacy):
       base_commit: abc123
       ref_commit: def456  (optional for full-only)

    Note: ref_commit is only required for delta_mode. Full-only benchmarks
    don't need ref_commit because the base_commit IS the vulnerable state.
    """
    errors: list[str] = []
    warnings: list[str] = []

    try:
        content = yaml.safe_load(meta_yaml.read_text())
        if not content:
            errors.append("meta.yaml is empty")
            return errors, warnings

        # Check for nested format (current standard)
        has_delta_mode = "delta_mode" in content and content["delta_mode"]
        has_full_mode = "full_mode" in content and content["full_mode"]

        if has_delta_mode or has_full_mode:
            # Nested format validation
            if has_delta_mode:
                delta = content["delta_mode"]
                if not delta.get("base_commit"):
                    errors.append("meta.yaml delta_mode missing base_commit")
                if not delta.get("ref_commit"):
                    errors.append("meta.yaml delta_mode missing ref_commit")
            if has_full_mode:
                full = content["full_mode"]
                if not full.get("base_commit"):
                    errors.append("meta.yaml full_mode missing base_commit")
                # Note: full_mode does NOT require ref_commit
                # The base_commit IS the vulnerable state for full-only benchmarks
        else:
            # Flat format validation (legacy)
            if "base_commit" not in content:
                errors.append(
                    "meta.yaml missing base_commit (and no delta_mode/full_mode sections)"
                )
            # Note: ref_commit is optional in flat format (might be full-only)
            if "ref_commit" not in content:
                warnings.append(
                    "meta.yaml missing ref_commit (only supports full mode, not delta)"
                )

    except yaml.YAMLError as e:
        errors.append(f"Invalid meta.yaml: {e}")

    return errors, warnings


def _validate_dockerfile_pkgs_pattern(dockerfile: Path) -> list[str]:
    """Check that Dockerfile contains COPY pkgs/ or ADD pkgs/ pattern.

    Standard Docker syntax is case-sensitive (COPY/ADD uppercase).
    """
    errors: list[str] = []

    content = dockerfile.read_text()
    # Match uncommented COPY/ADD pkgs/ lines
    pattern = re.compile(r"^(?!#)\s*(?:COPY|ADD)\s+pkgs/", re.MULTILINE)
    if not pattern.search(content):
        errors.append(
            "Dockerfile missing COPY/ADD pkgs/ pattern "
            "(source must be embedded from pkgs/)"
        )

    return errors


def _validate_pkgs_dir(pkgs_dir: Path, dockerfile: Path) -> tuple[list[str], list[str]]:
    """Validate pkgs/ directory structure.

    Returns:
        Tuple of (errors, warnings).
    """
    errors: list[str] = []
    warnings: list[str] = []

    expected_name = get_expected_source_dir(dockerfile)
    if not expected_name:
        warnings.append(
            "Could not determine expected tarball name from Dockerfile WORKDIR"
        )
        return errors, warnings

    expected_tarball = pkgs_dir / f"{expected_name}.tar.gz"
    if not expected_tarball.exists():
        errors.append(f"Expected source tarball not found: {expected_name}.tar.gz")

    # Check pkg_refs.txt for provenance
    pkg_refs = pkgs_dir / "pkg_refs.txt"
    if not pkg_refs.exists():
        warnings.append("pkgs/pkg_refs.txt not found (provenance tracking)")

    return errors, warnings


@dataclass
class TarballValidationResult:
    """Result of tarball naming validation."""

    valid: bool
    expected_name: Optional[str] = None
    found_tarballs: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def validate_tarball_naming(benchmark_path: Path) -> TarballValidationResult:
    """Validate that source tarball name matches Dockerfile WORKDIR.

    This is the strict validation for TAR-04/VAL-01/VAL-03 requirements.
    Returns errors (not warnings) for naming mismatches.

    Args:
        benchmark_path: Path to benchmark directory

    Returns:
        TarballValidationResult with validation status and details
    """
    errors: list[str] = []
    warnings: list[str] = []
    found_tarballs: list[str] = []

    pkgs_dir = benchmark_path / "pkgs"
    dockerfile = benchmark_path / "Dockerfile"

    # pkgs/ directory is required for bundled source
    if not pkgs_dir.exists():
        return TarballValidationResult(
            valid=False,
            errors=["Missing pkgs/ directory (bundled source tarballs required)"],
        )

    # Get expected tarball name from WORKDIR
    expected_name = get_expected_source_dir(dockerfile)
    if not expected_name:
        return TarballValidationResult(
            valid=False,
            errors=["Cannot determine expected tarball name from Dockerfile WORKDIR"],
        )

    # Source tarball should match WORKDIR name
    expected_tarball = pkgs_dir / f"{expected_name}.tar.gz"
    all_tarballs = list(pkgs_dir.glob("*.tar.gz"))

    # Collect tarball names
    for tb in all_tarballs:
        found_tarballs.append(tb.name)

    if not expected_tarball.exists():
        # Check for misnamed source tarball
        # Look for tarballs that might be the source (excluding known dependencies)
        dependency_patterns = [
            "fuzzing",
            "corpus",
            "samples",
            "testing",
            "maven",
            "jdk",
            "openjdk",
        ]

        potential_source_tarballs = []
        for tb in all_tarballs:
            name_lower = tb.stem.lower()
            is_dependency = any(pat in name_lower for pat in dependency_patterns)
            if not is_dependency:
                potential_source_tarballs.append(tb.name)

        if potential_source_tarballs:
            errors.append(
                f"Tarball naming mismatch: expected '{expected_name}.tar.gz' "
                f"but found: {', '.join(potential_source_tarballs)}"
            )
        else:
            errors.append(f"Expected source tarball not found: {expected_name}.tar.gz")

    return TarballValidationResult(
        valid=len(errors) == 0,
        expected_name=expected_name,
        found_tarballs=found_tarballs,
        errors=errors,
        warnings=warnings,
    )


def _validate_ref_diff(benchmark_path: Path, meta_yaml: Path) -> list[str]:
    """Validate ref.diff exists for delta mode benchmarks.

    Delta mode benchmarks need ref.diff to transform base_commit to ref_commit
    (the vulnerable state). Without ref.diff, delta mode won't work properly.
    """
    warnings: list[str] = []

    try:
        content = yaml.safe_load(meta_yaml.read_text())
        if not content:
            return warnings

        # Check if benchmark supports delta mode
        has_delta_mode = "delta_mode" in content and content["delta_mode"]

        # Also check flat format with ref_commit (legacy delta mode indicator)
        if not has_delta_mode and "ref_commit" in content:
            has_delta_mode = True

        if has_delta_mode:
            ref_diff = benchmark_path / ".aixcc" / "ref.diff"
            if not ref_diff.exists():
                warnings.append(
                    "Delta mode benchmark missing .aixcc/ref.diff "
                    "(run 'crsbench benchmark prepare-delta' to generate)"
                )

    except yaml.YAMLError:
        # meta.yaml parsing errors are already caught in _validate_meta_yaml
        pass

    return warnings


def get_benchmark_info(
    benchmark_path: Path,
) -> Optional[dict[str, str | bool]]:
    """Extract benchmark info for bundling.

    Uses MetaYamlAdapter as the single source of truth for benchmark metadata.

    For bundling, we need to know:
    - If benchmark has delta_mode: use delta_mode.base_commit, generate ref.diff
    - If full-only: use full_mode.base_commit, no ref.diff needed

    Args:
        benchmark_path: Path to benchmark directory

    Returns:
        Dict with main_repo, base_commit, has_delta_mode, and optionally ref_commit.
        Returns None if invalid.
    """
    adapter = MetaYamlAdapter.from_benchmark_path(benchmark_path)
    if not adapter:
        return None

    if not adapter.main_repo:
        return None

    try:
        base_commit = adapter.get_base_commit()
    except ValueError:
        return None

    has_delta_mode = adapter.get_mode() == BenchmarkMode.DELTA
    ref_commit = adapter.get_ref_commit()  # None for full mode

    info: dict[str, str | bool] = {
        "main_repo": adapter.main_repo,
        "base_commit": base_commit,
        "has_delta_mode": has_delta_mode,
    }

    if ref_commit:
        info["ref_commit"] = ref_commit

    return info
