"""Unit tests for PatchVerificationManager.

Tests for crsbench/evaluation/verification/patch/manager.py.
"""

from pathlib import Path

from crsbench.evaluation.verification.patch.manager import PatchVerificationManager


class TestPatchVerificationManagerInit:
    """Tests for PatchVerificationManager initialization."""

    def test_init_basic(self, tmp_path: Path) -> None:
        """Test basic initialization."""
        trial_dir = tmp_path / "trial-1"
        trial_dir.mkdir()
        patch_output_dir = trial_dir / "output" / "patches"
        patch_output_dir.mkdir(parents=True)

        manager = PatchVerificationManager(
            trial_dir=trial_dir,
            patch_output_dir=patch_output_dir,
            harness_name="fuzz_parser",
            benchmark_id="test-benchmark",
            input_cpvs_total=3,
        )

        assert manager.harness_name == "fuzz_parser"
        assert manager.benchmark_id == "test-benchmark"
        assert manager.input_cpvs_total == 3
        assert manager.patches_total == 0

    def test_init_with_exchange_patch_dir(self, tmp_path: Path) -> None:
        """Test initialization with exchange_patch_dir."""
        trial_dir = tmp_path / "trial-1"
        trial_dir.mkdir()
        exchange_patch_dir = tmp_path / "exchange" / "patches"
        exchange_patch_dir.mkdir(parents=True)

        manager = PatchVerificationManager(
            trial_dir=trial_dir,
            patch_output_dir=trial_dir / "output" / "patches",
            harness_name="fuzz_parser",
            benchmark_id="test-benchmark",
            input_cpvs_total=2,
            exchange_patch_dir=exchange_patch_dir,
        )

        assert manager._exchange_patch_dir == exchange_patch_dir


class TestDiscoverNewPatches:
    """Tests for PatchVerificationManager._discover_new_patches."""

    def test_discover_empty_directory(self, tmp_path: Path) -> None:
        """Test discovery in empty directory."""
        trial_dir = tmp_path / "trial-1"
        trial_dir.mkdir()
        patch_output_dir = trial_dir / "output" / "patches"
        patch_output_dir.mkdir(parents=True)

        manager = PatchVerificationManager(
            trial_dir=trial_dir,
            patch_output_dir=patch_output_dir,
            harness_name="fuzz_parser",
            benchmark_id="test-benchmark",
            input_cpvs_total=2,
        )

        new_cpv_ids, count = manager._discover_new_patches()
        assert new_cpv_ids == []
        assert count == 0

    def test_discover_patches(self, tmp_path: Path) -> None:
        """Test discovering patches in cpv_id/patch.diff structure."""
        trial_dir = tmp_path / "trial-1"
        trial_dir.mkdir()
        patch_output_dir = trial_dir / "output" / "patches"

        # Create patch structure
        (patch_output_dir / "cpv_0").mkdir(parents=True)
        (patch_output_dir / "cpv_0" / "patch.diff").write_text("diff content 0")
        (patch_output_dir / "cpv_1").mkdir(parents=True)
        (patch_output_dir / "cpv_1" / "patch.diff").write_text("diff content 1")

        manager = PatchVerificationManager(
            trial_dir=trial_dir,
            patch_output_dir=patch_output_dir,
            harness_name="fuzz_parser",
            benchmark_id="test-benchmark",
            input_cpvs_total=2,
        )

        new_cpv_ids, count = manager._discover_new_patches()
        assert count == 2
        assert set(new_cpv_ids) == {"cpv_0", "cpv_1"}


