"""Tests for verify_single_pov() per-POV RQ job function."""

import time
from unittest.mock import MagicMock, patch

from crsbench.distributed.evaluator_jobs import (
    EmbeddedPov,
    PovVerdict,
    SinglePovPayload,
    SinglePovResult,
    verify_single_pov,
)


class TestSinglePovPayload:
    """SinglePovPayload serialization."""

    def test_to_dict_roundtrip(self) -> None:
        pov = EmbeddedPov.from_bytes("pov_0", b"crash_input")
        payload = SinglePovPayload(
            experiment_name="exp-1",
            trial_id="trial-0",
            benchmark="test-bench",
            harness="fuzz_target",
            pov=pov,
            enqueued_at=1000.0,
        )

        d = payload.to_dict()
        restored = SinglePovPayload.from_dict(d)

        assert restored.experiment_name == "exp-1"
        assert restored.trial_id == "trial-0"
        assert restored.benchmark == "test-bench"
        assert restored.harness == "fuzz_target"
        assert restored.pov.pov_id == "pov_0"
        assert restored.pov.to_bytes() == b"crash_input"
        assert restored.enqueued_at == 1000.0

    def test_single_pov_not_list(self) -> None:
        """SinglePovPayload has a single pov, not a list."""
        pov = EmbeddedPov.from_bytes("pov_0", b"data")
        payload = SinglePovPayload(
            experiment_name="exp",
            trial_id="t",
            benchmark="b",
            harness="h",
            pov=pov,
            enqueued_at=0.0,
        )
        d = payload.to_dict()
        assert isinstance(d["pov"], dict)
        assert "pov_id" in d["pov"]


class TestPovVerdictStatus:
    """PovVerdict status field serialization."""

    def test_status_default(self) -> None:
        """status defaults to 'not_vulnerable'."""
        verdict = PovVerdict(pov_id="pov_0", triggered_bug=False)
        assert verdict.status == "not_vulnerable"

    def test_status_roundtrip(self) -> None:
        """status survives to_dict/from_dict roundtrip."""
        verdict = PovVerdict(
            pov_id="pov_0",
            triggered_bug=False,
            status="unintended_crash",
        )
        d = verdict.to_dict()
        assert d["status"] == "unintended_crash"

        restored = PovVerdict.from_dict(d)
        assert restored.status == "unintended_crash"

    def test_status_missing_in_dict(self) -> None:
        """from_dict handles missing status (backward compat)."""
        d = {"pov_id": "pov_0", "triggered_bug": False}
        verdict = PovVerdict.from_dict(d)
        assert verdict.status == "not_vulnerable"


class TestPovVerdictCrashLogs:
    """PovVerdict crash_logs field serialization."""

    def test_crash_logs_default_empty(self) -> None:
        """crash_logs defaults to empty dict."""
        verdict = PovVerdict(pov_id="pov_0", triggered_bug=False)
        assert verdict.crash_logs == {}

    def test_crash_logs_roundtrip(self) -> None:
        """crash_logs survives to_dict/from_dict roundtrip."""
        logs = {"base-asan": "ASAN error at ...", "patched-asan": "no crash"}
        verdict = PovVerdict(
            pov_id="pov_0",
            triggered_bug=True,
            cpv_matches=["cpv_0"],
            crash_logs=logs,
        )
        d = verdict.to_dict()
        assert d["crash_logs"] == logs

        restored = PovVerdict.from_dict(d)
        assert restored.crash_logs == logs

    def test_crash_logs_missing_in_dict(self) -> None:
        """from_dict handles missing crash_logs (backward compat)."""
        d = {"pov_id": "pov_0", "triggered_bug": False}
        verdict = PovVerdict.from_dict(d)
        assert verdict.crash_logs == {}


class TestSinglePovResult:
    """SinglePovResult serialization."""

    def test_to_dict_roundtrip(self) -> None:
        verdict = PovVerdict(
            pov_id="pov_0",
            triggered_bug=True,
            cpv_matches=["cpv_0"],
        )
        result = SinglePovResult(
            trial_id="trial-0",
            benchmark="test-bench",
            harness="fuzz_target",
            verdict=verdict,
            completed_at=2000.0,
        )

        d = result.to_dict()
        restored = SinglePovResult.from_dict(d)

        assert restored.trial_id == "trial-0"
        assert restored.verdict.pov_id == "pov_0"
        assert restored.verdict.triggered_bug is True
        assert restored.verdict.cpv_matches == ["cpv_0"]
        assert restored.completed_at == 2000.0


