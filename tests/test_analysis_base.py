"""Unit tests for analysis base module."""

import pytest
from pathlib import Path

from crsbench.evaluation.analysis.base import AnalysisResult, AnalyzerInterface


class TestAnalysisResult:
    """Test AnalysisResult dataclass."""

    def test_create_result(self):
        """Test creating an AnalysisResult."""
        result = AnalysisResult(
            crs_name="test-crs",
            metrics={"metric1": 100, "metric2": 200},
            summary="Test summary",
            warnings=["warning1"],
            metadata={"version": "1.0"}
        )

        assert result.crs_name == "test-crs"
        assert result.metrics == {"metric1": 100, "metric2": 200}
        assert result.summary == "Test summary"
        assert result.warnings == ["warning1"]
        assert result.metadata == {"version": "1.0"}

    def test_default_values(self):
        """Test AnalysisResult with default values."""
        result = AnalysisResult(crs_name="test-crs")

        assert result.crs_name == "test-crs"
        assert result.metrics == {}
        assert result.summary == ""
        assert result.warnings == []
        assert result.metadata == {}

    def test_to_dict(self):
        """Test converting AnalysisResult to dict."""
        result = AnalysisResult(
            crs_name="test-crs",
            metrics={"count": 42},
            summary="Summary",
            warnings=["warn"],
            metadata={"v": "1"}
        )

        data = result.to_dict()

        assert data == {
            'crs_name': 'test-crs',
            'metrics': {'count': 42},
            'summary': 'Summary',
            'warnings': ['warn'],
            'metadata': {'v': '1'}
        }

    def test_from_dict(self):
        """Test creating AnalysisResult from dict."""
        data = {
            'crs_name': 'test-crs',
            'metrics': {'value': 123},
            'summary': 'Test',
            'warnings': ['w1', 'w2'],
            'metadata': {'key': 'value'}
        }

        result = AnalysisResult.from_dict(data)

        assert result.crs_name == 'test-crs'
        assert result.metrics == {'value': 123}
        assert result.summary == 'Test'
        assert result.warnings == ['w1', 'w2']
        assert result.metadata == {'key': 'value'}

    def test_from_dict_partial(self):
        """Test from_dict with minimal data."""
        data = {'crs_name': 'test-crs'}

        result = AnalysisResult.from_dict(data)

        assert result.crs_name == 'test-crs'
        assert result.metrics == {}
        assert result.summary == ''
        assert result.warnings == []
        assert result.metadata == {}

    def test_round_trip(self):
        """Test to_dict() -> from_dict() round trip."""
        original = AnalysisResult(
            crs_name="test-crs",
            metrics={"x": 1, "y": 2},
            summary="Summary",
            warnings=["w"],
            metadata={"m": "v"}
        )

        data = original.to_dict()
        restored = AnalysisResult.from_dict(data)

        assert restored.crs_name == original.crs_name
        assert restored.metrics == original.metrics
        assert restored.summary == original.summary
        assert restored.warnings == original.warnings
        assert restored.metadata == original.metadata


class TestAnalyzerInterface:
    """Test AnalyzerInterface abstract base class."""

    def test_cannot_instantiate_directly(self):
        """Test that AnalyzerInterface cannot be instantiated."""
        with pytest.raises(TypeError, match="Can't instantiate abstract class"):
            AnalyzerInterface()

    def test_must_implement_crs_name(self):
        """Test that crs_name property must be implemented."""
        class IncompleteAnalyzer(AnalyzerInterface):
            def analyze_snapshot(self, snapshot_dir: Path):
                pass

            def analyze_trial(self, trial_dir: Path):
                pass

        with pytest.raises(TypeError):
            IncompleteAnalyzer()

    def test_must_implement_analyze_snapshot(self):
        """Test that analyze_snapshot must be implemented."""
        class IncompleteAnalyzer(AnalyzerInterface):
            @property
            def crs_name(self) -> str:
                return "test"

            def analyze_trial(self, trial_dir: Path):
                pass

        with pytest.raises(TypeError):
            IncompleteAnalyzer()

    def test_must_implement_analyze_trial(self):
        """Test that analyze_trial must be implemented."""
        class IncompleteAnalyzer(AnalyzerInterface):
            @property
            def crs_name(self) -> str:
                return "test"

            def analyze_snapshot(self, snapshot_dir: Path):
                pass

        with pytest.raises(TypeError):
            IncompleteAnalyzer()

    def test_complete_implementation(self):
        """Test that a complete implementation can be instantiated."""
        class CompleteAnalyzer(AnalyzerInterface):
            @property
            def crs_name(self) -> str:
                return "test-crs"

            def analyze_snapshot(self, snapshot_dir: Path):
                return None

            def analyze_trial(self, trial_dir: Path):
                return None

        # Should not raise
        analyzer = CompleteAnalyzer()
        assert analyzer.crs_name == "test-crs"

    def test_analyze_time_series_optional(self):
        """Test that analyze_time_series has default implementation."""
        class MinimalAnalyzer(AnalyzerInterface):
            @property
            def crs_name(self) -> str:
                return "test"

            def analyze_snapshot(self, snapshot_dir: Path):
                return None

            def analyze_trial(self, trial_dir: Path):
                return None

        analyzer = MinimalAnalyzer()

        # Default implementation returns None
        result = analyzer.analyze_time_series([])
        assert result is None

    def test_can_override_analyze_time_series(self):
        """Test that analyze_time_series can be overridden."""
        class TimeSeriesAnalyzer(AnalyzerInterface):
            @property
            def crs_name(self) -> str:
                return "test"

            def analyze_snapshot(self, snapshot_dir: Path):
                return None

            def analyze_trial(self, trial_dir: Path):
                return None

            def analyze_time_series(self, snapshots):
                return {"data": [1, 2, 3]}

        analyzer = TimeSeriesAnalyzer()
        result = analyzer.analyze_time_series([])
        assert result == {"data": [1, 2, 3]}
