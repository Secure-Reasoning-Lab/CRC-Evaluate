"""Unit tests for oss-crs adapter modules.

Tests config_gen.py, compose_common.py, OssCrsAdapter (both modes),
and ExperimentConfig crs_compose validation.

Requirements covered: ADAPT-02, COMPOSE-01, COMPOSE-02, COMPOSE-03,
COMPOSE-04, COMPOSE-05.
"""

from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from crsbench.evaluation.adapter import OssCrsAdapter
from crsbench.evaluation.adapter.compose_common import (
    docker_compose_down_cleanup,
    force_cleanup_work_dir_containers,
    generate_run_id,
    read_crs_source_from_registry,
    run_oss_crs_artifacts,
    run_oss_crs_build_target,
    run_oss_crs_prepare,
    run_oss_crs_run,
)
from crsbench.evaluation.adapter.config_gen import (
    CrsComposeCrsEntry,
    CrsComposeInfra,
    CrsComposeLiteLLMConfig,
    CrsComposeLiteLLMExternalConfig,
    CrsComposeLLMConfig,
    CrsComposeSource,
    CrsComposeYaml,
)
from crsbench.evaluation.results import CRSExecutionResult
from crsbench.evaluation.runner import BenchmarkRunner, EvaluationError
from crsbench.validation.schemas import HarnessFile

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

