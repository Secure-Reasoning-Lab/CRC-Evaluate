#!/usr/bin/env python3
"""
Migration script to convert Team-Atlanta benchmark format to CRSBench RFC format.

This script migrates benchmark projects from Team-Atlanta's format to the
standardized CRSBench RFC format, converting directory structures, configuration
files, and organizing ground truth data according to the RFC specification.
"""

import argparse
import csv
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import yaml

from crsbench.migration.config_converter import ConfigConverter
from crsbench.migration.file_migrator import FileMigrator
from crsbench.migration.vuln_metadata_generator import VulnMetadataGenerator


@dataclass
class VulnInfo:
    """Information about a vulnerability for CSV reporting."""
    project_name: str
    source: str
    repo_url: str
    mode: str
    branch: str
    language: str
    harness_name: str
    vuln_id: str


@dataclass
class MigrationContext:
    """Context object for tracking migration state."""

    source_dir: Path
    target_dir: Path
    dry_run: bool
    project_name: str
    successful: bool = True
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    num_vulns: int = 0
    num_harnesses: int = 0
    vulns: List[VulnInfo] = field(default_factory=list)
    repo_url: str = ""  # Repository URL from project.yaml


class AtlantaToRFCMigrator:
    """Main migration orchestrator."""

    def __init__(self, source_dir: Path, target_dir: Path, dry_run: bool = False):
        """
        Initialize the migrator.

        Args:
            source_dir: Team-Atlanta projects directory
            target_dir: CRSBench benchmarks directory
            dry_run: If True, perform validation without actual file operations
        """
        self.source_dir = source_dir
        self.target_dir = target_dir
        self.dry_run = dry_run
        self.logger = logging.getLogger(__name__)

        # Initialize converters
        self.config_converter = ConfigConverter()
        self.vuln_generator = VulnMetadataGenerator()
        self.file_migrator = FileMigrator(dry_run=dry_run)

    def discover_projects(self, specific_projects: Optional[List[str]] = None) -> List[Path]:
        """
        Discover all Team-Atlanta projects to migrate.

        Args:
            specific_projects: Optional list of specific project names to migrate

        Returns:
            List of project directory paths
        """
        # Team-Atlanta structure: projects/aixcc/{language}/{project-name}
        aixcc_dir = self.source_dir / "aixcc"
        if not aixcc_dir.exists():
            self.logger.error(f"aixcc directory not found: {aixcc_dir}")
            return []

        # Only process C and Java/JVM projects
        allowed_languages = ['c', 'java', 'jvm']

        projects = []
        for lang_dir in aixcc_dir.iterdir():
            if not lang_dir.is_dir():
                continue

            # Filter by language
            if lang_dir.name.lower() not in allowed_languages:
                self.logger.debug(f"Skipping language directory: {lang_dir.name}")
                continue

            for project_dir in lang_dir.iterdir():
                if not project_dir.is_dir():
                    continue

                # Check if this project should be migrated
                if specific_projects and project_dir.name not in specific_projects:
                    continue

                # Verify it's a valid project (has .aixcc directory)
                if (project_dir / ".aixcc").exists():
                    projects.append(project_dir)

        return projects

    def migrate_project(self, project_dir: Path) -> MigrationContext:
        """
        Migrate a single project from Team-Atlanta format to RFC format.

        Args:
            project_dir: Path to the Team-Atlanta project directory

        Returns:
            MigrationContext with migration results
        """
        project_name = project_dir.name
        self.logger.info(f"{'[DRY RUN] ' if self.dry_run else ''}Processing project: {project_name}")

        # Extract language from project path (e.g., .../aixcc/c/project-name -> "C")
        language = self._extract_language(project_dir)

        ctx = MigrationContext(
            source_dir=project_dir,
            target_dir=self.target_dir / project_name,
            dry_run=self.dry_run,
            project_name=project_name
        )
        ctx.language = language

        try:
            # Step 1: Validate source structure
            self._validate_source(ctx)

            # Step 1.5: Read project.yaml to get repo URL
            self._read_project_yaml(ctx)

            # Step 2: Convert config.yaml to meta.yaml
            self._convert_config(ctx)

            # Step 3: Migrate root files (build.sh, Dockerfile, etc.)
            self._migrate_root_files(ctx)

            # Step 4: Process each vulnerability and harness
            self._migrate_vulnerabilities(ctx)

            # Step 5: Post-migration validation
            if not self.dry_run:
                self._validate_target(ctx)

            if ctx.successful:
                self.logger.info(f"Successfully migrated {project_name}")
            else:
                self.logger.error(f"Migration failed for {project_name}: {ctx.errors}")

        except Exception as e:
            ctx.successful = False
            ctx.errors.append(f"Unexpected error: {str(e)}")
            self.logger.exception(f"Error migrating {project_name}")

        return ctx

    def _extract_language(self, project_dir: Path) -> str:
        """
        Extract programming language from project path.

        Args:
            project_dir: Project directory path

        Returns:
            Language name (e.g., "C", "Java", "Rust")
        """
        # Path structure: .../aixcc/{language}/{project-name}
        try:
            parts = project_dir.parts
            if 'aixcc' in parts:
                lang_index = parts.index('aixcc') + 1
                if lang_index < len(parts):
                    lang = parts[lang_index]
                    # Capitalize language name
                    return lang.upper()
        except (ValueError, IndexError):
            pass

        return "Unknown"

    def _validate_source(self, ctx: MigrationContext) -> None:
        """Validate that the source project has required files."""
        aixcc_dir = ctx.source_dir / ".aixcc"
        config_file = aixcc_dir / "config.yaml"

        if not config_file.exists():
            ctx.successful = False
            ctx.errors.append(f"Missing required config.yaml in {aixcc_dir}")
            raise FileNotFoundError(f"config.yaml not found: {config_file}")

        # Check for build.sh (required)
        if not (ctx.source_dir / "build.sh").exists():
            ctx.warnings.append("Missing build.sh (will create placeholder)")

        # Check for Dockerfile (optional but recommended)
        if not (ctx.source_dir / "Dockerfile").exists():
            ctx.warnings.append("Missing Dockerfile")

    def _read_project_yaml(self, ctx: MigrationContext) -> None:
        """Read project.yaml to extract repository URL."""
        project_yaml = ctx.source_dir / "project.yaml"

        if not project_yaml.exists():
            ctx.warnings.append("Missing project.yaml, repo URL will be empty")
            ctx.repo_url = ""
            return

        try:
            with open(project_yaml, 'r') as f:
                project_data = yaml.safe_load(f)

            # Extract main_repo field
            ctx.repo_url = project_data.get('main_repo', '')

            if not ctx.repo_url:
                ctx.warnings.append("project.yaml missing 'main_repo' field")

        except Exception as e:
            ctx.warnings.append(f"Error reading project.yaml: {str(e)}")
            ctx.repo_url = ""

    def _convert_config(self, ctx: MigrationContext) -> None:
        """Convert config.yaml to meta.yaml format."""
        self.logger.info(f"  Converting config.yaml to meta.yaml")

        source_config = ctx.source_dir / ".aixcc" / "config.yaml"
        target_meta = ctx.target_dir / ".aixcc" / "meta.yaml"

        # Convert configuration - returns MetaConfig Pydantic model
        meta_config, harness_info = self.config_converter.convert(source_config)

        # Store harness info in context for later use
        ctx.num_harnesses = len(harness_info)
        ctx.num_vulns = sum(len(h.get('cpvs', [])) for h in harness_info.values())

        # Determine mode (delta or full)
        if meta_config.delta_mode:
            ctx.mode = 'delta'
        elif meta_config.full_mode:
            ctx.mode = 'full'
        else:
            ctx.mode = 'unknown'

        # Write meta.yaml using Pydantic model
        if not self.dry_run:
            meta_config.to_yaml(target_meta)
            self.logger.debug(f"Migrated from Team-Atlanta format to RFC format: {target_meta}")
        else:
            self.logger.info(f"    [DRY RUN] Would write: {target_meta}")

        # Store harness info for vulnerability migration
        ctx.harness_info = harness_info

    def _migrate_root_files(self, ctx: MigrationContext) -> None:
        """Copy root-level project files and directories, excluding .aixcc."""
        self.logger.info(f"  Migrating root files")

        # Copy all files and directories in project root, excluding .aixcc
        for item in ctx.source_dir.iterdir():
            # Skip .aixcc directory - it will be migrated separately
            if item.name == ".aixcc":
                continue

            target = ctx.target_dir / item.name

            if item.is_file():
                self.file_migrator.copy_file(item, target, ctx)
            elif item.is_dir():
                self.file_migrator.copy_directory(item, target, ctx)

    def _migrate_vulnerabilities(self, ctx: MigrationContext) -> None:
        """Migrate vulnerability data for each harness."""
        self.logger.info(f"  Migrating vulnerabilities")

        harness_info = getattr(ctx, 'harness_info', {})

        for harness_name, harness_data in harness_info.items():
            cpvs = harness_data.get('cpvs', [])

            for cpv in cpvs:
                cpv_name = cpv['name']
                self.logger.info(f"    Processing {harness_name}/{cpv_name}")

                # Create vulnerability directory structure
                vuln_dir = ctx.target_dir / ".aixcc" / harness_name / cpv_name

                # Generate vulnerability metadata YAML
                self._generate_vuln_metadata(ctx, harness_name, cpv, vuln_dir)

                # Migrate patch files
                self._migrate_patches(ctx, harness_name, cpv_name, vuln_dir)

                # Migrate POV blobs
                self._migrate_pov_blobs(ctx, harness_name, cpv_name, vuln_dir)

                # Migrate crash logs
                self._migrate_crash_logs(ctx, harness_name, cpv_name, vuln_dir)

                # Record vulnerability info for CSV report
                vuln_info = VulnInfo(
                    project_name=ctx.project_name,
                    source="Team-Atlanta",
                    repo_url=ctx.repo_url,  # From project.yaml
                    mode=getattr(ctx, 'mode', 'unknown'),
                    branch="master",  # Default branch
                    language=getattr(ctx, 'language', 'Unknown'),
                    harness_name=harness_name,
                    vuln_id=cpv_name
                )
                ctx.vulns.append(vuln_info)

    def _generate_vuln_metadata(self, ctx: MigrationContext, harness_name: str,
                               cpv: dict, vuln_dir: Path) -> None:
        """Generate vulnerability metadata YAML file."""
        cpv_name = cpv['name']
        vuln_yaml = vuln_dir / "vuln.yaml"

        # Try to find original vulnerability file
        # Path: {source_dir}/.aixcc/vulns/{harness_name}/{cpv_name}.yaml
        source_vuln_file = ctx.source_dir / ".aixcc" / "vulns" / harness_name / f"{cpv_name}.yaml"

        # Try to find crash log file for parsing function name
        # Path: {source_dir}/.aixcc/crash_logs/{harness_name}/{cpv_name}.log
        crash_log_path = ctx.source_dir / ".aixcc" / "crash_logs" / harness_name / f"{cpv_name}.log"

        # Get language from context (defaults to "C")
        language = getattr(ctx, 'language', 'C')

        metadata = self.vuln_generator.generate(cpv, harness_name, source_vuln_file, crash_log_path, language)

        if not self.dry_run:
            metadata.to_yaml(vuln_yaml)
            if source_vuln_file.exists():
                self.logger.debug(f"Migrated vulnerability metadata from {source_vuln_file} to {vuln_yaml}")
            else:
                self.logger.debug(f"Generated MOCK vulnerability metadata: {vuln_yaml}")
        else:
            self.logger.info(f"      [DRY RUN] Would write: {vuln_yaml}")

    def _migrate_patches(self, ctx: MigrationContext, harness_name: str,
                        cpv_name: str, vuln_dir: Path) -> None:
        """Migrate patch files."""
        source_patch = ctx.source_dir / ".aixcc" / "patches" / harness_name / f"{cpv_name}.diff"

        if not source_patch.exists():
            ctx.warnings.append(f"Missing patch for {harness_name}/{cpv_name}")
            return

        target_patch = vuln_dir / "patches" / "patch_0.patch"
        self.file_migrator.copy_file(source_patch, target_patch, ctx)

    def _migrate_pov_blobs(self, ctx: MigrationContext, harness_name: str,
                          cpv_name: str, vuln_dir: Path) -> None:
        """Migrate POV blob files."""
        # Primary POV blob (from povs directory)
        source_pov = ctx.source_dir / ".aixcc" / "povs" / harness_name / cpv_name

        blob_index = 0
        if source_pov.exists() and source_pov.is_file():
            target_blob = vuln_dir / "blobs" / f"pov_{blob_index}.blob"
            self.file_migrator.copy_file(source_pov, target_blob, ctx)
            blob_index += 1
        else:
            ctx.warnings.append(f"Missing primary POV blob for {harness_name}/{cpv_name}")

        # Variant POV blobs (from variants directory)
        variants_dir = ctx.source_dir / ".aixcc" / "variants" / cpv_name / harness_name
        if variants_dir.exists():
            for variant_file in sorted(variants_dir.glob("*.blob")):
                target_blob = vuln_dir / "blobs" / f"pov_{blob_index}.blob"
                self.file_migrator.copy_file(variant_file, target_blob, ctx)
                blob_index += 1

    def _migrate_crash_logs(self, ctx: MigrationContext, harness_name: str,
                           cpv_name: str, vuln_dir: Path) -> None:
        """Migrate crash log files."""
        # Primary crash log
        source_log = ctx.source_dir / ".aixcc" / "crash_logs" / harness_name / f"{cpv_name}.log"

        log_index = 0
        if source_log.exists():
            target_log = vuln_dir / "logs" / f"pov_{log_index}.log"
            self.file_migrator.copy_file(source_log, target_log, ctx)
            log_index += 1
        else:
            ctx.warnings.append(f"Missing crash log for {harness_name}/{cpv_name}")

        # Variant crash logs
        variants_dir = ctx.source_dir / ".aixcc" / "variants" / cpv_name / harness_name
        if variants_dir.exists():
            for variant_file in sorted(variants_dir.glob("*.log")):
                target_log = vuln_dir / "logs" / f"pov_{log_index}.log"
                self.file_migrator.copy_file(variant_file, target_log, ctx)
                log_index += 1

    def _validate_target(self, ctx: MigrationContext) -> None:
        """Validate the migrated project structure."""
        # Check that meta.yaml was created
        meta_yaml = ctx.target_dir / ".aixcc" / "meta.yaml"
        if not meta_yaml.exists():
            ctx.successful = False
            ctx.errors.append("meta.yaml was not created")

    def migrate_all(self, specific_projects: Optional[List[str]] = None) -> List[MigrationContext]:
        """
        Migrate all discovered projects.

        Args:
            specific_projects: Optional list of specific project names to migrate

        Returns:
            List of MigrationContext objects with results
        """
        projects = self.discover_projects(specific_projects)

        if not projects:
            self.logger.warning("No projects found to migrate")
            return []

        self.logger.info(f"Found {len(projects)} projects to migrate")

        results = []
        for project_dir in projects:
            ctx = self.migrate_project(project_dir)
            results.append(ctx)

        return results


