"""Format validation subcommand.

Validates benchmark structure and meta.yaml schema with per-check columns:
- struct: Dockerfile, project.yaml, meta.yaml existence
- schema: meta.yaml Pydantic schema validation
- harness: harness directory ↔ meta.yaml consistency
- cpv: CPV directory ↔ meta.yaml consistency, vuln.yaml, no conflicts
- blob: pov_0.blob exists and non-empty per CPV (ground truth)
- patch: patches exist and non-empty per CPV
"""

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from crsbench.benchmark.packaging.validate import (
    validate_benchmark as structural_validate,
)
from crsbench.benchmark.packaging.validate import (
    validate_tarball_naming,
)

if TYPE_CHECKING:
    from collections.abc import Callable

from crsbench.benchmark_ci.cli.benchmark_discovery import (
    discover_cpv_ids,
    discover_harness_names,
)
from crsbench.benchmark_ci.cli.common_args import (
    create_benchmark_selection_parent,
    create_output_options_parent,
)
from crsbench.benchmark_ci.cli.discovery import resolve_benchmark_paths
from crsbench.benchmark_ci.cli.output import print_results_table, save_output_dir
from crsbench.benchmark_ci.models import (
    BenchmarkValidationResult,
    CheckMode,
    CheckResult,
    CheckStatus,
    ValidationSummary,
)
from crsbench.utils.logger import get_logger
from crsbench.validation import validate_benchmark as schema_validate

logger = get_logger(__name__)

# Format sub-check names (column order in table)
FORMAT_CHECK_NAMES = ("struct", "schema", "harness", "cpv", "blob", "patch")


def register(subparsers: argparse._SubParsersAction) -> None:
    """Register the format subcommand."""
    parser = subparsers.add_parser(
        "format",
        parents=[
            create_benchmark_selection_parent(),
            create_output_options_parent(),
        ],
        help="Run format validation only (fast, no Docker)",
    )
    parser.add_argument(
        "--source",
        type=str,
        choices=["pkgs", "main_repo"],
        default="pkgs",
        help="Source mode: also validate pkgs/ when 'pkgs' (default: pkgs)",
    )
    parser.add_argument(
        "--parallel",
        "-p",
        type=int,
        default=1,
        help="Number of benchmarks to validate in parallel (default: 1, sequential)",
    )
    parser.set_defaults(ci_func=run_format)


def _load_meta_yaml_harnesses(benchmark_path: Path) -> list[dict]:
    """Load harness declarations from .aixcc/meta.yaml.

    Returns list of dicts with 'name' and 'vulns' (list of vuln_keyword strings).
    Returns empty list if meta.yaml is missing or unparseable.
    """
    meta_path = benchmark_path / ".aixcc" / "meta.yaml"
    if not meta_path.exists():
        return []
    try:
        content = yaml.safe_load(meta_path.read_text())
        if not content or "harness_files" not in content:
            return []
        harnesses = []
        for h in content["harness_files"]:
            name = h.get("name", "")
            vulns = [v.get("vuln_keyword", "") for v in (h.get("vulns") or [])]
            harnesses.append({"name": name, "vulns": vulns})
        return harnesses
    except (yaml.YAMLError, AttributeError, TypeError):
        return []


def _check_struct(path: Path, source_mode: str) -> CheckResult:
    """Check structural files: Dockerfile, project.yaml, meta.yaml."""
    t0 = time.time()
    errors: list[str] = []

    struct_result = structural_validate(path)
    errors.extend(struct_result.errors)

    # Filter pkgs/ref.diff warnings by source mode
    for warning in struct_result.warnings:
        if source_mode != "pkgs" and (
            "tarball" in warning or "pkg_refs" in warning or "ref.diff" in warning
        ):
            continue
        errors.append(f"WARN: {warning}")

    elapsed = time.time() - t0
    if errors:
        return CheckResult(
            status=CheckStatus.FAIL,
            time_seconds=elapsed,
            error="; ".join(errors[:3]),
            details={"issues": errors},
        )
    return CheckResult(status=CheckStatus.PASS, time_seconds=elapsed)


def _check_schema(path: Path) -> CheckResult:
    """Check meta.yaml Pydantic schema validation."""
    t0 = time.time()
    schema_result = schema_validate(path)
    elapsed = time.time() - t0

    if not schema_result.is_valid:
        errors = [err.message for err in schema_result.errors[:5]]
        return CheckResult(
            status=CheckStatus.FAIL,
            time_seconds=elapsed,
            error="; ".join(errors[:3]),
            details={"issues": errors},
        )
    return CheckResult(status=CheckStatus.PASS, time_seconds=elapsed)


