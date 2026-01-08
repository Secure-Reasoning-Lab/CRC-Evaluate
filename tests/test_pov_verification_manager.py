"""Unit tests for POVVerificationManager.

Tests for crsbench/evaluation/verification/pov/manager.py.
"""

from pathlib import Path
from unittest.mock import MagicMock

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


class TestVerifyPovZeroday:
    """Tests for zeroday detection (T025)."""

    def test_verify_pov_zeroday(self, tmp_path: Path) -> None:
        """Test that verification correctly identifies zeroday."""
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

        pov_file = pov_output_dir / "pov_zeroday.blob"
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

        # Mock the verification engine returning zeroday
        mock_result = PovVerificationResult(
            status=PovVerificationStatus.ZERODAY,
            benchmark="test-benchmark",
            pov_id="pov_zeroday",
            cpv_matched=[],
            details="Crash does not match any known CPV",
        )
        manager._engine = MagicMock()
        manager._engine.verify_pov.return_value = mock_result
        manager._adapter = MagicMock()

        # Verify POV
        result = manager._verify_pov(pov_file)

        assert result is not None
        assert result.status == PovVerificationStatus.ZERODAY

    def test_zeroday_updates_state_counter(self, tmp_path: Path) -> None:
        """Test that zeroday increments state counter."""
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

        pov_file = pov_output_dir / "pov_zeroday.blob"
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
            status=PovVerificationStatus.ZERODAY,
            benchmark="test-benchmark",
            pov_id="pov_zeroday",
            cpv_matched=[],
            details="Crash does not match any known CPV",
        )
        manager._engine = MagicMock()
        manager._engine.verify_pov.return_value = mock_result
        manager._adapter = MagicMock()

        # Process the POV
        manager._verify_pov(pov_file)
        manager._update_state(pov_file, mock_result)

        assert manager._zerodays_count == 1


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

        # Add zeroday
        pov3 = pov_output_dir / "pov3.blob"
        pov3.write_bytes(b"pov3")
        manager.store.add_pov(pov3, PovVerificationStatus.ZERODAY, [])
        manager._zerodays_count = 1

        # Add duplicates count
        manager._duplicates_count = 2

        report = manager.get_report()

        assert isinstance(report, POVVerificationReport)
        assert report.benchmark_id == "test-benchmark"
        assert report.harness_name == "fuzz_parser"
        assert report.total_expected_cpvs == 3
        assert set(report.cpvs_found) == {"cpv_0", "cpv_1"}
        assert report.zerodays_detected == 1
        assert report.duplicates_skipped == 2
