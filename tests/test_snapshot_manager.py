"""Unit tests for SnapshotManager."""

import json
import tarfile
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from crsbench.evaluation.snapshot import list_snapshots, load_snapshot_metadata
from crsbench.evaluation.snapshot_manager import SnapshotManager


class TestSnapshotManagerInit:
    """Test SnapshotManager initialization."""

    def test_init_valid(self, tmp_path):
        """Test initializing with valid parameters."""
        manager = SnapshotManager(tmp_path, snapshot_period=60)

        assert manager.trial_dir == tmp_path
        assert manager.snapshot_period == 60
        assert manager.cycle == 0
        assert manager.running is False

    def test_init_custom_start_time(self, tmp_path):
        """Test initializing with custom start time."""
        start_time = time.time() - 1000
        manager = SnapshotManager(
            tmp_path, snapshot_period=60, trial_start_time=start_time
        )

        assert manager.trial_start_time == start_time

    def test_init_invalid_period(self, tmp_path):
        """Test initializing with invalid snapshot period."""
        with pytest.raises(ValueError, match="snapshot_period must be > 0"):
            SnapshotManager(tmp_path, snapshot_period=0)

        with pytest.raises(ValueError, match="snapshot_period must be > 0"):
            SnapshotManager(tmp_path, snapshot_period=-1)

    def test_init_nonexistent_dir(self):
        """Test initializing with non-existent directory."""
        with pytest.raises(ValueError, match="trial_dir does not exist"):
            SnapshotManager(
                Path("/does/not/exist/at/all/really/truly"), snapshot_period=60
            )


