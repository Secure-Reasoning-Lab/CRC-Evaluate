"""Generate CRS Compose YAML with the structure expected by OSS-CRS."""

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
    memory: Optional[str] = None
    llm_budget: Optional[int] = None
    additional_env: Optional[dict[str, str]] = None


class CrsComposeInfra(BaseModel):
    """Infrastructure resource config for oss-crs services."""

    cpuset: str
    memory: Optional[str] = None


class CrsComposeLlmConfig(BaseModel):
    """LiteLLM config path for the compatibility compose schema."""

    litellm_config: str


class CrsComposeLiteLLMInternalConfig(BaseModel):
    """oss-crs internal LiteLLM mode configuration."""

    config_path: str


class CrsComposeLiteLLMExternalConfig(BaseModel):
    """oss-crs external LiteLLM mode configuration."""

    url: Optional[str] = None
    url_env: Optional[str] = None
    key: Optional[str] = None
    key_env: Optional[str] = None


class CrsComposeLiteLLMConfig(BaseModel):
    """Top-level oss-crs LiteLLM mode selection."""

    mode: str
    model_check: bool = True
    internal: Optional[CrsComposeLiteLLMInternalConfig] = None
    external: Optional[CrsComposeLiteLLMExternalConfig] = None


class CrsComposeLLMConfig(BaseModel):
    """LLM configuration block for crs-compose."""

    litellm: CrsComposeLiteLLMConfig


class CrsComposeYaml(BaseModel):
    """Full CRS Compose schema with CRS entries serialized as top-level keys."""

    run_env: str = Field(default="local")
    docker_registry: str
    oss_crs_infra: CrsComposeInfra
    crs_entries: dict[str, CrsComposeCrsEntry]
    llm_config: Optional[CrsComposeLLMConfig] = None

    def to_yaml(self, path: Path) -> None:
        """Write a CRS Compose YAML file with CRS entries alongside the reserved keys."""
        data: dict[str, object] = {
            "run_env": self.run_env,
            "docker_registry": self.docker_registry,
            "oss_crs_infra": self.oss_crs_infra.model_dump(exclude_none=True),
        }

        # CRS entries are top-level keys rather than members of a crs_entries mapping.
        for name, entry in self.crs_entries.items():
            data[name] = entry.model_dump(exclude_none=True)

        if self.llm_config:
            data["llm_config"] = self.llm_config.model_dump(exclude_none=True)

        path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))
        logger.debug(f"Wrote crs-compose config to {path}")
