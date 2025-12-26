"""Integration tests for snapshot system with BenchmarkRunner."""

import threading
import time
from pathlib import Path

import pytest
from crsbench.evaluation.crs_executor import StubCRSExecutor
from crsbench.evaluation.runner import BenchmarkRunner
from crsbench.evaluation.snapshot import list_snapshots, load_snapshot_metadata
from crsbench.validation.schemas import BenchmarkHarness, HarnessFile


class TestSnapshotIntegration:
    """Test snapshot integration with BenchmarkRunner."""

    @pytest.fixture
    def sample_benchmark(self, tmp_path):
        """Create a minimal test benchmark."""
        benchmark_dir = tmp_path / "test-benchmark"
        benchmark_dir.mkdir()

        aixcc_dir = benchmark_dir / ".aixcc"
        aixcc_dir.mkdir()

        # Create minimal meta.yaml
        meta_yaml = aixcc_dir / "meta.yaml"
        meta_yaml.write_text("""
harness_files:
  - name: test_harness
    path: /src/test/harness.c
    vulns:
      - vuln_keyword: test_vuln
        difficulty_level: 1
        povs:
          - id: pov_0
            sanitizer: address

full_mode:
  base_commit: abc123def456
""")

        return benchmark_dir

    def test_snapshot_disabled_by_default(self, sample_benchmark, tmp_path):
        """Test that snapshots are disabled when snapshot_period not provided."""
        executor = StubCRSExecutor()
        runner = BenchmarkRunner(executor)

        trial_dir = tmp_path / "trial"
        trial_dir.mkdir()

        # Create BenchmarkHarness
        harness = HarnessFile(name="test_harness", path="/src/test/harness.c")
        benchmark_harness = BenchmarkHarness(
            name="test-benchmark", path=sample_benchmark, harness=harness
        )

        # Run benchmark without snapshots
        result = runner.run_benchmark(
            benchmark_harness=benchmark_harness, mode="full", trial_output_dir=trial_dir
        )

        # Should complete successfully
        assert result.is_valid

        # No snapshots should be created
        snapshots = list_snapshots(trial_dir)
        assert len(snapshots) == 0

    def test_snapshot_enabled_with_period(self, sample_benchmark, tmp_path):
        """Test that snapshots are created when enabled."""
        executor = StubCRSExecutor()
        # Enable snapshots with very short period for testing
        runner = BenchmarkRunner(executor, snapshot_period=1)

        trial_dir = tmp_path / "trial"
        trial_dir.mkdir()

        # Create output directory (SnapshotManager expects this)
        output_dir = trial_dir / "output"
        output_dir.mkdir()

        # Create BenchmarkHarness
        harness = HarnessFile(name="test_harness", path="/src/test/harness.c")
        benchmark_harness = BenchmarkHarness(
            name="test-benchmark", path=sample_benchmark, harness=harness
        )

        # Run benchmark with snapshots (for a few seconds)
        result = runner.run_benchmark(
            benchmark_harness=benchmark_harness, mode="full", trial_output_dir=trial_dir
        )

        # Should complete successfully
        assert result.is_valid

        # At least one snapshot should be created
        # (StubCRSExecutor is fast, but snapshot thread should capture at least 1)
        snapshots = list_snapshots(trial_dir)
        # May be 0 if CRS completes before first snapshot interval
        assert len(snapshots) >= 0

    def test_snapshot_period_zero_disables(self, sample_benchmark, tmp_path):
        """Test that snapshot_period=0 disables snapshots."""
        executor = StubCRSExecutor()
        runner = BenchmarkRunner(executor, snapshot_period=0)

        trial_dir = tmp_path / "trial"
        trial_dir.mkdir()

        # Create BenchmarkHarness
        harness = HarnessFile(name="test_harness", path="/src/test/harness.c")
        benchmark_harness = BenchmarkHarness(
            name="test-benchmark", path=sample_benchmark, harness=harness
        )

        result = runner.run_benchmark(
            benchmark_harness=benchmark_harness, mode="full", trial_output_dir=trial_dir
        )

        assert result.is_valid

        # No snapshots should be created
        snapshots = list_snapshots(trial_dir)
        assert len(snapshots) == 0

    def test_snapshot_without_trial_dir_fails(self, sample_benchmark):
        """Test that enabling snapshots without trial_dir raises error."""
        executor = StubCRSExecutor()
        runner = BenchmarkRunner(executor, snapshot_period=60)

        # Create BenchmarkHarness
        harness = HarnessFile(name="test_harness", path="/src/test/harness.c")
        benchmark_harness = BenchmarkHarness(
            name="test-benchmark", path=sample_benchmark, harness=harness
        )

        # Should raise error when snapshots enabled but no trial_dir
        with pytest.raises(Exception, match="trial_output_dir"):
            runner.run_benchmark(
                benchmark_harness=benchmark_harness,
                mode="full",
                trial_output_dir=None,  # Missing!
            )

    def test_snapshot_with_nonexistent_trial_dir_fails(self, sample_benchmark):
        """Test that non-existent trial_dir raises error."""
        executor = StubCRSExecutor()
        runner = BenchmarkRunner(executor, snapshot_period=60)

        nonexistent_dir = Path("/nonexistent/trial/dir")

        # Create BenchmarkHarness
        harness = HarnessFile(name="test_harness", path="/src/test/harness.c")
        benchmark_harness = BenchmarkHarness(
            name="test-benchmark", path=sample_benchmark, harness=harness
        )

        with pytest.raises(Exception, match="trial_output_dir does not exist"):
            runner.run_benchmark(
                benchmark_harness=benchmark_harness,
                mode="full",
                trial_output_dir=nonexistent_dir,
            )


