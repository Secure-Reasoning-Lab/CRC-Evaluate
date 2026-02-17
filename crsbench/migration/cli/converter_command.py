"""CLI commands for bidirectional benchmark format conversion.

Usage:
  crsbench migrate atlanta-to-rfc --source-dir ... --target-dir ...
  crsbench migrate rfc-to-atlanta --source-dir ... --target-dir ...
"""

import argparse
import sys
from pathlib import Path

from crsbench.migration.converter import AtlantaToRFCMigrator, RFCToAtlantaMigrator
from crsbench.utils.logger import configure_logger, get_logger, log_summary

logger = get_logger(__name__)


def setup_logging(*, verbose: bool = False) -> None:
    """Configure logging based on verbosity."""
    level = "DEBUG" if verbose else "INFO"
    configure_logger(level=level)


# =============================================================================
# Atlanta to RFC
# =============================================================================


def _add_atlanta_to_rfc_args(parser: argparse.ArgumentParser) -> None:
    """Add arguments for atlanta-to-rfc subcommand."""
    parser.add_argument(
        "--source-dir",
        type=Path,
        required=True,
        help="Atlanta OSS-Fuzz projects directory (contains .aixcc/config.yaml)",
    )
    parser.add_argument(
        "--target-dir",
        type=Path,
        required=True,
        help="CRSBench RFC benchmarks directory (target for conversion)",
    )
    parser.add_argument(
        "--projects",
        type=str,
        help="Comma-separated list of specific projects to convert",
    )
    parser.add_argument(
        "--output-csv",
        type=Path,
        default="migration_report.csv",
        help="Output CSV file for migration report (default: migration_report.csv)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Perform validation without actual file operations",
    )
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="Generate CSV report only without migration or validation (fast)",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable verbose logging",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force conversion even if project already exists (default: skip existing)",
    )


def run_atlanta_to_rfc(args: argparse.Namespace) -> int:
    """Execute the atlanta-to-rfc command."""
    setup_logging(verbose=args.verbose)

    # Validate directories
    if not args.source_dir.exists():
        logger.error(f"Source directory does not exist: {args.source_dir}")
        return 1

    if not args.dry_run and not args.report_only:
        args.target_dir.mkdir(parents=True, exist_ok=True)

    # Parse projects list
    specific_projects = None
    if args.projects:
        specific_projects = [p.strip() for p in args.projects.split(",")]

    # Create migrator
    migrator = AtlantaToRFCMigrator(
        source_dir=args.source_dir,
        target_dir=args.target_dir,
        dry_run=args.dry_run,
        force=args.force,
    )

    # Import CSV writer
    from crsbench.migration.converter.atlanta_to_rfc import write_csv_report

    # Run in appropriate mode
    if args.report_only:
        logger.info("Running in REPORT-ONLY mode (no migration or validation)")
        results = migrator.collect_all_info(specific_projects)

        successful = sum(1 for r in results if r.successful)
        failed = sum(1 for r in results if not r.successful)
        total_vulns = sum(len(r.vulns) for r in results)

        log_summary(
            "Report Collection",
            {
                "total_projects": len(results),
                "successful": successful,
                "failed": failed,
                "total_vulnerabilities": total_vulns,
            },
            show_percentage=False,
        )
    else:
        results = migrator.migrate_all(specific_projects)

        successful = sum(1 for r in results if r.successful and not r.skipped)
        skipped = sum(1 for r in results if r.skipped)
        failed = sum(1 for r in results if not r.successful and not r.skipped)

        dry_run_prefix = "[DRY RUN] " if args.dry_run else ""
        log_summary(
            f"{dry_run_prefix}Migration",
            {
                "total_projects": len(results),
                "successful": successful,
                "skipped": skipped,
                "failed": failed,
            },
            show_percentage=False,
        )

    # Write CSV report
    if results:
        write_csv_report(results, args.output_csv)
        logger.info(f"CSV report written to: {args.output_csv}")

    failed_count = sum(1 for r in results if not r.successful and not r.skipped)
    return 0 if failed_count == 0 else 1


# =============================================================================
# RFC to Atlanta
# =============================================================================


