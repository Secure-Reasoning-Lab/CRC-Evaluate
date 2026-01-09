"""Unit tests for POV verification data models.

Tests for crsbench/evaluation/verification/pov/models.py.
"""

from datetime import datetime

from crsbench.evaluation.verification.models import PovVerificationStatus
from crsbench.evaluation.verification.pov.models import (
    POVEntry,
    POVSnapshot,
    POVVerificationReport,
)


class TestPOVEntry:
    """Tests for POVEntry model."""

    def test_required_fields(self) -> None:
        """Test creating POVEntry with required fields."""
        entry = POVEntry(
            hash="abc123def456",
            first_seen_ts=1704672000.0,
            file_size=256,
            status=PovVerificationStatus.CPV,
        )

        assert entry.hash == "abc123def456"
        assert entry.first_seen_ts == 1704672000.0
        assert entry.file_size == 256
        assert entry.status == PovVerificationStatus.CPV

    def test_default_values(self) -> None:
        """Test default values for optional fields."""
        entry = POVEntry(
            hash="abc123def456",
            first_seen_ts=1704672000.0,
            file_size=256,
            status=PovVerificationStatus.CPV,
        )

        assert entry.cpv_matched == []
        assert entry.crash_log_path is None
        assert entry.verification_duration == 0.0

    def test_full_entry(self) -> None:
        """Test creating POVEntry with all fields."""
        entry = POVEntry(
            hash="abc123def456",
            first_seen_ts=1704672000.0,
            file_size=256,
            status=PovVerificationStatus.CPV,
            cpv_matched=["cpv_0", "cpv_1"],
            crash_log_path="crash_logs/abc123def456.log",
            verification_duration=45.2,
        )

        assert entry.cpv_matched == ["cpv_0", "cpv_1"]
        assert entry.crash_log_path == "crash_logs/abc123def456.log"
        assert entry.verification_duration == 45.2

    def test_all_status_values(self) -> None:
        """Test POVEntry with different PovVerificationStatus values."""
        for status in [
            PovVerificationStatus.CPV,
            PovVerificationStatus.ZERODAY,
            PovVerificationStatus.NOT_VULNERABLE,
            PovVerificationStatus.ERROR,
        ]:
            entry = POVEntry(
                hash="abc123def456",
                first_seen_ts=1704672000.0,
                file_size=256,
                status=status,
            )
            assert entry.status == status


class TestPOVSnapshot:
    """Tests for POVSnapshot model."""

    def test_required_fields(self) -> None:
        """Test creating POVSnapshot with required fields."""
        snapshot = POVSnapshot(
            cycle=1,
            timestamp=1704672060.0,
            elapsed_time=60.0,
            harness_name="fuzz_parser",
        )

        assert snapshot.cycle == 1
        assert snapshot.timestamp == 1704672060.0
        assert snapshot.elapsed_time == 60.0
        assert snapshot.harness_name == "fuzz_parser"

    def test_default_values(self) -> None:
        """Test default values for optional fields."""
        snapshot = POVSnapshot(
            cycle=1,
            timestamp=1704672060.0,
            elapsed_time=60.0,
            harness_name="fuzz_parser",
        )

        assert snapshot.cpvs_found == []
        assert snapshot.cpvs_remaining == []
        assert snapshot.povs_total == 0
        assert snapshot.povs_new == 0
        assert snapshot.duplicates_skipped == 0
        assert snapshot.zerodays_count == 0
        assert snapshot.early_stop_triggered is False

    def test_full_snapshot(self) -> None:
        """Test creating POVSnapshot with all fields."""
        snapshot = POVSnapshot(
            cycle=2,
            timestamp=1704672120.0,
            elapsed_time=120.0,
            harness_name="fuzz_parser",
            cpvs_found=["cpv_0", "cpv_1"],
            cpvs_remaining=["cpv_2"],
            povs_total=10,
            povs_new=3,
            duplicates_skipped=2,
            zerodays_count=1,
            early_stop_triggered=False,
        )

        assert snapshot.cpvs_found == ["cpv_0", "cpv_1"]
        assert snapshot.cpvs_remaining == ["cpv_2"]
        assert snapshot.povs_total == 10


class TestPOVVerificationReport:
    """Tests for POVVerificationReport model."""

    def test_required_fields(self) -> None:
        """Test creating report with required fields."""
        report = POVVerificationReport(
            benchmark_id="test-benchmark",
            harness_name="fuzz_parser",
            total_expected_cpvs=3,
        )

        assert report.benchmark_id == "test-benchmark"
        assert report.harness_name == "fuzz_parser"
        assert report.total_expected_cpvs == 3

    def test_default_values(self) -> None:
        """Test default values for optional fields."""
        report = POVVerificationReport(
            benchmark_id="test-benchmark",
            harness_name="fuzz_parser",
            total_expected_cpvs=3,
        )

        assert report.cpvs_found == []
        assert report.cpvs_remaining == []
        assert report.total_povs_processed == 0
        assert report.duplicates_skipped == 0
        assert report.zerodays_detected == 0
        assert report.verification_errors == 0
        assert report.verification_timeouts == 0
        assert report.early_stopped is False
        assert report.early_stop_time is None
        assert report.total_duration_seconds == 0.0

    def test_to_dict(self) -> None:
        """Test report serialization to dict."""
        early_stop_time = datetime(2024, 1, 8, 12, 30, 0)
        report = POVVerificationReport(
            benchmark_id="test-benchmark",
            harness_name="fuzz_parser",
            total_expected_cpvs=3,
            cpvs_found=["cpv_0", "cpv_1"],
            cpvs_remaining=["cpv_2"],
            total_povs_processed=15,
            duplicates_skipped=5,
            zerodays_detected=1,
            verification_errors=2,
            verification_timeouts=1,
            early_stopped=True,
            early_stop_time=early_stop_time,
            total_duration_seconds=300.5,
        )

        data = report.to_dict()

        assert data["benchmark_id"] == "test-benchmark"
        assert data["harness_name"] == "fuzz_parser"
        assert data["total_expected_cpvs"] == 3
        assert data["cpvs_found"] == ["cpv_0", "cpv_1"]
        assert data["cpvs_remaining"] == ["cpv_2"]
        assert data["total_povs_processed"] == 15
        assert data["duplicates_skipped"] == 5
        assert data["zerodays_detected"] == 1
        assert data["verification_errors"] == 2
        assert data["verification_timeouts"] == 1
        assert data["early_stopped"] is True
        assert data["early_stop_time"] == early_stop_time.isoformat()
        assert data["total_duration_seconds"] == 300.5

    def test_to_dict_without_early_stop_time(self) -> None:
        """Test to_dict when early_stop_time is None."""
        report = POVVerificationReport(
            benchmark_id="test-benchmark",
            harness_name="fuzz_parser",
            total_expected_cpvs=3,
        )

        data = report.to_dict()

        assert data["early_stop_time"] is None
