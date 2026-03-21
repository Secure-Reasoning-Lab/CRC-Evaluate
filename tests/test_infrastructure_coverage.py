"""Tests for coverage-related infrastructure methods.

Tests:
- has_harness(): Check if harness exists in build output
- run_coverage(): Run coverage and copy helper output into requested directory
- MetaYamlAdapter.from_benchmark_path(): Load adapter from benchmark dir
"""

import fcntl
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from crsbench.builder.infrastructure import OSSFuzzInfrastructure
from crsbench.validation.meta_adapter import MetaYamlAdapter


@pytest.fixture
def mock_oss_fuzz(tmp_path: Path) -> Path:
    """Create mock oss-fuzz directory with coverage helper."""
    oss_fuzz = tmp_path / "oss-fuzz"
    (oss_fuzz / "infra").mkdir(parents=True)
    (oss_fuzz / "projects").mkdir()
    (oss_fuzz / "build" / "out").mkdir(parents=True)

    # Mock helper.py presence; run_coverage() itself is unit-tested by mocking
    # subprocess execution.
    helper = oss_fuzz / "infra" / "helper.py"
    helper.write_text("#!/usr/bin/env python3\n")
    return oss_fuzz


class TestHasHarness:
    """Tests for has_harness()."""

    def test_exists_returns_true(self, mock_oss_fuzz: Path):
        """Harness file exists → True."""
        build = mock_oss_fuzz / "build" / "out" / "proj"
        build.mkdir(parents=True)
        (build / "fuzz").write_text("binary")

        infra = OSSFuzzInfrastructure(mock_oss_fuzz)
        assert infra.has_harness("proj", "fuzz") is True

    def test_missing_returns_false(self, mock_oss_fuzz: Path):
        """Harness doesn't exist → False."""
        infra = OSSFuzzInfrastructure(mock_oss_fuzz)
        assert infra.has_harness("proj", "fuzz") is False


class TestRunCoverage:
    """Tests for run_coverage()."""

    def test_uses_supported_helper_flags_and_copies_outputs(
        self, mock_oss_fuzz: Path, tmp_path: Path
    ):
        """Verify helper.py uses supported flags and outputs are copied."""
        corpus = tmp_path / "seeds"
        corpus.mkdir()
        (corpus / "input").write_bytes(b"test")
        output = tmp_path / "out"
        project_out = mock_oss_fuzz / "build" / "out" / "proj"

        def _fake_helper_run(cmd, timeout, **kwargs):
            del cmd, timeout, kwargs
            report_dir = project_out / "report" / "linux"
            dumps_dir = project_out / "dumps"
            report_dir.mkdir(parents=True, exist_ok=True)
            dumps_dir.mkdir(parents=True, exist_ok=True)
            (report_dir / "summary.json").write_text('{"data": [{"totals": {}}]}')
            (dumps_dir / "merged.profdata").write_text("profdata")
            return MagicMock(returncode=0, stdout="", stderr="")

        with (
            patch("crsbench.builder.infrastructure.run_with_timeout") as mock_run,
            patch("crsbench.builder.infrastructure.docker_rmtree", return_value=True),
            patch(
                "crsbench.builder.infrastructure.fix_docker_ownership",
                return_value=True,
            ),
        ):
            mock_run.side_effect = _fake_helper_run
            infra = OSSFuzzInfrastructure(mock_oss_fuzz)
            success, returned_output = infra.run_coverage(
                "proj", "fuzz", corpus, output
            )

            cmd = mock_run.call_args[0][0]
            assert success is True
            assert returned_output == output
            assert "--coverage-output-dir" not in cmd
            assert "--timeout" not in cmd
            assert "--port" in cmd
            assert "--corpus-dir" in cmd
            assert "--fuzz-target" in cmd
            assert str(corpus) in cmd
            assert (output / "report" / "linux" / "summary.json").exists()
            assert (output / "dumps" / "merged.profdata").exists()

    def test_run_coverage_serializes_shared_project_output(
        self, mock_oss_fuzz: Path, tmp_path: Path
    ):
        corpus = tmp_path / "seeds"
        corpus.mkdir()
        (corpus / "input").write_bytes(b"test")
        output_a = tmp_path / "out-a"
        output_b = tmp_path / "out-b"
        project_out = mock_oss_fuzz / "build" / "out" / "proj"
        first_started = threading.Event()
        allow_first_finish = threading.Event()
        second_started = threading.Event()
        second_waiting_for_lock = threading.Event()
        second_blocking_lock_attempt = threading.Event()
        helper_call_count = 0
        results: list[tuple[bool, Path]] = []

        def _fake_helper_run(cmd, timeout, **kwargs):
            del cmd, timeout, kwargs
            nonlocal helper_call_count
            helper_call_count += 1
            if helper_call_count == 1:
                first_started.set()
                allow_first_finish.wait(timeout=2)
            else:
                second_started.set()
            report_dir = project_out / "report" / "linux"
            dumps_dir = project_out / "dumps"
            report_dir.mkdir(parents=True, exist_ok=True)
            dumps_dir.mkdir(parents=True, exist_ok=True)
            (report_dir / "summary.json").write_text('{"data": [{"totals": {}}]}')
            return MagicMock(returncode=0, stdout="", stderr="")

        def _run_coverage(output_dir: Path):
            infra = OSSFuzzInfrastructure(mock_oss_fuzz)
            results.append(infra.run_coverage("proj", "fuzz", corpus, output_dir))

        real_flock = fcntl.flock
        blocking_lock_seen = False

        def _instrumented_flock(fd: int, operation: int):
            nonlocal blocking_lock_seen
            if operation == fcntl.LOCK_EX and not blocking_lock_seen:
                try:
                    return real_flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    blocking_lock_seen = True
                    second_waiting_for_lock.set()
                    result = real_flock(fd, fcntl.LOCK_EX)
                    second_blocking_lock_attempt.set()
                    return result
            return real_flock(fd, operation)

        with (
            patch("crsbench.builder.infrastructure.run_with_timeout") as mock_run,
            patch("crsbench.builder.infrastructure.docker_rmtree", return_value=True),
            patch(
                "crsbench.builder.infrastructure.fix_docker_ownership",
                return_value=True,
            ),
            patch(
                "crsbench.builder.infrastructure.fcntl.flock",
                side_effect=_instrumented_flock,
            ),
            patch.dict(
                "os.environ",
                {"CRSBENCH_COVERAGE_LOCK_DIR": str(tmp_path / "locks")},
                clear=False,
            ),
        ):
            mock_run.side_effect = _fake_helper_run
            thread_a = threading.Thread(target=_run_coverage, args=(output_a,))
            thread_b = threading.Thread(target=_run_coverage, args=(output_b,))
            thread_a.start()
            assert first_started.wait(timeout=1)

            thread_b.start()
            assert second_waiting_for_lock.wait(timeout=1)
            assert second_blocking_lock_attempt.is_set() is False

            allow_first_finish.set()
            thread_a.join(timeout=2)
            thread_b.join(timeout=2)

        assert second_started.is_set() is True
        assert second_blocking_lock_attempt.is_set() is True
        assert helper_call_count == 2
        assert len(results) == 2
        assert all(success for success, _ in results)
        assert (output_a / "report" / "linux" / "summary.json").exists()
        assert (output_b / "report" / "linux" / "summary.json").exists()


