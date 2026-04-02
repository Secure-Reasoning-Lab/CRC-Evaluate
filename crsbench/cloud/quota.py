"""Shared quota validation entry point for provider-neutral cloud launches."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

    from crsbench.cloud.models import CloudLaunchPlan, QuotaShortage


class CloudQuotaValidationError(RuntimeError):
    """Raised when live provider quota cannot satisfy the launch plan."""

    def __init__(self, shortages: list[QuotaShortage] | str) -> None:
        if isinstance(shortages, str):
            self.shortages: list[QuotaShortage] = []
            super().__init__(shortages)
            return
        self.shortages = shortages
        super().__init__(self._format(shortages))

    @staticmethod
    def _format(shortages: list[QuotaShortage]) -> str:
        parts = [
            (
                f"{shortage.provider}:{shortage.scope}:{shortage.resource_family} "
                f"required={shortage.required} available={shortage.available}"
            )
            for shortage in shortages
        ]
        return "quota validation failed: " + "; ".join(parts)


class QuotaValidator:
    """Validate one launch plan against provider-specific live quota."""

    def __init__(self, *, adapters: Mapping[str, object]) -> None:
        self._adapters = dict(adapters)

    def validate(
        self, plan: CloudLaunchPlan, *, include_orchestrator: bool = True
    ) -> None:
        """Raise if any provider cannot satisfy its part of the launch plan."""
        shortages: list[QuotaShortage] = []
        provider_names = {placement.provider for placement in plan.worker_placements}
        provider_names.update(
            placement.provider for placement in plan.evaluator_placements
        )
        if include_orchestrator:
            provider_names.add(plan.orchestrator.provider)
        for provider_name in sorted(provider_names):
            adapter = self._adapters.get(provider_name)
            if adapter is None:
                raise ValueError(
                    f"No quota adapter registered for provider {provider_name!r}"
                )
            shortages.extend(
                adapter.quota_shortages(
                    plan,
                    include_orchestrator=include_orchestrator,
                )
            )
        if shortages:
            raise CloudQuotaValidationError(shortages)
