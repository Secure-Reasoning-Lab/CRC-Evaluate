"""Base classes for CRS-specific analysis.

This module defines the interface and data structures for CRS-specific
analysis of crs-data/ outputs from trials and snapshots.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Any, Optional, List


@dataclass
class AnalysisResult:
    """Result from CRS-specific analysis.

    Attributes:
        crs_name: Name of the CRS that was analyzed
        metrics: CRS-specific metrics extracted from crs-data (e.g., {"execs": 1000})
        summary: Human-readable summary of the analysis
        warnings: List of warnings or issues encountered during analysis
        metadata: Additional metadata (e.g., analyzer version, timestamp)
    """
    crs_name: str
    metrics: Dict[str, Any] = field(default_factory=dict)
    summary: str = ""
    warnings: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            'crs_name': self.crs_name,
            'metrics': self.metrics,
            'summary': self.summary,
            'warnings': self.warnings,
            'metadata': self.metadata
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'AnalysisResult':
        """Create AnalysisResult from dictionary."""
        return cls(
            crs_name=data['crs_name'],
            metrics=data.get('metrics', {}),
            summary=data.get('summary', ''),
            warnings=data.get('warnings', []),
            metadata=data.get('metadata', {})
        )


class AnalyzerInterface(ABC):
    """Abstract base class for CRS-specific analyzers.

    CRS developers implement this interface to analyze their CRS's
    custom data in the crs-data/ directory.

    Design principles:
    - Read-only: Analyzers should never modify trial data
    - Graceful: Return None if data is missing, don't raise exceptions
    - Flexible: No format enforcement on crs-data/ contents
    """

    @property
    @abstractmethod
    def crs_name(self) -> str:
        """Return CRS name (e.g., 'atlantis-c', 'patchagent').

        This name is used to map the analyzer to CRS trials.
        Should match the CRS identifier used in experiment configs.
        """
        pass

    @abstractmethod
    def analyze_snapshot(self, snapshot_dir: Path) -> Optional[AnalysisResult]:
        """Analyze a single snapshot's crs-data.

        Args:
            snapshot_dir: Extracted snapshot directory containing crs-data/
                         Expected structure: snapshot_dir/crs-data/*

        Returns:
            AnalysisResult if analysis succeeds, None if crs-data missing/invalid

        Notes:
            - This method should be read-only (no modifications to snapshot_dir)
            - Should handle missing/malformed data gracefully (return None)
            - Should NOT raise exceptions (catch and return None or AnalysisResult with warnings)
            - Can return AnalysisResult with empty metrics if data exists but parsing fails

        Example:
            >>> analyzer = MyCRSAnalyzer()
            >>> result = analyzer.analyze_snapshot(Path("/tmp/snapshot-0001"))
            >>> if result:
            >>>     print(result.summary)
        """
        pass

    @abstractmethod
    def analyze_trial(self, trial_dir: Path) -> Optional[AnalysisResult]:
        """Analyze final trial data (from trial_dir/output/crs-data/).

        Args:
            trial_dir: Trial output directory
                      Expected structure: trial_dir/output/crs-data/*

        Returns:
            AnalysisResult if analysis succeeds, None if crs-data missing/invalid

        Notes:
            - Same error handling principles as analyze_snapshot()
            - Can reuse analyze_snapshot() logic by creating a "snapshot-like" view

        Example:
            >>> analyzer = MyCRSAnalyzer()
            >>> result = analyzer.analyze_trial(Path("/experiments/trial0"))
            >>> if result:
            >>>     print(f"Final metrics: {result.metrics}")
        """
        pass

    def analyze_time_series(self, snapshots: List[Path]) -> Optional[Dict[str, Any]]:
        """Optional: Analyze time-series data across multiple snapshots.

        Args:
            snapshots: List of extracted snapshot directories (sorted by cycle)
                      Each directory contains crs-data/

        Returns:
            Dict with time-series metrics (e.g., {"coverage_over_time": [10, 20, 30]})
            None if not implemented or insufficient data

        Notes:
            - This method is optional (default implementation returns None)
            - Useful for tracking progress over time (e.g., coverage growth)
            - Can call analyze_snapshot() for each snapshot and aggregate results

        Example:
            >>> analyzer = MyCRSAnalyzer()
            >>> snapshots = [Path("/tmp/snap-0001"), Path("/tmp/snap-0002")]
            >>> time_series = analyzer.analyze_time_series(snapshots)
            >>> if time_series:
            >>>     print(time_series["iterations_over_time"])
        """
        return None  # Default: not implemented
