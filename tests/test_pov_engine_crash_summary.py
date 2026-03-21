import threading
import time
from unittest.mock import MagicMock, patch

from crsbench.builder import VariantType
from crsbench.evaluation.verification.models import (
    PovVerificationRequest,
    PovVerificationResult,
    PovVerificationStatus,
)
from crsbench.evaluation.verification.pov.engine import VerificationEngine


def test_extract_crash_summary_matches_pid_prefixed_asan_error():
    engine = object.__new__(VerificationEngine)
    crash_log = (
        "PREAMBLE0\n"
        "PREAMBLE1\n"
        "PREAMBLE2\n"
        "PREAMBLE3\n"
        "PREAMBLE4\n"
        "==12345==ERROR: AddressSanitizer: heap-buffer-overflow on address 0x0\n"
        "READ of size 1\n"
        "SUMMARY: AddressSanitizer: heap-buffer-overflow /src/a.c:10:5\n"
    )
    summary = engine._extract_crash_summary(crash_log, max_lines=20)
    assert any("ERROR: AddressSanitizer" in line for line in summary)
    assert any(line.startswith("SUMMARY: AddressSanitizer") for line in summary)
    # PID-prefixed sanitizer branch should not include the full preamble region.
    assert not any("PREAMBLE0" in line for line in summary)


def test_extract_crash_summary_ignores_non_sanitizer_summary_lines():
    engine = object.__new__(VerificationEngine)
    crash_log = (
        "SUMMARY: coverage corpus stats\n"
        "more coverage output\n"
        "==54321==ERROR: AddressSanitizer: heap-use-after-free\n"
        "WRITE of size 8\n"
        "SUMMARY: AddressSanitizer: heap-use-after-free /src/uaf.c:12:3\n"
    )
    summary = engine._extract_crash_summary(crash_log, max_lines=20)
    assert not any("coverage corpus stats" in line for line in summary)
    assert any("ERROR: AddressSanitizer" in line for line in summary)


def test_verify_pov_preserves_stderr_only_crash_output():
    engine = object.__new__(VerificationEngine)

    adapter = MagicMock()
    adapter.benchmark_name = "test-benchmark"
    adapter.get_mode.return_value = MagicMock(value="full")

    build_result = MagicMock()
    build_result.success = True
    build_result.config.variant_type = VariantType.FULL_BASE
    build_result.config.cpv_num = None

    request = PovVerificationRequest(
        pov_data=b"boom",
        harness="test_harness",
        benchmark="test-benchmark",
        pov_id="pov_0",
    )

    with (
        patch(
            "crsbench.evaluation.verification.pov.engine.VerdictResolver.resolve",
            return_value=PovVerificationResult(
                status=PovVerificationStatus.UNINTENDED_CRASH,
                benchmark="test-benchmark",
                pov_id="pov_0",
            ),
        ),
        patch.object(engine, "_execute_reproduce") as mock_execute,
    ):
        mock_execute.return_value = MagicMock(
            variant_name="base-asan",
            variant_type=VariantType.FULL_BASE,
            cpv_num=None,
            crashed=True,
            crash_log="",
            stderr="==1==ERROR: AddressSanitizer: heap-buffer-overflow",
        )

        result = engine.verify_pov(
            request=request,
            adapter=adapter,
            build_results={"base-asan": build_result},
        )

    assert result.crash_info is not None
    assert (
        result.crash_info["stdout"]["base-asan"]
        == "==1==ERROR: AddressSanitizer: heap-buffer-overflow"
    )
    assert (
        result.crash_info["stderr"]["base-asan"]
        == "==1==ERROR: AddressSanitizer: heap-buffer-overflow"
    )


def test_verify_povs_parallel_preserves_stderr_only_crash_output():
    engine = object.__new__(VerificationEngine)

    adapter = MagicMock()
    adapter.benchmark_name = "test-benchmark"
    adapter.get_mode.return_value = MagicMock(value="full")

    build_result = MagicMock()
    build_result.success = True
    build_result.config.variant_type = VariantType.FULL_BASE
    build_result.config.cpv_num = None

    with (
        patch(
            "crsbench.evaluation.verification.pov.engine.VerdictResolver.resolve",
            return_value=PovVerificationResult(
                status=PovVerificationStatus.UNINTENDED_CRASH,
                benchmark="test-benchmark",
                pov_id="pov_0",
            ),
        ),
        patch.object(engine, "_execute_reproduce") as mock_execute,
    ):
        mock_execute.return_value = MagicMock(
            variant_name="base-asan",
            variant_type=VariantType.FULL_BASE,
            cpv_num=None,
            crashed=True,
            crash_log="",
            stderr="==2==ERROR: AddressSanitizer: stack-overflow",
            pov_id="pov_0",
            harness="test_harness",
        )

        results = engine.verify_povs_parallel(
            pov_harness_pairs=[("pov_0", b"boom", "test_harness")],
            adapter=adapter,
            build_results={"base-asan": build_result},
        )

    assert len(results) == 1
    crash_info = results[0].crash_info
    assert crash_info is not None
    assert (
        crash_info["stdout"]["base-asan"]
        == "==2==ERROR: AddressSanitizer: stack-overflow"
    )
    assert (
        crash_info["stderr"]["base-asan"]
        == "==2==ERROR: AddressSanitizer: stack-overflow"
    )


def test_verify_povs_parallel_uses_verify_workers_for_reproduce_calls():
    engine = object.__new__(VerificationEngine)
    engine.verify_workers = 2

    adapter = MagicMock()
    adapter.benchmark_name = "test-benchmark"
    adapter.get_mode.return_value = MagicMock(value="full")

    base_result = MagicMock()
    base_result.success = True
    base_result.config.variant_type = VariantType.FULL_BASE
    base_result.config.cpv_num = None

    patched_result = MagicMock()
    patched_result.success = True
    patched_result.config.variant_type = VariantType.ALL_PATCHED
    patched_result.config.cpv_num = None

    lock = threading.Lock()
    active = 0
    max_active = 0

    def _fake_execute(task):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.05)
        with lock:
            active -= 1
        return MagicMock(
            variant_name=task.variant_name,
            variant_type=task.variant_type,
            cpv_num=task.cpv_num,
            crashed=False,
            crash_log="",
            stderr="",
            pov_id=task.pov_id,
            harness=task.harness,
        )

    with (
        patch.object(engine, "_execute_reproduce", side_effect=_fake_execute),
        patch(
            "crsbench.evaluation.verification.pov.engine.VerdictResolver.resolve",
            return_value=PovVerificationResult(
                status=PovVerificationStatus.NOT_VULNERABLE,
                benchmark="test-benchmark",
                pov_id="pov_0",
            ),
        ),
    ):
        results = engine.verify_povs_parallel(
            pov_harness_pairs=[("pov_0", b"boom", "test_harness")],
            adapter=adapter,
            build_results={
                "base-asan": base_result,
                "allpatched-asan": patched_result,
            },
        )

    assert len(results) == 1
    assert max_active >= 2
