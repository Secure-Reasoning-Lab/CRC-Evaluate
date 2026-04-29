"""Tests for execution timing timestamps carried through result models."""

import json

import pytest
from crsbench.evaluation.results import (
    CRSExecutionResult,
    HarnessResult,
)
from crsbench.evaluation.results import (
    TrialMetadata as EvaluationTrialMetadata,
)
from crsbench.validation.schemas import (
    SourceInfo,
    TrialMode,
)
from crsbench.validation.schemas import (
    TrialMetadata as FileTrialMetadata,
)


def test_crs_execution_result_accepts_detailed_timing_fields() -> None:
    """CRSExecutionResult should expose build/run start and end times."""
    result = CRSExecutionResult(
        harness_name="example",
        execution_time=12.5,
        success=True,
        output="ok",
        build_start_time=1000.0,
        build_end_time=1003.5,
        run_start_time=1004.0,
        run_end_time=1012.5,
    )

    assert result.build_start_time == pytest.approx(1000.0)
    assert result.build_end_time == pytest.approx(1003.5)
    assert result.run_start_time == pytest.approx(1004.0)
    assert result.run_end_time == pytest.approx(1012.5)


def test_harness_result_carries_detailed_timing_fields() -> None:
    """HarnessResult should carry build/run start and end times."""
    result = HarnessResult(
        name="example",
        path="/src/example.c",
        execution_time=12.5,
        build_start_time=2000.0,
        build_end_time=2002.0,
        run_start_time=2002.5,
        run_end_time=2012.5,
    )

    assert result.build_start_time == pytest.approx(2000.0)
    assert result.build_end_time == pytest.approx(2002.0)
    assert result.run_start_time == pytest.approx(2002.5)
    assert result.run_end_time == pytest.approx(2012.5)


def test_evaluation_trial_metadata_serialization_includes_unix_and_stage_times() -> (
    None
):
    """Shared TrialMetadata serialization should include detailed timing fields."""
    metadata = EvaluationTrialMetadata(
        timestamp_start=3000.0,
        timestamp_end=3015.0,
        timestamp_unix=3000.25,
        build_start_time=3001.0,
        build_end_time=3005.0,
        run_start_time=3005.5,
        run_end_time=3015.0,
    )

    data = metadata.model_dump()

    assert data["timestamp_unix"] == pytest.approx(3000.25)
    assert data["build_start_time"] == pytest.approx(3001.0)
    assert data["build_end_time"] == pytest.approx(3005.0)
    assert data["run_start_time"] == pytest.approx(3005.5)
    assert data["run_end_time"] == pytest.approx(3015.0)

    loaded = json.loads(metadata.model_dump_json())
    assert loaded["timestamp_unix"] == pytest.approx(3000.25)
    assert loaded["build_start_time"] == pytest.approx(3001.0)
    assert loaded["build_end_time"] == pytest.approx(3005.0)
    assert loaded["run_start_time"] == pytest.approx(3005.5)
    assert loaded["run_end_time"] == pytest.approx(3015.0)


def test_file_trial_metadata_accepts_unix_and_stage_times_without_changing_iso_timestamp() -> (
    None
):
    """File-backed TrialMetadata should accept new float fields and keep ISO timestamp."""
    metadata = FileTrialMetadata(
        timestamp="2026-04-29T12:00:00Z",
        trial_num=7,
        crs="example-crs",
        benchmark="example-benchmark",
        harness="example-harness",
        mode=TrialMode.bug_finding,
        source=SourceInfo(path="/tmp/src"),
        timestamp_unix=4000.25,
        build_start_time=4001.0,
        build_end_time=4004.0,
        run_start_time=4004.5,
        run_end_time=4010.0,
    )

    data = metadata.model_dump()

    assert data["timestamp"] == "2026-04-29T12:00:00Z"
    assert data["timestamp_unix"] == pytest.approx(4000.25)
    assert data["build_start_time"] == pytest.approx(4001.0)
    assert data["build_end_time"] == pytest.approx(4004.0)
    assert data["run_start_time"] == pytest.approx(4004.5)
    assert data["run_end_time"] == pytest.approx(4010.0)
