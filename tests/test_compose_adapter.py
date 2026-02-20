"""Unit tests for oss-crs adapter modules.

Tests config_gen.py, compose_common.py, OssCrsAdapter (both modes),
and ExperimentConfig crs_compose validation.

Requirements covered: ADAPT-02, COMPOSE-01, COMPOSE-02, COMPOSE-03,
COMPOSE-04, COMPOSE-05.
"""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from crsbench.evaluation.adapter import OssCrsAdapter
from crsbench.evaluation.adapter.compose_common import (
    docker_compose_down_cleanup,
    find_exchange_dir,
    find_submit_dir,
    read_crs_source_from_registry,
    run_oss_crs_build_target,
    run_oss_crs_prepare,
    run_oss_crs_run,
)
from crsbench.evaluation.adapter.config_gen import (
    CrsComposeCrsEntry,
    CrsComposeInfra,
    CrsComposeLlmConfig,
    CrsComposeSource,
    CrsComposeYaml,
)
from crsbench.evaluation.results import CRSExecutionResult
from crsbench.evaluation.runner import BenchmarkRunner
from crsbench.validation.schemas import HarnessFile

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

FACTORY_ARGS = {
    "crs_config_name": "test-crs",
    "oss_fuzz_path": Path("/tmp/fake/oss-fuzz"),
    "registry_dir": Path("/tmp/fake/registry"),
    "benchmarks_root": Path("/tmp/fake/benchmarks"),
    "crs_configs_dir": Path("/tmp/fake/configs"),
}


# ===========================================================================
# Config Generation (COMPOSE-05)
# ===========================================================================


class TestCrsComposeYaml:
    """Tests for CrsComposeYaml Pydantic model and to_yaml serialization."""

    def test_to_yaml_writes_valid_yaml(self, tmp_path: Path) -> None:
        yaml_path = tmp_path / "crs-compose.yaml"
        compose = CrsComposeYaml(
            docker_registry="ghcr.io/test",
            oss_crs_infra=CrsComposeInfra(cpuset="0-3", memory="8G"),
            crs_entries={
                "my-crs": CrsComposeCrsEntry(
                    source=CrsComposeSource(
                        url="https://github.com/test/crs.git", ref="main"
                    ),
                    cpuset="0-7",
                    memory="16G",
                ),
            },
        )
        compose.to_yaml(yaml_path)

        data = yaml.safe_load(yaml_path.read_text())
        assert isinstance(data, dict)

    def test_crs_entries_are_top_level_keys(self, tmp_path: Path) -> None:
        yaml_path = tmp_path / "crs-compose.yaml"
        compose = CrsComposeYaml(
            docker_registry="ghcr.io/test",
            oss_crs_infra=CrsComposeInfra(cpuset="0-3", memory="8G"),
            crs_entries={
                "my-crs": CrsComposeCrsEntry(
                    source=CrsComposeSource(url="https://example.com/crs.git"),
                    cpuset="0-3",
                    memory="8G",
                ),
            },
        )
        compose.to_yaml(yaml_path)

        data = yaml.safe_load(yaml_path.read_text())
        # CRS name must be a top-level key
        assert "my-crs" in data
        # Must NOT be nested under "crs_entries"
        assert "crs_entries" not in data
        assert data["my-crs"]["source"]["url"] == "https://example.com/crs.git"

    def test_multiple_crs_entries_as_top_level_keys(self, tmp_path: Path) -> None:
        yaml_path = tmp_path / "crs-compose.yaml"
        compose = CrsComposeYaml(
            docker_registry="ghcr.io/test",
            oss_crs_infra=CrsComposeInfra(cpuset="0-3", memory="8G"),
            crs_entries={
                "crs-alpha": CrsComposeCrsEntry(
                    source=CrsComposeSource(url="https://alpha.git"),
                    cpuset="0-3",
                    memory="8G",
                ),
                "crs-beta": CrsComposeCrsEntry(
                    source=CrsComposeSource(url="https://beta.git"),
                    cpuset="4-7",
                    memory="16G",
                ),
            },
        )
        compose.to_yaml(yaml_path)

        data = yaml.safe_load(yaml_path.read_text())
        assert "crs-alpha" in data
        assert "crs-beta" in data
        assert data["crs-alpha"]["source"]["url"] == "https://alpha.git"
        assert data["crs-beta"]["source"]["url"] == "https://beta.git"

    def test_reserved_keys_present(self, tmp_path: Path) -> None:
        yaml_path = tmp_path / "crs-compose.yaml"
        compose = CrsComposeYaml(
            docker_registry="ghcr.io/test",
            oss_crs_infra=CrsComposeInfra(cpuset="0-3", memory="8G"),
            crs_entries={},
        )
        compose.to_yaml(yaml_path)

        data = yaml.safe_load(yaml_path.read_text())
        assert "run_env" in data
        assert "docker_registry" in data
        assert "oss_crs_infra" in data

    def test_llm_config_omitted_when_none(self, tmp_path: Path) -> None:
        yaml_path = tmp_path / "crs-compose.yaml"
        compose = CrsComposeYaml(
            docker_registry="ghcr.io/test",
            oss_crs_infra=CrsComposeInfra(cpuset="0-3", memory="8G"),
            crs_entries={},
            llm_config=None,
        )
        compose.to_yaml(yaml_path)

        data = yaml.safe_load(yaml_path.read_text())
        assert "llm_config" not in data

    def test_llm_config_included_when_set(self, tmp_path: Path) -> None:
        yaml_path = tmp_path / "crs-compose.yaml"
        compose = CrsComposeYaml(
            docker_registry="ghcr.io/test",
            oss_crs_infra=CrsComposeInfra(cpuset="0-3", memory="8G"),
            crs_entries={},
            llm_config=CrsComposeLlmConfig(litellm_config="/etc/litellm.yaml"),
        )
        compose.to_yaml(yaml_path)

        data = yaml.safe_load(yaml_path.read_text())
        assert "llm_config" in data
        assert data["llm_config"]["litellm_config"] == "/etc/litellm.yaml"

    def test_crs_entry_names_preserved(self, tmp_path: Path) -> None:
        yaml_path = tmp_path / "crs-compose.yaml"
        names = ["CRS-With-Dashes", "crs_underscore", "crs123"]
        entries = {
            name: CrsComposeCrsEntry(
                source=CrsComposeSource(), cpuset="0-3", memory="8G"
            )
            for name in names
        }
        compose = CrsComposeYaml(
            docker_registry="ghcr.io/test",
            oss_crs_infra=CrsComposeInfra(cpuset="0-3", memory="8G"),
            crs_entries=entries,
        )
        compose.to_yaml(yaml_path)

        data = yaml.safe_load(yaml_path.read_text())
        for name in names:
            assert name in data


