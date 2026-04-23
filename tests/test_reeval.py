"""Unit tests for the re-eval CLI module."""

import argparse
import json
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock, patch

import pytest
from crsbench.evaluation.reeval.cli import (
    _AsyncPatchTrialState,
    _AsyncTrialState,
    _discover_trial_patches,
    _drain_all_async_patch_results,
    _drain_all_async_results,
    _enqueue_trial_povs,
    _load_experiment_config,
    _load_target_cpv_id_from_trial_metadata,
    _reeval_bug_finding,
    _reeval_patch_generation,
    _resolve_benchmark_path,
    _resolve_experiment_dir,
    _resolve_output_dir,
    _resolve_trial_sanitizer,
    _save_patch_results,
    _save_pov_results,
    _write_async_patch_logs,
    add_reeval_subparser,
    run_reeval,
)
from crsbench.evaluation.verification.models import (
    PatchVerificationResult,
    PatchVerificationStatus,
    PovVerificationStatus,
)


def _as_async_trial_states(*states: object) -> list[_AsyncTrialState]:
    return cast("list[_AsyncTrialState]", list(states))


def _as_async_patch_trial_states(*states: object) -> list[_AsyncPatchTrialState]:
    return cast("list[_AsyncPatchTrialState]", list(states))


class TestAddReevalSubparser:
    """Tests for add_reeval_subparser."""

    def test_registers_reeval_command(self) -> None:
        """Should register 're-eval' subcommand with expected arguments."""
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest="command")
        add_reeval_subparser(subparsers)

        args = parser.parse_args(["re-eval", "--experiment-config", "/tmp/config.yaml"])
        assert args.command == "re-eval"
        assert args.experiment_config == Path("/tmp/config.yaml")

    def test_default_values(self) -> None:
        """Should set correct defaults for optional arguments."""
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest="command")
        add_reeval_subparser(subparsers)

        args = parser.parse_args(["re-eval", "-c", "/tmp/config.yaml"])
        assert args.oss_fuzz_path is None
        assert args.source is None
        assert args.mode == "snapshot"
        assert args.jobs is None
        assert args.cores_per_job is None
        assert args.force_rebuild is False
        assert args.output is None
        assert args.verbose is False

    def test_all_flags(self) -> None:
        """Should parse all optional flags."""
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest="command")
        add_reeval_subparser(subparsers)

        args = parser.parse_args(
            [
                "re-eval",
                "-c",
                "/tmp/config.yaml",
                "--oss-fuzz-path",
                "/opt/oss-fuzz",
                "--source",
                "pkgs",
                "--jobs",
                "4",
                "--cores-per-job",
                "8",
                "--force-rebuild",
                "--mode",
                "full",
                "--output",
                "/tmp/out",
                "-v",
            ]
        )
        assert args.oss_fuzz_path == Path("/opt/oss-fuzz")
        assert args.source == "pkgs"
        assert args.jobs == 4
        assert args.cores_per_job == 8
        assert args.force_rebuild is True
        assert args.mode == "full"
        assert args.output == Path("/tmp/out")
        assert args.verbose is True


class TestLoadExperimentConfig:
    """Tests for _load_experiment_config."""

    def test_loads_valid_yaml(self, tmp_path: Path) -> None:
        """Should parse valid YAML config."""
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "experiment: test-exp\nexperiment_filestore: /tmp/store\n"
        )
        config = _load_experiment_config(config_path)
        assert config["experiment"] == "test-exp"
        assert config["experiment_filestore"] == "/tmp/store"

    def test_loads_grouped_yaml_and_normalizes_compat_keys(
        self, tmp_path: Path
    ) -> None:
        """Should flatten grouped experiment/runtime/storage fields for re-eval."""
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "experiment:\n"
            "  name: test-exp\n"
            "runtime:\n"
            "  per_pov_verify_timeout: 91\n"
            "  patch_verify_variants: true\n"
            "  redis:\n"
            "    host: localhost\n"
            "storage:\n"
            "  experiment_filestore: /tmp/store\n"
        )

        config = _load_experiment_config(config_path)

        assert config["experiment"] == "test-exp"
        assert config["experiment_filestore"] == "/tmp/store"
        assert config["per_pov_verify_timeout"] == 91
        assert config["patch_verify_variants"] is True
        assert config["redis_host"] == "localhost"

    def test_missing_file_exits(self, tmp_path: Path) -> None:
        """Should exit if config file not found."""
        with pytest.raises(SystemExit):
            _load_experiment_config(tmp_path / "nonexistent.yaml")


class TestResolveExperimentDir:
    """Tests for _resolve_experiment_dir."""

    def test_returns_filestore_slash_experiment(self) -> None:
        """Should combine experiment_filestore and experiment."""
        config = {
            "experiment_filestore": "/data/experiments",
            "experiment": "my-exp",
        }
        result = _resolve_experiment_dir(config)
        assert result == Path("/data/experiments/my-exp")


class TestResolveBenchmarkPath:
    """Tests for _resolve_benchmark_path."""

    def test_default_root(self) -> None:
        """Should use 'benchmarks' as default root."""
        result = _resolve_benchmark_path("afc-curl-delta-01", None)
        assert result == Path("benchmarks/afc-curl-delta-01")

    def test_custom_root(self) -> None:
        """Should use provided root."""
        result = _resolve_benchmark_path(
            "afc-curl-delta-01", Path("/custom/benchmarks")
        )
        assert result == Path("/custom/benchmarks/afc-curl-delta-01")


class TestResolveOutputDir:
    """Tests for _resolve_output_dir."""

    def test_no_output_base_returns_trial_dir(self) -> None:
        """Should return trial_dir when output_base is None."""
        trial_dir = Path("/data/exp/crs/bench/trial-0")
        result = _resolve_output_dir(trial_dir, None, Path("/data/exp"))
        assert result == trial_dir

    def test_with_output_base_mirrors_structure(self) -> None:
        """Should mirror trial path structure under output_base."""
        experiment_dir = Path("/data/exp")
        trial_dir = Path("/data/exp/crs/bench/trial-0")
        output_base = Path("/tmp/reeval")

        result = _resolve_output_dir(trial_dir, output_base, experiment_dir)
        assert result == Path("/tmp/reeval/crs/bench/trial-0")


class TestSavePovResults:
    """Tests for _save_pov_results."""

    def test_writes_json(self, tmp_path: Path) -> None:
        """Should write results as JSON."""
        mock_result = MagicMock()
        mock_result.to_dict.return_value = {
            "status": "CPV",
            "benchmark": "test",
            "cpv_matched": ["cpv_0"],
        }

        path = _save_pov_results([mock_result], tmp_path)

        assert path.exists()
        data = json.loads(path.read_text())
        assert len(data) == 1
        assert data[0]["status"] == "CPV"

    def test_creates_directory(self, tmp_path: Path) -> None:
        """Should create output directory if needed."""
        dest = tmp_path / "nested" / "dir"
        _save_pov_results([], dest)
        assert dest.exists()


class TestSavePatchResults:
    """Tests for _save_patch_results."""

    def test_writes_json(self, tmp_path: Path) -> None:
        """Should write patch results as JSON via PatchVerificationOutput."""
        with patch(
            "crsbench.evaluation.reeval.cli.PatchVerificationOutput"
        ) as mock_cls:
            mock_output = MagicMock()
            mock_output.model_dump_json.return_value = '{"summary": {}, "results": []}'
            mock_cls.from_results.return_value = mock_output

            path = _save_patch_results([], 0, tmp_path)

            assert path.exists()
            mock_cls.from_results.assert_called_once_with([], 0)

    def test_creates_directory(self, tmp_path: Path) -> None:
        """Should create output directory if needed."""
        dest = tmp_path / "nested" / "dir"
        with patch(
            "crsbench.evaluation.reeval.cli.PatchVerificationOutput"
        ) as mock_cls:
            mock_output = MagicMock()
            mock_output.model_dump_json.return_value = "{}"
            mock_cls.from_results.return_value = mock_output

            _save_patch_results([], 0, dest)
            assert dest.exists()

    def test_summary_uses_patch_ids_not_pov_ids(self, tmp_path: Path) -> None:
        """Summary patch_ids should reflect patch IDs."""
        result = PatchVerificationResult(
            status=PatchVerificationStatus.VALID,
            patch_id="patch_abc",
            pov_id="cpv_0",
            benchmark="bench",
            patch_path=tmp_path / "p.diff",
            harness="h",
        )
        path = _save_patch_results([result], 1, tmp_path)
        data = json.loads(path.read_text())
        assert data["summary"]["patch_ids"] == ["patch_abc"]


