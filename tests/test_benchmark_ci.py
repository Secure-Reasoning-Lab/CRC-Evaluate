"""Tests for benchmark_ci module.

Tests the simplified benchmark CI validation that delegates to existing engines,
avoiding race conditions by reusing VerificationEngine's pre-build pattern.

Related: Issue #59 - variant-level locking for concurrent build conflicts
"""

from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from crsbench.benchmark_ci.models import (
    BenchmarkValidationResult,
    CheckResult,
    CheckStatus,
    ValidationSummary,
)
from crsbench.benchmark_ci.validator import BenchmarkValidator


class TestCheckResult:
    """Tests for CheckResult dataclass."""

    def test_check_result_creation(self) -> None:
        """Test basic CheckResult creation."""
        result = CheckResult(
            status=CheckStatus.PASS,
            time_seconds=1.5,
            error="",
            details={"key": "value"},
        )
        assert result.status == CheckStatus.PASS
        assert result.time_seconds == 1.5
        assert result.error == ""
        assert result.details == {"key": "value"}

    def test_check_result_skip_factory(self) -> None:
        """Test CheckResult.skip() factory method."""
        result = CheckResult.skip("test reason")
        assert result.status == CheckStatus.SKIP
        assert result.time_seconds == 0.0
        assert result.error == "test reason"
        assert result.details == {}

    def test_check_result_make_error_factory(self) -> None:
        """Test CheckResult.make_error() factory method."""
        result = CheckResult.make_error("error message", 2.5)
        assert result.status == CheckStatus.ERROR
        assert result.time_seconds == 2.5
        assert result.error == "error message"
        assert result.details == {}

    def test_check_result_to_dict(self) -> None:
        """Test CheckResult.to_dict() serialization."""
        result = CheckResult(
            status=CheckStatus.FAIL,
            time_seconds=3.0,
            error="test error",
            details={"count": 5},
        )
        d = result.to_dict()
        assert d == {
            "status": "fail",
            "time_seconds": 3.0,
            "build_time": 0.0,
            "verify_time": 0.0,
            "error": "test error",
            "details": {"count": 5},
            "fallback_used": False,
        }

    def test_check_result_build_verify_times(self) -> None:
        """Test CheckResult with build_time and verify_time fields."""
        result = CheckResult(
            status=CheckStatus.PASS,
            time_seconds=90.0,
            build_time=60.0,
            verify_time=30.0,
        )
        assert result.build_time == 60.0
        assert result.verify_time == 30.0
        d = result.to_dict()
        assert d["build_time"] == 60.0
        assert d["verify_time"] == 30.0

    def test_check_result_format_status_verify_only(self) -> None:
        """Test format_status shows V:Xs when only verify_time is set."""
        result = CheckResult(
            status=CheckStatus.PASS,
            time_seconds=30.0,
            verify_time=30.0,
        )
        assert result.format_status() == "PASS(V:30s)"

    def test_check_result_format_status_build_and_verify(self) -> None:
        """Test format_status shows B:Xm V:Ys when both are set."""
        result = CheckResult(
            status=CheckStatus.PASS,
            time_seconds=150.0,
            build_time=120.0,
            verify_time=30.0,
        )
        assert result.format_status() == "PASS(B:2m V:30s)"

    def test_check_result_format_status_total_only(self) -> None:
        """Test format_status shows total when no split times."""
        result = CheckResult(
            status=CheckStatus.PASS,
            time_seconds=120.0,
        )
        assert result.format_status() == "PASS(2m)"


