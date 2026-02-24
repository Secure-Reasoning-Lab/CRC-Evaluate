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

    assert [(cpv, patch_id) for cpv, patch_id, _ in discovered] == [
        ("cpv_7", "patch_0"),
        ("cpv_7", "patch_1"),
    ]
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

    assert [(cpv, patch_id) for cpv, patch_id, _ in discovered] == [
        ("cpv_0", "patch_0"),
        ("cpv_0", "patch_1"),
        ("cpv_1", "patch_0"),
    ]
    assert [p.name for _, _, p in discovered] == ["a.diff", "b.diff", "c.diff"]


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