class TestReevalPatchGeneration:
    """Tests for local patch-generation re-eval parity with live evaluator."""

    def test_flat_trial_patches_use_trial_target_cpv_mapping(
        self, tmp_path: Path
    ) -> None:
        """Local re-eval should use the trial-aware per-patch verification path."""
        trial_dir = tmp_path / "trial-0"
        patch_dir = trial_dir / "output" / "patches"
        pov_dir = trial_dir / "crs-input" / "povs"
        patch_dir.mkdir(parents=True)
        pov_dir.mkdir(parents=True)

        (trial_dir / "metadata.json").write_text(json.dumps({"target_cpv_id": "cpv_7"}))
        patch_path = patch_dir / "patch.diff"
        patch_path.write_text("--- a/a.c\n+++ b/a.c\n")
        (pov_dir / "cpv_1.blob").write_bytes(b"other")
        target_pov = pov_dir / "cpv_7.blob"
        target_pov.write_bytes(b"target")

        benchmark_path = tmp_path / "bench"
        benchmark_path.mkdir()
        oss_fuzz_path = tmp_path / "oss-fuzz"
        oss_fuzz_path.mkdir()
        dest_dir = tmp_path / "dest"

        expected = PatchVerificationResult(
            status=PatchVerificationStatus.VALID,
            patch_id="patch_0",
            pov_id="cpv_7",
            benchmark="bench",
            patch_path=patch_path,
            harness="harness-a",
        )

        with patch(
            "crsbench.evaluation.verification.patch.engine.PatchVerificationEngine"
        ) as mock_engine_cls:
            mock_engine = MagicMock()
            mock_engine.verify_patches.side_effect = AssertionError(
                "local re-eval should use per-patch verification"
            )
            mock_engine.verify_patch.return_value = expected
            mock_engine_cls.return_value = mock_engine

            count = _reeval_patch_generation(
                trial_dir=trial_dir,
                benchmark_path=benchmark_path,
                oss_fuzz_path=oss_fuzz_path,
                harness="harness-a",
                dest_dir=dest_dir,
                source_mode="pkgs",
                sanitizer="address",
                jobs=None,
                cores_per_job=None,
                per_pov_verify_timeout=91,
                patch_verify_variants=False,
                force_rebuild=False,
                use_inc_build=True,
            )

        assert count == 1
        mock_engine.verify_patch.assert_called_once()
        call_kwargs = mock_engine.verify_patch.call_args.kwargs
        assert call_kwargs["benchmark_path"] == benchmark_path
        assert call_kwargs["harness"] == "harness-a"
        assert call_kwargs["pov_path"] == target_pov
        assert call_kwargs["patch"].pov_id == "cpv_7"

    def test_flat_trial_patches_infer_single_staged_pov_when_target_missing(
        self, tmp_path: Path
    ) -> None:
        """Flat patch trials should infer the lone staged POV like live verify does."""
        trial_dir = tmp_path / "trial-0"
        patch_dir = trial_dir / "output" / "patches"
        pov_dir = trial_dir / "crs-input" / "povs"
        patch_dir.mkdir(parents=True)
        pov_dir.mkdir(parents=True)

        patch_path = patch_dir / "patch.diff"
        patch_path.write_text("--- a/a.c\n+++ b/a.c\n")
        target_pov = pov_dir / "cpv_7.blob"
        target_pov.write_bytes(b"target")

        benchmark_path = tmp_path / "bench"
        benchmark_path.mkdir()
        oss_fuzz_path = tmp_path / "oss-fuzz"
        oss_fuzz_path.mkdir()
        dest_dir = tmp_path / "dest"

        expected = PatchVerificationResult(
            status=PatchVerificationStatus.VALID,
            patch_id="patch_0",
            pov_id="cpv_7",
            benchmark="bench",
            patch_path=patch_path,
            harness="harness-a",
        )

        with patch(
            "crsbench.evaluation.verification.patch.engine.PatchVerificationEngine"
        ) as mock_engine_cls:
            mock_engine = MagicMock()
            mock_engine.verify_patch.return_value = expected
            mock_engine_cls.return_value = mock_engine

            count = _reeval_patch_generation(
                trial_dir=trial_dir,
                benchmark_path=benchmark_path,
                oss_fuzz_path=oss_fuzz_path,
                harness="harness-a",
                dest_dir=dest_dir,
                source_mode="pkgs",
                sanitizer="address",
                jobs=None,
                cores_per_job=None,
                per_pov_verify_timeout=91,
                patch_verify_variants=False,
                force_rebuild=False,
                use_inc_build=True,
            )

        assert count == 1
        mock_engine.verify_patch.assert_called_once()
        call_kwargs = mock_engine.verify_patch.call_args.kwargs
        assert call_kwargs["pov_path"] == target_pov
        assert call_kwargs["patch"].pov_id == "cpv_7"

    def test_local_patch_generation_always_forces_fresh_rebuild(
        self, tmp_path: Path
    ) -> None:
        """Local re-eval should mirror live runner and always rebuild patches fresh."""
        trial_dir = tmp_path / "trial-0"
        patch_dir = trial_dir / "output" / "patches" / "cpv_7"
        pov_dir = trial_dir / "crs-input" / "povs"
        patch_dir.mkdir(parents=True)
        pov_dir.mkdir(parents=True)

        patch_path = patch_dir / "patch.diff"
        patch_path.write_text("--- a/a.c\n+++ b/a.c\n")
        (pov_dir / "cpv_7.blob").write_bytes(b"target")

        benchmark_path = tmp_path / "bench"
        benchmark_path.mkdir()
        oss_fuzz_path = tmp_path / "oss-fuzz"
        oss_fuzz_path.mkdir()
        dest_dir = tmp_path / "dest"

        with patch(
            "crsbench.evaluation.verification.patch.engine.PatchVerificationEngine"
        ) as mock_engine_cls:
            mock_engine = MagicMock()
            mock_engine.verify_patch.return_value = PatchVerificationResult(
                status=PatchVerificationStatus.VALID,
                patch_id="patch_0",
                pov_id="cpv_7",
                benchmark="bench",
                patch_path=patch_path,
                harness="harness-a",
            )
            mock_engine_cls.return_value = mock_engine

            _reeval_patch_generation(
                trial_dir=trial_dir,
                benchmark_path=benchmark_path,
                oss_fuzz_path=oss_fuzz_path,
                harness="harness-a",
                dest_dir=dest_dir,
                source_mode="pkgs",
                sanitizer="address",
                jobs=None,
                cores_per_job=None,
                per_pov_verify_timeout=91,
                patch_verify_variants=False,
                force_rebuild=False,
                use_inc_build=True,
            )

        assert mock_engine_cls.call_args.kwargs["force_rebuild"] is True


class TestAsyncPatchLogPersistence:
    """Tests for async patch log persistence."""

    def test_writes_flat_patch_logs(self, tmp_path: Path) -> None:
        """Should persist async logs under patches/logs as flat files."""
        result = {
            "logs": {
                "patch-a__cpv-cpv_0__pov__pov_0.stdout": "ok",
                "patch-a__cpv-cpv_0__pov__pov_0.stderr": "err",
            }
        }
        _write_async_patch_logs(tmp_path, result)

        logs_dir = tmp_path / "patches" / "logs"
        assert (logs_dir / "patch-a__cpv-cpv_0__pov__pov_0.stdout").read_text() == "ok"
        assert (logs_dir / "patch-a__cpv-cpv_0__pov__pov_0.stderr").read_text() == "err"

    def test_handles_name_collisions_without_overwrite(self, tmp_path: Path) -> None:
        """Colliding filenames should be preserved with numeric suffixes."""
        result = {"logs": {"dup.stdout": "first"}}
        _write_async_patch_logs(tmp_path, result)
        _write_async_patch_logs(tmp_path, result)

        logs_dir = tmp_path / "patches" / "logs"
        files = sorted(p.name for p in logs_dir.glob("dup*.stdout"))
        assert files == ["dup-1.stdout", "dup.stdout"]


