"""Format validation subcommand."""

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from crsbench.benchmark_ci.cli.common_args import (
    create_benchmark_selection_parent,
    create_output_options_parent,
)
from crsbench.benchmark_ci.cli.discovery import resolve_benchmark_paths
from crsbench.benchmark_ci.cli.output import print_results_table, save_output_dir
from crsbench.benchmark_ci.models import (
    BenchmarkValidationResult,
    CheckMode,
    CheckResult,
    CheckStatus,
    ValidationSummary,
)
from crsbench.utils.logger import get_logger
from crsbench.validation import validate_benchmark as format_validate

logger = get_logger(__name__)


def register(subparsers: argparse._SubParsersAction) -> None:
    """Register the format subcommand."""
    parser = subparsers.add_parser(
        "format",
        parents=[
            create_benchmark_selection_parent(),
            create_output_options_parent(),
        ],
        help="Run format validation only (fast, no Docker)",
    )
    parser.add_argument(
        "--parallel",
        "-p",
        type=int,
        default=1,
        help="Number of benchmarks to validate in parallel (default: 1, sequential)",
    )
    parser.set_defaults(ci_func=run_format)


def _validate_benchmark(path: Path) -> BenchmarkValidationResult:
    """Run format check for a single benchmark."""
    start_time = time.time()
    start_dt = datetime.now()

    try:
        result = format_validate(path)
        elapsed = time.time() - start_time

        if result.is_valid:
            format_check = CheckResult(
                status=CheckStatus.PASS,
                time_seconds=elapsed,
                details={"warnings": result.warning_count},
            )
        else:
            error_msgs = "; ".join(e.message for e in result.errors[:3])
            format_check = CheckResult(
                status=CheckStatus.FAIL,
                time_seconds=elapsed,
                error=error_msgs,
                details={
                    "error_count": result.error_count,
                    "warning_count": result.warning_count,
                },
            )
    except Exception as exc:
        elapsed = time.time() - start_time
        format_check = CheckResult.make_error(str(exc), time_seconds=elapsed)

    return BenchmarkValidationResult(
        benchmark=path.name,
        benchmark_path=path,
        format_check=format_check,
        started_at=start_dt,
        finished_at=datetime.now(),
    )


def run_format(args: argparse.Namespace) -> int:
    """Run format validation on resolved benchmarks."""
    paths = resolve_benchmark_paths(
        benchmark_arg=getattr(args, "benchmark", None),
        all_benchmarks=getattr(args, "all", False),
        filter_pattern=getattr(args, "filter", None),
    )

    parallel = getattr(args, "parallel", 1)
    summary = ValidationSummary(started_at=datetime.now())

    if parallel <= 1:
        for path in paths:
            result = _validate_benchmark(path)
            summary.add_result(result)
    else:
        with ThreadPoolExecutor(max_workers=parallel) as executor:
            futures = {
                executor.submit(_validate_benchmark, path): path for path in paths
            }
            for future in as_completed(futures):
                path = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    logger.error(f"Unexpected error validating {path.name}: {exc}")
                    result = BenchmarkValidationResult(
                        benchmark=path.name,
                        benchmark_path=path,
                        started_at=datetime.now(),
                        finished_at=datetime.now(),
                    )
                summary.add_result(result)

    summary.finished_at = datetime.now()

    print_results_table(
        summary,
        check_mode=CheckMode.ALL,
        no_color=getattr(args, "no_color", False),
    )

    output_dir = getattr(args, "output_dir", None)
    if output_dir:
        save_output_dir(summary, Path(output_dir), check_mode=CheckMode.ALL)

    output_path = getattr(args, "output", None)
    if output_path:
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(summary.to_dict(), indent=2))

    if summary.failed > 0 or summary.errors > 0:
        return 1
    return 0
