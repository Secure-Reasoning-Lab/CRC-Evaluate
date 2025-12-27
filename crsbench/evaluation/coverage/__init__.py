"""Coverage collection module for CRSBench.

Provides coverage data collection and tracking during CRS evaluation:

Components:
- CoverageConfig: Configuration for coverage collection
- CoverageStore: In-memory store with JSON persistence
- CoverageStrategy: Abstract strategy for coverage collection (LLVM/JaCoCo)
- CoverageCollector: Orchestrates coverage collection
- CoverageManager: Thread-based periodic coverage collection

Usage:
    from crsbench.evaluation.coverage import (
        CoverageConfig,
        CoverageCollector,
        CoverageManager,
        create_coverage_strategy,
    )
"""

from crsbench.evaluation.coverage.collector import CoverageCollector
from crsbench.evaluation.coverage.manager import CoverageManager
from crsbench.evaluation.coverage.models import (
    CoverageConfig,
    CoverageReport,
    CoverageSnapshot,
    CoverageSummary,
)
from crsbench.evaluation.coverage.store import CoverageStore
from crsbench.evaluation.coverage.strategy import (
    CoverageStrategy,
    CoverageStrategyError,
    JaCoCoLineStrategy,
    LLVMCovLineStrategy,
    create_coverage_strategy,
)

__all__ = [
    # Config
    "CoverageConfig",
    # Models
    "CoverageReport",
    "CoverageSnapshot",
    "CoverageSummary",
    # Store
    "CoverageStore",
    # Strategy
    "CoverageStrategy",
    "CoverageStrategyError",
    "LLVMCovLineStrategy",
    "JaCoCoLineStrategy",
    "create_coverage_strategy",
    # Collector
    "CoverageCollector",
    # Manager
    "CoverageManager",
]