class TestRunReeval:
    """Tests for run_reeval."""

    def _make_args(
        self,
        config_path: Path,
        oss_fuzz: Path,
        output: Path | None = None,
    ) -> argparse.Namespace:
        """Create a Namespace matching CLI args."""
        return argparse.Namespace(
            experiment_config=config_path,
            oss_fuzz_path=oss_fuzz,
            source="main_repo",
            mode="snapshot",
            jobs=None,
            cores_per_job=None,
            force_rebuild=False,
            per_pov_verify_timeout=None,
            output=output,
            verbose=False,
        )

    def test_missing_experiment_dir(self, tmp_path: Path) -> None:
        """Should return 1 when experiment dir doesn't exist."""
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            f"experiment: nonexistent\nexperiment_filestore: {tmp_path}\n"
        )
        oss_fuzz = tmp_path / "oss-fuzz"
        oss_fuzz.mkdir()

        args = self._make_args(config_path, oss_fuzz)
        result = run_reeval(args)
        assert result == 1

    def test_missing_oss_fuzz(self, tmp_path: Path) -> None:
        """Should return 1 when oss-fuzz dir doesn't exist."""
        experiment_dir = tmp_path / "my-exp"
        experiment_dir.mkdir(parents=True)

        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            f"experiment: my-exp\nexperiment_filestore: {tmp_path}\n"
        )

        args = self._make_args(config_path, tmp_path / "no-oss-fuzz")
        result = run_reeval(args)
        assert result == 1

    def test_no_trials_found(self, tmp_path: Path) -> None:
        """Should return 1 when no trials found."""
        experiment_dir = tmp_path / "my-exp"
        experiment_dir.mkdir(parents=True)

        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            f"experiment: my-exp\nexperiment_filestore: {tmp_path}\n"
        )
        oss_fuzz = tmp_path / "oss-fuzz"
        oss_fuzz.mkdir()

        args = self._make_args(config_path, oss_fuzz)
        result = run_reeval(args)
        assert result == 1

    @staticmethod
    def _make_trial_metadata(
        trial_num: int,
        benchmark: str = "test-bench",
        harness: str = "test-harness",
        mode: str = "bug_finding",
    ) -> dict:
        """Create valid trial metadata dict."""
        return {
            "timestamp": "2024-01-01T00:00:00",
            "trial_num": trial_num,
            "crs": "test-crs",
            "benchmark": benchmark,
            "harness": harness,
            "mode": mode,
            "source": {"path": "/src/test"},
            "sanitizer": "address",
        }

    @staticmethod
    def _create_minimal_reeval_outputs(trial_dir: Path, mode: str) -> None:
        """Create minimal output artifacts required for re-eval readiness."""
        if mode == "bug_finding":
            pov_dir = trial_dir / "output" / "povs"
            pov_dir.mkdir(parents=True, exist_ok=True)
            (pov_dir / "pov_0.blob").write_bytes(b"pov")
        elif mode == "patch_generation":
            patch_dir = trial_dir / "output" / "patches" / "cpv_0"
            patch_dir.mkdir(parents=True, exist_ok=True)
            (patch_dir / "patch.diff").write_text("--- a/a.c\n+++ b/a.c\n")

    def test_dispatches_bug_finding(self, tmp_path: Path) -> None:
        """Should dispatch to _reeval_bug_finding for bug_finding trials."""
        # Setup experiment dir with a trial
        experiment_dir = tmp_path / "my-exp"
        trial_dir = (
            experiment_dir / "crs" / "bench" / "harness" / "bug_finding" / "trial-0"
        )
        trial_dir.mkdir(parents=True)

        metadata = self._make_trial_metadata(0)
        (trial_dir / "metadata.json").write_text(json.dumps(metadata))
        self._create_minimal_reeval_outputs(trial_dir, mode="bug_finding")

        # Create benchmark dir
        bench_dir = tmp_path / "benchmarks" / "test-bench"
        bench_dir.mkdir(parents=True)

        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "experiment: my-exp\n"
            f"experiment_filestore: {tmp_path}\n"
            f"benchmarks_root: {tmp_path / 'benchmarks'}\n"
        )
        oss_fuzz = tmp_path / "oss-fuzz"
        oss_fuzz.mkdir()

        args = self._make_args(config_path, oss_fuzz)

        with patch("crsbench.evaluation.reeval.cli._reeval_bug_finding") as mock_bf:
            mock_bf.return_value = 3
            result = run_reeval(args)

            mock_bf.assert_called_once()
            call_kwargs = mock_bf.call_args
            assert call_kwargs.kwargs["benchmark_path"] == bench_dir
            assert call_kwargs.kwargs["harness"] == "test-harness"
            assert call_kwargs.kwargs["sanitizer"] == "address"
            assert result == 0

    def test_bug_finding_uses_config_source_mode_when_cli_omits_override(
        self, tmp_path: Path
    ) -> None:
        """Should default bug-finding re-eval source mode from experiment config."""
        experiment_dir = tmp_path / "my-exp"
        trial_dir = (
            experiment_dir / "crs" / "bench" / "harness" / "bug_finding" / "trial-0"
        )
        trial_dir.mkdir(parents=True)

        metadata = self._make_trial_metadata(0)
        (trial_dir / "metadata.json").write_text(json.dumps(metadata))
        self._create_minimal_reeval_outputs(trial_dir, mode="bug_finding")

        bench_dir = tmp_path / "benchmarks" / "test-bench"
        bench_dir.mkdir(parents=True)

        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "experiment: my-exp\n"
            f"experiment_filestore: {tmp_path}\n"
            f"benchmarks_root: {tmp_path / 'benchmarks'}\n"
            "source_mode: main_repo\n"
        )
        oss_fuzz = tmp_path / "oss-fuzz"
        oss_fuzz.mkdir()

        args = self._make_args(config_path, oss_fuzz)
        args.source = None

        with patch("crsbench.evaluation.reeval.cli._reeval_bug_finding") as mock_bf:
            mock_bf.return_value = 1
            result = run_reeval(args)

            mock_bf.assert_called_once()
            assert mock_bf.call_args.kwargs["benchmark_path"] == bench_dir
            assert mock_bf.call_args.kwargs["source_mode"] == "main_repo"
            assert result == 0

    def test_bug_finding_uses_config_inc_build_setting(self, tmp_path: Path) -> None:
        """Bug-finding re-eval should honor experiment inc_build_enabled."""
        experiment_dir = tmp_path / "my-exp"
        trial_dir = (
            experiment_dir / "crs" / "bench" / "harness" / "bug_finding" / "trial-0"
        )
        trial_dir.mkdir(parents=True)

        metadata = self._make_trial_metadata(0)
        (trial_dir / "metadata.json").write_text(json.dumps(metadata))
        self._create_minimal_reeval_outputs(trial_dir, mode="bug_finding")

        bench_dir = tmp_path / "benchmarks" / "test-bench"
        bench_dir.mkdir(parents=True)

        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "experiment: my-exp\n"
            f"experiment_filestore: {tmp_path}\n"
            f"benchmarks_root: {tmp_path / 'benchmarks'}\n"
            "inc_build_enabled: false\n"
        )
        oss_fuzz = tmp_path / "oss-fuzz"
        oss_fuzz.mkdir()

        args = self._make_args(config_path, oss_fuzz)
        args.source = None

        with patch("crsbench.evaluation.reeval.cli._reeval_bug_finding") as mock_bf:
            mock_bf.return_value = 1
            result = run_reeval(args)

            mock_bf.assert_called_once()
            assert mock_bf.call_args.kwargs["benchmark_path"] == bench_dir
            assert mock_bf.call_args.kwargs["use_inc_build"] is False
            assert result == 0

    def test_dispatches_patch_generation(self, tmp_path: Path) -> None:
        """Should dispatch to _reeval_patch_generation for patch_generation trials."""
        experiment_dir = tmp_path / "my-exp"
        trial_dir = (
            experiment_dir
            / "crs"
            / "bench"
            / "harness"
            / "patch_generation"
            / "trial-0"
        )
        trial_dir.mkdir(parents=True)

        metadata = self._make_trial_metadata(0, mode="patch_generation")
        (trial_dir / "metadata.json").write_text(json.dumps(metadata))
        self._create_minimal_reeval_outputs(trial_dir, mode="patch_generation")

        bench_dir = tmp_path / "benchmarks" / "test-bench"
        bench_dir.mkdir(parents=True)

        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "experiment: my-exp\n"
            f"experiment_filestore: {tmp_path}\n"
            f"benchmarks_root: {tmp_path / 'benchmarks'}\n"
            "patch_verify_variants: true\n"
        )
        oss_fuzz = tmp_path / "oss-fuzz"
        oss_fuzz.mkdir()

        args = self._make_args(config_path, oss_fuzz)

        with patch(
            "crsbench.evaluation.reeval.cli._reeval_patch_generation"
        ) as mock_pg:
            mock_pg.return_value = 2
            result = run_reeval(args)

            mock_pg.assert_called_once()
            call_kwargs = mock_pg.call_args
            assert call_kwargs.kwargs["benchmark_path"] == bench_dir
            assert call_kwargs.kwargs["harness"] == "test-harness"
            assert call_kwargs.kwargs["patch_verify_variants"] is True
            assert result == 0

    def test_dispatches_patch_generation_with_grouped_config(
        self, tmp_path: Path
    ) -> None:
        """Should read grouped storage/runtime keys for patch re-eval."""
        experiment_dir = tmp_path / "my-exp"
        trial_dir = (
            experiment_dir
            / "crs"
            / "bench"
            / "harness"
            / "patch_generation"
            / "trial-0"
        )
        trial_dir.mkdir(parents=True)

        metadata = self._make_trial_metadata(0, mode="patch_generation")
        (trial_dir / "metadata.json").write_text(json.dumps(metadata))
        self._create_minimal_reeval_outputs(trial_dir, mode="patch_generation")

        bench_dir = tmp_path / "benchmarks" / "test-bench"
        bench_dir.mkdir(parents=True)

        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "experiment:\n"
            "  name: my-exp\n"
            "runtime:\n"
            "  per_pov_verify_timeout: 91\n"
            "  patch_verify_variants: true\n"
            "storage:\n"
            f"  experiment_filestore: {tmp_path}\n"
            f"benchmarks_root: {tmp_path / 'benchmarks'}\n"
        )
        oss_fuzz = tmp_path / "oss-fuzz"
        oss_fuzz.mkdir()

        args = self._make_args(config_path, oss_fuzz)

        with patch(
            "crsbench.evaluation.reeval.cli._reeval_patch_generation"
        ) as mock_pg:
            mock_pg.return_value = 2
            result = run_reeval(args)

            mock_pg.assert_called_once()
            call_kwargs = mock_pg.call_args
            assert call_kwargs.kwargs["benchmark_path"] == bench_dir
            assert call_kwargs.kwargs["patch_verify_variants"] is True
            assert call_kwargs.kwargs["per_pov_verify_timeout"] == 91
            assert result == 0

    def test_skips_missing_benchmark(self, tmp_path: Path) -> None:
        """Should skip trial when benchmark path doesn't exist."""
        experiment_dir = tmp_path / "my-exp"
        trial_dir = experiment_dir / "trial-0"
        trial_dir.mkdir(parents=True)

        metadata = self._make_trial_metadata(0, benchmark="nonexistent-bench")
        (trial_dir / "metadata.json").write_text(json.dumps(metadata))
        self._create_minimal_reeval_outputs(trial_dir, mode="bug_finding")

        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            f"experiment: my-exp\nexperiment_filestore: {tmp_path}\n"
        )
        oss_fuzz = tmp_path / "oss-fuzz"
        oss_fuzz.mkdir()

        args = self._make_args(config_path, oss_fuzz)
        # Should return 1 since the only trial errored and produced 0 results
        result = run_reeval(args)
        assert result == 1

    def test_handles_exception_in_trial(self, tmp_path: Path) -> None:
        """Should continue processing other trials after an exception."""
        experiment_dir = tmp_path / "my-exp"

        # Create two trials
        for i in range(2):
            trial_dir = experiment_dir / f"trial-{i}"
            trial_dir.mkdir(parents=True)
            metadata = self._make_trial_metadata(i)
            (trial_dir / "metadata.json").write_text(json.dumps(metadata))
            self._create_minimal_reeval_outputs(trial_dir, mode="bug_finding")

        bench_dir = tmp_path / "benchmarks" / "test-bench"
        bench_dir.mkdir(parents=True)

        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "experiment: my-exp\n"
            f"experiment_filestore: {tmp_path}\n"
            f"benchmarks_root: {tmp_path / 'benchmarks'}\n"
        )
        oss_fuzz = tmp_path / "oss-fuzz"
        oss_fuzz.mkdir()

        args = self._make_args(config_path, oss_fuzz)

        call_count = 0

        def side_effect(**_kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("simulated failure")
            return 5

        with patch("crsbench.evaluation.reeval.cli._reeval_bug_finding") as mock_bf:
            mock_bf.side_effect = side_effect
            result = run_reeval(args)

            assert mock_bf.call_count == 2
            # Should succeed since second trial produced results
            assert result == 0

    def test_output_dir_mirroring(self, tmp_path: Path) -> None:
        """Should mirror trial path when --output is specified."""
        experiment_dir = tmp_path / "my-exp"
        trial_dir = experiment_dir / "crs" / "bench" / "trial-0"
        trial_dir.mkdir(parents=True)

        metadata = self._make_trial_metadata(0)
        (trial_dir / "metadata.json").write_text(json.dumps(metadata))
        self._create_minimal_reeval_outputs(trial_dir, mode="bug_finding")

        bench_dir = tmp_path / "benchmarks" / "test-bench"
        bench_dir.mkdir(parents=True)

        output_dir = tmp_path / "reeval-out"

        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "experiment: my-exp\n"
            f"experiment_filestore: {tmp_path}\n"
            f"benchmarks_root: {tmp_path / 'benchmarks'}\n"
        )
        oss_fuzz = tmp_path / "oss-fuzz"
        oss_fuzz.mkdir()

        args = self._make_args(config_path, oss_fuzz, output=output_dir)

        with patch("crsbench.evaluation.reeval.cli._reeval_bug_finding") as mock_bf:
            mock_bf.return_value = 1
            run_reeval(args)

            call_kwargs = mock_bf.call_args
            expected_dest = output_dir / "crs" / "bench" / "trial-0"
            assert call_kwargs.kwargs["dest_dir"] == expected_dest

    def test_patch_generation_async_uses_redis_queues(self, tmp_path: Path) -> None:
        """Patch generation should enqueue to Redis queues in async mode."""
        from crsbench.validation.schemas import TrialMode

        experiment_dir = tmp_path / "my-exp"
        trial_dir = (
            experiment_dir
            / "crs"
            / "bench"
            / "harness"
            / "patch_generation"
            / "trial-0"
        )
        trial_dir.mkdir(parents=True)

        bench_dir = tmp_path / "benchmarks" / "test-bench"
        bench_dir.mkdir(parents=True)

        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "experiment: my-exp\n"
            f"experiment_filestore: {tmp_path}\n"
            f"benchmarks_root: {tmp_path / 'benchmarks'}\n"
            "redis_host: localhost\n"
        )
        oss_fuzz = tmp_path / "oss-fuzz"
        oss_fuzz.mkdir()

        args = self._make_args(config_path, oss_fuzz)

        mock_trial = SimpleNamespace(
            status="valid",
            reeval_ready=True,
            reeval_reason="ready",
            trial_dir=trial_dir,
            benchmark="test-bench",
            harness="test-harness",
            mode=TrialMode.patch_generation,
            trial_num=0,
        )

        with (
            patch(
                "crsbench.reporting.snapshot_loader.discover_trials",
                return_value=[mock_trial],
            ),
            patch(
                "crsbench.distributed.runtime_session.DistributedRuntimeSession.for_reeval"
            ) as mock_for_reeval,
            patch(
                "crsbench.evaluation.reeval.cli._enqueue_trial_patches"
            ) as mock_enqueue,
            patch(
                "crsbench.evaluation.reeval.cli._drain_all_async_patch_results",
                return_value=1,
            ) as mock_drain,
            patch(
                "crsbench.evaluation.reeval.cli._reeval_patch_generation"
            ) as mock_local_pg,
        ):
            mock_session = MagicMock()
            mock_session.trial_queue = MagicMock()
            mock_session.build_queue = MagicMock()
            mock_session.verify_queue = MagicMock()
            mock_session.registry.get_experiment.return_value = None
            mock_for_reeval.return_value = mock_session
            mock_enqueue.return_value = SimpleNamespace(
                trial_id="test-bench__test-harness__trial-0", job_ids=["jid-1"]
            )

            result = run_reeval(args)

            assert result == 0
            mock_enqueue.assert_called_once()
            mock_drain.assert_called_once()
            mock_local_pg.assert_not_called()

    def test_patch_generation_redis_host_none_stays_local(self, tmp_path: Path) -> None:
        """redis_host: none should disable async mode and keep local re-eval."""
        from crsbench.validation.schemas import TrialMode

        experiment_dir = tmp_path / "my-exp"
        trial_dir = (
            experiment_dir
            / "crs"
            / "bench"
            / "harness"
            / "patch_generation"
            / "trial-0"
        )
        trial_dir.mkdir(parents=True)
        bench_dir = tmp_path / "benchmarks" / "test-bench"
        bench_dir.mkdir(parents=True)
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "experiment: my-exp\n"
            f"experiment_filestore: {tmp_path}\n"
            f"benchmarks_root: {tmp_path / 'benchmarks'}\n"
            "redis_host: none\n"
        )
        oss_fuzz = tmp_path / "oss-fuzz"
        oss_fuzz.mkdir()
        args = self._make_args(config_path, oss_fuzz)

        mock_trial = SimpleNamespace(
            status="valid",
            reeval_ready=True,
            reeval_reason="ready",
            trial_dir=trial_dir,
            benchmark="test-bench",
            harness="test-harness",
            mode=TrialMode.patch_generation,
            trial_num=0,
        )
        with (
            patch(
                "crsbench.reporting.snapshot_loader.discover_trials",
                return_value=[mock_trial],
            ),
            patch(
                "crsbench.distributed.runtime_session.DistributedRuntimeSession.for_reeval"
            ) as mock_for_reeval,
            patch(
                "crsbench.evaluation.reeval.cli._reeval_patch_generation",
                return_value=1,
            ) as mock_local_pg,
            patch(
                "crsbench.evaluation.reeval.cli._enqueue_trial_patches"
            ) as mock_enqueue,
        ):
            result = run_reeval(args)

        assert result == 0
        mock_for_reeval.assert_not_called()
        mock_local_pg.assert_called_once()
        mock_enqueue.assert_not_called()

    def test_bug_finding_async_enqueues_sanitizer(self, tmp_path: Path) -> None:
        """Async bug-finding re-eval should pass trial sanitizer to queue payloads."""
        from crsbench.validation.schemas import TrialMode

        experiment_dir = tmp_path / "my-exp"
        trial_dir = (
            experiment_dir / "crs" / "bench" / "harness" / "bug_finding" / "trial-0"
        )
        trial_dir.mkdir(parents=True)
        bench_dir = tmp_path / "benchmarks" / "test-bench"
        bench_dir.mkdir(parents=True)
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "experiment: my-exp\n"
            f"experiment_filestore: {tmp_path}\n"
            f"benchmarks_root: {tmp_path / 'benchmarks'}\n"
            "redis_host: localhost\n"
        )
        oss_fuzz = tmp_path / "oss-fuzz"
        oss_fuzz.mkdir()
        args = self._make_args(config_path, oss_fuzz)

        mock_trial = SimpleNamespace(
            status="valid",
            reeval_ready=True,
            reeval_reason="ready",
            trial_dir=trial_dir,
            benchmark="test-bench",
            harness="test-harness",
            mode=TrialMode.bug_finding,
            sanitizer="undefined",
            trial_num=0,
        )

        with (
            patch(
                "crsbench.reporting.snapshot_loader.discover_trials",
                return_value=[mock_trial],
            ),
            patch(
                "crsbench.distributed.runtime_session.DistributedRuntimeSession.for_reeval"
            ) as mock_for_reeval,
            patch(
                "crsbench.evaluation.reeval.cli._enqueue_trial_povs",
                return_value=SimpleNamespace(trial_id="t", job_ids=["jid-1"]),
            ) as mock_enqueue,
            patch(
                "crsbench.evaluation.reeval.cli._drain_all_async_results",
                return_value=1,
            ),
            patch(
                "crsbench.evaluation.reeval.cli._reeval_bug_finding"
            ) as mock_local_bf,
        ):
            mock_session = MagicMock()
            mock_session.trial_queue = MagicMock()
            mock_session.build_queue = MagicMock()
            mock_session.verify_queue = MagicMock()
            mock_session.registry.get_experiment.return_value = None
            mock_for_reeval.return_value = mock_session

            result = run_reeval(args)

        assert result == 0
        mock_local_bf.assert_not_called()
        mock_enqueue.assert_called_once()
        assert mock_enqueue.call_args.kwargs["sanitizer"] == "undefined"

    def test_bug_finding_async_uses_config_source_mode_and_inc_build(
        self, tmp_path: Path
    ) -> None:
        """Async bug-finding should forward config runtime settings to queue enqueue."""
        from crsbench.validation.schemas import TrialMode

        experiment_dir = tmp_path / "my-exp"
        trial_dir = (
            experiment_dir / "crs" / "bench" / "harness" / "bug_finding" / "trial-0"
        )
        trial_dir.mkdir(parents=True)
        bench_dir = tmp_path / "benchmarks" / "test-bench"
        bench_dir.mkdir(parents=True)
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "experiment: my-exp\n"
            f"experiment_filestore: {tmp_path}\n"
            f"benchmarks_root: {tmp_path / 'benchmarks'}\n"
            "redis_host: localhost\n"
            "source_mode: main_repo\n"
            "inc_build_enabled: false\n"
        )
        oss_fuzz = tmp_path / "oss-fuzz"
        oss_fuzz.mkdir()
        args = self._make_args(config_path, oss_fuzz)
        args.source = None

        mock_trial = SimpleNamespace(
            status="valid",
            reeval_ready=True,
            reeval_reason="ready",
            trial_dir=trial_dir,
            benchmark="test-bench",
            harness="test-harness",
            mode=TrialMode.bug_finding,
            sanitizer="address",
            trial_num=0,
        )

        with (
            patch(
                "crsbench.reporting.snapshot_loader.discover_trials",
                return_value=[mock_trial],
            ),
            patch(
                "crsbench.distributed.runtime_session.DistributedRuntimeSession.for_reeval"
            ) as mock_for_reeval,
            patch(
                "crsbench.evaluation.reeval.cli._enqueue_trial_povs",
                return_value=SimpleNamespace(trial_id="t", job_ids=["jid-1"]),
            ) as mock_enqueue,
            patch(
                "crsbench.evaluation.reeval.cli._drain_all_async_results",
                return_value=1,
            ),
        ):
            mock_session = MagicMock()
            mock_session.trial_queue = MagicMock()
            mock_session.build_queue = MagicMock()
            mock_session.verify_queue = MagicMock()
            mock_session.registry.get_experiment.return_value = None
            mock_for_reeval.return_value = mock_session

            result = run_reeval(args)

        assert result == 0
        mock_enqueue.assert_called_once()
        assert mock_enqueue.call_args.kwargs["source_mode"] == "main_repo"
        assert mock_enqueue.call_args.kwargs["use_inc_build"] is False

    @pytest.mark.parametrize("redis_host_value", ["false", "0"])
    def test_patch_generation_redis_host_falsy_stays_local(
        self, tmp_path: Path, redis_host_value: str
    ) -> None:
        """Falsy redis_host YAML scalars should disable async mode."""
        from crsbench.validation.schemas import TrialMode

        experiment_dir = tmp_path / "my-exp"
        trial_dir = (
            experiment_dir
            / "crs"
            / "bench"
            / "harness"
            / "patch_generation"
            / "trial-0"
        )
        trial_dir.mkdir(parents=True)
        bench_dir = tmp_path / "benchmarks" / "test-bench"
        bench_dir.mkdir(parents=True)
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "experiment: my-exp\n"
            f"experiment_filestore: {tmp_path}\n"
            f"benchmarks_root: {tmp_path / 'benchmarks'}\n"
            f"redis_host: {redis_host_value}\n"
        )
        oss_fuzz = tmp_path / "oss-fuzz"
        oss_fuzz.mkdir()
        args = self._make_args(config_path, oss_fuzz)

        mock_trial = SimpleNamespace(
            status="valid",
            reeval_ready=True,
            reeval_reason="ready",
            trial_dir=trial_dir,
            benchmark="test-bench",
            harness="test-harness",
            mode=TrialMode.patch_generation,
            trial_num=0,
        )
        with (
            patch(
                "crsbench.reporting.snapshot_loader.discover_trials",
                return_value=[mock_trial],
            ),
            patch(
                "crsbench.distributed.runtime_session.DistributedRuntimeSession.for_reeval"
            ) as mock_for_reeval,
            patch(
                "crsbench.evaluation.reeval.cli._reeval_patch_generation",
                return_value=1,
            ) as mock_local_pg,
            patch(
                "crsbench.evaluation.reeval.cli._enqueue_trial_patches"
            ) as mock_enqueue,
        ):
            result = run_reeval(args)

        assert result == 0
        mock_for_reeval.assert_not_called()
        mock_local_pg.assert_called_once()
        mock_enqueue.assert_not_called()

    def test_async_registry_lock_contention_exits_nonzero(self, tmp_path: Path) -> None:
        """re-eval should fail fast when registry lock cannot be acquired."""
        from crsbench.distributed.runtime_session import LockContentionError
        from crsbench.validation.schemas import TrialMode

        experiment_dir = tmp_path / "my-exp"
        trial_dir = (
            experiment_dir
            / "crs"
            / "bench"
            / "harness"
            / "patch_generation"
            / "trial-0"
        )
        trial_dir.mkdir(parents=True)
        bench_dir = tmp_path / "benchmarks" / "test-bench"
        bench_dir.mkdir(parents=True)
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "experiment: my-exp\n"
            f"experiment_filestore: {tmp_path}\n"
            f"benchmarks_root: {tmp_path / 'benchmarks'}\n"
            "redis_host: localhost\n"
        )
        oss_fuzz = tmp_path / "oss-fuzz"
        oss_fuzz.mkdir()
        args = self._make_args(config_path, oss_fuzz)

        mock_trial = SimpleNamespace(
            status="valid",
            reeval_ready=True,
            reeval_reason="ready",
            trial_dir=trial_dir,
            benchmark="test-bench",
            harness="test-harness",
            mode=TrialMode.patch_generation,
            trial_num=0,
        )

        with (
            patch(
                "crsbench.reporting.snapshot_loader.discover_trials",
                return_value=[mock_trial],
            ),
            patch(
                "crsbench.distributed.runtime_session.DistributedRuntimeSession.for_reeval"
            ) as mock_for_reeval,
            patch(
                "crsbench.evaluation.reeval.cli._enqueue_trial_patches"
            ) as mock_enqueue,
        ):
            mock_session = MagicMock()
            mock_session.trial_queue = MagicMock()
            mock_session.build_queue = MagicMock()
            mock_session.verify_queue = MagicMock()
            mock_session.registry.get_experiment.return_value = None
            mock_session.register_or_raise.side_effect = LockContentionError(
                "already locked"
            )
            mock_for_reeval.return_value = mock_session

            result = run_reeval(args)

        assert result == 1
        mock_enqueue.assert_not_called()

    def test_async_registry_register_failure_exits_nonzero(
        self, tmp_path: Path
    ) -> None:
        """re-eval should return nonzero when registry registration fails."""
        from crsbench.validation.schemas import TrialMode

        experiment_dir = tmp_path / "my-exp"
        trial_dir = (
            experiment_dir
            / "crs"
            / "bench"
            / "harness"
            / "patch_generation"
            / "trial-0"
        )
        trial_dir.mkdir(parents=True)
        bench_dir = tmp_path / "benchmarks" / "test-bench"
        bench_dir.mkdir(parents=True)
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "experiment: my-exp\n"
            f"experiment_filestore: {tmp_path}\n"
            f"benchmarks_root: {tmp_path / 'benchmarks'}\n"
            "redis_host: localhost\n"
        )
        oss_fuzz = tmp_path / "oss-fuzz"
        oss_fuzz.mkdir()
        args = self._make_args(config_path, oss_fuzz)

        mock_trial = SimpleNamespace(
            status="valid",
            reeval_ready=True,
            reeval_reason="ready",
            trial_dir=trial_dir,
            benchmark="test-bench",
            harness="test-harness",
            mode=TrialMode.patch_generation,
            trial_num=0,
        )

        with (
            patch(
                "crsbench.reporting.snapshot_loader.discover_trials",
                return_value=[mock_trial],
            ),
            patch(
                "crsbench.distributed.runtime_session.DistributedRuntimeSession.for_reeval"
            ) as mock_for_reeval,
            patch(
                "crsbench.evaluation.reeval.cli._enqueue_trial_patches"
            ) as mock_enqueue,
        ):
            mock_session = MagicMock()
            mock_session.trial_queue = MagicMock()
            mock_session.build_queue = MagicMock()
            mock_session.verify_queue = MagicMock()
            mock_session.registry.get_experiment.return_value = None
            mock_session.register_or_raise.side_effect = RuntimeError(
                "registry publish failed"
            )
            mock_for_reeval.return_value = mock_session

            result = run_reeval(args)

        assert result == 1
        mock_enqueue.assert_not_called()
        mock_session.cleanup.assert_called()

    def test_async_registration_publishes_runtime_settings(
        self, tmp_path: Path
    ) -> None:
        """Configless re-eval registration should publish evaluator runtime settings."""
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "experiment: my-exp\n"
            f"experiment_filestore: {tmp_path}\n"
            f"benchmarks_root: {tmp_path / 'benchmarks'}\n"
            "redis_host: localhost\n"
            "source_mode: main_repo\n"
            "max_total_time: 999\n"
            "build_timeout: 123\n"
            "per_pov_verify_timeout: 91\n"
            "inc_image_policy: pull_only\n"
            "inc_image_registry: ghcr.io/example/custom\n"
            "inc_image_max_pull_bytes: 456\n"
            "inc_image_pull_timeout_sec: 78\n"
            "project_image_prefix: custom-prefix\n"
        )
        experiment_dir = tmp_path / "my-exp"
        experiment_dir.mkdir(parents=True)
        (tmp_path / "benchmarks").mkdir()
        oss_fuzz = tmp_path / "oss-fuzz"
        oss_fuzz.mkdir()
        args = self._make_args(config_path, oss_fuzz)
        args.source = None

        with (
            patch(
                "crsbench.reporting.snapshot_loader.discover_trials",
                return_value=[],
            ),
            patch(
                "crsbench.distributed.runtime_session.DistributedRuntimeSession.for_reeval"
            ) as mock_for_reeval,
        ):
            mock_session = MagicMock()
            mock_session.trial_queue = MagicMock()
            mock_session.build_queue = MagicMock()
            mock_session.verify_queue = MagicMock()
            mock_session.registry.get_experiment.return_value = None
            mock_for_reeval.return_value = mock_session

            result = run_reeval(args)

        assert result == 1
        registration = mock_session.register_or_raise.call_args.args[0]
        assert registration.source_mode == "main_repo"
        assert registration.max_total_time == 999
        assert registration.build_timeout == 123
        assert registration.per_pov_verify_timeout == 91
        assert registration.inc_image_policy == "pull_only"
        assert registration.inc_image_registry == "ghcr.io/example/custom"
        assert registration.inc_image_max_pull_bytes == 456
        assert registration.inc_image_pull_timeout_sec == 78
        assert registration.local_image_prefix == "custom-prefix"


