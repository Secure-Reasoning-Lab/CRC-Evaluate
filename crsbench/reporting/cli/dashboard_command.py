"""CLI command for starting the dashboard web server."""

import argparse
import os
import subprocess
from pathlib import Path

from crsbench.utils.logger import get_logger

logger = get_logger(__name__)


def add_dashboard_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Add the 'dashboard' subcommand to the CLI.

    Args:
        subparsers: Subparser action from main argument parser
    """
    dashboard_parser = subparsers.add_parser(
        "dashboard",
        help="Start the web dashboard for visualizing experiment results",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="""
Start an interactive web dashboard to visualize CRS experiment results.

The dashboard reads pre-generated JSON reports from the report filestore.
Use 'crsbench report' to generate reports first.

The dashboard provides:
- Experiment overview with metrics and charts
- CRS comparison and benchmark analysis
- Trial-level details with time-series data
        """,
        epilog="""
Examples:
  # Start dashboard with default settings
  %(prog)s --report-dir ./report_filestore

  # Start dashboard on a specific port
  %(prog)s --report-dir ./report_filestore --port 3001

  # Start in production mode
  %(prog)s --report-dir ./report_filestore --production
        """,
    )

    dashboard_parser.add_argument(
        "--report-dir",
        type=str,
        required=True,
        metavar="PATH",
        help="Path to the report filestore directory containing generated JSON reports",
    )

    dashboard_parser.add_argument(
        "--port",
        type=int,
        default=3000,
        metavar="PORT",
        help="Port to run the dashboard on (default: 3000)",
    )

    dashboard_parser.add_argument(
        "--production",
        action="store_true",
        help="Run in production mode (builds and serves optimized bundle)",
    )

    dashboard_parser.set_defaults(command="dashboard")


def run_dashboard(args: argparse.Namespace) -> int:
    """Execute the dashboard command.

    Args:
        args: Parsed command line arguments

    Returns:
        Exit code (0 for success, 1 for error)
    """
    report_dir = Path(args.report_dir).resolve()
    port = args.port
    production = args.production

    # Validate report directory exists
    if not report_dir.exists():
        logger.error(f"Report directory not found: {report_dir}")
        logger.error("Use 'crsbench report' to generate reports first.")
        return 1

    if not report_dir.is_dir():
        logger.error(f"Path is not a directory: {report_dir}")
        return 1

    # Find the dashboard directory relative to this file
    dashboard_dir = _find_dashboard_dir()
    if dashboard_dir is None:
        logger.error(
            "Dashboard directory not found. "
            "Please ensure the dashboard is installed in the repository."
        )
        return 1

    # Check if node_modules exists
    node_modules = dashboard_dir / "node_modules"
    if not node_modules.exists():
        logger.info("Installing dashboard dependencies...")
        result = subprocess.run(
            ["npm", "install"],
            cwd=dashboard_dir,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            logger.error(f"Failed to install dependencies: {result.stderr}")
            return 1

    # Set up environment
    env = os.environ.copy()
    env["REPORT_DIR"] = str(report_dir)
    env["PORT"] = str(port)

    logger.info("Starting dashboard server...")
    logger.info(f"  Report directory: {report_dir}")
    logger.info(f"  Port: {port}")
    logger.info(f"  Mode: {'production' if production else 'development'}")

    # Build and start in production mode, or run dev server
    if production:
        # Build the production bundle
        logger.info("Building production bundle...")
        build_result = subprocess.run(
            ["npm", "run", "build"],
            cwd=dashboard_dir,
            env=env,
        )
        if build_result.returncode != 0:
            logger.error("Failed to build dashboard")
            return 1

        # Start production server
        cmd = ["npm", "run", "start"]
    else:
        cmd = ["npm", "run", "dev"]

    logger.info(f"\nDashboard available at: http://localhost:{port}")
    logger.info("Press Ctrl+C to stop the server.\n")

    # Run the server
    process = subprocess.Popen(
        cmd,
        cwd=dashboard_dir,
        env=env,
    )

    try:
        # Wait for the process to complete
        return process.wait()
    except KeyboardInterrupt:
        logger.info("\nStopping dashboard server...")
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            logger.warning("Process did not terminate, killing...")
            process.kill()
            process.wait()
        return 0


def _find_dashboard_dir() -> Path | None:
    """Find the dashboard directory.

    Returns:
        Path to dashboard directory, or None if not found
    """
    # Try relative to this file (inside crsbench package)
    this_file = Path(__file__).resolve()
    repo_root = (
        this_file.parent.parent.parent.parent
    )  # cli -> reporting -> crsbench -> repo
    dashboard_dir = repo_root / "dashboard"

    if dashboard_dir.exists() and (dashboard_dir / "package.json").exists():
        return dashboard_dir

    # Try current working directory
    cwd_dashboard = Path.cwd() / "dashboard"
    if cwd_dashboard.exists() and (cwd_dashboard / "package.json").exists():
        return cwd_dashboard

    return None
