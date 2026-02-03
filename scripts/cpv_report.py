"""CPV detection report for CRSBench experiments.

Usage:
    python cpv_report.py <experiment_dir> [--benchmarks-dir <path>]

Example:
    python cpv_report.py experiment-data-afc/e2e-bugfind-afc
    python cpv_report.py experiment-data-afc/e2e-bugfind-afc --benchmarks-dir /home/dongkwan/CRSBench/benchmarks
"""

import argparse
import csv
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class CPVResult:
    cpv_id: str
    found: bool
    first_pov_relative_time: float | None = None  # seconds from CRS start
    pov_hash: str | None = None


@dataclass
class POVStatusCounts:
    """Per-status POV counts from pov_store."""
    cpv: int = 0
    unintended_crash: int = 0
    not_vulnerable: int = 0
    error: int = 0

    @property
    def total(self) -> int:
        return self.cpv + self.unintended_crash + self.not_vulnerable + self.error


@dataclass
class TrialReport:
    benchmark: str
    harness: str
    crs: str
    trial_num: int
    total_cpvs: int
    found_cpvs: int
    cpv_results: list[CPVResult] = field(default_factory=list)
    last_pov_relative_time: float | None = None  # last POV mtime relative to CRS start
    pov_counts: POVStatusCounts = field(default_factory=POVStatusCounts)
    success: bool = False
    failure_reason: str = ""  # e.g. "build_failure", "timeout", "no_povs", "runtime_error"


def load_ground_truth(benchmarks_dir: Path, benchmark_name: str) -> dict[str, list[str]]:
    """Load ground truth CPVs from meta.yaml.

    Returns:
        dict mapping harness_name -> list of cpv_ids (e.g. ["cpv_0", "cpv_1"])
    """
    meta_path = benchmarks_dir / benchmark_name / ".aixcc" / "meta.yaml"
    if not meta_path.exists():
        return {}

    with meta_path.open() as f:
        data = yaml.safe_load(f)

    harness_cpvs: dict[str, list[str]] = {}
    for harness in data.get("harness_files", []):
        name = harness["name"]
        vulns = harness.get("vulns") or []
        cpvs = [v["vuln_keyword"] for v in vulns]
        if cpvs:
            harness_cpvs[name] = cpvs
    return harness_cpvs


def load_pov_store(trial_dir: Path) -> dict | None:
    """Load pov_store.json from trial directory."""
    pov_store_path = trial_dir / "povs" / "pov_store.json"
    if not pov_store_path.exists():
        return None
    with pov_store_path.open() as f:
        return json.load(f)


def load_trial_metadata(trial_dir: Path) -> dict | None:
    """Load metadata.json from trial directory."""
    meta_path = trial_dir / "metadata.json"
    if not meta_path.exists():
        return None
    with meta_path.open() as f:
        return json.load(f)


def load_execution_json(trial_dir: Path) -> dict | None:
    """Load execution.json from trial directory."""
    exec_path = trial_dir / "execution.json"
    if not exec_path.exists():
        return None
    with exec_path.open() as f:
        return json.load(f)


def _is_timeout(execution: dict | None, trial_dir: Path) -> bool:
    """Check whether a trial was terminated due to timeout.

    Checks both the execution.json timeout flag and the worker.log for
    timeout evidence (needed for older data where execution.json had a
    bug that always recorded timeout as false).
    """
    if execution:
        exec_info = execution.get("execution", {})
        if exec_info.get("timeout"):
            return True

    # Fallback: check worker.log for graceful timeout messages.
    # Older execution.json files recorded "timeout": false even for
    # graceful timeout kills (bug: checked returncode == 124 instead
    # of the actual timed_out flag from run_with_graceful_timeout).
    worker_log_path = trial_dir / "worker.log"
    if worker_log_path.exists():
        worker_log = worker_log_path.read_text(errors="replace")[-5000:]
        if "Timeout reached after" in worker_log:
            return True

    return False