# ===========================================================================
# Registry Source Reading (COMPOSE-05)
# ===========================================================================


class TestReadCrsSourceFromRegistry:
    """Tests for read_crs_source_from_registry utility."""

    def test_reads_source_url_and_ref(self, tmp_path: Path) -> None:
        crs_dir = tmp_path / "my-crs"
        crs_dir.mkdir()
        pkg_yaml = crs_dir / "pkg.yaml"
        pkg_yaml.write_text(
            yaml.dump(
                {
                    "source": {
                        "url": "https://github.com/team/crs.git",
                        "ref": "v1.0.0",
                    }
                }
            )
        )

        result = read_crs_source_from_registry(tmp_path, "my-crs")
        assert isinstance(result, CrsComposeSource)
        assert result.url == "https://github.com/team/crs.git"
        assert result.ref == "v1.0.0"

    def test_raises_file_not_found_when_pkg_yaml_missing(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="pkg.yaml"):
            read_crs_source_from_registry(tmp_path, "nonexistent-crs")

    def test_raises_value_error_when_source_key_missing(self, tmp_path: Path) -> None:
        crs_dir = tmp_path / "bad-crs"
        crs_dir.mkdir()
        (crs_dir / "pkg.yaml").write_text(yaml.dump({"name": "bad-crs"}))

        with pytest.raises(ValueError, match="source"):
            read_crs_source_from_registry(tmp_path, "bad-crs")


# ===========================================================================
# Compose Common (COMPOSE-01, COMPOSE-02, COMPOSE-03)
# ===========================================================================