class TestSnapshotCapture:
    """Test snapshot capture functionality."""

    def setup_trial_dir(self, tmp_path):
        """Helper to set up a trial directory with outputs."""
        # Create output directories
        output_dir = tmp_path / "output"
        output_dir.mkdir()

        pov_dir = output_dir / "povs"
        pov_dir.mkdir()

        patches_dir = output_dir / "patches"
        patches_dir.mkdir()

        corpus_dir = output_dir / "seeds"
        corpus_dir.mkdir()

        # Create some test files
        (pov_dir / "pov_001").write_bytes(b"test pov 1")
        (pov_dir / "pov_002").write_bytes(b"test pov 2")

        patch_dir_0 = patches_dir / "pov_0"
        patch_dir_0.mkdir()
        (patch_dir_0 / "patch.diff").write_text("patch content")

        (corpus_dir / "input-001").write_bytes(b"corpus data")

        # Create logs
        (tmp_path / "llm-usage.json").write_text('{"total_api_calls": 10}')
        (tmp_path / "crs-output.log").write_text("CRS log output")
        (tmp_path / "config.yaml").write_text("experiment: test")
        (tmp_path / "execution.json").write_text('{"trial_id": "test"}')

        return tmp_path

    def test_capture_snapshot_creates_archive(self, tmp_path):
        """Test that capture_snapshot creates tar.gz archive."""
        trial_dir = self.setup_trial_dir(tmp_path)
        manager = SnapshotManager(trial_dir, snapshot_period=60)

        manager.capture_snapshot()

        # Check archive was created
        archive_path = trial_dir / "snapshot-0001.tar.gz"
        assert archive_path.exists()

        # Check completion marker
        marker_path = trial_dir / "snapshot-0001.complete"
        assert marker_path.exists()

        # Check cycle incremented
        assert manager.cycle == 1

    def test_capture_snapshot_contains_metadata(self, tmp_path):
        """Test snapshot contains valid metadata."""
        trial_dir = self.setup_trial_dir(tmp_path)
        manager = SnapshotManager(trial_dir, snapshot_period=600)

        manager.capture_snapshot()

        archive_path = trial_dir / "snapshot-0001.tar.gz"
        metadata = load_snapshot_metadata(archive_path)

        assert metadata is not None
        assert metadata.cycle == 1
        assert metadata.snapshot_period == 600
        assert metadata.elapsed_time >= 0

    def test_capture_snapshot_contains_files(self, tmp_path):
        """Test snapshot contains expected files."""
        trial_dir = self.setup_trial_dir(tmp_path)
        manager = SnapshotManager(trial_dir, snapshot_period=60)

        manager.capture_snapshot()

        # Extract and verify contents
        archive_path = trial_dir / "snapshot-0001.tar.gz"
        extract_dir = tmp_path / "extracted"
        extract_dir.mkdir()

        with tarfile.open(archive_path, "r:gz") as tar:
            tar.extractall(extract_dir, filter="data")

        # Check required files
        assert (extract_dir / "metadata.json").exists()
        assert (extract_dir / "config.yaml").exists()
        assert (extract_dir / "execution.json").exists()
        assert (extract_dir / "llm-usage.json").exists()
        assert (extract_dir / "crs-output.log").exists()

        # Check POVs
        assert (extract_dir / "povs" / "pov_001").exists()
        assert (extract_dir / "povs" / "pov_002").exists()

        # Check patches (organized by POV ID)
        assert (extract_dir / "patches" / "pov_0" / "patch.diff").exists()

        # Check corpus
        assert (extract_dir / "seeds" / "input-001").exists()

    def test_full_pov_capture(self, tmp_path):
        """Test all POVs are captured in each snapshot (full capture)."""
        trial_dir = self.setup_trial_dir(tmp_path)
        manager = SnapshotManager(trial_dir, snapshot_period=60)

        # First snapshot
        snapshot1 = manager.capture_snapshot()
        assert snapshot1.has_povs

        # Add new POV
        pov_dir = trial_dir / "output" / "povs"
        (pov_dir / "pov_003").write_bytes(b"test pov 3")

        # Second snapshot
        snapshot2 = manager.capture_snapshot()
        assert snapshot2.has_povs  # POVs captured

        # Verify second snapshot has ALL POVs (full capture)
        archive_path = trial_dir / "snapshot-0002.tar.gz"
        extract_dir = tmp_path / "extracted2"
        extract_dir.mkdir()

        with tarfile.open(archive_path, "r:gz") as tar:
            tar.extractall(extract_dir, filter="data")

        # Should have all 3 POVs
        pov_files = sorted([p.name for p in (extract_dir / "povs").iterdir()])
        assert len(pov_files) == 3
        assert pov_files == ["pov_001", "pov_002", "pov_003"]

    def test_full_patch_capture(self, tmp_path):
        """Test all patches are captured in each snapshot (full capture)."""
        trial_dir = self.setup_trial_dir(tmp_path)
        manager = SnapshotManager(trial_dir, snapshot_period=60)

        # First snapshot
        snapshot1 = manager.capture_snapshot()
        assert snapshot1.has_patches

        # Add new patch
        patches_dir = trial_dir / "output" / "patches"
        patch_dir_1 = patches_dir / "pov_1"
        patch_dir_1.mkdir()
        (patch_dir_1 / "patch.diff").write_text("new patch")

        # Second snapshot
        snapshot2 = manager.capture_snapshot()
        assert snapshot2.has_patches  # Patches captured

        # Verify second snapshot has ALL patches (full capture)
        archive_path = trial_dir / "snapshot-0002.tar.gz"
        extract_dir = tmp_path / "extracted2"
        extract_dir.mkdir()

        with tarfile.open(archive_path, "r:gz") as tar:
            tar.extractall(extract_dir, filter="data")

        # Should have both patches
        assert (extract_dir / "patches" / "pov_1" / "patch.diff").exists()
        assert (extract_dir / "patches" / "pov_0" / "patch.diff").exists()

    def test_capture_without_optional_files(self, tmp_path):
        """Test snapshot works when optional files are missing."""
        # Create minimal trial dir
        output_dir = tmp_path / "output"
        output_dir.mkdir()

        manager = SnapshotManager(tmp_path, snapshot_period=60)
        manager.capture_snapshot()

        # Should create archive without errors
        archive_path = tmp_path / "snapshot-0001.tar.gz"
        assert archive_path.exists()

    def test_multiple_snapshots(self, tmp_path):
        """Test creating multiple snapshots."""
        trial_dir = self.setup_trial_dir(tmp_path)
        manager = SnapshotManager(trial_dir, snapshot_period=60)

        # Create 3 snapshots
        for i in range(3):
            manager.capture_snapshot()

        assert manager.cycle == 3

        # Verify all snapshots exist
        snapshots = list_snapshots(trial_dir)
        assert len(snapshots) == 3
        assert all(s.is_complete for s in snapshots)

    def test_refresh_final_symlink_after_manual_capture(self, tmp_path):
        """Manual capture path should refresh snapshot-final symlink."""
        trial_dir = self.setup_trial_dir(tmp_path)
        manager = SnapshotManager(trial_dir, snapshot_period=60)

        manager.capture_snapshot()
        manager.refresh_final_symlink()

        final_link = trial_dir / "snapshot-final.tar.gz"
        assert final_link.is_symlink()
        assert final_link.readlink().name == "snapshot-0001.tar.gz"

    def test_refresh_final_symlink_waits_for_capture_lock(self, tmp_path):
        """refresh_final_symlink should serialize with in-flight capture."""
        trial_dir = self.setup_trial_dir(tmp_path)
        manager = SnapshotManager(trial_dir, snapshot_period=60)
        manager.capture_snapshot()

        done = threading.Event()

        manager._capture_lock.acquire()
        try:
            t = threading.Thread(
                target=lambda: (
                    manager.refresh_final_symlink(),
                    done.set(),
                ),
                daemon=True,
            )
            t.start()
            time.sleep(0.05)
            assert not done.is_set()
        finally:
            manager._capture_lock.release()

        t.join(timeout=1.0)
        assert done.is_set()


