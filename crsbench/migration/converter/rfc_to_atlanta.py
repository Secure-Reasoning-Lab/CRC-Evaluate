"""
RFC to Atlanta OSS-Fuzz format converter.

Converts CRSBench RFC format benchmarks back to Atlanta OSS-Fuzz format.
This enables roundtrip conversion: Atlanta → RFC → Atlanta (lossless).
"""

import argparse
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from crsbench.migration.converter.config import ConfigConverter
from crsbench.migration.converter.files import FileMigrator
from crsbench.utils.logger import configure_logger, get_logger


@dataclass
class ReverseConversionContext:
    """Context for RFC → Atlanta conversion."""

    source_dir: Path
    target_dir: Path
    dry_run: bool
    project_name: str
    successful: bool = True
    skipped: bool = False
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    num_harnesses: int = 0
    num_vulns: int = 0


class RFCToAtlantaMigrator:
    """Converts RFC format benchmarks to Atlanta OSS-Fuzz format."""

    def __init__(
        self,
        source_dir: Path,
        target_dir: Path,
        *,
        dry_run: bool = False,
        force: bool = False,
    ):
        """
        Initialize the reverse migrator.

        Args:
            source_dir: RFC format benchmarks directory
            target_dir: Atlanta OSS-Fuzz projects directory
            dry_run: If True, perform validation without actual file operations
            force: If True, overwrite existing converted projects
        """
        self.source_dir = source_dir
        self.target_dir = target_dir
        self.dry_run = dry_run
        self.force = force
        self.logger = get_logger(__name__)

        self.config_converter = ConfigConverter()
        self.file_migrator = FileMigrator(dry_run=dry_run)

    def discover_projects(
        self, specific_projects: Optional[List[str]] = None
    ) -> List[Path]:
        """
        Discover RFC format benchmarks to convert.

        Args:
            specific_projects: Optional list of specific project names

        Returns:
            List of project directory paths with .aixcc/meta.yaml
        """
        projects = []

        for item in self.source_dir.iterdir():
            if not item.is_dir():
                continue

            # Check for RFC format indicator (meta.yaml)
            meta_yaml = item / ".aixcc" / "meta.yaml"
            if not meta_yaml.exists():
                continue

            # Filter by specific projects if provided
            if specific_projects and item.name not in specific_projects:
                continue

            projects.append(item)

        return sorted(projects, key=lambda p: p.name)

    def migrate_project(self, project_dir: Path) -> ReverseConversionContext:
        """
        Convert a single project from RFC to Atlanta format.

        Args:
            project_dir: Path to the RFC format project

        Returns:
            ReverseConversionContext with conversion results
        """
        project_name = project_dir.name
        self.logger.info(
            f"{'[DRY RUN] ' if self.dry_run else ''}Converting: {project_name}"
        )

        ctx = ReverseConversionContext(
            source_dir=project_dir,
            target_dir=self.target_dir / project_name,
            dry_run=self.dry_run,
            project_name=project_name,
        )

        # Check if already exists
        target_config = ctx.target_dir / ".aixcc" / "config.yaml"
        if target_config.exists() and not self.force:
            ctx.skipped = True
            self.logger.info("  Already converted, skipping (use --force to overwrite)")
            return ctx

        # Clean up existing .aixcc directory when --force is used
        target_aixcc = ctx.target_dir / ".aixcc"
        if target_aixcc.exists() and self.force:
            if not self.dry_run:
                shutil.rmtree(target_aixcc)
                self.logger.info("  Cleaned up existing .aixcc directory")
            else:
                self.logger.info("  [DRY RUN] Would clean up existing .aixcc directory")

        try:
            # Step 1: Validate source
            self._validate_source(ctx)

            # Step 2: Convert meta.yaml → config.yaml
            self._convert_config(ctx)

            # Step 3: Copy root files
            self._copy_root_files(ctx)

            # Step 4: Flatten vulnerability structure
            self._flatten_vulnerabilities(ctx)

            # Step 5: Copy tests directory if exists
            self._copy_tests(ctx)

            if ctx.successful:
                self.logger.info(f"  Successfully converted {project_name}")
            else:
                self.logger.error(f"  Conversion failed: {ctx.errors}")

        except Exception as e:
            ctx.successful = False
            ctx.errors.append(f"Unexpected error: {e}")
            self.logger.exception(f"Error converting {project_name}")

        return ctx

    def _validate_source(self, ctx: ReverseConversionContext) -> None:
        """Validate RFC format source structure."""
        meta_yaml = ctx.source_dir / ".aixcc" / "meta.yaml"

        if not meta_yaml.exists():
            ctx.successful = False
            ctx.errors.append("Missing .aixcc/meta.yaml")
            raise FileNotFoundError(f"meta.yaml not found: {meta_yaml}")

    def _convert_config(self, ctx: ReverseConversionContext) -> None:
        """Convert meta.yaml to config.yaml."""
        self.logger.info("  Converting meta.yaml → config.yaml")

        source_meta = ctx.source_dir / ".aixcc" / "meta.yaml"
        target_config = ctx.target_dir / ".aixcc" / "config.yaml"

        # Convert using ConfigConverter
        config = self.config_converter.convert_reverse(source_meta)

        # Count harnesses and vulns
        ctx.num_harnesses = len(config.get("harness_files", []))
        ctx.num_vulns = sum(
            len(h.get("cpvs", [])) for h in config.get("harness_files", [])
        )

        if not self.dry_run:
            self.config_converter.write_atlanta_config(config, target_config)
            self.logger.debug(f"  Wrote config.yaml: {target_config}")
        else:
            self.logger.info(f"    [DRY RUN] Would write: {target_config}")

    def _copy_root_files(self, ctx: ReverseConversionContext) -> None:
        """Copy root-level project files."""
        self.logger.info("  Copying root files")

        root_files = ["build.sh", "test.sh", "Dockerfile", "project.yaml"]

        for filename in root_files:
            source = ctx.source_dir / filename
            target = ctx.target_dir / filename

            if source.exists():
                self.file_migrator.copy_file(source, target, ctx)

        # Copy any other files at root (excluding .aixcc)
        for item in ctx.source_dir.iterdir():
            if item.name == ".aixcc" or item.name in root_files:
                continue

            target = ctx.target_dir / item.name

            if item.is_file():
                self.file_migrator.copy_file(item, target, ctx)
            elif item.is_dir():
                self.file_migrator.copy_directory(item, target, ctx)

    def _flatten_vulnerabilities(self, ctx: ReverseConversionContext) -> None:
        """Flatten RFC vulnerability structure to Atlanta format."""
        self.logger.info("  Flattening vulnerability structure")

        aixcc_dir = ctx.source_dir / ".aixcc"

        # Find all harness directories (directories that aren't meta.yaml, tests, etc.)
        for harness_dir in aixcc_dir.iterdir():
            if not harness_dir.is_dir():
                continue

            # Skip non-harness directories
            if harness_dir.name in {"tests"}:
                continue

            harness_name = harness_dir.name

            # Find CPV directories
            for cpv_dir in harness_dir.iterdir():
                if not cpv_dir.is_dir():
                    continue

                cpv_name = cpv_dir.name
                self.logger.debug(f"    Processing {harness_name}/{cpv_name}")

                # Flatten each type of file
                self._flatten_patches(ctx, harness_name, cpv_name, cpv_dir)
                self._flatten_povs(ctx, harness_name, cpv_name, cpv_dir)
                self._flatten_logs(ctx, harness_name, cpv_name, cpv_dir)
                self._convert_vuln_yaml(ctx, harness_name, cpv_name, cpv_dir)

    def _flatten_patches(
        self,
        ctx: ReverseConversionContext,
        harness_name: str,
        cpv_name: str,
        cpv_dir: Path,
    ) -> None:
        """RFC patches/ → Atlanta patches/{harness}/{cpv}.diff"""
        source = cpv_dir / "patches" / "patch_0.diff"
        target = (
            ctx.target_dir / ".aixcc" / "patches" / harness_name / f"{cpv_name}.diff"
        )

        if source.exists():
            self.file_migrator.copy_file(source, target, ctx)
        else:
            ctx.warnings.append(f"Missing patch: {harness_name}/{cpv_name}")

    def _flatten_povs(
        self,
        ctx: ReverseConversionContext,
        harness_name: str,
        cpv_name: str,
        cpv_dir: Path,
    ) -> None:
        """RFC blobs/ → Atlanta povs/{harness}/{cpv} (no extension)"""
        source = cpv_dir / "blobs" / "pov_0.blob"
        target = ctx.target_dir / ".aixcc" / "povs" / harness_name / cpv_name

        if source.exists():
            self.file_migrator.copy_file(source, target, ctx)
        else:
            ctx.warnings.append(f"Missing POV blob: {harness_name}/{cpv_name}")

    def _flatten_logs(
        self,
        ctx: ReverseConversionContext,
        harness_name: str,
        cpv_name: str,
        cpv_dir: Path,
    ) -> None:
        """RFC logs/ → Atlanta crash_logs/{harness}/{cpv}.log"""
        source = cpv_dir / "logs" / "pov_0.log"
        target = (
            ctx.target_dir / ".aixcc" / "crash_logs" / harness_name / f"{cpv_name}.log"
        )

        if source.exists():
            self.file_migrator.copy_file(source, target, ctx)
        else:
            ctx.warnings.append(f"Missing crash log: {harness_name}/{cpv_name}")

    def _convert_vuln_yaml(
        self,
        ctx: ReverseConversionContext,
        harness_name: str,
        cpv_name: str,
        cpv_dir: Path,
    ) -> None:
        """RFC vuln.yaml → Atlanta vulns/{harness}/{cpv}.yaml"""
        source = cpv_dir / "vuln.yaml"
        target = ctx.target_dir / ".aixcc" / "vulns" / harness_name / f"{cpv_name}.yaml"

        if not source.exists():
            ctx.warnings.append(f"Missing vuln.yaml: {harness_name}/{cpv_name}")
            return

        if self.dry_run:
            self.logger.info(f"    [DRY RUN] Would convert: {target}")
            return

        # Read RFC vuln.yaml
        with source.open("r") as f:
            rfc_vuln = yaml.safe_load(f)

        # Convert to Atlanta format
        atlanta_vuln = self._rfc_to_atlanta_vuln(rfc_vuln, harness_name, cpv_name)

        # Write Atlanta format
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w") as f:
            yaml.dump(
                atlanta_vuln,
                f,
                default_flow_style=False,
                allow_unicode=True,
                sort_keys=False,
                indent=2,
                width=120,
            )

        self.logger.debug(f"    Converted vuln.yaml: {target}")

    def _rfc_to_atlanta_vuln(
        self, rfc_vuln: Dict[str, Any], harness_name: str, cpv_name: str
    ) -> Dict[str, Any]:
        """
        Convert RFC vuln.yaml structure to Atlanta format.

        Args:
            rfc_vuln: RFC format vulnerability dictionary
            harness_name: Harness name
            cpv_name: CPV name

        Returns:
            Atlanta format vulnerability dictionary
        """
        return {
            "metadata_spec_version": "v1",
            "name": rfc_vuln.get("name", cpv_name),
            "author": "crsbench",
            "details": {
                "cwes": rfc_vuln.get("cwes", []),
                "description": rfc_vuln.get("description", ""),
                "locations": rfc_vuln.get("locations", []),
            },
            "pov": {
                "harness": harness_name,
                "blob": f"{cpv_name}",
            },
            "patch": {
                "good": f"{cpv_name}.diff",
            },
        }

    def _copy_tests(self, ctx: ReverseConversionContext) -> None:
        """Copy tests directory if exists."""
        source_tests = ctx.source_dir / ".aixcc" / "tests"

        if not source_tests.exists():
            return

        target_tests = ctx.target_dir / ".aixcc" / "tests"
        self.logger.info("  Copying tests directory")
        self.file_migrator.copy_directory(source_tests, target_tests, ctx)

    def migrate_all(
        self, specific_projects: Optional[List[str]] = None
    ) -> List[ReverseConversionContext]:
        """
        Convert all discovered projects.

        Args:
            specific_projects: Optional list of specific project names

        Returns:
            List of ReverseConversionContext with results
        """
        projects = self.discover_projects(specific_projects)

        if not projects:
            self.logger.warning("No RFC format projects found")
            return []

        self.logger.info(f"Found {len(projects)} projects to convert")

        results = []
        for project_dir in projects:
            ctx = self.migrate_project(project_dir)
            results.append(ctx)

        return results


