#!/usr/bin/env python3
"""Experiment runner for CRSBench evaluation framework.

This script provides a single entry point for running CRS evaluations with
standardized experiment configurations, CRS integration, and benchmark suite
management.

Usage:
    crsbench \
        --experiment-config experiment-config.yaml \
        --benchmarks benchmark1,benchmark2 \
        --experiment-name my-experiment \
        --crses atlantis-c,atlantis-multilang
"""

import argparse
import logging
import sys
from pathlib import Path
from typing import List

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def parse_arguments() -> argparse.Namespace:
    """Parse command line arguments.

    Returns:
        Parsed arguments with experiment configuration.
    """
    parser = argparse.ArgumentParser(
        prog='crsbench',
        description='Run CRS evaluation experiments',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Run single benchmark with single CRS
  %(prog)s --experiment-config config.yaml --benchmarks bench1 \\
           --experiment-name exp1 --crses atlantis-c

  # Run multiple benchmarks with multiple CRSes
  %(prog)s --experiment-config config.yaml \\
           --benchmarks bench1,bench2,bench3 \\
           --experiment-name multi-exp --crses crs1,crs2

  # Run benchmark suite
  %(prog)s --experiment-config config.yaml --benchmarks crsbench-c \\
           --experiment-name suite-exp --crses atlantis-multilang
        """
    )

    parser.add_argument(
        '--experiment-config',
        type=str,
        required=True,
        metavar='CONFIG_FILE',
        help='Path to experiment configuration YAML file (e.g., experiment-config.yaml)'
    )

    parser.add_argument(
        '--benchmarks',
        type=str,
        required=True,
        metavar='BENCHMARK_LIST',
        help='Comma-separated list of benchmarks or benchmark suite name (e.g., bench1,bench2 or crsbench-c)'
    )

    parser.add_argument(
        '--experiment-name',
        type=str,
        required=True,
        metavar='EXPERIMENT_NAME',
        help='Name for this experiment (used for tracking and reporting)'
    )

    parser.add_argument(
        '--crses',
        type=str,
        required=True,
        metavar='CRS_LIST',
        help='Comma-separated list of CRS implementations to evaluate (e.g., atlantis-c,atlantis-multilang)'
    )

    return parser.parse_args()


def validate_arguments(args: argparse.Namespace) -> None:
    """Validate parsed arguments.

    Args:
        args: Parsed command line arguments

    Raises:
        SystemExit: If validation fails
    """
    # Validate experiment config file exists
    config_path = Path(args.experiment_config)
    if not config_path.exists():
        logger.error(f"Experiment configuration file not found: {config_path}")
        sys.exit(1)

    if not config_path.is_file():
        logger.error(f"Experiment configuration path is not a file: {config_path}")
        sys.exit(1)

    # Validate file extension
    if config_path.suffix not in ['.yaml', '.yml']:
        logger.warning(f"Configuration file does not have .yaml/.yml extension: {config_path}")

    logger.info(f"Experiment configuration: {config_path}")


def parse_list_argument(arg_value: str) -> List[str]:
    """Parse comma-separated list argument.

    Args:
        arg_value: Comma-separated string value

    Returns:
        List of stripped strings
    """
    return [item.strip() for item in arg_value.split(',') if item.strip()]


def main() -> None:
    """Main entry point for the experiment runner."""
    # Parse arguments
    args = parse_arguments()

    # Validate arguments
    validate_arguments(args)

    # Parse list arguments
    benchmarks = parse_list_argument(args.benchmarks)
    crses = parse_list_argument(args.crses)

    # Log experiment configuration
    logger.info("="*60)
    logger.info("CRSBench Experiment Runner")
    logger.info("="*60)
    logger.info(f"Experiment name: {args.experiment_name}")
    logger.info(f"Configuration file: {args.experiment_config}")
    logger.info(f"Benchmarks ({len(benchmarks)}): {', '.join(benchmarks)}")
    logger.info(f"CRSes ({len(crses)}): {', '.join(crses)}")
    logger.info("="*60)

    # TODO: Implement actual experiment running logic
    logger.info("Experiment running logic not yet implemented")
    logger.info("This is a placeholder that will be implemented in future iterations")

    # Print parsed configuration for verification
    print("\nParsed Configuration:")
    print(f"  Experiment Name: {args.experiment_name}")
    print(f"  Config File: {args.experiment_config}")
    print(f"  Benchmarks: {benchmarks}")
    print(f"  CRSes: {crses}")


if __name__ == "__main__":
    main()