class TestBenchmarkValidationResult:
    """Tests for BenchmarkValidationResult dataclass."""

    def test_total_status_all_pass(self) -> None:
        """Test total_status when all checks pass."""
        result = BenchmarkValidationResult(
            benchmark="test-bench",
            benchmark_path=Path("/tmp/test"),
            format_check=CheckResult(status=CheckStatus.PASS, time_seconds=1.0),
            pov_check=CheckResult(status=CheckStatus.PASS, time_seconds=2.0),
            patch_check=CheckResult(status=CheckStatus.PASS, time_seconds=3.0),
        )
        assert result.total_status == CheckStatus.PASS

    def test_total_status_with_failure(self) -> None:
        """Test total_status when one check fails."""
        result = BenchmarkValidationResult(
            benchmark="test-bench",
            benchmark_path=Path("/tmp/test"),
            format_check=CheckResult(status=CheckStatus.PASS, time_seconds=1.0),
            pov_check=CheckResult(status=CheckStatus.FAIL, time_seconds=2.0),
            patch_check=CheckResult(status=CheckStatus.PASS, time_seconds=3.0),
        )
        assert result.total_status == CheckStatus.FAIL

    def test_total_status_with_error(self) -> None:
        """Test total_status when one check has error."""
        result = BenchmarkValidationResult(
            benchmark="test-bench",
            benchmark_path=Path("/tmp/test"),
            format_check=CheckResult(status=CheckStatus.PASS, time_seconds=1.0),
            pov_check=CheckResult(status=CheckStatus.PASS, time_seconds=2.0),
            patch_check=CheckResult(status=CheckStatus.ERROR, time_seconds=3.0),
        )
        assert result.total_status == CheckStatus.ERROR

    def test_total_status_with_some_skip(self) -> None:
        """Test total_status when some checks are skipped but executed ones pass."""
        result = BenchmarkValidationResult(
            benchmark="test-bench",
            benchmark_path=Path("/tmp/test"),
            format_check=CheckResult(status=CheckStatus.PASS, time_seconds=1.0),
            pov_check=CheckResult(status=CheckStatus.SKIP, time_seconds=0.0),
            patch_check=CheckResult(status=CheckStatus.SKIP, time_seconds=0.0),
        )
        # Executed checks (format) passed, skipped checks don't affect total
        assert result.total_status == CheckStatus.PASS

    def test_total_status_all_skip(self) -> None:
        """Test total_status when all checks are skipped."""
        result = BenchmarkValidationResult(
            benchmark="test-bench",
            benchmark_path=Path("/tmp/test"),
            format_check=CheckResult(status=CheckStatus.SKIP, time_seconds=0.0),
            pov_check=CheckResult(status=CheckStatus.SKIP, time_seconds=0.0),
            patch_check=CheckResult(status=CheckStatus.SKIP, time_seconds=0.0),
        )
        # All checks skipped, so total should be SKIP
        assert result.total_status == CheckStatus.SKIP

    def test_total_time(self) -> None:
        """Test total_time calculation."""
        result = BenchmarkValidationResult(
            benchmark="test-bench",
            benchmark_path=Path("/tmp/test"),
            format_check=CheckResult(status=CheckStatus.PASS, time_seconds=1.5),
            pov_check=CheckResult(status=CheckStatus.PASS, time_seconds=2.5),
            patch_check=CheckResult(status=CheckStatus.PASS, time_seconds=3.0),
        )
        assert result.total_time == 7.0

    def test_total_time_with_coverage(self) -> None:
        """Test total_time includes coverage when present."""
        result = BenchmarkValidationResult(
            benchmark="test-bench",
            benchmark_path=Path("/tmp/test"),
            format_check=CheckResult(status=CheckStatus.PASS, time_seconds=1.0),
            pov_check=CheckResult(status=CheckStatus.PASS, time_seconds=2.0),
            patch_check=CheckResult(status=CheckStatus.PASS, time_seconds=3.0),
            coverage_check=CheckResult(status=CheckStatus.PASS, time_seconds=4.0),
        )
        assert result.total_time == 10.0

    def test_to_dict(self) -> None:
        """Test BenchmarkValidationResult.to_dict() serialization."""
        started = datetime(2024, 1, 1, 12, 0, 0)
        finished = datetime(2024, 1, 1, 12, 0, 10)
        result = BenchmarkValidationResult(
            benchmark="test-bench",
            benchmark_path=Path("/tmp/test"),
            format_check=CheckResult(status=CheckStatus.PASS, time_seconds=1.0),
            pov_check=CheckResult(status=CheckStatus.PASS, time_seconds=2.0),
            patch_check=CheckResult(status=CheckStatus.PASS, time_seconds=3.0),
            started_at=started,
            finished_at=finished,
        )
        d = result.to_dict()
        assert d["benchmark"] == "test-bench"
        assert d["total_status"] == "pass"
        assert d["total_time_seconds"] == 6.0
        assert d["format_check"]["status"] == "pass"
        assert d["started_at"] == "2024-01-01T12:00:00"


