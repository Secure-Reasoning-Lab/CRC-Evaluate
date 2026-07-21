"""Tests for OSS-CRS internal LiteLLM accounting."""

import json
from pathlib import Path

from crsbench.evaluation.oss_crs_spend import OssCrsSpendReport


def _write_report(path: Path, total: float, *, updated_at: int = 1780000000) -> None:
    path.write_text(
        json.dumps(
            {
                "totals": {"credits_used": total},
                "crs": {"test-crs": {"credits_used": total}},
                "updated_at": updated_at,
            }
        )
    )


def test_reads_spend_and_writes_compatible_usage_file(tmp_path: Path) -> None:
    report_path = tmp_path / "litellm-spend-report.json"
    _write_report(report_path, 1.25)
    report = OssCrsSpendReport(
        report_path,
        trial_id="trial-1",
        max_budget_usd=30,
    )

    output_path = tmp_path / "llm-usage.json"
    assert report.write_usage_file(output_path) == output_path

    output = json.loads(output_path.read_text())
    assert output["trial_id"] == "trial-1"
    assert output["total_cost_usd"] == 1.25
    assert output["key_info"]["max_budget"] == 30
    assert output["key_info"]["by_crs"] == {"test-crs": 1.25}
    assert output["total_input_tokens"] == 0


def test_retains_last_valid_non_decreasing_snapshot(tmp_path: Path) -> None:
    report_path = tmp_path / "litellm-spend-report.json"
    _write_report(report_path, 2.0)
    report = OssCrsSpendReport(report_path, trial_id="trial-1")
    assert report.read().total_cost_usd == 2.0

    _write_report(report_path, 0.0)
    assert report.read().total_cost_usd == 2.0

    report_path.write_text('{"totals":')
    assert report.read().total_cost_usd == 2.0


def test_empty_report_does_not_create_usage_file(tmp_path: Path) -> None:
    report_path = tmp_path / "litellm-spend-report.json"
    report_path.write_text("")
    report = OssCrsSpendReport(report_path, trial_id="trial-1")

    output_path = tmp_path / "llm-usage.json"
    assert report.write_usage_file(output_path) is None
    assert not output_path.exists()


def test_budget_state_uses_configured_budget(tmp_path: Path) -> None:
    report_path = tmp_path / "litellm-spend-report.json"
    _write_report(report_path, 10.5)
    report = OssCrsSpendReport(
        report_path,
        trial_id="trial-1",
        max_budget_usd=10,
    )

    assert report.budget_state() == (10.5, 10)
