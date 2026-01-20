"""Canary string module for benchmark contamination detection.

This module provides tools to detect if benchmark data has leaked into
LLM training datasets using unique canary strings (UUIDs).

Based on BIG-bench canary string methodology:
https://github.com/google/BIG-bench

Usage:
    # Inject canaries for a prefix group (all get same UUID)
    crsbench benchmark inject-canary benchmarks/ --filter "atlanta-*"

    # Each prefix group gets its own UUID, stored in registry
    # ~/.config/crsbench/canary-registry.json
"""

from crsbench.benchmark.canary.detector import (
    check_uuid_in_response,
    create_detection_prompt,
    list_registered_canaries,
)
from crsbench.benchmark.canary.generator import (
    extract_canary_from_benchmark,
    extract_canary_from_file,
    generate_canary_block,
    inject_canaries_by_prefix,
    inject_canary_into_benchmark,
    inject_canary_into_file,
    load_registry,
    save_registry,
)
from crsbench.benchmark.canary.models import (
    BIGBENCH_CANARY_UUID,
    CANARY_WARNING,
    CanaryRegistry,
    ContaminationResult,
    InjectionResult,
)

__all__ = [
    "BIGBENCH_CANARY_UUID",
    "CANARY_WARNING",
    "CanaryRegistry",
    "ContaminationResult",
    "InjectionResult",
    "check_uuid_in_response",
    "create_detection_prompt",
    "extract_canary_from_benchmark",
    "extract_canary_from_file",
    "generate_canary_block",
    "inject_canaries_by_prefix",
    "inject_canary_into_benchmark",
    "inject_canary_into_file",
    "list_registered_canaries",
    "load_registry",
    "save_registry",
]