def setup_logging(*, verbose: bool = False) -> None:
    """Configure logging based on verbosity."""
    level = "DEBUG" if verbose else "INFO"
    configure_logger(level=level)


def main() -> None:
    """Main entry point for the reverse migration script."""
    parser = argparse.ArgumentParser(
        description="Convert CRSBench RFC format benchmarks to Atlanta OSS-Fuzz format",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Convert all projects (skip existing by default)
  python rfc_to_atlanta.py --source-dir /path/to/benchmarks --target-dir /path/to/oss-fuzz/projects

  # Force re-conversion of all projects (overwrite existing)
  python rfc_to_atlanta.py --source-dir /path/to/benchmarks --target-dir /path/to/oss-fuzz/projects --force

  # Dry run to validate
  python rfc_to_atlanta.py --source-dir /path/to/benchmarks --target-dir /path/to/oss-fuzz/projects --dry-run

  # Convert specific projects
  python rfc_to_atlanta.py --source-dir /path/to/benchmarks --target-dir /path/to/oss-fuzz/projects --projects curl-delta-04,libxml2-delta-03
        """,
    )

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
        action="store_true",
        help="Enable verbose logging",
    )

    parser.add_argument(
        "--force",
        action="store_true",
        help="Force conversion even if project already exists (default: skip existing)",
    )

    args = parser.parse_args()

    # Setup logging
    setup_logging(verbose=args.verbose)
    logger = get_logger(__name__)

    # Validate directories
    if not args.source_dir.exists():
        logger.error(f"Source directory does not exist: {args.source_dir}")
        sys.exit(1)

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
    logger.info(f"\n{dry_run_prefix}Conversion Summary:")
    logger.info(f"  Total projects: {len(results)}")
    logger.info(f"  Successful: {successful}")
    logger.info(f"  Skipped: {skipped}")
    logger.info(f"  Failed: {failed}")
    logger.info(f"  Total harnesses: {total_harnesses}")
    logger.info(f"  Total vulnerabilities: {total_vulns}")

    # Log errors and warnings
    for ctx in results:
        if ctx.errors:
            logger.error(f"  {ctx.project_name} errors: {ctx.errors}")
        if ctx.warnings:
            logger.warning(f"  {ctx.project_name} warnings: {ctx.warnings}")

    # Exit with appropriate code
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
