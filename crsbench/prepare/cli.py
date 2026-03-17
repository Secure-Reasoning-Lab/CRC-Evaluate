"""CLI entry point for environment preparation."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

from crsbench.prepare.uniafl_backend import prepare_uniafl_backend
from crsbench.utils.logger import get_logger
from crsbench.utils.run_helper import ensure_oss_fuzz_root

logger = get_logger(__name__)

AIXCC_BASE_IMAGES = [
    "ghcr.io/aixcc-finals/base-builder:v1.3.0",
    "ghcr.io/aixcc-finals/base-runner:v1.3.0",
    "ghcr.io/aixcc-finals/base-builder-jvm:v1.3.0",
]
HELPER_PULL_TIMEOUT = 900
DOCKER_PULL_TIMEOUT = 900
BASE_BUILD_TIMEOUT = 7200


def add_prepare_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Add the top-level ``prepare`` command.

    This command bootstraps managed third_party dependencies and ensures
    OSS-Fuzz base images are available locally.
    """
    parser = subparsers.add_parser(
        "prepare",
        help="Prepare local environment (third_party + OSS-Fuzz base images)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s prepare
  %(prog)s prepare --coverage
  %(prog)s prepare --skip-base-images
  %(prog)s prepare --build-base-images
        """,
    )
    parser.add_argument(
        "--coverage",
        action="store_true",
        help=(
            "Prepare the Atlantis/given_fuzzer coverage backend instead of "
            "OSS-Fuzz. The checkout path is fixed at "
            "third_party/atlantis-multilang-given_fuzzer."
        ),
    )
    parser.add_argument(
        "--skip-base-images",
        action="store_true",
        help="Only bootstrap managed third_party checkout; skip OSS-Fuzz base images",
    )
    parser.add_argument(
        "--build-base-images",
        action="store_true",
        help="Build OSS-Fuzz base images locally via infra/base-images/all.sh",
    )
    parser.set_defaults(command="prepare")


def run_prepare(args: argparse.Namespace) -> int:
    """Execute local environment preparation."""
    if getattr(args, "coverage", False):
        if args.skip_base_images or args.build_base_images:
            logger.error(
                "Invalid options: --coverage cannot be combined with "
                "--skip-base-images or --build-base-images"
            )
            return 2
        try:
            return prepare_uniafl_backend()
        except (FileNotFoundError, RuntimeError) as e:
            logger.error(f"Failed to prepare UniAFL coverage backend: {e}")
            return 1

    if args.skip_base_images and args.build_base_images:
        logger.error(
            "Invalid options: --skip-base-images and --build-base-images "
            "cannot be used together"
        )
        return 2

    try:
        oss_fuzz_root = Path(ensure_oss_fuzz_root())
    except Exception as e:
        logger.error(f"Failed to bootstrap managed oss-fuzz checkout: {e}")
        return 1

    logger.info(f"Managed oss-fuzz root: {oss_fuzz_root}")

    if args.skip_base_images:
        logger.info("Skipping base image preparation (--skip-base-images)")
        return 0

    helper_cmd = ["python3", "infra/helper.py", "pull_images"]
    logger.info("Pulling OSS-Fuzz base images via helper.py pull_images")
    try:
        pull_result = subprocess.run(
            helper_cmd,
            cwd=oss_fuzz_root,
            capture_output=True,
            text=True,
            timeout=HELPER_PULL_TIMEOUT,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        logger.error(
            "Timed out while pulling OSS-Fuzz base images "
            f"(timeout={HELPER_PULL_TIMEOUT}s)"
        )
        return 1
    except OSError as e:
        logger.error(f"Failed to execute OSS-Fuzz base image pull command: {e}")
        return 1
    if pull_result.returncode != 0:
        logger.error("Failed to pull OSS-Fuzz base images")
        if pull_result.stdout:
            logger.error(pull_result.stdout.strip())
        if pull_result.stderr:
            logger.error(pull_result.stderr.strip())
        return pull_result.returncode or 1

    for image in AIXCC_BASE_IMAGES:
        logger.info(f"Pulling AIXCC base image: {image}")
        try:
            aixcc_pull = subprocess.run(
                ["docker", "pull", image],
                capture_output=True,
                text=True,
                timeout=DOCKER_PULL_TIMEOUT,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            logger.error(
                f"Timed out pulling AIXCC base image {image} "
                f"(timeout={DOCKER_PULL_TIMEOUT}s)"
            )
            return 1
        except OSError as e:
            logger.error(f"Failed to execute docker pull for {image}: {e}")
            return 1
        if aixcc_pull.returncode != 0:
            logger.error(f"Failed to pull AIXCC base image: {image}")
            if aixcc_pull.stdout:
                logger.error(aixcc_pull.stdout.strip())
            if aixcc_pull.stderr:
                logger.error(aixcc_pull.stderr.strip())
            return aixcc_pull.returncode or 1

    if args.build_base_images:
        build_script = oss_fuzz_root / "infra" / "base-images" / "all.sh"
        if not build_script.exists():
            logger.error(f"Missing base image build script: {build_script}")
            return 1

        logger.info("Building OSS-Fuzz base images locally (this may take a while)")
        try:
            build_result = subprocess.run(
                ["bash", str(build_script)],
                cwd=oss_fuzz_root,
                capture_output=True,
                text=True,
                timeout=BASE_BUILD_TIMEOUT,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            logger.error(
                "Timed out while building OSS-Fuzz base images locally "
                f"(timeout={BASE_BUILD_TIMEOUT}s)"
            )
            return 1
        except OSError as e:
            logger.error(f"Failed to execute local base image build script: {e}")
            return 1
        if build_result.returncode != 0:
            logger.error("Failed to build OSS-Fuzz base images locally")
            if build_result.stdout:
                logger.error(build_result.stdout.strip())
            if build_result.stderr:
                logger.error(build_result.stderr.strip())
            return build_result.returncode or 1

    logger.info("Environment prepare completed")
    return 0