class TestSnapshotThread:
    """Test snapshot thread lifecycle."""

    def test_thread_lifecycle(self, tmp_path):
        """Test starting and stopping snapshot thread."""
        manager = SnapshotManager(tmp_path, snapshot_period=60)

        # Start thread
        thread = threading.Thread(target=manager.run, daemon=True)
        thread.start()

        assert thread.is_alive()
        assert manager.running is True

        # Let it run briefly
        time.sleep(0.1)

        # Stop thread
        manager.stop()
        thread.join(timeout=2.0)

        assert not thread.is_alive()
        assert manager.running is False

    def test_thread_captures_snapshots(self, tmp_path):
        """Test thread actually captures snapshots."""
        # Create output directory
        output_dir = tmp_path / "output"
        output_dir.mkdir()

        # Use very short period for testing
        manager = SnapshotManager(tmp_path, snapshot_period=1)

        thread = threading.Thread(target=manager.run, daemon=True)
        thread.start()

        try:
            # Wait for at least one snapshot
            time.sleep(2.5)

            # Stop thread
            manager.stop()
            thread.join(timeout=2.0)

            # Should have captured at least 1-2 snapshots
            snapshots = list_snapshots(tmp_path)
            assert len(snapshots) >= 1

        finally:
            if thread.is_alive():
                manager.stop()
                thread.join(timeout=1.0)

    def test_quick_shutdown(self, tmp_path):
        """Test thread responds quickly to shutdown signal."""
        manager = SnapshotManager(tmp_path, snapshot_period=3600)  # 1 hour

        thread = threading.Thread(target=manager.run, daemon=True)
        thread.start()

        # Wait a bit
        time.sleep(0.1)

        # Stop and time how long it takes
        start = time.time()
        manager.stop()
        thread.join(timeout=2.0)
        elapsed = time.time() - start

        # Should stop quickly (not wait for full period)
        assert elapsed < 2.0
        assert not thread.is_alive()

    def test_error_handling(self, tmp_path):
        """Test snapshot errors don't crash thread."""
        manager = SnapshotManager(tmp_path, snapshot_period=1)

        # Delete trial_dir to cause errors
        import shutil

        shutil.rmtree(tmp_path)

        thread = threading.Thread(target=manager.run, daemon=True)
        thread.start()

        try:
            # Let it try to capture (will fail)
            time.sleep(2.5)

            # Thread should still be running despite errors
            assert thread.is_alive()

            # Stop thread
            manager.stop()
            thread.join(timeout=2.0)

            assert not thread.is_alive()

        finally:
            if thread.is_alive():
                manager.stop()
                thread.join(timeout=1.0)


