"""CLI command for report generation."""

import argparse
import traceback
from pathlib import Path

from crsbench.evaluation.trial_paths import experiment_dir as resolve_experiment_dir
from crsbench.reporting.errors import ReportError
from crsbench.reporting.orchestrator import ReportGenerator
from crsbench.utils.logger import configure_logger, get_logger

logger = get_logger(__name__)


def add_report_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Add the 'report' subcommand to the CLI.

    Args:
        subparsers: Subparser action from main argument parser
    """
    report_parser = subparsers.add_parser(
        "report",
        help="Generate reports from experiment results",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="""
Generate comprehensive reports from CRS experiment results.

This command processes trial snapshots and generates human-readable
reports with metrics, visualizations, and analysis.
        """,
        epilog="""
Examples:
  # Generate report for an experiment (by name)
  %(prog)s --experiment test-experiment --output ./reports

  # Generate report for an experiment (by directory path)
  %(prog)s --experiment-dir ./experiment_filestore/test-experiment

  # Generate JSON reports only
  %(prog)s --experiment test-experiment --format json

  # Generate HTML reports only
  %(prog)s --experiment test-experiment --format html

  # Generate CSV reports only
  %(prog)s --experiment test-experiment --format csv

  # Generate all formats (JSON + HTML + CSV)
  %(prog)s --experiment test-experiment --format all

  # Preview what would be generated (dry-run)
  %(prog)s --experiment test-experiment --dry-run

  # Validate experiment completeness only
  %(prog)s --experiment test-experiment --validate-only

  # Generate report for a single trial
  %(prog)s --trial ./experiment_filestore/test-exp/json-c__ensemble-c/trial-1
        """,
    )

    report_parser.add_argument(
        "--experiment",
        type=str,
        metavar="EXPERIMENT_NAME",
        help="Experiment name to generate report for (used with --experiment-filestore)",
    )

    report_parser.add_argument(
        "--experiment-dir",
        type=str,
        metavar="EXPERIMENT_DIR",
        help="Direct path to experiment directory (alternative to --experiment)",
    )

    report_parser.add_argument(
        "--trial",
        type=str,
        metavar="TRIAL_DIR",
        help="Path to a single trial directory to generate report for",
    )

    report_parser.add_argument(
        "--experiment-filestore",
        type=str,
        default="experiment_filestore",
        metavar="PATH",
        help="Path to experiment filestore directory (default: experiment_filestore)",
    )

    report_parser.add_argument(
        "--output",
        type=Path,
        default=Path("report_filestore"),
        metavar="OUTPUT_DIR",
        help="Output directory for reports (default: report_filestore)",
    )

    report_parser.add_argument(
        "--benchmarks-root",
        type=str,
        default="benchmarks",
        metavar="BENCHMARKS_ROOT",
        help="Path to benchmarks root directory for ground truth comparison (default: benchmarks)",
    )

    report_parser.add_argument(
        "--format",
        type=str,
        choices=["json", "html", "csv", "both", "all"],
        default="both",
        metavar="FORMAT",
        help="Report format: json, html, csv, both (json+html), or all (json+html+csv) (default: both)",
    )

    report_parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Only validate experiment completeness, don't generate reports",
    )

    report_parser.add_argument(
        "--include-incomplete",
        action="store_true",
        help="Include incomplete trials in report (default: skip incomplete)",
    )

    report_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview trials and CSV columns that would be generated without creating reports",
    )

    report_parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging (logs all commands executed with their working directories)",
    )

    report_parser.set_defaults(command="report")


def run_report(args: argparse.Namespace) -> int:
    """Execute the report command.

    Args:
        args: Parsed command line arguments

    Returns:
        Exit code (0 for success, 1 for error)
    """
    # Set debug logging if requested
    if hasattr(args, "debug") and args.debug:
        configure_logger(level="DEBUG")
        logger.debug("Debug logging enabled")

    # Validate arguments
    has_experiment = args.experiment or args.experiment_dir
    if not has_experiment and not args.trial:
        logger.error("Must specify --experiment, --experiment-dir, or --trial")
        return 1

    if args.experiment and args.experiment_dir:
        logger.error("Cannot specify both --experiment and --experiment-dir")
        return 1

    if has_experiment and args.trial:
        logger.error("Cannot specify both experiment and --trial")
        return 1

    # Determine skip_incomplete setting
    skip_incomplete = not args.include_incomplete

    try:
        if args.trial:
            return _generate_trial_report(args)
        return _generate_experiment_report(args, skip_incomplete=skip_incomplete)
    except ReportError as e:
        logger.error(f"Report generation failed: {e}")
        return 1
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        logger.error("Full traceback:")
        logger.error(traceback.format_exc())
        return 1


def _generate_experiment_report(
    args: argparse.Namespace, *, skip_incomplete: bool
) -> int:
    """Generate report for an experiment.

    Args:
        args: Parsed command line arguments
        skip_incomplete: Whether to skip incomplete trials

    Returns:
        Exit code
    """
    # Determine experiment directory
    if args.experiment_dir:
        experiment_dir = Path(args.experiment_dir)
        experiment_name = experiment_dir.name
    else:
        experiment_name = args.experiment
        experiment_filestore = Path(args.experiment_filestore)
        experiment_dir = resolve_experiment_dir(experiment_filestore, experiment_name)

    output_dir = args.output / experiment_name
    benchmarks_root = Path(args.benchmarks_root)

    if not experiment_dir.exists():
        logger.error(f"Experiment directory not found: {experiment_dir}")
        return 1

    if not benchmarks_root.exists():
        logger.error(f"Benchmarks root directory not found: {benchmarks_root}")
        return 1

    logger.info(f"Processing experiment: {experiment_name}")
    logger.info(f"Experiment directory: {experiment_dir}")
    logger.info(f"Output directory: {output_dir}")
    logger.info(f"Benchmarks root: {benchmarks_root}")

    # Create report generator
    generator = ReportGenerator(output_dir=output_dir, benchmarks_root=benchmarks_root)

    # Validate-only mode
    if args.validate_only:
        logger.info("Running validation only...")
        report = generator.validate_experiment(experiment_dir)
        logger.info("\n" + report)
        return 0

    # Dry-run mode
    if args.dry_run:
        logger.info("Running dry-run preview...")
        preview = generator.dry_run(experiment_dir)
        _display_dry_run_preview(preview)
        return 0

    # Generate reports
    logger.info(f"Generating {args.format} reports...")
    result = generator.generate_experiment_report(
        experiment_dir=experiment_dir,
        format=args.format,
        skip_incomplete=skip_incomplete,
    )

    # Display results
    logger.info("\n" + "=" * 60)
    logger.info("Report Generation Complete")
    logger.info("=" * 60)

    for report_type, path in result.items():
        if isinstance(path, list):
            logger.info(f"  {report_type.upper()}:")
            for p in path:
                logger.info(f"    - {p}")
        else:
            logger.info(f"  {report_type.upper()}: {path}")

    return 0


def _display_dry_run_preview(preview: dict) -> None:
    """Display dry-run preview information.

    Args:
        preview: Preview data from dry_run()
    """
    trials = preview["trials"]
    csv_files = preview["csv_files"]

    logger.info("\nDry-Run Report Preview")
    logger.info("=" * 60)
    logger.info("")
    logger.info(f"Trials to include ({len(trials)}):")

    for trial in trials:
        logger.info(
            f"  - {trial['trial_num']}: {trial['crs']} / "
            f"{trial['benchmark']} / {trial['harness']} ({trial['mode']})"
        )

    logger.info("")
    logger.info("CSV files that would be generated:")

    for filename, file_info in csv_files.items():
        logger.info(f"  - {filename} ({file_info['rows']} rows)")
        logger.info(f"    Columns: {', '.join(file_info['columns'][:5])}...")

    logger.info("")
    logger.info("=" * 60)


def _generate_trial_report(args: argparse.Namespace) -> int:
    """Generate report for a single trial.

    Args:
        args: Parsed command line arguments

    Returns:
        Exit code
    """
    trial_dir = Path(args.trial)
    output_dir = args.output
    benchmarks_root = Path(args.benchmarks_root)

    if not trial_dir.exists():
        logger.error(f"Trial directory not found: {trial_dir}")
        return 1

    if not benchmarks_root.exists():
        logger.error(f"Benchmarks root directory not found: {benchmarks_root}")
        return 1

    logger.info(f"Processing trial: {trial_dir}")
    logger.info(f"Output directory: {output_dir}")
    logger.info(f"Benchmarks root: {benchmarks_root}")

    # Create report generator
    generator = ReportGenerator(output_dir=output_dir, benchmarks_root=benchmarks_root)

    # Generate reports
    logger.info(f"Generating {args.format} reports...")
    result = generator.generate_trial_report(
        trial_dir=trial_dir,
        format=args.format,
    )

    # Display results
    logger.info("\n" + "=" * 60)
    logger.info("Report Generation Complete")
    logger.info("=" * 60)

    for report_type, path in result.items():
        logger.info(f"  {report_type.upper()}: {path}")

    return 0