def _add_rfc_to_atlanta_args(parser: argparse.ArgumentParser) -> None:
    """Add arguments for rfc-to-atlanta subcommand."""
    parser.add_argument(
        "--source-dir",
        type=Path,
        required=True,
        help="RFC format benchmarks directory (contains projects with .aixcc/meta.yaml)",
    )
    parser.add_argument(
        "--target-dir",
        type=Path,
        required=True,
        help="Atlanta OSS-Fuzz projects directory (target for conversion)",
    )
    parser.add_argument(
        "--projects",
        type=str,
        help="Comma-separated list of specific projects to convert",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Perform validation without actual file operations",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable verbose logging",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force conversion even if project already exists (default: skip existing)",
    )


def run_rfc_to_atlanta(args: argparse.Namespace) -> int:
    """Execute the rfc-to-atlanta command."""
    setup_logging(verbose=args.verbose)

    # Validate directories
    if not args.source_dir.exists():
        logger.error(f"Source directory does not exist: {args.source_dir}")
        return 1

    if not args.dry_run:
        args.target_dir.mkdir(parents=True, exist_ok=True)

    # Parse projects list
    specific_projects = None
    if args.projects:
        specific_projects = [p.strip() for p in args.projects.split(",")]

    # Create migrator
    migrator = RFCToAtlantaMigrator(
        source_dir=args.source_dir,
        target_dir=args.target_dir,
        dry_run=args.dry_run,
        force=args.force,
    )

    # Run migration
    results = migrator.migrate_all(specific_projects)

    # Print summary
    successful = sum(1 for r in results if r.successful and not r.skipped)
    skipped = sum(1 for r in results if r.skipped)
    failed = sum(1 for r in results if not r.successful and not r.skipped)
    total_harnesses = sum(r.num_harnesses for r in results)
    total_vulns = sum(r.num_vulns for r in results)

    dry_run_prefix = "[DRY RUN] " if args.dry_run else ""
    log_summary(
        f"{dry_run_prefix}Conversion",
        {
            "total_projects": len(results),
            "successful": successful,
            "skipped": skipped,
            "failed": failed,
            "total_harnesses": total_harnesses,
            "total_vulns": total_vulns,
        },
        show_percentage=False,
    )

    # Log errors and warnings
    for ctx in results:
        if ctx.errors:
            logger.error(f"  {ctx.project_name} errors: {ctx.errors}")
        if ctx.warnings:
            logger.warning(f"  {ctx.project_name} warnings: {ctx.warnings}")

    return 0 if failed == 0 else 1


# =============================================================================
# Main migrate subparser
# =============================================================================