class TestEdgeCases:
    """Test edge cases and error conditions."""

    def test_concurrent_file_access(self, tmp_path):
        """Test snapshot handles concurrent file writes."""
        # This tests the scenario where CRS is writing while snapshot reads
        trial_dir = tmp_path
        output_dir = trial_dir / "output"
        output_dir.mkdir()
        pov_dir = output_dir / "povs"
        pov_dir.mkdir()

        manager = SnapshotManager(trial_dir, snapshot_period=60)

        # Create initial POV
        (pov_dir / "pov_001").write_bytes(b"data")

        # Capture snapshot
        manager.capture_snapshot()

        # Should succeed without errors
        assert (trial_dir / "snapshot-0001.tar.gz").exists()

    def test_cleanup_temp_directory(self, tmp_path):
        """Test temp directories are cleaned up."""
        trial_dir = tmp_path
        output_dir = trial_dir / "output"
        output_dir.mkdir()

        manager = SnapshotManager(trial_dir, snapshot_period=60)
        manager.capture_snapshot()

        # No .snapshot-NNNN directories should remain
        temp_dirs = list(trial_dir.glob(".snapshot-*"))
        assert len(temp_dirs) == 0

    def test_snapshot_with_empty_directories(self, tmp_path):
        """Test snapshot with empty output directories."""
        trial_dir = tmp_path
        output_dir = trial_dir / "output"
        output_dir.mkdir()
        (output_dir / "povs").mkdir()
        (output_dir / "patches").mkdir()
        (output_dir / "seeds").mkdir()

        manager = SnapshotManager(trial_dir, snapshot_period=60)
        manager.capture_snapshot()

        # Should create snapshot without errors
        assert (trial_dir / "snapshot-0001.tar.gz").exists()