FACTORY_ARGS = {
    "crs_config_name": "test-crs",
    "oss_fuzz_path": Path("/tmp/fake/oss-fuzz"),
    "registry_dir": Path("/tmp/fake/registry"),
    "benchmarks_root": Path("/tmp/fake/benchmarks"),
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
            llm_config=CrsComposeLLMConfig(
                litellm=CrsComposeLiteLLMConfig(
                    mode="external",
                    model_check=True,
                    external=CrsComposeLiteLLMExternalConfig(
                        url="https://litellm.example",
                        key="sk-test-key",
                    ),
                )
            ),
        )
        compose.to_yaml(yaml_path)

        data = yaml.safe_load(yaml_path.read_text())
        assert "llm_config" in data
        assert data["llm_config"]["litellm"]["mode"] == "external"
        assert (
            data["llm_config"]["litellm"]["external"]["url"]
            == "https://litellm.example"
        )

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
        crs_yaml = tmp_path / "my-crs.yaml"
        crs_yaml.write_text(
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
        with pytest.raises(FileNotFoundError, match="Expected"):
            read_crs_source_from_registry(tmp_path, "nonexistent-crs")

    def test_raises_value_error_when_source_key_missing(self, tmp_path: Path) -> None:
        (tmp_path / "bad-crs.yaml").write_text(yaml.dump({"name": "bad-crs"}))

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
            "--fuzz-proj-path",
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
            "--fuzz-proj-path",
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
    def test_run_appends_bug_candidate_args(
        self, mock_rwgt: MagicMock, tmp_path: Path
    ) -> None:
        mock_rwgt.return_value = ("out", "err", 0, False)
        compose_file = tmp_path / "crs-compose.yaml"
        work_dir = tmp_path / "work"
        target = tmp_path / "benchmark"
        bug_candidate = tmp_path / "candidate.sarif"

        run_oss_crs_run(
            compose_file,
            work_dir,
            target,
            "harness",
            timeout=3600,
            bug_candidate=bug_candidate,
        )

        cmd = mock_rwgt.call_args[0][0]
        assert "--bug-candidate" in cmd
        assert str(bug_candidate) in cmd
        assert "--bug-candidate-dir" not in cmd

    @patch("crsbench.evaluation.adapter.compose_common.run_with_graceful_timeout")
    def test_run_appends_bug_candidate_dir_arg(
        self, mock_rwgt: MagicMock, tmp_path: Path
    ) -> None:
        mock_rwgt.return_value = ("out", "err", 0, False)
        compose_file = tmp_path / "crs-compose.yaml"
        work_dir = tmp_path / "work"
        target = tmp_path / "benchmark"
        bug_candidate_dir = tmp_path / "bug-candidates"

        run_oss_crs_run(
            compose_file,
            work_dir,
            target,
            "harness",
            timeout=3600,
            bug_candidate_dir=bug_candidate_dir,
        )

        cmd = mock_rwgt.call_args[0][0]
        assert "--bug-candidate-dir" in cmd
        assert str(bug_candidate_dir) in cmd
        assert "--bug-candidate" not in cmd

    def test_run_rejects_mutually_exclusive_bug_candidate_flags(
        self, tmp_path: Path
    ) -> None:
        with pytest.raises(ValueError, match="mutually exclusive"):
            run_oss_crs_run(
                tmp_path / "compose.yaml",
                tmp_path / "work",
                tmp_path / "bench",
                "harness",
                timeout=3600,
                bug_candidate=tmp_path / "candidate.sarif",
                bug_candidate_dir=tmp_path / "bug-candidates",
            )

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
        assert "--bug-candidate" not in cmd
        assert "--bug-candidate-dir" not in cmd

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

        # One compose-down call per compose file (no global network prune)
        assert mock_run.call_count == 2

    def test_generate_run_id_format(self) -> None:
        """generate_run_id returns 'run-{ts}-{suffix}' with random suffix."""
        rid = generate_run_id()
        assert rid.startswith("run-")
        parts = rid.split("-", maxsplit=2)
        assert len(parts) == 3
        assert parts[1].isdigit()
        assert len(parts[2]) == 4

    @patch("crsbench.evaluation.adapter.compose_common.subprocess.run")
    def test_run_oss_crs_artifacts_parses_json(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        """run_oss_crs_artifacts calls oss-crs artifacts and parses JSON."""
        import json

        artifacts = {
            "build_id": "abc",
            "run_id": "run-1",
            "sanitizer": "address",
            "exchange_dir": {
                "base": "/work/EXCHANGE_DIR/h",
                "pov": "/work/EXCHANGE_DIR/h/povs",
                "patch": "/work/EXCHANGE_DIR/h/patches",
            },
            "crs": {
                "test-crs": {
                    "submit_dir": "/work/SUBMIT_DIR/h",
                }
            },
        }
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps(artifacts), stderr=""
        )

        result = run_oss_crs_artifacts(
            tmp_path / "compose.yaml",
            tmp_path / "work",
            tmp_path / "bench",
            "harness1",
            "run-1",
        )
        assert result == artifacts
        cmd = mock_run.call_args[0][0]
        assert cmd[1] == "artifacts"
        assert "--run-id" in cmd
        assert "run-1" in cmd

    @patch("crsbench.evaluation.adapter.compose_common.subprocess.run")
    def test_run_oss_crs_artifacts_raises_on_failure(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="error"
        )

        with pytest.raises(RuntimeError, match="artifacts failed"):
            run_oss_crs_artifacts(
                tmp_path / "compose.yaml",
                tmp_path / "work",
                tmp_path / "bench",
                "harness1",
                "run-1",
            )

    @patch("crsbench.evaluation.adapter.compose_common.run_with_graceful_timeout")
    def test_run_appends_run_id(self, mock_rwgt: MagicMock, tmp_path: Path) -> None:
        """run_oss_crs_run appends --run-id when provided."""
        mock_rwgt.return_value = ("out", "err", 0, False)
        compose_file = tmp_path / "crs-compose.yaml"

        run_oss_crs_run(
            compose_file,
            tmp_path / "work",
            tmp_path / "bench",
            "harness",
            timeout=3600,
            run_id="run-42",
        )

        cmd = mock_rwgt.call_args[0][0]
        assert "--run-id" in cmd
        assert "run-42" in cmd


class TestForceCleanupOnTimeout:
    """Regression: GitHub issue #182 -- subprocess timeout must force-kill
    Docker containers that survived the Python subprocess death."""

    @patch(
        "crsbench.evaluation.adapter.compose_common.force_cleanup_work_dir_containers"
    )
    @patch("crsbench.evaluation.adapter.compose_common.subprocess.run")
    def test_prepare_timeout_triggers_force_cleanup(
        self,
        mock_run: MagicMock,
        mock_force_cleanup: MagicMock,
        tmp_path: Path,
    ) -> None:
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="oss-crs", timeout=10)
        compose_file = tmp_path / "crs-compose.yaml"
        work_dir = tmp_path / "work"
        work_dir.mkdir()

        stdout, stderr, rc = run_oss_crs_prepare(compose_file, work_dir, timeout=10)

        assert rc == -1
        assert "timed out" in stderr
        mock_force_cleanup.assert_called_once_with(work_dir)

    @patch(
        "crsbench.evaluation.adapter.compose_common.force_cleanup_work_dir_containers"
    )
    @patch("crsbench.evaluation.adapter.compose_common.subprocess.run")
    def test_build_target_timeout_triggers_force_cleanup(
        self,
        mock_run: MagicMock,
        mock_force_cleanup: MagicMock,
        tmp_path: Path,
    ) -> None:
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="oss-crs", timeout=10)
        compose_file = tmp_path / "crs-compose.yaml"
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        target = tmp_path / "benchmark"
        target.mkdir()

        stdout, stderr, rc = run_oss_crs_build_target(
            compose_file, work_dir, target, timeout=10
        )

        assert rc == -1
        assert "timed out" in stderr
        mock_force_cleanup.assert_called_once_with(work_dir)

    @patch("crsbench.evaluation.adapter.compose_common.subprocess.run")
    def test_force_cleanup_kills_mount_matching_containers(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        resolved = str(work_dir.resolve())

        # Plan the sequence of subprocess.run calls the helper makes:
        # (1) docker_compose_down_cleanup -> rglob empty, no docker calls
        # (2) docker ps -aq -> returns two container ids
        # (3) docker inspect -> first container mounts work_dir, second doesn't
        # (4) docker kill <matching>
        # (5) docker rm -f <matching>
        def _fake_run(*args, **_kwargs):
            argv = args[0]
            if argv[:2] == ["docker", "ps"]:
                return subprocess.CompletedProcess(
                    argv, returncode=0, stdout="cid-a\ncid-b\n", stderr=""
                )
            if argv[:2] == ["docker", "inspect"]:
                return subprocess.CompletedProcess(
                    argv,
                    returncode=0,
                    stdout=f"cid-a {resolved}/shared \ncid-b /other/path \n",
                    stderr="",
                )
            if argv[:2] == ["docker", "kill"]:
                return subprocess.CompletedProcess(
                    argv, returncode=0, stdout="", stderr=""
                )
            if argv[:2] == ["docker", "rm"]:
                return subprocess.CompletedProcess(
                    argv, returncode=0, stdout="", stderr=""
                )
            # docker compose down path from docker_compose_down_cleanup
            return subprocess.CompletedProcess(argv, returncode=0, stdout="", stderr="")

        mock_run.side_effect = _fake_run

        force_cleanup_work_dir_containers(work_dir)

        kill_calls = [
            c
            for c in mock_run.call_args_list
            if c.args and c.args[0][:2] == ["docker", "kill"]
        ]
        rm_calls = [
            c
            for c in mock_run.call_args_list
            if c.args and c.args[0][:2] == ["docker", "rm"]
        ]
        assert len(kill_calls) == 1
        assert "cid-a" in kill_calls[0].args[0]
        assert "cid-b" not in kill_calls[0].args[0]
        assert len(rm_calls) == 1
        assert "cid-a" in rm_calls[0].args[0]

    @patch("crsbench.evaluation.adapter.compose_common.subprocess.run")
    def test_force_cleanup_uses_path_segment_match_not_substring(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        """Regression test for the trial-1 / trial-10 substring bug.

        A plain `work_dir_resolved in mounts_blob` check would incorrectly
        match any sibling path that shares the same prefix. With 14
        concurrent trials on a single worker, `/data/work/trial-1`'s
        cleanup would kill containers belonging to `/data/work/trial-10`,
        `/data/work/trial-100`, etc. The fix is a whole-path segment
        match: the mount source must equal work_dir or start with
        ``work_dir + "/"``.

        This test pins the fix by enumerating every tricky case:
        - exact-match mount (MUST kill)
        - descendant mount (MUST kill)
        - numeric-suffix sibling (MUST NOT kill) — the core bug
        - lexical prefix sibling (MUST NOT kill)
        - unrelated path (MUST NOT kill)
        - multi-mount container where one mount matches (MUST kill)
        - multi-mount container where no mount matches (MUST NOT kill)
        """
        # Pin work_dir to a stable absolute path independent of tmp_path
        # so the assertions below can reason about exact string matches.
        # We bypass the normal tmp_path fixture because we need a parent
        # directory with multiple sibling numeric-suffixed subdirectories
        # and the substring bug is about the suffix, not the test env.
        work_root = tmp_path / "work"
        work_root.mkdir()
        work_dir = work_root / "trial-1"
        work_dir.mkdir()
        resolved = str(work_dir.resolve())

        # Craft a fleet of fake containers, one per scenario.
        # The inspect output format is "{id} {source1} {source2} ..." per
        # line (space-separated mount sources), matching the adapter's
        # docker inspect -f template.
        containers = {
            # === MUST kill ===
            "cid-exact": [resolved],  # exact-match mount
            "cid-descendant": [f"{resolved}/logs"],  # deeper descendant
            "cid-deep": [f"{resolved}/oss-crs-workdir/runs/foo"],  # many levels
            "cid-multi-match": [
                "/unrelated/path",
                f"{resolved}/shared",  # second mount matches
            ],
            # === MUST NOT kill ===
            # THE CORE BUG: numeric-suffix sibling. Substring check would
            # match because `trial-1` is a prefix of `trial-10`.
            "cid-sibling-numeric": [f"{work_root}/trial-10/shared"],
            "cid-sibling-numeric-100": [f"{work_root}/trial-100"],
            # Lexical prefix sibling: work_dir name is a prefix of another
            # directory name at the same parent level.
            "cid-sibling-lex": [f"{work_root}/trial-1a/data"],
            # Completely unrelated path.
            "cid-unrelated": ["/var/run/docker.sock"],
            # Multi-mount container with only near-misses.
            "cid-multi-near-miss": [
                f"{work_root}/trial-10",
                f"{work_root}/trial-1a",
            ],
        }
        must_kill = {"cid-exact", "cid-descendant", "cid-deep", "cid-multi-match"}
        must_not_kill = set(containers) - must_kill

        # Build the docker inspect stdout from the container map. One
        # line per container, matching the real -f template output.
        inspect_stdout = "\n".join(
            f"{cid} {' '.join(sources)}" for cid, sources in containers.items()
        )
        ps_stdout = "\n".join(containers.keys())

        def _fake_run(*args, **_kwargs):
            argv = args[0]
            if argv[:2] == ["docker", "ps"]:
                return subprocess.CompletedProcess(
                    argv, returncode=0, stdout=ps_stdout + "\n", stderr=""
                )
            if argv[:2] == ["docker", "inspect"]:
                return subprocess.CompletedProcess(
                    argv, returncode=0, stdout=inspect_stdout + "\n", stderr=""
                )
            if argv[:2] == ["docker", "kill"]:
                return subprocess.CompletedProcess(
                    argv, returncode=0, stdout="", stderr=""
                )
            if argv[:2] == ["docker", "rm"]:
                return subprocess.CompletedProcess(
                    argv, returncode=0, stdout="", stderr=""
                )
            # docker compose down path from docker_compose_down_cleanup
            return subprocess.CompletedProcess(argv, returncode=0, stdout="", stderr="")

        mock_run.side_effect = _fake_run

        force_cleanup_work_dir_containers(work_dir)

        # Extract the exact set of container ids that were passed to
        # `docker kill` and `docker rm -f`.
        kill_calls = [
            c
            for c in mock_run.call_args_list
            if c.args and c.args[0][:2] == ["docker", "kill"]
        ]
        rm_calls = [
            c
            for c in mock_run.call_args_list
            if c.args and c.args[0][:2] == ["docker", "rm"]
        ]

        # Exactly one kill call and one rm call, both with the same
        # container-id arguments (after "docker kill" / "docker rm -f").
        assert len(kill_calls) == 1, (
            f"Expected exactly 1 docker kill call; got {len(kill_calls)}. "
            f"This usually means cleanup is running multiple passes or the "
            f"test fake is matching the wrong subprocess invocation."
        )
        assert len(rm_calls) == 1

        killed = set(kill_calls[0].args[0][2:])
        removed = set(rm_calls[0].args[0][3:])  # skip "docker", "rm", "-f"
        assert killed == removed, "kill and rm container sets diverged"

        # Positive assertions: every must-kill container was killed.
        missing = must_kill - killed
        assert not missing, (
            f"Containers that mount work_dir were NOT killed: {sorted(missing)}. "
            f"Killed set: {sorted(killed)}"
        )

        # Negative assertions: no sibling/unrelated container was killed.
        # This is the load-bearing check — substring matching would make
        # this fail because "trial-1" is a substring of "trial-10".
        spurious = must_not_kill & killed
        assert not spurious, (
            f"Containers that do NOT mount work_dir were killed due to a "
            f"substring match bug. Spurious kills: {sorted(spurious)}. "
            f"This likely means force_cleanup_work_dir_containers reverted "
            f"to the pre-fix `work_dir_resolved in mounts_blob` check."
        )

    @patch("crsbench.evaluation.adapter.compose_common.subprocess.run")
    def test_force_cleanup_trailing_slash_in_work_dir(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        """A work_dir path whose resolved form has no trailing slash must
        still match descendant mounts, and must not over-match when the
        separator is normalized.
        """
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        resolved = str(work_dir.resolve())
        # Sanity: pathlib.Path.resolve() returns no trailing slash.
        assert not resolved.endswith("/")

        def _fake_run(*args, **_kwargs):
            argv = args[0]
            if argv[:2] == ["docker", "ps"]:
                return subprocess.CompletedProcess(
                    argv, returncode=0, stdout="cid-x\ncid-y\n", stderr=""
                )
            if argv[:2] == ["docker", "inspect"]:
                return subprocess.CompletedProcess(
                    argv,
                    returncode=0,
                    # cid-x mounts the work_dir exactly (no trailing slash)
                    # cid-y mounts a descendant with a normal trailing sep
                    stdout=f"cid-x {resolved}\ncid-y {resolved}/oss-crs-workdir\n",
                    stderr="",
                )
            return subprocess.CompletedProcess(argv, returncode=0, stdout="", stderr="")

        mock_run.side_effect = _fake_run

        force_cleanup_work_dir_containers(work_dir)

        kill_calls = [
            c
            for c in mock_run.call_args_list
            if c.args and c.args[0][:2] == ["docker", "kill"]
        ]
        assert len(kill_calls) == 1
        killed = set(kill_calls[0].args[0][2:])
        assert killed == {"cid-x", "cid-y"}


# ===========================================================================
# OssCrsAdapter bug-finding (ADAPT-02, COMPOSE-01, COMPOSE-02, COMPOSE-03, COMPOSE-04)
# ===========================================================================


class TestOssCrsAdapterBugFindFull:
    """Comprehensive tests for OssCrsAdapter (bug-finding) lifecycle."""

    def _make_adapter(self, tmp_path: Path) -> OssCrsAdapter:
        """Create adapter with registry dir containing a valid oss-crs YAML."""
        registry = tmp_path / "registry"
        registry.mkdir(parents=True)
        (registry / "test-crs.yaml").write_text(
            yaml.dump(
                {
                    "source": {
                        "url": "https://github.com/team/crs.git",
                        "ref": "main",
                    }
                }
            )
        )
        cfg_dir = tmp_path / "configs" / "test-crs"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        (cfg_dir / "config-resource.yaml").write_text(
            yaml.dump(
                {
                    "workers": {"local": {"cpuset": "0-3", "memory": "8G"}},
                    "crs": {"test-crs": {"workers": ["local"]}},
                }
            )
        )
        adapter = OssCrsAdapter(
            crs_config_name="test-crs",
            oss_fuzz_path=tmp_path / "oss-fuzz",
            registry_dir=registry,
            benchmarks_root=tmp_path / "benchmarks",
            mode="bug-finding",
        )
        adapter.configure(
            {
                "litellm_runtime_url": "https://litellm.example",
                "litellm_runtime_api_key": "sk-test-key",
            }
        )
        return adapter

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
                "litellm_config_path": "/tmp/custom-litellm.yaml",
                "run_id": "trial-abc-123",
            }
        )
        assert adapter._build_timeout == 900
        assert adapter._run_timeout == 1800
        assert adapter._docker_registry == "ghcr.io/team"
        assert adapter._oss_crs_cmd == "/opt/oss-crs"
        assert adapter._oss_crs_infra_cpuset == "0-15"
        assert adapter._infra_mem_limit == "32G"
        assert adapter._litellm_config_path == "/tmp/custom-litellm.yaml"
        assert adapter._configured_run_id == "trial-abc-123"
        assert adapter._run_id == "trial-abc-123"

    def test_build_lock_file_path_uses_crs_project_and_sanitizer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        adapter = self._make_adapter(tmp_path)
        adapter.configure({"sanitizer": "address"})
        monkeypatch.setenv("CRSBENCH_OSS_CRS_BUILD_LOCK_DIR", str(tmp_path / "locks"))

        lock_path = adapter._build_lock_file_path("proj1")

        assert lock_path.parent == (tmp_path / "locks")
        assert lock_path.name == "crsbench-oss-crs-build-test-crs-proj1-address.lock"

    def test_configure_sanitizer_change_invalidates_compose_and_build_cache(
        self, tmp_path: Path
    ) -> None:
        adapter = self._make_adapter(tmp_path)
        adapter._compose_file = tmp_path / "trial1" / "crs-compose.yaml"
        adapter._work_dir = tmp_path / "trial1" / "oss-crs-workdir"
        adapter._resolved_artifacts = {"exchange_dir": {"base": "/tmp/exchange"}}
        adapter._built_projects.add("proj1")
        adapter._prepared = True

        adapter.configure({"sanitizer": "undefined"})

        assert adapter._sanitizer == "undefined"
        assert adapter._compose_file is None
        assert adapter._work_dir is None
        assert adapter._resolved_artifacts is None
        assert adapter._built_projects == set()
        assert adapter._prepared is False

    def test_configure_sanitizer_change_preserves_explicit_work_dir(
        self, tmp_path: Path
    ) -> None:
        adapter = self._make_adapter(tmp_path)
        adapter._compose_file = tmp_path / "trial1" / "crs-compose.yaml"
        adapter._work_dir = tmp_path / "trial1" / "oss-crs-workdir"
        configured_work_dir = tmp_path / "custom-workdir"

        adapter.configure(
            {"sanitizer": "undefined", "work_dir": str(configured_work_dir)}
        )

        assert adapter._sanitizer == "undefined"
        assert adapter._compose_file is None
        assert adapter._work_dir == configured_work_dir

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

    # ------------------------------------------------------------------
    # Prepare lock tests
    # ------------------------------------------------------------------

    def test_prepare_lock_file_path_uses_crs_name_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Prepare lock should be CRS-scoped, not benchmark-scoped."""
        monkeypatch.setenv("CRSBENCH_OSS_CRS_BUILD_LOCK_DIR", str(tmp_path / "locks"))
        adapter = self._make_adapter(tmp_path)
        adapter.configure({"sanitizer": "address"})

        lock_path = adapter._prepare_lock_file_path()

        assert lock_path.parent == (tmp_path / "locks")
        assert lock_path.name == "crsbench-oss-crs-prepare-test-crs.lock"

    def test_prepare_lock_path_independent_of_sanitizer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same CRS should get same prepare lock regardless of sanitizer."""
        monkeypatch.setenv("CRSBENCH_OSS_CRS_BUILD_LOCK_DIR", str(tmp_path / "locks"))
        adapter = self._make_adapter(tmp_path)

        adapter.configure({"sanitizer": "address"})
        path_asan = adapter._prepare_lock_file_path()

        adapter.configure({"sanitizer": "undefined"})
        path_ubsan = adapter._prepare_lock_file_path()

        assert path_asan == path_ubsan

    @patch("crsbench.evaluation.adapter.compose_common.subprocess.run")
    def test_prepare_runs_once_across_two_benchmarks_same_adapter(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        """Within one adapter instance, prepare should run once, not per benchmark."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="ok", stderr=""
        )
        adapter = self._make_adapter(tmp_path)
        adapter.configure({"docker_registry": "ghcr.io/t"})

        bench1 = tmp_path / "benchmarks" / "proj1"
        bench1.mkdir(parents=True)
        bench2 = tmp_path / "benchmarks" / "proj2"
        bench2.mkdir(parents=True)
        trial = tmp_path / "trial"
        trial.mkdir()

        adapter.build(bench1, trial)
        adapter.build(bench2, trial)

        # Should be: prepare(1) + build-target(1) + build-target(2) = 3
        # prepare NOT called again for proj2
        assert mock_run.call_count == 3
        cmds = [call[0][0][1] for call in mock_run.call_args_list]
        assert cmds == ["prepare", "build-target", "build-target"]

    @patch("crsbench.evaluation.adapter.compose_common.subprocess.run")
    def test_sanitizer_change_resets_prepared_flag(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        """Changing sanitizer must re-prepare because compose env embeds SANITIZER."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="ok", stderr=""
        )
        adapter = self._make_adapter(tmp_path)
        adapter.configure({"docker_registry": "ghcr.io/t", "sanitizer": "address"})

        bench = tmp_path / "benchmarks" / "proj1"
        bench.mkdir(parents=True)
        trial1 = tmp_path / "trial1"
        trial1.mkdir()
        adapter.build(bench, trial1)

        # prepare(1) + build-target(1) = 2
        assert mock_run.call_count == 2
        assert adapter._prepared is True

        # Change sanitizer — must reset _prepared because compose env changes.
        mock_run.reset_mock()
        adapter.configure({"docker_registry": "ghcr.io/t", "sanitizer": "undefined"})
        assert adapter._prepared is False

        trial2 = tmp_path / "trial2"
        trial2.mkdir()
        adapter.build(bench, trial2)

        # prepare + build-target again (new sanitizer env)
        assert mock_run.call_count == 2
        cmds = [call[0][0][1] for call in mock_run.call_args_list]
        assert cmds == ["prepare", "build-target"]

    @patch("crsbench.evaluation.adapter.compose_common.subprocess.run")
    def test_prepare_failure_does_not_set_prepared_flag(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        """If prepare fails, _prepared should stay False so retry is possible."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="prepare error"
        )
        adapter = self._make_adapter(tmp_path)
        adapter.configure({"docker_registry": "ghcr.io/t"})
        bench = tmp_path / "benchmarks" / "proj1"
        bench.mkdir(parents=True)

        with pytest.raises(RuntimeError, match="prepare failed"):
            adapter.build(bench, tmp_path / "trial")

        assert adapter._prepared is False

    @patch("crsbench.evaluation.adapter.compose_common.subprocess.run")
    def test_prepare_retried_after_failure(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        """After a prepare failure, the next build() should retry prepare."""
        mock_run.side_effect = [
            # First build: prepare fails
            subprocess.CompletedProcess(
                args=[], returncode=1, stdout="", stderr="prepare error"
            ),
            # Second build: prepare succeeds, build-target succeeds
            subprocess.CompletedProcess(args=[], returncode=0, stdout="ok", stderr=""),
            subprocess.CompletedProcess(args=[], returncode=0, stdout="ok", stderr=""),
        ]
        adapter = self._make_adapter(tmp_path)
        adapter.configure({"docker_registry": "ghcr.io/t"})
        bench = tmp_path / "benchmarks" / "proj1"
        bench.mkdir(parents=True)

        with pytest.raises(RuntimeError, match="prepare failed"):
            adapter.build(bench, tmp_path / "trial1")

        # Retry should call prepare again
        trial2 = tmp_path / "trial2"
        trial2.mkdir()
        adapter.build(bench, trial2)

        assert adapter._prepared is True
        assert mock_run.call_count == 3
        cmds = [call[0][0][1] for call in mock_run.call_args_list]
        assert cmds == ["prepare", "prepare", "build-target"]

    @patch("crsbench.evaluation.adapter.compose_common.subprocess.run")
    def test_build_lock_serializes_same_project_different_harnesses(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        """Two builds for same project share a build lock key."""
        adapter = self._make_adapter(tmp_path)
        adapter.configure({"sanitizer": "address"})

        path1 = adapter._build_lock_file_path("proj1")
        path2 = adapter._build_lock_file_path("proj1")
        assert path1 == path2

        # Different projects get different locks
        path3 = adapter._build_lock_file_path("proj2")
        assert path1 != path3

    @patch("crsbench.evaluation.adapter.compose_common.subprocess.run")
    def test_concurrent_prepare_serialized_by_flock(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        """Verify that flock file is created during prepare."""
        call_count = 0

        def track_calls(*args, **_kwargs):
            nonlocal call_count
            call_count += 1
            cmd = args[0]
            if cmd[1] == "prepare":
                # Verify the lock file exists while prepare runs
                lock_path = adapter._prepare_lock_file_path()
                assert lock_path.exists(), "Lock file should exist during prepare"
            return subprocess.CompletedProcess(
                args=[], returncode=0, stdout="ok", stderr=""
            )

        mock_run.side_effect = track_calls
        adapter = self._make_adapter(tmp_path)
        adapter.configure({"docker_registry": "ghcr.io/t"})
        bench = tmp_path / "benchmarks" / "proj1"
        bench.mkdir(parents=True)
        trial = tmp_path / "trial"
        trial.mkdir()

        adapter.build(bench, trial)
        assert call_count == 2  # prepare + build-target

    @patch("crsbench.evaluation.adapter.compose_common.subprocess.run")
    def test_two_adapters_both_call_prepare(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        """Separate adapter instances (simulating separate workers) both run prepare.

        Each adapter instance calls prepare independently.
        With separate work directories, each adapter builds independently
        (build-done markers are scoped to work_dir to avoid stale markers
        across experiments).
        """
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="ok", stderr=""
        )

        # Use separate base dirs to simulate truly separate worker processes
        # (each worker creates its own adapter instance)
        base1 = tmp_path / "worker1"
        base1.mkdir()
        bench1 = base1 / "benchmarks" / "proj1"
        bench1.mkdir(parents=True)
        adapter1 = self._make_adapter(base1)
        adapter1.configure({"docker_registry": "ghcr.io/t"})
        trial1 = base1 / "trial1"
        trial1.mkdir()
        adapter1.build(bench1, trial1)

        base2 = tmp_path / "worker2"
        base2.mkdir()
        bench2 = base2 / "benchmarks" / "proj1"
        bench2.mkdir(parents=True)
        adapter2 = self._make_adapter(base2)
        adapter2.configure({"docker_registry": "ghcr.io/t"})
        trial2 = base2 / "trial2"
        trial2.mkdir()
        adapter2.build(bench2, trial2)

        # Both adapters call prepare + build-target independently (separate work dirs).
        assert mock_run.call_count == 4
        cmds = [call[0][0][1] for call in mock_run.call_args_list]
        assert cmds == ["prepare", "build-target", "prepare", "build-target"]

    @patch("crsbench.evaluation.adapter.compose_common.subprocess.run")
    def test_host_global_build_done_marker_is_ignored(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        """A leftover host-global build-done marker must not skip
        build-target.

        Earlier revisions of this adapter cached "build already done"
        state in ``/tmp/crsbench-oss-crs-build-done-*``.  That cache is
        unsound because build outputs live under each trial's per-trial
        ``work_dir`` and are not transferable across fresh work_dirs.
        The current adapter no longer consults such markers; this test
        guards that the old marker is ignored and build-target actually
        runs on a fresh trial.
        """
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="ok", stderr=""
        )
        adapter = self._make_adapter(tmp_path)
        adapter.configure({"docker_registry": "ghcr.io/t"})
        bench = tmp_path / "benchmarks" / "proj1"
        bench.mkdir(parents=True)
        trial = tmp_path / "trial"
        trial.mkdir()

        # Manually write the legacy host-global marker to simulate a
        # prior adapter revision.
        crs_token = adapter._lock_token(adapter._crs_config_name)
        project_token = adapter._lock_token("proj1")
        sanitizer_token = adapter._lock_token(adapter._sanitizer)
        lock_dir = adapter._lock_dir()
        lock_dir.mkdir(parents=True, exist_ok=True)
        marker = (
            lock_dir
            / f"crsbench-oss-crs-build-done-{crs_token}-{project_token}-{sanitizer_token}"
        )
        marker.touch()

        adapter.build(bench, trial)

        cmds = [call[0][0][1] for call in mock_run.call_args_list]
        assert "build-target" in cmds, (
            "Legacy host-global marker must not suppress build-target"
        )

    @patch("crsbench.evaluation.adapter.compose_common.subprocess.run")
    def test_build_target_failure_does_not_affect_prepared_flag(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        """build-target failure should not reset the _prepared flag."""
        mock_run.side_effect = [
            # prepare succeeds
            subprocess.CompletedProcess(args=[], returncode=0, stdout="ok", stderr=""),
            # build-target fails
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

        # prepare succeeded, so flag should be set
        assert adapter._prepared is True

    @patch("crsbench.evaluation.adapter.compose_common.run_with_graceful_timeout")
    @patch("crsbench.evaluation.adapter.compose_common.subprocess.run")
    def test_run_passes_runtime_input_paths_when_present(
        self,
        mock_subprocess: MagicMock,
        mock_rwgt: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Bug-finding mode should still forward staged runtime input paths."""
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

        pov_dir = trial / "povs"
        pov_dir.mkdir()
        (pov_dir / "crash-001").write_bytes(b"\x00" * 8)

        diff_path = trial / "ref.diff"
        diff_path.write_text("--- a/file\n+++ b/file\n")

        seed_dir = trial / "seeds"
        seed_dir.mkdir()
        (seed_dir / "seed-a").write_text("seed")

        bug_candidates_dir = trial / "bug-candidates"
        bug_candidates_dir.mkdir()
        (bug_candidates_dir / "cpv_0.sarif").write_text("{}")

        adapter.build(bench, trial)
        harness = MagicMock()
        harness.name = "fuzz_target"

        adapter.run(bench, harness, trial)

        cmd = mock_rwgt.call_args[0][0]
        assert "--pov-dir" in cmd
        assert str(pov_dir) in cmd
        assert "--diff" in cmd
        assert str(diff_path) in cmd
        assert "--seed-dir" in cmd
        assert str(seed_dir) in cmd
        assert "--bug-candidate-dir" in cmd
        assert str(bug_candidates_dir) in cmd

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
        assert data["docker_registry"] == ""

    @patch("crsbench.evaluation.adapter.compose_common.subprocess.run")
    def test_generate_compose_yaml_assigns_cpusets_from_allocated_cpus(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        adapter = self._make_adapter(tmp_path)
        adapter.configure(
            {
                "allocated_cpus": "20-23",
                "oss_crs_infra": {"num_cores": 1, "mem_limit": "8G"},
                "crs_services": {
                    "test-crs": {
                        "num_cores": 2,
                        "mem_limit": "16G",
                    }
                },
            }
        )

        compose_path = adapter._generate_compose_yaml(tmp_path / "trial")
        data = yaml.safe_load(compose_path.read_text())
        assert data["oss_crs_infra"]["cpuset"] == "20"
        assert data["test-crs"]["cpuset"] == "21-22"

    @patch("crsbench.evaluation.adapter.compose_common.subprocess.run")
    def test_generate_compose_yaml_uses_visible_cpu_affinity_when_unset(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        adapter = self._make_adapter(tmp_path)
        adapter.configure(
            {
                "oss_crs_infra": {"shared": True},
                "crs_services": {
                    "test-crs": {
                        "num_cores": 2,
                    }
                },
            }
        )

        with patch(
            "crsbench.evaluation.adapter.oss_crs.os.sched_getaffinity",
            return_value={20, 21, 22, 23},
        ):
            compose_path = adapter._generate_compose_yaml(tmp_path / "trial")

        data = yaml.safe_load(compose_path.read_text())
        assert data["oss_crs_infra"]["cpuset"] == "20-21"
        assert data["test-crs"]["cpuset"] == "20-21"

    @patch("crsbench.evaluation.adapter.compose_common.subprocess.run")
    def test_generate_compose_yaml_rejects_insufficient_allocated_cpus(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        adapter = self._make_adapter(tmp_path)
        adapter.configure(
            {
                "allocated_cpus": "0",
                "oss_crs_infra": {"num_cores": 1, "mem_limit": "8G"},
                "crs_services": {
                    "test-crs": {
                        "num_cores": 1,
                        "mem_limit": "16G",
                    }
                },
            }
        )
        with pytest.raises(RuntimeError, match="Insufficient allocated CPUs"):
            adapter._generate_compose_yaml(tmp_path / "trial")

    @patch("crsbench.evaluation.adapter.compose_common.subprocess.run")
    def test_generate_compose_yaml_shared_infra_uses_service_union_cpuset(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        adapter = self._make_adapter(tmp_path)
        adapter.configure(
            {
                "allocated_cpus": "20-23",
                "oss_crs_infra": {"shared": True},
                "crs_services": {
                    "test-crs": {
                        "num_cores": 2,
                    }
                },
            }
        )

        compose_path = adapter._generate_compose_yaml(tmp_path / "trial")
        data = yaml.safe_load(compose_path.read_text())
        assert data["test-crs"]["cpuset"] == "20-21"
        assert data["oss_crs_infra"]["cpuset"] == "20-21"

    @patch("crsbench.evaluation.adapter.compose_common.subprocess.run")
    def test_shared_infra_with_multiple_services_keeps_current_trial_scope(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        adapter = self._make_adapter(tmp_path)
        adapter.configure(
            {
                "allocated_cpus": "20-25",
                "oss_crs_infra": {"shared": True},
                "crs_services": {
                    "test-crs": {"num_cores": 2},
                    "other-crs": {"num_cores": 2},
                },
            }
        )

        compose_path = adapter._generate_compose_yaml(tmp_path / "trial")
        data = yaml.safe_load(compose_path.read_text())
        assert "test-crs" in data
        assert "other-crs" not in data
        assert data["test-crs"]["cpuset"] == "20-21"
        assert data["oss_crs_infra"]["cpuset"] == "20-21"

    @patch("crsbench.evaluation.adapter.compose_common.subprocess.run")
    def test_configure_crs_services_filters_to_current_trial_crs(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        adapter = self._make_adapter(tmp_path)
        adapter.configure(
            {
                "crs_services": {
                    "test-crs": {"num_cores": 1, "mem_limit": "8G"},
                    "other-crs": {"num_cores": 1, "mem_limit": "8G"},
                }
            }
        )
        compose_path = adapter._generate_compose_yaml(tmp_path / "trial")
        data = yaml.safe_load(compose_path.read_text())
        assert "test-crs" in data
        assert "other-crs" not in data

    @patch("crsbench.evaluation.adapter.compose_common.subprocess.run")
    def test_configure_crs_services_missing_current_trial_crs_falls_back_to_default(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        adapter = self._make_adapter(tmp_path)
        adapter.configure(
            {
                "crs_services": {
                    "other-crs": {"num_cores": 1, "mem_limit": "8G"},
                }
            }
        )
        adapter.configure({"allocated_memory": "10G"})
        with patch(
            "crsbench.evaluation.adapter.oss_crs.os.sched_getaffinity",
            return_value={8, 9, 10},
        ):
            compose_path = adapter._generate_compose_yaml(tmp_path / "trial")
        data = yaml.safe_load(compose_path.read_text())
        assert "test-crs" in data
        assert "other-crs" not in data
        assert data["test-crs"]["cpuset"] == "8-10"
        assert data["test-crs"]["memory"] == "10G"

    @patch("crsbench.evaluation.adapter.compose_common.subprocess.run")
    def test_configure_crs_services_missing_current_resets_stale_override(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        adapter = self._make_adapter(tmp_path)
        adapter.configure({"allocated_memory": "8G"})
        adapter.configure(
            {
                "crs_services": {
                    "test-crs": {"num_cores": 3, "mem_limit": "8G"},
                }
            }
        )
        adapter.configure(
            {
                "crs_services": {
                    "other-crs": {"num_cores": 1, "mem_limit": "4G"},
                }
            }
        )
        adapter.configure({"allocated_memory": "10G"})
        with patch(
            "crsbench.evaluation.adapter.oss_crs.os.sched_getaffinity",
            return_value={0, 1, 2},
        ):
            compose_path = adapter._generate_compose_yaml(tmp_path / "trial")
        data = yaml.safe_load(compose_path.read_text())
        assert data["test-crs"]["cpuset"] == "0-2"
        assert data["test-crs"]["memory"] == "10G"

    @patch("crsbench.evaluation.adapter.compose_common.subprocess.run")
    def test_configure_accepts_flat_crs_compose_service_keys(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        adapter = self._make_adapter(tmp_path)
        adapter.configure(
            {
                "oss_crs_infra": {"num_cores": 1},
                "test-crs": {"num_cores": 1},
            }
        )
        compose_path = adapter._generate_compose_yaml(tmp_path / "trial")
        data = yaml.safe_load(compose_path.read_text())
        assert "test-crs" in data

    def test_get_budget_policy_defaults_to_continue(self, tmp_path: Path) -> None:
        adapter = self._make_adapter(tmp_path)
        assert adapter.get_budget_policy() == "continue"

    def test_get_budget_policy_reads_configured_value(self, tmp_path: Path) -> None:
        adapter = self._make_adapter(tmp_path)
        adapter.configure(
            {
                "crs_services": {
                    "test-crs": {
                        "num_cores": 1,
                        "mem_limit": "8G",
                        "budget_policy": "terminate",
                    }
                }
            }
        )
        assert adapter.get_budget_policy() == "terminate"

    def test_get_budget_policy_rejects_invalid_value(self, tmp_path: Path) -> None:
        adapter = self._make_adapter(tmp_path)
        adapter.configure(
            {
                "crs_services": {
                    "test-crs": {
                        "num_cores": 1,
                        "mem_limit": "8G",
                        "budget_policy": "nuke-everything",
                    }
                }
            }
        )
        assert adapter.get_budget_policy() == "continue"

    @patch("crsbench.evaluation.adapter.compose_common.subprocess.run")
    def test_generate_compose_yaml_sets_memory_when_unset(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        adapter = self._make_adapter(tmp_path)
        adapter.configure(
            {
                "allocated_memory": "12G",
                "oss_crs_infra": {"num_cores": 1},
                "crs_services": {"test-crs": {"num_cores": 1}},
            }
        )
        compose_path = adapter._generate_compose_yaml(tmp_path / "trial")
        data = yaml.safe_load(compose_path.read_text())
        assert data["oss_crs_infra"]["memory"] == "12G"
        assert data["test-crs"]["memory"] == "12G"

    @patch("crsbench.evaluation.adapter.compose_common.subprocess.run")
    def test_generate_compose_yaml_uses_visible_memory_when_unset(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        adapter = self._make_adapter(tmp_path)
        adapter.configure(
            {
                "oss_crs_infra": {"num_cores": 1},
                "crs_services": {"test-crs": {"num_cores": 1}},
            }
        )

        with patch(
            "crsbench.evaluation.adapter.oss_crs._default_memory_limit",
            return_value="24576MB",
        ):
            compose_path = adapter._generate_compose_yaml(tmp_path / "trial")

        data = yaml.safe_load(compose_path.read_text())
        assert data["oss_crs_infra"]["memory"] == "24576MB"
        assert data["test-crs"]["memory"] == "24576MB"

    @patch("crsbench.evaluation.adapter.compose_common.subprocess.run")
    def test_generate_compose_yaml_service_memory_tracks_infra_updates(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        adapter = self._make_adapter(tmp_path)
        adapter.configure(
            {
                "oss_crs_infra": {"num_cores": 1},
                "crs_services": {"test-crs": {"num_cores": 1}},
            }
        )
        adapter.configure({"allocated_memory": "10G"})
        compose_path = adapter._generate_compose_yaml(tmp_path / "trial")
        data = yaml.safe_load(compose_path.read_text())
        assert data["oss_crs_infra"]["memory"] == "10G"
        assert data["test-crs"]["memory"] == "10G"

    @patch("crsbench.evaluation.adapter.compose_common.subprocess.run")
    def test_configure_explicit_memory_not_overridden_by_later_call(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        adapter = self._make_adapter(tmp_path)
        adapter.configure(
            {
                "oss_crs_infra": {"num_cores": 1, "mem_limit": "8G"},
                "crs_services": {"test-crs": {"num_cores": 1}},
            }
        )
        adapter.configure({"allocated_memory": "10G"})
        compose_path = adapter._generate_compose_yaml(tmp_path / "trial")
        data = yaml.safe_load(compose_path.read_text())
        assert data["oss_crs_infra"]["memory"] == "8G"
        assert data["test-crs"]["memory"] == "8G"

    @patch("crsbench.evaluation.adapter.compose_common.subprocess.run")
    def test_configure_unset_explicit_memory_allows_allocated_memory(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        adapter = self._make_adapter(tmp_path)
        adapter.configure({"oss_crs_infra": {"num_cores": 1, "mem_limit": "8G"}})
        adapter.configure(
            {
                "oss_crs_infra": {"num_cores": 1, "mem_limit": None},
                "allocated_memory": "10G",
            }
        )
        compose_path = adapter._generate_compose_yaml(tmp_path / "trial")
        data = yaml.safe_load(compose_path.read_text())
        assert data["oss_crs_infra"]["memory"] == "10G"
        assert data["test-crs"]["memory"] == "10G"

    @patch("crsbench.evaluation.adapter.compose_common.subprocess.run")
    def test_configure_unset_legacy_explicit_memory_allows_allocated_memory(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        adapter = self._make_adapter(tmp_path)
        adapter.configure({"oss_crs_infra_memory": "8G"})
        adapter.configure({"oss_crs_infra_memory": None, "allocated_memory": "11G"})
        compose_path = adapter._generate_compose_yaml(tmp_path / "trial")
        data = yaml.safe_load(compose_path.read_text())
        assert data["oss_crs_infra"]["memory"] == "11G"
        assert data["test-crs"]["memory"] == "11G"

    @patch("crsbench.evaluation.adapter.compose_common.subprocess.run")
    def test_generate_compose_yaml_does_not_inject_external_litellm_env(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        """Compose generation should not inject LiteLLM runtime env into CRS env."""
        adapter = self._make_adapter(tmp_path)
        adapter.configure(
            {
                "litellm_runtime_url": "https://litellm.example",
                "litellm_runtime_api_key": "sk-test-key",
            }
        )
        trial = tmp_path / "trial"

        compose_path = adapter._generate_compose_yaml(trial)

        data = yaml.safe_load(compose_path.read_text())
        env = data["test-crs"].get("additional_env", {})
        assert "CRSBENCH_LLM_UPSTREAM_BASE_URL" not in env
        assert "CRSBENCH_LLM_API_KEY" not in env

    @patch("crsbench.evaluation.adapter.compose_common.subprocess.run")
    def test_generate_compose_yaml_includes_external_litellm_config_when_present(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        """Compose generation should include llm_config.litellm external mode."""
        adapter = self._make_adapter(tmp_path)
        adapter.configure(
            {
                "docker_registry": "ghcr.io/t",
                "litellm_runtime_url": "https://litellm.example",
                "litellm_runtime_api_key": "sk-test-key",
            }
        )

        compose_path = adapter._generate_compose_yaml(tmp_path / "trial")
        data = yaml.safe_load(compose_path.read_text())
        assert data["llm_config"]["litellm"]["mode"] == "external"
        assert (
            data["llm_config"]["litellm"]["external"]["url"]
            == "https://litellm.example"
        )
        assert data["llm_config"]["litellm"]["external"]["key"] == "sk-test-key"

    @patch("crsbench.evaluation.adapter.compose_common.subprocess.run")
    def test_generate_compose_yaml_skips_required_llm_validation_when_skip_litellm(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        """skip_litellm should bypass llm_config loading and required_llms checks."""
        adapter = self._make_adapter(tmp_path)
        # Add required_llms metadata; without skip_litellm this would require llm_config.
        registry_yaml = tmp_path / "registry" / "test-crs.yaml"
        data = yaml.safe_load(registry_yaml.read_text())
        data["required_llms"] = ["claude-opus-4-6"]
        registry_yaml.write_text(yaml.dump(data))

        adapter.configure(
            {
                "docker_registry": "ghcr.io/t",
                "skip_litellm": True,
            }
        )
        compose_path = adapter._generate_compose_yaml(tmp_path / "trial")
        compose_data = yaml.safe_load(compose_path.read_text())
        assert "llm_config" not in compose_data

    @patch("crsbench.evaluation.adapter.compose_common.subprocess.run")
    def test_generate_compose_yaml_merges_additional_env_with_external_litellm(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        adapter = self._make_adapter(tmp_path)

        adapter.configure(
            {
                "docker_registry": "ghcr.io/t",
                "additional_env": {
                    "ANTHROPIC_MODEL": "claude-sonnet-4-5-20250929",
                },
                "litellm_runtime_url": "https://litellm.example",
                "litellm_runtime_api_key": "sk-test-key",
            }
        )
        compose_path = adapter._generate_compose_yaml(tmp_path / "trial")
        compose_data = yaml.safe_load(compose_path.read_text())
        env = compose_data["test-crs"]["additional_env"]
        assert env["ANTHROPIC_MODEL"] == "claude-sonnet-4-5-20250929"
        assert "CRSBENCH_LLM_UPSTREAM_BASE_URL" not in env
        assert "CRSBENCH_LLM_API_KEY" not in env

    @patch("crsbench.evaluation.adapter.compose_common.subprocess.run")
    def test_generate_compose_yaml_no_longer_raises_when_required_llm_missing_locally(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        """required_llms are validated by oss-crs external model check, not local YAML."""
        adapter = self._make_adapter(tmp_path)
        registry_yaml = tmp_path / "registry" / "test-crs.yaml"
        data = yaml.safe_load(registry_yaml.read_text())
        data["required_llms"] = ["claude-opus-4-6"]
        registry_yaml.write_text(yaml.dump(data))

        adapter.configure(
            {
                "docker_registry": "ghcr.io/t",
                "litellm_runtime_url": "https://litellm.example",
                "litellm_runtime_api_key": "sk-test-key",
            }
        )
        compose_path = adapter._generate_compose_yaml(tmp_path / "trial")
        compose_data = yaml.safe_load(compose_path.read_text())
        assert compose_data["llm_config"]["litellm"]["mode"] == "external"

    @patch("crsbench.evaluation.adapter.compose_common.subprocess.run")
    def test_generate_compose_yaml_raises_when_external_runtime_not_injected(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        """External mode must receive explicit runtime URL/key from CRSBench."""
        adapter = self._make_adapter(tmp_path)
        adapter.configure(
            {
                "litellm_runtime_url": "",
                "litellm_runtime_api_key": "",
            }
        )

        with pytest.raises(RuntimeError, match="requires explicit runtime URL/API key"):
            adapter._generate_compose_yaml(tmp_path / "trial")

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

    @patch("crsbench.evaluation.adapter.oss_crs.generate_run_id")
    @patch("crsbench.evaluation.adapter.oss_crs.run_oss_crs_artifacts")
    @patch("crsbench.evaluation.adapter.compose_common.subprocess.run")
    def test_resolve_artifacts_prefers_configured_run_id(
        self,
        mock_subprocess: MagicMock,
        mock_artifacts: MagicMock,
        mock_generate_run_id: MagicMock,
        tmp_path: Path,
    ) -> None:
        mock_subprocess.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="ok", stderr=""
        )
        mock_generate_run_id.return_value = "run-should-not-be-used"
        mock_artifacts.return_value = {
            "exchange_dir": {"base": str(tmp_path / "exchange")},
        }

        adapter = self._make_adapter(tmp_path)
        adapter.configure({"docker_registry": "ghcr.io/t", "run_id": "trial-run-id-01"})
        bench = tmp_path / "benchmarks" / "proj1"
        bench.mkdir(parents=True)
        trial = tmp_path / "trial"
        trial.mkdir()
        adapter.build(bench, trial)

        adapter.resolve_artifacts(bench, "fuzz_target", trial)

        args, kwargs = mock_artifacts.call_args
        assert args[4] == "trial-run-id-01"
        assert kwargs["oss_crs_cmd"] == adapter._oss_crs_cmd
        assert kwargs["sanitizer"] == adapter._sanitizer
        mock_generate_run_id.assert_not_called()

    @patch("crsbench.evaluation.adapter.compose_common.run_with_graceful_timeout")
    @patch("crsbench.evaluation.adapter.compose_common.subprocess.run")
    def test_run_uses_configured_run_id_when_pre_resolve_is_skipped(
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
        adapter.configure({"docker_registry": "ghcr.io/t", "run_id": "trial-run-id-02"})
        bench = tmp_path / "benchmarks" / "proj1"
        bench.mkdir(parents=True)
        trial = tmp_path / "trial"
        trial.mkdir()
        adapter.build(bench, trial)
        harness = MagicMock()
        harness.name = "fuzz_target"

        adapter.run(bench, harness, trial)

        cmd = mock_rwgt.call_args[0][0]
        assert "--run-id" in cmd
        assert "trial-run-id-02" in cmd

    @patch("crsbench.evaluation.adapter.oss_crs.generate_run_id")
    @patch("crsbench.evaluation.adapter.compose_common.run_with_graceful_timeout")
    @patch("crsbench.evaluation.adapter.compose_common.subprocess.run")
    def test_adapter_reuse_without_pre_resolve_generates_new_ids_per_trial(
        self,
        mock_subprocess: MagicMock,
        mock_rwgt: MagicMock,
        mock_generate_run_id: MagicMock,
        tmp_path: Path,
    ) -> None:
        mock_subprocess.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="ok", stderr=""
        )
        mock_rwgt.return_value = ("output", "", 0, False)
        mock_generate_run_id.side_effect = ["run-A", "run-B"]

        adapter = self._make_adapter(tmp_path)
        adapter.configure({"docker_registry": "ghcr.io/t"})
        bench = tmp_path / "benchmarks" / "proj1"
        bench.mkdir(parents=True)
        harness = MagicMock()
        harness.name = "fuzz_target"

        trial1 = tmp_path / "trial1"
        trial2 = tmp_path / "trial2"
        trial1.mkdir()
        trial2.mkdir()

        adapter.build(bench, trial1)
        adapter.run(bench, harness, trial1)

        adapter.build(bench, trial2)
        adapter.run(bench, harness, trial2)

        assert mock_generate_run_id.call_count == 2
        assert "run-A" in mock_rwgt.call_args_list[0][0][0]
        assert "run-B" in mock_rwgt.call_args_list[1][0][0]

    @patch("crsbench.evaluation.adapter.oss_crs.generate_run_id")
    @patch("crsbench.evaluation.adapter.oss_crs.run_oss_crs_artifacts")
    @patch("crsbench.evaluation.adapter.compose_common.run_with_graceful_timeout")
    @patch("crsbench.evaluation.adapter.compose_common.subprocess.run")
    def test_adapter_reuse_without_configured_run_id_generates_new_ids_per_trial(
        self,
        mock_subprocess: MagicMock,
        mock_rwgt: MagicMock,
        mock_artifacts: MagicMock,
        mock_generate_run_id: MagicMock,
        tmp_path: Path,
    ) -> None:
        mock_subprocess.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="ok", stderr=""
        )
        mock_rwgt.return_value = ("output", "", 0, False)
        mock_artifacts.return_value = {}
        mock_generate_run_id.side_effect = ["run-A", "run-B"]

        adapter = self._make_adapter(tmp_path)
        adapter.configure({"docker_registry": "ghcr.io/t"})
        bench = tmp_path / "benchmarks" / "proj1"
        bench.mkdir(parents=True)
        harness = MagicMock()
        harness.name = "fuzz_target"

        trial1 = tmp_path / "trial1"
        trial2 = tmp_path / "trial2"
        trial1.mkdir()
        trial2.mkdir()

        adapter.build(bench, trial1)
        adapter.resolve_artifacts(bench, harness.name, trial1)
        adapter.run(bench, harness, trial1)

        adapter.build(bench, trial2)
        adapter.resolve_artifacts(bench, harness.name, trial2)
        adapter.run(bench, harness, trial2)

        assert mock_generate_run_id.call_count == 2
        assert mock_artifacts.call_args_list[0][0][4] == "run-A"
        assert mock_artifacts.call_args_list[1][0][4] == "run-B"
        assert "run-A" in mock_rwgt.call_args_list[0][0][0]
        assert "run-B" in mock_rwgt.call_args_list[1][0][0]

    @patch("crsbench.evaluation.adapter.oss_crs.generate_run_id")
    @patch("crsbench.evaluation.adapter.oss_crs.run_oss_crs_artifacts")
    @patch("crsbench.evaluation.adapter.compose_common.run_with_graceful_timeout")
    @patch("crsbench.evaluation.adapter.compose_common.subprocess.run")
    def test_adapter_reuse_with_configured_run_id_updates_per_trial(
        self,
        mock_subprocess: MagicMock,
        mock_rwgt: MagicMock,
        mock_artifacts: MagicMock,
        mock_generate_run_id: MagicMock,
        tmp_path: Path,
    ) -> None:
        mock_subprocess.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="ok", stderr=""
        )
        mock_rwgt.return_value = ("output", "", 0, False)
        mock_artifacts.return_value = {}

        adapter = self._make_adapter(tmp_path)
        adapter.configure({"docker_registry": "ghcr.io/t", "run_id": "trial-A"})
        bench = tmp_path / "benchmarks" / "proj1"
        bench.mkdir(parents=True)
        harness = MagicMock()
        harness.name = "fuzz_target"

        trial1 = tmp_path / "trial1"
        trial2 = tmp_path / "trial2"
        trial1.mkdir()
        trial2.mkdir()

        adapter.build(bench, trial1)
        adapter.resolve_artifacts(bench, harness.name, trial1)
        adapter.run(bench, harness, trial1)

        adapter.configure({"run_id": "trial-B"})
        adapter.build(bench, trial2)
        adapter.resolve_artifacts(bench, harness.name, trial2)
        adapter.run(bench, harness, trial2)

        mock_generate_run_id.assert_not_called()
        assert mock_artifacts.call_args_list[0][0][4] == "trial-A"
        assert mock_artifacts.call_args_list[1][0][4] == "trial-B"
        assert "trial-A" in mock_rwgt.call_args_list[0][0][0]
        assert "trial-B" in mock_rwgt.call_args_list[1][0][0]

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

        # subprocess.run should have been called for docker compose down.
        all_calls = [c[0][0] for c in mock_subprocess.call_args_list]
        down_calls = [c for c in all_calls if "down" in c]
        assert len(down_calls) >= 1
        prune_calls = [c for c in all_calls if "prune" in c]
        assert len(prune_calls) == 0

    @patch("crsbench.evaluation.adapter.compose_common.logger.warning")
    @patch("crsbench.evaluation.adapter.compose_common.subprocess.run")
    def test_docker_cleanup_warning_interpolates_details(
        self,
        mock_run: MagicMock,
        mock_warning: MagicMock,
        tmp_path: Path,
    ) -> None:
        compose_file = tmp_path / "docker-compose.yml"
        compose_file.write_text("version: '3'")
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=7,
            stdout="compose stdout",
            stderr="compose stderr",
        )

        docker_compose_down_cleanup(tmp_path)

        mock_warning.assert_called_once_with(
            f"docker compose down failed for {compose_file} (rc=7): compose stderr"
        )

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

        # Set up EXCHANGE_DIR POV path
        pov_dir = tmp_path / "exchange" / "pov"
        pov_dir.mkdir(parents=True)
        (pov_dir / "crash-001").write_bytes(b"\x00" * 16)
        (pov_dir / "crash-002").write_bytes(b"\xff" * 8)

        adapter._resolved_artifacts = {
            "exchange_dir": {"pov": str(pov_dir)},
        }
        trial = tmp_path / "trial_out"

        metadata = adapter.collect_results(trial, "harness1")

        assert metadata["type"] == "bug-finding"
        output_dir = Path(metadata["output_dir"])
        assert (output_dir / "povs" / "crash-001").exists()
        assert (output_dir / "povs" / "crash-002").exists()

    def test_collect_results_copies_seeds_to_canonical_output_path(
        self, tmp_path: Path
    ) -> None:
        adapter = self._make_adapter(tmp_path)
        adapter.configure({"docker_registry": "ghcr.io/t"})

        seed_dir = tmp_path / "exchange" / "seed"
        seed_dir.mkdir(parents=True)
        (seed_dir / "seed-001").write_bytes(b"abc")
        (seed_dir / "seed-002").write_bytes(b"xyz")

        adapter._resolved_artifacts = {
            "exchange_dir": {"seed": str(seed_dir)},
        }
        trial = tmp_path / "trial_out"

        metadata = adapter.collect_results(trial, "harness1")

        assert metadata["type"] == "bug-finding"
        output_dir = Path(metadata["output_dir"])
        assert (output_dir / "seeds" / "seed-001").exists()
        assert (output_dir / "seeds" / "seed-002").exists()

    def test_collect_results_handles_missing_crs_in_artifacts(
        self, tmp_path: Path
    ) -> None:
        adapter = self._make_adapter(tmp_path)
        # Artifacts exist but don't contain our CRS name
        adapter._resolved_artifacts = {"crs": {"other-crs": {"submit_dir": "/x"}}}

        metadata = adapter.collect_results(tmp_path / "trial", "harness1")
        assert metadata["exchange_pov_dir"] is None
        assert metadata["shared_dirs_by_crs"] == {}

    def test_collect_results_handles_none_artifacts(self, tmp_path: Path) -> None:
        adapter = self._make_adapter(tmp_path)
        # Artifacts never resolved (build never called / artifacts failed)
        adapter._resolved_artifacts = None

        metadata = adapter.collect_results(tmp_path / "trial", "harness1")
        assert metadata["exchange_pov_dir"] is None
        assert metadata["shared_dirs_by_crs"] == {}

    @patch("crsbench.evaluation.adapter.oss_crs.run_oss_crs_artifacts")
    def test_collect_results_refreshes_artifacts_post_run(
        self,
        mock_artifacts: MagicMock,
        tmp_path: Path,
    ) -> None:
        adapter = self._make_adapter(tmp_path)
        adapter.configure({"docker_registry": "ghcr.io/t"})
        adapter._compose_file = tmp_path / "trial" / "crs-compose.yaml"
        adapter._work_dir = tmp_path / "trial" / "oss-crs-workdir"
        adapter._run_id = "run-1234"
        adapter._resolved_artifacts = None

        trial = tmp_path / "trial"
        staged = trial / "staged" / "proj1"
        staged.mkdir(parents=True)
        exchange_pov = tmp_path / "exchange" / "pov"
        exchange_pov.mkdir(parents=True)
        (exchange_pov / "crash-001").write_bytes(b"\x00")
        compose_stdout = tmp_path / "run-logs" / "compose.stdout.log"
        compose_stdout.parent.mkdir(parents=True)
        compose_stdout.write_text("compose-out")

        mock_artifacts.return_value = {
            "exchange_dir": {"pov": str(exchange_pov)},
            "run_logs": {"compose_stdout_log": str(compose_stdout)},
        }

        metadata = adapter.collect_results(trial, "harness1")

        mock_artifacts.assert_called_once_with(
            adapter._compose_file,
            adapter._work_dir,
            staged,
            "harness1",
            "run-1234",
            oss_crs_cmd=adapter._oss_crs_cmd,
            sanitizer=adapter._sanitizer,
        )
        output_dir = Path(metadata["output_dir"])
        assert (output_dir / "povs" / "crash-001").exists()
        assert (output_dir / "logs" / "docker-compose.stdout.log").read_text() == (
            "compose-out"
        )

    @patch("crsbench.evaluation.adapter.compose_common.subprocess.run")
    def test_generate_compose_includes_sanitizer_env(
        self,
        mock_subprocess: MagicMock,
        tmp_path: Path,
    ) -> None:
        mock_subprocess.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="ok", stderr=""
        )
        adapter = self._make_adapter(tmp_path)
        adapter.configure({"docker_registry": "ghcr.io/t", "sanitizer": "undefined"})
        bench = tmp_path / "benchmarks" / "proj1"
        bench.mkdir(parents=True)
        trial = tmp_path / "trial"
        trial.mkdir()

        adapter.build(bench, trial)

        compose_path = trial / "crs-compose.yaml"
        data = yaml.safe_load(compose_path.read_text())
        assert data["test-crs"]["additional_env"]["SANITIZER"] == "undefined"

    @patch("crsbench.evaluation.adapter.compose_common.subprocess.run")
    def test_build_resets_artifacts_between_trials_without_reuse(
        self,
        mock_subprocess: MagicMock,
        tmp_path: Path,
    ) -> None:
        mock_subprocess.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="ok", stderr=""
        )
        adapter = self._make_adapter(tmp_path)
        adapter.configure({"docker_registry": "ghcr.io/t"})
        bench = tmp_path / "benchmarks" / "proj1"
        bench.mkdir(parents=True)

        trial1_pov_dir = tmp_path / "trial1-exchange" / "pov"
        trial1_pov_dir.mkdir(parents=True)
        (trial1_pov_dir / "crash-001").write_bytes(b"\x01")
        adapter._run_id = "run-trial1"
        adapter._resolved_artifacts = {"exchange_dir": {"pov": str(trial1_pov_dir)}}

        trial2 = tmp_path / "trial2"
        trial2.mkdir()
        adapter.build(bench, trial2)

        assert adapter._run_id is None
        assert adapter._resolved_artifacts is None

        metadata = adapter.collect_results(trial2, "harness1")
        output_dir = Path(metadata["output_dir"])
        assert metadata["exchange_pov_dir"] is None
        assert not (output_dir / "povs" / "crash-001").exists()

    @patch("crsbench.evaluation.adapter.compose_common.run_with_graceful_timeout")
    @patch("crsbench.evaluation.adapter.compose_common.subprocess.run")
    def test_build_resets_runtime_run_logs_between_trials_without_reuse(
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
        harness = MagicMock()
        harness.name = "harness1"

        trial1 = tmp_path / "trial1"
        trial1.mkdir()
        adapter.build(bench, trial1)
        adapter.run(bench, harness, trial1)
        assert adapter._runtime_run_logs_base_dir is not None

        trial2 = tmp_path / "trial2"
        trial2.mkdir()
        adapter.build(bench, trial2)

        assert adapter._runtime_run_logs_base_dir is None

    @patch("crsbench.evaluation.adapter.oss_crs.run_oss_crs_artifacts")
    @patch("crsbench.evaluation.adapter.compose_common.run_with_graceful_timeout")
    @patch("crsbench.evaluation.adapter.compose_common.subprocess.run")
    def test_collect_results_copies_runtime_run_logs_without_artifacts(
        self,
        mock_subprocess: MagicMock,
        mock_rwgt: MagicMock,
        mock_artifacts: MagicMock,
        tmp_path: Path,
    ) -> None:
        mock_subprocess.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="ok", stderr=""
        )
        mock_rwgt.return_value = ("output", "", 0, False)
        mock_artifacts.side_effect = RuntimeError("artifacts unavailable")

        adapter = self._make_adapter(tmp_path)
        adapter.configure({"docker_registry": "ghcr.io/t"})
        bench = tmp_path / "benchmarks" / "proj1"
        bench.mkdir(parents=True)
        trial = tmp_path / "trial"
        trial.mkdir()

        adapter.build(bench, trial)
        harness = MagicMock()
        harness.name = "harness1"
        adapter.run(bench, harness, trial)

        run_logs_base = adapter._runtime_run_logs_base_dir
        assert run_logs_base is not None
        services = run_logs_base / "services"
        crs_logs = run_logs_base / "crs" / "test-crs"
        services.mkdir(parents=True)
        crs_logs.mkdir(parents=True)
        (run_logs_base / "docker-compose.stdout.log").write_text("compose-out")
        (run_logs_base / "docker-compose.stderr.log").write_text("compose-err")
        (services / "service-a.stdout.log").write_text("service-out")
        (crs_logs / "runner.stdout.log").write_text("runner-out")

        metadata = adapter.collect_results(trial, "harness1")

        logs_dir = Path(metadata["output_dir"]) / "logs"
        assert (logs_dir / "docker-compose.stdout.log").read_text() == "compose-out"
        assert (logs_dir / "docker-compose.stderr.log").read_text() == "compose-err"
        assert (
            logs_dir / "services" / "service-a.stdout.log"
        ).read_text() == "service-out"
        assert (
            logs_dir / "crs" / "test-crs" / "runner.stdout.log"
        ).read_text() == "runner-out"
        assert metadata["run_logs_base_dir"] == str(run_logs_base)
        assert metadata["service_logs_dir"] == str(services)

    def test_collect_results_copies_top_level_run_logs(self, tmp_path: Path) -> None:
        adapter = self._make_adapter(tmp_path)
        adapter.configure({"docker_registry": "ghcr.io/t"})

        run_logs_base = tmp_path / "run-logs"
        services = run_logs_base / "services"
        services.mkdir(parents=True)
        compose_stdout = run_logs_base / "docker-compose.stdout.log"
        compose_stderr = run_logs_base / "docker-compose.stderr.log"
        (services / "exchange.log").write_text("exchange-ok")
        compose_stdout.write_text("compose-out")
        compose_stderr.write_text("compose-err")

        adapter._resolved_artifacts = {
            "run_logs": {
                "base": str(run_logs_base),
                "compose_stdout_log": str(compose_stdout),
                "compose_stderr_log": str(compose_stderr),
                "service_logs": str(services),
            }
        }
        trial = tmp_path / "trial_out"

        metadata = adapter.collect_results(trial, "harness1")
        logs_dir = Path(metadata["output_dir"]) / "logs"
        assert (logs_dir / "docker-compose.stdout.log").read_text() == "compose-out"
        assert (logs_dir / "docker-compose.stderr.log").read_text() == "compose-err"
        assert (logs_dir / "services" / "exchange.log").read_text() == "exchange-ok"
        assert metadata["run_logs_base_dir"] == str(run_logs_base)

    def test_collect_results_copies_crs_run_logs(self, tmp_path: Path) -> None:
        adapter = self._make_adapter(tmp_path)
        adapter.configure({"docker_registry": "ghcr.io/t"})

        crs_a = tmp_path / "run-logs" / "crs" / "test-crs"
        crs_b = tmp_path / "run-logs" / "crs" / "other-crs"
        crs_a.mkdir(parents=True)
        crs_b.mkdir(parents=True)
        (crs_a / "stdout.log").write_text("a")
        (crs_b / "stdout.log").write_text("b")
        nested = crs_a / "claude-logs" / "projects" / "-work-src-libexif"
        nested.mkdir(parents=True)
        (nested / "session.jsonl").write_text('{"event":"ok"}')

        adapter._resolved_artifacts = {
            "crs": {
                "test-crs": {"run_logs": str(crs_a)},
                "other-crs": {"run_logs": str(crs_b)},
            }
        }
        trial = tmp_path / "trial_out"

        metadata = adapter.collect_results(trial, "harness1")
        logs_dir = Path(metadata["output_dir"]) / "logs" / "crs"
        assert (logs_dir / "test-crs" / "stdout.log").read_text() == "a"
        assert (logs_dir / "other-crs" / "stdout.log").read_text() == "b"
        assert (
            logs_dir
            / "test-crs"
            / "claude-logs"
            / "projects"
            / "-work-src-libexif"
            / "session.jsonl"
        ).read_text() == '{"event":"ok"}'
        assert metadata["crs_run_logs_by_crs"] == {
            "other-crs": str(crs_b),
            "test-crs": str(crs_a),
        }

    def test_collect_results_copies_crs_log_dir_artifacts(self, tmp_path: Path) -> None:
        adapter = self._make_adapter(tmp_path)
        adapter.configure({"docker_registry": "ghcr.io/t"})

        crs_log_dir = tmp_path / "log-dir" / "crs" / "test-crs"
        crs_log_dir.mkdir(parents=True)
        (crs_log_dir / "agent.log").write_text("agent-ok")
        nested = crs_log_dir / "sessions"
        nested.mkdir(parents=True)
        (nested / "transcript.jsonl").write_text('{"event":"ok"}')

        adapter._resolved_artifacts = {
            "crs": {
                "test-crs": {"log_dir": str(crs_log_dir)},
            }
        }
        trial = tmp_path / "trial_out"

        metadata = adapter.collect_results(trial, "harness1")
        logs_dir = (
            Path(metadata["output_dir"]) / "logs" / "crs" / "test-crs" / "log_dir"
        )
        assert (logs_dir / "agent.log").read_text() == "agent-ok"
        assert (
            logs_dir / "sessions" / "transcript.jsonl"
        ).read_text() == '{"event":"ok"}'
        assert metadata["crs_log_dirs_by_crs"] == {
            "test-crs": str(crs_log_dir),
        }


# ===========================================================================
# OssCrsAdapter bug-fixing (ADAPT-02, COMPOSE-03, COMPOSE-04)
# ===========================================================================


class TestOssCrsAdapterBugFixFull:
    """Comprehensive tests for OssCrsAdapter (bug-fixing) lifecycle."""

    def _make_adapter(self, tmp_path: Path) -> OssCrsAdapter:
        """Create adapter with registry dir containing a valid oss-crs YAML."""
        registry = tmp_path / "registry"
        registry.mkdir(parents=True)
        (registry / "test-crs.yaml").write_text(
            yaml.dump(
                {
                    "source": {
                        "url": "https://github.com/team/crs.git",
                        "ref": "main",
                    }
                }
            )
        )
        cfg_dir = tmp_path / "configs" / "test-crs"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        (cfg_dir / "config-resource.yaml").write_text(
            yaml.dump(
                {
                    "workers": {"local": {"cpuset": "0-3", "memory": "8G"}},
                    "crs": {"test-crs": {"workers": ["local"]}},
                }
            )
        )
        adapter = OssCrsAdapter(
            crs_config_name="test-crs",
            oss_fuzz_path=tmp_path / "oss-fuzz",
            registry_dir=registry,
            benchmarks_root=tmp_path / "benchmarks",
            mode="bug-fixing",
        )
        adapter.configure(
            {
                "litellm_runtime_url": "https://litellm.example",
                "litellm_runtime_api_key": "sk-test-key",
            }
        )
        return adapter

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
    def test_run_passes_bug_candidate_dir(
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

        bug_candidates_dir = trial / "bug-candidates"
        bug_candidates_dir.mkdir()
        (bug_candidates_dir / "cpv_0.sarif").write_text("{}")

        adapter.build(bench, trial)
        harness = MagicMock()
        harness.name = "fuzz_target"

        adapter.run(bench, harness, trial)

        cmd = mock_rwgt.call_args[0][0]
        assert "--bug-candidate-dir" in cmd
        assert str(bug_candidates_dir) in cmd

    @patch("crsbench.evaluation.adapter.compose_common.run_with_graceful_timeout")
    @patch("crsbench.evaluation.adapter.compose_common.subprocess.run")
    def test_run_omits_bug_candidate_dir_when_not_present(
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

        adapter.run(bench, harness, trial)

        cmd = mock_rwgt.call_args[0][0]
        assert "--bug-candidate-dir" not in cmd
        assert "--bug-candidate" not in cmd

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

        patch_dir = tmp_path / "exchange" / "patch"
        patch_dir.mkdir(parents=True)
        (patch_dir / "fix.patch").write_text("--- a/bug.c\n+++ b/bug.c\n")

        adapter._resolved_artifacts = {
            "exchange_dir": {"patch": str(patch_dir)},
        }
        trial = tmp_path / "trial_out"

        metadata = adapter.collect_results(trial, "harness1")

        assert metadata["type"] == "bug-fixing"
        output_dir = Path(metadata["output_dir"])
        assert (output_dir / "patches" / "fix.patch").exists()

    def test_collect_results_returns_patches_list(self, tmp_path: Path) -> None:
        adapter = self._make_adapter(tmp_path)
        adapter.configure({"docker_registry": "ghcr.io/t"})

        patch_dir = tmp_path / "exchange" / "patch"
        patch_dir.mkdir(parents=True)
        (patch_dir / "fix1.patch").write_text("patch1")
        (patch_dir / "fix2.patch").write_text("patch2")

        adapter._resolved_artifacts = {
            "exchange_dir": {"patch": str(patch_dir)},
        }
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
            "inputs": {"pov": {"enabled": False}},
            "experiment_filestore": Path("/tmp/store"),
            "report_filestore": Path("/tmp/report"),
            "benchmarks": ["bench1"],
        }

    def test_accepts_strict_contract_with_crs_compose(self) -> None:
        from crsbench.validation.schemas import ExperimentConfig

        cfg = self._base_config()
        cfg["crs_compose"] = {
            "oss_crs_infra": {"num_cores": 1, "mem_limit": "8G"},
            "crs1": {"num_cores": 1, "mem_limit": "8G"},
        }

        config = ExperimentConfig(**cfg)
        assert config.crs_compose is not None
        assert config.crs_compose.oss_crs_infra.num_cores == 1

    def test_rejects_missing_crs_compose(self) -> None:
        from crsbench.validation.schemas import ExperimentConfig
        from pydantic import ValidationError

        cfg = self._base_config()
        with pytest.raises(ValidationError, match="crs_compose is required"):
            ExperimentConfig(**cfg)

    def test_rejects_legacy_adapter_field(self) -> None:
        from crsbench.validation.schemas import ExperimentConfig
        from pydantic import ValidationError

        cfg = self._base_config()
        cfg["adapter"] = "oss-crs"
        cfg["crs_compose"] = {
            "oss_crs_infra": {"num_cores": 1, "mem_limit": "8G"},
            "crs1": {"num_cores": 1, "mem_limit": "8G"},
        }

        with pytest.raises(ValidationError, match="extra_forbidden|Extra inputs"):
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

    def test_cleanup_issue_does_not_flip_run_successful(self, tmp_path: Path) -> None:
        adapter = MagicMock()
        adapter.run.return_value = self._make_success_result()
        adapter.collect_results.return_value = {"type": "bug-finding"}

        runner = self._make_runner_with_adapter(adapter)
        trial_dir = tmp_path / "trial"
        trial_dir.mkdir()
        harness = self._make_harness()

        with patch.object(
            runner, "_stop_managers", return_value="snapshot cleanup warning"
        ):
            result = runner._execute_crs_with_managers(
                harness=harness,
                benchmark_path=tmp_path,
                trial_output_dir=trial_dir,
                trial_start_time=0.0,
            )

        harness_result = result[0]
        assert harness_result.run_successful is True
        assert harness_result.run_output is not None
        assert "[cleanup] snapshot cleanup warning" in harness_result.run_output


# ===========================================================================
# Bug-fixing Input Staging (POV variant selection)
# ===========================================================================


class TestBugFixInputStaging:
    """Tests for BenchmarkRunner._prepare_bugfix_inputs variant selection."""

    @staticmethod
    def _make_benchmark_with_variants(tmp_path: Path) -> Path:
        benchmark = tmp_path / "benchmarks" / "test-project"
        aixcc = benchmark / ".aixcc"
        harness_dir = aixcc / "fuzz_target"
        (harness_dir / "cpv_0" / "blobs").mkdir(parents=True)
        (harness_dir / "cpv_1" / "blobs").mkdir(parents=True)

        # CPV 0 has three POV variants.
        (harness_dir / "cpv_0" / "blobs" / "pov_0.blob").write_bytes(b"a")
        (harness_dir / "cpv_0" / "blobs" / "pov_1.blob").write_bytes(b"b")
        (harness_dir / "cpv_0" / "blobs" / "pov_2.blob").write_bytes(b"c")
        # CPV 1 has one POV variant.
        (harness_dir / "cpv_1" / "blobs" / "pov_0.blob").write_bytes(b"d")

        (benchmark / "project.yaml").write_text(
            yaml.dump(
                {
                    "main_repo": "https://github.com/test/project.git",
                    "repo_name": "project",
                    "language": "c",
                }
            )
        )
        (aixcc / "meta.yaml").write_text(
            yaml.dump(
                {
                    "harness_files": [
                        {
                            "name": "fuzz_target",
                            "path": "/src/project/fuzz_target.c",
                            "vulns": [
                                {
                                    "vuln_keyword": "cpv_0",
                                    "povs": [
                                        {"id": "pov_0", "sanitizer": "address"},
                                        {"id": "pov_1", "sanitizer": "address"},
                                        {"id": "pov_2", "sanitizer": "address"},
                                    ],
                                },
                                {
                                    "vuln_keyword": "cpv_1",
                                    "povs": [
                                        {"id": "pov_0", "sanitizer": "address"},
                                    ],
                                },
                            ],
                        }
                    ],
                    "full_mode": {
                        "base_commit": "abc123def456789012345678901234567890abcd",
                    },
                }
            )
        )
        return benchmark

    @staticmethod
    def _make_runner(max_variants: int | None) -> BenchmarkRunner:
        adapter = MagicMock()
        adapter.mode = "bug-fixing"
        return BenchmarkRunner(
            adapter=adapter,
            snapshot_period=0,
            max_pov_variants_per_cpv=max_variants,
        )

    def test_prepare_bugfix_inputs_single_variant_per_cpv(self, tmp_path: Path) -> None:
        benchmark = self._make_benchmark_with_variants(tmp_path)
        trial_dir = tmp_path / "trial"
        trial_dir.mkdir()
        runner = self._make_runner(1)

        runner._prepare_bugfix_inputs(benchmark, "fuzz_target", trial_dir)

        staged = {p.name for p in (trial_dir / "povs").iterdir()}
        assert staged == {"cpv_0", "cpv_1"}
        assert (trial_dir / "crs-input" / "cpvs" / "cpv_0").exists()
        assert (trial_dir / "crs-input" / "cpvs" / "cpv_1").exists()

    def test_prepare_bugfix_inputs_multiple_variants_per_cpv(
        self, tmp_path: Path
    ) -> None:
        benchmark = self._make_benchmark_with_variants(tmp_path)
        trial_dir = tmp_path / "trial"
        trial_dir.mkdir()
        runner = self._make_runner(2)

        runner._prepare_bugfix_inputs(benchmark, "fuzz_target", trial_dir)

        staged = {p.name for p in (trial_dir / "povs").iterdir()}
        # cpv_0 has 2 staged variants, cpv_1 has only 1 available.
        assert staged == {"cpv_0", "cpv_0__pov_1", "cpv_1"}
        assert "cpv_0__pov_2" not in staged

    def test_prepare_bugfix_inputs_all_variants_when_unbounded(
        self, tmp_path: Path
    ) -> None:
        benchmark = self._make_benchmark_with_variants(tmp_path)
        trial_dir = tmp_path / "trial"
        trial_dir.mkdir()
        runner = self._make_runner(None)

        runner._prepare_bugfix_inputs(benchmark, "fuzz_target", trial_dir)

        staged = {p.name for p in (trial_dir / "povs").iterdir()}
        assert staged == {"cpv_0", "cpv_0__pov_1", "cpv_0__pov_2", "cpv_1"}

    def test_prepare_bugfix_inputs_target_cpv_only(self, tmp_path: Path) -> None:
        benchmark = self._make_benchmark_with_variants(tmp_path)
        trial_dir = tmp_path / "trial"
        trial_dir.mkdir()
        runner = self._make_runner(None)

        runner._prepare_bugfix_inputs(
            benchmark, "fuzz_target", trial_dir, target_cpv_id="cpv_1"
        )

        staged = {p.name for p in (trial_dir / "povs").iterdir()}
        assert staged == {"cpv_1"}
        assert (trial_dir / "crs-input" / "cpvs" / "cpv_1").exists()
        assert not (trial_dir / "crs-input" / "cpvs" / "cpv_0").exists()

    def test_prepare_bugfix_inputs_target_cpv_missing(self, tmp_path: Path) -> None:
        benchmark = self._make_benchmark_with_variants(tmp_path)
        trial_dir = tmp_path / "trial"
        trial_dir.mkdir()
        runner = self._make_runner(None)

        with pytest.raises(EvaluationError, match="Target CPV not found"):
            runner._prepare_bugfix_inputs(
                benchmark, "fuzz_target", trial_dir, target_cpv_id="cpv_missing"
            )

    def test_prepare_runtime_inputs_skips_pov_when_disabled(
        self, tmp_path: Path
    ) -> None:
        benchmark = self._make_benchmark_with_variants(tmp_path)
        trial_dir = tmp_path / "trial"
        trial_dir.mkdir()
        (trial_dir / "povs").mkdir()
        (trial_dir / "ref.diff").write_text("legacy")

        adapter = MagicMock()
        adapter.mode = "bug-fixing"
        runner = BenchmarkRunner(
            adapter=adapter,
            snapshot_period=0,
            pov_input_enabled=False,
            diff_input_enabled=False,
            seed_corpus_enabled=False,
        )

        runner._prepare_runtime_inputs(benchmark, "fuzz_target", trial_dir)

        assert not (trial_dir / "povs").exists()
        assert not (trial_dir / "ref.diff").exists()

    def test_prepare_runtime_inputs_stages_seed_and_diff(self, tmp_path: Path) -> None:
        benchmark = self._make_benchmark_with_variants(tmp_path)
        seed_dir = benchmark / ".aixcc" / "fuzz_target" / "seeds"
        seed_dir.mkdir(parents=True, exist_ok=True)
        (seed_dir / "seed_a").write_bytes(b"a")
        (seed_dir / "seed_b").write_bytes(b"b")
        (seed_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "files": {
                        "seed_a": {"relative_time": 5},
                        "seed_b": {"relative_time": 20},
                    }
                }
            )
        )
        ref_diff = benchmark / ".aixcc" / "ref.diff"
        ref_diff.write_text("--- a/f.c\n+++ b/f.c\n")

        trial_dir = tmp_path / "trial"
        trial_dir.mkdir()

        adapter = MagicMock()
        adapter.mode = "bug-fixing"
        runner = BenchmarkRunner(
            adapter=adapter,
            snapshot_period=0,
            pov_input_enabled=False,
            seed_corpus_enabled=True,
            seed_corpus_max_time=10,
            diff_input_enabled=True,
        )

        runner._prepare_runtime_inputs(benchmark, "fuzz_target", trial_dir)

        staged_seed_files = {p.name for p in (trial_dir / "seeds").iterdir()}
        assert staged_seed_files == {"seed_a"}
        assert not (trial_dir / "seeds" / "manifest.json").exists()
        assert (trial_dir / "ref.diff").exists()

    def test_prepare_runtime_inputs_stages_bug_candidates_from_sarif(
        self, tmp_path: Path
    ) -> None:
        benchmark = self._make_benchmark_with_variants(tmp_path)
        hints_dir = benchmark / ".aixcc" / "fuzz_target" / "cpv_0" / "hints"
        hints_dir.mkdir(parents=True, exist_ok=True)
        (hints_dir / "level_1.sarif").write_text('{"version":"2.1.0"}')

        trial_dir = tmp_path / "trial"
        trial_dir.mkdir()

        adapter = MagicMock()
        adapter.mode = "bug-fixing"
        runner = BenchmarkRunner(
            adapter=adapter,
            snapshot_period=0,
            pov_input_enabled=False,
            seed_corpus_enabled=False,
            diff_input_enabled=False,
            sarif_input_enabled=True,
            sarif_level=1,
        )

        runner._prepare_runtime_inputs(
            benchmark, "fuzz_target", trial_dir, target_cpv_id="cpv_0"
        )

        staged = trial_dir / "bug-candidates" / "cpv_0.sarif"
        assert staged.exists()

    def test_prepare_runtime_inputs_cleans_stale_bug_candidates(
        self, tmp_path: Path
    ) -> None:
        benchmark = self._make_benchmark_with_variants(tmp_path)
        trial_dir = tmp_path / "trial"
        trial_dir.mkdir()
        stale_dir = trial_dir / "bug-candidates"
        stale_dir.mkdir()
        (stale_dir / "stale.sarif").write_text("{}")

        adapter = MagicMock()
        adapter.mode = "bug-fixing"
        runner = BenchmarkRunner(
            adapter=adapter,
            snapshot_period=0,
            pov_input_enabled=False,
            seed_corpus_enabled=False,
            diff_input_enabled=False,
            sarif_input_enabled=False,
            sarif_level=None,
        )

        runner._prepare_runtime_inputs(benchmark, "fuzz_target", trial_dir)

        assert not stale_dir.exists()

    def test_prepare_runtime_inputs_replaces_stale_pov_dirs(
        self, tmp_path: Path
    ) -> None:
        benchmark = self._make_benchmark_with_variants(tmp_path)
        trial_dir = tmp_path / "trial"
        trial_dir.mkdir()
        stale_adapter = trial_dir / "povs"
        stale_input = trial_dir / "crs-input" / "povs"
        stale_cpvs = trial_dir / "crs-input" / "cpvs"
        stale_adapter.mkdir(parents=True)
        stale_input.mkdir(parents=True)
        stale_cpvs.mkdir(parents=True)
        (stale_adapter / "old").write_text("old")
        (stale_input / "old").write_text("old")
        (stale_cpvs / "old").mkdir()

        adapter = MagicMock()
        adapter.mode = "bug-fixing"
        runner = BenchmarkRunner(
            adapter=adapter,
            snapshot_period=0,
            max_pov_variants_per_cpv=1,
            pov_input_enabled=True,
        )

        runner._prepare_runtime_inputs(benchmark, "fuzz_target", trial_dir)

        assert not (stale_adapter / "old").exists()
        assert not (stale_input / "old").exists()
        assert not (stale_cpvs / "old").exists()

    def test_prepare_runtime_inputs_bugfinding_skips_pov_staging(
        self, tmp_path: Path
    ) -> None:
        """Bug-finding must never stage ground-truth POVs, even when pov_input_enabled."""
        benchmark = self._make_benchmark_with_variants(tmp_path)
        trial_dir = tmp_path / "trial"
        trial_dir.mkdir()

        adapter = MagicMock()
        adapter.mode = "bug-finding"
        runner = BenchmarkRunner(
            adapter=adapter,
            snapshot_period=0,
            max_pov_variants_per_cpv=1,
            pov_input_enabled=True,
            seed_corpus_enabled=False,
            diff_input_enabled=False,
            sarif_input_enabled=False,
        )

        runner._prepare_runtime_inputs(
            benchmark, "fuzz_target", trial_dir, target_cpv_id="cpv_missing"
        )

        # POV staging must be skipped for bug-finding to avoid leaking answers.
        assert not (trial_dir / "povs").exists()
        assert not (trial_dir / "crs-input" / "povs").exists()

    def test_prepare_runtime_inputs_fails_when_sarif_enabled_but_missing(
        self, tmp_path: Path
    ) -> None:
        benchmark = self._make_benchmark_with_variants(tmp_path)
        trial_dir = tmp_path / "trial"
        trial_dir.mkdir()

        adapter = MagicMock()
        adapter.mode = "bug-fixing"
        runner = BenchmarkRunner(
            adapter=adapter,
            snapshot_period=0,
            pov_input_enabled=False,
            sarif_input_enabled=True,
            sarif_level=1,
        )

        with pytest.raises(
            EvaluationError, match="SARIF bug-candidate input enabled but no matching"
        ):
            runner._prepare_runtime_inputs(benchmark, "fuzz_target", trial_dir)

    def test_prepare_runtime_inputs_fails_when_seed_enabled_but_missing(
        self, tmp_path: Path
    ) -> None:
        benchmark = self._make_benchmark_with_variants(tmp_path)
        trial_dir = tmp_path / "trial"
        trial_dir.mkdir()

        adapter = MagicMock()
        adapter.mode = "bug-fixing"
        runner = BenchmarkRunner(
            adapter=adapter,
            snapshot_period=0,
            pov_input_enabled=False,
            seed_corpus_enabled=True,
        )

        with pytest.raises(
            EvaluationError, match="Seed corpus input enabled but unavailable"
        ):
            runner._prepare_runtime_inputs(benchmark, "fuzz_target", trial_dir)

    def test_prepare_runtime_inputs_fails_when_diff_enabled_but_missing(
        self, tmp_path: Path
    ) -> None:
        benchmark = self._make_benchmark_with_variants(tmp_path)
        trial_dir = tmp_path / "trial"
        trial_dir.mkdir()

        adapter = MagicMock()
        adapter.mode = "bug-fixing"
        runner = BenchmarkRunner(
            adapter=adapter,
            snapshot_period=0,
            pov_input_enabled=False,
            diff_input_enabled=True,
        )

        with pytest.raises(
            EvaluationError,
            match="Diff input enabled but benchmark ref.diff is missing",
        ):
            runner._prepare_runtime_inputs(benchmark, "fuzz_target", trial_dir)


class TestBugFixPatchStatsCollection:
    """Tests for bug-fixing patch stats collection/reporting."""

    def test_sets_total_input_povs_even_when_no_patch_results(
        self, tmp_path: Path
    ) -> None:
        adapter = MagicMock()
        adapter.mode = "bug-fixing"
        runner = BenchmarkRunner(adapter=adapter, snapshot_period=0)
        collector = MagicMock()

        trial_dir = tmp_path / "trial"
        povs_dir = trial_dir / "crs-input" / "povs"
        povs_dir.mkdir(parents=True)
        (povs_dir / "cpv_0").write_bytes(b"a")
        (povs_dir / "cpv_1").write_bytes(b"b")

        runner._collect_crs_results(
            collector=collector,
            trial_output_dir=trial_dir,
            pov_verification_results=[],
            patch_verification_results=[],
        )

        collector.set_patch_stats.assert_called_once_with(2, 0, [])

    def test_total_input_povs_ignores_hidden_and_directories(
        self, tmp_path: Path
    ) -> None:
        adapter = MagicMock()
        adapter.mode = "bug-fixing"
        runner = BenchmarkRunner(adapter=adapter, snapshot_period=0)
        collector = MagicMock()

        trial_dir = tmp_path / "trial"
        povs_dir = trial_dir / "crs-input" / "povs"
        povs_dir.mkdir(parents=True)
        (povs_dir / "cpv_0").write_bytes(b"a")
        (povs_dir / ".hidden").write_bytes(b"hidden")
        (povs_dir / "nested").mkdir()

        runner._collect_crs_results(
            collector=collector,
            trial_output_dir=trial_dir,
            pov_verification_results=[],
            patch_verification_results=[],
        )

        collector.set_patch_stats.assert_called_once_with(1, 0, [])

    def test_counts_produced_patches_when_verification_skipped(
        self, tmp_path: Path
    ) -> None:
        adapter = MagicMock()
        adapter.mode = "bug-fixing"
        runner = BenchmarkRunner(adapter=adapter, snapshot_period=0)
        collector = MagicMock()

        trial_dir = tmp_path / "trial"
        povs_dir = trial_dir / "crs-input" / "povs"
        patch_dir = trial_dir / "output" / "patches"
        povs_dir.mkdir(parents=True)
        patch_dir.mkdir(parents=True)
        (povs_dir / "cpv_0").write_bytes(b"a")
        (patch_dir / "patch_0.diff").write_text("diff --git a b")
        (patch_dir / "patch_1.diff").write_text("diff --git a b")

        runner._collect_crs_results(
            collector=collector,
            trial_output_dir=trial_dir,
            pov_verification_results=[],
            patch_verification_results=[],
            target_cpv_id="cpv_0",
        )

        collector.set_patch_stats.assert_called_once_with(1, 2, [])


# ===========================================================================
# Benchmark Staging (Ground Truth Leakage Prevention)
# ===========================================================================


class TestStageBenchmark:
    """Tests for _stage_benchmark() dotfile filtering and file staging."""

    def _make_adapter(self, tmp_path: Path) -> OssCrsAdapter:
        registry = tmp_path / "registry"
        registry.mkdir(parents=True)
        (registry / "test-crs.yaml").write_text(
            yaml.dump(
                {
                    "source": {
                        "url": "https://github.com/team/crs.git",
                        "ref": "main",
                    }
                }
            )
        )
        cfg_dir = tmp_path / "configs" / "test-crs"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        (cfg_dir / "config-resource.yaml").write_text(
            yaml.dump(
                {
                    "workers": {"local": {"cpuset": "0-3", "memory": "8G"}},
                    "crs": {"test-crs": {"workers": ["local"]}},
                }
            )
        )
        adapter = OssCrsAdapter(
            crs_config_name="test-crs",
            oss_fuzz_path=tmp_path / "oss-fuzz",
            registry_dir=registry,
            benchmarks_root=tmp_path / "benchmarks",
            mode="bug-finding",
        )
        adapter.configure(
            {
                "litellm_runtime_url": "https://litellm.example",
                "litellm_runtime_api_key": "sk-test-key",
            }
        )
        return adapter

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
        target_path_idx = build_target_cmd.index("--fuzz-proj-path") + 1
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
        target_path_idx = run_cmd.index("--fuzz-proj-path") + 1
        target_path = run_cmd[target_path_idx]
        assert "/staged/" in target_path
        assert target_path != str(bench)
