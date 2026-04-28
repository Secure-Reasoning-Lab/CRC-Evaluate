"""Metrics aggregation for the reporting module."""

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from crsbench.reporting.models import (
    BenchmarkMetrics,
    CRSMetrics,
    ExperimentMetrics,
    SnapshotData,
    TimeSeriesPoint,
    TrialInfo,
    TrialMetrics,
)
from crsbench.utils.logger import get_logger

logger = get_logger(__name__)


# Persistent per-trial POV verification store written by the evaluator
# (see crsbench/evaluation/verification/pov/store.py).
_POV_STORE_RELPATH = Path("povs") / "pov_store.json"


class MetricsAggregator:
    """Aggregate metrics from snapshot data.

    This class computes trial-level and experiment-level metrics including:
    - POV discovery statistics
    - Patch generation statistics
    - LLM cost breakdown
    - Time-series analysis

    Example:
        aggregator = MetricsAggregator()
        trial_metrics = aggregator.aggregate_trial(trial_info, snapshots)
    """

    @staticmethod
    def _load_pov_status_breakdown(trial_dir: str | Path) -> dict[str, int]:
        """Count POV entries by verification status from pov_store.json.

        Reads the persistent per-trial POV store (written by the evaluator's
        POV verification manager) and returns a count for each
        ``PovVerificationStatus`` plus a unique-crash-site count for the
        unintended (zero-day) bucket. Missing or malformed stores yield zeros
        so the caller does not need to guard.

        Args:
            trial_dir: Path to the trial directory.

        Returns:
            Dict with keys: povs_cpv, povs_unintended, povs_not_vulnerable,
            povs_error, unintended_unique_sites.
        """
        zero = {
            "povs_cpv": 0,
            "povs_unintended": 0,
            "povs_not_vulnerable": 0,
            "povs_error": 0,
            "unintended_unique_sites": 0,
        }
        store_path = Path(trial_dir) / _POV_STORE_RELPATH
        if not store_path.exists():
            return zero

        try:
            data = json.loads(store_path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"Invalid pov_store.json at {store_path}: {e}")
            return zero

        povs = data.get("povs", {}) or {}
        counts = dict(zero)
        unintended_signatures: set[str] = set()
        unintended_unsigned = 0
        for entry in povs.values():
            status = entry.get("status")
            if status == "cpv":
                counts["povs_cpv"] += 1
            elif status == "unintended_crash":
                counts["povs_unintended"] += 1
                sig = entry.get("crash_signature")
                if sig:
                    unintended_signatures.add(sig)
                else:
                    unintended_unsigned += 1
            elif status == "not_vulnerable":
                counts["povs_not_vulnerable"] += 1
            elif status == "error":
                counts["povs_error"] += 1
        # Unsigned unintended entries cannot be deduped; count each as its own site.
        counts["unintended_unique_sites"] = (
            len(unintended_signatures) + unintended_unsigned
        )
        return counts

    @staticmethod
    def _extract_run_config(trial_dir: str | Path) -> tuple[str | None, str | None]:
        """Extract run_mode and sanitizer from trial directory path.

        Path pattern: <experiment>/<config>/<harness>/<run_mode>/<sanitizer>/trial-N

        Args:
            trial_dir: Trial directory path

        Returns:
            Tuple of (run_mode, sanitizer) or (None, None) if not found
        """
        path = Path(trial_dir)
        parts = path.parts

        # Need at least: mode/sanitizer/trial-N
        if len(parts) < 3:
            return None, None

        try:
            # run_mode is third from end (e.g., "full" or "delta")
            run_mode = parts[-3]
            # sanitizer is second from end (e.g., "address")
            sanitizer = parts[-2]

            # Validate run_mode is expected value
            if run_mode not in ("full", "delta"):
                return None, None

            return run_mode, sanitizer
        except (IndexError, ValueError):
            return None, None

    def aggregate_trial(
        self,
        trial_info: TrialInfo,
        snapshots: list[SnapshotData],
        *,
        total_cpvs: int = 0,
    ) -> TrialMetrics:
        """Aggregate metrics for a single trial.

        Args:
            trial_info: Trial information
            snapshots: List of snapshot data from the trial
            total_cpvs: Total CPVs for this harness from ground truth (default: 0)

        Returns:
            Aggregated trial metrics
        """
        # Extract run configuration from trial path
        run_mode, sanitizer = self._extract_run_config(trial_info.trial_dir)

        # Load per-status POV breakdown from the evaluator's pov_store.json.
        # Independent of CRS snapshot tarballs, so populate even when no
        # snapshots are available.
        pov_status = self._load_pov_status_breakdown(trial_info.trial_dir)

        if not snapshots:
            return TrialMetrics(
                trial_dir=str(trial_info.trial_dir),
                trial_num=trial_info.trial_num,
                crs=trial_info.crs,
                benchmark=trial_info.benchmark,
                harness=trial_info.harness,
                mode=trial_info.mode,
                run_mode=run_mode,
                sanitizer=sanitizer,
                povs_cpv=pov_status["povs_cpv"],
                povs_unintended=pov_status["povs_unintended"],
                povs_not_vulnerable=pov_status["povs_not_vulnerable"],
                povs_error=pov_status["povs_error"],
                unintended_unique_sites=pov_status["unintended_unique_sites"],
            )

        # Sort snapshots by cycle
        sorted_snapshots = sorted(snapshots, key=lambda s: s.cycle)

        # Collect all unique POV and patch names across snapshots
        all_pov_names: set[str] = set()
        all_patch_names: set[str] = set()

        for snap in sorted_snapshots:
            all_pov_names.update(snap.pov_names)
            all_patch_names.update(snap.patch_names)

        # Count unique POVs and patches
        total_povs = len(all_pov_names)
        total_patches = len(all_patch_names)

        # Get final snapshot for cumulative LLM usage
        final_snapshot = sorted_snapshots[-1]
        llm_usage = final_snapshot.llm_usage

        # Extract per-model usage
        llm_usage_by_model: dict[str, dict] = {}
        for model, usage in llm_usage.by_model.items():
            llm_usage_by_model[model] = {
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "cost_usd": usage.cost_usd,
            }

        # Compute time metrics
        total_time = final_snapshot.elapsed_time
        time_to_first_pov = self._compute_time_to_first_pov(
            sorted_snapshots, trial_info
        )

        # Compute time-series data
        time_series = self._compute_time_series(sorted_snapshots)

        # Early stop analysis
        early_stop_time: float | None = None
        early_stop_cost: float | None = None
        cpvs_found_count = 0
        all_cpvs_found = False

        if total_cpvs > 0:
            # Find first snapshot where all CPVs were discovered
            # Only triggers if pov_verification.json exists (cpvs_found non-empty)
            for snap in sorted_snapshots:
                # NOTE: we assume we did have at least one ground truth cpv!!
                if snap.cpvs_found and not snap.cpvs_remaining:
                    # All CPVs found - cpvs_remaining is empty
                    early_stop_time = snap.running_elapsed_time or snap.elapsed_time
                    early_stop_cost = snap.llm_usage.total_cost_usd
                    cpvs_found_count = len(snap.cpvs_found)
                    all_cpvs_found = True
                    logger.debug(
                        f"Early stop detected: {trial_info.trial_dir} - "
                        f"all {total_cpvs} CPVs found at "
                        f"run_elapsed_time={early_stop_time:.1f}s, cost=${early_stop_cost:.4f}"
                    )
                    break

            # If not all found, use last snapshot's CPV count (if pov_verification.json exists)
            if not all_cpvs_found and sorted_snapshots:
                last_snap = sorted_snapshots[-1]
                if last_snap.cpvs_found:
                    cpvs_found_count = len(last_snap.cpvs_found)

        # Calculate savings
        time_saved: float | None = None
        cost_saved: float | None = None
        if early_stop_time is not None:
            time_saved = total_time - early_stop_time
        if early_stop_cost is not None:
            cost_saved = llm_usage.total_cost_usd - early_stop_cost

        return TrialMetrics(
            trial_dir=str(trial_info.trial_dir),
            trial_num=trial_info.trial_num,
            crs=trial_info.crs,
            benchmark=trial_info.benchmark,
            harness=trial_info.harness,
            mode=trial_info.mode,
            run_mode=run_mode,
            sanitizer=sanitizer,
            total_povs_discovered=total_povs,
            unique_pov_names=sorted(all_pov_names),
            total_patches_generated=total_patches,
            unique_patch_names=sorted(all_patch_names),
            total_llm_cost=llm_usage.total_cost_usd,
            total_llm_tokens=llm_usage.total_input_tokens
            + llm_usage.total_output_tokens,
            total_llm_input_tokens=llm_usage.total_input_tokens,
            total_llm_output_tokens=llm_usage.total_output_tokens,
            llm_usage_by_model=llm_usage_by_model,
            total_time=total_time,
            time_to_first_pov=time_to_first_pov,
            time_series=time_series,
            snapshot_count=len(sorted_snapshots),
            total_cpvs=total_cpvs,
            cpvs_found_count=cpvs_found_count,
            all_cpvs_found=all_cpvs_found,
            early_stop_time=early_stop_time,
            early_stop_cost=early_stop_cost,
            time_saved=time_saved,
            cost_saved=cost_saved,
            povs_cpv=pov_status["povs_cpv"],
            povs_unintended=pov_status["povs_unintended"],
            povs_not_vulnerable=pov_status["povs_not_vulnerable"],
            povs_error=pov_status["povs_error"],
            unintended_unique_sites=pov_status["unintended_unique_sites"],
        )

    def aggregate_experiment(
        self,
        experiment_dir: str,
        trial_metrics_list: list[TrialMetrics],
    ) -> ExperimentMetrics:
        """Aggregate metrics across all trials in an experiment.

        Args:
            experiment_dir: Path to experiment directory
            trial_metrics_list: List of trial metrics

        Returns:
            Aggregated experiment metrics
        """
        if not trial_metrics_list:
            return ExperimentMetrics(experiment_dir=experiment_dir)

        total_trials = len(trial_metrics_list)

        # Compute averages
        avg_povs = (
            sum(m.total_povs_discovered for m in trial_metrics_list) / total_trials
        )
        avg_patches = (
            sum(m.total_patches_generated for m in trial_metrics_list) / total_trials
        )
        avg_cost = sum(m.total_llm_cost for m in trial_metrics_list) / total_trials

        # Group by CRS
        by_crs = self._group_by_crs(trial_metrics_list)

        # Group by benchmark
        by_benchmark = self._group_by_benchmark(trial_metrics_list)

        return ExperimentMetrics(
            experiment_dir=experiment_dir,
            total_trials=total_trials,
            valid_trials=total_trials,
            avg_povs_per_trial=avg_povs,
            avg_patches_per_trial=avg_patches,
            avg_cost_per_trial=avg_cost,
            by_crs=by_crs,
            by_benchmark=by_benchmark,
            trial_metrics=trial_metrics_list,
        )

    def _compute_time_to_first_pov(
        self,
        snapshots: list[SnapshotData],
        trial_info: TrialInfo | None = None,
    ) -> float | None:
        """Compute time to first POV discovery.

        Preferred path: anchor on ``metadata.json:timestamp`` (the canonical
        trial start time written by trial preparation) and the per-POV
        ``file_mtime`` recorded in ``povs/pov_store.json``. This is robust to
        re-eval pipelines that mutate ``crs_run_start_time`` and yields
        sub-snapshot precision.

        Fallback: the earliest snapshot whose ``pov_count > 0``. This is
        snapshot-period coarse but works when pov_store / metadata data is
        unavailable.

        Args:
            snapshots: Sorted list of snapshots
            trial_info: Optional trial info; required for the precise path

        Returns:
            Time to first POV in seconds, or None if no POVs are recorded
        """
        if trial_info is not None:
            precise = self._first_pov_time_from_pov_store(trial_info)
            if precise is not None:
                return precise

        for snapshot in snapshots:
            if snapshot.pov_count > 0:
                return snapshot.elapsed_time
        return None

    @staticmethod
    def _first_pov_time_from_pov_store(trial_info: TrialInfo) -> float | None:
        """Compute first-PoV time from pov_store.json anchored on metadata.

        Authoritative trial start: ``trial_info.metadata.timestamp`` (ISO8601);
        falls back to ``metadata.json:timestamp`` on disk.

        Discovery time per POV: ``file_mtime`` from ``povs/pov_store.json``,
        which is preserved across re-evals (only ``crs_run_start_time`` was
        observed to drift). When ``cpv_to_first_pov`` is populated we use
        those entries (matched-CPV PoVs only); otherwise we fall back to the
        earliest ``file_mtime`` across all stored POVs.

        Returns None if any required input is missing or the inputs are
        inconsistent (e.g. file_mtime predates trial start).
        """
        trial_start = MetricsAggregator._trial_start_epoch(trial_info)
        if trial_start is None:
            return None

        store_path = Path(trial_info.trial_dir) / _POV_STORE_RELPATH
        if not store_path.is_file():
            return None
        try:
            data = json.loads(store_path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"Unreadable pov_store.json {store_path}: {e}")
            return None

        candidates: list[float] = []

        cpv_to_first = data.get("cpv_to_first_pov") or {}
        for entry in cpv_to_first.values():
            ts = entry.get("discovery_ts") if isinstance(entry, dict) else None
            if isinstance(ts, (int, float)):
                candidates.append(float(ts))

        if not candidates:
            povs = data.get("povs") or {}
            for entry in povs.values():
                if not isinstance(entry, dict):
                    continue
                mt = entry.get("file_mtime")
                if isinstance(mt, (int, float)):
                    candidates.append(float(mt))

        if not candidates:
            return None

        first_pov_ts = min(candidates)
        delta = first_pov_ts - trial_start
        if delta < 0:
            # Likely retried/resumed run with a stale metadata timestamp.
            # Decline to claim a value rather than emit a negative time.
            logger.warning(
                f"first PoV mtime predates trial start at {trial_info.trial_dir}; "
                f"refusing to emit negative time_to_first_pov"
            )
            return None
        return delta

    @staticmethod
    def _trial_start_epoch(trial_info: TrialInfo) -> float | None:
        """Return trial start time as a Unix epoch.

        Reads ``metadata.timestamp`` from the loaded TrialInfo if present;
        otherwise re-reads ``metadata.json`` from disk. Returns None when no
        usable timestamp can be parsed.
        """
        ts = getattr(getattr(trial_info, "metadata", None), "timestamp", None)
        if ts is None:
            meta_path = Path(trial_info.trial_dir) / "metadata.json"
            if not meta_path.is_file():
                return None
            try:
                ts = json.loads(meta_path.read_text()).get("timestamp")
            except (OSError, json.JSONDecodeError):
                return None
        if ts is None:
            return None
        try:
            if isinstance(ts, (int, float)):
                return float(ts)
            return datetime.fromisoformat(str(ts)).timestamp()
        except (ValueError, TypeError):
            return None

    def _compute_time_series(
        self, snapshots: list[SnapshotData]
    ) -> list[TimeSeriesPoint]:
        """Compute time-series data from snapshots.

        For snapshots with running_elapsed_time = 0 or None (build phase),
        only keep the one with the largest elapsed_time (last snapshot before
        CRS started running). Keep all snapshots with running_elapsed_time > 0.

        Args:
            snapshots: Sorted list of snapshots

        Returns:
            List of time series points
        """
        if not snapshots:
            return []

        # Separate snapshots into two groups:
        # 1. Snapshots with running_elapsed_time = 0 or None (build phase)
        # 2. Snapshots with running_elapsed_time > 0 (CRS running phase)
        build_snapshots = [
            s
            for s in snapshots
            if s.running_elapsed_time is None or s.running_elapsed_time == 0
        ]
        running_snapshots = [
            s
            for s in snapshots
            if s.running_elapsed_time is not None and s.running_elapsed_time > 0
        ]

        # Among build snapshots, only keep the one with largest elapsed_time
        # (the last snapshot before CRS started running)
        filtered_snapshots = []
        if build_snapshots:
            last_build_snapshot = max(build_snapshots, key=lambda s: s.elapsed_time)
            filtered_snapshots.append(last_build_snapshot)
        filtered_snapshots.extend(running_snapshots)

        # Sort by elapsed_time to ensure correct order
        filtered_snapshots.sort(key=lambda s: s.elapsed_time)

        # Create a set of filtered snapshot cycles for quick lookup
        filtered_cycles = {s.cycle for s in filtered_snapshots}

        # Compute cumulative counts from ALL snapshots (not just filtered ones)
        # But only create TimeSeriesPoint entries for filtered snapshots
        time_series: list[TimeSeriesPoint] = []
        cumulative_povs = 0
        cumulative_patches = 0

        for snapshot in snapshots:
            cumulative_povs += snapshot.pov_count
            cumulative_patches += snapshot.patch_count

            # Only create time series point for filtered snapshots
            if snapshot.cycle in filtered_cycles:
                running_time = snapshot.running_elapsed_time or 0.0

                point = TimeSeriesPoint(
                    elapsed_time=snapshot.elapsed_time,
                    running_elapsed_time=running_time,
                    cumulative_povs=cumulative_povs,
                    cumulative_patches=cumulative_patches,
                    llm_tokens=(
                        snapshot.llm_usage.total_input_tokens
                        + snapshot.llm_usage.total_output_tokens
                    ),
                    llm_input_tokens=snapshot.llm_usage.total_input_tokens,
                    llm_output_tokens=snapshot.llm_usage.total_output_tokens,
                    llm_cost=snapshot.llm_usage.total_cost_usd,
                )
                time_series.append(point)

        return time_series

    def _group_by_crs(
        self, trial_metrics_list: list[TrialMetrics]
    ) -> dict[str, CRSMetrics]:
        """Group trial metrics by CRS.

        Args:
            trial_metrics_list: List of trial metrics

        Returns:
            Dict mapping CRS name to aggregated metrics
        """
        crs_groups: dict[str, list[TrialMetrics]] = defaultdict(list)
        for m in trial_metrics_list:
            crs_groups[m.crs].append(m)

        result: dict[str, CRSMetrics] = {}
        for crs, metrics in crs_groups.items():
            trials_count = len(metrics)
            result[crs] = CRSMetrics(
                crs=crs,
                trial_count=trials_count,
                avg_povs=sum(m.total_povs_discovered for m in metrics) / trials_count,
                avg_patches=sum(m.total_patches_generated for m in metrics)
                / trials_count,
                avg_cost=sum(m.total_llm_cost for m in metrics) / trials_count,
                total_cost=sum(m.total_llm_cost for m in metrics),
                total_povs=sum(m.total_povs_discovered for m in metrics),
            )

        return result

    def _group_by_benchmark(
        self, trial_metrics_list: list[TrialMetrics]
    ) -> dict[str, BenchmarkMetrics]:
        """Group trial metrics by benchmark.

        Args:
            trial_metrics_list: List of trial metrics

        Returns:
            Dict mapping benchmark name to aggregated metrics
        """
        benchmark_groups: dict[str, list[TrialMetrics]] = defaultdict(list)
        for m in trial_metrics_list:
            benchmark_groups[m.benchmark].append(m)

        result: dict[str, BenchmarkMetrics] = {}
        for benchmark, metrics in benchmark_groups.items():
            trials_count = len(metrics)

            # Compute average time to first POV
            times = [m.time_to_first_pov for m in metrics if m.time_to_first_pov]
            avg_time_to_first = sum(times) / len(times) if times else None

            result[benchmark] = BenchmarkMetrics(
                benchmark=benchmark,
                trial_count=trials_count,
                avg_povs=sum(m.total_povs_discovered for m in metrics) / trials_count,
                avg_patches=sum(m.total_patches_generated for m in metrics)
                / trials_count,
                avg_time_to_first_pov=avg_time_to_first,
                total_cost=sum(m.total_llm_cost for m in metrics),
            )

        return result