class TestAsyncPatchDrain:
    """Tests for async patch result draining."""

    def test_drain_keeps_result_when_log_write_fails(self, tmp_path: Path) -> None:
        """Patch results should still be saved if log persistence errors."""
        trial_state = SimpleNamespace(
            trial_id="bench__h__trial-1",
            total_input_povs=1,
            dest_dir=tmp_path,
            job_ids=["job-1"],
        )
        verdict = {
            "trial_id": "bench__h__trial-1",
            "benchmark": "bench",
            "harness": "h",
            "cpv_id": "cpv_0",
            "patch_id": "patch_0",
            "status": "valid",
            "security_verdict": "PASS",
            "logs": {"one.stdout": "x"},
        }

        with (
            patch(
                "crsbench.distributed.patch_queue.poll_patch_verdicts",
                return_value=([verdict], []),
            ),
            patch(
                "crsbench.evaluation.reeval.cli._write_async_patch_logs",
                side_effect=OSError("disk full"),
            ),
            patch("crsbench.evaluation.reeval.cli._save_patch_results") as mock_save,
        ):
            count = _drain_all_async_patch_results(
                _as_async_patch_trial_states(trial_state),
                "localhost",
            )

        assert count == 1
        mock_save.assert_called_once()

    def test_drain_uses_live_patch_queue_drain_loop(self, tmp_path: Path) -> None:
        """Async patch drain should use the shared drain loop with recovery semantics."""
        trial_state = SimpleNamespace(
            trial_id="bench__h__trial-1",
            total_input_povs=1,
            dest_dir=tmp_path,
            job_ids=["job-1"],
        )
        verdict = {
            "trial_id": "bench__h__trial-1",
            "benchmark": "bench",
            "harness": "h",
            "cpv_id": "cpv_0",
            "patch_id": "patch_0",
            "status": "valid",
            "security_verdict": "PASS",
        }

        with (
            patch(
                "crsbench.distributed.patch_queue.drain_patch_verdicts",
                return_value=[verdict],
            ) as mock_drain,
            patch("crsbench.evaluation.reeval.cli._save_patch_results") as mock_save,
        ):
            count = _drain_all_async_patch_results(
                _as_async_patch_trial_states(trial_state),
                "localhost",
                timeout_seconds=77.0,
            )

        assert count == 1
        mock_drain.assert_called_once_with("localhost", ["job-1"], timeout=77.0)
        mock_save.assert_called_once()