def detect_failure_reason(trial_dir: Path, pov_store: dict | None) -> str:
    """Detect failure reason for a trial that didn't find all CPVs.

    Returns one of:
        "build_failure" - CRS build failed
        "timeout" - CRS execution timed out without producing POVs
        "runtime_error" - CRS crashed or returned non-zero
        "oom_killed" - CRS killed by SIGKILL (likely OOM, not timeout)
        "no_povs" - CRS ran OK but produced no POVs
        "not_triggered" - CRS ran, produced POVs, but didn't trigger this CPV
        "" - success (all CPVs found) or unknown
    """
    has_fail = (trial_dir / ".fail").exists()
    has_success = (trial_dir / ".success").exists()

    execution = load_execution_json(trial_dir)
    timed_out = _is_timeout(execution, trial_dir)

    # Check execution.json for non-timeout failures
    if execution:
        exec_info = execution.get("execution", {})
        if not exec_info.get("success", True) and not timed_out:
            returncode = exec_info.get("returncode", -1)
            # Build failures typically have specific error patterns
            error_type = execution.get("error_type", "")
            if "Build" in error_type:
                return "build_failure"
            if returncode == -9:
                return "oom_killed"
            return f"runtime_error (exit {returncode})"

    # Check .fail marker without execution.json
    if has_fail and not has_success and not timed_out:
        # Check worker.log for build timeout
        worker_log_path = trial_dir / "worker.log"
        if worker_log_path.exists():
            worker_log = worker_log_path.read_text(errors="replace")[-3000:]
            if "Build timeout" in worker_log:
                return "build_timeout"

        # Try to get error info from stderr
        stderr_path = trial_dir / "crs_stderr.log"
        if stderr_path.exists():
            stderr = stderr_path.read_text(errors="replace")[-2000:]
            if any(kw in stderr.lower() for kw in ["build fail", "compilation error", "make error", "cmake error"]):
                return "build_failure"
        return "runtime_error"

    # CRS ran (possibly to timeout) but didn't find CPVs.
    # Prefer specific POV-based reasons over generic "timeout".
    if pov_store and pov_store.get("povs"):
        # Had POVs but didn't match this CPV
        return "not_triggered"

    # No POVs produced
    if timed_out:
        return "timeout"
    if pov_store is None:
        return "no_povs"
    return "no_povs"


def analyze_trial(
    trial_dir: Path,
    benchmarks_dir: Path,
) -> TrialReport | None:
    """Analyze a single trial directory."""
    metadata = load_trial_metadata(trial_dir)
    if not metadata:
        return None

    benchmark = metadata.get("benchmark", "unknown")
    harness = metadata.get("harness", "unknown")
    crs = metadata.get("crs", "unknown")

    match = re.match(r"trial-(\d+)", trial_dir.name)
    trial_num = int(match.group(1)) if match else 0

    # Load ground truth
    ground_truth = load_ground_truth(benchmarks_dir, benchmark)
    expected_cpvs = ground_truth.get(harness, [])

    # Load POV store
    pov_store = load_pov_store(trial_dir)
    cpv_to_first_pov = pov_store.get("cpv_to_first_pov", {}) if pov_store else {}
    crs_start = pov_store.get("crs_run_start_time", 0) if pov_store else 0

    # Per-CPV results
    cpv_results = []
    for cpv_id in sorted(expected_cpvs):
        info = cpv_to_first_pov.get(cpv_id)
        if info:
            cpv_results.append(CPVResult(
                cpv_id=cpv_id,
                found=True,
                first_pov_relative_time=info.get("relative_time"),
                pov_hash=info.get("pov_hash"),
            ))
        else:
            cpv_results.append(CPVResult(cpv_id=cpv_id, found=False))

    # Count POVs by verification status and find last POV time
    pov_counts = POVStatusCounts()
    last_pov_time = None
    if pov_store:
        mtimes = []
        for pov_entry in pov_store.get("povs", {}).values():
            status = pov_entry.get("status", "")
            if status == "cpv":
                pov_counts.cpv += 1
            elif status == "unintended_crash":
                pov_counts.unintended_crash += 1
            elif status == "not_vulnerable":
                pov_counts.not_vulnerable += 1
            elif status == "error":
                pov_counts.error += 1

            if crs_start:
                mtime = pov_entry.get("file_mtime")
                if mtime:
                    mtimes.append(mtime - crs_start)
        if mtimes:
            last_pov_time = max(mtimes)

    has_success = (trial_dir / ".success").exists()
    found_count = sum(1 for c in cpv_results if c.found)

    # Detect failure reason for missed CPVs
    failure_reason = ""
    if found_count < len(expected_cpvs):
        failure_reason = detect_failure_reason(trial_dir, pov_store)

    return TrialReport(
        benchmark=benchmark,
        harness=harness,
        crs=crs,
        trial_num=trial_num,
        total_cpvs=len(expected_cpvs),
        found_cpvs=found_count,
        cpv_results=cpv_results,
        last_pov_relative_time=last_pov_time,
        pov_counts=pov_counts,
        success=has_success,
        failure_reason=failure_reason,
    )


