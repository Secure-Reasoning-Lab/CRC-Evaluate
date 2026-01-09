"""Metrics aggregation for the reporting module."""

from collections import defaultdict

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

    def aggregate_trial(
        self,
        trial_info: TrialInfo,
        snapshots: list[SnapshotData],
    ) -> TrialMetrics:
        """Aggregate metrics for a single trial.

        Args:
            trial_info: Trial information
            snapshots: List of snapshot data from the trial

        Returns:
            Aggregated trial metrics
        """
        if not snapshots:
            return TrialMetrics(
                trial_dir=str(trial_info.trial_dir),
                trial_num=trial_info.trial_num,
                crs=trial_info.crs,
                benchmark=trial_info.benchmark,
                harness=trial_info.harness,
                mode=trial_info.mode,
            )

        # Sort snapshots by cycle
        sorted_snapshots = sorted(snapshots, key=lambda s: s.cycle)

        # Collect all unique POV and patch names across snapshots
        all_pov_names: set[str] = set()
        all_patch_names: set[str] = set()
        total_povs = 0
        total_patches = 0

        for snap in sorted_snapshots:
            all_pov_names.update(snap.pov_names)
            all_patch_names.update(snap.patch_names)
            total_povs += snap.pov_count
            total_patches += snap.patch_count

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
        time_to_first_pov = self._compute_time_to_first_pov(sorted_snapshots)

        # Compute time-series data
        time_series = self._compute_time_series(sorted_snapshots)

        return TrialMetrics(
            trial_dir=str(trial_info.trial_dir),
            trial_num=trial_info.trial_num,
            crs=trial_info.crs,
            benchmark=trial_info.benchmark,
            harness=trial_info.harness,
            mode=trial_info.mode,
            total_povs_discovered=total_povs,
            unique_pov_names=sorted(all_pov_names),
            total_patches_generated=total_patches,
            unique_patch_names=sorted(all_patch_names),
            total_llm_cost=llm_usage.total_cost_usd,
            total_llm_tokens=llm_usage.total_input_tokens
            + llm_usage.total_output_tokens,
            llm_usage_by_model=llm_usage_by_model,
            total_time=total_time,
            time_to_first_pov=time_to_first_pov,
            time_series=time_series,
            snapshot_count=len(sorted_snapshots),
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

    def _compute_time_to_first_pov(self, snapshots: list[SnapshotData]) -> float | None:
        """Compute time to first POV discovery.

        Args:
            snapshots: Sorted list of snapshots

        Returns:
            Time to first POV in seconds, or None if no POVs found
        """
        for snapshot in snapshots:
            if snapshot.pov_count > 0:
                return snapshot.elapsed_time
        return None

    def _compute_time_series(
        self, snapshots: list[SnapshotData]
    ) -> list[TimeSeriesPoint]:
        """Compute time-series data from snapshots.

        Args:
            snapshots: Sorted list of snapshots

        Returns:
            List of time series points
        """
        time_series: list[TimeSeriesPoint] = []
        cumulative_povs = 0
        cumulative_patches = 0

        for snapshot in snapshots:
            cumulative_povs += snapshot.pov_count
            cumulative_patches += snapshot.patch_count

            point = TimeSeriesPoint(
                elapsed_time=snapshot.elapsed_time,
                cumulative_povs=cumulative_povs,
                cumulative_patches=cumulative_patches,
                llm_tokens=(
                    snapshot.llm_usage.total_input_tokens
                    + snapshot.llm_usage.total_output_tokens
                ),
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
