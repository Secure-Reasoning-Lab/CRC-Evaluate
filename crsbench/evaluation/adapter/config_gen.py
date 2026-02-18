"""CRS-compose YAML configuration generation.

Pydantic models mirroring the crs-compose.yaml schema. The to_yaml() method
serializes CRS entries as top-level keys (NOT nested under a ``crs_entries``
key), matching the format that crs-compose expects.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

import yaml
from pydantic import BaseModel, Field

from crsbench.utils.logger import get_logger

if TYPE_CHECKING:
    from pathlib import Path

logger = get_logger(__name__)


class CrsComposeSource(BaseModel):
    """CRS source location for crs-compose."""

    url: Optional[str] = None
    ref: Optional[str] = None
    local_path: Optional[str] = None


class CrsComposeCrsEntry(BaseModel):
    """A single CRS entry for crs-compose.yaml."""

    source: CrsComposeSource
    cpuset: str
    memory: str
    llm_budget: Optional[int] = None
    additional_env: Optional[dict[str, str]] = None


class CrsComposeInfra(BaseModel):
    """Infrastructure resource config for oss-crs services."""

    cpuset: str
    memory: str


class CrsComposeLlmConfig(BaseModel):
    """LLM configuration for crs-compose."""

    litellm_config: str


class CrsComposeYaml(BaseModel):
    """Full crs-compose.yaml schema for generation.

    CRS entries are stored internally in ``crs_entries`` but serialized as
    top-level keys in the output YAML (not nested under ``crs_entries:``).
    """

    run_env: str = Field(default="local")
    docker_registry: str
    oss_crs_infra: CrsComposeInfra
    crs_entries: dict[str, CrsComposeCrsEntry]
    llm_config: Optional[CrsComposeLlmConfig] = None

    def to_yaml(self, path: Path) -> None:
        """Write to YAML file in crs-compose format.

        CRS entries become top-level keys alongside reserved keys
        (run_env, docker_registry, oss_crs_infra, llm_config).
        """
        data: dict[str, object] = {
            "run_env": self.run_env,
            "docker_registry": self.docker_registry,
            "oss_crs_infra": self.oss_crs_infra.model_dump(exclude_none=True),
        }

        # CRS entries as top-level keys (NOT nested under "crs_entries")
        for name, entry in self.crs_entries.items():
            data[name] = entry.model_dump(exclude_none=True)

        if self.llm_config:
            data["llm_config"] = self.llm_config.model_dump()

        path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))
        logger.debug("Wrote crs-compose config to %s", path)