class TestComposeCommon:
    """Tests for compose_common.py subprocess wrappers and utilities."""

    @patch("crsbench.evaluation.adapter.compose_common.subprocess.run")
    def test_prepare_builds_correct_command(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="ok", stderr=""
        )
        compose_file = tmp_path / "crs-compose.yaml"
        work_dir = tmp_path / "work"

        run_oss_crs_prepare(compose_file, work_dir)

        args = mock_run.call_args[0][0]
        assert args == [
            "oss-crs",
            "prepare",
            "--compose-file",
            str(compose_file),
            "--work-dir",
            str(work_dir),
        ]

    @patch("crsbench.evaluation.adapter.compose_common.subprocess.run")
    def test_build_target_builds_correct_command(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="ok", stderr=""
        )
        compose_file = tmp_path / "crs-compose.yaml"
        work_dir = tmp_path / "work"
        target = tmp_path / "benchmark"

        run_oss_crs_build_target(compose_file, work_dir, target)

        args = mock_run.call_args[0][0]
        assert args == [
            "oss-crs",
            "build-target",
            "--compose-file",
            str(compose_file),
            "--work-dir",
            str(work_dir),
            "--target-proj-path",
            str(target),
        ]

    @patch("crsbench.evaluation.adapter.compose_common.run_with_graceful_timeout")
    def test_run_builds_correct_command(
        self, mock_rwgt: MagicMock, tmp_path: Path
    ) -> None:
        mock_rwgt.return_value = ("out", "err", 0, False)
        compose_file = tmp_path / "crs-compose.yaml"
        work_dir = tmp_path / "work"
        target = tmp_path / "benchmark"

        run_oss_crs_run(compose_file, work_dir, target, "test_harness", timeout=3600)

        cmd = mock_rwgt.call_args[0][0]
        assert cmd == [
            "oss-crs",
            "run",
            "--compose-file",
            str(compose_file),
            "--work-dir",
            str(work_dir),
            "--target-proj-path",
            str(target),
            "--target-harness",
            "test_harness",
            "--timeout",
            "3600",
        ]

    @patch("crsbench.evaluation.adapter.compose_common.run_with_graceful_timeout")
    def test_run_appends_optional_args(
        self, mock_rwgt: MagicMock, tmp_path: Path
    ) -> None:
        mock_rwgt.return_value = ("out", "err", 0, False)
        compose_file = tmp_path / "crs-compose.yaml"
        work_dir = tmp_path / "work"
        target = tmp_path / "benchmark"
        pov_dir = tmp_path / "povs"
        diff = tmp_path / "ref.diff"
        seeds = tmp_path / "seeds"

        run_oss_crs_run(
            compose_file,
            work_dir,
            target,
            "harness",
            timeout=3600,
            pov_dir=pov_dir,
            diff=diff,
            seed_dir=seeds,
        )

        cmd = mock_rwgt.call_args[0][0]
        assert "--pov-dir" in cmd
        assert str(pov_dir) in cmd
        assert "--diff" in cmd
        assert str(diff) in cmd
        assert "--seed-dir" in cmd
        assert str(seeds) in cmd

    @patch("crsbench.evaluation.adapter.compose_common.run_with_graceful_timeout")
    def test_run_omits_optional_args_when_none(
        self, mock_rwgt: MagicMock, tmp_path: Path
    ) -> None:
        mock_rwgt.return_value = ("out", "err", 0, False)
        compose_file = tmp_path / "crs-compose.yaml"

        run_oss_crs_run(
            compose_file,
            tmp_path / "work",
            tmp_path / "bench",
            "harness",
            timeout=3600,
        )

        cmd = mock_rwgt.call_args[0][0]
        assert "--pov-dir" not in cmd
        assert "--diff" not in cmd
        assert "--seed-dir" not in cmd

    @patch("crsbench.evaluation.adapter.compose_common.run_with_graceful_timeout")
    def test_run_passes_stop_event(self, mock_rwgt: MagicMock, tmp_path: Path) -> None:
        mock_rwgt.return_value = ("out", "err", 0, False)
        stop = threading.Event()

        run_oss_crs_run(
            tmp_path / "compose.yaml",
            tmp_path / "work",
            tmp_path / "bench",
            "harness",
            timeout=3600,
            stop_event=stop,
        )

        assert mock_rwgt.call_args[1]["stop_event"] is stop

    @patch("crsbench.evaluation.adapter.compose_common.subprocess.run")
    def test_docker_cleanup_never_raises(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        mock_run.side_effect = OSError("docker not found")
        compose_dir = tmp_path / "sub"
        compose_dir.mkdir()
        (compose_dir / "docker-compose.yaml").write_text("version: '3'")

        # Must not raise
        docker_compose_down_cleanup(tmp_path)

    @patch("crsbench.evaluation.adapter.compose_common.subprocess.run")
    def test_docker_cleanup_calls_down_for_each_compose_file(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        sub1 = tmp_path / "a"
        sub1.mkdir()
        (sub1 / "docker-compose.yaml").write_text("version: '3'")
        sub2 = tmp_path / "b"
        sub2.mkdir()
        (sub2 / "docker-compose.yml").write_text("version: '3'")

        docker_compose_down_cleanup(tmp_path)

        # 2 compose-down calls + 1 docker network prune call
        assert mock_run.call_count == 3

    def test_find_submit_dir_oss_crs_convention(self, tmp_path: Path) -> None:
        """Verify find_submit_dir matches oss-crs CRS.get_submit_dir() layout.

        oss-crs creates SUBMIT_DIR at:
          crs_compose/{config_hash}/{sanitizer}/runs/{run_id}/crs/{crs_name}/{target_key}/SUBMIT_DIR/{harness}/

        The glob pattern must explicitly match each level so that a layout
        change in oss-crs causes a visible failure.
        """
        submit = (
            tmp_path
            / "crs_compose"
            / "8d20a8aeb804"  # config_hash
            / "address"  # sanitizer
            / "runs"
            / "17715598561c"  # run_id
            / "crs"
            / "my-crs"  # crs_name
            / "afc-wireshark-full-01_ddaef6f5de3a"  # target_key
            / "SUBMIT_DIR"
            / "handler_ber"  # harness
        )
        submit.mkdir(parents=True)
        (submit / "povs").mkdir()
        (submit / "povs" / "pov_0.bin").write_bytes(b"\x00")

        result = find_submit_dir(tmp_path, "my-crs", "handler_ber")
        assert result is not None
        assert result == submit

    def test_find_submit_dir_rejects_wrong_layout(self, tmp_path: Path) -> None:
        """Ensure the explicit pattern does NOT match arbitrary nesting.

        If someone accidentally puts crs/ at a wrong depth, the pattern
        should not match — unlike a loose ``**`` glob.
        """
        wrong = (
            tmp_path
            / "crs_compose"
            / "abc123"
            / "crs"  # wrong: crs/ is directly under hash, no sanitizer/runs
            / "my-crs"
            / "target_img"
            / "SUBMIT_DIR"
            / "harness1"
        )
        wrong.mkdir(parents=True)

        result = find_submit_dir(tmp_path, "my-crs", "harness1")
        assert result is None

    def test_find_submit_dir_returns_none_when_no_match(self, tmp_path: Path) -> None:
        result = find_submit_dir(tmp_path, "no-crs", "no-harness")
        assert result is None

    def test_find_exchange_dir_oss_crs_convention(self, tmp_path: Path) -> None:
        """Verify find_exchange_dir matches oss-crs CRS.get_exchange_dir() layout.

        oss-crs creates EXCHANGE_DIR at:
          crs_compose/{config_hash}/{sanitizer}/runs/{run_id}/EXCHANGE_DIR/{target_key}/{harness}/
        """
        exchange = (
            tmp_path
            / "crs_compose"
            / "abc123"
            / "address"
            / "runs"
            / "run-0"
            / "EXCHANGE_DIR"
            / "target_1"
            / "handler_ber"
        )
        exchange.mkdir(parents=True)
        (exchange / "povs").mkdir()
        (exchange / "povs" / "pov_0.bin").write_bytes(b"\x00")

        result = find_exchange_dir(tmp_path, "handler_ber")
        assert result is not None
        assert result == exchange

    def test_find_exchange_dir_returns_none_when_no_match(self, tmp_path: Path) -> None:
        result = find_exchange_dir(tmp_path, "no-harness")
        assert result is None

    def test_find_exchange_dir_no_crs_name_needed(self, tmp_path: Path) -> None:
        """EXCHANGE_DIR is CRS-agnostic; ensure it works without crs_name."""
        exchange = (
            tmp_path
            / "crs_compose"
            / "hash1"
            / "address"
            / "runs"
            / "run-1"
            / "EXCHANGE_DIR"
            / "proj_target"
            / "fuzz_target"
        )
        exchange.mkdir(parents=True)

        result = find_exchange_dir(tmp_path, "fuzz_target")
        assert result is not None
        assert "EXCHANGE_DIR" in str(result)


# ===========================================================================
# OssCrsAdapter bug-finding (ADAPT-02, COMPOSE-01, COMPOSE-02, COMPOSE-03, COMPOSE-04)
# ===========================================================================


class TestOssCrsAdapterBugFindFull:
    """Comprehensive tests for OssCrsAdapter (bug-finding) lifecycle."""

    def _make_adapter(self, tmp_path: Path) -> OssCrsAdapter:
        """Create adapter with registry dir containing a valid pkg.yaml."""
        registry = tmp_path / "registry"
        crs_dir = registry / "test-crs"
        crs_dir.mkdir(parents=True)
        (crs_dir / "pkg.yaml").write_text(
            yaml.dump(
                {
                    "source": {
                        "url": "https://github.com/team/crs.git",
                        "ref": "main",
                    }
                }
            )
        )
        return OssCrsAdapter(
            crs_config_name="test-crs",
            oss_fuzz_path=tmp_path / "oss-fuzz",
            registry_dir=registry,
            benchmarks_root=tmp_path / "benchmarks",
            crs_configs_dir=tmp_path / "configs",
            mode="bug-finding",
        )

    def test_configure_stores_fields(self, tmp_path: Path) -> None:
        adapter = self._make_adapter(tmp_path)
        adapter.configure(
            {
                "build_timeout": 900,
                "run_timeout": 1800,
                "docker_registry": "ghcr.io/team",
                "oss_crs_cmd": "/opt/oss-crs",
                "oss_crs_infra_cpuset": "0-15",
                "oss_crs_infra_memory": "32G",
            }
        )
        assert adapter._build_timeout == 900
        assert adapter._run_timeout == 1800
        assert adapter._docker_registry == "ghcr.io/team"
        assert adapter._oss_crs_cmd == "/opt/oss-crs"
        assert adapter._oss_crs_infra_cpuset == "0-15"
        assert adapter._oss_crs_infra_memory == "32G"

    @patch("crsbench.evaluation.adapter.compose_common.subprocess.run")
    def test_build_calls_prepare_then_build_target(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="ok", stderr=""
        )
        adapter = self._make_adapter(tmp_path)
        adapter.configure({"docker_registry": "ghcr.io/t"})
        bench = tmp_path / "benchmarks" / "proj1"
        bench.mkdir(parents=True)
        trial = tmp_path / "trial"
        trial.mkdir()

        adapter.build(bench, trial)

        # Should call subprocess.run twice: once for prepare, once for build-target
        assert mock_run.call_count == 2
        first_cmd = mock_run.call_args_list[0][0][0]
        second_cmd = mock_run.call_args_list[1][0][0]
        assert first_cmd[1] == "prepare"
        assert second_cmd[1] == "build-target"

    @patch("crsbench.evaluation.adapter.compose_common.subprocess.run")
    def test_build_skips_if_already_built(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="ok", stderr=""
        )
        adapter = self._make_adapter(tmp_path)
        adapter.configure({"docker_registry": "ghcr.io/t"})
        bench = tmp_path / "benchmarks" / "proj1"
        bench.mkdir(parents=True)
        trial = tmp_path / "trial"
        trial.mkdir()

        adapter.build(bench, trial)
        mock_run.reset_mock()

        # Second call should skip
        adapter.build(bench, trial)
        mock_run.assert_not_called()

    @patch("crsbench.evaluation.adapter.compose_common.subprocess.run")
    def test_build_raises_when_prepare_fails(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="prepare error"
        )
        adapter = self._make_adapter(tmp_path)
        adapter.configure({"docker_registry": "ghcr.io/t"})
        bench = tmp_path / "benchmarks" / "proj1"
        bench.mkdir(parents=True)

        with pytest.raises(RuntimeError, match="prepare failed"):
            adapter.build(bench, tmp_path / "trial")

    @patch("crsbench.evaluation.adapter.compose_common.subprocess.run")
    def test_build_raises_when_build_target_fails(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        # prepare succeeds, build-target fails
        mock_run.side_effect = [
            subprocess.CompletedProcess(args=[], returncode=0, stdout="ok", stderr=""),
            subprocess.CompletedProcess(
                args=[], returncode=1, stdout="", stderr="build error"
            ),
        ]
        adapter = self._make_adapter(tmp_path)
        adapter.configure({"docker_registry": "ghcr.io/t"})
        bench = tmp_path / "benchmarks" / "proj1"
        bench.mkdir(parents=True)

        with pytest.raises(RuntimeError, match="build-target failed"):
            adapter.build(bench, tmp_path / "trial")

    @patch("crsbench.evaluation.adapter.compose_common.subprocess.run")
    def test_generate_compose_yaml_calls_registry(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        """_generate_compose_yaml reads CRS source from registry."""
        adapter = self._make_adapter(tmp_path)
        adapter.configure({"docker_registry": "ghcr.io/t"})
        trial = tmp_path / "trial"

        compose_path = adapter._generate_compose_yaml(trial)

        assert compose_path.exists()
        data = yaml.safe_load(compose_path.read_text())
        assert "test-crs" in data
        assert data["test-crs"]["source"]["url"] == "https://github.com/team/crs.git"

    @patch("crsbench.evaluation.adapter.compose_common.run_with_graceful_timeout")
    @patch("crsbench.evaluation.adapter.compose_common.subprocess.run")
    def test_run_calls_oss_crs_run(
        self,
        mock_subprocess: MagicMock,
        mock_rwgt: MagicMock,
        tmp_path: Path,
    ) -> None:
        mock_subprocess.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="ok", stderr=""
        )
        mock_rwgt.return_value = ("output", "", 0, False)

        adapter = self._make_adapter(tmp_path)
        adapter.configure({"docker_registry": "ghcr.io/t"})
        bench = tmp_path / "benchmarks" / "proj1"
        bench.mkdir(parents=True)
        trial = tmp_path / "trial"
        trial.mkdir()

        adapter.build(bench, trial)
        harness = MagicMock()
        harness.name = "fuzz_target"

        result = adapter.run(bench, harness, trial)

        mock_rwgt.assert_called_once()
        cmd = mock_rwgt.call_args[0][0]
        assert cmd[1] == "run"
        assert "--target-harness" in cmd
        assert "fuzz_target" in cmd
        assert isinstance(result, CRSExecutionResult)
        assert result.success is True

    @patch("crsbench.evaluation.adapter.compose_common.run_with_graceful_timeout")
    @patch("crsbench.evaluation.adapter.compose_common.subprocess.run")
    def test_run_docker_cleanup_in_finally(
        self,
        mock_subprocess: MagicMock,
        mock_rwgt: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Docker cleanup runs even when run() raises."""
        mock_subprocess.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="ok", stderr=""
        )
        mock_rwgt.side_effect = OSError("process error")

        adapter = self._make_adapter(tmp_path)
        adapter.configure({"docker_registry": "ghcr.io/t"})
        bench = tmp_path / "benchmarks" / "proj1"
        bench.mkdir(parents=True)
        trial = tmp_path / "trial"
        trial.mkdir()

        adapter.build(bench, trial)

        # Create a docker-compose file in workdir to verify cleanup is attempted
        work_dir = adapter._work_dir
        assert work_dir is not None
        dc_file = work_dir / "docker-compose.yaml"
        dc_file.write_text("version: '3'")

        harness = MagicMock()
        harness.name = "fuzz_target"

        with pytest.raises(OSError, match="process error"):
            adapter.run(bench, harness, trial)

        # subprocess.run should have been called for docker compose down
        # and docker network prune (the last two calls after the build calls)
        all_calls = [c[0][0] for c in mock_subprocess.call_args_list]
        down_calls = [c for c in all_calls if "down" in c]
        assert len(down_calls) >= 1
        prune_calls = [c for c in all_calls if "prune" in c]
        assert len(prune_calls) >= 1

    @patch("crsbench.evaluation.adapter.compose_common.run_with_graceful_timeout")
    @patch("crsbench.evaluation.adapter.compose_common.subprocess.run")
    def test_run_passes_stop_event(
        self,
        mock_subprocess: MagicMock,
        mock_rwgt: MagicMock,
        tmp_path: Path,
    ) -> None:
        mock_subprocess.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="ok", stderr=""
        )
        mock_rwgt.return_value = ("out", "", 0, False)

        adapter = self._make_adapter(tmp_path)
        adapter.configure({"docker_registry": "ghcr.io/t"})
        bench = tmp_path / "benchmarks" / "proj1"
        bench.mkdir(parents=True)
        trial = tmp_path / "trial"
        trial.mkdir()

        adapter.build(bench, trial)
        harness = MagicMock()
        harness.name = "fuzz_target"
        stop = threading.Event()

        adapter.run(bench, harness, trial, stop_event=stop)

        assert mock_rwgt.call_args[1]["stop_event"] is stop

    @patch("crsbench.evaluation.adapter.compose_common.run_with_graceful_timeout")
    @patch("crsbench.evaluation.adapter.compose_common.subprocess.run")
    def test_run_returns_execution_result(
        self,
        mock_subprocess: MagicMock,
        mock_rwgt: MagicMock,
        tmp_path: Path,
    ) -> None:
        mock_subprocess.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="ok", stderr=""
        )
        mock_rwgt.return_value = ("stdout-data", "stderr-data", 1, True)

        adapter = self._make_adapter(tmp_path)
        adapter.configure({"docker_registry": "ghcr.io/t"})
        bench = tmp_path / "benchmarks" / "proj1"
        bench.mkdir(parents=True)
        trial = tmp_path / "trial"
        trial.mkdir()

        adapter.build(bench, trial)
        harness = MagicMock()
        harness.name = "fuzz_target"

        result = adapter.run(bench, harness, trial)

        assert result.harness_name == "fuzz_target"
        assert result.success is False
        assert result.timed_out is True
        assert result.output == "stdout-data"
        assert result.error == "stderr-data"

    def test_collect_results_copies_povs(self, tmp_path: Path) -> None:
        adapter = self._make_adapter(tmp_path)
        adapter.configure({"docker_registry": "ghcr.io/t"})

        # Set up work_dir with SUBMIT_DIR matching oss-crs convention
        work_dir = tmp_path / "work"
        submit = (
            work_dir
            / "crs_compose"
            / "hash123"  # config_hash
            / "address"  # sanitizer
            / "runs"
            / "run001"  # run_id
            / "crs"
            / "test-crs"
            / "target_img"
            / "SUBMIT_DIR"
            / "harness1"
        )
        pov_dir = submit / "povs"
        pov_dir.mkdir(parents=True)
        (pov_dir / "crash-001").write_bytes(b"\x00" * 16)
        (pov_dir / "crash-002").write_bytes(b"\xff" * 8)

        adapter._work_dir = work_dir
        trial = tmp_path / "trial_out"

        metadata = adapter.collect_results(trial, "harness1")

        assert metadata["type"] == "bug-finding"
        output_dir = Path(metadata["output_dir"])
        assert (output_dir / "povs" / "crash-001").exists()
        assert (output_dir / "povs" / "crash-002").exists()

    def test_collect_results_handles_missing_submit_dir(self, tmp_path: Path) -> None:
        adapter = self._make_adapter(tmp_path)
        adapter._work_dir = tmp_path / "empty-work"
        adapter._work_dir.mkdir()

        metadata = adapter.collect_results(tmp_path / "trial", "harness1")
        assert metadata["submit_dir"] is None

    def test_collect_results_handles_none_work_dir(self, tmp_path: Path) -> None:
        adapter = self._make_adapter(tmp_path)
        # work_dir is None (build never called)
        metadata = adapter.collect_results(tmp_path / "trial", "harness1")
        assert metadata["submit_dir"] is None


# ===========================================================================
# OssCrsAdapter bug-fixing (ADAPT-02, COMPOSE-03, COMPOSE-04)
# ===========================================================================


class TestOssCrsAdapterBugFixFull:
    """Comprehensive tests for OssCrsAdapter (bug-fixing) lifecycle."""

    def _make_adapter(self, tmp_path: Path) -> OssCrsAdapter:
        """Create adapter with registry dir containing a valid pkg.yaml."""
        registry = tmp_path / "registry"
        crs_dir = registry / "test-crs"
        crs_dir.mkdir(parents=True)
        (crs_dir / "pkg.yaml").write_text(
            yaml.dump(
                {
                    "source": {
                        "url": "https://github.com/team/crs.git",
                        "ref": "main",
                    }
                }
            )
        )
        return OssCrsAdapter(
            crs_config_name="test-crs",
            oss_fuzz_path=tmp_path / "oss-fuzz",
            registry_dir=registry,
            benchmarks_root=tmp_path / "benchmarks",
            crs_configs_dir=tmp_path / "configs",
            mode="bug-fixing",
        )

    @patch("crsbench.evaluation.adapter.compose_common.run_with_graceful_timeout")
    @patch("crsbench.evaluation.adapter.compose_common.subprocess.run")
    def test_run_passes_pov_dir(
        self,
        mock_subprocess: MagicMock,
        mock_rwgt: MagicMock,
        tmp_path: Path,
    ) -> None:
        mock_subprocess.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="ok", stderr=""
        )
        mock_rwgt.return_value = ("out", "", 0, False)

        adapter = self._make_adapter(tmp_path)
        adapter.configure({"docker_registry": "ghcr.io/t"})
        bench = tmp_path / "benchmarks" / "proj1"
        bench.mkdir(parents=True)
        trial = tmp_path / "trial"
        trial.mkdir()

        # Create pov directory in trial
        pov_dir = trial / "povs"
        pov_dir.mkdir()
        (pov_dir / "crash-001").write_bytes(b"\x00" * 8)

        adapter.build(bench, trial)
        harness = MagicMock()
        harness.name = "fuzz_target"

        adapter.run(bench, harness, trial)

        cmd = mock_rwgt.call_args[0][0]
        assert "--pov-dir" in cmd
        assert str(pov_dir) in cmd

    @patch("crsbench.evaluation.adapter.compose_common.run_with_graceful_timeout")
    @patch("crsbench.evaluation.adapter.compose_common.subprocess.run")
    def test_run_passes_diff(
        self,
        mock_subprocess: MagicMock,
        mock_rwgt: MagicMock,
        tmp_path: Path,
    ) -> None:
        mock_subprocess.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="ok", stderr=""
        )
        mock_rwgt.return_value = ("out", "", 0, False)

        adapter = self._make_adapter(tmp_path)
        adapter.configure({"docker_registry": "ghcr.io/t"})
        bench = tmp_path / "benchmarks" / "proj1"
        bench.mkdir(parents=True)
        trial = tmp_path / "trial"
        trial.mkdir()

        # Create ref.diff
        diff_path = trial / "ref.diff"
        diff_path.write_text("--- a/file\n+++ b/file\n")

        adapter.build(bench, trial)
        harness = MagicMock()
        harness.name = "fuzz_target"

        adapter.run(bench, harness, trial)

        cmd = mock_rwgt.call_args[0][0]
        assert "--diff" in cmd
        assert str(diff_path) in cmd

    @patch("crsbench.evaluation.adapter.compose_common.run_with_graceful_timeout")
    @patch("crsbench.evaluation.adapter.compose_common.subprocess.run")
    def test_run_respects_stop_event(
        self,
        mock_subprocess: MagicMock,
        mock_rwgt: MagicMock,
        tmp_path: Path,
    ) -> None:
        mock_subprocess.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="ok", stderr=""
        )
        mock_rwgt.return_value = ("out", "", 0, False)

        adapter = self._make_adapter(tmp_path)
        adapter.configure({"docker_registry": "ghcr.io/t"})
        bench = tmp_path / "benchmarks" / "proj1"
        bench.mkdir(parents=True)
        trial = tmp_path / "trial"
        trial.mkdir()
        adapter.build(bench, trial)

        harness = MagicMock()
        harness.name = "fuzz_target"
        stop = threading.Event()

        adapter.run(bench, harness, trial, stop_event=stop)

        assert mock_rwgt.call_args[1]["stop_event"] is stop

    def test_collect_results_copies_patches(self, tmp_path: Path) -> None:
        adapter = self._make_adapter(tmp_path)
        adapter.configure({"docker_registry": "ghcr.io/t"})

        # Set up SUBMIT_DIR matching oss-crs convention
        work_dir = tmp_path / "work"
        submit = (
            work_dir
            / "crs_compose"
            / "hash456"  # config_hash
            / "address"  # sanitizer
            / "runs"
            / "run001"  # run_id
            / "crs"
            / "test-crs"
            / "target_img"
            / "SUBMIT_DIR"
            / "harness1"
        )
        patch_dir = submit / "patches"
        patch_dir.mkdir(parents=True)
        (patch_dir / "fix.patch").write_text("--- a/bug.c\n+++ b/bug.c\n")

        adapter._work_dir = work_dir
        trial = tmp_path / "trial_out"

        metadata = adapter.collect_results(trial, "harness1")

        assert metadata["type"] == "bug-fixing"
        output_dir = Path(metadata["output_dir"])
        assert (output_dir / "patches" / "fix.patch").exists()

    def test_collect_results_returns_patches_list(self, tmp_path: Path) -> None:
        adapter = self._make_adapter(tmp_path)
        adapter.configure({"docker_registry": "ghcr.io/t"})

        work_dir = tmp_path / "work"
        submit = (
            work_dir
            / "crs_compose"
            / "hash789"  # config_hash
            / "address"  # sanitizer
            / "runs"
            / "run001"  # run_id
            / "crs"
            / "test-crs"
            / "target_img"
            / "SUBMIT_DIR"
            / "harness1"
        )
        patch_dir = submit / "patches"
        patch_dir.mkdir(parents=True)
        (patch_dir / "fix1.patch").write_text("patch1")
        (patch_dir / "fix2.patch").write_text("patch2")

        adapter._work_dir = work_dir
        trial = tmp_path / "trial_out"

        metadata = adapter.collect_results(trial, "harness1")

        assert "patches" in metadata
        assert len(metadata["patches"]) == 2


# ===========================================================================
# ExperimentConfig Schema Validation (COMPOSE-05)
# ===========================================================================


class TestExperimentConfigComposeValidation:
    """Tests for ExperimentConfig crs_compose validation."""

    def _base_config(self) -> dict:
        return {
            "experiment": "test",
            "trials": 1,
            "mode": "delta",
            "max_total_time": 86400,
            "difficulty_level": 1,
            "experiment_filestore": Path("/tmp/store"),
            "report_filestore": Path("/tmp/report"),
            "crses": ["crs1"],
            "benchmarks": ["bench1"],
        }

    def test_accepts_oss_crs_adapter_with_crs_compose(self) -> None:
        from crsbench.validation.schemas import (
            AdapterType,
            CrsComposeConfig,
            ExperimentConfig,
        )

        cfg = self._base_config()
        cfg["adapter"] = AdapterType.OSS_CRS
        cfg["crs_compose"] = CrsComposeConfig(docker_registry="ghcr.io/test")

        config = ExperimentConfig(**cfg)
        assert config.crs_compose is not None
        assert config.crs_compose.docker_registry == "ghcr.io/test"

    def test_accepts_oss_crs_adapter_without_crs_compose(self) -> None:
        from crsbench.validation.schemas import AdapterType, ExperimentConfig

        cfg = self._base_config()
        cfg["adapter"] = AdapterType.OSS_CRS

        config = ExperimentConfig(**cfg)
        assert config.crs_compose is None

    def test_rejects_invalid_adapter_value(self) -> None:
        from crsbench.validation.schemas import ExperimentConfig
        from pydantic import ValidationError

        cfg = self._base_config()
        cfg["adapter"] = "crs-compose-bugfind"

        with pytest.raises(ValidationError):
            ExperimentConfig(**cfg)


class TestCollectResultsWiring:
    """Tests for collect_results() wiring in BenchmarkRunner."""

    @staticmethod
    def _make_runner_with_adapter(adapter: MagicMock) -> BenchmarkRunner:
        return BenchmarkRunner(adapter=adapter, snapshot_period=0)

    @staticmethod
    def _make_harness() -> HarnessFile:
        return HarnessFile(name="fuzz_target", path="/src/fuzz_target.c")

    @staticmethod
    def _make_success_result() -> CRSExecutionResult:
        return CRSExecutionResult(
            harness_name="fuzz_target",
            execution_time=1.0,
            success=True,
            output="ok",
        )

    @staticmethod
    def _make_failure_result() -> CRSExecutionResult:
        return CRSExecutionResult(
            harness_name="fuzz_target",
            execution_time=1.0,
            success=False,
            output="failed",
            error="build error",
        )

    def test_collect_results_called_after_successful_run(self, tmp_path: Path) -> None:
        adapter = MagicMock()
        adapter.run.return_value = self._make_success_result()
        adapter.collect_results.return_value = {"type": "bug-finding"}

        runner = self._make_runner_with_adapter(adapter)
        trial_dir = tmp_path / "trial"
        trial_dir.mkdir()
        harness = self._make_harness()

        runner._execute_crs_with_managers(
            harness=harness,
            benchmark_path=tmp_path,
            trial_output_dir=trial_dir,
            trial_start_time=0.0,
        )

        adapter.collect_results.assert_called_once_with(trial_dir, "fuzz_target")

    def test_collect_results_called_even_on_failed_run(self, tmp_path: Path) -> None:
        """collect_results() is always called regardless of exit code.

        oss-crs run returns non-zero when Docker containers exit non-zero
        (e.g. fuzzer killed by timeout), but POVs/patches may still exist in
        SUBMIT_DIR and must be collected.
        """
        adapter = MagicMock()
        adapter.run.return_value = self._make_failure_result()

        runner = self._make_runner_with_adapter(adapter)
        trial_dir = tmp_path / "trial"
        trial_dir.mkdir()
        harness = self._make_harness()

        runner._execute_crs_with_managers(
            harness=harness,
            benchmark_path=tmp_path,
            trial_output_dir=trial_dir,
            trial_start_time=0.0,
        )

        adapter.collect_results.assert_called_once_with(trial_dir, "fuzz_target")

    def test_collect_results_failure_does_not_fail_trial(self, tmp_path: Path) -> None:
        adapter = MagicMock()
        adapter.run.return_value = self._make_success_result()
        adapter.collect_results.side_effect = RuntimeError("collect failed")

        runner = self._make_runner_with_adapter(adapter)
        trial_dir = tmp_path / "trial"
        trial_dir.mkdir()
        harness = self._make_harness()

        result = runner._execute_crs_with_managers(
            harness=harness,
            benchmark_path=tmp_path,
            trial_output_dir=trial_dir,
            trial_start_time=0.0,
        )

        harness_result = result[0]
        assert harness_result.run_successful is True
        adapter.collect_results.assert_called_once()


# ===========================================================================
# Benchmark Staging (Ground Truth Leakage Prevention)
# ===========================================================================


class TestStageBenchmark:
    """Tests for _stage_benchmark() dotfile filtering and file staging."""

    def _make_adapter(self, tmp_path: Path) -> OssCrsAdapter:
        registry = tmp_path / "registry"
        crs_dir = registry / "test-crs"
        crs_dir.mkdir(parents=True)
        (crs_dir / "pkg.yaml").write_text(
            yaml.dump(
                {
                    "source": {
                        "url": "https://github.com/team/crs.git",
                        "ref": "main",
                    }
                }
            )
        )
        return OssCrsAdapter(
            crs_config_name="test-crs",
            oss_fuzz_path=tmp_path / "oss-fuzz",
            registry_dir=registry,
            benchmarks_root=tmp_path / "benchmarks",
            crs_configs_dir=tmp_path / "configs",
            mode="bug-finding",
        )

    def _make_benchmark(self, tmp_path: Path) -> Path:
        bench = tmp_path / "bench-proj"
        bench.mkdir()
        (bench / "Dockerfile").write_text("FROM ubuntu:22.04\n")
        (bench / "build.sh").write_text("#!/bin/bash\n")
        (bench / "test.sh").write_text("#!/bin/bash\n")
        (bench / "project.yaml").write_text("language: c\n")
        # Ground truth dirs that should be excluded
        (bench / ".aixcc").mkdir()
        (bench / ".aixcc" / "vuln.yaml").write_text("secret\n")
        (bench / ".agent").mkdir()
        (bench / ".agent" / "config.json").write_text("{}\n")
        (bench / ".git").mkdir()
        (bench / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
        return bench

    def test_excludes_dotfiles(self, tmp_path: Path) -> None:
        adapter = self._make_adapter(tmp_path)
        bench = self._make_benchmark(tmp_path)
        trial = tmp_path / "trial"
        trial.mkdir()

        staged = adapter._stage_benchmark(bench, trial)

        entries = {e.name for e in staged.iterdir()}
        assert "Dockerfile" in entries
        assert "build.sh" in entries
        assert "test.sh" in entries
        assert "project.yaml" in entries
        # Dotfiles/dirs must be absent (except .dockerignore)
        assert ".aixcc" not in entries
        assert ".agent" not in entries
        assert ".git" not in entries

    def test_copies_files(self, tmp_path: Path) -> None:
        adapter = self._make_adapter(tmp_path)
        bench = self._make_benchmark(tmp_path)
        trial = tmp_path / "trial"
        trial.mkdir()

        staged = adapter._stage_benchmark(bench, trial)

        dockerfile = staged / "Dockerfile"
        assert dockerfile.is_file()
        assert not dockerfile.is_symlink()
        assert dockerfile.read_text() == (bench / "Dockerfile").read_text()

    def test_adds_dockerignore(self, tmp_path: Path) -> None:
        adapter = self._make_adapter(tmp_path)
        bench = self._make_benchmark(tmp_path)
        trial = tmp_path / "trial"
        trial.mkdir()

        staged = adapter._stage_benchmark(bench, trial)

        dockerignore = staged / ".dockerignore"
        assert dockerignore.exists()
        content = dockerignore.read_text()
        assert ".aixcc" in content
        assert ".agent" in content

    def test_preserves_benchmark_name(self, tmp_path: Path) -> None:
        adapter = self._make_adapter(tmp_path)
        bench = self._make_benchmark(tmp_path)
        trial = tmp_path / "trial"
        trial.mkdir()

        staged = adapter._stage_benchmark(bench, trial)

        # Staged dir must end with the benchmark name so oss-crs Target
        # extracts the correct project name for Docker image tagging.
        assert staged.name == bench.name

    def test_recreates_fresh(self, tmp_path: Path) -> None:
        adapter = self._make_adapter(tmp_path)
        bench = self._make_benchmark(tmp_path)
        trial = tmp_path / "trial"
        trial.mkdir()

        # First staging
        staged = adapter._stage_benchmark(bench, trial)
        # Add a stale file
        (staged / "stale.txt").write_text("old")

        # Second staging should recreate fresh
        staged2 = adapter._stage_benchmark(bench, trial)
        assert not (staged2 / "stale.txt").exists()
        assert (staged2 / "Dockerfile").is_file()

    @patch("crsbench.evaluation.adapter.compose_common.subprocess.run")
    def test_build_passes_staged_path(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="ok", stderr=""
        )
        adapter = self._make_adapter(tmp_path)
        adapter.configure({"docker_registry": "ghcr.io/t"})
        bench = self._make_benchmark(tmp_path)
        trial = tmp_path / "trial"
        trial.mkdir()

        adapter.build(bench, trial)

        # build-target is the second subprocess.run call
        build_target_cmd = mock_run.call_args_list[1][0][0]
        target_path_idx = build_target_cmd.index("--target-proj-path") + 1
        target_path = build_target_cmd[target_path_idx]
        assert "/staged/" in target_path
        assert target_path != str(bench)

    @patch("crsbench.evaluation.adapter.compose_common.run_with_graceful_timeout")
    @patch("crsbench.evaluation.adapter.compose_common.subprocess.run")
    def test_run_passes_staged_path(
        self,
        mock_subprocess: MagicMock,
        mock_rwgt: MagicMock,
        tmp_path: Path,
    ) -> None:
        mock_subprocess.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="ok", stderr=""
        )
        mock_rwgt.return_value = ("output", "", 0, False)

        adapter = self._make_adapter(tmp_path)
        adapter.configure({"docker_registry": "ghcr.io/t"})
        bench = self._make_benchmark(tmp_path)
        trial = tmp_path / "trial"
        trial.mkdir()

        adapter.build(bench, trial)
        harness = MagicMock()
        harness.name = "fuzz_target"

        adapter.run(bench, harness, trial)

        run_cmd = mock_rwgt.call_args[0][0]
        target_path_idx = run_cmd.index("--target-proj-path") + 1
        target_path = run_cmd[target_path_idx]
        assert "/staged/" in target_path
        assert target_path != str(bench)
