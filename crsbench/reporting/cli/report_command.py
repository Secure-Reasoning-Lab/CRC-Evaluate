"""CLI command for report generation."""

import argparse
from pathlib import Path

from crsbench.reporting.errors import ReportError
from crsbench.reporting.orchestrator import ReportGenerator
from crsbench.utils.logger import get_logger

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
  # Generate report for an experiment
  %(prog)s --experiment test-experiment --output ./reports

  # Generate JSON reports only
  %(prog)s --experiment test-experiment --format json

  # Generate HTML reports only
  %(prog)s --experiment test-experiment --format html

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
        help="Experiment name to generate report for",
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
        type=str,
        default="report_filestore",
        metavar="OUTPUT_DIR",
        help="Output directory for reports (default: report_filestore)",
    )

    report_parser.add_argument(
        "--format",
        type=str,
        choices=["json", "html", "both"],
        default="both",
        metavar="FORMAT",
        help="Report format: json, html, or both (default: both)",
    )

    report_parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Only validate experiment completeness, don't generate reports",
    )

    report_parser.add_argument(
        "--skip-incomplete",
        action="store_true",
        default=True,
        help="Skip incomplete trials (default: True)",
    )

    report_parser.add_argument(
        "--include-incomplete",
        action="store_true",
        help="Include incomplete trials in report (overrides --skip-incomplete)",
    )

    report_parser.set_defaults(command="report")


def run_report(args: argparse.Namespace) -> int:
    """Execute the report command.

    Args:
        args: Parsed command line arguments

    Returns:
        Exit code (0 for success, 1 for error)
    """
    # Validate arguments
    if not args.experiment and not args.trial:
        logger.error("Must specify either --experiment or --trial")
        return 1

    if args.experiment and args.trial:
        logger.error("Cannot specify both --experiment and --trial")
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
    experiment_name = args.experiment
    experiment_filestore = Path(args.experiment_filestore)
    experiment_dir = experiment_filestore / experiment_name
    output_dir = Path(args.output) / experiment_name

    if not experiment_dir.exists():
        logger.error(f"Experiment directory not found: {experiment_dir}")
        return 1

    logger.info(f"Processing experiment: {experiment_name}")
    logger.info(f"Experiment directory: {experiment_dir}")
    logger.info(f"Output directory: {output_dir}")

    # Create report generator
    generator = ReportGenerator(output_dir=output_dir)

    # Validate-only mode
    if args.validate_only:
        logger.info("Running validation only...")
        report = generator.validate_experiment(experiment_dir)
        logger.info("\n" + report)
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
        logger.info(f"  {report_type.upper()}: {path}")

    return 0


def _generate_trial_report(args: argparse.Namespace) -> int:
    """Generate report for a single trial.

    Args:
        args: Parsed command line arguments

    Returns:
        Exit code
    """
    trial_dir = Path(args.trial)
    output_dir = Path(args.output)

    if not trial_dir.exists():
        logger.error(f"Trial directory not found: {trial_dir}")
        return 1

    logger.info(f"Processing trial: {trial_dir}")
    logger.info(f"Output directory: {output_dir}")

    # Create report generator
    generator = ReportGenerator(output_dir=output_dir)

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
