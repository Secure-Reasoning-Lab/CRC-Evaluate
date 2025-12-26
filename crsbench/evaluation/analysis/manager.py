"""Manager for discovering and executing CRS-specific analyzers.

This module implements AnalysisManager which auto-discovers analyzer
implementations and executes them with graceful error handling.
"""

import importlib
import inspect
from pathlib import Path
from typing import Dict, List, Optional

from crsbench.evaluation.analysis.base import AnalysisResult, AnalyzerInterface
from crsbench.utils.logger import get_logger

logger = get_logger(__name__)


class AnalysisManager:
    """Discovers and executes CRS-specific analyzers.

    Responsibilities:
    - Auto-discover analyzer modules in crsbench/evaluation/analysis/analyzers/
    - Load analyzers dynamically based on CRS name
    - Execute analyzers on snapshots or trial data
    - Handle errors gracefully (missing analyzer or analyzer exceptions)

    Example:
        >>> manager = AnalysisManager()
        >>> result = manager.analyze_snapshot("atlantis-c", Path("/tmp/snapshot-0001"))
        >>> if result:
        >>>     print(result.summary)
    """

    def __init__(self):
        """Initialize manager and discover available analyzers."""
        self.analyzers: Dict[str, AnalyzerInterface] = {}
        self._discover_analyzers()

    def _discover_analyzers(self):
        """Scan analyzers/ directory and load all analyzer modules.

        Auto-discovery process:
        1. Find all .py files in crsbench/evaluation/analysis/analyzers/
        2. Import each module
        3. Find classes that inherit from AnalyzerInterface
        4. Instantiate and register by crs_name
        """
        # Get analyzers directory path
        analyzers_dir = Path(__file__).parent / "analyzers"

        if not analyzers_dir.exists():
            logger.warning(f"Analyzers directory not found: {analyzers_dir}")
            return

        # Find all Python files (except __init__.py)
        analyzer_files = [
            f for f in analyzers_dir.glob("*.py") if f.name != "__init__.py"
        ]

        logger.debug(f"Discovering analyzers in {analyzers_dir}")
        logger.debug(f"Found {len(analyzer_files)} analyzer files")

        for analyzer_file in analyzer_files:
            try:
                # Import module dynamically
                module_name = (
                    f"crsbench.evaluation.analysis.analyzers.{analyzer_file.stem}"
                )
                module = importlib.import_module(module_name)

                # Find analyzer classes in module
                for name, obj in inspect.getmembers(module, inspect.isclass):
                    # Check if it's an AnalyzerInterface subclass (but not the base class itself)
                    if (
                        issubclass(obj, AnalyzerInterface)
                        and obj is not AnalyzerInterface
                        and not inspect.isabstract(obj)
                    ):
                        try:
                            # Instantiate analyzer
                            analyzer = obj()
                            crs_name = analyzer.crs_name

                            # Register analyzer
                            if crs_name in self.analyzers:
                                logger.warning(
                                    f"Duplicate analyzer for {crs_name}: "
                                    f"{analyzer.__class__.__name__} (skipping)"
                                )
                            else:
                                self.analyzers[crs_name] = analyzer
                                logger.info(
                                    f"Registered analyzer for '{crs_name}': "
                                    f"{analyzer.__class__.__name__}"
                                )

                        except Exception as e:
                            logger.warning(
                                f"Failed to instantiate {name} from {analyzer_file.name}: {e}"
                            )

            except Exception as e:
                logger.warning(f"Failed to import {analyzer_file.name}: {e}")

        logger.info(
            f"Discovered {len(self.analyzers)} analyzers: {list(self.analyzers.keys())}"
        )

    def get_analyzer(self, crs_name: str) -> Optional[AnalyzerInterface]:
        """Get analyzer for a CRS.

        Args:
            crs_name: CRS identifier (e.g., "atlantis-c", "patchagent")

        Returns:
            AnalyzerInterface instance if available, None otherwise

        Example:
            >>> manager = AnalysisManager()
            >>> analyzer = manager.get_analyzer("atlantis-c")
            >>> if analyzer:
            >>>     print(f"Found analyzer for {analyzer.crs_name}")
        """
        return self.analyzers.get(crs_name)

    def list_analyzers(self) -> List[str]:
        """Get list of available CRS analyzers.

        Returns:
            List of CRS names that have analyzers

        Example:
            >>> manager = AnalysisManager()
            >>> print(manager.list_analyzers())
            ['example-crs', 'atlantis-c']
        """
        return list(self.analyzers.keys())

    def analyze_snapshot(
        self, crs_name: str, snapshot_dir: Path
    ) -> Optional[AnalysisResult]:
        """Analyze a snapshot for a specific CRS.

        Args:
            crs_name: CRS identifier
            snapshot_dir: Extracted snapshot directory containing crs-data/

        Returns:
            AnalysisResult if analyzer exists and succeeds, None otherwise

        Notes:
            - Returns None if no analyzer exists for this CRS (not an error)
            - Returns None if analyzer raises exception (logged as warning)
            - Returns AnalysisResult if analyzer succeeds (may have empty metrics)

        Example:
            >>> manager = AnalysisManager()
            >>> result = manager.analyze_snapshot("atlantis-c", Path("/tmp/snapshot-0001"))
            >>> if result:
            >>>     print(f"Metrics: {result.metrics}")
        """
        analyzer = self.get_analyzer(crs_name)
        if not analyzer:
            logger.debug(f"No analyzer found for CRS: {crs_name}")
            return None

        try:
            logger.debug(f"Analyzing snapshot {snapshot_dir} with {crs_name} analyzer")
            result = analyzer.analyze_snapshot(snapshot_dir)

            if result:
                logger.debug(f"Analysis complete: {result.summary}")
            else:
                logger.debug("Analyzer returned None (no data or failed)")

            return result

        except Exception as e:
            logger.warning(
                f"Analyzer {crs_name} failed on snapshot {snapshot_dir}: {e}",
                exc_info=True,
            )
            return None

    def analyze_trial(self, crs_name: str, trial_dir: Path) -> Optional[AnalysisResult]:
        """Analyze final trial data for a specific CRS.

        Args:
            crs_name: CRS identifier
            trial_dir: Trial output directory

        Returns:
            AnalysisResult if analyzer exists and succeeds, None otherwise

        Example:
            >>> manager = AnalysisManager()
            >>> result = manager.analyze_trial("patchagent", Path("/experiments/trial0"))
            >>> if result:
            >>>     print(f"Final summary: {result.summary}")
        """
        analyzer = self.get_analyzer(crs_name)
        if not analyzer:
            logger.debug(f"No analyzer found for CRS: {crs_name}")
            return None

        try:
            logger.debug(f"Analyzing trial {trial_dir} with {crs_name} analyzer")
            result = analyzer.analyze_trial(trial_dir)

            if result:
                logger.debug(f"Trial analysis complete: {result.summary}")
            else:
                logger.debug("Analyzer returned None (no data or failed)")

            return result

        except Exception as e:
            logger.warning(
                f"Analyzer {crs_name} failed on trial {trial_dir}: {e}", exc_info=True
            )
            return None

    def analyze_time_series(
        self, crs_name: str, snapshots: List[Path]
    ) -> Optional[Dict]:
        """Analyze time-series data across multiple snapshots.

        Args:
            crs_name: CRS identifier
            snapshots: List of extracted snapshot directories (sorted by cycle)

        Returns:
            Dict with time-series metrics if analyzer implements this, None otherwise

        Example:
            >>> manager = AnalysisManager()
            >>> snapshots = [Path("/tmp/snap-0001"), Path("/tmp/snap-0002")]
            >>> time_series = manager.analyze_time_series("atlantis-c", snapshots)
            >>> if time_series:
            >>>     print(time_series.get("coverage_over_time"))
        """
        analyzer = self.get_analyzer(crs_name)
        if not analyzer:
            logger.debug(f"No analyzer found for CRS: {crs_name}")
            return None

        try:
            logger.debug(
                f"Analyzing time-series ({len(snapshots)} snapshots) "
                f"with {crs_name} analyzer"
            )
            result = analyzer.analyze_time_series(snapshots)

            if result:
                logger.debug(f"Time-series analysis complete: {len(result)} metrics")
            else:
                logger.debug("Time-series analysis not implemented or returned None")

            return result

        except Exception as e:
            logger.warning(
                f"Time-series analyzer {crs_name} failed: {e}", exc_info=True
            )
            return None
