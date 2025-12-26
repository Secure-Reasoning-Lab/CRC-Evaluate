"""Main benchmark snapshot generator implementation."""

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Tuple

import yaml

from crsbench.utils.logger import get_logger
from crsbench.validation.format_validator import validate_benchmark
from crsbench.validation.schemas import BenchmarkConfig

logger = get_logger(__name__)


@dataclass
class POVData:
    """POV ground truth data from benchmark."""

    blob: bytes
    log: str
    sanitizer: str
    error_token: Optional[str]


@dataclass
class BenchmarkData:
    """Complete benchmark ground truth data.

    Attributes:
        meta: Parsed benchmark configuration from meta.yaml
        povs: POV data indexed by (harness_name, vuln_keyword, pov_id)
        patches: Patch content indexed by (harness_name, vuln_keyword, patch_id)
    """

    meta: BenchmarkConfig
    povs: Dict[Tuple[str, str, str], POVData]
    patches: Dict[Tuple[str, str, str], str]


def load_benchmark_ground_truth(benchmark_path: Path) -> BenchmarkData:
    """Load all ground truth data from benchmark.

    Args:
        benchmark_path: Path to benchmark directory containing .aixcc/

    Returns:
        BenchmarkData with loaded meta.yaml, POVs, and patches

    Raises:
        FileNotFoundError: If benchmark or .aixcc directory doesn't exist
        ValueError: If meta.yaml is invalid
    """
    if not benchmark_path.exists():
        raise FileNotFoundError(f"Benchmark directory not found: {benchmark_path}")

    aixcc_dir = benchmark_path / ".aixcc"
    if not aixcc_dir.exists():
        raise FileNotFoundError(f".aixcc directory not found in: {benchmark_path}")

    logger.info(f"Loading benchmark ground truth from {benchmark_path}")

    # Parse and validate meta.yaml
    meta_path = aixcc_dir / "meta.yaml"
    if not meta_path.exists():
        raise FileNotFoundError(f"meta.yaml not found: {meta_path}")

    validation_result = validate_benchmark(str(benchmark_path))
    if not validation_result.is_valid:
        errors = "\n".join(issue.message for issue in validation_result.issues)
        raise ValueError(f"Invalid meta.yaml:\n{errors}")

    with meta_path.open("r") as f:
        meta_dict = yaml.safe_load(f)
    meta = BenchmarkConfig(**meta_dict)

    # Load POVs and patches for each harness/vulnerability
    povs: Dict[Tuple[str, str, str], POVData] = {}
    patches: Dict[Tuple[str, str, str], str] = {}

    for harness in meta.harness_files:
        harness_dir = aixcc_dir / harness.name

        if not harness_dir.exists():
            logger.warning(f"Harness directory not found: {harness_dir}")
            continue

        for vuln in harness.vulns or []:
            vuln_dir = harness_dir / vuln.vuln_keyword

            if not vuln_dir.exists():
                logger.warning(
                    f"Vulnerability directory not found: {vuln_dir} "
                    f"(harness: {harness.name}, vuln: {vuln.vuln_keyword})"
                )
                continue

            # Load POVs
            blobs_dir = vuln_dir / "blobs"
            logs_dir = vuln_dir / "logs"

            for pov_spec in vuln.povs:
                pov_id = pov_spec.id

                # Load POV blob
                blob_path = blobs_dir / f"{pov_id}.blob"
                if not blob_path.exists():
                    logger.warning(f"POV blob not found: {blob_path}")
                    continue

                blob_data = blob_path.read_bytes()

                # Load POV log (optional but expected)
                log_path = logs_dir / f"{pov_id}.log"
                log_data = ""
                if log_path.exists():
                    log_data = log_path.read_text()
                else:
                    logger.warning(f"POV log not found: {log_path}")

                # Store POV data
                pov_key = (harness.name, vuln.vuln_keyword, pov_id)
                povs[pov_key] = POVData(
                    blob=blob_data,
                    log=log_data,
                    sanitizer=pov_spec.sanitizer,
                    error_token=pov_spec.error_token,
                )

            # Load patches
            patches_dir = vuln_dir / "patches"
            if patches_dir.exists():
                for patch_file in patches_dir.glob("*.diff"):
                    patch_id = patch_file.stem
                    patch_content = patch_file.read_text()

                    patch_key = (harness.name, vuln.vuln_keyword, patch_id)
                    patches[patch_key] = patch_content

    logger.info(
        f"Loaded {len(povs)} POVs and {len(patches)} patches from {benchmark_path.name}"
    )

    return BenchmarkData(meta=meta, povs=povs, patches=patches)


