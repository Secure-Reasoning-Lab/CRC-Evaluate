"""MCP server and configuration for CRSBench migration tools."""

from crsbench.migration.mcp.config import (
    BASE_BENCHMARKS_DIR,
    BASE_DIR,
    BASE_PROJECTS_DIR,
    BASE_TMP_LOGS,
    CRSBENCH_ROOT,
    DOCKER_IMAGE_PREFIX,
    OSS_FUZZ_AIXCC_DIR,
    OSS_FUZZ_DIR,
    OSS_FUZZ_PROJECTS_DIR,
)
from crsbench.migration.mcp.server import start_mcp_server

__all__ = [
    # Config
    "BASE_BENCHMARKS_DIR",
    "BASE_DIR",
    "BASE_PROJECTS_DIR",
    "BASE_TMP_LOGS",
    "CRSBENCH_ROOT",
    "DOCKER_IMAGE_PREFIX",
    "OSS_FUZZ_AIXCC_DIR",
    "OSS_FUZZ_DIR",
    "OSS_FUZZ_PROJECTS_DIR",
    # Server
    "start_mcp_server",
]