class TestValidationSummary:
    """Tests for ValidationSummary dataclass."""

    def test_empty_summary(self) -> None:
        """Test empty ValidationSummary."""
        summary = ValidationSummary(started_at=datetime.now())
        assert summary.total == 0
        assert summary.passed == 0
        assert summary.failed == 0
        assert summary.errors == 0

    def test_summary_counts(self) -> None:
        """Test ValidationSummary counts results correctly."""
        summary = ValidationSummary(started_at=datetime.now())

        # Add a passing result
        summary.add_result(
            BenchmarkValidationResult(
                benchmark="bench1",
                benchmark_path=Path("/tmp/bench1"),
                format_check=CheckResult(status=CheckStatus.PASS, time_seconds=1.0),
                pov_check=CheckResult(status=CheckStatus.PASS, time_seconds=1.0),
                patch_check=CheckResult(status=CheckStatus.PASS, time_seconds=1.0),
            )
        )

        # Add a failing result
        summary.add_result(
            BenchmarkValidationResult(
                benchmark="bench2",
                benchmark_path=Path("/tmp/bench2"),
                format_check=CheckResult(status=CheckStatus.PASS, time_seconds=1.0),
                pov_check=CheckResult(status=CheckStatus.FAIL, time_seconds=1.0),
                patch_check=CheckResult(status=CheckStatus.PASS, time_seconds=1.0),
            )
        )

        # Add an error result
        summary.add_result(
            BenchmarkValidationResult(
                benchmark="bench3",
                benchmark_path=Path("/tmp/bench3"),
                format_check=CheckResult(status=CheckStatus.ERROR, time_seconds=1.0),
                pov_check=CheckResult(status=CheckStatus.SKIP, time_seconds=0.0),
                patch_check=CheckResult(status=CheckStatus.SKIP, time_seconds=0.0),
            )
        )

        assert summary.total == 3
        assert summary.passed == 1
        assert summary.failed == 1
        assert summary.errors == 1

    def test_to_dict(self) -> None:
        """Test ValidationSummary.to_dict() serialization."""
        summary = ValidationSummary(started_at=datetime(2024, 1, 1, 12, 0, 0))
        summary.add_result(
            BenchmarkValidationResult(
                benchmark="bench1",
                benchmark_path=Path("/tmp/bench1"),
                format_check=CheckResult(status=CheckStatus.PASS, time_seconds=1.0),
                pov_check=CheckResult(status=CheckStatus.PASS, time_seconds=1.0),
                patch_check=CheckResult(status=CheckStatus.PASS, time_seconds=1.0),
            )
        )
        summary.finished_at = datetime(2024, 1, 1, 12, 0, 10)

        d = summary.to_dict()
        assert d["summary"]["total"] == 1
        assert d["summary"]["passed"] == 1
        assert len(d["results"]) == 1
        assert d["started_at"] == "2024-01-01T12:00:00"


