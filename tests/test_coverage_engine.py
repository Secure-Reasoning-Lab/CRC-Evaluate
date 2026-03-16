"""Tests for CoverageEngine.

Essential tests for:
- Worker configuration
- Thread-safe coverage merging
- Summary computation
- Sequential coverage collection
- Variant building
"""

import json
from pathlib import Path
from typing import Generator
from unittest.mock import MagicMock, patch

import pytest
import yaml
from crsbench.evaluation.coverage.backend import CoverageRunResult
from crsbench.evaluation.coverage.engine import UNIAFL_BUILD_SENTINEL, CoverageEngine
from crsbench.evaluation.coverage.models import TimedCoverageInput
from crsbench.evaluation.coverage.strategy import (
    CoverageStrategyError,
    JaCoCoLineStrategy,
)


@pytest.fixture
def mock_oss_fuzz(tmp_path: Path) -> Path:
    """Create mock OSS-Fuzz directory."""
    oss_fuzz = tmp_path / "oss-fuzz"
    (oss_fuzz / "infra").mkdir(parents=True)
    (oss_fuzz / "infra" / "helper.py").touch()
    (oss_fuzz / "projects").mkdir()
    (oss_fuzz / "build" / "out").mkdir(parents=True)
    return oss_fuzz


@pytest.fixture
def mock_benchmark(tmp_path: Path) -> Path:
    """Create mock benchmark with meta.yaml and project.yaml."""
    benchmark = tmp_path / "test-benchmark"
    benchmark.mkdir()
    aixcc = benchmark / ".aixcc"
    aixcc.mkdir()

    meta = {
        "delta_mode": {"ref_commit": "a" * 40, "base_commit": "b" * 40},
        "harness_files": [
            {
                "name": "fuzz_target",
                "path": "/src/fuzz.c",
                "vulns": [
                    {
                        "vuln_keyword": "cpv_0",
                        "povs": [
                            {
                                "id": "pov_0",
                                "name": "pov_0",
                                "path": "cpv_0/pov_0",
                                "sanitizer": "address",
                            }
                        ],
                    }
                ],
            }
        ],
    }
    (aixcc / "meta.yaml").write_text(yaml.dump(meta))
    (benchmark / "project.yaml").write_text(
        yaml.dump({"language": "c", "main_repo": "https://example.com/repo"})
    )
    return benchmark


@pytest.fixture
def mock_corpus(tmp_path: Path) -> Path:
    """Create mock corpus directory with 3 files."""
    corpus = tmp_path / "seeds"
    corpus.mkdir()
    for i in range(3):
        (corpus / f"input_{i}").write_bytes(f"content{i}".encode())
    return corpus


@pytest.fixture
def engine(mock_oss_fuzz: Path) -> Generator[CoverageEngine, None, None]:
    """Create CoverageEngine with cleanup."""
    eng = CoverageEngine(mock_oss_fuzz, build_workers=2)
    yield eng
    eng.cleanup()


