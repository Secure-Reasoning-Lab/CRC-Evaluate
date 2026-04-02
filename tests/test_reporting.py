"""Tests for the reporting module."""

import json
import tarfile
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from crsbench.reporting import (
    BenchmarkMetrics,
    CRSMetrics,
    ExperimentMetrics,
    ExperimentValidationResult,
    HTMLReportGenerator,
    JSONReportGenerator,
    LLMUsageFile,
    MetricsAggregator,
    ModelUsage,
    SnapshotData,
    SnapshotLoader,
    TimeSeriesPoint,
    TrialInfo,
    TrialMetrics,
    discover_trials,
)
from crsbench.reporting.orchestrator import ReportGenerator
from crsbench.reporting.validator import ExperimentValidator


@pytest.fixture
def temp_dir():
    """Create a temporary directory for test files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def sample_llm_usage() -> LLMUsageFile:
    """Create sample LLM usage data."""
    return LLMUsageFile(
        total_input_tokens=10000,
        total_output_tokens=5000,
        total_cached_tokens=1000,
        total_cost_usd=0.50,
        by_model={
            "claude-sonnet-4": ModelUsage(
                input_tokens=10000,
                output_tokens=5000,
                cost_usd=0.50,
            )
        },
    )


@pytest.fixture
def sample_snapshot_data(temp_dir, sample_llm_usage) -> SnapshotData:
    """Create sample snapshot data for testing."""
    return SnapshotData(
        trial_dir=temp_dir,
        cycle=1,
        timestamp=1234567890.0,
        elapsed_time=900.0,
        running_elapsed_time=800.0,
        snapshot_period=900,
        pov_count=2,
        patch_count=1,
        corpus_count=10,
        pov_names=["pov_001", "pov_002"],
        patch_names=["patch_001"],
        llm_usage=sample_llm_usage,
        has_config=True,
        has_execution_metadata=True,
        has_crs_log=True,
    )


@pytest.fixture
def sample_trial_info(temp_dir) -> TrialInfo:
    """Create sample trial info for testing."""
    return TrialInfo(
        trial_dir=temp_dir,
        trial_num=1,
        crs="ensemble-c",
        benchmark="json-c",
        harness="fuzz_harness",
        mode="bug_finding",
        status="valid",
        snapshot_count=5,
        complete_snapshot_count=5,
    )


@pytest.fixture
def sample_trial_metrics() -> TrialMetrics:
    """Create sample trial metrics for testing."""
    return TrialMetrics(
        trial_dir="/tmp/trial-1",
        trial_num=1,
        benchmark="json-c",
        crs="ensemble-c",
        harness="fuzz_harness",
        mode="bug_finding",
        total_povs_discovered=5,
        unique_pov_names=["pov_001", "pov_002", "pov_003"],
        total_patches_generated=8,
        unique_patch_names=["patch_001", "patch_002", "patch_003", "patch_004"],
        total_llm_cost=12.50,
        total_llm_tokens=50000,
        total_llm_input_tokens=37000,
        total_llm_output_tokens=13000,
        llm_usage_by_model={
            "claude-sonnet-4": {
                "input_tokens": 30000,
                "output_tokens": 10000,
                "cost_usd": 7.50,
            },
            "gpt-4": {"input_tokens": 7000, "output_tokens": 3000, "cost_usd": 5.00},
        },
        total_time=3600.0,
        time_to_first_pov=900.0,
        time_series=[
            TimeSeriesPoint(
                elapsed_time=2700.0,
                running_elapsed_time=0.0,
                cumulative_povs=4,
                cumulative_patches=6,
                llm_tokens=40000,
                llm_input_tokens=30000,
                llm_output_tokens=10000,
                llm_cost=10.00,
            ),
            TimeSeriesPoint(
                elapsed_time=3600.0,
                running_elapsed_time=3200.0,
                cumulative_povs=5,
                cumulative_patches=8,
                llm_tokens=50000,
                llm_input_tokens=37000,
                llm_output_tokens=13000,
                llm_cost=12.50,
            ),
        ],
        snapshot_count=4,
    )


def test_generate_experiment_report_skips_rich_progress_when_stdout_not_tty(
    temp_dir: Path, sample_trial_info: TrialInfo, sample_trial_metrics: TrialMetrics
) -> None:
    generator = ReportGenerator(
        output_dir=temp_dir / "reports",
        benchmarks_root=temp_dir / "benchmarks",
    )
    experiment_metrics = MagicMock()

    with (
        patch(
            "crsbench.reporting.orchestrator.discover_trials",
            return_value=[sample_trial_info],
        ),
        patch.object(
            generator.snapshot_loader,
            "load_trial_snapshots",
            return_value=[MagicMock()],
        ),
        patch.object(generator, "_get_cpv_info", return_value=(1, ["cpv_0"])),
        patch.object(
            generator.metrics_aggregator,
            "aggregate_trial",
            return_value=sample_trial_metrics,
        ),
        patch.object(
            generator.metrics_aggregator,
            "aggregate_experiment",
            return_value=experiment_metrics,
        ),
        patch.object(
            generator.json_generator,
            "generate_trial_report",
            return_value=temp_dir / "trial.json",
        ),
        patch.object(
            generator.json_generator,
            "generate_experiment_report",
            return_value=temp_dir / "experiment.json",
        ),
        patch("sys.stdout.isatty", return_value=False),
        patch("rich.progress.Progress") as progress,
    ):
        result = generator.generate_experiment_report(
            temp_dir / "experiment", format="json"
        )

    assert result["json"] == temp_dir / "experiment.json"
    progress.assert_not_called()


def test_get_experiment_metrics_skips_rich_progress_when_stdout_not_tty(
    temp_dir: Path, sample_trial_info: TrialInfo, sample_trial_metrics: TrialMetrics
) -> None:
    generator = ReportGenerator(
        output_dir=temp_dir / "reports",
        benchmarks_root=temp_dir / "benchmarks",
    )
    experiment_metrics = MagicMock()

    with (
        patch(
            "crsbench.reporting.orchestrator.discover_trials",
            return_value=[sample_trial_info],
        ),
        patch.object(
            generator.snapshot_loader,
            "load_trial_snapshots",
            return_value=[MagicMock()],
        ),
        patch.object(generator, "_get_cpv_info", return_value=(1, ["cpv_0"])),
        patch.object(
            generator.metrics_aggregator,
            "aggregate_trial",
            return_value=sample_trial_metrics,
        ),
        patch.object(
            generator.metrics_aggregator,
            "aggregate_experiment",
            return_value=experiment_metrics,
        ),
        patch("sys.stdout.isatty", return_value=False),
        patch("rich.progress.Progress") as progress,
    ):
        result = generator.get_experiment_metrics(temp_dir / "experiment")

    assert result is experiment_metrics
    progress.assert_not_called()


@pytest.fixture
def create_snapshot_archive(temp_dir):
    """Factory fixture to create snapshot archives."""

    def _create_archive(
        cycle: int,
        pov_count: int = 2,
        patch_count: int = 1,
        *,
        complete: bool = True,
        include_coverage_json: bool = False,
    ) -> Path:
        """Create a snapshot archive with test data."""
        # Create snapshot directory
        snapshot_dir = temp_dir / f"snapshot-{cycle:04d}"
        snapshot_dir.mkdir(parents=True)

        # Create metadata.json (matches SnapshotMetadataFile)
        metadata = {
            "cycle": cycle,
            "timestamp": 1234567890.0 + cycle * 900,
            "elapsed_time": cycle * 900.0,
            "snapshot_period": 900,
            "running_elapsed_time": cycle * 800.0,
        }
        (snapshot_dir / "metadata.json").write_text(json.dumps(metadata))

        # Create POVs directory
        povs_dir = snapshot_dir / "povs"
        povs_dir.mkdir()
        for i in range(pov_count):
            (povs_dir / f"pov_{i:03d}").write_bytes(b"pov binary data")

        # Create patches directory (organized by POV ID)
        patches_dir = snapshot_dir / "patches"
        patches_dir.mkdir()
        for i in range(patch_count):
            patch_pov_dir = patches_dir / f"pov_{i:03d}"
            patch_pov_dir.mkdir()
            (patch_pov_dir / "patch.diff").write_text("--- a/file.c\n+++ b/file.c\n")

        # Create llm-usage.json (matches LLMUsageFile)
        llm_usage = {
            "total_input_tokens": 10000 * cycle,
            "total_output_tokens": 5000 * cycle,
            "total_cached_tokens": 1000 * cycle,
            "total_cost_usd": 0.50 * cycle,
            "by_model": {
                "claude-sonnet-4": {
                    "input_tokens": 10000 * cycle,
                    "output_tokens": 5000 * cycle,
                    "cost_usd": 0.50 * cycle,
                }
            },
        }
        (snapshot_dir / "llm-usage.json").write_text(json.dumps(llm_usage))

        # Create CRS log
        (snapshot_dir / "crs-output.log").write_text(f"CRS log for cycle {cycle}")
        if include_coverage_json:
            (snapshot_dir / "coverage.json").write_text('{"lines_covered": 1}')

        # Create tar.gz archive
        archive_path = temp_dir / f"snapshot-{cycle:04d}.tar.gz"
        with tarfile.open(archive_path, "w:gz") as tar:
            for item in snapshot_dir.iterdir():
                tar.add(item, arcname=item.name)

        # Create completion marker
        if complete:
            (temp_dir / f"snapshot-{cycle:04d}.complete").touch()

        return archive_path

    return _create_archive


class TestSnapshotData:
    """Tests for SnapshotData model."""

    def test_model_dump(self, sample_snapshot_data):
        """Test conversion to dictionary."""
        result = sample_snapshot_data.model_dump()

        assert result["cycle"] == 1
        assert result["pov_count"] == 2
        assert result["patch_count"] == 1
        assert result["elapsed_time"] == 900.0


class TestTrialMetrics:
    """Tests for TrialMetrics model."""

    def test_model_dump(self, sample_trial_metrics):
        """Test conversion to dictionary."""
        result = sample_trial_metrics.model_dump()

        assert result["trial_num"] == 1
        assert result["benchmark"] == "json-c"
        assert result["crs"] == "ensemble-c"
        assert result["total_povs_discovered"] == 5

    def test_unique_povs_property(self, sample_trial_metrics):
        """Test unique_povs computed property."""
        assert sample_trial_metrics.unique_povs == 3

    def test_unique_patches_property(self, sample_trial_metrics):
        """Test unique_patches computed property."""
        assert sample_trial_metrics.unique_patches == 4


class TestSnapshotLoader:
    """Tests for SnapshotLoader."""

    def test_load_snapshot(self, temp_dir, create_snapshot_archive):
        """Test loading a single snapshot archive."""
        # Create a snapshot archive
        archive_path = create_snapshot_archive(cycle=1)

        # Load the snapshot
        loader = SnapshotLoader()
        snapshot = loader.load_snapshot(archive_path, temp_dir)

        assert snapshot.cycle == 1
        assert snapshot.pov_count == 2
        assert snapshot.patch_count == 1
        assert len(snapshot.pov_names) == 2
        assert len(snapshot.patch_names) == 1

    def test_load_trial_snapshots(self, temp_dir, create_snapshot_archive):
        """Test loading all snapshots for a trial."""
        # Create multiple snapshot archives
        create_snapshot_archive(cycle=1)
        create_snapshot_archive(cycle=2)
        create_snapshot_archive(cycle=3)

        # Create trial metadata
        trial_metadata = {
            "timestamp": "2024-01-01T00:00:00",
            "trial_num": 1,
            "crs": "ensemble-c",
            "benchmark": "json-c",
            "harness": "fuzz_harness",
            "mode": "bug_finding",
            "source": {"path": "/src", "commit": "abc123"},
        }
        (temp_dir / "metadata.json").write_text(json.dumps(trial_metadata))

        # Load snapshots
        loader = SnapshotLoader()
        snapshots = loader.load_trial_snapshots(temp_dir)

        assert len(snapshots) == 3
        assert snapshots[0].cycle == 1
        assert snapshots[1].cycle == 2
        assert snapshots[2].cycle == 3

    def test_skip_incomplete_snapshots(self, temp_dir, create_snapshot_archive):
        """Test that incomplete snapshots are skipped."""
        create_snapshot_archive(cycle=1, complete=True)
        create_snapshot_archive(cycle=2, complete=False)  # Incomplete
        create_snapshot_archive(cycle=3, complete=True)

        loader = SnapshotLoader()
        snapshots = loader.load_trial_snapshots(temp_dir)

        # Should only load complete snapshots (cycles 1 and 3)
        assert len(snapshots) == 2
        assert snapshots[0].cycle == 1
        assert snapshots[1].cycle == 3

    def test_load_snapshot_marks_post_trial_timeline_as_coverage(
        self, temp_dir, create_snapshot_archive
    ):
        archive_path = create_snapshot_archive(cycle=1, include_coverage_json=False)
        coverage_dir = temp_dir / "coverage"
        coverage_dir.mkdir()
        (coverage_dir / "coverage_timeline.json").write_text("{}")

        loader = SnapshotLoader()
        snapshot = loader.load_snapshot(archive_path, temp_dir)

        assert snapshot.has_coverage is True

    def test_load_snapshot_marks_final_coverage_summary_as_coverage(
        self, temp_dir, create_snapshot_archive
    ):
        archive_path = create_snapshot_archive(cycle=1, include_coverage_json=False)
        (temp_dir / "final_coverage.json").write_text("{}")

        loader = SnapshotLoader()
        snapshot = loader.load_snapshot(archive_path, temp_dir)

        assert snapshot.has_coverage is True

    def test_load_snapshot_marks_has_coverage_from_snapshot_file(
        self, temp_dir, create_snapshot_archive
    ):
        archive_path = create_snapshot_archive(cycle=1, include_coverage_json=True)

        loader = SnapshotLoader()
        snapshot = loader.load_snapshot(archive_path, temp_dir)

        assert snapshot.has_coverage is True

    def test_load_snapshot_marks_has_coverage_from_trial_timeline(
        self, temp_dir, create_snapshot_archive
    ):
        archive_path = create_snapshot_archive(cycle=1)
        coverage_dir = temp_dir / "coverage"
        coverage_dir.mkdir()
        (coverage_dir / "coverage_timeline.json").write_text("{}")

        loader = SnapshotLoader()
        snapshot = loader.load_snapshot(archive_path, temp_dir)

        assert snapshot.has_coverage is True

    def test_load_snapshot_marks_has_coverage_from_final_coverage(
        self, temp_dir, create_snapshot_archive
    ):
        archive_path = create_snapshot_archive(cycle=1)
        (temp_dir / "final_coverage.json").write_text("{}")

        loader = SnapshotLoader()
        snapshot = loader.load_snapshot(archive_path, temp_dir)

        assert snapshot.has_coverage is True

    def test_load_snapshot_keeps_has_coverage_false_without_coverage_artifacts(
        self, temp_dir, create_snapshot_archive
    ):
        archive_path = create_snapshot_archive(cycle=1)

        loader = SnapshotLoader()
        snapshot = loader.load_snapshot(archive_path, temp_dir)

        assert snapshot.has_coverage is False


class TestMetricsAggregator:
    """Tests for MetricsAggregator."""

    def test_aggregate_trial(self, temp_dir, sample_llm_usage):
        """Test aggregating metrics for a trial."""
        trial_info = TrialInfo(
            trial_dir=temp_dir,
            trial_num=1,
            crs="ensemble-c",
            benchmark="json-c",
            harness="fuzz_harness",
            mode="bug_finding",
            status="valid",
        )

        # Create multiple snapshots
        snapshots = [
            SnapshotData(
                trial_dir=temp_dir,
                cycle=1,
                timestamp=1000.0,
                elapsed_time=900.0,
                running_elapsed_time=800.0,
                snapshot_period=900,
                pov_count=1,
                patch_count=0,
                pov_names=["pov_001"],
                patch_names=[],
                llm_usage=LLMUsageFile(
                    total_input_tokens=5000,
                    total_output_tokens=2500,
                    total_cost_usd=0.25,
                ),
            ),
            SnapshotData(
                trial_dir=temp_dir,
                cycle=2,
                timestamp=2000.0,
                elapsed_time=1800.0,
                running_elapsed_time=1600.0,
                snapshot_period=900,
                pov_count=2,
                patch_count=1,
                pov_names=["pov_002", "pov_003"],
                patch_names=["patch_001"],
                llm_usage=LLMUsageFile(
                    total_input_tokens=10000,
                    total_output_tokens=5000,
                    total_cost_usd=0.50,
                ),
            ),
        ]

        aggregator = MetricsAggregator()
        metrics = aggregator.aggregate_trial(
            trial_info=trial_info,
            snapshots=snapshots,
        )

        assert metrics.trial_num == 1
        assert metrics.benchmark == "json-c"
        assert metrics.crs == "ensemble-c"
        assert metrics.total_povs_discovered == 3  # 1 + 2
        assert metrics.total_patches_generated == 1  # 0 + 1
        assert metrics.total_llm_cost == 0.50  # From final snapshot
        assert metrics.time_to_first_pov == 900.0

    def test_aggregate_experiment(self, sample_trial_metrics):
        """Test aggregating metrics for an experiment."""
        trial_metrics_list = [
            sample_trial_metrics,
            TrialMetrics(
                trial_dir="/tmp/trial-2",
                trial_num=2,
                benchmark="json-c",
                crs="multi-retrieval",
                harness="fuzz_harness",
                mode="bug_finding",
                total_povs_discovered=3,
                total_patches_generated=5,
                total_llm_cost=8.00,
            ),
        ]

        aggregator = MetricsAggregator()
        exp_metrics = aggregator.aggregate_experiment(
            experiment_dir="/tmp/test-exp",
            trial_metrics_list=trial_metrics_list,
        )

        assert exp_metrics.experiment_dir == "/tmp/test-exp"
        assert exp_metrics.total_trials == 2
        assert exp_metrics.avg_povs_per_trial == 4.0  # (5 + 3) / 2
        assert len(exp_metrics.by_crs) == 2
        assert len(exp_metrics.by_benchmark) == 1

    def test_empty_snapshots(self, temp_dir):
        """Test handling empty snapshot list."""
        trial_info = TrialInfo(
            trial_dir=temp_dir,
            trial_num=1,
            crs="ensemble-c",
            benchmark="json-c",
            harness="fuzz_harness",
            mode="bug_finding",
            status="valid",
        )

        aggregator = MetricsAggregator()
        metrics = aggregator.aggregate_trial(
            trial_info=trial_info,
            snapshots=[],
        )

        assert metrics.total_povs_discovered == 0
        assert metrics.total_patches_generated == 0


class TestExperimentValidator:
    """Tests for ExperimentValidator."""

    def test_validate_complete_experiment(self, temp_dir):
        """Test validating a complete experiment."""
        # Create experiment structure
        exp_dir = temp_dir / "test-exp"
        exp_dir.mkdir()

        # Create trial directory and metadata
        trial_dir = exp_dir / "json-c__ensemble-c" / "trial-1"
        trial_dir.mkdir(parents=True)

        trial_metadata = {
            "timestamp": "2024-01-01T00:00:00",
            "trial_num": 1,
            "crs": "ensemble-c",
            "benchmark": "json-c",
            "harness": "fuzz_harness",
            "mode": "bug_finding",
            "source": {"path": "/src", "commit": "abc123"},
        }
        (trial_dir / "metadata.json").write_text(json.dumps(trial_metadata))

        # Validate
        validator = ExperimentValidator()
        result = validator.validate_experiment_completeness(exp_dir)

        assert result.is_complete()
        assert result.valid_count == 1
        assert result.missing_metadata_count == 0

    def test_validate_missing_metadata(self, temp_dir):
        """Test detecting missing metadata."""
        # Create experiment structure without metadata
        exp_dir = temp_dir / "test-exp"
        exp_dir.mkdir()

        trial_dir = exp_dir / "json-c__ensemble-c" / "trial-1"
        trial_dir.mkdir(parents=True)
        # Don't create metadata.json

        validator = ExperimentValidator()
        result = validator.validate_experiment_completeness(exp_dir)

        assert not result.is_complete()
        assert result.missing_metadata_count == 1

    def test_generate_completeness_report(self, temp_dir):
        """Test generating a completeness report."""
        result = ExperimentValidationResult(
            experiment_dir=str(temp_dir),
            total_discovered=3,
            valid_count=2,
            missing_metadata_count=1,
            invalid_metadata_count=0,
            valid_trials=[
                TrialInfo(
                    trial_dir=temp_dir / "trial-1",
                    trial_num=1,
                    benchmark="json-c",
                    crs="ensemble-c",
                    harness="fuzz_harness",
                    mode="bug_finding",
                ),
                TrialInfo(
                    trial_dir=temp_dir / "trial-2",
                    trial_num=2,
                    benchmark="json-c",
                    crs="ensemble-c",
                    harness="fuzz_harness",
                    mode="bug_finding",
                ),
            ],
            invalid_trials=[
                TrialInfo(
                    trial_dir=temp_dir / "trial-3",
                    trial_num=3,
                    status="missing_metadata",
                    error="metadata.json not found",
                )
            ],
        )

        validator = ExperimentValidator()
        report = validator.generate_completeness_report(result)

        assert "Experiment Completeness Report" in report
        assert "Total discovered trials: 3" in report
        assert "Invalid Trials:" in report


class TestJSONReportGenerator:
    """Tests for JSONReportGenerator."""

    def test_generate_trial_report(
        self, temp_dir, sample_trial_metrics, sample_llm_usage
    ):
        """Test generating a trial JSON report."""
        snapshots = [
            SnapshotData(
                trial_dir=temp_dir,
                cycle=1,
                timestamp=1000.0,
                elapsed_time=900.0,
                running_elapsed_time=800.0,
                snapshot_period=900,
                pov_count=1,
                patch_count=0,
                pov_names=["pov_001"],
                patch_names=[],
                llm_usage=sample_llm_usage,
            )
        ]

        generator = JSONReportGenerator(output_dir=temp_dir)
        report_path = generator.generate_trial_report(sample_trial_metrics, snapshots)

        assert report_path.exists()
        assert report_path.suffix == ".json"

        # Verify content
        report = json.loads(report_path.read_text())
        assert report["report_type"] == "trial"
        assert report["trial"]["trial_num"] == 1
        assert report["trial"]["benchmark"] == "json-c"

    def test_generate_experiment_report(self, temp_dir, sample_trial_metrics):
        """Test generating an experiment JSON report."""
        exp_metrics = ExperimentMetrics(
            experiment_dir="/tmp/test-exp",
            total_trials=1,
            valid_trials=1,
            avg_povs_per_trial=5.0,
            avg_patches_per_trial=8.0,
            avg_cost_per_trial=12.50,
            by_crs={
                "ensemble-c": CRSMetrics(crs="ensemble-c", trial_count=1, avg_povs=5.0)
            },
            by_benchmark={
                "json-c": BenchmarkMetrics(
                    benchmark="json-c", trial_count=1, avg_povs=5.0
                )
            },
            trial_metrics=[sample_trial_metrics],
        )

        generator = JSONReportGenerator(output_dir=temp_dir)
        report_path = generator.generate_experiment_report(exp_metrics)

        assert report_path.exists()

        report = json.loads(report_path.read_text())
        assert report["report_type"] == "experiment"
        assert report["summary"]["total_trials"] == 1


class TestHTMLReportGenerator:
    """Tests for HTMLReportGenerator."""

    def test_generate_trial_report(
        self, temp_dir, sample_trial_metrics, sample_llm_usage
    ):
        """Test generating a trial HTML report."""
        snapshots = [
            SnapshotData(
                trial_dir=temp_dir,
                cycle=1,
                timestamp=1000.0,
                elapsed_time=900.0,
                running_elapsed_time=800.0,
                snapshot_period=900,
                pov_count=1,
                patch_count=0,
                pov_names=["pov_001"],
                patch_names=[],
                llm_usage=sample_llm_usage,
            )
        ]

        generator = HTMLReportGenerator(output_dir=temp_dir)
        report_path = generator.generate_trial_report(sample_trial_metrics, snapshots)

        assert report_path.exists()
        assert report_path.suffix == ".html"

        # Verify content
        content = report_path.read_text()
        assert "Trial Report" in content
        assert "Trial 1" in content
        assert "Plotly" in content  # Should include Plotly

    def test_generate_experiment_report(self, temp_dir, sample_trial_metrics):
        """Test generating an experiment HTML report."""
        exp_metrics = ExperimentMetrics(
            experiment_dir="/tmp/test-exp",
            total_trials=1,
            valid_trials=1,
            avg_povs_per_trial=5.0,
            avg_patches_per_trial=8.0,
            avg_cost_per_trial=12.50,
            by_crs={
                "ensemble-c": CRSMetrics(
                    crs="ensemble-c", trial_count=1, avg_povs=5.0, total_cost=12.50
                )
            },
            by_benchmark={
                "json-c": BenchmarkMetrics(
                    benchmark="json-c", trial_count=1, avg_povs=5.0, avg_patches=8.0
                )
            },
            trial_metrics=[sample_trial_metrics],
        )

        generator = HTMLReportGenerator(output_dir=temp_dir)
        report_path = generator.generate_experiment_report(exp_metrics)

        assert report_path.exists()

        content = report_path.read_text()
        assert "Experiment Report" in content
        assert "test-exp" in content


class TestDiscoverTrials:
    """Tests for discover_trials function."""

    def test_discover_from_directory_scan(self, temp_dir):
        """Test discovering trials by directory scanning."""
        exp_dir = temp_dir / "test-exp"
        exp_dir.mkdir()

        # Create trial directory with metadata
        trial_dir = exp_dir / "json-c__ensemble-c" / "trial-1"
        trial_dir.mkdir(parents=True)
        trial_metadata = {
            "timestamp": "2024-01-01T00:00:00",
            "trial_num": 1,
            "crs": "ensemble-c",
            "benchmark": "json-c",
            "harness": "fuzz_harness",
            "mode": "bug_finding",
            "source": {"path": "/src", "commit": "abc123"},
        }
        (trial_dir / "metadata.json").write_text(json.dumps(trial_metadata))

        trials = discover_trials(exp_dir)

        assert len(trials) == 1
        assert trials[0].trial_num == 1
        assert trials[0].status == "valid"

    def test_discover_multiple_trials(self, temp_dir):
        """Test discovering multiple trials."""
        exp_dir = temp_dir / "test-exp"
        exp_dir.mkdir()

        # Create trial 1
        trial1_dir = exp_dir / "json-c__ensemble-c" / "trial-1"
        trial1_dir.mkdir(parents=True)
        trial1_metadata = {
            "timestamp": "2024-01-01T00:00:00",
            "trial_num": 1,
            "crs": "ensemble-c",
            "benchmark": "json-c",
            "harness": "fuzz_harness",
            "mode": "bug_finding",
            "source": {"path": "/src", "commit": "abc123"},
        }
        (trial1_dir / "metadata.json").write_text(json.dumps(trial1_metadata))

        # Create trial 2 (missing metadata)
        trial2_dir = exp_dir / "json-c__multi-retrieval" / "trial-1"
        trial2_dir.mkdir(parents=True)
        # Don't create metadata.json

        trials = discover_trials(exp_dir)

        assert len(trials) == 2
        valid_trials = [t for t in trials if t.status == "valid"]
        missing_trials = [t for t in trials if t.status == "missing_metadata"]
        assert len(valid_trials) == 1
        assert len(missing_trials) == 1


class TestReportGeneratorCPVFiltering:
    """Tests for ReportGenerator CPV filtering by sanitizer."""

    def test_get_cpv_info_filters_by_sanitizer(self, temp_dir):
        """Test that _get_cpv_info filters CPVs by sanitizer."""
        from crsbench.reporting.orchestrator import ReportGenerator
        from crsbench.validation.schemas import (
            POV,
            BenchmarkConfig,
            HarnessFile,
            Vulnerability,
        )

        # Create a benchmark directory with meta.yaml
        benchmark_dir = temp_dir / "benchmarks" / "test-benchmark"
        benchmark_dir.mkdir(parents=True)

        # Create meta.yaml with CPVs having different sanitizers
        from crsbench.validation.schemas import FullMode

        config = BenchmarkConfig(
            harness_files=[
                HarnessFile(
                    name="test_harness",
                    path="/src/test_harness.c",
                    vulns=[
                        Vulnerability(
                            vuln_keyword="cpv_0",
                            povs=[
                                POV(id="pov_0", sanitizer="address"),
                            ],
                        ),
                        Vulnerability(
                            vuln_keyword="cpv_1",
                            povs=[
                                POV(id="pov_1", sanitizer="undefined"),
                            ],
                        ),
                        Vulnerability(
                            vuln_keyword="cpv_2",
                            povs=[
                                POV(id="pov_2_asan", sanitizer="address"),
                                POV(id="pov_2_ubsan", sanitizer="undefined"),
                            ],
                        ),
                    ],
                )
            ],
            full_mode=FullMode(base_commit="abc123def456"),
        )

        # Write meta.yaml
        meta_yaml_path = benchmark_dir / ".aixcc" / "meta.yaml"
        config.to_yaml(meta_yaml_path)

        # Create trial directory with sanitizer in path
        # Pattern: <experiment>/<config>/<harness>/<run_mode>/<sanitizer>/trial-N
        trial_dir_address = (
            temp_dir
            / "test-exp"
            / "ensemble-c"
            / "test_harness"
            / "full"
            / "address"
            / "trial-1"
        )
        trial_dir_address.mkdir(parents=True)

        trial_dir_undefined = (
            temp_dir
            / "test-exp"
            / "ensemble-c"
            / "test_harness"
            / "full"
            / "undefined"
            / "trial-2"
        )
        trial_dir_undefined.mkdir(parents=True)

        # Create ReportGenerator
        generator = ReportGenerator(
            output_dir=temp_dir / "reports",
            benchmarks_root=temp_dir / "benchmarks",
        )

        # Test address sanitizer trial
        trial_info_address = TrialInfo(
            trial_dir=trial_dir_address,
            trial_num=1,
            crs="ensemble-c",
            benchmark="test-benchmark",
            harness="test_harness",
            mode="bug_finding",
            status="valid",
        )

        total_cpvs, cpv_ids = generator._get_cpv_info(trial_info_address)

        # Should only get CPVs with address sanitizer POVs (cpv_0, cpv_2)
        assert total_cpvs == 2
        assert set(cpv_ids) == {"cpv_0", "cpv_2"}

        # Test undefined sanitizer trial
        trial_info_undefined = TrialInfo(
            trial_dir=trial_dir_undefined,
            trial_num=2,
            crs="ensemble-c",
            benchmark="test-benchmark",
            harness="test_harness",
            mode="bug_finding",
            status="valid",
        )

        total_cpvs, cpv_ids = generator._get_cpv_info(trial_info_undefined)

        # Should only get CPVs with undefined sanitizer POVs (cpv_1, cpv_2)
        assert total_cpvs == 2
        assert set(cpv_ids) == {"cpv_1", "cpv_2"}
