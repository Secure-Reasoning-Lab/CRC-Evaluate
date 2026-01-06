#!/usr/bin/env python3
"""Run verify, patch-verify, coverage for all benchmarks and report status.

This script runs crsbench commands sequentially for all benchmark projects
and generates a status table showing pass/fail and timing for each command.

Usage:
    python benchmark_status.py [--benchmarks-dir BENCHMARKS_DIR] [--output OUTPUT]
    python benchmark_status.py --filter "mock-*"
    python benchmark_status.py --projects sanity-mock-c-delta-01,sanity-mock-java-delta-01

Examples:
    python benchmark_status.py
    python benchmark_status.py --filter "sanity-*"
    python benchmark_status.py --output status.json
"""

import argparse
import fnmatch
import json
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class CommandResult:
    """Result of a single command execution."""

    status: str  # "pass", "fail", "skip"
    time_seconds: float
    error: str = ""


@dataclass
class BenchmarkResult:
    """Results for a single benchmark."""

    name: str
    format_validate: CommandResult = field(default_factory=lambda: CommandResult("skip", 0.0))
    verify: CommandResult = field(default_factory=lambda: CommandResult("skip", 0.0))
    patch_verify: CommandResult = field(
        default_factory=lambda: CommandResult("skip", 0.0)
    )
    coverage: CommandResult = field(default_factory=lambda: CommandResult("skip", 0.0))
    # Future: inc_build: CommandResult = field(default_factory=lambda: CommandResult("skip", 0.0))

    @property
    def total_status(self) -> str:
        """Return 'pass' if all commands pass, 'fail' otherwise."""
        statuses = [self.format_validate.status, self.verify.status, self.patch_verify.status, self.coverage.status]
        if all(s == "pass" for s in statuses):
            return "pass"
        if any(s == "fail" for s in statuses):
            return "fail"
        return "skip"

    @property
    def total_time(self) -> float:
        """Total time for all commands."""
        return self.format_validate.time_seconds + self.verify.time_seconds + self.patch_verify.time_seconds + self.coverage.time_seconds


def discover_benchmarks(benchmarks_dir: Path, filter_pattern: str | None = None) -> list[Path]:
    """Discover all benchmark directories."""
    benchmarks = []
    for path in sorted(benchmarks_dir.iterdir()):
        if not path.is_dir():
            continue
        # Check for .aixcc directory (indicates valid benchmark)
        if not (path / ".aixcc").exists():
            continue
        if filter_pattern and not fnmatch.fnmatch(path.name, filter_pattern):
            continue
        benchmarks.append(path)
    return benchmarks


@dataclass
class CommandOutput:
    """Full output from a command execution."""

    success: bool
    time_seconds: float
    stdout: str
    stderr: str
    error: str = ""


def run_command(cmd: list[str], timeout: int = 600) -> CommandOutput:
    """Run a command and return full output."""
    start = time.time()
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        elapsed = time.time() - start
        return CommandOutput(
            success=result.returncode == 0,
            time_seconds=elapsed,
            stdout=result.stdout,
            stderr=result.stderr,
            error="" if result.returncode == 0 else (result.stderr[:500] if result.stderr else "Command failed"),
        )
    except subprocess.TimeoutExpired as e:
        elapsed = time.time() - start
        # Handle bytes or str stdout/stderr from TimeoutExpired
        stdout = ""
        stderr = ""
        if e.stdout:
            stdout = e.stdout.decode() if isinstance(e.stdout, bytes) else e.stdout
        if e.stderr:
            stderr = e.stderr.decode() if isinstance(e.stderr, bytes) else e.stderr
        return CommandOutput(
            success=False,
            time_seconds=elapsed,
            stdout=stdout,
            stderr=stderr,
            error=f"Timeout after {timeout}s",
        )
    except Exception as e:
        elapsed = time.time() - start
        return CommandOutput(
            success=False,
            time_seconds=elapsed,
            stdout="",
            stderr="",
            error=str(e),
        )


def get_script_dir() -> Path:
    """Get the directory containing this script."""
    return Path(__file__).parent


def save_log(log_dir: Path | None, name: str, output: CommandOutput, check_output: CommandOutput | None = None):
    """Save command output to log file."""
    if not log_dir:
        return
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"{name}.log"
    with open(log_file, "w") as f:
        f.write(f"=== {name} ===\n")
        f.write(f"Success: {output.success}\n")
        f.write(f"Time: {output.time_seconds:.1f}s\n")
        if output.error:
            f.write(f"Error: {output.error}\n")
        f.write("\n=== STDOUT ===\n")
        f.write(output.stdout)
        f.write("\n=== STDERR ===\n")
        f.write(output.stderr)
        if check_output:
            f.write("\n\n=== VALIDATION ===\n")
            f.write(f"Success: {check_output.success}\n")
            f.write("\n=== VALIDATION STDOUT ===\n")
            f.write(check_output.stdout)
            f.write("\n=== VALIDATION STDERR ===\n")
            f.write(check_output.stderr)


