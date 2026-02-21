"""Unit tests for StackBasedDedup deduplication strategy.

Tests for the stack-based deduplication in crsbench/evaluation/verification/dedup.py.
"""

from crsbench.evaluation.verification.dedup import StackBasedDedup, get_dedup_strategy
from crsbench.evaluation.verification.models import (
    PovVerificationResult,
    PovVerificationStatus,
)


def _make_result(
    status: PovVerificationStatus,
    cpv_matched: list[str] | None = None,
    crash_log: str | None = None,
    pov_id: str = "pov",
) -> PovVerificationResult:
    """Helper to create a PovVerificationResult with optional crash info."""
    crash_info = None
    if crash_log:
        crash_info = {"stdout": {"variant-a": crash_log}}
    return PovVerificationResult(
        status=status,
        benchmark="test-bench",
        cpv_matched=cpv_matched or [],
        pov_id=pov_id,
        crash_info=crash_info,
    )


# Sample crash logs producing different signatures
CRASH_LOG_A = """\
==12345==ERROR: AddressSanitizer: heap-buffer-overflow on address 0x602000000210
    #0 0x55f8a1 in func_a /src/a.c:10:5
    #1 0x55f9b2 in main /src/main.c:5:3
SUMMARY: AddressSanitizer: heap-buffer-overflow /src/a.c:10:5
"""

CRASH_LOG_B = """\
==12345==ERROR: AddressSanitizer: use-after-free on address 0x602000000310
    #0 0x55f8a1 in func_b /src/b.c:20:5
    #1 0x55f9b2 in main /src/main.c:5:3
SUMMARY: AddressSanitizer: use-after-free /src/b.c:20:5
"""


class TestStackBasedDedupUnintendedCrashes:
    """Tests for UNINTENDED_CRASH deduplication by crash signature."""

    def test_same_signature_deduplicated(self) -> None:
        """Test that UNINTENDED_CRASH results with same crash signature are deduped."""
        dedup = StackBasedDedup()
        results = [
            _make_result(
                PovVerificationStatus.UNINTENDED_CRASH,
                crash_log=CRASH_LOG_A,
                pov_id="pov1",
            ),
            _make_result(
                PovVerificationStatus.UNINTENDED_CRASH,
                crash_log=CRASH_LOG_A,
                pov_id="pov2",
            ),
            _make_result(
                PovVerificationStatus.UNINTENDED_CRASH,
                crash_log=CRASH_LOG_A,
                pov_id="pov3",
            ),
        ]

        deduped = dedup.deduplicate(results)

        assert len(deduped) == 1
        assert deduped[0].pov_id == "pov1"

    def test_different_signatures_kept(self) -> None:
        """Test that UNINTENDED_CRASH results with different signatures are kept."""
        dedup = StackBasedDedup()
        results = [
            _make_result(
                PovVerificationStatus.UNINTENDED_CRASH,
                crash_log=CRASH_LOG_A,
                pov_id="pov1",
            ),
            _make_result(
                PovVerificationStatus.UNINTENDED_CRASH,
                crash_log=CRASH_LOG_B,
                pov_id="pov2",
            ),
        ]

        deduped = dedup.deduplicate(results)

        assert len(deduped) == 2

    def test_no_crash_log_kept(self) -> None:
        """Test that UNINTENDED_CRASH without crash log is kept."""
        dedup = StackBasedDedup()
        results = [
            _make_result(PovVerificationStatus.UNINTENDED_CRASH, pov_id="pov1"),
        ]

        deduped = dedup.deduplicate(results)

        assert len(deduped) == 1


class TestStackBasedDedupCpvResults:
    """Tests for CPV result deduplication (same as PatchBasedDedup)."""

    def test_cpv_dedup_by_cpv_matched(self) -> None:
        """Test that CPV results still dedup by cpv_matched set."""
        dedup = StackBasedDedup()
        results = [
            _make_result(
                PovVerificationStatus.CPV, cpv_matched=["cpv_0"], pov_id="pov1"
            ),
            _make_result(
                PovVerificationStatus.CPV, cpv_matched=["cpv_0"], pov_id="pov2"
            ),
            _make_result(
                PovVerificationStatus.CPV, cpv_matched=["cpv_1"], pov_id="pov3"
            ),
        ]

        deduped = dedup.deduplicate(results)

        assert len(deduped) == 2
        cpv_sets = [tuple(sorted(r.cpv_matched)) for r in deduped]
        assert ("cpv_0",) in cpv_sets
        assert ("cpv_1",) in cpv_sets


class TestStackBasedDedupMixedStatuses:
    """Tests for mixed status handling."""

    def test_mixed_statuses(self) -> None:
        """Test that mixed statuses are handled correctly."""
        dedup = StackBasedDedup()
        results = [
            _make_result(
                PovVerificationStatus.CPV, cpv_matched=["cpv_0"], pov_id="pov1"
            ),
            _make_result(
                PovVerificationStatus.UNINTENDED_CRASH,
                crash_log=CRASH_LOG_A,
                pov_id="pov2",
            ),
            _make_result(
                PovVerificationStatus.UNINTENDED_CRASH,
                crash_log=CRASH_LOG_A,
                pov_id="pov3",
            ),
            _make_result(PovVerificationStatus.NOT_VULNERABLE, pov_id="pov4"),
            _make_result(PovVerificationStatus.NOT_VULNERABLE, pov_id="pov5"),
            _make_result(
                PovVerificationStatus.UNINTENDED_CRASH,
                crash_log=CRASH_LOG_B,
                pov_id="pov6",
            ),
        ]

        deduped = dedup.deduplicate(results)

        # 1 CPV + 1 first CRASH_LOG_A + 2 NOT_VULNERABLE + 1 CRASH_LOG_B = 5
        assert len(deduped) == 5
        statuses = [r.status for r in deduped]
        assert statuses.count(PovVerificationStatus.UNINTENDED_CRASH) == 2
        assert statuses.count(PovVerificationStatus.CPV) == 1
        assert statuses.count(PovVerificationStatus.NOT_VULNERABLE) == 2