class TestPatchDiscovery:
    """Tests for re-eval patch discovery helpers."""

    def test_discover_trial_patches_flat_layout_uses_unknown_without_target_cpv(
        self, tmp_path: Path
    ) -> None:
        """Flat patch layout maps to 'unknown' CPV when target cannot be inferred."""
        patch_dir = tmp_path / "output" / "patches"
        patch_dir.mkdir(parents=True)
        (patch_dir / "patch_0.diff").write_text("diff")

        result = _discover_trial_patches(patch_dir, target_cpv_id=None)
        assert len(result) == 1
        assert result[0][0] == "unknown"

    def test_discover_trial_patches_flat_layout_uses_metadata_target(
        self, tmp_path: Path
    ) -> None:
        """target_cpv_id loaded from metadata should map flat patch layout."""
        trial_dir = tmp_path / "trial-1"
        patch_dir = trial_dir / "output" / "patches"
        patch_dir.mkdir(parents=True)
        (patch_dir / "patch_0.diff").write_text("diff")
        (trial_dir / "metadata.json").write_text(json.dumps({"target_cpv_id": "cpv_7"}))

        target = _load_target_cpv_id_from_trial_metadata(trial_dir)
        discovered = _discover_trial_patches(patch_dir, target_cpv_id=target)

        assert target == "cpv_7"
        assert len(discovered) == 1
        assert discovered[0][0] == "cpv_7"

    def test_discover_trial_patches_deduplicates_mixed_layout(
        self, tmp_path: Path
    ) -> None:
        """Mixed layouts should preserve runner-style distinct stable patch IDs."""
        patch_dir = tmp_path / "output" / "patches"
        structured = patch_dir / "cpv_0"
        structured.mkdir(parents=True)
        (structured / "patch_0.diff").write_text("structured diff")
        (patch_dir / "patch_0.diff").write_text("flat duplicate")

        discovered = _discover_trial_patches(patch_dir, target_cpv_id="cpv_0")

        assert len(discovered) == 2
        assert [item[0] for item in discovered] == ["cpv_0", "cpv_0"]
        assert discovered[0][1].startswith("structured_patch_0_")
        assert discovered[1][1].startswith("flat_patch_0_")

    def test_resolve_trial_sanitizer_prefers_metadata_over_path(
        self, tmp_path: Path
    ) -> None:
        """Metadata sanitizer should win over path inference."""
        trial_dir = tmp_path / "address" / "bench" / "h" / "delta" / "trial-1"
        trial_dir.mkdir(parents=True)
        (trial_dir / "metadata.json").write_text(json.dumps({"sanitizer": "undefined"}))
        trial = SimpleNamespace()
        assert _resolve_trial_sanitizer(trial, trial_dir) == "undefined"

    def test_resolve_trial_sanitizer_invalid_metadata_falls_back_to_path(
        self, tmp_path: Path
    ) -> None:
        """Invalid metadata sanitizer should not override valid trial path sanitizer."""
        trial_dir = tmp_path / "x" / "bench" / "h" / "memory" / "trial-1"
        trial_dir.mkdir(parents=True)
        (trial_dir / "metadata.json").write_text(json.dumps({"sanitizer": "bogus"}))
        trial = SimpleNamespace()
        assert _resolve_trial_sanitizer(trial, trial_dir) == "memory"