def run_format_validate(benchmark_path: Path, log_dir: Path | None = None) -> CommandResult:
    """Run format validation for a benchmark."""
    start = time.time()
    try:
        # Import validation module
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from crsbench.validation import validate_benchmark

        result = validate_benchmark(benchmark_path)
        elapsed = time.time() - start

        # Save log if directory provided
        if log_dir:
            log_dir.mkdir(parents=True, exist_ok=True)
            log_file = log_dir / "format-validate.log"
            with open(log_file, "w") as f:
                f.write(f"=== format-validate ===\n")
                f.write(f"Success: {result.is_valid}\n")
                f.write(f"Time: {elapsed:.3f}s\n")
                f.write(f"Errors: {result.error_count}\n")
                f.write(f"Warnings: {result.warning_count}\n")
                if result.errors:
                    f.write("\n=== ERRORS ===\n")
                    for error in result.errors:
                        f.write(f"  - {error.message}\n")
                        if error.field:
                            f.write(f"    Field: {error.field}\n")
                if result.warnings:
                    f.write("\n=== WARNINGS ===\n")
                    for warning in result.warnings:
                        f.write(f"  - {warning.message}\n")
                f.write(f"\n=== METADATA ===\n")
                for key, value in result.metadata.items():
                    f.write(f"  {key}: {value}\n")

        if result.is_valid:
            return CommandResult(status="pass", time_seconds=elapsed)
        else:
            error_msgs = "; ".join(e.message for e in result.errors[:3])
            return CommandResult(status="fail", time_seconds=elapsed, error=error_msgs)

    except Exception as e:
        elapsed = time.time() - start
        return CommandResult(status="fail", time_seconds=elapsed, error=str(e))


def run_verify(benchmark_path: Path, output_file: Path, log_dir: Path | None = None, *, force_rebuild: bool = True) -> CommandResult:
    """Run crsbench verify for a benchmark."""
    cmd = [
        "uv", "run", "crsbench", "verify",
        str(benchmark_path),
        "--output", str(output_file),
        "--format", "json",
    ]
    if force_rebuild:
        cmd.append("--force-rebuild")
    output = run_command(cmd)

    if not output.success:
        save_log(log_dir, "verify", output)
        return CommandResult(status="fail", time_seconds=output.time_seconds, error=output.error)

    # Validate results using check script
    check_cmd = [
        "python", str(get_script_dir() / "check_ci_results.py"),
        "verify", str(benchmark_path), str(output_file),
    ]
    check_output = run_command(check_cmd, timeout=60)

    save_log(log_dir, "verify", output, check_output)

    # Copy result JSON to log dir
    if log_dir and output_file.exists():
        shutil.copy(output_file, log_dir / "verify.json")

    return CommandResult(
        status="pass" if check_output.success else "fail",
        time_seconds=output.time_seconds,
        error=check_output.error if not check_output.success else "",
    )


def run_patch_verify(benchmark_path: Path, output_file: Path, log_dir: Path | None = None, *, force_rebuild: bool = True) -> CommandResult:
    """Run crsbench patch-verify for a benchmark."""
    cmd = [
        "uv", "run", "crsbench", "patch-verify",
        str(benchmark_path),
        "--output", str(output_file),
        "--format", "json",
    ]
    if force_rebuild:
        cmd.append("--force-rebuild")
    output = run_command(cmd)

    if not output.success:
        save_log(log_dir, "patch-verify", output)
        return CommandResult(status="fail", time_seconds=output.time_seconds, error=output.error)

    # Validate results using check script
    check_cmd = [
        "python", str(get_script_dir() / "check_ci_results.py"),
        "patch-verify", str(output_file),
    ]
    check_output = run_command(check_cmd, timeout=60)

    save_log(log_dir, "patch-verify", output, check_output)

    # Copy result JSON to log dir
    if log_dir and output_file.exists():
        shutil.copy(output_file, log_dir / "patch-verify.json")

    return CommandResult(
        status="pass" if check_output.success else "fail",
        time_seconds=output.time_seconds,
        error=check_output.error if not check_output.success else "",
    )