class TestVerifySinglePov:
    """verify_single_pov() RQ job function."""

    def _make_payload(self, pov_data: bytes = b"crash") -> dict:
        pov = EmbeddedPov.from_bytes("pov_0", pov_data)
        return SinglePovPayload(
            experiment_name="exp",
            trial_id="trial-0",
            benchmark="test-bench",
            harness="fuzz_target",
            pov=pov,
            enqueued_at=time.time(),
        ).to_dict()

    @patch("crsbench.distributed.evaluator_jobs._built_results", {})
    def test_missing_benchmark_returns_error(self) -> None:
        """Returns error verdict when benchmark not in build cache."""
        result_dict = verify_single_pov(self._make_payload())
        result = SinglePovResult.from_dict(result_dict)

        assert result.verdict.pov_id == "pov_0"
        assert result.verdict.triggered_bug is False
        assert result.verdict.status == "error"
        assert result.verdict.error is not None
        assert "No built variants" in result.verdict.error

    @patch("crsbench.distributed.evaluator_jobs._verification_engine", None)
    @patch(
        "crsbench.distributed.evaluator_jobs._built_results",
        {"test-bench": {"v": "result"}},
    )
    def test_engine_not_initialized(self) -> None:
        """Returns error when VerificationEngine is not initialized."""
        result_dict = verify_single_pov(self._make_payload())
        result = SinglePovResult.from_dict(result_dict)

        assert result.verdict.triggered_bug is False
        assert result.verdict.status == "error"
        assert "not initialized" in result.verdict.error

    @patch("crsbench.distributed.evaluator_jobs._built_results", {"test-bench": {}})
    def test_adapter_load_failure(self) -> None:
        """Returns error when adapter fails to load."""
        mock_engine = MagicMock()
        mock_engine.load_adapter.return_value = None

        with patch(
            "crsbench.distributed.evaluator_jobs._verification_engine", mock_engine
        ):
            result_dict = verify_single_pov(self._make_payload())

        result = SinglePovResult.from_dict(result_dict)
        assert result.verdict.triggered_bug is False
        assert result.verdict.status == "error"
        assert "Failed to load adapter" in result.verdict.error

    @patch("crsbench.distributed.evaluator_jobs._built_results", {"test-bench": {}})
    def test_successful_cpv_match(self) -> None:
        """Successful verification with CPV match."""
        from crsbench.evaluation.verification.models import (
            PovVerificationResult,
            PovVerificationStatus,
        )

        mock_engine = MagicMock()
        mock_adapter = MagicMock()
        mock_engine.load_adapter.return_value = mock_adapter
        mock_engine.verify_pov.return_value = PovVerificationResult(
            status=PovVerificationStatus.CPV,
            benchmark="test-bench",
            cpv_matched=["cpv_0"],
        )

        with patch(
            "crsbench.distributed.evaluator_jobs._verification_engine", mock_engine
        ):
            result_dict = verify_single_pov(self._make_payload())

        result = SinglePovResult.from_dict(result_dict)
        assert result.verdict.triggered_bug is True
        assert result.verdict.status == "cpv"
        assert result.verdict.cpv_matches == ["cpv_0"]
        assert result.verdict.error is None

    @patch("crsbench.distributed.evaluator_jobs._built_results", {"test-bench": {}})
    def test_cpv_match_includes_crash_logs(self) -> None:
        """Crash logs from engine result are propagated to verdict."""
        from crsbench.evaluation.verification.models import (
            PovVerificationResult,
            PovVerificationStatus,
        )

        mock_engine = MagicMock()
        mock_adapter = MagicMock()
        mock_engine.load_adapter.return_value = mock_adapter
        mock_engine.verify_pov.return_value = PovVerificationResult(
            status=PovVerificationStatus.CPV,
            benchmark="test-bench",
            cpv_matched=["cpv_0"],
            crash_info={
                "stdout": {
                    "base-asan": "ASAN: heap-buffer-overflow",
                    "patched-asan": "no crash",
                },
                "other_key": "ignored_by_our_code",
            },
        )

        with patch(
            "crsbench.distributed.evaluator_jobs._verification_engine", mock_engine
        ):
            result_dict = verify_single_pov(self._make_payload())

        result = SinglePovResult.from_dict(result_dict)
        assert result.verdict.triggered_bug is True
        assert result.verdict.crash_logs == {
            "base-asan": "ASAN: heap-buffer-overflow",
            "patched-asan": "no crash",
        }

    @patch("crsbench.distributed.evaluator_jobs._built_results", {"test-bench": {}})
    def test_no_crash_info_gives_empty_crash_logs(self) -> None:
        """When crash_info is None, crash_logs should be empty."""
        from crsbench.evaluation.verification.models import (
            PovVerificationResult,
            PovVerificationStatus,
        )

        mock_engine = MagicMock()
        mock_adapter = MagicMock()
        mock_engine.load_adapter.return_value = mock_adapter
        mock_engine.verify_pov.return_value = PovVerificationResult(
            status=PovVerificationStatus.CPV,
            benchmark="test-bench",
            cpv_matched=["cpv_0"],
            crash_info=None,
        )

        with patch(
            "crsbench.distributed.evaluator_jobs._verification_engine", mock_engine
        ):
            result_dict = verify_single_pov(self._make_payload())

        result = SinglePovResult.from_dict(result_dict)
        assert result.verdict.crash_logs == {}

    @patch("crsbench.distributed.evaluator_jobs._built_results", {"test-bench": {}})
    def test_not_vulnerable(self) -> None:
        """POV does not trigger vulnerability."""
        from crsbench.evaluation.verification.models import (
            PovVerificationResult,
            PovVerificationStatus,
        )

        mock_engine = MagicMock()
        mock_adapter = MagicMock()
        mock_engine.load_adapter.return_value = mock_adapter
        mock_engine.verify_pov.return_value = PovVerificationResult(
            status=PovVerificationStatus.NOT_VULNERABLE,
            benchmark="test-bench",
            cpv_matched=[],
        )

        with patch(
            "crsbench.distributed.evaluator_jobs._verification_engine", mock_engine
        ):
            result_dict = verify_single_pov(self._make_payload())

        result = SinglePovResult.from_dict(result_dict)
        assert result.verdict.triggered_bug is False
        assert result.verdict.status == "not_vulnerable"
        assert result.verdict.cpv_matches == []

    @patch("crsbench.distributed.evaluator_jobs._built_results", {"test-bench": {}})
    def test_unintended_crash_status(self) -> None:
        """UNINTENDED_CRASH is correctly reflected in verdict status."""
        from crsbench.evaluation.verification.models import (
            PovVerificationResult,
            PovVerificationStatus,
        )

        mock_engine = MagicMock()
        mock_adapter = MagicMock()
        mock_engine.load_adapter.return_value = mock_adapter
        mock_engine.verify_pov.return_value = PovVerificationResult(
            status=PovVerificationStatus.UNINTENDED_CRASH,
            benchmark="test-bench",
            cpv_matched=[],
        )

        with patch(
            "crsbench.distributed.evaluator_jobs._verification_engine", mock_engine
        ):
            result_dict = verify_single_pov(self._make_payload())

        result = SinglePovResult.from_dict(result_dict)
        assert result.verdict.triggered_bug is False
        assert result.verdict.status == "unintended_crash"
        assert result.verdict.cpv_matches == []
        assert result.verdict.error is None

    @patch("crsbench.distributed.evaluator_jobs._built_results", {"test-bench": {}})
    def test_verification_exception(self) -> None:
        """Exception during verification produces error verdict."""
        mock_engine = MagicMock()
        mock_adapter = MagicMock()
        mock_engine.load_adapter.return_value = mock_adapter
        mock_engine.verify_pov.side_effect = RuntimeError("Docker timeout")

        with patch(
            "crsbench.distributed.evaluator_jobs._verification_engine", mock_engine
        ):
            result_dict = verify_single_pov(self._make_payload())

        result = SinglePovResult.from_dict(result_dict)
        assert result.verdict.triggered_bug is False
        assert result.verdict.status == "error"
        assert "Docker timeout" in result.verdict.error


