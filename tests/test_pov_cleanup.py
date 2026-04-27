"""Unit tests for POV cleanup helper used by `benchmark ci pov --delete-failed-povs`."""

from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from crsbench.benchmark_ci.cli.pov_cleanup import (
    FailedPov0,
    collect_failed_povs,
    prompt_and_delete_failed_povs,
)
from crsbench.benchmark_ci.jobs.base import JobResult
from crsbench.benchmark_ci.jobs.flat import VerifyCpvPovJob, VerifyCpvVarJob
from crsbench.executor.types import ExecutorResult, JobStatus


def _job_result(job_id: str, *, success: bool, verdicts: list[dict]) -> JobResult:
    now = datetime.now()
    return JobResult(
        job_id=job_id,
        job_type="verify",
        success=success,
        started_at=now,
        finished_at=now,
        elapsed_seconds=0.1,
        details={"verdicts": verdicts},
    )


def _make_benchmark(
    tmp_path: Path, harness: str, cpv_id: str, pov_ids: list[str]
) -> Path:
    bench = tmp_path / "afc-fake-delta-01"
    blobs = bench / ".aixcc" / harness / cpv_id / "blobs"
    logs = bench / ".aixcc" / harness / cpv_id / "logs"
    blobs.mkdir(parents=True)
    logs.mkdir(parents=True)
    for pov_id in pov_ids:
        (blobs / f"{pov_id}.blob").write_bytes(b"x")
        (logs / f"{pov_id}.log").write_text("log")
    return bench


def test_collect_failed_povs_skips_passing_variants(tmp_path):
    bench = _make_benchmark(tmp_path, "harness1", "cpv_0", ["pov_0", "pov_1", "pov_2"])
    var_job = VerifyCpvVarJob(
        benchmark_name=bench.name,
        cpv_id="cpv_0",
        harness="harness1",
        benchmark_path=bench,
        pov_paths=[
            bench / ".aixcc" / "harness1" / "cpv_0" / "blobs" / "pov_1.blob",
            bench / ".aixcc" / "harness1" / "cpv_0" / "blobs" / "pov_2.blob",
        ],
    )
    dag_results = {
        var_job.job_id: ExecutorResult(
            job_id=var_job.job_id,
            status=JobStatus.SUCCESS,
            elapsed_seconds=1.0,
            job_result=_job_result(
                var_job.job_id,
                success=False,
                verdicts=[
                    {"pov_id": "pov_1", "status": "cpv"},
                    {"pov_id": "pov_2", "status": "not_vulnerable"},
                ],
            ),
        ),
    }

    pov0_warnings, to_delete = collect_failed_povs([var_job], dag_results)

    assert pov0_warnings == []
    assert len(to_delete) == 1
    assert to_delete[0].pov_id == "pov_2"
    assert to_delete[0].status == "not_vulnerable"
    assert to_delete[0].blob_path.name == "pov_2.blob"
    assert to_delete[0].log_path.name == "pov_2.log"


def test_collect_failed_povs_warns_on_pov0_failure_without_deleting(tmp_path):
    bench = _make_benchmark(tmp_path, "h1", "cpv_0", ["pov_0"])
    pov_job = VerifyCpvPovJob(
        benchmark_name=bench.name,
        cpv_id="cpv_0",
        harness="h1",
        benchmark_path=bench,
        pov_path=bench / ".aixcc" / "h1" / "cpv_0" / "blobs" / "pov_0.blob",
    )
    dag_results = {
        pov_job.job_id: ExecutorResult(
            job_id=pov_job.job_id,
            status=JobStatus.SUCCESS,
            elapsed_seconds=1.0,
            job_result=_job_result(
                pov_job.job_id,
                success=False,
                verdicts=[{"pov_id": "pov_0", "status": "unintended_crash"}],
            ),
        ),
    }

    pov0_warnings, to_delete = collect_failed_povs([pov_job], dag_results)

    assert to_delete == []
    assert len(pov0_warnings) == 1
    assert pov0_warnings[0] == FailedPov0(
        benchmark_path=bench,
        harness="h1",
        cpv_id="cpv_0",
        pov_id="pov_0",
        status="unintended_crash",
    )


def test_collect_failed_povs_skips_jobs_without_results(tmp_path):
    bench = _make_benchmark(tmp_path, "h", "cpv_0", ["pov_0", "pov_1"])
    var_job = VerifyCpvVarJob(
        benchmark_name=bench.name,
        cpv_id="cpv_0",
        harness="h",
        benchmark_path=bench,
        pov_paths=[bench / ".aixcc" / "h" / "cpv_0" / "blobs" / "pov_1.blob"],
    )
    pov0_warnings, to_delete = collect_failed_povs([var_job], dag_results={})
    assert pov0_warnings == []
    assert to_delete == []


