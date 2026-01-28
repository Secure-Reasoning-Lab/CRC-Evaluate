"""CLI module for benchmark CI testing."""

import argparse

from crsbench.benchmark_ci.cli.commands import (
    all_cmd,
    coverage_cmd,
    format_cmd,
    parse_cmd,
    patch_cmd,
    pov_cmd,
    retry_cmd,
    rts_cmd,
)


def add_ci_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Add 'ci' subcommand with sub-subcommands to the CLI."""
    ci_parser = subparsers.add_parser(
        "ci",
        help="Validate benchmarks (format, POV, patch, coverage, rts)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Subcommands:
  format      Run format validation only (fast, no Docker)
  pov         Run POV verification
  patch       Run patch verification
  coverage    Run coverage validation
  rts         Run regression test selection checks
  all         Run all validation checks
  parse       Parse and display results from output directory
  retry       Retry failed benchmarks from a previous CI run
        """,
    )
    ci_parser.set_defaults(command="ci")

    ci_subparsers = ci_parser.add_subparsers(dest="ci_subcommand")

    # Register all subcommands
    format_cmd.register(ci_subparsers)
    pov_cmd.register(ci_subparsers)
    patch_cmd.register(ci_subparsers)
    coverage_cmd.register(ci_subparsers)
    rts_cmd.register(ci_subparsers)
    all_cmd.register(ci_subparsers)
    parse_cmd.register(ci_subparsers)
    retry_cmd.register(ci_subparsers)


def dispatch_ci(args: argparse.Namespace) -> int:
    """Dispatch to appropriate ci subcommand handler."""
    if not hasattr(args, "ci_func") or args.ci_func is None:
        from crsbench.utils.logger import get_logger

        logger = get_logger(__name__)
        logger.error("No subcommand specified. Run 'crsbench ci --help' for usage.")
        return 1
    return args.ci_func(args)


__all__ = ["add_ci_subparser", "dispatch_ci"]
