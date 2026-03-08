"""Capability probe CI subcommand.

Compares declared capabilities in project.yaml against runtime probes:
- inc_build declaration vs inc-build image availability
- rts_mode declaration vs runtime prerequisites (inc-build + test.sh)

This check is intended to catch drift between declared and actual behavior.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import yaml

from crsbench.benchmark_ci.cli.common_args import (
    create_benchmark_selection_parent,
    create_output_options_parent,
)
from crsbench.benchmark_ci.cli.discovery import resolve_benchmark_paths
from crsbench.benchmark_ci.validator import _load_project_capabilities
from crsbench.builder.infrastructure import OSSFuzzInfrastructure
from crsbench.utils.logger import get_logger
from crsbench.utils.run_helper import ensure_oss_fuzz_root

if TYPE_CHECKING:
    import argparse

logger = get_logger(__name__)

DEFAULT_INC_REGISTRY = "ghcr.io/team-atlanta/crsbench"


@dataclass
class CapabilityProbeResult:
    """Result of declaration vs probe comparison for one benchmark."""

    benchmark: str
    declared_inc_build: bool
    probed_inc_build: bool
    declared_rts_mode: Optional[str]
    probed_rts_mode: Optional[str]
    reasons: list[str]
    matches: bool


def register(subparsers: argparse._SubParsersAction) -> None:
    """Register the capabilities subcommand."""
    parser = subparsers.add_parser(
        "capabilities",
        parents=[
            create_benchmark_selection_parent(),
            create_output_options_parent(),
        ],
        help="Probe runtime capabilities and compare with project.yaml declarations",
    )
    parser.add_argument(
        "--registry",
        type=str,
        default=os.environ.get("CRSBENCH_INC_BUILD_REGISTRY", DEFAULT_INC_REGISTRY),
        help=(
            "Container registry for probing inc-build images "
            f"(default: env CRSBENCH_INC_BUILD_REGISTRY or {DEFAULT_INC_REGISTRY})"
        ),
    )
    parser.add_argument(
        "--probe-local-only",
        action="store_true",
        help="Do not pull images from registry while probing inc-build capability",
    )
    parser.set_defaults(ci_func=run_capabilities)


def _load_project_sanitizers(benchmark_path: Path) -> list[str]:
    """Load sanitizer list from project.yaml (default: ['address'])."""
    project_yaml = benchmark_path / "project.yaml"
    if not project_yaml.exists():
        return ["address"]

    try:
        with project_yaml.open() as f:
            data = yaml.safe_load(f) or {}
        sanitizers = data.get("sanitizers")
        if isinstance(sanitizers, list) and sanitizers:
            parsed = [str(s).strip() for s in sanitizers if str(s).strip()]
            if parsed:
                return parsed
    except Exception as exc:
        logger.warning(f"Failed to parse sanitizers for {benchmark_path.name}: {exc}")

    return ["address"]


def _probe_runtime_capabilities(
    benchmark_path: Path,
    *,
    registry: str,
    probe_local_only: bool,
) -> tuple[bool, Optional[str], list[str]]:
    """Probe runtime capabilities based on local/registry image + test.sh presence."""
    benchmark_name = benchmark_path.name
    declared_inc, declared_rts = _load_project_capabilities(benchmark_path)
    reasons: list[str] = []

    infra = OSSFuzzInfrastructure(Path(ensure_oss_fuzz_root()))
    sanitizers = _load_project_sanitizers(benchmark_path)

    inc_available = False
    for sanitizer in sanitizers:
        available = infra.is_inc_image_available(benchmark_name, sanitizer, registry)
        if not available and not probe_local_only:
            # Default mode is pull-capable. local-only explicitly disables pull.
            if infra.pull_inc_build_image(benchmark_name, sanitizer, registry):
                available = infra.is_inc_image_available(
                    benchmark_name, sanitizer, registry
                )
        if available:
            inc_available = True
            reasons.append(f"inc-build image available for sanitizer '{sanitizer}'")
            break

    if not inc_available:
        reasons.append(
            "no inc-build image available for declared sanitizers "
            f"{sanitizers} in registry '{registry}'"
        )

    tests_available = infra.is_tests_available(benchmark_name)
    if tests_available:
        reasons.append("test.sh found in oss-fuzz/projects/<benchmark>")
    else:
        reasons.append("test.sh missing in oss-fuzz/projects/<benchmark>")

    # Runtime RTS support requires both inc-build and test.sh.
    # If runtime supports RTS but project.yaml omits rts_mode, use "supported"
    # to surface under-declaration mismatch.
    runtime_rts_supported = inc_available and tests_available
    if runtime_rts_supported:
        probed_rts_mode = declared_rts if declared_rts else "supported"
    else:
        probed_rts_mode = None

    # declared_inc is read only for diagnostics; probe is independent of declaration.
    _ = declared_inc
    return inc_available, probed_rts_mode, reasons


def _probe_benchmark(
    benchmark_path: Path,
    *,
    registry: str,
    probe_local_only: bool,
) -> CapabilityProbeResult:
    """Probe one benchmark and compare declaration vs runtime observations."""
    declared_inc, declared_rts = _load_project_capabilities(benchmark_path)
    probed_inc, probed_rts, reasons = _probe_runtime_capabilities(
        benchmark_path,
        registry=registry,
        probe_local_only=probe_local_only,
    )

    inc_matches = declared_inc == probed_inc
    rts_matches = declared_rts == probed_rts

    # Mode probing cannot infer exact RTS mode value (e.g. jcgeks/openclover).
    # Treat any declared rts_mode as matching when runtime RTS support exists.
    if declared_rts and probed_rts:
        rts_matches = True

    if declared_rts and not probed_rts:
        reasons.append("rts_mode is declared but runtime RTS prerequisites are missing")
    if not declared_rts and probed_rts:
        reasons.append(
            "runtime RTS prerequisites are available but project.yaml does not declare rts_mode"
        )

    matches = inc_matches and rts_matches
    return CapabilityProbeResult(
        benchmark=benchmark_path.name,
        declared_inc_build=declared_inc,
        probed_inc_build=probed_inc,
        declared_rts_mode=declared_rts,
        probed_rts_mode=probed_rts,
        reasons=reasons,
        matches=matches,
    )


def run_capabilities(args: argparse.Namespace) -> int:
    """Run capability probe checks for selected benchmarks."""
    paths = resolve_benchmark_paths(
        benchmark_arg=getattr(args, "benchmark", None),
        benchmarks_list=getattr(args, "benchmarks", None),
        benchmark_suite=getattr(args, "benchmark_suite", None),
        all_benchmarks=getattr(args, "all", False),
        filter_pattern=getattr(args, "filter", None),
    )

    registry = getattr(args, "registry", DEFAULT_INC_REGISTRY)
    probe_local_only = getattr(args, "probe_local_only", False)

    logger.info(
        f"Running capability probes for {len(paths)} benchmark(s), "
        f"registry={registry}, local_only={probe_local_only}"
    )

    results = [
        _probe_benchmark(
            path,
            registry=registry,
            probe_local_only=probe_local_only,
        )
        for path in paths
    ]

    failures = 0
    for r in results:
        status = "PASS" if r.matches else "FAIL"
        logger.info(
            f"[{status}] {r.benchmark} "
            f"inc_build: declared={r.declared_inc_build} probed={r.probed_inc_build}, "
            f"rts_mode: declared={r.declared_rts_mode} probed={r.probed_rts_mode}"
        )
        if not r.matches:
            failures += 1
            for reason in r.reasons:
                logger.error(f"  - {reason}")

    output_path = getattr(args, "output", None)
    if output_path:
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps([asdict(r) for r in results], indent=2))

    return 1 if failures > 0 else 0