class TestCoverageEngine:
    """Essential tests for CoverageEngine."""

    def test_worker_configuration(self, mock_oss_fuzz: Path):
        """Test worker counts are set correctly."""
        eng = CoverageEngine(mock_oss_fuzz, build_workers=8)
        assert eng.build_workers == 8
        # Note: verify_workers removed - parallelism handled by DAGExecutor

    def test_default_workspace_uses_repo_local_atlantis_cache(self) -> None:
        """Coverage analysis can run without an OSS-Fuzz checkout."""
        eng = CoverageEngine(build_workers=1)
        variant_name = "test-benchmark-cov-delta-coverage"
        expected = (
            Path(__file__).resolve().parents[1]
            / ".crsbench-coverage"
            / variant_name
            / "build"
            / "out"
        )
        assert eng.infra.get_build_output_path(variant_name) == expected

    def test_merge_overlapping_coverage(self, engine: CoverageEngine):
        """Test merging coverage deduplicates overlapping lines."""
        merged = {"main": {"src": "main.c", "lines": {1, 2, 3}}}
        engine._merge_coverage_safe(
            merged, {"main": {"src": "main.c", "lines": [3, 4, 5]}}
        )
        assert merged["main"]["lines"] == {1, 2, 3, 4, 5}

    def test_merge_multiple_functions(self, engine: CoverageEngine):
        """Test merging coverage across multiple functions."""
        merged: dict = {}
        engine._merge_coverage_safe(merged, {"func1": {"src": "a.c", "lines": [1, 2]}})
        engine._merge_coverage_safe(merged, {"func2": {"src": "b.c", "lines": [10]}})
        assert "func1" in merged
        assert "func2" in merged

    def test_compute_summary(self, engine: CoverageEngine):
        """Test summary computation uses totals from batch coverage."""
        merged = {
            "func1": {"src": "a.c", "lines": {1, 2, 3}},
            "func2": {"src": "b.c", "lines": {10, 11}},
        }
        totals = {
            "lines_covered": 50,
            "lines_total": 100,
            "lines_percent": 50.0,
            "functions_covered": 10,
            "functions_total": 20,
        }
        summary = engine._compute_summary(merged, 10, 8, 6, totals)

        # Values come from totals dict, not merged_coverage
        assert summary.lines_covered == 50
        assert summary.lines_total == 100
        assert summary.lines_percent == 50.0
        assert summary.functions_covered == 10
        assert summary.functions_total == 20
        assert summary.corpus_total == 10
        assert summary.corpus_contributing == 8
        assert summary.corpus_unique == 6

    @patch("crsbench.evaluation.coverage.engine.create_coverage_strategy")
    def test_sequential_corpus_processing(
        self,
        mock_create_strategy,
        mock_oss_fuzz: Path,
        mock_benchmark: Path,
        mock_corpus: Path,
    ):
        """Test corpus files are processed through the warm session backend."""
        mock_strategy = MagicMock()
        session = MagicMock()
        session.__enter__.return_value = session
        session.__exit__.return_value = False
        session.collect_many.return_value = {
            corpus_file: CoverageRunResult(
                coverage_data={"main": {"src": "main.c", "lines": [1, 2, 3]}}
            )
            for corpus_file in sorted(mock_corpus.iterdir())
        }
        session.collect_batch_totals.return_value = {
            "lines_covered": 10,
            "lines_total": 100,
            "lines_percent": 10.0,
            "functions_covered": 5,
            "functions_total": 20,
        }
        mock_strategy.open_session.return_value = session
        mock_create_strategy.return_value = mock_strategy

        # No verify_workers - parallelism handled by DAGExecutor
        eng = CoverageEngine(mock_oss_fuzz)

        with (
            patch.object(
                eng,
                "_build_coverage_variant",
                return_value="test-benchmark-cov-delta-coverage",
            ),
            patch.object(eng.infra, "has_harness", return_value=True),
            patch.object(eng, "_maybe_open_session", return_value=session),
        ):
            report = eng.collect_coverage(mock_benchmark, mock_corpus)

        session.collect_many.assert_called_once()
        session.collect_batch_totals.assert_called_once_with(mock_corpus)
        mock_strategy.collect_single_coverage.assert_not_called()
        assert report.final_summary.corpus_total == 3
        assert report.final_summary.lines_total == 100
        assert report.harness_name == "fuzz_target"

    def test_build_variant_success(self, mock_benchmark: Path, engine: CoverageEngine):
        """Test successful Atlantis-backed coverage variant build."""
        adapter = engine._load_adapter(mock_benchmark)
        assert adapter is not None
        variant_name = "test-benchmark-cov-delta-coverage"
        build_output_dir = engine.infra.get_build_output_path(variant_name)
        control_root = build_output_dir.parent / f".{variant_name}-oss-crs"
        atlantis_out = build_output_dir.parent / "atlantis-build"

        build = MagicMock(
            build_id="build-123",
            compose_file=control_root / "crs-compose.yaml",
            control_root=control_root,
            atlantis_build_output_dir=atlantis_out,
        )

        with (
            patch(
                "crsbench.evaluation.coverage.engine.build_atlantis_coverage_artifacts",
                return_value=build,
            ) as mock_build,
            patch(
                "crsbench.evaluation.coverage.engine.current_uniafl_checkout_fingerprint",
                return_value="fingerprint-1",
            ),
            patch(
                "crsbench.evaluation.coverage.engine.current_prepare_image_ids",
                return_value={"multilang-given_fuzzer-crs:latest": "sha256:test"},
            ),
            patch(
                "crsbench.evaluation.coverage.engine.fix_docker_ownership"
            ) as mock_fix_ownership,
            patch.object(engine.infra, "write_build_metadata") as mock_write_meta,
        ):
            result = engine._build_coverage_variant(adapter)

        assert result == variant_name
        mock_build.assert_called_once_with(
            benchmark_path=mock_benchmark,
            normalized_build_output_dir=build_output_dir,
            control_root=control_root,
        )
        sentinel = json.loads((build_output_dir / UNIAFL_BUILD_SENTINEL).read_text())
        assert sentinel["variant_name"] == variant_name
        assert sentinel["build_id"] == "build-123"
        assert sentinel["checkout_fingerprint"] == "fingerprint-1"
        assert sentinel["prepare_image_ids"] == {
            "multilang-given_fuzzer-crs:latest": "sha256:test"
        }
        mock_fix_ownership.assert_called_once_with(build_output_dir)
        mock_write_meta.assert_called_once()

    def test_build_variant_uses_default_atlantis_checkout(
        self, mock_benchmark: Path, mock_oss_fuzz: Path
    ):
        eng = CoverageEngine(mock_oss_fuzz)
        adapter = eng._load_adapter(mock_benchmark)
        assert adapter is not None
        variant_name = "test-benchmark-cov-delta-coverage"
        build_output_dir = eng.infra.get_build_output_path(variant_name)
        control_root = build_output_dir.parent / f".{variant_name}-oss-crs"

        with (
            patch(
                "crsbench.evaluation.coverage.engine.build_atlantis_coverage_artifacts",
                return_value=MagicMock(
                    build_id="build-123",
                    compose_file=Path("/tmp/compose.yaml"),
                    control_root=control_root,
                    atlantis_build_output_dir=Path("/tmp/out"),
                ),
            ) as mock_build,
            patch(
                "crsbench.evaluation.coverage.engine.current_uniafl_checkout_fingerprint",
                return_value="fingerprint-1",
            ),
            patch(
                "crsbench.evaluation.coverage.engine.current_prepare_image_ids",
                return_value={"multilang-given_fuzzer-crs:latest": "sha256:test"},
            ),
            patch("crsbench.evaluation.coverage.engine.fix_docker_ownership"),
            patch.object(eng.infra, "write_build_metadata"),
        ):
            result = eng._build_coverage_variant(adapter)

        assert result == variant_name
        mock_build.assert_called_once_with(
            benchmark_path=mock_benchmark,
            normalized_build_output_dir=build_output_dir,
            control_root=control_root,
        )

    def test_build_variant_uses_standard_coverage_variant_suffix(
        self,
        mock_benchmark: Path,
        engine: CoverageEngine,
    ):
        """Coverage builds must keep the standard cov/mode/coverage suffix."""
        adapter = engine._load_adapter(mock_benchmark)
        assert adapter is not None

        with (
            patch(
                "crsbench.evaluation.coverage.engine.build_atlantis_coverage_artifacts",
                return_value=MagicMock(
                    build_id="build-123",
                    compose_file=Path("/tmp/compose.yaml"),
                    control_root=Path("/tmp/control"),
                    atlantis_build_output_dir=Path("/tmp/out"),
                ),
            ),
            patch(
                "crsbench.evaluation.coverage.engine.current_uniafl_checkout_fingerprint",
                return_value="fingerprint-1",
            ),
            patch(
                "crsbench.evaluation.coverage.engine.current_prepare_image_ids",
                return_value={"multilang-given_fuzzer-crs:latest": "sha256:test"},
            ),
            patch("crsbench.evaluation.coverage.engine.fix_docker_ownership"),
            patch.object(engine.infra, "write_build_metadata"),
        ):
            result = engine._build_coverage_variant(adapter)

        assert result == "test-benchmark-cov-delta-coverage"

    def test_build_variant_failure(self, mock_benchmark: Path, engine: CoverageEngine):
        """Test failed build returns None."""
        adapter = engine._load_adapter(mock_benchmark)
        assert adapter is not None

        with (
            patch(
                "crsbench.evaluation.coverage.engine.build_atlantis_coverage_artifacts",
                side_effect=RuntimeError("boom"),
            ),
            patch(
                "crsbench.evaluation.coverage.engine.current_uniafl_checkout_fingerprint",
                return_value="fingerprint-1",
            ),
            patch(
                "crsbench.evaluation.coverage.engine.current_prepare_image_ids",
                return_value={"multilang-given_fuzzer-crs:latest": "sha256:test"},
            ),
        ):
            result = engine._build_coverage_variant(adapter)

        assert result is None

    def test_build_variant_reuses_existing_jvm_build(
        self, mock_benchmark: Path, engine: CoverageEngine
    ):
        """Reuse an existing UniAFL JVM build instead of recompiling."""
        adapter = engine._load_adapter(mock_benchmark)
        adapter.lang = "jvm"
        variant_name = "test-benchmark-cov-delta-coverage"
        build_output_dir = engine.infra.get_build_output_path(variant_name)
        build_output_dir.mkdir(parents=True, exist_ok=True)
        (build_output_dir / ".crsbench-repo").mkdir()
        (build_output_dir / UNIAFL_BUILD_SENTINEL).write_text(
            json.dumps(
                {
                    "checkout_fingerprint": "fingerprint-1",
                    "prepare_image_ids": {
                        "multilang-given_fuzzer-crs:latest": "sha256:test"
                    },
                }
            )
        )
        (build_output_dir / "fuzz_target").write_text("wrapper")

        with (
            patch(
                "crsbench.evaluation.coverage.engine.build_atlantis_coverage_artifacts"
            ) as mock_build,
            patch(
                "crsbench.evaluation.coverage.engine.current_uniafl_checkout_fingerprint",
                return_value="fingerprint-1",
            ),
            patch(
                "crsbench.evaluation.coverage.engine.current_prepare_image_ids",
                return_value={"multilang-given_fuzzer-crs:latest": "sha256:test"},
            ),
        ):
            result = engine._build_coverage_variant(adapter)

        assert result == variant_name
        mock_build.assert_not_called()

    def test_build_variant_reuses_existing_native_build(
        self, mock_benchmark: Path, engine: CoverageEngine
    ):
        """Reuse an existing UniAFL native build only when coverage-out exists."""
        adapter = engine._load_adapter(mock_benchmark)
        variant_name = "test-benchmark-cov-delta-coverage"
        build_output_dir = engine.infra.get_build_output_path(variant_name)
        build_output_dir.mkdir(parents=True, exist_ok=True)
        (build_output_dir / ".crsbench-repo").mkdir()
        (build_output_dir / UNIAFL_BUILD_SENTINEL).write_text(
            json.dumps(
                {
                    "checkout_fingerprint": "fingerprint-1",
                    "prepare_image_ids": {
                        "multilang-given_fuzzer-crs:latest": "sha256:test"
                    },
                }
            )
        )
        (build_output_dir / "coverage-out").mkdir()

        with (
            patch(
                "crsbench.evaluation.coverage.engine.build_atlantis_coverage_artifacts"
            ) as mock_build,
            patch(
                "crsbench.evaluation.coverage.engine.current_uniafl_checkout_fingerprint",
                return_value="fingerprint-1",
            ),
            patch(
                "crsbench.evaluation.coverage.engine.current_prepare_image_ids",
                return_value={"multilang-given_fuzzer-crs:latest": "sha256:test"},
            ),
        ):
            result = engine._build_coverage_variant(adapter)

        assert result == variant_name
        mock_build.assert_not_called()

    def test_build_variant_rebuilds_when_checkout_fingerprint_changes(
        self, mock_benchmark: Path, engine: CoverageEngine
    ):
        adapter = engine._load_adapter(mock_benchmark)
        assert adapter is not None
        variant_name = "test-benchmark-cov-delta-coverage"
        build_output_dir = engine.infra.get_build_output_path(variant_name)
        build_output_dir.mkdir(parents=True, exist_ok=True)
        (build_output_dir / ".crsbench-repo").mkdir()
        (build_output_dir / UNIAFL_BUILD_SENTINEL).write_text(
            json.dumps(
                {
                    "checkout_fingerprint": "old-fingerprint",
                    "prepare_image_ids": {
                        "multilang-given_fuzzer-crs:latest": "sha256:test"
                    },
                }
            )
        )
        (build_output_dir / "coverage-out").mkdir()

        with (
            patch(
                "crsbench.evaluation.coverage.engine.current_uniafl_checkout_fingerprint",
                return_value="new-fingerprint",
            ),
            patch(
                "crsbench.evaluation.coverage.engine.current_prepare_image_ids",
                return_value={"multilang-given_fuzzer-crs:latest": "sha256:test"},
            ),
            patch(
                "crsbench.evaluation.coverage.engine.build_atlantis_coverage_artifacts",
                return_value=MagicMock(
                    build_id="build-123",
                    compose_file=Path("/tmp/compose.yaml"),
                    control_root=Path("/tmp/control"),
                    atlantis_build_output_dir=Path("/tmp/out"),
                ),
            ) as mock_build,
            patch("crsbench.evaluation.coverage.engine.fix_docker_ownership"),
            patch.object(engine.infra, "write_build_metadata"),
        ):
            result = engine._build_coverage_variant(adapter)

        assert result == variant_name
        mock_build.assert_called_once()

    def test_build_variant_rebuilds_when_prepare_image_ids_change(
        self, mock_benchmark: Path, engine: CoverageEngine
    ):
        adapter = engine._load_adapter(mock_benchmark)
        assert adapter is not None
        variant_name = "test-benchmark-cov-delta-coverage"
        build_output_dir = engine.infra.get_build_output_path(variant_name)
        build_output_dir.mkdir(parents=True, exist_ok=True)
        (build_output_dir / ".crsbench-repo").mkdir()
        (build_output_dir / UNIAFL_BUILD_SENTINEL).write_text(
            json.dumps(
                {
                    "checkout_fingerprint": "fingerprint-1",
                    "prepare_image_ids": {
                        "multilang-given_fuzzer-crs:latest": "sha256:old"
                    },
                }
            )
        )
        (build_output_dir / "coverage-out").mkdir()

        with (
            patch(
                "crsbench.evaluation.coverage.engine.current_uniafl_checkout_fingerprint",
                return_value="fingerprint-1",
            ),
            patch(
                "crsbench.evaluation.coverage.engine.current_prepare_image_ids",
                return_value={"multilang-given_fuzzer-crs:latest": "sha256:new"},
            ),
            patch(
                "crsbench.evaluation.coverage.engine.build_atlantis_coverage_artifacts",
                return_value=MagicMock(
                    build_id="build-123",
                    compose_file=Path("/tmp/compose.yaml"),
                    control_root=Path("/tmp/control"),
                    atlantis_build_output_dir=Path("/tmp/out"),
                ),
            ) as mock_build,
            patch("crsbench.evaluation.coverage.engine.fix_docker_ownership"),
            patch.object(engine.infra, "write_build_metadata"),
        ):
            result = engine._build_coverage_variant(adapter)

        assert result == variant_name
        mock_build.assert_called_once()

    def test_build_variant_does_not_reuse_partial_jvm_build_without_sentinel(
        self, mock_benchmark: Path, engine: CoverageEngine
    ):
        adapter = engine._load_adapter(mock_benchmark)
        adapter.lang = "jvm"
        variant_name = "test-benchmark-cov-delta-coverage"
        build_output_dir = engine.infra.get_build_output_path(variant_name)
        build_output_dir.mkdir(parents=True, exist_ok=True)
        (build_output_dir / ".crsbench-repo").mkdir()
        (build_output_dir / "fuzz_target").write_text("wrapper")

        with (
            patch(
                "crsbench.evaluation.coverage.engine.build_atlantis_coverage_artifacts",
                return_value=MagicMock(
                    build_id="build-123",
                    compose_file=Path("/tmp/compose.yaml"),
                    control_root=Path("/tmp/control"),
                    atlantis_build_output_dir=Path("/tmp/out"),
                ),
            ) as mock_build,
            patch(
                "crsbench.evaluation.coverage.engine.current_prepare_image_ids",
                return_value={"multilang-given_fuzzer-crs:latest": "sha256:test"},
            ),
            patch("crsbench.evaluation.coverage.engine.fix_docker_ownership"),
            patch.object(engine.infra, "write_build_metadata"),
        ):
            result = engine._build_coverage_variant(adapter)

        assert result == variant_name
        mock_build.assert_called_once()
        assert (build_output_dir / UNIAFL_BUILD_SENTINEL).exists()

    def test_timed_totals_reuse_existing_session(
        self, mock_oss_fuzz: Path, mock_benchmark: Path, mock_corpus: Path
    ):
        eng = CoverageEngine(mock_oss_fuzz)
        adapter = eng._load_adapter(mock_benchmark)
        strategy = MagicMock()
        session = MagicMock()
        session.collect_many.return_value = {
            path: CoverageRunResult(
                coverage_data={"f": {"src": "a.c", "lines": [1]}},
                raw_cov_path=Path("/tmp/f.cov"),
            )
            for path in sorted(mock_corpus.iterdir())
        }
        session.collect_batch_totals.return_value = {
            "lines_covered": 1,
            "lines_total": 10,
            "lines_percent": 10.0,
            "functions_covered": 1,
            "functions_total": 1,
        }

        class SessionContext:
            def __enter__(self):
                return session

            def __exit__(self, exc_type, exc, tb):
                return False

        timed_inputs = [
            TimedCoverageInput(
                path=path,
                relative_time=float(i),
                original_name=path.name,
                input_hash=f"h{i}",
                content_hash=f"c{i}",
                size=path.stat().st_size,
            )
            for i, path in enumerate(sorted(mock_corpus.iterdir()))
        ]

        with (
            patch.object(
                eng,
                "_build_coverage_variant",
                return_value="test-benchmark-cov-delta-coverage",
            ),
            patch.object(eng.infra, "has_harness", return_value=True),
            patch.object(eng, "_get_or_create_strategy", return_value=strategy),
            patch.object(eng, "_maybe_open_session", return_value=SessionContext()),
        ):
            _, summary = eng.collect_timed_line_coverage(mock_benchmark, timed_inputs)

        assert summary.lines_total == 10
        session.collect_batch_totals.assert_called_once()

    def test_build_variant_uses_atlantis_build_pipeline(
        self,
        mock_benchmark: Path,
        engine: CoverageEngine,
    ):
        """Coverage variant build should use the Atlantis oss-crs pipeline."""
        adapter = engine._load_adapter(mock_benchmark)
        assert adapter is not None
        build_out = engine.infra.get_build_output_path(
            "test-benchmark-cov-delta-coverage"
        )
        control_root = build_out.parent / ".test-benchmark-cov-delta-coverage-oss-crs"
        build = MagicMock(
            build_id="build-123",
            compose_file=control_root / "crs-compose.yaml",
            control_root=control_root,
            atlantis_build_output_dir=control_root / "out",
        )

        with (
            patch(
                "crsbench.evaluation.coverage.engine.build_atlantis_coverage_artifacts",
                return_value=build,
            ) as mock_build,
            patch(
                "crsbench.evaluation.coverage.engine.current_uniafl_checkout_fingerprint",
                return_value="fingerprint-1",
            ),
            patch(
                "crsbench.evaluation.coverage.engine.current_prepare_image_ids",
                return_value={"multilang-given_fuzzer-crs:latest": "sha256:test"},
            ),
            patch(
                "crsbench.evaluation.coverage.engine.fix_docker_ownership"
            ) as mock_fix_ownership,
            patch.object(engine.infra, "write_build_metadata") as mock_write_meta,
        ):
            result = engine._build_coverage_variant(adapter)

        assert result == "test-benchmark-cov-delta-coverage"
        mock_build.assert_called_once_with(
            benchmark_path=mock_benchmark,
            normalized_build_output_dir=build_out,
            control_root=control_root,
        )
        mock_fix_ownership.assert_called_once_with(build_out)
        mock_write_meta.assert_called_once()

    def test_collect_timed_line_coverage_fails_if_all_inputs_fail(
        self, mock_benchmark: Path, engine: CoverageEngine
    ):
        """Timed coverage should fail instead of reporting an empty success."""
        timed_inputs = [
            TimedCoverageInput(
                content_hash="abc",
                original_name="a.bin",
                path=Path("/tmp/a.bin"),
                relative_time=1.0,
                size=1,
            )
        ]

        adapter = MagicMock()
        adapter.get_harness_names.return_value = ["fuzz_target"]
        session = MagicMock()
        session.__enter__.return_value = session
        session.__exit__.return_value = False
        session.collect_many.return_value = {}
        session.collect_single.return_value = CoverageRunResult(coverage_data={})
        session.collect_batch_totals.return_value = {}
        strategy = MagicMock()
        strategy.open_session.return_value = session

        with (
            patch.object(engine, "_load_adapter", return_value=adapter),
            patch.object(
                engine,
                "_build_coverage_variant",
                return_value="test-benchmark-delta-coverage",
            ),
            patch.object(engine.infra, "has_harness", return_value=True),
            patch.object(engine, "_get_or_create_strategy", return_value=strategy),
            patch.object(engine, "_maybe_open_session", return_value=session),
        ):
            with pytest.raises(CoverageStrategyError, match="failed for all inputs"):
                engine.collect_timed_line_coverage(
                    mock_benchmark,
                    timed_inputs,
                    harness_filter="fuzz_target",
                )

    def test_collect_timed_line_coverage_records_raw_artifact_metadata(
        self, mock_benchmark: Path, engine: CoverageEngine, tmp_path: Path
    ):
        seed_path = tmp_path / "a.bin"
        seed_path.write_bytes(b"a")
        timed_input = TimedCoverageInput(
            content_hash="abc",
            original_name="a.bin",
            path=seed_path,
            relative_time=1.0,
            size=1,
        )

        adapter = MagicMock()
        adapter.get_harness_names.return_value = ["fuzz_target"]

        class _Session:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return None

            def collect_single(self, corpus_file: Path):
                return CoverageRunResult(
                    coverage_data={"main": {"src": "/src/a.c", "lines": [1, 2]}},
                    raw_cov_path=tmp_path / "raw" / "abc.cov",
                    crashed=True,
                    crash_log_path=tmp_path / "raw" / "abc.crash.log",
                )

            def collect_batch_totals(self, corpus_dir: Path):
                return {
                    "lines_covered": 2,
                    "lines_total": 10,
                    "lines_percent": 20.0,
                    "functions_covered": 1,
                    "functions_total": 5,
                }

        class _Strategy:
            def open_session(self, harness_name: str, *, output_dir=None):
                return _Session()

        with (
            patch.object(engine, "_load_adapter", return_value=adapter),
            patch.object(
                engine,
                "_build_coverage_variant",
                return_value="test-benchmark-delta-coverage",
            ),
            patch.object(engine.infra, "has_harness", return_value=True),
            patch.object(engine, "_get_or_create_strategy", return_value=_Strategy()),
        ):
            seeds, summary = engine.collect_timed_line_coverage(
                mock_benchmark,
                [timed_input],
                harness_filter="fuzz_target",
                output_dir=tmp_path / "coverage",
            )

        assert summary.lines_covered == 2
        assert len(seeds) == 1
        assert seeds[0].lines_covered == 2
        assert seeds[0].crashed is True
        assert seeds[0].raw_cov_path == tmp_path / "raw" / "abc.cov"
        assert seeds[0].crash_log_path == tmp_path / "raw" / "abc.crash.log"

    def test_collect_timed_line_coverage_retains_crash_only_inputs(
        self, mock_benchmark: Path, engine: CoverageEngine, tmp_path: Path
    ):
        seed_path = tmp_path / "crash.bin"
        seed_path.write_bytes(b"boom")
        timed_input = TimedCoverageInput(
            content_hash="crash",
            original_name="crash.bin",
            path=seed_path,
            relative_time=2.0,
            size=4,
        )

        adapter = MagicMock()
        adapter.get_harness_names.return_value = ["fuzz_target"]

        class _Session:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return None

            def collect_single(self, corpus_file: Path):
                del corpus_file
                return CoverageRunResult(
                    coverage_data={},
                    raw_cov_path=tmp_path / "raw" / "crash.cov",
                    crashed=True,
                    crash_log_path=tmp_path / "raw" / "crash.crash.log",
                )

            def collect_batch_totals(self, corpus_dir: Path):
                del corpus_dir
                return {
                    "lines_covered": 0,
                    "lines_total": 10,
                    "lines_percent": 0.0,
                    "functions_covered": 0,
                    "functions_total": 5,
                }

        class _Strategy:
            def open_session(self, harness_name: str, *, output_dir=None):
                del harness_name, output_dir
                return _Session()

        with (
            patch.object(engine, "_load_adapter", return_value=adapter),
            patch.object(
                engine,
                "_build_coverage_variant",
                return_value="test-benchmark-delta-coverage",
            ),
            patch.object(engine.infra, "has_harness", return_value=True),
            patch.object(engine, "_get_or_create_strategy", return_value=_Strategy()),
        ):
            seeds, summary = engine.collect_timed_line_coverage(
                mock_benchmark,
                [timed_input],
                harness_filter="fuzz_target",
                output_dir=tmp_path / "coverage",
            )

        assert summary.lines_covered == 0
        assert len(seeds) == 1
        assert seeds[0].lines_covered == 0
        assert seeds[0].crashed is True
        assert seeds[0].raw_cov_path == tmp_path / "raw" / "crash.cov"
        assert seeds[0].crash_log_path == tmp_path / "raw" / "crash.crash.log"

    def test_jacoco_strategy_uses_uniafl_session(
        self, mock_oss_fuzz: Path, tmp_path: Path
    ):
        project_name = "proj-cov-delta-coverage"
        build_output_dir = mock_oss_fuzz / "build" / "out" / project_name
        build_output_dir.mkdir(parents=True)
        (build_output_dir / "ExpanderFuzzer").write_text("#!/bin/sh\n")
        strategy = JaCoCoLineStrategy(mock_oss_fuzz, project_name, work_dir=tmp_path)

        with patch(
            "crsbench.evaluation.coverage.strategy.UniAFLCoverageSession"
        ) as mock_session_cls:
            session = strategy.open_session(
                "ExpanderFuzzer", output_dir=tmp_path / "out"
            )

        assert session is mock_session_cls.return_value
