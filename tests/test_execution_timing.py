"""Tests for build/run timing split in CRS execution."""

from crsbench.evaluation.crs_executor import CRSExecutionResult
from crsbench.evaluation.results import HarnessResult, TrialMetadata


class TestCRSExecutionResultTiming:
    """Tests for CRSExecutionResult timing fields."""

    def test_execution_result_default_timing(self) -> None:
        """Test that build_time and run_time default to None."""
        result = CRSExecutionResult(
            harness_name="test_harness",
            execution_time=100.0,
            success=True,
            output="test output",
        )
        assert result.build_time is None
        assert result.run_time is None

    def test_execution_result_with_timing(self) -> None:
        """Test CRSExecutionResult with explicit timing values."""
        result = CRSExecutionResult(
            harness_name="test_harness",
            execution_time=100.0,
            success=True,
            output="test output",
            build_time=30.0,
            run_time=70.0,
        )
        assert result.build_time == 30.0
        assert result.run_time == 70.0
        # Verify total matches (approximately - may differ due to overhead)
        assert result.execution_time == 100.0

    def test_execution_result_timing_with_timeout(self) -> None:
        """Test timing fields when execution times out."""
        result = CRSExecutionResult(
            harness_name="test_harness",
            execution_time=3600.0,
            success=True,
            output="timed out",
            timed_out=True,
            build_time=60.0,
            run_time=3540.0,
        )
        assert result.timed_out is True
        assert result.build_time == 60.0
        assert result.run_time == 3540.0


class TestHarnessResultTiming:
    """Tests for HarnessResult timing fields."""

    def test_harness_result_default_timing(self) -> None:
        """Test that build_time and run_time default to None."""
        result = HarnessResult(
            name="test_harness",
            path="/src/test.c",
            execution_time=100.0,
        )
        assert result.build_time is None
        assert result.run_time is None

    def test_harness_result_with_timing(self) -> None:
        """Test HarnessResult with explicit timing values."""
        result = HarnessResult(
            name="test_harness",
            path="/src/test.c",
            execution_time=100.0,
            build_time=25.0,
            run_time=75.0,
        )
        assert result.build_time == 25.0
        assert result.run_time == 75.0

    def test_harness_result_preserves_other_fields(self) -> None:
        """Test that timing doesn't affect other fields."""
        result = HarnessResult(
            name="test_harness",
            path="/src/test.c",
            execution_time=100.0,
            run_successful=True,
            run_output="output data",
            build_time=25.0,
            run_time=75.0,
        )
        assert result.name == "test_harness"
        assert result.path == "/src/test.c"
        assert result.run_successful is True
        assert result.run_output == "output data"


class TestTrialMetadataTiming:
    """Tests for TrialMetadata timing fields."""

    def test_trial_metadata_default_timing(self) -> None:
        """Test that build_time and run_time default to None."""
        metadata = TrialMetadata(
            timestamp_start=1700000000.0,
            timestamp_end=1700000100.0,
        )
        assert metadata.build_time is None
        assert metadata.run_time is None

    def test_trial_metadata_with_timing(self) -> None:
        """Test TrialMetadata with explicit timing values."""
        metadata = TrialMetadata(
            timestamp_start=1700000000.0,
            timestamp_end=1700000100.0,
            build_time=30.0,
            run_time=70.0,
        )
        assert metadata.build_time == 30.0
        assert metadata.run_time == 70.0

    def test_trial_metadata_serialization(self) -> None:
        """Test that timing is included in serialization."""
        metadata = TrialMetadata(
            experiment_filestore="/data/exp",
            max_total_time=3600,
            difficulty_level=1,
            timestamp_start=1700000000.0,
            timestamp_end=1700000100.0,
            build_time=30.0,
            run_time=70.0,
        )
        data = metadata.model_dump()
        assert data["build_time"] == 30.0
        assert data["run_time"] == 70.0

    def test_trial_metadata_json_serialization(self) -> None:
        """Test that timing works with JSON serialization."""
        import json

        metadata = TrialMetadata(
            timestamp_start=1700000000.0,
            timestamp_end=1700000100.0,
            build_time=30.5,
            run_time=69.5,
        )
        json_str = metadata.model_dump_json()
        loaded = json.loads(json_str)
        assert loaded["build_time"] == 30.5
        assert loaded["run_time"] == 69.5


class TestTimingConsistency:
    """Tests for timing consistency across components."""

    def test_timing_flow_crs_to_harness(self) -> None:
        """Test that timing can be transferred from CRSExecutionResult to HarnessResult."""
        crs_result = CRSExecutionResult(
            harness_name="test",
            execution_time=100.0,
            success=True,
            output="",
            build_time=40.0,
            run_time=60.0,
        )

        # Simulate what runner.py does
        harness_result = HarnessResult(
            name=crs_result.harness_name,
            path="/src/test.c",
            execution_time=crs_result.execution_time,
            run_successful=crs_result.success,
            run_output=crs_result.output,
            build_time=crs_result.build_time,
            run_time=crs_result.run_time,
        )

        assert harness_result.build_time == 40.0
        assert harness_result.run_time == 60.0

    def test_timing_flow_harness_to_metadata(self) -> None:
        """Test that timing can be transferred from HarnessResult to TrialMetadata."""
        harness_result = HarnessResult(
            name="test",
            path="/src/test.c",
            execution_time=100.0,
            build_time=40.0,
            run_time=60.0,
        )

        # Simulate what jobs.py does
        metadata = TrialMetadata(
            timestamp_start=1700000000.0,
            timestamp_end=1700000100.0,
            build_time=harness_result.build_time,
            run_time=harness_result.run_time,
        )

        assert metadata.build_time == 40.0
        assert metadata.run_time == 60.0

    def test_timing_partial_availability(self) -> None:
        """Test handling when only build_time or run_time is available."""
        # Only build_time available (e.g., failure during run)
        result1 = CRSExecutionResult(
            harness_name="test",
            execution_time=50.0,
            success=False,
            output="",
            build_time=50.0,
            run_time=None,
        )
        assert result1.build_time == 50.0
        assert result1.run_time is None

        # Edge case: build succeeded but run failed immediately
        result2 = CRSExecutionResult(
            harness_name="test",
            execution_time=50.1,
            success=False,
            output="",
            build_time=50.0,
            run_time=0.1,
        )
        assert result2.build_time == 50.0
        assert result2.run_time == 0.1
