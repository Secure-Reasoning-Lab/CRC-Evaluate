"""Canonical provider-neutral cloud preflight report models."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from enum import StrEnum
from typing import Any, Mapping, Sequence


class CloudPreflightCheckStatus(StrEnum):
    """Canonical status for one preflight check."""

    PASS = "pass"
    WARNING = "warning"
    FAIL = "fail"


class CloudPreflightVerdict(StrEnum):
    """Canonical top-level preflight verdict."""

    READY = "ready"
    WARNING = "warning"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class CloudPreflightCheck:
    """One named preflight check result."""

    name: str
    status: CloudPreflightCheckStatus | str
    summary: str
    detail: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", _coerce_check_status(self.status))

    @property
    def is_failure(self) -> bool:
        return self.status is CloudPreflightCheckStatus.FAIL

    @property
    def is_warning(self) -> bool:
        return self.status is CloudPreflightCheckStatus.WARNING


@dataclass(frozen=True)
class CloudPreflightReport:
    """Canonical provider-neutral cloud preflight report."""

    experiment: str
    provider: str
    plan: Mapping[str, Any]
    resolved_defaults: Mapping[str, Any]
    env_summary: Mapping[str, Any]
    checks: list[CloudPreflightCheck] = field(default_factory=list)
    reconnect_notes: list[str] = field(default_factory=list)
    schema_version: int = 1

    @property
    def verdict(self) -> CloudPreflightVerdict:
        return derive_cloud_preflight_verdict(self.checks)

    def as_dict(self) -> dict[str, Any]:
        """Return the stable JSON shape used by the CLI."""
        return {
            "schema_version": self.schema_version,
            "experiment": self.experiment,
            "provider": self.provider,
            "verdict": self.verdict.value,
            "plan": _to_jsonable(self.plan),
            "resolved_defaults": _to_jsonable(self.resolved_defaults),
            "env_summary": _to_jsonable(self.env_summary),
            "checks": [_to_jsonable(check) for check in self.checks],
            "reconnect_notes": list(self.reconnect_notes),
        }


def build_cloud_preflight_report(
    *,
    schema_version: int = 1,
    experiment: str,
    provider: str,
    plan: Mapping[str, Any],
    resolved_defaults: Mapping[str, Any],
    env_summary: Mapping[str, Any],
    checks: Sequence[CloudPreflightCheck | Mapping[str, Any]],
    reconnect_notes: Sequence[str],
) -> CloudPreflightReport:
    """Build a canonical report from already-resolved preflight inputs."""
    return CloudPreflightReport(
        schema_version=schema_version,
        experiment=experiment,
        provider=provider,
        plan=dict(plan),
        resolved_defaults=dict(resolved_defaults),
        env_summary=dict(env_summary),
        checks=[
            check
            if isinstance(check, CloudPreflightCheck)
            else CloudPreflightCheck(**check)
            for check in checks
        ],
        reconnect_notes=list(reconnect_notes),
    )


def derive_cloud_preflight_verdict(
    checks: Sequence[CloudPreflightCheck],
) -> CloudPreflightVerdict:
    """Derive the canonical verdict from check statuses."""
    if any(check.is_failure for check in checks):
        return CloudPreflightVerdict.BLOCKED
    if any(check.is_warning for check in checks):
        return CloudPreflightVerdict.WARNING
    return CloudPreflightVerdict.READY


def _coerce_check_status(
    status: CloudPreflightCheckStatus | str,
) -> CloudPreflightCheckStatus:
    if isinstance(status, CloudPreflightCheckStatus):
        return status
    return CloudPreflightCheckStatus(status)


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    if is_dataclass(value):
        return {
            key: _to_jsonable(inner_value) for key, inner_value in asdict(value).items()
        }
    if isinstance(value, dict):
        return {key: _to_jsonable(inner_value) for key, inner_value in value.items()}
    if isinstance(value, list):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [_to_jsonable(item) for item in value]
    return value
