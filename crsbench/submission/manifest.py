"""Validation and local registry generation for CRC submission manifests."""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, TypeAlias, cast

import yaml
from oss_crs.src.config.crs import CRSConfig
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

MANIFEST_FILENAME = "submission.yaml"
_TEAM_ID_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,46}[a-z0-9])?$")
CrsType: TypeAlias = Literal["bug-finding", "bug-fixing"]


class SubmissionError(ValueError):
    """Raised when a submission cannot be safely registered."""


class SubmissionMetadata(BaseModel):
    """Human-readable submission metadata."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("submission.name must not be empty")
        return normalized


class SubmissionCrsReference(BaseModel):
    """Repository-relative path to one CRS source directory."""

    model_config = ConfigDict(extra="forbid")

    path: str

    @field_validator("path")
    @classmethod
    def validate_relative_path(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("CRS path must not be empty")
        if "\x00" in normalized or "\\" in normalized:
            raise ValueError("CRS path must use repository-relative POSIX syntax")
        if re.match(r"^[A-Za-z]:", normalized):
            raise ValueError("CRS path must not be absolute")

        candidate = PurePosixPath(normalized)
        if candidate.is_absolute() or normalized == ".":
            raise ValueError("CRS path must point below the submission root")
        if any(part in {"", ".", ".."} for part in normalized.split("/")):
            raise ValueError("CRS path must not contain empty, '.' or '..' segments")
        return candidate.as_posix()


class SubmissionCrsSelection(BaseModel):
    """Finder and Patcher selected for evaluation."""

    model_config = ConfigDict(extra="forbid")

    finder: SubmissionCrsReference
    patcher: SubmissionCrsReference


class SubmissionManifest(BaseModel):
    """Versioned CRC submission manifest."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    submission: SubmissionMetadata
    crs: SubmissionCrsSelection


@dataclass(frozen=True)
class ValidatedCrs:
    """One selected CRS after source and metadata validation."""

    role: Literal["finder", "patcher"]
    path: Path
    crs_yaml: Path
    name: str
    crs_type: CrsType
    required_llms: tuple[str, ...]


@dataclass(frozen=True)
class ValidatedSubmission:
    """Validated submission ready for evaluator-local registration."""

    root: Path
    manifest_path: Path
    name: str
    finder: ValidatedCrs
    patcher: ValidatedCrs


@dataclass(frozen=True)
class RegisteredSubmission:
    """Registry entries generated for a validated submission."""

    submission: ValidatedSubmission
    registry_dir: Path
    finder_registry_name: str
    patcher_registry_name: str
    finder_registry_path: Path
    patcher_registry_path: Path