def add_migrate_subparser(subparsers) -> None:
    """Add 'migrate' subcommand with nested subcommands to the CLI."""
    migrate_parser = subparsers.add_parser(
        "migrate",
        help="Migration and generation tools for CRSBench benchmarks",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Subcommands:
  atlanta-to-rfc      Convert Atlanta OSS-Fuzz format to CRSBench RFC format
  rfc-to-atlanta      Convert CRSBench RFC format to Atlanta OSS-Fuzz format
  generate-test-sh    Generate test.sh for benchmarks using Claude Agent SDK
  generate-vuln-yaml  Generate vuln.yaml for CPVs using Claude Agent SDK

Examples:
  # Convert Atlanta to RFC
  crsbench migrate atlanta-to-rfc --source-dir /path/to/oss-fuzz/projects --target-dir /path/to/benchmarks

  # Convert RFC to Atlanta
  crsbench migrate rfc-to-atlanta --source-dir /path/to/benchmarks --target-dir /path/to/oss-fuzz/projects

  # Generate test.sh
  crsbench migrate generate-test-sh --benchmark curl-delta-01

  # Generate vuln.yaml
  crsbench migrate generate-vuln-yaml --benchmark curl-delta-01
        """,
    )

    # Add nested subparsers for migrate command
    migrate_subparsers = migrate_parser.add_subparsers(
        dest="migrate_subcommand",
        help="Migration/generation subcommand",
    )

    # atlanta-to-rfc subcommand
    atlanta_to_rfc_parser = migrate_subparsers.add_parser(
        "atlanta-to-rfc",
        help="Convert Atlanta OSS-Fuzz benchmarks to CRSBench RFC format",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  crsbench migrate atlanta-to-rfc --source-dir /path/to/oss-fuzz/projects --target-dir /path/to/benchmarks
  crsbench migrate atlanta-to-rfc --source-dir /path/to/oss-fuzz/projects --target-dir /path/to/benchmarks --dry-run
  crsbench migrate atlanta-to-rfc --source-dir /path/to/oss-fuzz/projects --target-dir /path/to/benchmarks --projects curl-delta-04
        """,
    )
    _add_atlanta_to_rfc_args(atlanta_to_rfc_parser)

    # rfc-to-atlanta subcommand
    rfc_to_atlanta_parser = migrate_subparsers.add_parser(
        "rfc-to-atlanta",
        help="Convert CRSBench RFC format benchmarks to Atlanta OSS-Fuzz format",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  crsbench migrate rfc-to-atlanta --source-dir /path/to/benchmarks --target-dir /path/to/oss-fuzz/projects
  crsbench migrate rfc-to-atlanta --source-dir /path/to/benchmarks --target-dir /path/to/oss-fuzz/projects --dry-run
  crsbench migrate rfc-to-atlanta --source-dir /path/to/benchmarks --target-dir /path/to/oss-fuzz/projects --projects curl-delta-04
        """,
    )
    _add_rfc_to_atlanta_args(rfc_to_atlanta_parser)

    # generate-test-sh subcommand
    from crsbench.migration.cli.test_sh_command import (
        _add_arguments as _add_test_sh_args,
    )

    generate_test_sh_parser = migrate_subparsers.add_parser(
        "generate-test-sh",
        help="Generate test.sh for CRSBench benchmarks using Claude Agent SDK",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  crsbench migrate generate-test-sh --benchmark apache-commons-compress-delta-01
  crsbench migrate generate-test-sh --benchmarks bench1 bench2 --parallel 2
  crsbench migrate generate-test-sh --all-missing
        """,
    )
    _add_test_sh_args(generate_test_sh_parser)

    # generate-vuln-yaml subcommand
    from crsbench.migration.cli.vuln_yaml_command import (
        _add_arguments as _add_vuln_yaml_args,
    )

    generate_vuln_yaml_parser = migrate_subparsers.add_parser(
        "generate-vuln-yaml",
        help="Generate vuln.yaml for CRSBench CPVs using Claude Agent SDK",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  crsbench migrate generate-vuln-yaml
  crsbench migrate generate-vuln-yaml --benchmark atlanta-curl-delta-01
  crsbench migrate generate-vuln-yaml --benchmark atlanta-curl-delta-01 --harness curl_fuzzer_http --cpv cpv_0
        """,
    )
    _add_vuln_yaml_args(generate_vuln_yaml_parser)

    migrate_parser.set_defaults(command="migrate")


def run_migrate(args: argparse.Namespace) -> int:
    """Execute the migrate command by dispatching to subcommands."""
    if not hasattr(args, "migrate_subcommand") or args.migrate_subcommand is None:
        logger.error("No subcommand specified.")
        logger.info(
            "Available: atlanta-to-rfc, rfc-to-atlanta, generate-test-sh, generate-vuln-yaml"
        )
        logger.info("Run 'crsbench migrate --help' for usage information.")
        return 1

    if args.migrate_subcommand == "atlanta-to-rfc":
        return run_atlanta_to_rfc(args)
    if args.migrate_subcommand == "rfc-to-atlanta":
        return run_rfc_to_atlanta(args)
    if args.migrate_subcommand == "generate-test-sh":
        from crsbench.migration.cli.test_sh_command import run_generate_test_sh

        return run_generate_test_sh(args)
    if args.migrate_subcommand == "generate-vuln-yaml":
        from crsbench.migration.cli.vuln_yaml_command import run_generate_vuln_yaml

        return run_generate_vuln_yaml(args)
    logger.error(f"Unknown migrate subcommand: {args.migrate_subcommand}")
    return 1


# =============================================================================
# Standalone CLI
# =============================================================================


def main() -> None:
    """Standalone CLI entry point for converter commands."""
    parser = argparse.ArgumentParser(
        description="Bidirectional benchmark format converter",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", help="Conversion direction")

    # Add nested migrate subparsers directly for standalone use
    atlanta_to_rfc_parser = subparsers.add_parser(
        "atlanta-to-rfc",
        help="Convert Atlanta OSS-Fuzz benchmarks to CRSBench RFC format",
    )
    _add_atlanta_to_rfc_args(atlanta_to_rfc_parser)

    rfc_to_atlanta_parser = subparsers.add_parser(
        "rfc-to-atlanta",
        help="Convert CRSBench RFC format benchmarks to Atlanta OSS-Fuzz format",
    )
    _add_rfc_to_atlanta_args(rfc_to_atlanta_parser)

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "atlanta-to-rfc":
        sys.exit(run_atlanta_to_rfc(args))
    elif args.command == "rfc-to-atlanta":
        sys.exit(run_rfc_to_atlanta(args))


if __name__ == "__main__":
    main()
