"""Benchmark snapshot generator for CRSBench.

This module generates realistic trial snapshots from benchmark ground truth data,
simulating POV/patch discovery timelines for testing and demonstration purposes.

Main components:
- BenchmarkSnapshotGenerator: Main generator class
- DiscoveryTimeline: Models POV/patch discovery over time
- FaultInjector: Generates invalid POVs/patches for testing
- SnapshotBuilder: Creates snapshot archives

Usage:
    from crsbench.bench_snapgen import BenchmarkSnapshotGenerator

    generator = BenchmarkSnapshotGenerator(
        benchmark_path=Path("benchmarks/my-benchmark"),
        output_dir=Path("/tmp/trial-001"),
        trial_duration=7200,
        snapshot_period=900
    )

    generator.generate_trial_snapshots(
        mode='bug-finding',
        difficulty_level=2
    )
"""

from crsbench.bench_snapgen.builder import SnapshotBuilder
from crsbench.bench_snapgen.fault_injection import (
    FaultInjector,
    inject_faults_into_timeline,
)
from crsbench.bench_snapgen.generator import (
    BenchmarkData,
    BenchmarkSnapshotGenerator,
    POVData,
    load_benchmark_ground_truth,
)
from crsbench.bench_snapgen.timeline import (
    DiscoveryEvent,
    DiscoveryTimeline,
    PatchGenerationModel,
    POVDiscoveryModel,
    create_discovery_timeline,
)

__all__ = [
    "BenchmarkSnapshotGenerator",
    "BenchmarkData",
    "POVData",
    "load_benchmark_ground_truth",
    "DiscoveryEvent",
    "DiscoveryTimeline",
    "POVDiscoveryModel",
    "PatchGenerationModel",
    "create_discovery_timeline",
    "FaultInjector",
    "inject_faults_into_timeline",
    "SnapshotBuilder",
]