class TestBenchmarkValidator:
    """Tests for BenchmarkValidator class.

    These tests verify the validator delegates to existing engines correctly,
    which is the key fix for the race condition issue (#59).
    """

    def test_validate_format_passes(self, tmp_path: Path) -> None:
        """Test format validation delegates to format_validate."""
        # Create a minimal benchmark structure
        benchmark_path = tmp_path / "test-benchmark"
        benchmark_path.mkdir()
        (benchmark_path / ".aixcc").mkdir()

        validator = BenchmarkValidator()

        with patch("crsbench.benchmark_ci.validator.format_validate") as mock_validate:
            mock_result = MagicMock()
            mock_result.is_valid = True
            mock_result.warning_count = 0
            mock_validate.return_value = mock_result

            result = validator.validate_format(benchmark_path)

            assert result.status == CheckStatus.PASS
            mock_validate.assert_called_once_with(benchmark_path)

    def test_validate_format_fails(self, tmp_path: Path) -> None:
        """Test format validation returns FAIL on invalid benchmark."""
        benchmark_path = tmp_path / "test-benchmark"
        benchmark_path.mkdir()

        validator = BenchmarkValidator()

        with patch("crsbench.benchmark_ci.validator.format_validate") as mock_validate:
            mock_error = MagicMock()
            mock_error.message = "Missing required field"

            mock_result = MagicMock()
            mock_result.is_valid = False
            mock_result.errors = [mock_error]
            mock_result.error_count = 1
            mock_result.warning_count = 0
            mock_validate.return_value = mock_result

            result = validator.validate_format(benchmark_path)

            assert result.status == CheckStatus.FAIL
            assert "Missing required field" in result.error

    def test_validate_benchmark_with_skip_format(self, tmp_path: Path) -> None:
        """Test skip_format option skips format validation."""
        benchmark_path = tmp_path / "test-benchmark"
        benchmark_path.mkdir()

        validator = BenchmarkValidator()

        with (
            patch.object(validator, "validate_format") as mock_format,
            patch.object(validator, "validate_povs") as mock_pov,
            patch.object(validator, "validate_patches") as mock_patch,
        ):
            mock_pov.return_value = CheckResult(
                status=CheckStatus.PASS, time_seconds=1.0
            )
            mock_patch.return_value = CheckResult(
                status=CheckStatus.PASS, time_seconds=1.0
            )

            result = validator.validate_benchmark(benchmark_path, skip_format=True)

            # Format should not be called
            mock_format.assert_not_called()
            # Other validations should run
            mock_pov.assert_called_once()
            mock_patch.assert_called_once()
            # Format result should be skip
            assert result.format_check.status == CheckStatus.SKIP
            assert result.format_check.error == "--skip-format"

    def test_validate_benchmark_with_skip_verify(self, tmp_path: Path) -> None:
        """Test skip_verify option skips POV verification."""
        benchmark_path = tmp_path / "test-benchmark"
        benchmark_path.mkdir()

        validator = BenchmarkValidator()

        with (
            patch.object(validator, "validate_format") as mock_format,
            patch.object(validator, "validate_povs") as mock_pov,
            patch.object(validator, "validate_patches") as mock_patch,
        ):
            mock_format.return_value = CheckResult(
                status=CheckStatus.PASS, time_seconds=1.0
            )
            mock_patch.return_value = CheckResult(
                status=CheckStatus.PASS, time_seconds=1.0
            )

            result = validator.validate_benchmark(benchmark_path, skip_verify=True)

            mock_format.assert_called_once()
            mock_pov.assert_not_called()  # Should not be called
            mock_patch.assert_called_once()
            assert result.pov_check.status == CheckStatus.SKIP
            assert result.pov_check.error == "--skip-verify"

    def test_validate_benchmark_with_skip_patch_verify(self, tmp_path: Path) -> None:
        """Test skip_patch_verify option skips patch verification."""
        benchmark_path = tmp_path / "test-benchmark"
        benchmark_path.mkdir()

        validator = BenchmarkValidator()

        with (
            patch.object(validator, "validate_format") as mock_format,
            patch.object(validator, "validate_povs") as mock_pov,
            patch.object(validator, "validate_patches") as mock_patch,
        ):
            mock_format.return_value = CheckResult(
                status=CheckStatus.PASS, time_seconds=1.0
            )
            mock_pov.return_value = CheckResult(
                status=CheckStatus.PASS, time_seconds=1.0
            )

            result = validator.validate_benchmark(
                benchmark_path, skip_patch_verify=True
            )

            mock_format.assert_called_once()
            mock_pov.assert_called_once()
            mock_patch.assert_not_called()  # Should not be called
            assert result.patch_check.status == CheckStatus.SKIP
            assert result.patch_check.error == "--skip-patch-verify"

    def test_validate_benchmark_early_exit_on_format_failure(
        self, tmp_path: Path
    ) -> None:
        """Test validation stops early if format check fails."""
        benchmark_path = tmp_path / "test-benchmark"
        benchmark_path.mkdir()

        validator = BenchmarkValidator()

        with (
            patch.object(validator, "validate_format") as mock_format,
            patch.object(validator, "validate_povs") as mock_pov,
            patch.object(validator, "validate_patches") as mock_patch,
        ):
            mock_format.return_value = CheckResult(
                status=CheckStatus.FAIL,
                time_seconds=1.0,
                error="Invalid format",
            )

            result = validator.validate_benchmark(benchmark_path)

            mock_format.assert_called_once()
            mock_pov.assert_not_called()  # Should not run
            mock_patch.assert_not_called()  # Should not run
            assert result.format_check.status == CheckStatus.FAIL
            assert result.pov_check.status == CheckStatus.SKIP
            assert "format failure" in result.pov_check.error

    def test_validate_povs_delegates_to_verification_engine(
        self, tmp_path: Path
    ) -> None:
        """Test POV validation delegates to VerificationEngine.

        This is key to avoiding race conditions - VerificationEngine
        pre-builds all variants before parallel verification.
        """
        benchmark_path = tmp_path / "test-benchmark"
        benchmark_path.mkdir()

        validator = BenchmarkValidator()

        with patch(
            "crsbench.benchmark_ci.validator.VerificationEngine"
        ) as mock_engine_class:
            mock_engine = MagicMock()
            mock_engine_class.return_value = mock_engine

            # Mock successful POV verification
            mock_pov_result = MagicMock()
            mock_pov_result.pov_id = "pov_001"
            mock_pov_result.status.value = "CPV"
            # Use the actual enum and dataclass
            from crsbench.evaluation.verification.models import (
                PovBenchmarkOutput,
                PovVerificationStatus,
            )

            mock_pov_result.status = PovVerificationStatus.CPV

            mock_engine.verify_benchmark.return_value = PovBenchmarkOutput(
                results=[mock_pov_result], skipped_count=0, fallback_used=False
            )

            result = validator.validate_povs(benchmark_path)

            # Verify engine was created and used
            mock_engine_class.assert_called_once()
            mock_engine.verify_benchmark.assert_called_once_with(
                benchmark_path, force_rebuild=False, use_inc_build=False
            )
            assert result.status == CheckStatus.PASS

    def test_validate_patches_delegates_to_patch_engine(self, tmp_path: Path) -> None:
        """Test patch validation delegates to PatchVerificationEngine."""
        benchmark_path = tmp_path / "test-benchmark"
        benchmark_path.mkdir()

        validator = BenchmarkValidator()

        with patch(
            "crsbench.benchmark_ci.validator.PatchVerificationEngine"
        ) as mock_engine_class:
            mock_engine = MagicMock()
            mock_engine_class.return_value = mock_engine

            # Mock successful patch verification with dataclass return type
            mock_patch_result = MagicMock()
            mock_patch_result.patch_id = "patch_001"
            from crsbench.evaluation.verification.models import (
                PatchBenchmarkOutput,
                PatchVerificationStatus,
            )

            mock_patch_result.status = PatchVerificationStatus.VALID

            mock_engine.verify_benchmark.return_value = PatchBenchmarkOutput(
                results=[mock_patch_result], fallback_used=False
            )

            result = validator.validate_patches(benchmark_path)

            mock_engine_class.assert_called_once()
            mock_engine.verify_benchmark.assert_called_once_with(benchmark_path)
            mock_engine.cleanup.assert_called_once()
            assert result.status == CheckStatus.PASS