def test_prompt_and_delete_yes_removes_files_and_writes_changelog(tmp_path):
    bench = _make_benchmark(tmp_path, "h", "cpv_0", ["pov_0", "pov_1", "pov_2"])
    var_job = VerifyCpvVarJob(
        benchmark_name=bench.name,
        cpv_id="cpv_0",
        harness="h",
        benchmark_path=bench,
        pov_paths=[
            bench / ".aixcc" / "h" / "cpv_0" / "blobs" / "pov_1.blob",
            bench / ".aixcc" / "h" / "cpv_0" / "blobs" / "pov_2.blob",
        ],
    )
    dag_results = {
        var_job.job_id: ExecutorResult(
            job_id=var_job.job_id,
            status=JobStatus.SUCCESS,
            elapsed_seconds=1.0,
            job_result=_job_result(
                var_job.job_id,
                success=False,
                verdicts=[
                    {"pov_id": "pov_1", "status": "not_vulnerable"},
                    {"pov_id": "pov_2", "status": "unintended_crash"},
                ],
            ),
        ),
    }

    blobs = bench / ".aixcc" / "h" / "cpv_0" / "blobs"
    logs = bench / ".aixcc" / "h" / "cpv_0" / "logs"

    with (
        patch("crsbench.benchmark_ci.cli.pov_cleanup.sys.stdin") as mock_stdin,
        patch("builtins.input", return_value="y"),
    ):
        mock_stdin.isatty.return_value = True
        prompt_and_delete_failed_povs([var_job], dag_results)

    # pov_0 is preserved
    assert (blobs / "pov_0.blob").exists()
    assert (logs / "pov_0.log").exists()

    # failed variants removed (blob + log)
    for pov_id in ("pov_1", "pov_2"):
        assert not (blobs / f"{pov_id}.blob").exists()
        assert not (logs / f"{pov_id}.log").exists()

    changelog = bench / ".aixcc" / "CHANGELOG.md"
    assert changelog.exists()
    text = changelog.read_text()
    assert text.startswith("# CHANGELOG")
    assert "Removed failed variant POVs" in text
    assert "pov_1.blob" in text
    assert "pov_1.log" in text
    assert "pov_2.blob" in text
    assert "verdict=not_vulnerable" in text
    assert "verdict=unintended_crash" in text


def test_prompt_and_delete_no_keeps_files(tmp_path):
    bench = _make_benchmark(tmp_path, "h", "cpv_0", ["pov_0", "pov_1"])
    var_job = VerifyCpvVarJob(
        benchmark_name=bench.name,
        cpv_id="cpv_0",
        harness="h",
        benchmark_path=bench,
        pov_paths=[bench / ".aixcc" / "h" / "cpv_0" / "blobs" / "pov_1.blob"],
    )
    dag_results = {
        var_job.job_id: ExecutorResult(
            job_id=var_job.job_id,
            status=JobStatus.SUCCESS,
            elapsed_seconds=1.0,
            job_result=_job_result(
                var_job.job_id,
                success=False,
                verdicts=[{"pov_id": "pov_1", "status": "not_vulnerable"}],
            ),
        ),
    }

    with (
        patch("crsbench.benchmark_ci.cli.pov_cleanup.sys.stdin") as mock_stdin,
        patch("builtins.input", return_value="n"),
    ):
        mock_stdin.isatty.return_value = True
        prompt_and_delete_failed_povs([var_job], dag_results)

    assert (bench / ".aixcc" / "h" / "cpv_0" / "blobs" / "pov_1.blob").exists()
    assert (bench / ".aixcc" / "h" / "cpv_0" / "logs" / "pov_1.log").exists()
    assert not (bench / ".aixcc" / "CHANGELOG.md").exists()


def test_prompt_and_delete_aborts_when_not_a_tty(tmp_path):
    bench = _make_benchmark(tmp_path, "h", "cpv_0", ["pov_0", "pov_1"])
    var_job = VerifyCpvVarJob(
        benchmark_name=bench.name,
        cpv_id="cpv_0",
        harness="h",
        benchmark_path=bench,
        pov_paths=[bench / ".aixcc" / "h" / "cpv_0" / "blobs" / "pov_1.blob"],
    )
    dag_results = {
        var_job.job_id: ExecutorResult(
            job_id=var_job.job_id,
            status=JobStatus.SUCCESS,
            elapsed_seconds=1.0,
            job_result=_job_result(
                var_job.job_id,
                success=False,
                verdicts=[{"pov_id": "pov_1", "status": "not_vulnerable"}],
            ),
        ),
    }

    with patch("crsbench.benchmark_ci.cli.pov_cleanup.sys.stdin") as mock_stdin:
        mock_stdin.isatty.return_value = False
        prompt_and_delete_failed_povs([var_job], dag_results)

    # Files preserved when no TTY available.
    assert (bench / ".aixcc" / "h" / "cpv_0" / "blobs" / "pov_1.blob").exists()


def test_changelog_appends_to_existing(tmp_path):
    bench = _make_benchmark(tmp_path, "h", "cpv_0", ["pov_0", "pov_1"])
    changelog = bench / ".aixcc" / "CHANGELOG.md"
    changelog.write_text("# CHANGELOG\n\n## 2026-01-01\n- Earlier change\n")

    var_job = VerifyCpvVarJob(
        benchmark_name=bench.name,
        cpv_id="cpv_0",
        harness="h",
        benchmark_path=bench,
        pov_paths=[bench / ".aixcc" / "h" / "cpv_0" / "blobs" / "pov_1.blob"],
    )
    dag_results = {
        var_job.job_id: ExecutorResult(
            job_id=var_job.job_id,
            status=JobStatus.SUCCESS,
            elapsed_seconds=1.0,
            job_result=_job_result(
                var_job.job_id,
                success=False,
                verdicts=[{"pov_id": "pov_1", "status": "not_vulnerable"}],
            ),
        ),
    }

    with (
        patch("crsbench.benchmark_ci.cli.pov_cleanup.sys.stdin") as mock_stdin,
        patch("builtins.input", return_value="yes"),
    ):
        mock_stdin.isatty.return_value = True
        prompt_and_delete_failed_povs([var_job], dag_results)

    text = changelog.read_text()
    assert text.startswith("# CHANGELOG")
    assert "Earlier change" in text
    assert "Removed failed variant POVs" in text
    # New entry inserted above prior entries.
    assert text.index("Removed failed variant POVs") < text.index("Earlier change")
