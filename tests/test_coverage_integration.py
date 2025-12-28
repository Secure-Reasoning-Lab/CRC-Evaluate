"""Integration tests for coverage module with snapshot manager."""

import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock


class TestCoverageManagerIntegration:
    """Tests for CoverageManager integration with SnapshotManager."""

    def test_coverage_manager_on_snapshot(self):
        """Test CoverageManager.on_snapshot() returns proper snapshot."""
        from crsbench.evaluation.coverage.collector import CoverageCollector
        from crsbench.evaluation.coverage.manager import CoverageManager
        from crsbench.evaluation.coverage.models import CoverageConfig
        from crsbench.evaluation.coverage.store import CoverageStore

        with tempfile.TemporaryDirectory() as tmp_dir:
            trial_dir = Path(tmp_dir) / "trial"
            trial_dir.mkdir()
            corpus_dir = trial_dir / "output" / "corpus"
            corpus_dir.mkdir(parents=True)

            # Create mock strategy
            mock_strategy = MagicMock()
            mock_strategy.project_name = "test-project"

            # Create store and collector with harness_name
            store = CoverageStore(trial_dir / "coverage")
            collector = CoverageCollector(
                mock_strategy, store, harness_name="fuzz_test"
            )

            # Create manager
            config = CoverageConfig(enabled=True, saturation_time=300)
            manager = CoverageManager(
                trial_dir=trial_dir,
                collector=collector,
                config=config,
                harness_name="fuzz_test",
                corpus_dir=corpus_dir,
            )

            # Call on_snapshot
            snapshot = manager.on_snapshot(cycle=1)

            assert snapshot.cycle == 1
            assert snapshot.harness_name == "fuzz_test"
            assert snapshot.saturation_detected is False

    def test_coverage_manager_saturation_detection(self):
        """Test time-based saturation detection."""
        from crsbench.evaluation.coverage.collector import CoverageCollector
        from crsbench.evaluation.coverage.manager import CoverageManager
        from crsbench.evaluation.coverage.models import CoverageConfig
        from crsbench.evaluation.coverage.store import CoverageStore

        with tempfile.TemporaryDirectory() as tmp_dir:
            trial_dir = Path(tmp_dir) / "trial"
            trial_dir.mkdir()
            corpus_dir = trial_dir / "output" / "corpus"
            corpus_dir.mkdir(parents=True)

            mock_strategy = MagicMock()
            mock_strategy.project_name = "test-project"

            store = CoverageStore(trial_dir / "coverage")
            collector = CoverageCollector(
                mock_strategy, store, harness_name="fuzz_test"
            )

            # Create manager with short saturation time
            config = CoverageConfig(enabled=True, saturation_time=1)  # 1 second
            manager = CoverageManager(
                trial_dir=trial_dir,
                collector=collector,
                config=config,
                harness_name="fuzz_test",
                corpus_dir=corpus_dir,
                trial_start_time=time.time() - 2,  # Started 2 seconds ago
            )

            # First snapshot should detect saturation (no new coverage for > 1s)
            snapshot = manager.on_snapshot(cycle=1)
            assert snapshot.saturation_detected is True

    def test_snapshot_manager_captures_coverage(self):
        """Test SnapshotManager captures coverage data in snapshot."""
        from crsbench.evaluation.coverage.collector import CoverageCollector
        from crsbench.evaluation.coverage.manager import CoverageManager
        from crsbench.evaluation.coverage.models import CoverageConfig
        from crsbench.evaluation.coverage.store import CoverageStore
        from crsbench.evaluation.snapshot_manager import SnapshotManager

        with tempfile.TemporaryDirectory() as tmp_dir:
            trial_dir = Path(tmp_dir) / "trial"
            trial_dir.mkdir()
            corpus_dir = trial_dir / "output" / "corpus"
            corpus_dir.mkdir(parents=True)

            mock_strategy = MagicMock()
            mock_strategy.project_name = "test-project"

            store = CoverageStore(trial_dir / "coverage")
            collector = CoverageCollector(
                mock_strategy, store, harness_name="fuzz_test"
            )

            config = CoverageConfig(enabled=True, saturation_time=300)
            coverage_manager = CoverageManager(
                trial_dir=trial_dir,
                collector=collector,
                config=config,
                harness_name="fuzz_test",
                corpus_dir=corpus_dir,
            )

            # Create SnapshotManager with coverage manager
            snapshot_manager = SnapshotManager(
                trial_dir=trial_dir,
                snapshot_period=60,
                coverage_manager=coverage_manager,
            )

            # Manually capture snapshot
            snapshot = snapshot_manager.capture_snapshot()

            # Verify snapshot was created
            assert snapshot.cycle == 1
            assert snapshot.is_complete is True

    def test_snapshot_archive_contains_coverage_json(self):
        """Test that coverage.json is included in snapshot archive."""
        import json
        import tarfile

        from crsbench.evaluation.coverage.collector import CoverageCollector
        from crsbench.evaluation.coverage.manager import CoverageManager
        from crsbench.evaluation.coverage.models import CoverageConfig
        from crsbench.evaluation.coverage.store import CoverageStore
        from crsbench.evaluation.snapshot_manager import SnapshotManager

        with tempfile.TemporaryDirectory() as tmp_dir:
            trial_dir = Path(tmp_dir) / "trial"
            trial_dir.mkdir()
            corpus_dir = trial_dir / "output" / "corpus"
            corpus_dir.mkdir(parents=True)

            mock_strategy = MagicMock()
            mock_strategy.project_name = "test-project"

            store = CoverageStore(trial_dir / "coverage")
            collector = CoverageCollector(
                mock_strategy, store, harness_name="fuzz_test"
            )

            config = CoverageConfig(enabled=True, saturation_time=300)
            coverage_manager = CoverageManager(
                trial_dir=trial_dir,
                collector=collector,
                config=config,
                harness_name="fuzz_test",
                corpus_dir=corpus_dir,
            )

            # Create SnapshotManager with coverage manager
            snapshot_manager = SnapshotManager(
                trial_dir=trial_dir,
                snapshot_period=60,
                coverage_manager=coverage_manager,
            )

            # Capture snapshot
            snapshot = snapshot_manager.capture_snapshot()

            # Extract and verify coverage.json is in archive
            archive_path = trial_dir / "snapshot-0001.tar.gz"
            assert archive_path.exists()

            extract_dir = Path(tmp_dir) / "extracted"
            extract_dir.mkdir()

            with tarfile.open(archive_path, "r:gz") as tar:
                tar.extractall(extract_dir)

            # Verify coverage.json exists and has correct content
            coverage_json = extract_dir / "coverage.json"
            assert coverage_json.exists(), "coverage.json should be in snapshot archive"

            coverage_data = json.loads(coverage_json.read_text())
            assert coverage_data["cycle"] == 1
            assert coverage_data["harness_name"] == "fuzz_test"
            assert "summary" in coverage_data
            assert coverage_data["saturation_detected"] is False

    def test_coverage_collector_with_harness_name(self):
        """Test CoverageCollector correctly uses harness_name."""
        from crsbench.evaluation.coverage.collector import CoverageCollector
        from crsbench.evaluation.coverage.store import CoverageStore

        with tempfile.TemporaryDirectory() as tmp_dir:
            store = CoverageStore(Path(tmp_dir) / "coverage")
            mock_strategy = MagicMock()
            mock_strategy.project_name = "test-project"

            # Create collector with harness name
            collector = CoverageCollector(
                mock_strategy, store, harness_name="fuzz_parse_input"
            )

            assert collector.harness_name == "fuzz_parse_input"


class TestCoverageBuilderIntegration:
    """Tests for CoverageBuilder integration."""

    def test_builder_creates_variant_path(self):
        """Test CoverageBuilder creates correct variant paths."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            oss_fuzz = Path(tmp_dir)
            (oss_fuzz / "infra").mkdir()
            (oss_fuzz / "infra" / "helper.py").touch()
            (oss_fuzz / "projects").mkdir()
            (oss_fuzz / "build" / "out").mkdir(parents=True)

            from crsbench.evaluation.coverage.builder import CoverageBuilder

            builder = CoverageBuilder(oss_fuzz)

            # Test variant name
            assert (
                builder.get_coverage_variant_name("mock-c-delta-01")
                == "mock-c-delta-01-coverage"
            )

            # Test build output path
            build_path = builder.get_build_output_path("mock-c-delta-01-coverage")
            assert build_path == oss_fuzz / "build" / "out" / "mock-c-delta-01-coverage"