class BenchmarkSnapshotGenerator:
    """Generate realistic trial snapshots from benchmark ground truth.

    This class orchestrates snapshot generation by:
    1. Loading benchmark ground truth (POVs, patches from .aixcc/)
    2. Creating realistic discovery timeline based on difficulty
    3. Generating snapshots at configured intervals

    Attributes:
        benchmark_path: Path to benchmark directory
        output_dir: Output directory for generated snapshots
        trial_duration: Trial duration in seconds
        snapshot_period: Snapshot interval in seconds
    """

    def __init__(
        self,
        benchmark_path: Path,
        output_dir: Path,
        trial_duration: int = 7200,
        snapshot_period: int = 900,
        harness: Optional[str] = None,
    ):
        """Initialize benchmark snapshot generator.

        Args:
            benchmark_path: Path to benchmark directory containing .aixcc/
            output_dir: Output directory for snapshots
            trial_duration: Trial duration in seconds (default: 7200 = 2 hours)
            snapshot_period: Snapshot interval in seconds (default: 900 = 15 min)
            harness: Target harness name (required)

        Raises:
            ValueError: If trial_duration, snapshot_period invalid, or harness not found
            FileNotFoundError: If benchmark_path doesn't exist
        """
        if trial_duration <= 0:
            raise ValueError(f"trial_duration must be > 0, got {trial_duration}")
        if snapshot_period <= 0:
            raise ValueError(f"snapshot_period must be > 0, got {snapshot_period}")
        if not benchmark_path.exists():
            raise FileNotFoundError(f"Benchmark not found: {benchmark_path}")
        if harness is None:
            raise ValueError("harness parameter is required")

        self.benchmark_path = benchmark_path
        self.output_dir = output_dir
        self.trial_duration = trial_duration
        self.snapshot_period = snapshot_period

        # Load ground truth
        self.benchmark_data = load_benchmark_ground_truth(benchmark_path)

        # Validate and store harness
        self.harness = self._validate_harness(harness)

        logger.info(
            f"BenchmarkSnapshotGenerator initialized: "
            f"benchmark={benchmark_path.name}, "
            f"harness={self.harness}, "
            f"duration={trial_duration}s, "
            f"period={snapshot_period}s"
        )

    def _validate_harness(self, harness: str) -> str:
        """Validate harness name exists and has POVs.

        Args:
            harness: Harness name to validate

        Returns:
            Validated harness name

        Raises:
            ValueError: If harness not found or has no POVs
        """
        harness_povs = [k for k in self.benchmark_data.povs if k[0] == harness]
        if not harness_povs:
            raise ValueError(f"Harness '{harness}' not found or has no POVs")
        return harness

    def generate_trial_snapshots(
        self,
        mode: str = "bug-finding",
        difficulty_level: int = 1,
        fault_injection_rate: float = 0.0,
    ) -> Path:
        """Generate complete trial snapshots.

        Args:
            mode: Generation mode ('bug-finding' or 'patch-generation')
            difficulty_level: Difficulty level 1-5 (controls discovery timing)
            fault_injection_rate: Rate of invalid data injection (0.0-1.0)

        Returns:
            Path to output directory containing generated snapshots

        Raises:
            ValueError: If mode or difficulty_level invalid
        """
        if mode not in ("bug-finding", "patch-generation"):
            raise ValueError(
                f"Invalid mode: {mode}. Must be 'bug-finding' or 'patch-generation'"
            )
        if not 1 <= difficulty_level <= 5:
            raise ValueError(f"difficulty_level must be 1-5, got {difficulty_level}")
        if not 0.0 <= fault_injection_rate <= 1.0:
            raise ValueError(
                f"fault_injection_rate must be in [0.0, 1.0], got {fault_injection_rate}"
            )

        logger.info(
            f"Generating trial snapshots: mode={mode}, difficulty={difficulty_level}, "
            f"fault_rate={fault_injection_rate}"
        )

        # Import here to avoid circular imports
        from crsbench.bench_snapgen.builder import SnapshotBuilder
        from crsbench.bench_snapgen.fault_injection import (
            FaultInjector,
            inject_faults_into_timeline,
        )
        from crsbench.bench_snapgen.timeline import create_discovery_timeline

        # Create output directory
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # 1. Create discovery timeline from ground truth
        timeline = create_discovery_timeline(
            benchmark_data=self.benchmark_data,
            difficulty_level=difficulty_level,
            max_time=self.trial_duration,
            mode=mode,
            harness=self.harness,
            snapshot_period=self.snapshot_period,
        )

        # 2. Inject faults if requested
        if fault_injection_rate > 0.0:
            fault_injector = FaultInjector(fault_rate=fault_injection_rate)
            inject_faults_into_timeline(timeline, fault_injector, self.trial_duration)

        # 3. Generate snapshots at intervals
        builder = SnapshotBuilder(self.output_dir)
        builder.snapshot_period = self.snapshot_period

        num_snapshots = int(self.trial_duration / self.snapshot_period)
        logger.info(f"Generating {num_snapshots} snapshots...")

        for cycle in range(1, num_snapshots + 1):
            elapsed_time = cycle * self.snapshot_period

            builder.build_snapshot(
                cycle=cycle,
                elapsed_time=elapsed_time,
                timeline=timeline,
                benchmark_name=self.benchmark_path.name,
                crs_name="bench-snapgen",
            )

        logger.info(f"Generated {num_snapshots} snapshots in {self.output_dir}")

        return self.output_dir


