"""Validate benchmark format and structure.

Ensures benchmarks have required files and valid pkgs/ structure.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from crsbench.benchmark.packaging.workdir_parser import get_expected_source_dir
from crsbench.utils.logger import get_logger

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

    # Check pkgs/ structure if exists
    pkgs_dir = benchmark_path / "pkgs"
    if pkgs_dir.exists():
        pkgs_warnings = _validate_pkgs_dir(pkgs_dir, dockerfile)
        warnings.extend(pkgs_warnings)

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
       ref_commit: def456
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
        else:
            # Flat format validation (legacy)
            if "base_commit" not in content:
                errors.append(
                    "meta.yaml missing base_commit (and no delta_mode/full_mode sections)"
                )
            if "ref_commit" not in content:
                warnings.append("meta.yaml missing ref_commit (full mode assumed)")

    except yaml.YAMLError as e:
        errors.append(f"Invalid meta.yaml: {e}")

    return errors, warnings


def _validate_pkgs_dir(pkgs_dir: Path, dockerfile: Path) -> list[str]:
    """Validate pkgs/ directory structure."""
    warnings: list[str] = []

    expected_name = get_expected_source_dir(dockerfile)
    if not expected_name:
        warnings.append(
            "Could not determine expected tarball name from Dockerfile WORKDIR"
        )
        return warnings

    expected_tarball = pkgs_dir / f"{expected_name}.tar.gz"
    if not expected_tarball.exists():
        # Check for any tarballs
        tarballs = list(pkgs_dir.glob("*.tar.gz"))
        if tarballs:
            warnings.append(
                f"pkgs/ has tarball(s) but expected {expected_tarball.name} "
                f"(found: {', '.join(t.name for t in tarballs)})"
            )
        else:
            warnings.append("pkgs/ exists but no tarballs found")

    # Check pkg_refs.txt for provenance
    pkg_refs = pkgs_dir / "pkg_refs.txt"
    if not pkg_refs.exists():
        warnings.append("pkgs/pkg_refs.txt not found (provenance tracking)")

    return warnings


def get_benchmark_info(
    benchmark_path: Path,
    *,
    mode: str = "delta",
) -> Optional[dict[str, str]]:
    """Extract benchmark info for bundling.

    Supports two meta.yaml formats:
    1. Nested format: delta_mode.base_commit, full_mode.base_commit
    2. Flat format: base_commit, ref_commit

    Args:
        benchmark_path: Path to benchmark directory
        mode: "delta" or "full" - determines which commits to extract

    Returns:
        Dict with main_repo, base_commit, ref_commit (optional), or None if invalid
    """
    project_yaml = benchmark_path / "project.yaml"
    meta_yaml = benchmark_path / ".aixcc" / "meta.yaml"

    if not project_yaml.exists() or not meta_yaml.exists():
        return None

    try:
        project = yaml.safe_load(project_yaml.read_text())
        meta = yaml.safe_load(meta_yaml.read_text())

        if not project or not meta:
            return None

        main_repo = project.get("main_repo")
        if not main_repo:
            return None

        # Try nested format first (current standard)
        has_delta_mode = "delta_mode" in meta and meta["delta_mode"]
        has_full_mode = "full_mode" in meta and meta["full_mode"]

        if has_delta_mode or has_full_mode:
            # Nested format
            if mode == "delta" and has_delta_mode:
                delta = meta["delta_mode"]
                base_commit = delta.get("base_commit")
                ref_commit = delta.get("ref_commit")
            elif mode == "full" and has_full_mode:
                full = meta["full_mode"]
                base_commit = full.get("base_commit")
                ref_commit = None
            elif has_delta_mode:
                # Fallback to delta if requested mode not available
                delta = meta["delta_mode"]
                base_commit = delta.get("base_commit")
                ref_commit = delta.get("ref_commit")
            else:
                # Fallback to full
                full = meta["full_mode"]
                base_commit = full.get("base_commit")
                ref_commit = None
        else:
            # Flat format (legacy)
            base_commit = meta.get("base_commit")
            ref_commit = meta.get("ref_commit")

        if not base_commit:
            return None

        info: dict[str, str] = {
            "main_repo": main_repo,
            "base_commit": base_commit,
        }

        if ref_commit:
            info["ref_commit"] = ref_commit

        return info

    except yaml.YAMLError:
        return None