def test_reeval_bug_finding_skips_duplicate_hashes(tmp_path: Path) -> None:
    """Local re-eval should cap verification to one file per content hash."""
    trial_dir = tmp_path / "trial-1"
    pov_dir = trial_dir / "output" / "povs"
    pov_dir.mkdir(parents=True)
    (pov_dir / "a.blob").write_bytes(b"same")
    (pov_dir / "b.blob").write_bytes(b"same")
    (pov_dir / "c.blob").write_bytes(b"unique")

    bench_dir = tmp_path / "bench"
    bench_dir.mkdir()
    oss_fuzz = tmp_path / "oss-fuzz"
    oss_fuzz.mkdir()

    mock_output = SimpleNamespace(results=[])
    mock_engine = MagicMock()
    mock_engine.verify_benchmark.return_value = mock_output

    with patch(
        "crsbench.evaluation.verification.VerificationEngine",
        return_value=mock_engine,
    ):
        result_count = _reeval_bug_finding(
            trial_dir=trial_dir,
            benchmark_path=bench_dir,
            oss_fuzz_path=oss_fuzz,
            harness="h",
            dest_dir=tmp_path / "out",
            source_mode="pkgs",
            jobs=None,
            cores_per_job=None,
            force_rebuild=False,
            use_inc_build=True,
            sanitizer="address",
        )

    assert result_count == 0
    call = mock_engine.verify_benchmark.call_args
    assert call is not None
    assert call.kwargs["max_per_hash"] == 1


class TestAsyncPatchDrainTimeout:
    """Tests for async patch drain timeout behavior."""

    def test_drain_times_out_on_stuck_jobs(self, tmp_path: Path) -> None:
        trial_state = SimpleNamespace(
            trial_id="bench__h__trial-1",
            total_input_povs=1,
            dest_dir=tmp_path,
            job_ids=["job-1"],
        )
        with (
            patch(
                "crsbench.distributed.patch_queue.drain_patch_verdicts",
                return_value=[],
            ),
            pytest.raises(TimeoutError, match="Timed out draining async patch jobs"),
        ):
            _drain_all_async_patch_results(
                _as_async_patch_trial_states(trial_state),
                "localhost",
                timeout_seconds=0.0,
            )

    def test_drain_timeout_persists_partial_results(self, tmp_path: Path) -> None:
        """Timeout should still save completed verdicts collected so far."""
        trial_state = SimpleNamespace(
            trial_id="bench__h__trial-1",
            total_input_povs=1,
            dest_dir=tmp_path,
            job_ids=["job-1", "job-2"],
        )
        verdict = {
            "trial_id": "bench__h__trial-1",
            "benchmark": "bench",
            "harness": "h",
            "cpv_id": "cpv_0",
            "patch_id": "patch_0",
            "status": "valid",
            "security_verdict": "PASS",
        }
        with (
            patch(
                "crsbench.distributed.patch_queue.drain_patch_verdicts",
                return_value=[verdict],
            ),
            patch("crsbench.evaluation.reeval.cli._save_patch_results") as mock_save,
            pytest.raises(TimeoutError, match="Timed out draining async patch jobs"),
        ):
            _drain_all_async_patch_results(
                _as_async_patch_trial_states(trial_state),
                "localhost",
                timeout_seconds=0.1,
            )

        mock_save.assert_called_once()


def test_enqueue_trial_povs_passes_redis_host_to_dispatcher_enqueue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from crsbench.distributed.queue import (
        EVALUATOR_ROUTING_MODEL_ENV,
        ROUTING_MODEL_DISPATCHER,
    )

    monkeypatch.setenv(EVALUATOR_ROUTING_MODEL_ENV, ROUTING_MODEL_DISPATCHER)

    trial_dir = tmp_path / "trial-1"
    pov_dir = trial_dir / "output" / "povs"
    pov_dir.mkdir(parents=True)
    (pov_dir / "a.blob").write_bytes(b"blob")

    with patch(
        "crsbench.distributed.verify_queue.enqueue_single_pov",
        return_value="verify:trial-1:bench:h:a.blob:deadbeef",
    ) as mock_enqueue:
        state = _enqueue_trial_povs(
            trial_dir=trial_dir,
            benchmark_name="bench",
            harness="h",
            dest_dir=tmp_path / "out",
            verify_queue=MagicMock(),
            experiment_name="exp1",
            redis_host="redis.local",
            sanitizer="address",
        )

    assert state is not None
    assert state.job_ids == ["verify:trial-1:bench:h:a.blob:deadbeef"]
    assert mock_enqueue.call_args.kwargs["redis_host"] == "redis.local"


def test_enqueue_trial_povs_passes_source_mode_and_inc_build(
    tmp_path: Path,
) -> None:
    trial_dir = tmp_path / "trial-1"
    pov_dir = trial_dir / "output" / "povs"
    pov_dir.mkdir(parents=True)
    (pov_dir / "a.blob").write_bytes(b"blob")

    with patch(
        "crsbench.distributed.verify_queue.enqueue_single_pov",
        return_value="verify:trial-1:bench:h:a.blob:deadbeef",
    ) as mock_enqueue:
        state = _enqueue_trial_povs(
            trial_dir=trial_dir,
            benchmark_name="bench",
            harness="h",
            dest_dir=tmp_path / "out",
            verify_queue=MagicMock(),
            experiment_name="exp1",
            redis_host="redis.local",
            sanitizer="address",
            source_mode="main_repo",
            use_inc_build=False,
        )

    assert state is not None
    assert mock_enqueue.call_args.kwargs["source_mode"] == "main_repo"
    assert mock_enqueue.call_args.kwargs["use_inc_build"] is False


def test_reeval_bug_finding_passes_inc_image_runtime_settings(tmp_path: Path) -> None:
    """Local bug-finding re-eval should use the same inc-image knobs as runner."""
    trial_dir = tmp_path / "trial-1"
    pov_dir = trial_dir / "output" / "povs"
    pov_dir.mkdir(parents=True)
    (pov_dir / "a.blob").write_bytes(b"blob")

    bench_dir = tmp_path / "bench"
    bench_dir.mkdir()
    oss_fuzz = tmp_path / "oss-fuzz"
    oss_fuzz.mkdir()

    mock_output = SimpleNamespace(results=[])
    mock_engine = MagicMock()
    mock_engine.verify_benchmark.return_value = mock_output

    with patch(
        "crsbench.evaluation.verification.VerificationEngine",
        return_value=mock_engine,
    ) as mock_engine_cls:
        _reeval_bug_finding(
            trial_dir=trial_dir,
            benchmark_path=bench_dir,
            oss_fuzz_path=oss_fuzz,
            harness="h",
            dest_dir=tmp_path / "out",
            source_mode="main_repo",
            jobs=None,
            cores_per_job=None,
            force_rebuild=False,
            use_inc_build=False,
            sanitizer="address",
            inc_image_policy="pull_only",
            inc_image_registry="ghcr.io/example/custom",
            inc_image_max_pull_bytes=456,
            inc_image_pull_timeout=78,
            local_image_prefix="custom-prefix",
        )

    assert mock_engine_cls.call_args.kwargs["source_mode"] == "main_repo"
    assert mock_engine_cls.call_args.kwargs["inc_image_policy"] == "pull_only"
    assert (
        mock_engine_cls.call_args.kwargs["inc_image_registry"]
        == "ghcr.io/example/custom"
    )
    assert mock_engine_cls.call_args.kwargs["inc_image_max_pull_bytes"] == 456
    assert mock_engine_cls.call_args.kwargs["inc_image_pull_timeout"] == 78
    assert mock_engine_cls.call_args.kwargs["local_image_prefix"] == "custom-prefix"