class TestCLIHelpers:
    """Tests for CLI helper functions."""

    def test_format_time_seconds(self) -> None:
        """Test format_time with seconds."""
        from crsbench.benchmark_ci.cli.main import format_time

        assert format_time(30.5) == "30.5s"
        assert format_time(0.1) == "0.1s"

    def test_format_time_minutes(self) -> None:
        """Test format_time with minutes."""
        from crsbench.benchmark_ci.cli.main import format_time

        assert format_time(90) == "1m30s"
        assert format_time(125.7) == "2m6s"

    def test_format_time_hours(self) -> None:
        """Test format_time with hours."""
        from crsbench.benchmark_ci.cli.main import format_time

        assert format_time(3661) == "1h1m"

    def test_discover_benchmarks(self, tmp_path: Path) -> None:
        """Test benchmark discovery."""
        from crsbench.benchmark_ci.cli.main import discover_benchmarks

        # Create some benchmark directories
        (tmp_path / "bench1" / ".aixcc").mkdir(parents=True)
        (tmp_path / "bench2" / ".aixcc").mkdir(parents=True)
        (tmp_path / "not-a-bench").mkdir()  # No .aixcc
        (tmp_path / ".hidden" / ".aixcc").mkdir(parents=True)  # Hidden

        benchmarks = discover_benchmarks(tmp_path)

        assert len(benchmarks) == 2
        names = [b.name for b in benchmarks]
        assert "bench1" in names
        assert "bench2" in names
        assert "not-a-bench" not in names
        assert ".hidden" not in names

    def test_discover_benchmarks_with_filter(self, tmp_path: Path) -> None:
        """Test benchmark discovery with filter pattern."""
        from crsbench.benchmark_ci.cli.main import discover_benchmarks

        (tmp_path / "sanity-test-01" / ".aixcc").mkdir(parents=True)
        (tmp_path / "sanity-test-02" / ".aixcc").mkdir(parents=True)
        (tmp_path / "afc-proj-01" / ".aixcc").mkdir(parents=True)

        benchmarks = discover_benchmarks(tmp_path, filter_pattern="sanity-*")

        assert len(benchmarks) == 2
        names = [b.name for b in benchmarks]
        assert "sanity-test-01" in names
        assert "sanity-test-02" in names
        assert "afc-proj-01" not in names