def _check_harness(path: Path) -> CheckResult:
    """Check harness directory ↔ meta.yaml consistency."""
    t0 = time.time()
    errors: list[str] = []

    dir_harnesses = set(discover_harness_names(path))
    meta_harnesses_data = _load_meta_yaml_harnesses(path)
    meta_harness_names = {h["name"] for h in meta_harnesses_data}

    # Directories without meta.yaml declaration
    for h in sorted(dir_harnesses - meta_harness_names):
        errors.append(f"directory .aixcc/{h}/ not declared in meta.yaml")

    # meta.yaml declarations without directory (only if harness has vulns)
    meta_with_vulns = {h["name"] for h in meta_harnesses_data if h["vulns"]}
    for h in sorted(meta_with_vulns - dir_harnesses):
        errors.append(f"meta.yaml declares harness '{h}' but no directory exists")

    elapsed = time.time() - t0
    if errors:
        return CheckResult(
            status=CheckStatus.FAIL,
            time_seconds=elapsed,
            error="; ".join(errors[:3]),
            details={"issues": errors},
        )
    return CheckResult(status=CheckStatus.PASS, time_seconds=elapsed)


def _check_cpv(path: Path) -> CheckResult:
    """Check CPV consistency: dirs match meta.yaml, vuln.yaml present, no conflicts."""
    t0 = time.time()
    errors: list[str] = []

    harnesses = discover_harness_names(path)
    meta_harnesses_data = _load_meta_yaml_harnesses(path)
    meta_cpvs_by_harness = {h["name"]: set(h["vulns"]) for h in meta_harnesses_data}

    cpv_to_harnesses: dict[str, list[str]] = {}

    for harness in harnesses:
        dir_cpvs = set(discover_cpv_ids(path, harness))
        meta_cpvs = meta_cpvs_by_harness.get(harness, set())

        # Directory CPVs not in meta.yaml
        for cpv_id in sorted(dir_cpvs - meta_cpvs):
            if meta_cpvs:  # Only warn if meta.yaml has CPV declarations
                errors.append(f"{harness}/{cpv_id}: not declared in meta.yaml")

        # meta.yaml CPVs without directory
        for cpv_id in sorted(meta_cpvs - dir_cpvs):
            errors.append(f"{harness}/{cpv_id}: declared in meta.yaml but no directory")

        # vuln.yaml presence
        for cpv_id in sorted(dir_cpvs):
            cpv_to_harnesses.setdefault(cpv_id, []).append(harness)
            cpv_dir = path / ".aixcc" / harness / cpv_id
            if not (cpv_dir / "vuln.yaml").exists():
                errors.append(f"{harness}/{cpv_id}: missing vuln.yaml")

    # Cross-harness conflicts
    for cpv_id, owners in sorted(cpv_to_harnesses.items()):
        if len(owners) > 1:
            errors.append(
                f"CPV conflict: {cpv_id} in multiple harnesses: {', '.join(owners)}"
            )

    elapsed = time.time() - t0
    if errors:
        return CheckResult(
            status=CheckStatus.FAIL,
            time_seconds=elapsed,
            error="; ".join(errors[:3]),
            details={"issues": errors},
        )
    return CheckResult(status=CheckStatus.PASS, time_seconds=elapsed)


def _check_blob(path: Path) -> CheckResult:
    """Check pov_0.blob exists and is non-empty per CPV (ground truth)."""
    t0 = time.time()
    errors: list[str] = []

    for harness in discover_harness_names(path):
        for cpv_id in discover_cpv_ids(path, harness):
            blobs_dir = path / ".aixcc" / harness / cpv_id / "blobs"
            ground_truth = blobs_dir / "pov_0.blob"

            if not ground_truth.exists():
                errors.append(f"{harness}/{cpv_id}: missing pov_0.blob")
            elif ground_truth.stat().st_size == 0:
                errors.append(f"{harness}/{cpv_id}: pov_0.blob is empty")

    elapsed = time.time() - t0
    if errors:
        return CheckResult(
            status=CheckStatus.FAIL,
            time_seconds=elapsed,
            error="; ".join(errors[:3]),
            details={"issues": errors},
        )
    return CheckResult(status=CheckStatus.PASS, time_seconds=elapsed)


def _check_patch(path: Path) -> CheckResult:
    """Check patches exist and are non-empty per CPV."""
    t0 = time.time()
    errors: list[str] = []

    for harness in discover_harness_names(path):
        for cpv_id in discover_cpv_ids(path, harness):
            patches_dir = path / ".aixcc" / harness / cpv_id / "patches"
            if not patches_dir.exists():
                errors.append(f"{harness}/{cpv_id}: missing patches/ directory")
                continue

            patch_files = list(patches_dir.glob("patch_*.diff"))
            if not patch_files:
                errors.append(f"{harness}/{cpv_id}: no patch_*.diff files")
                continue

            for pf in patch_files:
                if pf.stat().st_size == 0:
                    errors.append(f"{harness}/{cpv_id}: {pf.name} is empty")

    elapsed = time.time() - t0
    if errors:
        return CheckResult(
            status=CheckStatus.FAIL,
            time_seconds=elapsed,
            error="; ".join(errors[:3]),
            details={"issues": errors},
        )
    return CheckResult(status=CheckStatus.PASS, time_seconds=elapsed)


