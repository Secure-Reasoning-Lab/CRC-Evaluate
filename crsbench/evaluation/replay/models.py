from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from pathlib import Path

ReplayOutcome = Literal[
    "crash",
    "no_crash",
    "timeout",
    "error",
    "build_error",
    "missing_mapping",
    "unsupported_mapping",
    "target_project_missing",
]


@dataclass(frozen=True)
class SourcePovRecord:
    source_id: str
    source_dir: Path
    experiment_name: str
    trial_relative_path: str
    benchmark: str
    source_harness: str
    source_sanitizer: str | None
    original_pov_path: Path
    original_pov_relpath: str
    pov_filename: str
    pov_content_hash: str


@dataclass(frozen=True)
class ReplayTask:
    mapped_project: str
    sanitizer: str
    target_harness: str
    pov_content_hash: str
    pov_path: Path
    source_records: tuple[SourcePovRecord, ...]


@dataclass(frozen=True)
class SessionReplayResult:
    exit_code: int | None
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool
    session_restarted: bool
    crashed: bool | None
    error_message: str | None = None


@dataclass(frozen=True)
class ReplayResult:
    mapped_project: str
    sanitizer: str
    target_harness: str
    pov_content_hash: str
    outcome: ReplayOutcome
    exit_code: int | None
    duration_seconds: float
    artifact_dir: Path | None
    stdout_path: Path | None
    stderr_path: Path | None
    sanitizer_log_path: Path | None
    session_restarted: bool
    standard_zero_day: bool = False
    error_message: str | None = None
