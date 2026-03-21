"""CLI entry point for experiment config generation."""

from __future__ import annotations

import argparse
from pathlib import Path

from crsbench.utils.logger import get_logger

logger = get_logger(__name__)


def add_gen_config_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Add the ``gen-config`` subcommand.

    Guides users through required fields and generates a valid
    grouped-format experiment config YAML file.
    """
    parser = subparsers.add_parser(
        "gen-config",
        help="Generate an experiment config file interactively",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s gen-config
  %(prog)s gen-config --output my-experiment.yaml
  %(prog)s gen-config --validate
  %(prog)s gen-config --output config.yaml --validate
        """,
    )
    parser.add_argument(
        "--output",
        "-o",
        type=str,
        default="experiment-config.yaml",
        help="Output file path (default: experiment-config.yaml)",
    )
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Validate the generated config before writing",
    )
    parser.add_argument(
        "--suites-root",
        type=str,
        default="benchmark-suites",
        help="Benchmark suites directory (default: benchmark-suites)",
    )
    parser.add_argument(
        "--registry-dir",
        type=str,
        default="oss-crs/registry",
        help="CRS registry directory (default: oss-crs/registry)",
    )


def run_gen_config(args: argparse.Namespace) -> int:
    """Execute the gen-config command.

    Args:
        args: Parsed CLI arguments.

    Returns:
        Exit code (0 = success, 1 = failure).
    """
    from crsbench.genconfig.generator import generate_config

    output_path = Path(args.output)

    if output_path.exists():
        confirm = input(f"File '{output_path}' already exists. Overwrite? [y/N]: ")
        if confirm.strip().lower() not in ("y", "yes"):
            logger.info("Aborted.")
            return 1

    try:
        success = generate_config(
            output_path=output_path,
            validate=args.validate,
            suites_root=Path(args.suites_root),
            registry_dir=Path(args.registry_dir),
        )
        return 0 if success else 1
    except KeyboardInterrupt:
        logger.info("\nAborted.")
        return 1
    except Exception as e:
        logger.error(f"Config generation failed: {e}")
        return 1