def run_coverage(benchmark_path: Path, output_file: Path, log_dir: Path | None = None, *, force_rebuild: bool = True) -> CommandResult:
    """Run crsbench coverage for a benchmark."""
    # Create temp corpus with seed input
    with tempfile.TemporaryDirectory() as corpus_dir:
        seed_file = Path(corpus_dir) / "seed_input"
        seed_file.write_bytes(b"\x00" * 64)

        cmd = [
            "uv", "run", "crsbench", "coverage",
            str(benchmark_path),
            "--corpus-dir", corpus_dir,
            "--output", str(output_file),
            "--format", "json",
        ]
        if force_rebuild:
            cmd.append("--force-rebuild")
        output = run_command(cmd)

        if not output.success:
            save_log(log_dir, "coverage", output)
            return CommandResult(status="fail", time_seconds=output.time_seconds, error=output.error)

        # Validate results using check script
        check_cmd = [
            "python", str(get_script_dir() / "check_ci_results.py"),
            "coverage", str(output_file),
        ]
        check_output = run_command(check_cmd, timeout=60)

        save_log(log_dir, "coverage", output, check_output)

        # Copy result JSON to log dir
        if log_dir and output_file.exists():
            shutil.copy(output_file, log_dir / "coverage.json")

        return CommandResult(
            status="pass" if check_output.success else "fail",
            time_seconds=output.time_seconds,
            error=check_output.error if not check_output.success else "",
        )


