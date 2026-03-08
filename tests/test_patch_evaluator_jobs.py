"""Unit tests for distributed patch evaluator jobs."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from crsbench.builder.types import BenchmarkMode
from crsbench.distributed.patch_evaluator_jobs import (
    EmbeddedPatch,
    PatchJobPayload,
    _resolve_patch_variant_name,
)


def _make_payload(*, sanitizer: str = "address") -> PatchJobPayload:
    return PatchJobPayload(
        experiment_name="exp",
        trial_id="trial",
        benchmark="bench",
        harness="h0",
        cpv_id="cpv_0",
        patch=EmbeddedPatch(
            patch_id="patch_0",
            pov_id="cpv_0",
            patch_content_b64="",
        ),
        sanitizer=sanitizer,
        source_mode="pkgs",
        verify_variants=False,
        use_inc_build=True,
    )


def test_resolve_patch_variant_name_prefers_adapter_cpv_sanitizer() -> None:
    """Cleanup variant resolution should use CPV sanitizer over payload default."""
    payload = _make_payload(sanitizer="address")
    benchmark_path = Path("/bench")

    adapter = MagicMock()
    adapter.get_cpv_sanitizer.return_value = "undefined"
    adapter.get_mode.return_value = BenchmarkMode.DELTA
    adapter.lang = "c"
    adapter.get_ref_commit.return_value = "abc123"
    adapter.get_base_commit.return_value = "abc123"
    adapter.main_repo = "https://example.com/repo"

    with (
        patch("crsbench.utils.run_helper.ensure_oss_fuzz_root", return_value="/tmp/of"),
        patch(
            "crsbench.evaluation.verification.pov.VerificationEngine"
        ) as mock_engine_cls,
    ):
        mock_engine = MagicMock()
        mock_engine.load_adapter.return_value = adapter
        mock_engine_cls.return_value = mock_engine
        variant = _resolve_patch_variant_name(payload, benchmark_path)

    assert variant is not None
    assert "-ubsan-" in variant


def test_resolve_patch_variant_name_falls_back_to_payload_sanitizer() -> None:
    """When CPV sanitizer is unavailable, payload sanitizer should be used."""
    payload = _make_payload(sanitizer="address")
    benchmark_path = Path("/bench")

    adapter = MagicMock()
    adapter.get_cpv_sanitizer.return_value = ""
    adapter.get_mode.return_value = BenchmarkMode.DELTA
    adapter.lang = "c"
    adapter.get_ref_commit.return_value = "abc123"
    adapter.get_base_commit.return_value = "abc123"
    adapter.main_repo = "https://example.com/repo"

    with (
        patch("crsbench.utils.run_helper.ensure_oss_fuzz_root", return_value="/tmp/of"),
        patch(
            "crsbench.evaluation.verification.pov.VerificationEngine"
        ) as mock_engine_cls,
    ):
        mock_engine = MagicMock()
        mock_engine.load_adapter.return_value = adapter
        mock_engine_cls.return_value = mock_engine
        variant = _resolve_patch_variant_name(payload, benchmark_path)

    assert variant is not None
    assert "-asan-" in variant


def test_resolve_patch_variant_name_handles_cpv_sanitizer_error() -> None:
    """CPV sanitizer lookup errors should fall back to payload sanitizer."""
    payload = _make_payload(sanitizer="address")
    benchmark_path = Path("/bench")

    adapter = MagicMock()
    adapter.get_cpv_sanitizer.side_effect = RuntimeError("lookup failed")
    adapter.get_mode.return_value = BenchmarkMode.DELTA
    adapter.lang = "c"
    adapter.get_ref_commit.return_value = "abc123"
    adapter.get_base_commit.return_value = "abc123"
    adapter.main_repo = "https://example.com/repo"

    with (
        patch("crsbench.utils.run_helper.ensure_oss_fuzz_root", return_value="/tmp/of"),
        patch(
            "crsbench.evaluation.verification.pov.VerificationEngine"
        ) as mock_engine_cls,
    ):
        mock_engine = MagicMock()
        mock_engine.load_adapter.return_value = adapter
        mock_engine_cls.return_value = mock_engine
        variant = _resolve_patch_variant_name(payload, benchmark_path)

    assert variant is not None
    assert "-asan-" in variant