def main():
    """CLI entry point for benchmark snapshot generator."""
    parser = argparse.ArgumentParser(
        description="Generate realistic trial snapshots from CRSBench benchmark ground truth",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Basic usage - bug-finding mode
  python -m crsbench.bench_snapgen.generator \\
      --benchmark benchmarks/afc-curl-delta-01 \\
      --output /tmp/simulated-trial

  # Patch generation mode with medium difficulty
  python -m crsbench.bench_snapgen.generator \\
      --benchmark benchmarks/atlanta-activemq-delta-01 \\
      --output /tmp/patch-trial \\
      --mode patch-generation \\
      --difficulty 3

  # With fault injection (10% invalid data)
  python -m crsbench.bench_snapgen.generator \\
      --benchmark benchmarks/afc-curl-delta-01 \\
      --output /tmp/trial-with-faults \\
      --fault-injection-rate 0.10

  # Custom timing
  python -m crsbench.bench_snapgen.generator \\
      --benchmark benchmarks/afc-curl-delta-01 \\
      --output /tmp/custom-trial \\
      --duration 3600 \\
      --snapshot-period 600
        """,
    )

    # Required arguments
    parser.add_argument(
        "--benchmark",
        type=Path,
        required=True,
        help="Path to benchmark directory containing .aixcc/",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Output directory for generated snapshots",
    )

    # Optional timing arguments
    parser.add_argument(
        "--duration",
        type=int,
        default=7200,
        help="Trial duration in seconds (default: 7200 = 2 hours)",
    )
    parser.add_argument(
        "--snapshot-period",
        type=int,
        default=900,
        help="Snapshot interval in seconds (default: 900 = 15 minutes)",
    )

    # Mode and difficulty
    parser.add_argument(
        "--mode",
        choices=["bug-finding", "patch-generation"],
        default="bug-finding",
        help="Generation mode (default: bug-finding)",
    )
    parser.add_argument(
        "--difficulty",
        type=int,
        choices=[1, 2, 3, 4, 5],
        default=1,
        help="Difficulty level 1-5 (default: 1)",
    )

    # Harness selection
    parser.add_argument(
        "--harness",
        type=str,
        required=True,
        help="Target harness name",
    )

    # Fault injection
    parser.add_argument(
        "--fault-injection-rate",
        type=float,
        default=0.0,
        help="Rate of invalid data injection 0.0-1.0 (default: 0.0)",
    )

    # Multiple trials
    parser.add_argument(
        "--trials",
        type=int,
        default=1,
        help="Generate N independent trials (default: 1)",
    )

    args = parser.parse_args()

    # Validate arguments
    if not args.benchmark.exists():
        logger.error(f"Benchmark directory not found: {args.benchmark}")
        sys.exit(1)

    if args.duration <= 0:
        logger.error(f"Duration must be > 0, got {args.duration}")
        sys.exit(1)

    if args.snapshot_period <= 0:
        logger.error(f"Snapshot period must be > 0, got {args.snapshot_period}")
        sys.exit(1)

    if not 0.0 <= args.fault_injection_rate <= 1.0:
        logger.error(
            f"Fault injection rate must be in [0.0, 1.0], got {args.fault_injection_rate}"
        )
        sys.exit(1)

    if args.trials < 1:
        logger.error(f"Trials must be >= 1, got {args.trials}")
        sys.exit(1)

    # Generate trials
    logger.info(f"Starting snapshot generation: {args.trials} trial(s)")

    for trial_num in range(1, args.trials + 1):
        if args.trials > 1:
            trial_output_dir = args.output / f"trial-{trial_num:03d}"
        else:
            trial_output_dir = args.output

        logger.info(f"\n{'=' * 60}")
        logger.info(f"Generating trial {trial_num}/{args.trials}")
        logger.info(f"{'=' * 60}\n")

        try:
            generator = BenchmarkSnapshotGenerator(
                benchmark_path=args.benchmark,
                output_dir=trial_output_dir,
                trial_duration=args.duration,
                snapshot_period=args.snapshot_period,
                harness=args.harness,
            )

            output_dir = generator.generate_trial_snapshots(
                mode=args.mode,
                difficulty_level=args.difficulty,
                fault_injection_rate=args.fault_injection_rate,
            )

            logger.info(f"Trial {trial_num} completed: {output_dir}")

        except Exception as e:
            logger.error(f"Failed to generate trial {trial_num}: {e}", exc_info=True)
            sys.exit(1)

    logger.info(f"\n{'=' * 60}")
    logger.info("All trials completed successfully!")
    logger.info(f"{'=' * 60}")
    logger.info(f"Output directory: {args.output}")


if __name__ == "__main__":
    main()
