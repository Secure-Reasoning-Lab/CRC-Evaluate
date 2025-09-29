"""Builder infrastructure for CRSBench.

This module provides building and testing infrastructure for different types of projects,
with special support for OSS-Fuzz projects. The OSS-Fuzz builder implementation is adapted
from PatchAgent (https://github.com/cla7aye15I4nd/PatchAgent) with permission under the
Apache 2.0 license.

Original PatchAgent citation:
    Yu, Zheng et al. "PatchAgent: A Practical Program Repair Agent Mimicking Human Expertise"
    34rd USENIX Security Symposium (USENIX Security 25), 2025.
"""

from crsbench.builder.base import Builder, BuildResult, BuildStatus
from crsbench.builder.poc import POC, POCType
from crsbench.builder.utils import BuilderError, BuilderProcessError, BuilderTimeoutError
from crsbench.builder.ossfuzz import OSSFuzzBuilder, OSSFuzzPOC
from crsbench.builder.integration import (
    create_builder_from_config,
    validate_builder_config,
    get_supported_sanitizers
)

__all__ = [
    'Builder',
    'BuildResult',
    'BuildStatus',
    'POC',
    'POCType',
    'BuilderError',
    'BuilderProcessError',
    'BuilderTimeoutError',
    'OSSFuzzBuilder',
    'OSSFuzzPOC',
    'create_builder_from_config',
    'validate_builder_config',
    'get_supported_sanitizers'
]