def format_time(seconds: float) -> str:
    """Format time in human-readable format."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = int(seconds // 60)
    secs = seconds % 60
    return f"{minutes}m{secs:.0f}s"


def format_status(status: str) -> str:
    """Format status with color codes for terminal."""
    if status == "pass":
        return "\033[92mPASS\033[0m"  # Green
    elif status == "fail":
        return "\033[91mFAIL\033[0m"  # Red
    return "\033[93mSKIP\033[0m"  # Yellow


def print_table(results: list[BenchmarkResult], use_color: bool = True):
    """Print results as a formatted table."""
    # Header
    headers = [
        "Benchmark",
        "Format", "F.Time",
        "Verify", "V.Time",
        "Patch", "P.Time",
        "Coverage", "C.Time",
        "Total", "Total Time",
    ]

    # Calculate column widths
    max_name = max(len(r.name) for r in results) if results else 10
    max_name = max(max_name, len("Benchmark"))

    # Print header
    header_fmt = f"{{:<{max_name}}}  {{:^6}} {{:>6}}  {{:^6}} {{:>7}}  {{:^6}} {{:>7}}  {{:^8}} {{:>7}}  {{:^6}} {{:>10}}"
    print(header_fmt.format(*headers))
    print("-" * (max_name + 85))

    # Print rows
    for r in results:
        if use_color:
            row = [
                r.name,
                format_status(r.format_validate.status),
                format_time(r.format_validate.time_seconds),
                format_status(r.verify.status),
                format_time(r.verify.time_seconds),
                format_status(r.patch_verify.status),
                format_time(r.patch_verify.time_seconds),
                format_status(r.coverage.status),
                format_time(r.coverage.time_seconds),
                format_status(r.total_status),
                format_time(r.total_time),
            ]
        else:
            row = [
                r.name,
                r.format_validate.status.upper(),
                format_time(r.format_validate.time_seconds),
                r.verify.status.upper(),
                format_time(r.verify.time_seconds),
                r.patch_verify.status.upper(),
                format_time(r.patch_verify.time_seconds),
                r.coverage.status.upper(),
                format_time(r.coverage.time_seconds),
                r.total_status.upper(),
                format_time(r.total_time),
            ]
        # Use fixed width for status columns when colored (ANSI codes affect length)
        if use_color:
            print(f"{row[0]:<{max_name}}  {row[1]:^15} {row[2]:>6}  {row[3]:^15} {row[4]:>7}  {row[5]:^15} {row[6]:>7}  {row[7]:^17} {row[8]:>7}  {row[9]:^15} {row[10]:>10}")
        else:
            print(header_fmt.format(*row))

    # Print summary
    print("-" * (max_name + 70))
    total_pass = sum(1 for r in results if r.total_status == "pass")
    total_fail = sum(1 for r in results if r.total_status == "fail")
    total_time = sum(r.total_time for r in results)
    print(f"Summary: {total_pass} passed, {total_fail} failed, {len(results)} total")
    print(f"Total time: {format_time(total_time)}")


def save_json(results: list[BenchmarkResult], output_file: Path):
    """Save results to JSON file."""
    data = {
        "results": [
            {
                "name": r.name,
                "format_validate": {"status": r.format_validate.status, "time": r.format_validate.time_seconds, "error": r.format_validate.error},
                "verify": {"status": r.verify.status, "time": r.verify.time_seconds, "error": r.verify.error},
                "patch_verify": {"status": r.patch_verify.status, "time": r.patch_verify.time_seconds, "error": r.patch_verify.error},
                "coverage": {"status": r.coverage.status, "time": r.coverage.time_seconds, "error": r.coverage.error},
                "total_status": r.total_status,
                "total_time": r.total_time,
            }
            for r in results
        ],
        "summary": {
            "total": len(results),
            "passed": sum(1 for r in results if r.total_status == "pass"),
            "failed": sum(1 for r in results if r.total_status == "fail"),
            "total_time": sum(r.total_time for r in results),
        },
    }
    with open(output_file, "w") as f:
        json.dump(data, f, indent=2)
    print(f"\nResults saved to: {output_file}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run benchmark status checks")
    parser.add_argument(
        "--benchmarks-dir",
        type=Path,
        default=Path("benchmarks"),
        help="Directory containing benchmarks",
    )
    parser.add_argument(
        "--filter",
        type=str,
        help="Filter benchmarks by glob pattern (e.g., 'sanity-*', 'afc-curl-*')",
    )
    parser.add_argument(
        "--projects",
        type=str,
        help="Comma-separated list of specific projects to run",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Output directory for logs and results (contains summary.json and per-project subdirs)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Output JSON file for results (deprecated, use --output-dir)",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable colored output",
    )
    parser.add_argument(
        "--skip-format-validate",
        action="store_true",
        help="Skip format validation",
    )
    parser.add_argument(
        "--skip-verify",
        action="store_true",
        help="Skip verify command",
    )
    parser.add_argument(
        "--skip-patch-verify",
        action="store_true",
        help="Skip patch-verify command",
    )
    parser.add_argument(
        "--skip-coverage",
        action="store_true",
        help="Skip coverage command",
    )

    args = parser.parse_args()

    # Discover or parse benchmarks
    if args.projects:
        project_names = [p.strip() for p in args.projects.split(",")]
        benchmarks = [args.benchmarks_dir / name for name in project_names]
        # Validate
        for b in benchmarks:
            if not b.exists():
                print(f"ERROR: Benchmark not found: {b}")
                return 1
            if not (b / ".aixcc").exists():
                print(f"ERROR: Not a valid benchmark (missing .aixcc): {b}")
                return 1
    else:
        benchmarks = discover_benchmarks(args.benchmarks_dir, args.filter)

    if not benchmarks:
        print("No benchmarks found")
        return 1

    # Create output directory if specified
    output_dir: Path | None = args.output_dir
    if output_dir:
        output_dir.mkdir(parents=True, exist_ok=True)
        print(f"Output directory: {output_dir}")

    print(f"Running status checks for {len(benchmarks)} benchmarks...")
    print()

    results: list[BenchmarkResult] = []

    for i, benchmark_path in enumerate(benchmarks, 1):
        print(f"[{i}/{len(benchmarks)}] {benchmark_path.name}")

        result = BenchmarkResult(name=benchmark_path.name)

        # Create per-project log directory
        project_log_dir: Path | None = None
        if output_dir:
            project_log_dir = output_dir / benchmark_path.name
            project_log_dir.mkdir(parents=True, exist_ok=True)

        # Run format validation (fast, no temp dir needed)
        if not args.skip_format_validate:
            print("  Running format-validate...", end=" ", flush=True)
            result.format_validate = run_format_validate(benchmark_path, project_log_dir)
            print(f"{result.format_validate.status.upper()} ({format_time(result.format_validate.time_seconds)})")
            if result.format_validate.error:
                print(f"    Error: {result.format_validate.error[:100]}")

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)

            # Run verify
            if not args.skip_verify:
                print("  Running verify...", end=" ", flush=True)
                result.verify = run_verify(benchmark_path, tmp_path / "verify.json", project_log_dir)
                print(f"{result.verify.status.upper()} ({format_time(result.verify.time_seconds)})")
                if result.verify.error:
                    print(f"    Error: {result.verify.error[:100]}")

            # Run patch-verify
            if not args.skip_patch_verify:
                print("  Running patch-verify...", end=" ", flush=True)
                result.patch_verify = run_patch_verify(benchmark_path, tmp_path / "patch-verify.json", project_log_dir)
                print(f"{result.patch_verify.status.upper()} ({format_time(result.patch_verify.time_seconds)})")
                if result.patch_verify.error:
                    print(f"    Error: {result.patch_verify.error[:100]}")

            # Run coverage
            if not args.skip_coverage:
                print("  Running coverage...", end=" ", flush=True)
                result.coverage = run_coverage(benchmark_path, tmp_path / "coverage.json", project_log_dir)
                print(f"{result.coverage.status.upper()} ({format_time(result.coverage.time_seconds)})")
                if result.coverage.error:
                    print(f"    Error: {result.coverage.error[:100]}")

        results.append(result)
        print()

    # Print table
    print("\n" + "=" * 80)
    print("BENCHMARK STATUS REPORT")
    print("=" * 80 + "\n")
    print_table(results, use_color=not args.no_color)

    # Save JSON to output directory
    if output_dir:
        save_json(results, output_dir / "summary.json")

    # Also save to --output if specified (backwards compatibility)
    if args.output:
        save_json(results, args.output)

    # Return non-zero if any failed
    if any(r.total_status == "fail" for r in results):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