class TestLLMTrackingIntegration:
    """Test LLM tracking integration with SnapshotManager."""

    def test_init_with_llm_tracking(self, tmp_path):
        """Test SnapshotManager initialization with LLM tracking parameters."""
        mock_tracker = MagicMock()

        manager = SnapshotManager(
            trial_dir=tmp_path,
            snapshot_period=60,
            llm_tracker=mock_tracker,
            llm_api_key="test-api-key",
            llm_trial_id="test-trial-id",
        )

        assert manager.llm_tracker is mock_tracker
        assert manager.llm_api_key == "test-api-key"
        assert manager.llm_trial_id == "test-trial-id"

    def test_init_without_llm_tracking(self, tmp_path):
        """Test SnapshotManager initialization without LLM tracking."""
        manager = SnapshotManager(tmp_path, snapshot_period=60)

        assert manager.llm_tracker is None
        assert manager.llm_api_key is None
        assert manager.llm_trial_id is None

    def test_capture_llm_usage_from_api(self, tmp_path):
        """Test _capture_llm_usage queries LiteLLM API when tracker is available."""
        # Create output dir
        output_dir = tmp_path / "output"
        output_dir.mkdir()

        # Create mock tracker
        mock_tracker = MagicMock()

        manager = SnapshotManager(
            trial_dir=tmp_path,
            snapshot_period=60,
            llm_tracker=mock_tracker,
            llm_api_key="test-api-key",
            llm_trial_id="test-trial-id",
        )

        # Test _capture_llm_usage
        temp_dir = tmp_path / "temp_snapshot"
        temp_dir.mkdir()

        result = manager._capture_llm_usage(temp_dir)

        # Should have called write_llm_usage_file
        assert result is True
        mock_tracker.write_llm_usage_file.assert_called_once_with(
            api_key="test-api-key",
            trial_id="test-trial-id",
            output_path=temp_dir / "llm-usage.json",
        )

    def test_capture_llm_usage_fallback_to_file(self, tmp_path):
        """Test _capture_llm_usage falls back to file when API fails."""
        # Create output dir
        output_dir = tmp_path / "output"
        output_dir.mkdir()

        # Create mock tracker that raises an exception
        mock_tracker = MagicMock()
        mock_tracker.write_llm_usage_file.side_effect = Exception("API error")

        # Create existing llm-usage.json file for fallback
        llm_usage_data = {"total_api_calls": 10, "total_tokens": 1000}
        (tmp_path / "llm-usage.json").write_text(json.dumps(llm_usage_data))

        manager = SnapshotManager(
            trial_dir=tmp_path,
            snapshot_period=60,
            llm_tracker=mock_tracker,
            llm_api_key="test-api-key",
            llm_trial_id="test-trial-id",
        )

        # Test _capture_llm_usage
        temp_dir = tmp_path / "temp_snapshot"
        temp_dir.mkdir()

        result = manager._capture_llm_usage(temp_dir)

        # Should succeed via file-based fallback
        assert result is True
        assert (temp_dir / "llm-usage.json").exists()

        # Verify content was copied from fallback file
        captured_data = json.loads((temp_dir / "llm-usage.json").read_text())
        assert captured_data["total_api_calls"] == 10

    def test_capture_llm_usage_no_tracker_uses_file(self, tmp_path):
        """Test _capture_llm_usage uses file when no tracker is configured."""
        # Create existing llm-usage.json file
        llm_usage_data = {"total_api_calls": 5}
        (tmp_path / "llm-usage.json").write_text(json.dumps(llm_usage_data))

        manager = SnapshotManager(tmp_path, snapshot_period=60)

        temp_dir = tmp_path / "temp_snapshot"
        temp_dir.mkdir()

        result = manager._capture_llm_usage(temp_dir)

        # Should succeed via file copy
        assert result is True
        assert (temp_dir / "llm-usage.json").exists()

    def test_capture_llm_usage_returns_false_when_nothing_available(self, tmp_path):
        """Test _capture_llm_usage returns False when no tracker and no file."""
        manager = SnapshotManager(tmp_path, snapshot_period=60)

        temp_dir = tmp_path / "temp_snapshot"
        temp_dir.mkdir()

        result = manager._capture_llm_usage(temp_dir)

        # Should return False - no data available
        assert result is False
        assert not (temp_dir / "llm-usage.json").exists()

    def test_snapshot_capture_with_llm_tracking(self, tmp_path):
        """Test full snapshot capture includes LLM usage from API."""
        # Create output dir
        output_dir = tmp_path / "output"
        output_dir.mkdir()

        # Create mock tracker
        mock_tracker = MagicMock()

        manager = SnapshotManager(
            trial_dir=tmp_path,
            snapshot_period=60,
            llm_tracker=mock_tracker,
            llm_api_key="test-api-key",
            llm_trial_id="test-trial-id",
        )

        snapshot = manager.capture_snapshot()

        # Should have captured LLM usage
        assert snapshot.has_llm_usage is True
        mock_tracker.write_llm_usage_file.assert_called_once()