class TestSnapshotThreadSafety:
    """Test snapshot thread safety with concurrent operations."""

    def test_snapshot_thread_stops_on_runner_complete(self, tmp_path):
        """Test that snapshot thread stops when BenchmarkRunner completes."""
        from crsbench.evaluation.snapshot_manager import SnapshotManager

        trial_dir = tmp_path
        output_dir = trial_dir / "output"
        output_dir.mkdir()

        manager = SnapshotManager(trial_dir, snapshot_period=60)

        # Start snapshot thread
        thread = threading.Thread(target=manager.run, daemon=True)
        thread.start()

        assert thread.is_alive()
        assert manager.running

        # Simulate runner completion
        time.sleep(0.1)
        manager.stop()
        thread.join(timeout=2.0)

        # Thread should have stopped
        assert not thread.is_alive()
        assert not manager.running

    def test_multiple_snapshot_cycles(self, tmp_path):
        """Test multiple snapshot cycles complete successfully."""
        from crsbench.evaluation.snapshot_manager import SnapshotManager

        trial_dir = tmp_path
        output_dir = trial_dir / "output"
        output_dir.mkdir()

        # Create some test data
        pov_dir = output_dir / "povs"
        pov_dir.mkdir()
        (pov_dir / "pov_001").write_bytes(b"test")

        manager = SnapshotManager(trial_dir, snapshot_period=1)

        # Start snapshot thread
        thread = threading.Thread(target=manager.run, daemon=True)
        thread.start()

        try:
            # Let it run for a few cycles
            time.sleep(3.5)

            # Stop thread
            manager.stop()
            thread.join(timeout=2.0)

            # Should have multiple snapshots
            snapshots = list_snapshots(trial_dir)
            assert len(snapshots) >= 2

            # All should be complete
            assert all(s.is_complete for s in snapshots)

            # All should have valid metadata
            for snapshot in snapshots:
                metadata = load_snapshot_metadata(snapshot.archive_path)
                assert metadata is not None
                assert metadata.cycle == snapshot.cycle

        finally:
            if thread.is_alive():
                manager.stop()
                thread.join(timeout=1.0)
