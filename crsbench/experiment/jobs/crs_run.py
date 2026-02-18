"""CRS execution job with internal periodic verification.

This module provides CRSRunJob, which encapsulates the complete CRS trial
lifecycle including:
- Retrieving build results from shared context
- Running SnapshotManager for periodic capture
- POV verification via on_snapshot callbacks
- Early termination when all CPVs found via stop_event
"""

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from crsbench.benchmark_ci.jobs.base import Job, JobContext, JobResult
from crsbench.evaluation.adapter import OssCrsAdapter
from crsbench.evaluation.snapshot_manager import SnapshotManager
from crsbench.evaluation.verification.pov.config import POVVerificationConfig
from crsbench.evaluation.verification.pov.manager import POVVerificationManager
from crsbench.utils.logger import get_logger
from crsbench.validation.meta_adapter import MetaYamlAdapter

logger = get_logger(__name__)


@dataclass
class CRSRunJob(Job):
    """CRS execution job with internal periodic verification.

    Encapsulates the complete CRS trial lifecycle:
    1. Retrieve build results from context.shared
    2. Start SnapshotManager for periodic capture
    3. Run CRS with stop_event for early termination
    4. POVVerificationManager verifies via on_snapshot callbacks
    5. Stop when all CPVs found or timeout reached

    For bug-fixing CRS: Skip internal verification (defer to post-trial).
    Check crs_type and skip POVVerificationManager setup.
    """

    # Required fields
    crs_config_name: str
    benchmark_path: Path
    harness_name: str
    trial_num: int
    trial_output_dir: Path

    # CRS executor configuration
    oss_fuzz_path: Path
    registry_dir: Path
    benchmarks_root: Path
    crs_configs_dir: Path

    # Optional configuration
    run_timeout: int = 7200
    snapshot_interval: int = 300
    build_job_id: str = ""
    litellm_mode: Optional[str] = "passthrough"
    crs_type: str = "bug-finding"  # "bug-finding" or "bug-fixing"
    early_stop_enabled: bool = True
    executor_config: dict[str, Any] = field(default_factory=dict)

    @property
    def job_id(self) -> str:
        """Unique identifier for this job."""
        return (
            f"crs-run:{self.benchmark_path.name}:{self.harness_name}:{self.trial_num}"
        )

    @property
    def job_type(self) -> str:
        """Type of job."""
        return "crs_run"

    @property
    def depends_on(self) -> list[str]:
        """Job IDs this job depends on."""
        return [self.build_job_id] if self.build_job_id else []

    def execute(self, context: JobContext) -> JobResult:
        """Execute CRS with internal periodic verification.

        Args:
            context: Shared context with builder and configuration

        Returns:
            JobResult with success status, timing, and details
        """
        started_at = datetime.now()
        trial_start_time = time.time()

        try:
            # 1. Get build results from context.shared if build_job_id set
            adapter = self._get_adapter(context)
            if adapter is None:
                raise ValueError(
                    f"Could not load adapter for benchmark: {self.benchmark_path}"
                )

            # 2. Ensure trial output directory exists
            self.trial_output_dir.mkdir(parents=True, exist_ok=True)

            # 3. Create stop_event for early termination
            stop_event = threading.Event()

            # 4. Get expected CPV IDs for this harness
            expected_cpv_ids = self._get_expected_cpv_ids(adapter)

            # 5. Setup POV verification manager (bug-finding only)
            pov_verification_manager = None
            if self.crs_type == "bug-finding" and expected_cpv_ids:
                pov_output_dir = self.trial_output_dir / "output" / "povs"
                pov_config = POVVerificationConfig(
                    early_stop_enabled=self.early_stop_enabled
                )
                pov_verification_manager = POVVerificationManager(
                    trial_dir=self.trial_output_dir,
                    pov_output_dir=pov_output_dir,
                    config=pov_config,
                    harness_name=self.harness_name,
                    benchmark_id=self.benchmark_path.name,
                    expected_cpv_ids=expected_cpv_ids,
                    trial_start_time=trial_start_time,
                    stop_event=stop_event,
                    adapter=adapter,
                )
                logger.info(
                    f"POVVerificationManager initialized with {len(expected_cpv_ids)} expected CPVs"
                )

            # 6. Create SnapshotManager
            snapshot_manager = SnapshotManager(
                trial_dir=self.trial_output_dir,
                snapshot_period=self.snapshot_interval,
                trial_start_time=trial_start_time,
                pov_verification_manager=pov_verification_manager,
            )

            # 7. Create CRS adapter
            crs_adapter = OssCrsAdapter(
                crs_config_name=self.crs_config_name,
                oss_fuzz_path=self.oss_fuzz_path,
                registry_dir=self.registry_dir,
                benchmarks_root=self.benchmarks_root,
                crs_configs_dir=self.crs_configs_dir,
                litellm_mode=self.litellm_mode or "passthrough",
                mode=self.crs_type,
            )

            # Configure adapter with merged config
            executor_config = {
                "run_timeout": self.run_timeout,
                **self.executor_config,
            }
            crs_adapter.configure(executor_config)

            # 8. Get harness from adapter
            harness = adapter.get_harness(self.harness_name)
            if harness is None:
                raise ValueError(f"Harness not found: {self.harness_name}")

            # 9. Start SnapshotManager thread
            snapshot_thread = threading.Thread(target=snapshot_manager.run, daemon=True)
            snapshot_thread.start()
            logger.info("SnapshotManager thread started")

            # 10. Define callbacks for CRS execution
            def on_run_start() -> None:
                """Called when CRS run starts (after build)."""
                run_start = time.time()
                snapshot_manager.set_crs_run_start_time(run_start)
                if pov_verification_manager:
                    pov_verification_manager.set_crs_run_start_time(run_start)
                logger.info("CRS run started, snapshot timing reset")

            # 11. Run CRS with stop_event
            crs_result = crs_adapter.run(
                benchmark_path=self.benchmark_path,
                harness=harness,
                trial_output_dir=self.trial_output_dir,
                on_run_start=on_run_start,
                stop_event=stop_event,
            )

            # 12. Stop SnapshotManager
            snapshot_manager.stop()
            snapshot_thread.join(timeout=5.0)
            logger.info("SnapshotManager stopped")

            # 13. Collect results
            cpvs_found: list[str] = []
            early_termination = False

            if pov_verification_manager:
                cpvs_found = list(pov_verification_manager.found_cpvs)
                early_termination = pov_verification_manager._early_stop_triggered

            # 14. Build result
            finished_at = datetime.now()
            elapsed = (finished_at - started_at).total_seconds()

            success = crs_result.success
            error = crs_result.error if not success else None

            result = JobResult(
                job_id=self.job_id,
                job_type=self.job_type,
                success=success,
                started_at=started_at,
                finished_at=finished_at,
                elapsed_seconds=elapsed,
                logs=crs_result.output or "",
                error=error,
                details={
                    "benchmark": self.benchmark_path.name,
                    "harness": self.harness_name,
                    "trial_num": self.trial_num,
                    "crs_type": self.crs_type,
                    "cpvs_found": cpvs_found,
                    "cpvs_expected": len(expected_cpv_ids),
                    "early_termination": early_termination,
                    "crs_execution_time": crs_result.execution_time,
                    "crs_timed_out": crs_result.timed_out,
                    "snapshots_captured": snapshot_manager.cycle,
                },
            )
            self._write_job_log(context, result)
            return result

        except Exception as e:
            finished_at = datetime.now()
            elapsed = (finished_at - started_at).total_seconds()
            logger.error(f"CRSRunJob failed: {e}", exc_info=True)

            result = JobResult(
                job_id=self.job_id,
                job_type=self.job_type,
                success=False,
                started_at=started_at,
                finished_at=finished_at,
                elapsed_seconds=elapsed,
                error=str(e),
            )
            self._write_job_log(context, result)
            return result

    def _get_adapter(self, context: JobContext) -> Optional[MetaYamlAdapter]:
        """Get MetaYamlAdapter from context or load from benchmark path.

        Args:
            context: Job context with shared data

        Returns:
            MetaYamlAdapter or None if loading fails
        """
        # Try to get from shared context if build_job_id is set
        if self.build_job_id:
            build_data = context.shared.get(self.build_job_id, {})
            adapter = build_data.get("adapter")
            if adapter:
                return adapter

        # Fall back to loading from benchmark path
        return MetaYamlAdapter.from_benchmark_path(self.benchmark_path)

    def _get_expected_cpv_ids(self, adapter: MetaYamlAdapter) -> list[str]:
        """Get expected CPV IDs for this harness.

        Args:
            adapter: MetaYamlAdapter for the benchmark

        Returns:
            List of CPV IDs (e.g., ["cpv_0", "cpv_1"])
        """
        harness = adapter.get_harness(self.harness_name)
        if not harness or not harness.vulns:
            return []

        cpv_ids = []
        for vuln in harness.vulns:
            if vuln.vuln_keyword.startswith("cpv_"):
                cpv_ids.append(vuln.vuln_keyword)

        return cpv_ids