def _check_tarball(path: Path) -> CheckResult:
    """Check source tarball naming matches Dockerfile WORKDIR.

    This validates TAR-04/VAL-01/VAL-03 requirements:
    - Tarball name must match WORKDIR directory name
    - Naming mismatches are errors (not warnings)
    """
    t0 = time.time()

    result = validate_tarball_naming(path)
    elapsed = time.time() - t0

    if not result.valid:
        return CheckResult(
            status=CheckStatus.FAIL,
            time_seconds=elapsed,
            error="; ".join(result.errors[:3]),
            details={
                "issues": result.errors,
                "expected": result.expected_name,
                "found": result.found_tarballs,
            },
        )

    # Include warnings as info but still pass
    if result.warnings:
        return CheckResult(
            status=CheckStatus.PASS,
            time_seconds=elapsed,
            details={"warnings": result.warnings},
        )

    return CheckResult(status=CheckStatus.PASS, time_seconds=elapsed)


def _validate_benchmark(
    path: Path, source_mode: str = "pkgs"
) -> BenchmarkValidationResult:
    """Run all format sub-checks for a single benchmark.

    Produces individual CheckResult per check type (struct, schema, harness,
    cpv, blob, patch, and optionally tarball) and an overall format_check summary.

    When source_mode is "pkgs", also runs tarball naming validation (VAL-01).
    """
    start_dt = datetime.now()
    checks: dict[str, CheckResult] = {}

    check_funcs: list[tuple[str, Callable[[], CheckResult]]] = [
        ("struct", lambda: _check_struct(path, source_mode)),
        ("schema", lambda: _check_schema(path)),
        ("harness", lambda: _check_harness(path)),
        ("cpv", lambda: _check_cpv(path)),
        ("blob", lambda: _check_blob(path)),
        ("patch", lambda: _check_patch(path)),
    ]

    # Add tarball naming check when source mode is "pkgs" (VAL-01)
    if source_mode == "pkgs":
        check_funcs.append(("tarball", lambda: _check_tarball(path)))

    for name, func in check_funcs:
        try:
            checks[name] = func()
        except Exception as exc:
            checks[name] = CheckResult.make_error(str(exc))

    # Overall format_check: PASS only if all sub-checks pass
    total_time = sum(c.time_seconds for c in checks.values())
    all_pass = all(c.status == CheckStatus.PASS for c in checks.values())
    failed_names = [n for n, c in checks.items() if c.status != CheckStatus.PASS]

    if all_pass:
        format_check = CheckResult(status=CheckStatus.PASS, time_seconds=total_time)
    else:
        format_check = CheckResult(
            status=CheckStatus.FAIL,
            time_seconds=total_time,
            error=f"failed: {', '.join(failed_names)}",
        )

    return BenchmarkValidationResult(
        benchmark=path.name,
        benchmark_path=path,
        format_check=format_check,
        format_checks=checks,
        started_at=start_dt,
        finished_at=datetime.now(),
    )


def run_format(args: argparse.Namespace) -> int:
    """Run format validation on resolved benchmarks."""
    paths = resolve_benchmark_paths(
        benchmark_arg=getattr(args, "benchmark", None),
        benchmarks_list=getattr(args, "benchmarks", None),
        benchmark_suite=getattr(args, "benchmark_suite", None),
        all_benchmarks=getattr(args, "all", False),
        filter_pattern=getattr(args, "filter", None),
    )

    parallel = getattr(args, "parallel", 1)
    source_mode = getattr(args, "source", "pkgs")
    summary = ValidationSummary(started_at=datetime.now(), check_mode=CheckMode.FORMAT)

    if parallel <= 1:
        for path in paths:
            result = _validate_benchmark(path, source_mode)
            summary.add_result(result)
    else:
        with ThreadPoolExecutor(max_workers=parallel) as executor:
            futures = {
                executor.submit(_validate_benchmark, path, source_mode): path
                for path in paths
            }
            for future in as_completed(futures):
                path = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    logger.error(f"Unexpected error validating {path.name}: {exc}")
                    result = BenchmarkValidationResult(
                        benchmark=path.name,
                        benchmark_path=path,
                        started_at=datetime.now(),
                        finished_at=datetime.now(),
                    )
                summary.add_result(result)

    summary.finished_at = datetime.now()

    print_results_table(
        summary,
        check_mode=CheckMode.FORMAT,
        no_color=getattr(args, "no_color", False),
    )

    output_dir = getattr(args, "output_dir", None)
    if output_dir:
        save_output_dir(summary, Path(output_dir), check_mode=CheckMode.FORMAT)

    output_path = getattr(args, "output", None)
    if output_path:
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(summary.to_dict(), indent=2))

    if summary.failed > 0 or summary.errors > 0:
        return 1
    return 0