class TestAsyncPovDrain:
    """Tests for async POV drain behavior."""

    def test_async_drain_persists_raw_verdict_results(self, tmp_path: Path) -> None:
        trial_dir = tmp_path / "trial-1"
        trial_dir.mkdir(parents=True)
        pov_src = tmp_path / "pov0.blob"
        pov_src.write_bytes(b"blob")
        pov_hash = "abcdef0123456789"

        state = SimpleNamespace(
            trial_id="bench__h__trial-1",
            dest_dir=trial_dir,
            job_ids=["job-1", "job-2"],
            pov_hash_to_path={pov_hash: pov_src},
            benchmark_name="bench",
            experiment_name="bench",
        )
        verdict1 = {
            "trial_id": state.trial_id,
            "benchmark": "bench",
            "harness": "h",
            "verdict": {
                "pov_id": f"pov_a:{pov_hash}",
                "triggered_bug": True,
                "status": "cpv",
                "cpv_matches": ["cpv_0"],
                "variant_results": {},
                "crash_logs": {"fullbase": "crash-1"},
                "error": None,
            },
            "completed_at": 1.0,
        }
        verdict2 = {
            "trial_id": state.trial_id,
            "benchmark": "bench",
            "harness": "h",
            "verdict": {
                "pov_id": f"pov_b:{pov_hash}",
                "triggered_bug": True,
                "status": "cpv",
                "cpv_matches": ["cpv_0"],
                "variant_results": {},
                "crash_logs": {"fullbase": "crash-2"},
                "error": None,
            },
            "completed_at": 2.0,
        }

        with (
            patch(
                "crsbench.distributed.verify_queue.poll_single_pov_verdicts",
                side_effect=[([verdict1, verdict2], []), ([], [])],
            ) as mock_poll,
            patch("crsbench.evaluation.reeval.cli._save_pov_results") as mock_save,
            patch("crsbench.evaluation.reeval.cli._load_crs_run_start_time"),
            patch(
                "crsbench.evaluation.verification.pov.store.POVStore"
            ) as mock_store_cls,
        ):
            mock_store = MagicMock()
            mock_store.get_stats.return_value = {}
            mock_store_cls.return_value = mock_store
            mock_store_cls._extract_hash.side_effect = lambda pov_id: pov_id.rsplit(
                ":", 1
            )[1]

            count = _drain_all_async_results(
                _as_async_trial_states(state),
                "localhost",
            )

        assert count == 2
        assert mock_poll.call_args_list[0].kwargs["experiment_name"] == "bench"
        # Persisted output should keep raw queue verdict count.
        saved_results = mock_save.call_args.args[0]
        assert len(saved_results) == 2
        # Store population follows raw result set.
        assert mock_store.add_pov.call_count == 2
        mock_store.save.assert_called_once()

    def test_async_drain_rejects_mixed_experiment_names(self, tmp_path: Path) -> None:
        state_a = SimpleNamespace(
            trial_id="exp-a__bench__h__trial-1",
            dest_dir=tmp_path / "trial-1",
            job_ids=["job-1"],
            pov_hash_to_path={},
            benchmark_name="bench",
            experiment_name="exp-a",
        )
        state_b = SimpleNamespace(
            trial_id="exp-b__bench__h__trial-2",
            dest_dir=tmp_path / "trial-2",
            job_ids=["job-2"],
            pov_hash_to_path={},
            benchmark_name="bench",
            experiment_name="exp-b",
        )

        with pytest.raises(ValueError, match="same experiment_name"):
            _drain_all_async_results(
                _as_async_trial_states(state_a, state_b),
                "localhost",
            )

    def test_async_drain_preserves_crash_logs_when_pov_file_missing(
        self, tmp_path: Path
    ) -> None:
        trial_dir = tmp_path / "trial-1"
        trial_dir.mkdir(parents=True)
        pov_hash = "deadbeefdeadbeef"

        state = SimpleNamespace(
            trial_id="bench__h__trial-1",
            dest_dir=trial_dir,
            job_ids=["job-1"],
            pov_hash_to_path={},  # missing mapping on purpose
            benchmark_name="bench",
            experiment_name="bench",
        )
        verdict = {
            "trial_id": state.trial_id,
            "benchmark": "bench",
            "harness": "h",
            "verdict": {
                "pov_id": f"pov_missing:{pov_hash}",
                "triggered_bug": False,
                "status": "unintended_crash",
                "cpv_matches": [],
                "variant_results": {},
                "crash_logs": {"fullbase": "still-keep-this-log"},
                "error": None,
            },
            "completed_at": 1.0,
        }

        with (
            patch(
                "crsbench.distributed.verify_queue.poll_single_pov_verdicts",
                side_effect=[([verdict], []), ([], [])],
            ),
            patch("crsbench.evaluation.reeval.cli._save_pov_results"),
            patch("crsbench.evaluation.reeval.cli._load_crs_run_start_time"),
            patch(
                "crsbench.evaluation.verification.pov.store.POVStore"
            ) as mock_store_cls,
        ):
            mock_store = MagicMock()
            mock_store.get_stats.return_value = {}
            mock_store_cls.return_value = mock_store
            mock_store_cls._extract_hash.side_effect = lambda pov_id: pov_id.rsplit(
                ":", 1
            )[1]

            count = _drain_all_async_results(
                _as_async_trial_states(state),
                "localhost",
            )

        assert count == 1
        mock_store.store_crash_log.assert_called_once()
        mock_store.add_pov.assert_not_called()
        mock_store.add_pov_by_id.assert_called_once()
        mock_store.save.assert_called_once()

    def test_async_drain_isolates_per_result_store_failures(
        self, tmp_path: Path
    ) -> None:
        trial_dir = tmp_path / "trial-1"
        trial_dir.mkdir(parents=True)
        pov_src = tmp_path / "pov0.blob"
        pov_src.write_bytes(b"blob")
        pov_hash = "feedfacefeedface"

        state = SimpleNamespace(
            trial_id="bench__h__trial-1",
            dest_dir=trial_dir,
            job_ids=["job-1", "job-2"],
            pov_hash_to_path={pov_hash: pov_src},
            benchmark_name="bench",
            experiment_name="bench",
        )
        verdict1 = {
            "trial_id": state.trial_id,
            "benchmark": "bench",
            "harness": "h",
            "verdict": {
                "pov_id": f"pov_1:{pov_hash}",
                "triggered_bug": True,
                "status": "cpv",
                "cpv_matches": ["cpv_0"],
                "variant_results": {},
                "crash_logs": {"fullbase": "crash-1"},
                "error": None,
            },
            "completed_at": 1.0,
        }
        verdict2 = {
            "trial_id": state.trial_id,
            "benchmark": "bench",
            "harness": "h",
            "verdict": {
                "pov_id": f"pov_2:{pov_hash}",
                "triggered_bug": True,
                "status": "cpv",
                "cpv_matches": ["cpv_0"],
                "variant_results": {},
                "crash_logs": {"fullbase": "crash-2"},
                "error": None,
            },
            "completed_at": 2.0,
        }

        with (
            patch(
                "crsbench.distributed.verify_queue.poll_single_pov_verdicts",
                side_effect=[([verdict1, verdict2], []), ([], [])],
            ),
            patch("crsbench.evaluation.reeval.cli._save_pov_results") as mock_save,
            patch("crsbench.evaluation.reeval.cli._load_crs_run_start_time"),
            patch(
                "crsbench.evaluation.verification.pov.store.POVStore"
            ) as mock_store_cls,
        ):
            mock_store = MagicMock()
            mock_store.get_stats.return_value = {}
            mock_store.add_pov.side_effect = [RuntimeError("disk error"), None]
            mock_store_cls.return_value = mock_store
            mock_store_cls._extract_hash.side_effect = lambda pov_id: pov_id.rsplit(
                ":", 1
            )[1]

            count = _drain_all_async_results(
                _as_async_trial_states(state),
                "localhost",
            )

        assert count == 2
        # Drain should complete and still write collected verdict results.
        mock_save.assert_called_once()
        saved_results = mock_save.call_args.args[0]
        assert len(saved_results) == 2
        mock_store.save.assert_called_once()
        assert mock_store.add_pov.call_count == 2

    def test_async_drain_times_out_on_stuck_jobs(self, tmp_path: Path) -> None:
        trial_dir = tmp_path / "trial-1"
        trial_dir.mkdir(parents=True)
        state = SimpleNamespace(
            trial_id="bench__h__trial-1",
            dest_dir=trial_dir,
            job_ids=["job-1"],
            pov_hash_to_path={},
            job_to_pov_id={},
            benchmark_name="bench",
            experiment_name="bench",
        )

        with (
            patch(
                "crsbench.distributed.verify_queue.poll_single_pov_verdicts",
                return_value=([], ["job-1"]),
            ),
            patch(
                "crsbench.evaluation.reeval.cli.time.monotonic",
                side_effect=[0.0, 1.0],
            ),
            patch("crsbench.evaluation.reeval.cli._save_pov_results") as mock_save,
            pytest.raises(TimeoutError, match="Timed out draining async POV jobs"),
        ):
            _drain_all_async_results(
                _as_async_trial_states(state),
                "localhost",
                timeout_seconds=0.1,
            )

        mock_save.assert_not_called()

    def test_async_drain_timeout_persists_partial_results(self, tmp_path: Path) -> None:
        trial_dir = tmp_path / "trial-1"
        trial_dir.mkdir(parents=True)
        pov_src = tmp_path / "pov0.blob"
        pov_src.write_bytes(b"blob")
        pov_hash = "abcd1234efef9999"
        state = SimpleNamespace(
            trial_id="bench__h__trial-1",
            dest_dir=trial_dir,
            job_ids=["job-1", "job-2"],
            pov_hash_to_path={pov_hash: pov_src},
            job_to_pov_id={},
            benchmark_name="bench",
            experiment_name="bench",
        )
        verdict = {
            "trial_id": state.trial_id,
            "benchmark": "bench",
            "harness": "h",
            "verdict": {
                "pov_id": f"pov_1:{pov_hash}",
                "triggered_bug": True,
                "status": "cpv",
                "cpv_matches": ["cpv_0"],
                "variant_results": {},
                "crash_logs": {},
                "error": None,
            },
            "completed_at": 1.0,
        }

        with (
            patch(
                "crsbench.distributed.verify_queue.poll_single_pov_verdicts",
                return_value=([verdict], ["job-2"]),
            ),
            patch(
                "crsbench.evaluation.reeval.cli.time.monotonic",
                side_effect=[0.0, 0.0, 1.0],
            ),
            patch("crsbench.evaluation.reeval.cli._save_pov_results") as mock_save,
            patch("crsbench.evaluation.reeval.cli._load_crs_run_start_time"),
            patch(
                "crsbench.evaluation.verification.pov.store.POVStore"
            ) as mock_store_cls,
        ):
            mock_store = MagicMock()
            mock_store.get_stats.return_value = {}
            mock_store_cls.return_value = mock_store
            with pytest.raises(TimeoutError, match="Timed out draining async POV jobs"):
                _drain_all_async_results(
                    _as_async_trial_states(state),
                    "localhost",
                    timeout_seconds=0.1,
                )

        mock_save.assert_called_once()

    def test_async_drain_timeout_marks_pending_povs_as_error(
        self, tmp_path: Path
    ) -> None:
        """Timed out async POV drain should persist ERROR entries for pending jobs."""
        trial_dir = tmp_path / "trial-1"
        trial_dir.mkdir(parents=True)
        completed_hash = "abcd1234efef9999"
        pending_hash = "feedfacefeedface"
        completed_src = tmp_path / "pov0.blob"
        completed_src.write_bytes(b"blob")
        state = SimpleNamespace(
            trial_id="bench__h__trial-1",
            dest_dir=trial_dir,
            job_ids=["job-1", "job-2"],
            pov_hash_to_path={completed_hash: completed_src},
            job_to_pov_id={
                "job-1": f"pov_1:{completed_hash}",
                "job-2": f"pov_2:{pending_hash}",
            },
            benchmark_name="bench",
            experiment_name="bench",
        )
        verdict = {
            "trial_id": state.trial_id,
            "benchmark": "bench",
            "harness": "h",
            "verdict": {
                "pov_id": f"pov_1:{completed_hash}",
                "triggered_bug": True,
                "status": "cpv",
                "cpv_matches": ["cpv_0"],
                "variant_results": {},
                "crash_logs": {},
                "error": None,
            },
            "completed_at": 1.0,
        }

        with (
            patch(
                "crsbench.distributed.verify_queue.poll_single_pov_verdicts",
                return_value=([verdict], ["job-2"]),
            ),
            patch(
                "crsbench.evaluation.reeval.cli.time.monotonic",
                side_effect=[0.0, 0.0, 1.0],
            ),
            patch("crsbench.evaluation.reeval.cli._save_pov_results") as mock_save,
            patch("crsbench.evaluation.reeval.cli._load_crs_run_start_time"),
            patch(
                "crsbench.evaluation.verification.pov.store.POVStore"
            ) as mock_store_cls,
        ):
            mock_store = MagicMock()
            mock_store.get_stats.return_value = {}
            mock_store_cls.return_value = mock_store
            with pytest.raises(TimeoutError, match="Timed out draining async POV jobs"):
                _drain_all_async_results(
                    _as_async_trial_states(state),
                    "localhost",
                    timeout_seconds=0.1,
                )

        saved_results = mock_save.call_args.args[0]
        assert len(saved_results) == 2
        assert {result.status.value for result in saved_results} == {"cpv", "error"}
        mock_store.add_pov.assert_any_call(
            completed_src,
            PovVerificationStatus.CPV,
            ["cpv_0"],
            pov_hash=completed_hash,
        )
        mock_store.add_pov_by_id.assert_any_call(
            pending_hash,
            PovVerificationStatus.ERROR,
            [],
        )