def write_csv_report(results: List[MigrationContext], output_file: Path) -> None:
    """Write migration results to CSV file."""
    with open(output_file, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow([
            'Project Name',
            'Source',
            'Vuln. Repo URL',
            'Mode',
            'Branch',
            'Language',
            'Harness Name',
            'Vuln. ID'
        ])

        for ctx in results:
            if not ctx.successful:
                # For failed migrations, write a single row with error info
                writer.writerow([
                    ctx.project_name,
                    'Team-Atlanta',
                    'N/A (Migration Failed)',
                    'N/A',
                    'N/A',
                    getattr(ctx, 'language', 'Unknown'),
                    'N/A',
                    'N/A'
                ])
            else:
                # For successful migrations, write one row per vulnerability
                for vuln in ctx.vulns:
                    writer.writerow([
                        vuln.project_name,
                        vuln.source,
                        vuln.repo_url,
                        vuln.mode,
                        vuln.branch,
                        vuln.language,
                        vuln.harness_name,
                        vuln.vuln_id
                    ])


class ColoredFormatter(logging.Formatter):
    """Custom formatter with colors for different log levels."""

    # ANSI color codes
    COLORS = {
        'DEBUG': '\033[36m',      # Cyan
        'INFO': '\033[32m',       # Green
        'WARNING': '\033[33m',    # Yellow
        'ERROR': '\033[31m',      # Red
        'CRITICAL': '\033[35m',   # Magenta
    }
    RESET = '\033[0m'
    BOLD = '\033[1m'
    DIM = '\033[2m'

    def format(self, record):
        # Add color to level name
        levelname = record.levelname
        if levelname in self.COLORS:
            record.levelname = f"{self.COLORS[levelname]}{self.BOLD}{levelname}{self.RESET}"

        # Color the timestamp
        log_message = super().format(record)

        # Color patterns for special markers
        log_message = log_message.replace('[DRY RUN]', f'{self.BOLD}\033[35m[DRY RUN]{self.RESET}')
        log_message = log_message.replace('Successfully migrated', f'{self.BOLD}\033[32m✓ Successfully migrated{self.RESET}')
        log_message = log_message.replace('Failed', f'{self.BOLD}\033[31m✗ Failed{self.RESET}')
        log_message = log_message.replace('Processing project:', f'{self.BOLD}\033[34m→{self.RESET} Processing project:')
        log_message = log_message.replace('Would write:', f'{self.DIM}Would write:{self.RESET}')
        log_message = log_message.replace('Would copy:', f'{self.DIM}Would copy:{self.RESET}')

        return log_message


def setup_logging(verbose: bool = False) -> None:
    """Configure logging for the migration script with colors."""
    level = logging.DEBUG if verbose else logging.INFO

    # Create handler with colored formatter
    handler = logging.StreamHandler()
    handler.setFormatter(ColoredFormatter(
        fmt='[%(asctime)s] [%(levelname)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    ))

    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    root_logger.handlers = []  # Remove any existing handlers
    root_logger.addHandler(handler)


def main():
    """Main entry point for the migration script."""
    parser = argparse.ArgumentParser(
        description='Migrate Team-Atlanta benchmarks to CRSBench RFC format',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Migrate all projects
  python atlanta_to_rfc.py --source-dir /path/to/oss-fuzz/projects --target-dir /path/to/CRSBench/benchmarks

  # Dry run to validate
  python atlanta_to_rfc.py --source-dir /path/to/oss-fuzz/projects --target-dir /path/to/CRSBench/benchmarks --dry-run

  # Migrate specific projects
  python atlanta_to_rfc.py --source-dir /path/to/oss-fuzz/projects --target-dir /path/to/CRSBench/benchmarks --projects curl-delta-04,libxml2-delta-03
        """
    )

    parser.add_argument(
        '--source-dir',
        type=Path,
        required=True,
        help='Team-Atlanta projects directory (contains aixcc/)'
    )

    parser.add_argument(
        '--target-dir',
        type=Path,
        required=True,
        help='CRSBench benchmarks directory (target for conversion)'
    )

    parser.add_argument(
        '--projects',
        type=str,
        help='Comma-separated list of specific projects to migrate'
    )

    parser.add_argument(
        '--output-csv',
        type=Path,
        default='migration_report.csv',
        help='Output CSV file for migration report (default: migration_report.csv)'
    )

    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Perform validation without actual file operations'
    )

    parser.add_argument(
        '--verbose',
        action='store_true',
        help='Enable verbose logging'
    )

    args = parser.parse_args()

    # Setup logging
    setup_logging(args.verbose)
    logger = logging.getLogger(__name__)

    # Validate directories
    if not args.source_dir.exists():
        logger.error(f"Source directory does not exist: {args.source_dir}")
        sys.exit(1)

    if not args.dry_run:
        args.target_dir.mkdir(parents=True, exist_ok=True)

    # Parse projects list
    specific_projects = None
    if args.projects:
        specific_projects = [p.strip() for p in args.projects.split(',')]

    # Run migration
    migrator = AtlantaToRFCMigrator(
        source_dir=args.source_dir,
        target_dir=args.target_dir,
        dry_run=args.dry_run
    )

    results = migrator.migrate_all(specific_projects)

    # Print summary
    successful = sum(1 for r in results if r.successful)
    failed = len(results) - successful

    logger.info("=" * 60)
    logger.info(f"Migration {'DRY RUN ' if args.dry_run else ''}Summary:")
    logger.info(f"  Total projects: {len(results)}")
    logger.info(f"  Successful: {successful}")
    logger.info(f"  Failed: {failed}")
    logger.info("=" * 60)

    # Write CSV report
    if results:
        write_csv_report(results, args.output_csv)
        logger.info(f"CSV report written to: {args.output_csv}")

    # Exit with appropriate code
    sys.exit(0 if failed == 0 else 1)


if __name__ == '__main__':
    main()