class TestStackBasedDedupPreComputedSignature:
    """Tests for dedup using pre-computed crash_signature field."""

    def test_uses_precomputed_signature(self) -> None:
        """Test that dedup uses result.crash_signature before reparsing logs."""
        dedup = StackBasedDedup()
        # Results with pre-computed signature but no crash_info
        r1 = PovVerificationResult(
            status=PovVerificationStatus.UNINTENDED_CRASH,
            benchmark="test-bench",
            pov_id="pov1",
            crash_signature="abcdef1234567890",
        )
        r2 = PovVerificationResult(
            status=PovVerificationStatus.UNINTENDED_CRASH,
            benchmark="test-bench",
            pov_id="pov2",
            crash_signature="abcdef1234567890",
        )
        r3 = PovVerificationResult(
            status=PovVerificationStatus.UNINTENDED_CRASH,
            benchmark="test-bench",
            pov_id="pov3",
            crash_signature="different_hash__",
        )

        deduped = dedup.deduplicate([r1, r2, r3])

        assert len(deduped) == 2
        assert deduped[0].pov_id == "pov1"
        assert deduped[1].pov_id == "pov3"


class TestResultSerialization:
    """Tests for crash_signature in PovVerificationResult.to_dict()."""

    def test_to_dict_includes_crash_signature(self) -> None:
        """Test that to_dict() includes crash_signature when present."""
        result = PovVerificationResult(
            status=PovVerificationStatus.UNINTENDED_CRASH,
            benchmark="test-bench",
            pov_id="pov1",
            crash_signature="abcdef1234567890",
        )
        d = result.to_dict()

        assert "crash_signature" in d
        assert d["crash_signature"] == "abcdef1234567890"

    def test_to_dict_omits_none_crash_signature(self) -> None:
        """Test that to_dict() omits crash_signature when None."""
        result = PovVerificationResult(
            status=PovVerificationStatus.NOT_VULNERABLE,
            benchmark="test-bench",
        )
        d = result.to_dict()

        assert "crash_signature" not in d


class TestStackBasedDedupTopN:
    """Tests for top_n parameter on StackBasedDedup."""

    def test_top_n_stored(self) -> None:
        """Test that top_n is stored on the instance."""
        dedup = StackBasedDedup(top_n=3)
        assert dedup._top_n == 3

    def test_default_top_n(self) -> None:
        """Test that default top_n is 5."""
        dedup = StackBasedDedup()
        assert dedup._top_n == 5

    def test_invalid_top_n_raises(self) -> None:
        """top_n must be strictly positive."""
        import pytest

        with pytest.raises(ValueError, match="top_n must be > 0"):
            StackBasedDedup(top_n=0)

    def test_top_n_affects_signature(self) -> None:
        """Test that different top_n values produce different signatures."""
        # Build a crash log with many frames
        crash_log = (
            "==1==ERROR: AddressSanitizer: heap-buffer-overflow on 0x1\n"
            "    #0 0xaaa in f0 /a.c:1:1\n"
            "    #1 0xbbb in f1 /b.c:2:1\n"
            "    #2 0xccc in f2 /c.c:3:1\n"
            "    #3 0xddd in f3 /d.c:4:1\n"
            "    #4 0xeee in f4 /e.c:5:1\n"
        )
        r1 = _make_result(
            PovVerificationStatus.UNINTENDED_CRASH,
            crash_log=crash_log,
            pov_id="pov1",
        )
        # A log that only differs in frame #2+
        crash_log_b = (
            "==1==ERROR: AddressSanitizer: heap-buffer-overflow on 0x1\n"
            "    #0 0xaaa in f0 /a.c:1:1\n"
            "    #1 0xbbb in f1 /b.c:2:1\n"
            "    #2 0xccc in DIFFERENT /z.c:99:1\n"
            "    #3 0xddd in f3 /d.c:4:1\n"
            "    #4 0xeee in f4 /e.c:5:1\n"
        )
        r2 = _make_result(
            PovVerificationStatus.UNINTENDED_CRASH,
            crash_log=crash_log_b,
            pov_id="pov2",
        )

        # With top_n=2, only frames 0-1 are used → same sig → deduped
        dedup2 = StackBasedDedup(top_n=2)
        deduped = dedup2.deduplicate([r1, r2])
        assert len(deduped) == 1

        # With top_n=5, frame #2 differs → different sig → both kept
        dedup5 = StackBasedDedup(top_n=5)
        deduped = dedup5.deduplicate([r1, r2])
        assert len(deduped) == 2


class TestGetDedupStrategy:
    """Tests for get_dedup_strategy registration."""

    def test_stack_based_registered(self) -> None:
        """Test that 'stack-based' strategy is registered."""
        strategy = get_dedup_strategy("stack-based")

        assert isinstance(strategy, StackBasedDedup)
        assert strategy.name == "stack-based"

    def test_stack_based_top_n_passthrough(self) -> None:
        """Test that get_dedup_strategy passes top_n to StackBasedDedup."""
        strategy = get_dedup_strategy("stack-based", top_n=3)

        assert isinstance(strategy, StackBasedDedup)
        assert strategy._top_n == 3

    def test_non_stack_ignores_top_n(self) -> None:
        """Test that non-stack strategies ignore top_n."""
        strategy = get_dedup_strategy("patch-based", top_n=3)

        assert strategy.name == "patch-based"
