"""Unit tests for the re-eval CLI module."""

import argparse
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from crsbench.evaluation.reeval.cli import (
    _load_experiment_config,
    _resolve_benchmark_path,
    _resolve_experiment_dir,
    _resolve_output_dir,
    _save_patch_results,
    _save_pov_results,
    add_reeval_subparser,
    run_reeval,
)


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
        assert args.oss_fuzz is None
        assert args.source == "pkgs"
        assert args.build_workers is None
        assert args.verify_workers is None
        assert args.force_rebuild is False
        assert args.no_inc_build is False
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
                "--oss-fuzz",
                "/opt/oss-fuzz",
                "--source",
                "pkgs",
                "--build-workers",
                "4",
                "--verify-workers",
                "8",
                "--force-rebuild",
                "--no-inc-build",
                "--output",
                "/tmp/out",
                "-v",
            ]
        )
        assert args.oss_fuzz == Path("/opt/oss-fuzz")
        assert args.source == "pkgs"
        assert args.build_workers == 4
        assert args.verify_workers == 8
        assert args.force_rebuild is True
        assert args.no_inc_build is True
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
            oss_fuzz=oss_fuzz,
            source="main_repo",
            build_workers=None,
            verify_workers=None,
            force_rebuild=False,
            no_inc_build=False,
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
        }

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

        with patch(
            "crsbench.evaluation.reeval.cli._reeval_patch_generation"
        ) as mock_pg:
            mock_pg.return_value = 2
            result = run_reeval(args)

            mock_pg.assert_called_once()
            call_kwargs = mock_pg.call_args
            assert call_kwargs.kwargs["benchmark_path"] == bench_dir
            assert call_kwargs.kwargs["harness"] == "test-harness"
            assert result == 0

    def test_skips_missing_benchmark(self, tmp_path: Path) -> None:
        """Should skip trial when benchmark path doesn't exist."""
        experiment_dir = tmp_path / "my-exp"
        trial_dir = experiment_dir / "trial-0"
        trial_dir.mkdir(parents=True)

        metadata = self._make_trial_metadata(0, benchmark="nonexistent-bench")
        (trial_dir / "metadata.json").write_text(json.dumps(metadata))

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