class TestLazyBuildCache:
    """Test lazy build cache fallback in verify functions."""

    def _make_payload(self, pov_data: bytes = b"crash") -> dict:
        pov = EmbeddedPov.from_bytes("pov_0", pov_data)
        return SinglePovPayload(
            experiment_name="exp",
            trial_id="trial-0",
            benchmark="lazy-bench",
            harness="fuzz_target",
            pov=pov,
            enqueued_at=time.time(),
        ).to_dict()

    @patch("crsbench.distributed.evaluator_jobs._built_results", {})
    def test_lazy_load_succeeds(self) -> None:
        """Lazy load populates cache when engine can load adapter."""
        from crsbench.evaluation.verification.models import (
            PovVerificationResult,
            PovVerificationStatus,
        )

        mock_engine = MagicMock()
        mock_adapter = MagicMock()
        mock_engine.load_adapter.return_value = mock_adapter
        mock_engine.get_or_build_results.return_value = {"variant-1": MagicMock()}
        mock_engine.verify_pov.return_value = PovVerificationResult(
            status=PovVerificationStatus.CPV,
            benchmark="lazy-bench",
            cpv_matched=["cpv_0"],
        )

        mock_benchmark_path = MagicMock()
        mock_benchmark_path.exists.return_value = True

        with (
            patch(
                "crsbench.distributed.evaluator_jobs._verification_engine",
                mock_engine,
            ),
            patch(
                "crsbench.distributed.evaluator_jobs.resolve_benchmark_path",
                return_value=mock_benchmark_path,
            ),
        ):
            result_dict = verify_single_pov(self._make_payload())

        result = SinglePovResult.from_dict(result_dict)
        # Should succeed via lazy load (not return "No built variants" error)
        assert result.verdict.triggered_bug is True
        assert result.verdict.cpv_matches == ["cpv_0"]

    @patch("crsbench.distributed.evaluator_jobs._built_results", {})
    def test_lazy_load_fails_gracefully(self) -> None:
        """Lazy load failure still returns proper error."""
        mock_engine = MagicMock()
        mock_engine.load_adapter.return_value = None  # Adapter not found

        mock_benchmark_path = MagicMock()
        mock_benchmark_path.exists.return_value = True

        with (
            patch(
                "crsbench.distributed.evaluator_jobs._verification_engine",
                mock_engine,
            ),
            patch(
                "crsbench.distributed.evaluator_jobs.resolve_benchmark_path",
                return_value=mock_benchmark_path,
            ),
        ):
            result_dict = verify_single_pov(self._make_payload())

        result = SinglePovResult.from_dict(result_dict)
        assert result.verdict.triggered_bug is False
        assert "No built variants" in result.verdict.error
