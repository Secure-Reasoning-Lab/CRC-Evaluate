"""Unit tests for ExampleAnalyzer."""

import pytest
import json
from pathlib import Path

from crsbench.evaluation.analysis.analyzers.example import ExampleAnalyzer


class TestExampleAnalyzer:
    """Test ExampleAnalyzer implementation."""

    @pytest.fixture
    def analyzer(self):
        """Create ExampleAnalyzer instance."""
        return ExampleAnalyzer()

    def test_crs_name(self, analyzer):
        """Test CRS name property."""
        assert analyzer.crs_name == "example-crs"

    def test_analyze_snapshot_success(self, analyzer, tmp_path):
        """Test analyzing snapshot with valid data."""
        # Create mock crs-data
        crs_data_dir = tmp_path / "crs-data"
        crs_data_dir.mkdir()

        metrics_file = crs_data_dir / "metrics.json"
        metrics_file.write_text(json.dumps({
            "iterations": 100,
            "discoveries": 5,
            "timestamp": 1234567890.0
        }))

        result = analyzer.analyze_snapshot(tmp_path)

        assert result is not None
        assert result.crs_name == "example-crs"
        assert result.metrics["iterations"] == 100
        assert result.metrics["discoveries"] == 5
        assert result.metrics["timestamp"] == 1234567890.0
        assert "100 iterations" in result.summary
        assert "5 discoveries" in result.summary
        assert len(result.warnings) == 0
        assert result.metadata["analyzer_version"] == "1.0"

    def test_analyze_snapshot_no_crs_data(self, analyzer, tmp_path):
        """Test analyzing snapshot without crs-data directory."""
        result = analyzer.analyze_snapshot(tmp_path)

        assert result is None

    def test_analyze_snapshot_no_metrics_file(self, analyzer, tmp_path):
        """Test analyzing snapshot without metrics.json."""
        crs_data_dir = tmp_path / "crs-data"
        crs_data_dir.mkdir()

        result = analyzer.analyze_snapshot(tmp_path)

        assert result is not None
        assert result.crs_name == "example-crs"
        assert result.metrics == {}
        assert result.summary == "No metrics available"
        assert "metrics.json not found" in result.warnings[0]

    def test_analyze_snapshot_invalid_json(self, analyzer, tmp_path):
        """Test analyzing snapshot with invalid JSON."""
        crs_data_dir = tmp_path / "crs-data"
        crs_data_dir.mkdir()

        metrics_file = crs_data_dir / "metrics.json"
        metrics_file.write_text("invalid json {")

        result = analyzer.analyze_snapshot(tmp_path)

        assert result is not None
        assert result.crs_name == "example-crs"
        assert result.metrics == {}
        assert result.summary == "Analysis failed"
        assert any("Invalid JSON" in w for w in result.warnings)

    def test_analyze_snapshot_missing_fields(self, analyzer, tmp_path):
        """Test analyzing snapshot with missing fields in JSON."""
        crs_data_dir = tmp_path / "crs-data"
        crs_data_dir.mkdir()

        metrics_file = crs_data_dir / "metrics.json"
        metrics_file.write_text(json.dumps({"iterations": 50}))

        result = analyzer.analyze_snapshot(tmp_path)

        assert result is not None
        assert result.metrics["iterations"] == 50
        assert result.metrics["discoveries"] == 0  # Default value
        assert "timestamp" not in result.metrics

    def test_analyze_trial_success(self, analyzer, tmp_path):
        """Test analyzing trial with valid data."""
        # Create mock trial structure
        output_dir = tmp_path / "output"
        output_dir.mkdir()

        crs_data_dir = output_dir / "crs-data"
        crs_data_dir.mkdir()

        metrics_file = crs_data_dir / "metrics.json"
        metrics_file.write_text(json.dumps({
            "iterations": 200,
            "discoveries": 10
        }))

        result = analyzer.analyze_trial(tmp_path)

        assert result is not None
        assert result.crs_name == "example-crs"
        assert result.metrics["iterations"] == 200
        assert result.metrics["discoveries"] == 10

    def test_analyze_trial_no_output(self, analyzer, tmp_path):
        """Test analyzing trial without output directory."""
        result = analyzer.analyze_trial(tmp_path)

        assert result is None

    def test_analyze_time_series_success(self, analyzer, tmp_path):
        """Test time-series analysis with multiple snapshots."""
        snapshots = []

        for i in range(3):
            snapshot_dir = tmp_path / f"snapshot-{i:04d}"
            snapshot_dir.mkdir()

            crs_data_dir = snapshot_dir / "crs-data"
            crs_data_dir.mkdir()

            metrics_file = crs_data_dir / "metrics.json"
            metrics_file.write_text(json.dumps({
                "iterations": (i + 1) * 100,
                "discoveries": i + 1,
                "timestamp": 1234567890.0 + i * 60
            }))

            snapshots.append(snapshot_dir)

        result = analyzer.analyze_time_series(snapshots)

        assert result is not None
        assert result["iterations_over_time"] == [100, 200, 300]
        assert result["discoveries_over_time"] == [1, 2, 3]
        assert result["avg_iterations_per_cycle"] == 200.0
        assert result["total_discoveries"] == 6
        assert len(result["timestamps"]) == 3

    def test_analyze_time_series_empty(self, analyzer):
        """Test time-series analysis with no snapshots."""
        result = analyzer.analyze_time_series([])

        assert result is None

    def test_analyze_time_series_no_data(self, analyzer, tmp_path):
        """Test time-series analysis when snapshots have no data."""
        snapshots = []

        for i in range(2):
            snapshot_dir = tmp_path / f"snapshot-{i:04d}"
            snapshot_dir.mkdir()
            # No crs-data directory
            snapshots.append(snapshot_dir)

        result = analyzer.analyze_time_series(snapshots)

        assert result is None

    def test_analyze_time_series_partial_data(self, analyzer, tmp_path):
        """Test time-series analysis with some invalid snapshots."""
        snapshots = []

        # Valid snapshot
        snapshot_dir_1 = tmp_path / "snapshot-0001"
        snapshot_dir_1.mkdir()
        crs_data_dir_1 = snapshot_dir_1 / "crs-data"
        crs_data_dir_1.mkdir()
        metrics_file_1 = crs_data_dir_1 / "metrics.json"
        metrics_file_1.write_text(json.dumps({"iterations": 100, "discoveries": 1}))
        snapshots.append(snapshot_dir_1)

        # Invalid snapshot (no crs-data)
        snapshot_dir_2 = tmp_path / "snapshot-0002"
        snapshot_dir_2.mkdir()
        snapshots.append(snapshot_dir_2)

        # Valid snapshot
        snapshot_dir_3 = tmp_path / "snapshot-0003"
        snapshot_dir_3.mkdir()
        crs_data_dir_3 = snapshot_dir_3 / "crs-data"
        crs_data_dir_3.mkdir()
        metrics_file_3 = crs_data_dir_3 / "metrics.json"
        metrics_file_3.write_text(json.dumps({"iterations": 300, "discoveries": 3}))
        snapshots.append(snapshot_dir_3)

        result = analyzer.analyze_time_series(snapshots)

        # Should process valid snapshots only
        assert result is not None
        assert result["iterations_over_time"] == [100, 300]
        assert result["discoveries_over_time"] == [1, 3]

    def test_analyze_time_series_without_timestamps(self, analyzer, tmp_path):
        """Test time-series analysis when metrics don't have timestamps."""
        snapshots = []

        for i in range(2):
            snapshot_dir = tmp_path / f"snapshot-{i:04d}"
            snapshot_dir.mkdir()

            crs_data_dir = snapshot_dir / "crs-data"
            crs_data_dir.mkdir()

            metrics_file = crs_data_dir / "metrics.json"
            # No timestamp field
            metrics_file.write_text(json.dumps({
                "iterations": (i + 1) * 100,
                "discoveries": i + 1
            }))

            snapshots.append(snapshot_dir)

        result = analyzer.analyze_time_series(snapshots)

        assert result is not None
        assert "timestamps" not in result  # No timestamps collected
        assert result["iterations_over_time"] == [100, 200]