class TestBudgetPolicy:
    """Test budget_policy enforcement in SnapshotManager."""

    def _make_tracker(self, spend: float, max_budget: float) -> MagicMock:
        tracker = MagicMock()
        tracker.get_key_info.return_value = {
            "info": {"spend": spend, "max_budget": max_budget}
        }
        return tracker

    def test_init_rejects_invalid_policy(self, tmp_path):
        with pytest.raises(ValueError, match="budget_policy must be"):
            SnapshotManager(tmp_path, snapshot_period=60, budget_policy="abort")

    def test_terminate_sets_stop_event_on_overshoot(self, tmp_path):
        """Terminate policy signals stop_event and writes budget-exceeded.json."""
        tracker = self._make_tracker(spend=12.5, max_budget=10.0)
        stop_event = threading.Event()

        manager = SnapshotManager(
            trial_dir=tmp_path,
            snapshot_period=60,
            llm_tracker=tracker,
            llm_api_key="sk-test",
            llm_trial_id="trial-1",
            budget_policy="terminate",
            budget_stop_event=stop_event,
        )

        manager._check_budget(elapsed_time=42.0)

        assert stop_event.is_set() is True
        assert manager.budget_exceeded is True
        record = json.loads((tmp_path / "budget-exceeded.json").read_text())
        assert record["policy"] == "terminate"
        assert record["spend_usd"] == 12.5
        assert record["max_budget_usd"] == 10.0

    def test_continue_does_not_set_stop_event(self, tmp_path):
        """Continue policy logs overshoot but leaves stop_event untouched."""
        tracker = self._make_tracker(spend=12.5, max_budget=10.0)
        stop_event = threading.Event()

        manager = SnapshotManager(
            trial_dir=tmp_path,
            snapshot_period=60,
            llm_tracker=tracker,
            llm_api_key="sk-test",
            llm_trial_id="trial-1",
            budget_policy="continue",
            budget_stop_event=stop_event,
        )

        manager._check_budget(elapsed_time=42.0)

        assert stop_event.is_set() is False
        assert manager.budget_exceeded is True
        record = json.loads((tmp_path / "budget-exceeded.json").read_text())
        assert record["policy"] == "continue"

    def test_under_budget_no_action(self, tmp_path):
        """Below-cap spend must not record or signal termination."""
        tracker = self._make_tracker(spend=3.0, max_budget=10.0)
        stop_event = threading.Event()

        manager = SnapshotManager(
            trial_dir=tmp_path,
            snapshot_period=60,
            llm_tracker=tracker,
            llm_api_key="sk-test",
            llm_trial_id="trial-1",
            budget_policy="terminate",
            budget_stop_event=stop_event,
        )

        manager._check_budget(elapsed_time=10.0)

        assert stop_event.is_set() is False
        assert manager.budget_exceeded is False
        assert not (tmp_path / "budget-exceeded.json").exists()

    def test_idempotent_on_repeat_overshoot(self, tmp_path):
        """Subsequent overshoots after the first are no-ops."""
        tracker = self._make_tracker(spend=12.5, max_budget=10.0)
        stop_event = threading.Event()

        manager = SnapshotManager(
            trial_dir=tmp_path,
            snapshot_period=60,
            llm_tracker=tracker,
            llm_api_key="sk-test",
            llm_trial_id="trial-1",
            budget_policy="terminate",
            budget_stop_event=stop_event,
        )

        manager._check_budget(elapsed_time=10.0)
        manager._check_budget(elapsed_time=20.0)
        manager._check_budget(elapsed_time=30.0)

        # get_key_info is gated by budget_exceeded — only the first call queries.
        assert tracker.get_key_info.call_count == 1

    def test_get_key_info_failure_ignored(self, tmp_path):
        """Tracker errors must not propagate or mark budget exceeded."""
        tracker = MagicMock()
        tracker.get_key_info.side_effect = RuntimeError("boom")
        stop_event = threading.Event()

        manager = SnapshotManager(
            trial_dir=tmp_path,
            snapshot_period=60,
            llm_tracker=tracker,
            llm_api_key="sk-test",
            llm_trial_id="trial-1",
            budget_policy="terminate",
            budget_stop_event=stop_event,
        )

        manager._check_budget(elapsed_time=10.0)

        assert stop_event.is_set() is False
        assert manager.budget_exceeded is False

    def test_no_tracker_short_circuits(self, tmp_path):
        """Without a tracker/key configured, budget check is a no-op."""
        stop_event = threading.Event()

        manager = SnapshotManager(
            trial_dir=tmp_path,
            snapshot_period=60,
            budget_policy="terminate",
            budget_stop_event=stop_event,
        )

        manager._check_budget(elapsed_time=10.0)

        assert stop_event.is_set() is False
        assert manager.budget_exceeded is False

    def test_zero_max_budget_treated_as_unlimited(self, tmp_path):
        """A zero/None cap must not trigger the overshoot path."""
        tracker = self._make_tracker(spend=100.0, max_budget=0.0)
        stop_event = threading.Event()

        manager = SnapshotManager(
            trial_dir=tmp_path,
            snapshot_period=60,
            llm_tracker=tracker,
            llm_api_key="sk-test",
            llm_trial_id="trial-1",
            budget_policy="terminate",
            budget_stop_event=stop_event,
        )

        manager._check_budget(elapsed_time=10.0)

        assert stop_event.is_set() is False
        assert manager.budget_exceeded is False