class TestExchangeDirScanning:
    """Tests for EXCHANGE_DIR patch discovery via pre-resolved exchange_patch_dir."""

    def test_discover_from_exchange_dir(self, tmp_path: Path) -> None:
        """Patches in exchange_patch_dir are discovered."""
        trial_dir = tmp_path / "trial-1"
        trial_dir.mkdir()
        exchange_patch = tmp_path / "exchange" / "patches"
        exchange_patch.mkdir(parents=True)

        # Write patch to exchange dir only
        (exchange_patch / "cpv_0").mkdir()
        (exchange_patch / "cpv_0" / "patch.diff").write_text("exchange diff")

        manager = PatchVerificationManager(
            trial_dir=trial_dir,
            patch_output_dir=trial_dir / "output" / "patches",
            harness_name="fuzz_parser",
            benchmark_id="test-benchmark",
            input_cpvs_total=2,
            exchange_patch_dir=exchange_patch,
        )

        new_cpv_ids, count = manager._discover_new_patches()
        assert count == 1
        assert new_cpv_ids == ["cpv_0"]

    def test_discover_merges_both_dirs(self, tmp_path: Path) -> None:
        """Patches from both patch_output_dir and exchange_patch_dir are merged."""
        trial_dir = tmp_path / "trial-1"
        trial_dir.mkdir()
        exchange_patch = tmp_path / "exchange" / "patches"
        exchange_patch.mkdir(parents=True)
        patch_output_dir = trial_dir / "output" / "patches"

        # Write patch to output dir
        (patch_output_dir / "cpv_0").mkdir(parents=True)
        (patch_output_dir / "cpv_0" / "patch.diff").write_text("output diff")

        # Write different patch to exchange dir
        (exchange_patch / "cpv_1").mkdir()
        (exchange_patch / "cpv_1" / "patch.diff").write_text("exchange diff")

        manager = PatchVerificationManager(
            trial_dir=trial_dir,
            patch_output_dir=patch_output_dir,
            harness_name="fuzz_parser",
            benchmark_id="test-benchmark",
            input_cpvs_total=2,
            exchange_patch_dir=exchange_patch,
        )

        new_cpv_ids, count = manager._discover_new_patches()
        assert count == 2
        assert set(new_cpv_ids) == {"cpv_0", "cpv_1"}

    def test_dedup_same_cpv_across_dirs(self, tmp_path: Path) -> None:
        """Same cpv_id in both dirs is counted only once."""
        trial_dir = tmp_path / "trial-1"
        trial_dir.mkdir()
        exchange_patch = tmp_path / "exchange" / "patches"
        exchange_patch.mkdir(parents=True)
        patch_output_dir = trial_dir / "output" / "patches"

        # Same cpv_id in both directories
        (patch_output_dir / "cpv_0").mkdir(parents=True)
        (patch_output_dir / "cpv_0" / "patch.diff").write_text("diff v1")
        (exchange_patch / "cpv_0").mkdir()
        (exchange_patch / "cpv_0" / "patch.diff").write_text("diff v2")

        manager = PatchVerificationManager(
            trial_dir=trial_dir,
            patch_output_dir=patch_output_dir,
            harness_name="fuzz_parser",
            benchmark_id="test-benchmark",
            input_cpvs_total=2,
            exchange_patch_dir=exchange_patch,
        )

        new_cpv_ids, count = manager._discover_new_patches()
        # cpv_0 appears in both, but should only be counted once
        assert count == 1
        assert new_cpv_ids == ["cpv_0"]

    def test_no_exchange_dir_falls_back_gracefully(self, tmp_path: Path) -> None:
        """Without exchange_patch_dir, only patch_output_dir is scanned."""
        trial_dir = tmp_path / "trial-1"
        trial_dir.mkdir()
        patch_output_dir = trial_dir / "output" / "patches"
        (patch_output_dir / "cpv_0").mkdir(parents=True)
        (patch_output_dir / "cpv_0" / "patch.diff").write_text("diff")

        manager = PatchVerificationManager(
            trial_dir=trial_dir,
            patch_output_dir=patch_output_dir,
            harness_name="fuzz_parser",
            benchmark_id="test-benchmark",
            input_cpvs_total=2,
        )

        new_cpv_ids, count = manager._discover_new_patches()
        assert count == 1
