"""Unit tests for trial patch discovery in BenchmarkRunner."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from crsbench.evaluation.runner import BenchmarkRunner, PatchDiscoveryError


def _make_runner(mode: str = "bug-fixing") -> BenchmarkRunner:
    adapter = MagicMock()
    adapter.mode = mode
    return BenchmarkRunner(adapter=adapter, snapshot_period=0)


def test_discover_trial_patches_flat_with_target(tmp_path: Path) -> None:
    patch_dir = tmp_path / "output" / "patches"
    patch_dir.mkdir(parents=True)
    (patch_dir / "a.diff").write_text("diff-a")
    (patch_dir / "b.diff").write_text("diff-b")

    runner = _make_runner("bug-fixing")
    discovered = runner._discover_trial_patches_for_verification(
        patch_dir, target_cpv_id="cpv_7"
    )

    assert [cpv for cpv, _, _ in discovered] == ["cpv_7", "cpv_7"]
    assert discovered[0][1].startswith("flat_a_")
    assert discovered[1][1].startswith("flat_b_")
    assert discovered[0][1] != discovered[1][1]
    assert [p.name for _, _, p in discovered] == ["a.diff", "b.diff"]


def test_discover_trial_patches_flat_requires_target_for_bugfix(
    tmp_path: Path,
) -> None:
    patch_dir = tmp_path / "output" / "patches"
    patch_dir.mkdir(parents=True)
    (patch_dir / "a.diff").write_text("diff-a")

    runner = _make_runner("bug-fixing")
    with pytest.raises(PatchDiscoveryError, match="target_cpv_id"):
        runner._discover_trial_patches_for_verification(patch_dir, target_cpv_id=None)


def test_discover_trial_patches_structured_layout(tmp_path: Path) -> None:
    patch_dir = tmp_path / "output" / "patches"
    cpv0 = patch_dir / "cpv_0"
    cpv1 = patch_dir / "cpv_1"
    cpv0.mkdir(parents=True)
    cpv1.mkdir(parents=True)
    (cpv0 / "a.diff").write_text("diff-a")
    (cpv0 / "b.diff").write_text("diff-b")
    (cpv1 / "c.diff").write_text("diff-c")

    runner = _make_runner("bug-fixing")
    discovered = runner._discover_trial_patches_for_verification(
        patch_dir, target_cpv_id="cpv_unused"
    )

    assert [cpv for cpv, _, _ in discovered] == ["cpv_0", "cpv_0", "cpv_1"]
    patch_ids = [patch_id for _, patch_id, _ in discovered]
    assert patch_ids[0].startswith("structured_a_")
    assert patch_ids[1].startswith("structured_b_")
    assert patch_ids[2].startswith("structured_c_")
    assert len(set(patch_ids)) == 3
    assert [p.name for _, _, p in discovered] == ["a.diff", "b.diff", "c.diff"]


def test_discover_trial_patches_mixed_layout_avoids_identity_collisions(
    tmp_path: Path,
) -> None:
    patch_dir = tmp_path / "output" / "patches"
    cpv0 = patch_dir / "cpv_0"
    cpv0.mkdir(parents=True)
    (cpv0 / "patch_0.diff").write_text("structured")
    (patch_dir / "patch_0.diff").write_text("flat")

    runner = _make_runner("bug-fixing")
    discovered = runner._discover_trial_patches_for_verification(
        patch_dir, target_cpv_id="cpv_0"
    )

    identities = [(cpv, patch_id) for cpv, patch_id, _ in discovered]
    assert len(identities) == 2
    assert len(set(identities)) == 2


def test_find_trial_pov_for_cpv_prefers_direct_file(tmp_path: Path) -> None:
    pov_dir = tmp_path / "crs-input" / "povs"
    pov_dir.mkdir(parents=True)
    direct = pov_dir / "cpv_0"
    direct.write_bytes(b"abc")

    assert BenchmarkRunner._find_trial_pov_for_cpv(pov_dir, "cpv_0") == direct


def test_find_trial_pov_for_cpv_supports_blob_suffix(tmp_path: Path) -> None:
    pov_dir = tmp_path / "crs-input" / "povs"
    pov_dir.mkdir(parents=True)
    blob = pov_dir / "cpv_1.blob"
    blob.write_bytes(b"xyz")

    assert BenchmarkRunner._find_trial_pov_for_cpv(pov_dir, "cpv_1") == blob


def test_verify_patches_local_cleans_engine_on_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_dir = tmp_path / "trial" / "output" / "patches"
    patch_dir.mkdir(parents=True)
    (patch_dir / "cpv_0").mkdir()
    (patch_dir / "cpv_0" / "patch.diff").write_text("diff --git a b")

    pov_dir = tmp_path / "trial" / "crs-input" / "povs"
    pov_dir.mkdir(parents=True)
    (pov_dir / "cpv_0").write_bytes(b"pov")

    cleanup_called = {"value": False}

    class _FakeEngine:
        def __init__(self, **_kwargs) -> None:
            pass

        def verify_patch(self, **_kwargs):  # noqa: ANN003
            raise RuntimeError("verification exploded")

        def cleanup(self) -> None:
            cleanup_called["value"] = True

    monkeypatch.setattr(
        "crsbench.evaluation.runner.PatchVerificationEngine", _FakeEngine
    )

    runner = _make_runner("bug-fixing")
    results = runner._verify_patches_local(
        benchmark_path=tmp_path / "benchmark",
        trial_output_dir=tmp_path / "trial",
        oss_fuzz_path=tmp_path / "oss-fuzz",
        harness_name="harness-a",
        target_cpv_id="cpv_0",
    )

    assert results == []
    assert cleanup_called["value"] is True
