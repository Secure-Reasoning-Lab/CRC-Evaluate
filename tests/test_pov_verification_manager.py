"""Unit tests for POVVerificationManager.

Tests for crsbench/evaluation/verification/pov/manager.py.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

from crsbench.evaluation.verification.models import PovVerificationStatus
from crsbench.evaluation.verification.pov.config import POVVerificationConfig
from crsbench.evaluation.verification.pov.store import POVStore


class TestPOVVerificationManagerInit:
    """Tests for POVVerificationManager initialization."""

    def test_init_creates_store(self, tmp_path: Path) -> None:
        """Test that manager creates store on init."""
        from crsbench.evaluation.verification.pov.manager import (
            POVVerificationManager,
        )

        trial_dir = tmp_path / "trial-1"
        trial_dir.mkdir()
        pov_output_dir = trial_dir / "pov_output"
        pov_output_dir.mkdir()

        config = POVVerificationConfig()
        manager = POVVerificationManager(
            trial_dir=trial_dir,
            pov_output_dir=pov_output_dir,
            config=config,
            harness_name="fuzz_parser",
            benchmark_id="test-benchmark",
            expected_cpv_ids=["cpv_0", "cpv_1", "cpv_2"],
        )

        assert manager.config == config
        assert manager.harness_name == "fuzz_parser"
        assert manager.benchmark_id == "test-benchmark"
        assert isinstance(manager.store, POVStore)
        assert manager.total_expected_cpvs == 3

    def test_init_with_existing_store(self, tmp_path: Path) -> None:
        """Test init with pre-existing store."""
        from crsbench.evaluation.verification.pov.manager import (
            POVVerificationManager,
        )

        trial_dir = tmp_path / "trial-1"
        trial_dir.mkdir()
        pov_output_dir = trial_dir / "pov_output"
        pov_output_dir.mkdir()

        store = POVStore(trial_dir / "povs")
        config = POVVerificationConfig()
        manager = POVVerificationManager(
            trial_dir=trial_dir,
            pov_output_dir=pov_output_dir,
            config=config,
            harness_name="fuzz_parser",
            benchmark_id="test-benchmark",
            expected_cpv_ids=["cpv_0", "cpv_1", "cpv_2"],
            store=store,
        )

        assert manager.store is store


class TestDiscoverNewPovs:
    """Tests for POVVerificationManager._discover_new_povs (T023)."""

    def test_discover_new_povs_empty_directory(self, tmp_path: Path) -> None:
        """Test discovery in empty directory returns empty list."""
        from crsbench.evaluation.verification.pov.manager import (
            POVVerificationManager,
        )

        trial_dir = tmp_path / "trial-1"
        trial_dir.mkdir()
        pov_output_dir = trial_dir / "pov_output"
        pov_output_dir.mkdir()

        config = POVVerificationConfig()
        manager = POVVerificationManager(
            trial_dir=trial_dir,
            pov_output_dir=pov_output_dir,
            config=config,
            harness_name="fuzz_parser",
            benchmark_id="test-benchmark",
            expected_cpv_ids=["cpv_0", "cpv_1", "cpv_2"],
        )

        new_povs = manager._discover_new_povs()
        assert new_povs == []

    def test_discover_new_povs_finds_new_files(self, tmp_path: Path) -> None:
        """Test discovery finds new POV files."""
        from crsbench.evaluation.verification.pov.manager import (
            POVVerificationManager,
        )

        trial_dir = tmp_path / "trial-1"
        trial_dir.mkdir()
        pov_output_dir = trial_dir / "pov_output"
        pov_output_dir.mkdir()

        # Create POV files
        (pov_output_dir / "pov_1.blob").write_bytes(b"pov1")
        (pov_output_dir / "pov_2.blob").write_bytes(b"pov2")

        config = POVVerificationConfig()
        manager = POVVerificationManager(
            trial_dir=trial_dir,
            pov_output_dir=pov_output_dir,
            config=config,
            harness_name="fuzz_parser",
            benchmark_id="test-benchmark",
            expected_cpv_ids=["cpv_0", "cpv_1", "cpv_2"],
        )

        new_povs = manager._discover_new_povs()
        assert len(new_povs) == 2

    def test_discover_new_povs_skips_already_processed(self, tmp_path: Path) -> None:
        """Test discovery skips already processed POVs."""
        from crsbench.evaluation.verification.pov.manager import (
            POVVerificationManager,
        )

        trial_dir = tmp_path / "trial-1"
        trial_dir.mkdir()
        pov_output_dir = trial_dir / "pov_output"
        pov_output_dir.mkdir()

        # Create POV file
        pov_file = pov_output_dir / "pov_1.blob"
        pov_file.write_bytes(b"pov1")

        config = POVVerificationConfig()
        manager = POVVerificationManager(
            trial_dir=trial_dir,
            pov_output_dir=pov_output_dir,
            config=config,
            harness_name="fuzz_parser",
            benchmark_id="test-benchmark",
            expected_cpv_ids=["cpv_0", "cpv_1", "cpv_2"],
        )

        # First discovery
        new_povs = manager._discover_new_povs()
        assert len(new_povs) == 1

        # Mark as processed via store
        manager.store.add_pov(pov_file, PovVerificationStatus.CPV, ["cpv_0"])

        # Second discovery should skip
        new_povs = manager._discover_new_povs()
        assert len(new_povs) == 0


class TestVerifyPovMatchesCpv:
    """Tests for POV verification against CPVs (T024)."""

    def test_verify_pov_matches_cpv(self, tmp_path: Path) -> None:
        """Test that verification correctly identifies CPV match."""
        from crsbench.evaluation.verification.models import (
            PovVerificationResult,
            PovVerificationStatus,
        )
        from crsbench.evaluation.verification.pov.manager import (
            POVVerificationManager,
        )

        trial_dir = tmp_path / "trial-1"
        trial_dir.mkdir()
        pov_output_dir = trial_dir / "pov_output"
        pov_output_dir.mkdir()

        pov_file = pov_output_dir / "pov_1.blob"
        pov_file.write_bytes(b"pov_that_triggers_cpv_0")

        config = POVVerificationConfig()
        manager = POVVerificationManager(
            trial_dir=trial_dir,
            pov_output_dir=pov_output_dir,
            config=config,
            harness_name="fuzz_parser",
            benchmark_id="test-benchmark",
            expected_cpv_ids=["cpv_0", "cpv_1", "cpv_2"],
        )

        # Mock the verification engine and adapter
        mock_result = PovVerificationResult(
            status=PovVerificationStatus.CPV,
            benchmark="test-benchmark",
            pov_id="pov_1",
            cpv_matched=["cpv_0"],
            details="Crash matches cpv_0",
        )
        manager._engine = MagicMock()
        manager._engine.verify_pov.return_value = mock_result
        manager._adapter = MagicMock()

        # Verify POV
        result = manager._verify_pov(pov_file)

        assert result is not None
        assert result.status == PovVerificationStatus.CPV
        assert "cpv_0" in result.cpv_matched

    def test_verify_pov_updates_state_on_cpv_match(self, tmp_path: Path) -> None:
        """Test that state is updated when CPV is found."""
        from crsbench.evaluation.verification.models import (
            PovVerificationResult,
            PovVerificationStatus,
        )
        from crsbench.evaluation.verification.pov.manager import (
            POVVerificationManager,
        )

        trial_dir = tmp_path / "trial-1"
        trial_dir.mkdir()
        pov_output_dir = trial_dir / "pov_output"
        pov_output_dir.mkdir()

        pov_file = pov_output_dir / "pov_1.blob"
        pov_file.write_bytes(b"pov_that_triggers_cpv_0")

        config = POVVerificationConfig()
        manager = POVVerificationManager(
            trial_dir=trial_dir,
            pov_output_dir=pov_output_dir,
            config=config,
            harness_name="fuzz_parser",
            benchmark_id="test-benchmark",
            expected_cpv_ids=["cpv_0", "cpv_1", "cpv_2"],
        )

        # Mock the verification engine and adapter
        mock_result = PovVerificationResult(
            status=PovVerificationStatus.CPV,
            benchmark="test-benchmark",
            pov_id="pov_1",
            cpv_matched=["cpv_0"],
            details="Crash matches cpv_0",
        )
        manager._engine = MagicMock()
        manager._engine.verify_pov.return_value = mock_result
        manager._adapter = MagicMock()

        # Process the POV
        manager._verify_pov(pov_file)
        manager._update_state(pov_file, mock_result)

        # found_cpvs is derived from store
        assert "cpv_0" in manager.found_cpvs

    def test_cpv_pov_stored_to_cpvs_dir(self, tmp_path: Path) -> None:
        """Test that verified CPV POV files are stored to cpvs/{cpv_id}/blobs/."""
        from crsbench.evaluation.verification.models import (
            PovVerificationResult,
            PovVerificationStatus,
        )
        from crsbench.evaluation.verification.pov.manager import (
            POVVerificationManager,
        )

        trial_dir = tmp_path / "trial-1"
        trial_dir.mkdir()
        pov_output_dir = trial_dir / "pov_output"
        pov_output_dir.mkdir()

        # Create a POV file with specific content
        pov_content = b"pov_content_for_cpv_match"
        pov_file = pov_output_dir / "pov_cpv.blob"
        pov_file.write_bytes(pov_content)

        config = POVVerificationConfig()
        manager = POVVerificationManager(
            trial_dir=trial_dir,
            pov_output_dir=pov_output_dir,
            config=config,
            harness_name="fuzz_parser",
            benchmark_id="test-benchmark",
            expected_cpv_ids=["cpv_0"],
        )

        # Mock verification result with CPV match
        mock_result = PovVerificationResult(
            status=PovVerificationStatus.CPV,
            benchmark="test-benchmark",
            pov_id="pov_cpv",
            cpv_matched=["cpv_0"],
        )
        manager._engine = MagicMock()
        manager._adapter = MagicMock()

        # Update state with the result
        manager._update_state(pov_file, mock_result)

        # Verify POV file was stored to cpvs/cpv_0/blobs/
        cpv_blobs_dir = trial_dir / "povs" / "cpvs" / "cpv_0" / "blobs"
        stored_files = list(cpv_blobs_dir.glob("*.blob"))
        assert len(stored_files) == 1

        # Verify content matches
        stored_content = stored_files[0].read_bytes()
        assert stored_content == pov_content


class TestVerifyPovUnintendedCrash:
    """Tests for unintended crash detection (T025)."""

    def test_verify_pov_unintended_crash(self, tmp_path: Path) -> None:
        """Test that verification correctly identifies unintended crash."""
        from crsbench.evaluation.verification.models import (
            PovVerificationResult,
            PovVerificationStatus,
        )
        from crsbench.evaluation.verification.pov.manager import (
            POVVerificationManager,
        )

        trial_dir = tmp_path / "trial-1"
        trial_dir.mkdir()
        pov_output_dir = trial_dir / "pov_output"
        pov_output_dir.mkdir()

        pov_file = pov_output_dir / "pov_unintended.blob"
        pov_file.write_bytes(b"pov_that_triggers_unknown_crash")

        config = POVVerificationConfig()
        manager = POVVerificationManager(
            trial_dir=trial_dir,
            pov_output_dir=pov_output_dir,
            config=config,
            harness_name="fuzz_parser",
            benchmark_id="test-benchmark",
            expected_cpv_ids=["cpv_0", "cpv_1", "cpv_2"],
        )

        # Mock the verification engine returning unintended crash
        mock_result = PovVerificationResult(
            status=PovVerificationStatus.UNINTENDED_CRASH,
            benchmark="test-benchmark",
            pov_id="pov_unintended",
            cpv_matched=[],
            details="Crash does not match any known CPV",
        )
        manager._engine = MagicMock()
        manager._engine.verify_pov.return_value = mock_result
        manager._adapter = MagicMock()

        # Verify POV
        result = manager._verify_pov(pov_file)

        assert result is not None
        assert result.status == PovVerificationStatus.UNINTENDED_CRASH

    def test_unintended_crash_updates_state_counter(self, tmp_path: Path) -> None:
        """Test that unintended crash increments state counter."""
        from crsbench.evaluation.verification.models import (
            PovVerificationResult,
            PovVerificationStatus,
        )
        from crsbench.evaluation.verification.pov.manager import (
            POVVerificationManager,
        )

        trial_dir = tmp_path / "trial-1"
        trial_dir.mkdir()
        pov_output_dir = trial_dir / "pov_output"
        pov_output_dir.mkdir()

        pov_file = pov_output_dir / "pov_unintended.blob"
        pov_file.write_bytes(b"pov_that_triggers_unknown_crash")

        config = POVVerificationConfig()
        manager = POVVerificationManager(
            trial_dir=trial_dir,
            pov_output_dir=pov_output_dir,
            config=config,
            harness_name="fuzz_parser",
            benchmark_id="test-benchmark",
            expected_cpv_ids=["cpv_0", "cpv_1", "cpv_2"],
        )

        mock_result = PovVerificationResult(
            status=PovVerificationStatus.UNINTENDED_CRASH,
            benchmark="test-benchmark",
            pov_id="pov_unintended",
            cpv_matched=[],
            details="Crash does not match any known CPV",
        )
        manager._engine = MagicMock()
        manager._engine.verify_pov.return_value = mock_result
        manager._adapter = MagicMock()

        # Process the POV
        manager._verify_pov(pov_file)
        manager._update_state(pov_file, mock_result)

        assert manager._unintended_crashes_count == 1


class TestEarlyStopCondition:
    """Tests for early stop condition check (T035-T036)."""

    def test_should_terminate_all_cpvs_found(self, tmp_path: Path) -> None:
        """Test that _should_terminate returns True when all CPVs found."""
        from crsbench.evaluation.verification.pov.manager import (
            POVVerificationManager,
        )

        trial_dir = tmp_path / "trial-1"
        trial_dir.mkdir()
        pov_output_dir = trial_dir / "pov_output"
        pov_output_dir.mkdir()

        config = POVVerificationConfig(early_stop_enabled=True)
        manager = POVVerificationManager(
            trial_dir=trial_dir,
            pov_output_dir=pov_output_dir,
            config=config,
            harness_name="fuzz_parser",
            benchmark_id="test-benchmark",
            expected_cpv_ids=["cpv_0", "cpv_1"],
        )

        # Initially should not terminate
        assert manager._should_terminate() is False

        # Add found CPVs via store (simulating verification)
        pov1 = pov_output_dir / "pov1.blob"
        pov2 = pov_output_dir / "pov2.blob"
        pov1.write_bytes(b"pov1")
        pov2.write_bytes(b"pov2")
        manager.store.add_pov(pov1, PovVerificationStatus.CPV, ["cpv_0"])
        manager.store.add_pov(pov2, PovVerificationStatus.CPV, ["cpv_1"])

        # Now should terminate
        assert manager._should_terminate() is True

    def test_should_not_terminate_when_disabled(self, tmp_path: Path) -> None:
        """Test that _should_terminate returns False when early stop disabled."""
        from crsbench.evaluation.verification.pov.manager import (
            POVVerificationManager,
        )

        trial_dir = tmp_path / "trial-1"
        trial_dir.mkdir()
        pov_output_dir = trial_dir / "pov_output"
        pov_output_dir.mkdir()

        config = POVVerificationConfig(early_stop_enabled=False)
        manager = POVVerificationManager(
            trial_dir=trial_dir,
            pov_output_dir=pov_output_dir,
            config=config,
            harness_name="fuzz_parser",
            benchmark_id="test-benchmark",
            expected_cpv_ids=["cpv_0", "cpv_1"],
        )

        # Add found CPVs via store
        pov1 = pov_output_dir / "pov1.blob"
        pov2 = pov_output_dir / "pov2.blob"
        pov1.write_bytes(b"pov1")
        pov2.write_bytes(b"pov2")
        manager.store.add_pov(pov1, PovVerificationStatus.CPV, ["cpv_0"])
        manager.store.add_pov(pov2, PovVerificationStatus.CPV, ["cpv_1"])

        # Should not terminate because early stop is disabled
        assert manager._should_terminate() is False


class TestDuplicateDetection:
    """Tests for duplicate POV detection (T044-T045)."""

    def test_is_already_tested(self, tmp_path: Path) -> None:
        """Test that duplicate POVs are detected via store."""
        from crsbench.evaluation.verification.pov.manager import (
            POVVerificationManager,
        )

        trial_dir = tmp_path / "trial-1"
        trial_dir.mkdir()
        pov_output_dir = trial_dir / "pov_output"
        pov_output_dir.mkdir()

        pov_file = pov_output_dir / "pov_1.blob"
        pov_file.write_bytes(b"pov_content")

        config = POVVerificationConfig()
        manager = POVVerificationManager(
            trial_dir=trial_dir,
            pov_output_dir=pov_output_dir,
            config=config,
            harness_name="fuzz_parser",
            benchmark_id="test-benchmark",
            expected_cpv_ids=["cpv_0", "cpv_1", "cpv_2"],
        )

        # Not tested yet
        assert manager.store.is_already_tested(pov_file) is False

        # Add to store
        manager.store.add_pov(pov_file, PovVerificationStatus.CPV, ["cpv_0"])

        # Now should be detected as tested
        assert manager.store.is_already_tested(pov_file) is True

    def test_skip_hashes_integration(self, tmp_path: Path) -> None:
        """Test that skip_hashes is passed to VerificationEngine."""
        from crsbench.evaluation.verification.pov.manager import (
            POVVerificationManager,
        )

        trial_dir = tmp_path / "trial-1"
        trial_dir.mkdir()
        pov_output_dir = trial_dir / "pov_output"
        pov_output_dir.mkdir()

        pov_file = pov_output_dir / "pov_1.blob"
        pov_file.write_bytes(b"pov_content")

        config = POVVerificationConfig()
        manager = POVVerificationManager(
            trial_dir=trial_dir,
            pov_output_dir=pov_output_dir,
            config=config,
            harness_name="fuzz_parser",
            benchmark_id="test-benchmark",
            expected_cpv_ids=["cpv_0", "cpv_1", "cpv_2"],
        )

        # Add POV to store to populate tested hashes
        manager.store.add_pov(pov_file, PovVerificationStatus.CPV, ["cpv_0"])

        # Get tested hashes
        tested_hashes = manager.store.get_tested_hashes()

        assert len(tested_hashes) == 1


class TestOnSnapshot:
    """Tests for on_snapshot callback method."""

    def test_on_snapshot_creates_snapshot(self, tmp_path: Path) -> None:
        """Test that on_snapshot creates a POVSnapshot."""
        from crsbench.evaluation.verification.pov.manager import (
            POVVerificationManager,
        )

        trial_dir = tmp_path / "trial-1"
        trial_dir.mkdir()
        pov_output_dir = trial_dir / "pov_output"
        pov_output_dir.mkdir()

        config = POVVerificationConfig()
        manager = POVVerificationManager(
            trial_dir=trial_dir,
            pov_output_dir=pov_output_dir,
            config=config,
            harness_name="fuzz_parser",
            benchmark_id="test-benchmark",
            expected_cpv_ids=["cpv_0", "cpv_1", "cpv_2"],
        )

        snapshot = manager.on_snapshot(cycle=1)

        assert snapshot.cycle == 1
        assert snapshot.harness_name == "fuzz_parser"
        assert snapshot.cpvs_found == []
        assert snapshot.early_stop_triggered is False

    def test_on_snapshot_with_found_cpvs(self, tmp_path: Path) -> None:
        """Test that on_snapshot reflects found CPVs."""
        from crsbench.evaluation.verification.pov.manager import (
            POVVerificationManager,
        )

        trial_dir = tmp_path / "trial-1"
        trial_dir.mkdir()
        pov_output_dir = trial_dir / "pov_output"
        pov_output_dir.mkdir()

        config = POVVerificationConfig()
        manager = POVVerificationManager(
            trial_dir=trial_dir,
            pov_output_dir=pov_output_dir,
            config=config,
            harness_name="fuzz_parser",
            benchmark_id="test-benchmark",
            expected_cpv_ids=["cpv_0", "cpv_1", "cpv_2"],
        )

        # Add some found CPVs via store
        pov1 = pov_output_dir / "pov1.blob"
        pov2 = pov_output_dir / "pov2.blob"
        pov1.write_bytes(b"pov1")
        pov2.write_bytes(b"pov2")
        manager.store.add_pov(pov1, PovVerificationStatus.CPV, ["cpv_0"])
        manager.store.add_pov(pov2, PovVerificationStatus.CPV, ["cpv_1"])

        snapshot = manager.on_snapshot(cycle=1)

        assert set(snapshot.cpvs_found) == {"cpv_0", "cpv_1"}


class TestGetState:
    """Tests for get_state thread-safe getter."""

    def test_get_state_returns_snapshot_data(self, tmp_path: Path) -> None:
        """Test get_state returns thread-safe snapshot."""
        from crsbench.evaluation.verification.pov.manager import (
            POVVerificationManager,
        )

        trial_dir = tmp_path / "trial-1"
        trial_dir.mkdir()
        pov_output_dir = trial_dir / "pov_output"
        pov_output_dir.mkdir()

        config = POVVerificationConfig()
        manager = POVVerificationManager(
            trial_dir=trial_dir,
            pov_output_dir=pov_output_dir,
            config=config,
            harness_name="fuzz_parser",
            benchmark_id="test-benchmark",
            expected_cpv_ids=["cpv_0", "cpv_1", "cpv_2"],
        )

        # Add found CPV via store
        pov1 = pov_output_dir / "pov1.blob"
        pov1.write_bytes(b"pov1")
        manager.store.add_pov(pov1, PovVerificationStatus.CPV, ["cpv_0"])

        state_data = manager.get_state()

        assert state_data["benchmark_id"] == "test-benchmark"
        assert "cpv_0" in state_data["found_cpvs"]


class TestGetReport:
    """Tests for get_report final report generation."""

    def test_get_report_generates_verification_report(self, tmp_path: Path) -> None:
        """Test get_report generates POVVerificationReport."""
        from crsbench.evaluation.verification.pov.manager import (
            POVVerificationManager,
        )
        from crsbench.evaluation.verification.pov.models import (
            POVVerificationReport,
        )

        trial_dir = tmp_path / "trial-1"
        trial_dir.mkdir()
        pov_output_dir = trial_dir / "pov_output"
        pov_output_dir.mkdir()

        config = POVVerificationConfig()
        manager = POVVerificationManager(
            trial_dir=trial_dir,
            pov_output_dir=pov_output_dir,
            config=config,
            harness_name="fuzz_parser",
            benchmark_id="test-benchmark",
            expected_cpv_ids=["cpv_0", "cpv_1", "cpv_2"],
        )

        # Add found CPVs via store
        pov1 = pov_output_dir / "pov1.blob"
        pov2 = pov_output_dir / "pov2.blob"
        pov1.write_bytes(b"pov1")
        pov2.write_bytes(b"pov2")
        manager.store.add_pov(pov1, PovVerificationStatus.CPV, ["cpv_0"])
        manager.store.add_pov(pov2, PovVerificationStatus.CPV, ["cpv_1"])

        # Add unintended crash
        pov3 = pov_output_dir / "pov3.blob"
        pov3.write_bytes(b"pov3")
        manager.store.add_pov(pov3, PovVerificationStatus.UNINTENDED_CRASH, [])
        manager._unintended_crashes_count = 1

        # Add duplicates count
        manager._duplicates_count = 2

        report = manager.get_report()

        assert isinstance(report, POVVerificationReport)
        assert report.benchmark_id == "test-benchmark"
        assert report.harness_name == "fuzz_parser"
        assert report.total_expected_cpvs == 3
        assert set(report.cpvs_found) == {"cpv_0", "cpv_1"}
        assert report.unintended_crashes == 1
        assert report.duplicates_skipped == 2


class TestAsyncMode:
    """Tests for POVVerificationManager async mode (VU-02/03/04)."""

    def _make_manager(self, tmp_path: Path, *, redis_host=None):
        from crsbench.evaluation.verification.pov.manager import (
            POVVerificationManager,
        )

        trial_dir = tmp_path / "trial-1"
        trial_dir.mkdir(exist_ok=True)
        pov_output_dir = trial_dir / "pov_output"
        pov_output_dir.mkdir(exist_ok=True)

        config = POVVerificationConfig()
        return POVVerificationManager(
            trial_dir=trial_dir,
            pov_output_dir=pov_output_dir,
            config=config,
            harness_name="fuzz_parser",
            benchmark_id="test-benchmark",
            expected_cpv_ids=["cpv_0", "cpv_1"],
            redis_host=redis_host,
            experiment_name="exp1",
            trial_id="trial-1",
        )

    def test_async_mode_enabled_with_redis_host(self, tmp_path: Path) -> None:
        """Manager in async mode when redis_host is provided."""
        manager = self._make_manager(tmp_path, redis_host="redis.local")
        assert manager._async_mode is True

    def test_async_mode_disabled_without_redis_host(self, tmp_path: Path) -> None:
        """Manager in inline mode when redis_host is None."""
        manager = self._make_manager(tmp_path, redis_host=None)
        assert manager._async_mode is False

    @patch("crsbench.distributed.verify_queue.initialize_verify_queue")
    def test_get_verify_queue_initializes_lazily(
        self, mock_init_queue, tmp_path: Path
    ) -> None:
        """Verify queue is initialized lazily on first access."""
        mock_queue = MagicMock()
        mock_init_queue.return_value = mock_queue

        manager = self._make_manager(tmp_path, redis_host="redis.local")

        # Not initialized yet
        assert manager._verify_queue is None

        # First access triggers init
        queue = manager._get_verify_queue()
        assert queue is mock_queue
        mock_init_queue.assert_called_once_with("redis.local", "exp1")

        # Second access returns cached
        queue2 = manager._get_verify_queue()
        assert queue2 is mock_queue
        # Still only called once
        mock_init_queue.assert_called_once()

    @patch("crsbench.distributed.verify_queue.enqueue_single_pov")
    @patch("crsbench.distributed.verify_queue.initialize_verify_queue")
    def test_enqueue_pov_calls_verify_queue(
        self, mock_init_queue, mock_enqueue, tmp_path: Path
    ) -> None:
        """_enqueue_pov reads POV data and enqueues via verify_queue."""
        mock_queue = MagicMock()
        mock_init_queue.return_value = mock_queue
        mock_enqueue.return_value = "job-123"

        manager = self._make_manager(tmp_path, redis_host="redis.local")

        pov_file = tmp_path / "trial-1" / "pov_output" / "test.blob"
        pov_file.write_bytes(b"pov_data_content")

        job_id = manager._enqueue_pov(pov_file, "abc123hash")

        assert job_id == "job-123"
        mock_enqueue.assert_called_once()
        call_kwargs = mock_enqueue.call_args
        # pov_id format: {filename}:{hash}
        assert call_kwargs[1]["pov_id"] == "test.blob:abc123hash"

    @patch("crsbench.distributed.verify_queue.poll_single_pov_verdicts")
    def test_poll_pending_verdicts_processes_results(
        self, mock_poll, tmp_path: Path
    ) -> None:
        """_poll_pending_verdicts processes completed results into store."""
        from crsbench.distributed.evaluator_jobs import PovVerdict, SinglePovResult

        verdict = PovVerdict(
            pov_id="test.blob:abc123hash",
            triggered_bug=True,
            status="cpv",
            cpv_matches=["cpv_0"],
        )
        result = SinglePovResult(
            trial_id="trial-1",
            benchmark="test-benchmark",
            harness="fuzz_parser",
            verdict=verdict,
            completed_at=1700000100.0,
        )

        mock_poll.return_value = ([result.to_dict()], [])

        manager = self._make_manager(tmp_path, redis_host="redis.local")
        manager._pending_job_ids = ["job-123"]

        manager._poll_pending_verdicts()

        # Pending should be cleared
        assert manager._pending_job_ids == []
        # CPV should be found
        assert "cpv_0" in manager.found_cpvs

    @patch("crsbench.distributed.verify_queue.enqueue_single_pov")
    @patch("crsbench.distributed.verify_queue.initialize_verify_queue")
    def test_enqueue_pov_records_hash_to_path(
        self, mock_init_queue, mock_enqueue, tmp_path: Path
    ) -> None:
        """_enqueue_pov records pov_hash → pov_path mapping."""
        mock_queue = MagicMock()
        mock_init_queue.return_value = mock_queue
        mock_enqueue.return_value = "job-1"

        manager = self._make_manager(tmp_path, redis_host="redis.local")
        pov_file = tmp_path / "trial-1" / "pov_output" / "pov.blob"
        pov_file.write_bytes(b"test_content")

        manager._enqueue_pov(pov_file, "hash123")

        assert manager._pov_hash_to_path["hash123"] == pov_file

    @patch("crsbench.distributed.verify_queue.poll_single_pov_verdicts")
    def test_poll_stores_blob_and_crash_logs_for_cpv(
        self, mock_poll, tmp_path: Path
    ) -> None:
        """_poll_pending_verdicts stores POV blob and crash logs for CPV matches."""
        from crsbench.distributed.evaluator_jobs import PovVerdict, SinglePovResult

        pov_hash = "abc123hash"
        crash_logs = {
            "base-asan": "ASAN: heap-buffer-overflow at ...",
            "patched-asan": "no crash",
        }

        verdict = PovVerdict(
            pov_id=f"test.blob:{pov_hash}",
            triggered_bug=True,
            status="cpv",
            cpv_matches=["cpv_0"],
            crash_logs=crash_logs,
        )
        result = SinglePovResult(
            trial_id="trial-1",
            benchmark="test-benchmark",
            harness="fuzz_parser",
            verdict=verdict,
            completed_at=1700000100.0,
        )
        mock_poll.return_value = ([result.to_dict()], [])

        manager = self._make_manager(tmp_path, redis_host="redis.local")
        manager._pending_job_ids = ["job-1"]

        # Create POV file and register hash→path
        pov_file = tmp_path / "trial-1" / "pov_output" / "test.blob"
        pov_file.write_bytes(b"pov_blob_data")
        manager._pov_hash_to_path[pov_hash] = pov_file

        manager._poll_pending_verdicts()

        # Verify blob was stored
        cpv_blobs_dir = tmp_path / "trial-1" / "povs" / "cpvs" / "cpv_0" / "blobs"
        stored_blobs = list(cpv_blobs_dir.glob("*.blob"))
        assert len(stored_blobs) == 1
        assert stored_blobs[0].read_bytes() == b"pov_blob_data"

        # Verify crash logs were stored
        cpv_logs_dir = tmp_path / "trial-1" / "povs" / "cpvs" / "cpv_0" / "crash_logs"
        stored_logs = sorted(cpv_logs_dir.glob("*.log"))
        assert len(stored_logs) == 2
        log_names = {log.name for log in stored_logs}
        assert f"{pov_hash}-base-asan.log" in log_names
        assert f"{pov_hash}-patched-asan.log" in log_names

    @patch("crsbench.distributed.verify_queue.poll_single_pov_verdicts")
    def test_poll_skips_blob_when_file_missing(self, mock_poll, tmp_path: Path) -> None:
        """Blob storage is skipped if POV file was deleted, but crash logs still stored."""
        from crsbench.distributed.evaluator_jobs import PovVerdict, SinglePovResult

        pov_hash = "deadbeef1234"
        verdict = PovVerdict(
            pov_id=f"test.blob:{pov_hash}",
            triggered_bug=True,
            status="cpv",
            cpv_matches=["cpv_0"],
            crash_logs={"base-asan": "ASAN crash log"},
        )
        result = SinglePovResult(
            trial_id="trial-1",
            benchmark="test-benchmark",
            harness="fuzz_parser",
            verdict=verdict,
            completed_at=1700000100.0,
        )
        mock_poll.return_value = ([result.to_dict()], [])

        manager = self._make_manager(tmp_path, redis_host="redis.local")
        manager._pending_job_ids = ["job-1"]
        # No hash→path mapping (simulates file deleted or never recorded)

        manager._poll_pending_verdicts()

        # No blob stored (no file to copy from)
        cpv_blobs_dir = tmp_path / "trial-1" / "povs" / "cpvs" / "cpv_0" / "blobs"
        assert (
            not cpv_blobs_dir.exists() or len(list(cpv_blobs_dir.glob("*.blob"))) == 0
        )

        # Crash logs should still be stored
        cpv_logs_dir = tmp_path / "trial-1" / "povs" / "cpvs" / "cpv_0" / "crash_logs"
        stored_logs = list(cpv_logs_dir.glob("*.log"))
        assert len(stored_logs) == 1

    @patch("crsbench.distributed.verify_queue.poll_single_pov_verdicts")
    def test_poll_no_blob_for_non_cpv_but_logs_stored(
        self, mock_poll, tmp_path: Path
    ) -> None:
        """Non-CPV verdicts do not store blobs but DO store crash logs."""
        from crsbench.distributed.evaluator_jobs import PovVerdict, SinglePovResult

        verdict = PovVerdict(
            pov_id="test.blob:hash456",
            triggered_bug=False,
            cpv_matches=[],
            crash_logs={"base-asan": "no crash"},
        )
        result = SinglePovResult(
            trial_id="trial-1",
            benchmark="test-benchmark",
            harness="fuzz_parser",
            verdict=verdict,
            completed_at=1700000100.0,
        )
        mock_poll.return_value = ([result.to_dict()], [])

        manager = self._make_manager(tmp_path, redis_host="redis.local")
        manager._pending_job_ids = ["job-1"]

        manager._poll_pending_verdicts()

        # No blobs for non-CPV, but crash logs ARE stored
        povs_dir = tmp_path / "trial-1" / "povs"
        stored_blobs = list(povs_dir.rglob("*.blob"))
        stored_logs = list(povs_dir.rglob("*.log"))
        assert len(stored_blobs) == 0  # No blobs for non-CPV
        assert len(stored_logs) == 1  # Crash logs ARE stored for non-CPV

    @patch("crsbench.distributed.verify_queue.poll_single_pov_verdicts")
    def test_poll_handles_unintended_crash(self, mock_poll, tmp_path: Path) -> None:
        """UNINTENDED_CRASH verdicts increment the unintended_crashes_count."""
        from crsbench.distributed.evaluator_jobs import PovVerdict, SinglePovResult

        verdict = PovVerdict(
            pov_id="test.blob:unintended123",
            triggered_bug=False,
            status="unintended_crash",
            cpv_matches=[],
        )
        result = SinglePovResult(
            trial_id="trial-1",
            benchmark="test-benchmark",
            harness="fuzz_parser",
            verdict=verdict,
            completed_at=1700000100.0,
        )
        mock_poll.return_value = ([result.to_dict()], [])

        manager = self._make_manager(tmp_path, redis_host="redis.local")
        manager._pending_job_ids = ["job-1"]

        assert manager._unintended_crashes_count == 0
        manager._poll_pending_verdicts()

        assert manager._unintended_crashes_count == 1
        # No CPVs should be found
        assert len(manager.found_cpvs) == 0

    def test_on_snapshot_async_enqueues_and_polls(self, tmp_path: Path) -> None:
        """In async mode, on_snapshot enqueues new POVs and polls verdicts."""
        manager = self._make_manager(tmp_path, redis_host="redis.local")

        # Create a POV file
        pov_file = tmp_path / "trial-1" / "pov_output" / "pov1.blob"
        pov_file.write_bytes(b"async_pov_content")

        # Mock the enqueue and poll methods
        manager._enqueue_pov = MagicMock(return_value="job-456")
        manager._poll_pending_verdicts = MagicMock()

        snapshot = manager.on_snapshot(cycle=1)

        # Should have enqueued the POV
        manager._enqueue_pov.assert_called_once()
        # Should have polled for results
        manager._poll_pending_verdicts.assert_called_once()
        # POV hash should be marked tested in store
        assert snapshot.povs_new == 1
        assert len(manager._pending_job_ids) == 1

    def test_on_snapshot_inline_verifies_directly(self, tmp_path: Path) -> None:
        """In inline mode, on_snapshot verifies POVs synchronously."""
        from crsbench.evaluation.verification.models import PovVerificationResult

        manager = self._make_manager(tmp_path, redis_host=None)

        # Create a POV file
        pov_file = tmp_path / "trial-1" / "pov_output" / "pov1.blob"
        pov_file.write_bytes(b"inline_pov_content")

        # Mock the engine and adapter for inline verification
        mock_result = PovVerificationResult(
            status=PovVerificationStatus.NOT_VULNERABLE,
            benchmark="test-benchmark",
            pov_id="pov1.blob",
            cpv_matched=[],
        )
        manager._engine = MagicMock()
        manager._engine.verify_pov.return_value = mock_result
        manager._adapter = MagicMock()

        snapshot = manager.on_snapshot(cycle=1)

        # Should have verified inline
        manager._engine.verify_pov.assert_called_once()
        assert snapshot.povs_new == 1


class TestExchangeDirScanning:
    """Tests for EXCHANGE_DIR POV discovery."""

    def _make_exchange_dir(self, work_dir: Path, harness_name: str) -> Path:
        """Create a mock EXCHANGE_DIR structure and return the pov/ path."""
        exchange = (
            work_dir
            / "crs_compose"
            / "hash1"
            / "address"
            / "runs"
            / "run-0"
            / "EXCHANGE_DIR"
            / "target_1"
            / harness_name
            / "pov"
        )
        exchange.mkdir(parents=True)
        return exchange

    def _make_manager(self, tmp_path: Path, *, work_dir: Path = None):
        from crsbench.evaluation.verification.pov.manager import (
            POVVerificationManager,
        )

        trial_dir = tmp_path / "trial-1"
        trial_dir.mkdir(exist_ok=True)
        pov_output_dir = trial_dir / "pov_output"
        pov_output_dir.mkdir(exist_ok=True)

        config = POVVerificationConfig()
        return POVVerificationManager(
            trial_dir=trial_dir,
            pov_output_dir=pov_output_dir,
            config=config,
            harness_name="fuzz_parser",
            benchmark_id="test-benchmark",
            expected_cpv_ids=["cpv_0", "cpv_1"],
            work_dir=work_dir,
        )

    def test_discover_from_exchange_dir(self, tmp_path: Path) -> None:
        """POVs in EXCHANGE_DIR/pov/ are discovered alongside pov_output_dir."""
        work_dir = tmp_path / "workdir"
        work_dir.mkdir()
        exchange_pov = self._make_exchange_dir(work_dir, "fuzz_parser")

        # Write POV to EXCHANGE_DIR only (not to pov_output_dir)
        (exchange_pov / "exchange_pov.blob").write_bytes(b"exchange_pov_data")

        manager = self._make_manager(tmp_path, work_dir=work_dir)
        new_povs = manager._discover_new_povs()

        assert len(new_povs) == 1
        assert new_povs[0][0].name == "exchange_pov.blob"

    def test_discover_merges_both_dirs(self, tmp_path: Path) -> None:
        """POVs from both pov_output_dir and EXCHANGE_DIR are merged."""
        work_dir = tmp_path / "workdir"
        work_dir.mkdir()
        exchange_pov = self._make_exchange_dir(work_dir, "fuzz_parser")

        manager = self._make_manager(tmp_path, work_dir=work_dir)

        # Write POV to pov_output_dir
        (manager.pov_output_dir / "output_pov.blob").write_bytes(b"output_data")
        # Write different POV to EXCHANGE_DIR
        (exchange_pov / "exchange_pov.blob").write_bytes(b"exchange_data")

        new_povs = manager._discover_new_povs()
        names = {p.name for p, _ in new_povs}

        assert len(new_povs) == 2
        assert "output_pov.blob" in names
        assert "exchange_pov.blob" in names

    def test_dedup_same_content_across_dirs(self, tmp_path: Path) -> None:
        """Same POV content in both dirs is deduplicated within a single call."""
        work_dir = tmp_path / "workdir"
        work_dir.mkdir()
        exchange_pov = self._make_exchange_dir(work_dir, "fuzz_parser")

        manager = self._make_manager(tmp_path, work_dir=work_dir)

        # Write identical content to both directories
        content = b"identical_pov_content"
        (manager.pov_output_dir / "pov.blob").write_bytes(content)
        (exchange_pov / "pov_copy.blob").write_bytes(content)

        new_povs = manager._discover_new_povs()

        # Same hash in both dirs → only one returned (within-call dedup)
        assert len(new_povs) == 1

    def test_no_work_dir_falls_back_gracefully(self, tmp_path: Path) -> None:
        """Without work_dir, only pov_output_dir is scanned."""
        manager = self._make_manager(tmp_path, work_dir=None)
        (manager.pov_output_dir / "pov.blob").write_bytes(b"data")

        new_povs = manager._discover_new_povs()
        assert len(new_povs) == 1

    def test_lazy_resolution_exchange_dir_not_yet_created(self, tmp_path: Path) -> None:
        """Exchange dir not found on first call, found on subsequent call."""
        work_dir = tmp_path / "workdir"
        work_dir.mkdir()

        manager = self._make_manager(tmp_path, work_dir=work_dir)

        # First call: EXCHANGE_DIR doesn't exist yet
        assert manager._resolve_exchange_pov_dir() is None

        # Create EXCHANGE_DIR structure
        exchange_pov = self._make_exchange_dir(work_dir, "fuzz_parser")
        (exchange_pov / "pov.blob").write_bytes(b"data")

        # Second call: now found and cached
        result = manager._resolve_exchange_pov_dir()
        assert result is not None
        assert result == exchange_pov

        # Third call: uses cached value
        result2 = manager._resolve_exchange_pov_dir()
        assert result2 == result