class TestRunReevalCleanup:
    """Tests for run_reeval registry cleanup on async drain errors."""

    def _make_args(self, config_path: Path, oss_fuzz: Path) -> argparse.Namespace:
        return argparse.Namespace(
            experiment_config=config_path,
            oss_fuzz_path=oss_fuzz,
            source="pkgs",
            mode="snapshot",
            jobs=None,
            cores_per_job=None,
            force_rebuild=False,
            per_pov_verify_timeout=None,
            output=None,
            verbose=False,
        )

    def test_registry_cleanup_runs_when_async_patch_drain_raises(
        self, tmp_path: Path
    ) -> None:
        from crsbench.validation.schemas import TrialMode

        experiment_dir = tmp_path / "my-exp"
        trial_dir = experiment_dir / "crs" / "bench" / "patch_generation" / "trial-0"
        trial_dir.mkdir(parents=True)
        bench_dir = tmp_path / "benchmarks" / "test-bench"
        bench_dir.mkdir(parents=True)
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "experiment: my-exp\n"
            f"experiment_filestore: {tmp_path}\n"
            f"benchmarks_root: {tmp_path / 'benchmarks'}\n"
            "redis_host: localhost\n"
        )
        oss_fuzz = tmp_path / "oss-fuzz"
        oss_fuzz.mkdir()
        args = self._make_args(config_path, oss_fuzz)

        mock_trial = SimpleNamespace(
            status="valid",
            reeval_ready=True,
            reeval_reason="ready",
            trial_dir=trial_dir,
            benchmark="test-bench",
            harness="test-harness",
            mode=TrialMode.patch_generation,
            trial_num=0,
        )
        with (
            patch(
                "crsbench.reporting.snapshot_loader.discover_trials",
                return_value=[mock_trial],
            ),
            patch(
                "crsbench.distributed.runtime_session.DistributedRuntimeSession.for_reeval"
            ) as mock_for_reeval,
            patch(
                "crsbench.evaluation.reeval.cli._enqueue_trial_patches",
                return_value=SimpleNamespace(trial_id="t", job_ids=["j1"]),
            ),
            patch(
                "crsbench.evaluation.reeval.cli._drain_all_async_patch_results",
                side_effect=TimeoutError("stuck jobs"),
            ),
        ):
            mock_session = MagicMock()
            mock_session.trial_queue = MagicMock()
            mock_session.build_queue = MagicMock()
            mock_session.verify_queue = MagicMock()
            mock_session.registry.get_experiment.return_value = None
            mock_for_reeval.return_value = mock_session

            result = run_reeval(args)

        assert result == 1
        mock_session.cleanup.assert_called()

    def test_async_drain_error_forces_nonzero_exit_even_with_other_results(
        self, tmp_path: Path
    ) -> None:
        from crsbench.validation.schemas import TrialMode

        experiment_dir = tmp_path / "my-exp"
        patch_trial_dir = (
            experiment_dir / "crs" / "bench" / "patch_generation" / "trial-0"
        )
        bug_trial_dir = experiment_dir / "crs" / "bench" / "bug_finding" / "trial-1"
        patch_trial_dir.mkdir(parents=True)
        bug_trial_dir.mkdir(parents=True)
        bench_dir = tmp_path / "benchmarks" / "test-bench"
        bench_dir.mkdir(parents=True)
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "experiment: my-exp\n"
            f"experiment_filestore: {tmp_path}\n"
            f"benchmarks_root: {tmp_path / 'benchmarks'}\n"
            "redis_host: localhost\n"
        )
        oss_fuzz = tmp_path / "oss-fuzz"
        oss_fuzz.mkdir()
        args = self._make_args(config_path, oss_fuzz)

        patch_trial = SimpleNamespace(
            status="valid",
            reeval_ready=True,
            reeval_reason="ready",
            trial_dir=patch_trial_dir,
            benchmark="test-bench",
            harness="test-harness",
            mode=TrialMode.patch_generation,
            trial_num=0,
        )
        bug_trial = SimpleNamespace(
            status="valid",
            reeval_ready=True,
            reeval_reason="ready",
            trial_dir=bug_trial_dir,
            benchmark="test-bench",
            harness="test-harness",
            mode=TrialMode.bug_finding,
            trial_num=1,
        )
        with (
            patch(
                "crsbench.reporting.snapshot_loader.discover_trials",
                return_value=[patch_trial, bug_trial],
            ),
            patch(
                "crsbench.distributed.runtime_session.DistributedRuntimeSession.for_reeval"
            ) as mock_for_reeval,
            patch(
                "crsbench.evaluation.reeval.cli._enqueue_trial_povs",
                return_value=SimpleNamespace(
                    trial_id="test-bench__test-harness__trial-1", job_ids=["pov-1"]
                ),
            ),
            patch(
                "crsbench.evaluation.reeval.cli._enqueue_trial_patches",
                return_value=SimpleNamespace(
                    trial_id="test-bench__test-harness__trial-0", job_ids=["j1"]
                ),
            ),
            patch(
                "crsbench.evaluation.reeval.cli._drain_all_async_results",
                return_value=1,
            ),
            patch(
                "crsbench.evaluation.reeval.cli._drain_all_async_patch_results",
                side_effect=TimeoutError("stuck jobs"),
            ),
        ):
            mock_session = MagicMock()
            mock_session.trial_queue = MagicMock()
            mock_session.build_queue = MagicMock()
            mock_session.verify_queue = MagicMock()
            mock_session.registry.get_experiment.return_value = None
            mock_for_reeval.return_value = mock_session

            result = run_reeval(args)

        assert result == 1
