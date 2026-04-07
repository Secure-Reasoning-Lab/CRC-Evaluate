"""Tests for discovery-only mode (only_cpv_harnesses=False).

Validates that projects without ground truth CPVs can run end-to-end
when only_cpv_harnesses is disabled.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

from crsbench.evaluation.reeval.cli import _discover_trial_patches
from crsbench.run_experiment import generate_trial_matrix
from crsbench.validation.schemas import (
    POV,
    BenchmarkHarness,
    ExperimentConfig,
    HarnessFile,
    Vulnerability,
)


def _make_config(*, only_cpv_harnesses: bool = False, **overrides):
    """Create a minimal ExperimentConfig for testing."""
    defaults = {
        "experiment": "test-discovery",
        "trials": 1,
        "mode": "delta",
        "max_total_time": 20000,
        "inputs": {"pov": {"enabled": True, "max_variants_per_cpv": 1}},
        "experiment_filestore": "/tmp/exp",
        "report_filestore": "/tmp/rep",
        "crs_compose": {"crs1": {"num_cores": 1}},
        "benchmarks": ["bench1"],
        "only_cpv_harnesses": only_cpv_harnesses,
    }
    defaults.update(overrides)
    return ExperimentConfig(**defaults)


def _make_harness(*, name: str = "harness1", with_cpvs: bool = True):
    """Create a HarnessFile with or without CPVs."""
    vulns = None
    if with_cpvs:
        vulns = [
            Vulnerability(
                vuln_keyword="cpv_0",
                povs=[POV(id="pov_0", sanitizer="address")],
            ),
        ]
    return HarnessFile(name=name, path=f"/src/{name}.c", vulns=vulns)


def _make_benchmark_harness(
    *, benchmark: str = "bench1", harness_name: str = "h1", with_cpvs: bool = True
):
    """Create a BenchmarkHarness with or without CPVs."""
    return BenchmarkHarness(
        name=benchmark,
        path=Path(f"/tmp/{benchmark}"),
        harness=_make_harness(name=harness_name, with_cpvs=with_cpvs),
    )


# =============================================================================
# Trial Generation Tests
# =============================================================================


class TestDiscoveryModeTrialGeneration:
    """Test that only_cpv_harnesses=False includes harnesses without CPVs."""

    def _generate_with_mock(self, benchmark_harnesses, crs_type, config):
        """Run generate_trial_matrix with mocked CRS type and MetaYamlAdapter."""
        mock_adapter = MagicMock()

        def mock_get_harness(harness_name):
            for bh in benchmark_harnesses:
                if bh.harness.name == harness_name:
                    return bh.harness
            return None

        mock_adapter.get_harness = mock_get_harness

        with (
            patch("crsbench.run_experiment.get_crs_type", return_value=crs_type),
            patch(
                "crsbench.run_experiment.MetaYamlAdapter.from_meta_yaml",
                return_value=mock_adapter,
            ),
        ):
            return generate_trial_matrix(
                benchmark_harnesses,
                ["crs1"],
                config,
                registry_dir=Path("/tmp/registry"),
            )

    def test_bugfinding_includes_no_cpv_harness(self):
        """Bug-finding CRS includes no-CPV harnesses when only_cpv_harnesses=False."""
        config = _make_config(only_cpv_harnesses=False)
        bh = _make_benchmark_harness(with_cpvs=False)

        trials = self._generate_with_mock([bh], "bug-finding", config)

        assert len(trials) == 1
        assert trials[0].target_cpv_id is None

    def test_bugfixing_includes_no_cpv_harness(self):
        """Bug-fixing CRS includes no-CPV harnesses when only_cpv_harnesses=False."""
        config = _make_config(only_cpv_harnesses=False)
        bh = _make_benchmark_harness(with_cpvs=False)

        trials = self._generate_with_mock([bh], "bug-fixing", config)

        assert len(trials) == 1
        assert trials[0].target_cpv_id is None

    def test_bugfixing_still_splits_per_cpv_when_cpvs_exist(self):
        """Bug-fixing CRS still does per-CPV splitting when harness has CPVs."""
        config = _make_config(only_cpv_harnesses=False, trials=1)
        harness = HarnessFile(
            name="h1",
            path="/src/h1.c",
            vulns=[
                Vulnerability(
                    vuln_keyword="cpv_0", povs=[POV(id="pov_0", sanitizer="address")]
                ),
                Vulnerability(
                    vuln_keyword="cpv_1", povs=[POV(id="pov_0", sanitizer="address")]
                ),
            ],
        )
        bh = BenchmarkHarness(name="bench1", path=Path("/tmp/bench1"), harness=harness)

        trials = self._generate_with_mock([bh], "bug-fixing", config)

        assert len(trials) == 2
        assert {t.target_cpv_id for t in trials} == {"cpv_0", "cpv_1"}

    def test_only_cpv_harnesses_true_skips_no_cpv_harness(self):
        """Regression: only_cpv_harnesses=True still skips harnesses without CPVs."""
        config = _make_config(only_cpv_harnesses=True)
        bh_with = _make_benchmark_harness(harness_name="with_cpv", with_cpvs=True)
        bh_without = _make_benchmark_harness(
            benchmark="bench2", harness_name="without_cpv", with_cpvs=False
        )

        trials = self._generate_with_mock([bh_with, bh_without], "bug-finding", config)

        assert len(trials) == 1
        assert trials[0].benchmark_harness.harness.name == "with_cpv"

    def test_bugfixing_with_mixed_harnesses(self):
        """Bug-fixing CRS with only_cpv_harnesses=False handles mix of CPV and no-CPV."""
        config = _make_config(only_cpv_harnesses=False)
        bh_with = _make_benchmark_harness(harness_name="with_cpv", with_cpvs=True)
        bh_without = _make_benchmark_harness(
            benchmark="bench2", harness_name="without_cpv", with_cpvs=False
        )

        trials = self._generate_with_mock([bh_with, bh_without], "bug-fixing", config)

        # with_cpv: 1 CPV = 1 per-CPV trial; without_cpv: 1 generic trial
        assert len(trials) == 2
        cpv_trial = [t for t in trials if t.target_cpv_id is not None]
        generic_trial = [t for t in trials if t.target_cpv_id is None]
        assert len(cpv_trial) == 1
        assert cpv_trial[0].target_cpv_id == "cpv_0"
        assert len(generic_trial) == 1


# =============================================================================
# SARIF Staging Tests
# =============================================================================


class TestSarifStagingDiscoveryMode:
    """Test that SARIF staging degrades gracefully without CPVs."""

    def test_sarif_staging_warns_when_no_cpvs(self, tmp_path):
        """SARIF staging logs warning and returns (no error) when no CPVs available."""
        from crsbench.evaluation.runner import BenchmarkRunner

        # Create minimal mock setup
        mock_crs_adapter = MagicMock()
        runner = BenchmarkRunner(
            adapter=mock_crs_adapter,
            sarif_input_enabled=True,
            sarif_level=1,
        )

        benchmark_path = tmp_path / "bench1"
        benchmark_path.mkdir()
        trial_output_dir = tmp_path / "trial"
        trial_output_dir.mkdir()

        # Mock MetaYamlAdapter to return a harness with no vulns
        mock_meta_adapter = MagicMock()
        mock_harness = MagicMock()
        mock_harness.vulns = []
        mock_meta_adapter.get_harness.return_value = mock_harness

        with patch(
            "crsbench.validation.meta_adapter.MetaYamlAdapter.from_benchmark_path",
            return_value=mock_meta_adapter,
        ):
            # Should not raise — just warn and return
            runner._prepare_bug_candidate_inputs(
                benchmark_path=benchmark_path,
                harness_name="h1",
                trial_output_dir=trial_output_dir,
            )

        # No bug-candidates dir created
        assert not (trial_output_dir / "bug-candidates").exists()


# =============================================================================
# Re-eval Flat Patch Layout Tests
# =============================================================================


class TestReevalFlatPatchDiscoveryMode:
    """Test re-eval patch discovery handles no target_cpv_id."""

    def test_flat_patches_without_target_cpv_uses_unknown(self, tmp_path):
        """Flat patch layout with no target_cpv_id maps patches to 'unknown'."""
        patch_dir = tmp_path / "patches"
        patch_dir.mkdir()
        (patch_dir / "fix1.diff").write_text("--- a/file.c\n+++ b/file.c\n")
        (patch_dir / "fix2.diff").write_text("--- a/other.c\n+++ b/other.c\n")

        result = _discover_trial_patches(patch_dir, target_cpv_id=None)

        assert len(result) == 2
        assert all(cpv_id == "unknown" for cpv_id, _, _ in result)

    def test_flat_patches_with_target_cpv_maps_correctly(self, tmp_path):
        """Flat patch layout with target_cpv_id maps patches correctly."""
        patch_dir = tmp_path / "patches"
        patch_dir.mkdir()
        (patch_dir / "fix1.diff").write_text("--- a/file.c\n+++ b/file.c\n")

        result = _discover_trial_patches(patch_dir, target_cpv_id="cpv_0")

        assert len(result) == 1
        assert result[0][0] == "cpv_0"

    def test_structured_patches_unaffected(self, tmp_path):
        """Structured layout (patches/<cpv>/*.diff) still works normally."""
        patch_dir = tmp_path / "patches"
        cpv_dir = patch_dir / "cpv_0"
        cpv_dir.mkdir(parents=True)
        (cpv_dir / "fix1.diff").write_text("--- a/file.c\n+++ b/file.c\n")

        result = _discover_trial_patches(patch_dir, target_cpv_id=None)

        assert len(result) == 1
        assert result[0][0] == "cpv_0"

    def test_empty_patch_dir(self, tmp_path):
        """Empty or missing patch dir returns empty list."""
        result = _discover_trial_patches(tmp_path / "nonexistent", target_cpv_id=None)
        assert result == []
