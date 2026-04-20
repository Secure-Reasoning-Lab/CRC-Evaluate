"""CLI entry point for dataset download."""

import argparse
from pathlib import Path

from crsbench.dataset.registry import get_dataset_names
from crsbench.utils.logger import configure_logger, get_logger

logger = get_logger(__name__)


def add_dataset_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Add the dataset subcommand to argparse.

    Args:
        subparsers: Subparsers action from parent ArgumentParser
    """
    dataset_names = ", ".join(get_dataset_names())

    parser = subparsers.add_parser(
        "download",
        help="Download benchmark datasets from HuggingFace",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
Available datasets: {dataset_names}

Examples:
  crsbench download --all
  crsbench download --dataset crsbench
  crsbench download --dataset crsbench --benchmarks afc-curl-delta-01
  crsbench download --benchmark-suite sanity
  crsbench download --all --no-ground-truth  # skip .aixcc/ ground truth
  crsbench download --all --no-corpus        # skip .aixcc/{{harness}}/corpus/
  crsbench download --all --output-dir /tmp/benchmarks
        """,
    )

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--dataset",
        type=str,
        choices=get_dataset_names(),
        help="Dataset to download",
    )
    group.add_argument(
        "--benchmark-suite",
        type=str,
        dest="benchmark_suite",
        help="Download benchmarks from a suite file (e.g., 'sanity', 'afc-all')",
    )
    group.add_argument(
        "--all",
        action="store_true",
        dest="download_all",
        help="Download all available datasets",
    )

    parser.add_argument(
        "--benchmarks",
        type=str,
        nargs="+",
        help="Specific benchmark names to download (use with --dataset)",
    )

    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("benchmarks"),
        help="Output directory (default: benchmarks/)",
    )

    parser.add_argument(
        "--no-ground-truth",
        action="store_true",
        dest="no_ground_truth",
        help="Skip downloading ground-truth.tar.gz (.aixcc/ directory)",
    )

    parser.add_argument(
        "--no-corpus",
        action="store_true",
        dest="no_corpus",
        help="Skip downloading corpus.tar.gz (.aixcc/{harness}/corpus/ directories)",
    )

    parser.add_argument(
        "--benchmark-suites-root",
        type=Path,
        default=Path("benchmark-suites"),
        help="Root directory for suite YAML files (default: benchmark-suites/)",
    )

    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable verbose/debug logging",
    )

    parser.set_defaults(command="download")


def run_download(args: argparse.Namespace) -> int:
    """Execute the download command.

    Args:
        args: Parsed command line arguments

    Returns:
        Exit code (0 for success)
    """
    if hasattr(args, "verbose") and args.verbose:
        configure_logger(level="DEBUG")

    from crsbench.dataset.backends import check_hf_token

    is_valid, message = check_hf_token()
    if not is_valid:
        logger.error(message)
        return 1
    logger.info(message)

    if args.benchmarks and not args.dataset:
        logger.error("--benchmarks can only be used with --dataset")
        return 1

    if args.download_all:
        from crsbench.dataset.download import download_all

        download_all(
            args.output_dir,
            no_ground_truth=args.no_ground_truth,
            no_corpus=args.no_corpus,
        )
    elif args.benchmark_suite:
        from crsbench.dataset.download import download_suite

        download_suite(
            args.benchmark_suite,
            args.output_dir,
            args.benchmark_suites_root,
            no_ground_truth=args.no_ground_truth,
            no_corpus=args.no_corpus,
        )
    else:
        from crsbench.dataset.download import download_dataset

        download_dataset(
            args.dataset,
            args.output_dir,
            benchmarks=args.benchmarks,
            no_ground_truth=args.no_ground_truth,
            no_corpus=args.no_corpus,
        )

    return 0