class TestMetaYamlAdapterFromBenchmarkPath:
    """Tests for MetaYamlAdapter.from_benchmark_path()."""

    def test_valid_benchmark(self, tmp_path: Path):
        """Load valid benchmark with meta.yaml and project.yaml."""
        bench = tmp_path / "test-bench"
        bench.mkdir()
        (bench / ".aixcc").mkdir()
        (bench / ".aixcc" / "meta.yaml").write_text("""
delta_mode:
  ref_commit: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
  base_commit: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
harness_files:
  - name: fuzz
    path: /src/fuzz.c
    vulns:
      - vuln_keyword: cpv_0
        povs: [{id: pov_0, name: pov_0, path: cpv_0/pov_0, sanitizer: address}]
""")
        (bench / "project.yaml").write_text("language: c\nmain_repo: https://test.com")

        adapter = MetaYamlAdapter.from_benchmark_path(bench)
        assert adapter is not None
        assert adapter.benchmark_name == "test-bench"
        assert adapter.lang == "c"

    def test_missing_meta_returns_none(self, tmp_path: Path):
        """Missing meta.yaml → None."""
        bench = tmp_path / "test-bench"
        bench.mkdir()
        (bench / ".aixcc").mkdir()

        assert MetaYamlAdapter.from_benchmark_path(bench) is None

    def test_missing_project_yaml_uses_defaults(self, tmp_path: Path):
        """Missing project.yaml → use defaults (lang=c)."""
        bench = tmp_path / "test-bench"
        bench.mkdir()
        (bench / ".aixcc").mkdir()
        (bench / ".aixcc" / "meta.yaml").write_text("""
delta_mode:
  ref_commit: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
  base_commit: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
harness_files:
  - name: fuzz
    path: /src/fuzz.c
    vulns:
      - vuln_keyword: cpv_0
        povs: [{id: pov_0, name: pov_0, path: cpv_0/pov_0, sanitizer: address}]
""")

        adapter = MetaYamlAdapter.from_benchmark_path(bench)
        assert adapter is not None
        assert adapter.lang == "c"
        assert adapter.main_repo == ""