def _load_yaml_mapping(path: Path, description: str) -> dict[str, object]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SubmissionError(f"Could not read {description} {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise SubmissionError(f"Invalid YAML in {description} {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise SubmissionError(f"{description} must contain a YAML object: {path}")
    return raw


def _resolve_crs_path(root: Path, relative_path: str, role: str) -> Path:
    try:
        candidate = (root / Path(*PurePosixPath(relative_path).parts)).resolve(
            strict=True
        )
    except OSError as exc:
        raise SubmissionError(
            f"Selected {role} CRS path does not exist: {relative_path}"
        ) from exc

    if not candidate.is_relative_to(root):
        raise SubmissionError(
            f"Selected {role} CRS path escapes the submission root: {relative_path}"
        )
    if not candidate.is_dir():
        raise SubmissionError(
            f"Selected {role} CRS path is not a directory: {relative_path}"
        )
    return candidate


def _load_validated_crs(
    root: Path,
    role: Literal["finder", "patcher"],
    relative_path: str,
) -> ValidatedCrs:
    crs_path = _resolve_crs_path(root, relative_path, role)
    crs_yaml = crs_path / "oss-crs" / "crs.yaml"
    try:
        resolved_crs_yaml = crs_yaml.resolve(strict=True)
    except OSError as exc:
        raise SubmissionError(
            f"Selected {role} CRS is missing oss-crs/crs.yaml: {relative_path}"
        ) from exc

    if (
        not resolved_crs_yaml.is_relative_to(crs_path)
        or not resolved_crs_yaml.is_file()
    ):
        raise SubmissionError(
            f"Selected {role} CRS has an invalid oss-crs/crs.yaml: {relative_path}"
        )

    data = _load_yaml_mapping(resolved_crs_yaml, "CRS configuration")
    try:
        config = CRSConfig.model_validate(data)
    except ValueError as exc:
        raise SubmissionError(
            f"Invalid OSS-CRS configuration {resolved_crs_yaml}: {exc}"
        ) from exc

    if not config.name.strip():
        raise SubmissionError(
            f"CRS name must be a non-empty string: {resolved_crs_yaml}"
        )

    crs_types = [value.value for value in config.type]
    if len(crs_types) != 1 or crs_types[0] not in {"bug-finding", "bug-fixing"}:
        raise SubmissionError(
            "Selected CRS must declare exactly one supported type "
            f"(bug-finding or bug-fixing): {resolved_crs_yaml}"
        )
    crs_type = cast("CrsType", crs_types[0])
    expected_type: CrsType = "bug-finding" if role == "finder" else "bug-fixing"
    if crs_type != expected_type:
        raise SubmissionError(
            f"Selected {role} CRS must declare type '{expected_type}', got "
            f"'{crs_type}': {resolved_crs_yaml}"
        )

    raw_required_llms = data.get("required_llms", [])
    required_llms = [] if raw_required_llms is None else raw_required_llms
    if not isinstance(required_llms, list):
        raise SubmissionError(
            f"required_llms must be a list of non-empty strings: {resolved_crs_yaml}"
        )
    normalized_required_llms: list[str] = []
    for item in required_llms:
        if not isinstance(item, str) or not item.strip():
            raise SubmissionError(
                "required_llms must be a list of non-empty strings: "
                f"{resolved_crs_yaml}"
            )
        normalized_required_llms.append(item.strip())

    return ValidatedCrs(
        role=role,
        path=crs_path,
        crs_yaml=resolved_crs_yaml,
        name=config.name.strip(),
        crs_type=expected_type,
        required_llms=tuple(dict.fromkeys(normalized_required_llms)),
    )


def load_submission(submission_root: Path) -> ValidatedSubmission:
    """Load and validate a CRC-Template-compatible submission directory."""
    try:
        root = submission_root.expanduser().resolve(strict=True)
    except OSError as exc:
        raise SubmissionError(
            f"Submission root does not exist: {submission_root}"
        ) from exc
    if not root.is_dir():
        raise SubmissionError(f"Submission root is not a directory: {root}")

    manifest_path = root / MANIFEST_FILENAME
    if not manifest_path.is_file():
        raise SubmissionError(f"Submission manifest not found: {manifest_path}")
    if manifest_path.is_symlink():
        raise SubmissionError(
            f"Submission manifest must not be a symlink: {manifest_path}"
        )

    raw_manifest = _load_yaml_mapping(manifest_path, "submission manifest")
    try:
        manifest = SubmissionManifest.model_validate(raw_manifest)
    except ValidationError as exc:
        raise SubmissionError(
            f"Invalid submission manifest {manifest_path}: {exc}"
        ) from exc

    finder = _load_validated_crs(root, "finder", manifest.crs.finder.path)
    patcher = _load_validated_crs(root, "patcher", manifest.crs.patcher.path)
    if finder.path == patcher.path:
        raise SubmissionError("Finder and Patcher must use different CRS directories")

    return ValidatedSubmission(
        root=root,
        manifest_path=manifest_path,
        name=manifest.submission.name,
        finder=finder,
        patcher=patcher,
    )


def validate_team_id(team_id: str) -> str:
    """Validate an evaluator-owned registry namespace."""
    normalized = team_id.strip()
    if not _TEAM_ID_PATTERN.fullmatch(normalized):
        raise SubmissionError(
            "team ID must be 1-48 lowercase letters, digits or hyphens, "
            "and must start and end with a letter or digit"
        )
    return normalized


def _registry_yaml(registry_name: str, crs: ValidatedCrs) -> str:
    data = {
        "name": registry_name,
        "type": [crs.crs_type],
        "source": {"local_path": str(crs.path)},
    }
    return yaml.safe_dump(data, sort_keys=False)


def _write_registry_entries(entries: dict[Path, str]) -> None:
    staged: list[tuple[Path, Path]] = []
    try:
        for target, content in entries.items():
            file_descriptor, temporary_name = tempfile.mkstemp(
                dir=target.parent,
                prefix=f".{target.name}.",
            )
            temporary_path = Path(temporary_name)
            staged.append((temporary_path, target))
            with os.fdopen(file_descriptor, "w", encoding="utf-8") as temporary_file:
                temporary_file.write(content)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())

        for temporary_path, target in staged:
            temporary_path.replace(target)
    except OSError as exc:
        raise SubmissionError(f"Could not write registry entries: {exc}") from exc
    finally:
        for temporary_path, _ in staged:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def register_submission(
    submission_root: Path,
    *,
    team_id: str,
    registry_dir: Path,
    force: bool = False,
) -> RegisteredSubmission:
    """Validate a submission and generate namespaced local registry entries."""
    submission = load_submission(submission_root)
    namespace = validate_team_id(team_id)
    resolved_registry_dir = registry_dir.expanduser().resolve()
    try:
        resolved_registry_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SubmissionError(
            f"Could not create registry directory {resolved_registry_dir}: {exc}"
        ) from exc
    if not resolved_registry_dir.is_dir():
        raise SubmissionError(
            f"Registry output path is not a directory: {resolved_registry_dir}"
        )

    finder_registry_name = f"{namespace}-finder"
    patcher_registry_name = f"{namespace}-patcher"
    finder_registry_path = resolved_registry_dir / f"{finder_registry_name}.yaml"
    patcher_registry_path = resolved_registry_dir / f"{patcher_registry_name}.yaml"
    existing = [
        path
        for path in (finder_registry_path, patcher_registry_path)
        if path.exists() or path.is_symlink()
    ]
    if existing and not force:
        paths = ", ".join(str(path) for path in existing)
        raise SubmissionError(
            f"Registry entries already exist: {paths}. Use --force to replace them."
        )

    invalid_targets = [path for path in existing if path.is_dir()]
    if invalid_targets:
        paths = ", ".join(str(path) for path in invalid_targets)
        raise SubmissionError(f"Registry entry paths must not be directories: {paths}")

    _write_registry_entries(
        {
            finder_registry_path: _registry_yaml(
                finder_registry_name, submission.finder
            ),
            patcher_registry_path: _registry_yaml(
                patcher_registry_name, submission.patcher
            ),
        }
    )

    return RegisteredSubmission(
        submission=submission,
        registry_dir=resolved_registry_dir,
        finder_registry_name=finder_registry_name,
        patcher_registry_name=patcher_registry_name,
        finder_registry_path=finder_registry_path,
        patcher_registry_path=patcher_registry_path,
    )
