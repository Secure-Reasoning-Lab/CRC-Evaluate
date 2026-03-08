"""Storage metrics subcommand.

Shows storage usage breakdown per benchmark:
- Build: Artifacts in oss-fuzz/build/{out,work}/
- Docker: Unique Docker images (by image ID)
- Git/.aixcc: Git repository or .aixcc directory size
"""

import argparse
from pathlib import Path

from rich.console import Console
from rich.table import Table

from crsbench.benchmark_ci.cli.common_args import create_benchmark_selection_parent
from crsbench.benchmark_ci.cli.discovery import resolve_benchmark_paths
from crsbench.benchmark_ci.storage import (
    StorageMetrics,
    collect_benchmark_storage,
    format_storage_size,
)
from crsbench.utils.logger import get_logger
from crsbench.utils.run_helper import ensure_oss_fuzz_root

logger = get_logger(__name__)


def register(subparsers: argparse._SubParsersAction) -> None:
    """Register the storage subcommand."""
    parser = subparsers.add_parser(
        "storage",
        parents=[create_benchmark_selection_parent()],
        help="Show storage usage per benchmark (no Docker builds)",
    )
    parser.add_argument(
        "--project-image-prefix",
        type=str,
        default="crsbench",
        help="Docker image prefix (default: crsbench)",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable colored output",
    )
    parser.set_defaults(ci_func=run_storage)


def run_storage(args: argparse.Namespace) -> int:
    """Show storage metrics for benchmarks."""
    paths = resolve_benchmark_paths(
        benchmark_arg=getattr(args, "benchmark", None),
        benchmarks_list=getattr(args, "benchmarks", None),
        benchmark_suite=getattr(args, "benchmark_suite", None),
        all_benchmarks=getattr(args, "all", False),
        filter_pattern=getattr(args, "filter", None),
    )

    if not paths:
        logger.error("No benchmarks found")
        return 1

    oss_fuzz_path = Path(ensure_oss_fuzz_root())
    prefix = getattr(args, "project_image_prefix", "crsbench")
    no_color = getattr(args, "no_color", False)

    # Collect metrics for each benchmark
    results: list[tuple[str, StorageMetrics]] = []
    for path in paths:
        metrics = collect_benchmark_storage(
            benchmark_name=path.name,
            benchmark_path=path,
            oss_fuzz_path=oss_fuzz_path,
            project_image_prefix=prefix,
        )
        results.append((path.name, metrics))

    # Calculate totals
    total = StorageMetrics(
        build_artifacts_bytes=sum(m.build_artifacts_bytes for _, m in results),
        docker_image_bytes=sum(m.docker_image_bytes for _, m in results),
        git_bytes=sum(m.git_bytes for _, m in results),
    )

    # Print table
    console = Console(force_terminal=not no_color)
    table = Table(title="Storage Usage by Benchmark", show_footer=True)

    table.add_column("Benchmark", style="cyan", footer="Total")
    table.add_column(
        "Build",
        justify="right",
        footer=format_storage_size(total.build_artifacts_bytes),
    )
    table.add_column(
        "Docker", justify="right", footer=format_storage_size(total.docker_image_bytes)
    )
    table.add_column(
        "Git/.aixcc", justify="right", footer=format_storage_size(total.git_bytes)
    )
    table.add_column(
        "Total",
        justify="right",
        style="bold",
        footer=format_storage_size(total.total_bytes),
    )

    for name, m in results:
        table.add_row(
            name,
            format_storage_size(m.build_artifacts_bytes),
            format_storage_size(m.docker_image_bytes),
            format_storage_size(m.git_bytes),
            format_storage_size(m.total_bytes),
        )

    console.print(table)

    # Summary
    console.print(
        f"\n[bold]Total storage:[/bold] {format_storage_size(total.total_bytes)}"
    )
    console.print(
        f"  Build artifacts: {format_storage_size(total.build_artifacts_bytes)}"
    )
    console.print(f"  Docker images:   {format_storage_size(total.docker_image_bytes)}")
    console.print(f"  Git/.aixcc:      {format_storage_size(total.git_bytes)}")

    return 0