def fmt_time(seconds: float | None) -> str:
    """Format seconds as human-readable time."""
    if seconds is None:
        return "-"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}h{m:02d}m{s:02d}s"
    if m > 0:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def main():
    parser = argparse.ArgumentParser(description="CPV detection report")
    parser.add_argument("experiment_dir", type=Path, help="Experiment directory")
    parser.add_argument(
        "--benchmarks-dir", type=Path,
        default=Path("/home/dongkwan/CRSBench/benchmarks"),
        help="Path to benchmarks directory with ground truth",
    )
    parser.add_argument("--json", action="store_true", help="Output JSON")
    parser.add_argument("--csv", action="store_true", help="Output CSV")
    args = parser.parse_args()

    experiment_dir = args.experiment_dir.resolve()
    if not experiment_dir.exists():
        print(f"Error: {experiment_dir} not found", file=sys.stderr)
        sys.exit(1)

    # Discover trials
    trial_dirs = sorted(experiment_dir.rglob("trial-*"))
    trial_dirs = [d for d in trial_dirs if d.is_dir() and re.match(r"trial-\d+", d.name)]

    reports: list[TrialReport] = []
    for trial_dir in trial_dirs:
        report = analyze_trial(trial_dir, args.benchmarks_dir)
        if report and report.total_cpvs > 0:
            reports.append(report)

    if args.json:
        import dataclasses
        print(json.dumps([dataclasses.asdict(r) for r in reports], indent=2))
        return

    if args.csv:
        writer = csv.writer(sys.stdout)
        writer.writerow([
            "benchmark", "harness", "crs", "trial",
            "cpv_id", "found", "failure_reason",
            "first_pov_time_seconds", "first_pov_time", "pov_hash",
            "povs_total", "povs_cpv", "povs_unintended", "povs_not_vulnerable", "povs_error",
        ])
        for r in sorted(reports, key=lambda x: (x.benchmark, x.harness)):
            for cpv in r.cpv_results:
                reason = "" if cpv.found else r.failure_reason
                writer.writerow([
                    r.benchmark, r.harness, r.crs, r.trial_num,
                    cpv.cpv_id, cpv.found, reason,
                    cpv.first_pov_relative_time or "",
                    fmt_time(cpv.first_pov_relative_time) if cpv.found else "",
                    cpv.pov_hash or "",
                    r.pov_counts.total, r.pov_counts.cpv,
                    r.pov_counts.unintended_crash, r.pov_counts.not_vulnerable,
                    r.pov_counts.error,
                ])
        return

    # Print table
    total_all = 0
    found_all = 0

    header = (
        f"{'Benchmark':<45} {'Harness':<35} {'CPVs':>6} {'Found':>6} {'Rate':>7} {'Last POV':>10}"
        f"  {'POVs':>5} {'CPV':>5} {'Unint':>5} {'NotV':>5} {'Err':>5}"
    )
    print(f"\n{header}")
    print("-" * len(header))

    for r in sorted(reports, key=lambda x: (x.benchmark, x.harness)):
        total_all += r.total_cpvs
        found_all += r.found_cpvs
        pct = f"{r.found_cpvs/r.total_cpvs*100:.0f}%" if r.total_cpvs else "-"
        last = fmt_time(r.last_pov_relative_time)
        c = r.pov_counts
        print(
            f"{r.benchmark:<45} {r.harness:<35} {r.total_cpvs:>6} {r.found_cpvs:>6} {pct:>7} {last:>10}"
            f"  {c.total:>5} {c.cpv:>5} {c.unintended_crash:>5} {c.not_vulnerable:>5} {c.error:>5}"
        )

        # Per-CPV details
        for cpv in r.cpv_results:
            if cpv.found:
                status = "FOUND"
                t = fmt_time(cpv.first_pov_relative_time)
            else:
                status = f"MISS ({r.failure_reason})" if r.failure_reason else "MISS"
                t = ""
            print(f"  {'':>43} {cpv.cpv_id:<15} {status:<25} {t}")

    print("-" * len(header))
    overall_pct = f"{found_all/total_all*100:.0f}%" if total_all else "-"
    print(f"{'TOTAL':<45} {'':35} {total_all:>6} {found_all:>6} {overall_pct:>7}")
    print()


if __name__ == "__main__":
    main()
