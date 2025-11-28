"""Snapshot archive builder for bench_snapgen.

Builds snapshot archives from discovery timeline events, following the
same incremental capture strategy as runtime SnapshotManager.
"""

import json
import shutil
import tarfile
import time
from datetime import datetime
from pathlib import Path
from typing import List, Set

from crsbench.bench_snapgen.timeline import DiscoveryEvent, DiscoveryTimeline
from crsbench.evaluation.snapshot import SnapshotMetadata
from crsbench.utils.logger import get_logger

logger = get_logger(__name__)


class SnapshotBuilder:
    """Build snapshot archives from discovery timeline.

    Implements incremental tracking for POVs and patches, while capturing
    full logs and config at each snapshot (matching runtime SnapshotManager).

    Attributes:
        output_dir: Directory for snapshot archives
        captured_povs: Set of POV IDs already captured
        captured_patches: Set of patch paths already captured
    """

    def __init__(self, output_dir: Path):
        """Initialize snapshot builder.

        Args:
            output_dir: Directory to write snapshot archives
        """
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # Incremental tracking
        self.captured_povs: Set[str] = set()
        self.captured_patches: Set[str] = set()

        # Simulation parameters (set by generator)
        self.trial_start_time = time.time()
        self.snapshot_period = 900

    def build_snapshot(
        self,
        cycle: int,
        elapsed_time: float,
        timeline: DiscoveryTimeline,
        benchmark_name: str = "unknown",
        crs_name: str = "simulated-crs",
    ) -> Path:
        """Build a single snapshot archive.

        Args:
            cycle: Snapshot cycle number (1-indexed)
            elapsed_time: Time elapsed since trial start
            timeline: Discovery timeline with all events
            benchmark_name: Benchmark name for metadata
            crs_name: CRS name for metadata

        Returns:
            Path to created snapshot archive

        Raises:
            IOError: If snapshot creation fails
        """
        logger.info(f"Building snapshot {cycle:04d} (elapsed: {elapsed_time:.1f}s)")

        # Create temp directory for snapshot contents
        temp_dir = self.output_dir / f".snapshot-{cycle:04d}"
        temp_dir.mkdir(exist_ok=True)

        try:
            # Get events up to current time
            events = timeline.get_events_before(elapsed_time)

            # Write snapshot contents
            self._write_metadata(temp_dir, cycle, elapsed_time)
            self._write_config(temp_dir)
            self._write_execution_metadata(temp_dir, benchmark_name, crs_name)
            self._write_llm_usage(temp_dir, events)
            self._write_crs_log(temp_dir, events, elapsed_time)

            # Write incremental data (only new items)
            new_pov_count = self._write_incremental_povs(temp_dir, events)
            new_patch_count = self._write_incremental_patches(temp_dir, events)

            # Compress to tar.gz
            archive_path = self.output_dir / f"snapshot-{cycle:04d}.tar.gz"
            self._create_tar_gz(temp_dir, archive_path)

            # Create completion marker
            marker_path = self.output_dir / f"snapshot-{cycle:04d}.complete"
            marker_path.touch()

            logger.info(
                f"Snapshot {cycle:04d} completed: {archive_path.name} "
                f"({new_pov_count} new POVs, {new_patch_count} new patches)"
            )

            return archive_path

        finally:
            # Cleanup temp directory
            if temp_dir.exists():
                shutil.rmtree(temp_dir, ignore_errors=True)

    def _write_metadata(self, temp_dir: Path, cycle: int, elapsed_time: float):
        """Write snapshot metadata.json."""
        metadata = SnapshotMetadata(
            cycle=cycle,
            timestamp=time.time(),
            elapsed_time=elapsed_time,
            snapshot_period=self.snapshot_period,
        )

        metadata_path = temp_dir / "metadata.json"
        metadata_path.write_text(metadata.to_json())

    def _write_config(self, temp_dir: Path):
        """Write experiment config.yaml (simulated)."""
        # Generate minimal config for demonstration
        config_content = f"""experiment: simulated-trial
trials: 1
max_total_time: 7200
difficulty_level: 1
snapshot_period: {self.snapshot_period}
experiment_filestore: /tmp/experiments
report_filestore: /tmp/reports
crses:
  - simulated-crs
benchmark_suite: crsbench-simulated
"""
        config_path = temp_dir / "config.yaml"
        config_path.write_text(config_content)

    def _write_execution_metadata(
        self, temp_dir: Path, benchmark_name: str, crs_name: str
    ):
        """Write execution.json metadata."""
        execution = {
            "trial_id": f"simulated-trial-{int(time.time())}",
            "benchmark": benchmark_name,
            "crs": crs_name,
            "mode": "bug-finding",
            "started_at": datetime.fromtimestamp(self.trial_start_time).isoformat(),
            "timeout": 7200,
            "docker_image": f"gcr.io/oss-fuzz-base/{crs_name}:latest",
        }

        execution_path = temp_dir / "execution.json"
        execution_path.write_text(json.dumps(execution, indent=2))

    def _write_llm_usage(self, temp_dir: Path, events: List[DiscoveryEvent]):
        """Write llm-usage.json (simulated cumulative metrics)."""
        # Generate realistic LLM usage correlated with discoveries
        pov_count = len([e for e in events if e.event_type == "pov"])
        patch_count = len([e for e in events if e.event_type == "patch"])

        # Realistic token counts
        base_tokens = 10000  # Base analysis
        tokens_per_pov = 5000  # POV analysis
        tokens_per_patch = 15000  # Patch generation

        total_tokens = (
            base_tokens + (pov_count * tokens_per_pov) + (patch_count * tokens_per_patch)
        )

        llm_usage = {
            "total_api_calls": pov_count * 10 + patch_count * 20,
            "total_input_tokens": int(total_tokens * 0.7),
            "total_output_tokens": int(total_tokens * 0.3),
            "total_cached_tokens": int(total_tokens * 0.4),
            "total_cost_usd": round(total_tokens * 0.00003, 4),
            "by_model": {
                "claude-sonnet-4": {
                    "calls": int((pov_count * 10 + patch_count * 20) * 0.6),
                    "input_tokens": int(total_tokens * 0.42),
                    "output_tokens": int(total_tokens * 0.18),
                    "cost_usd": round(total_tokens * 0.00002, 4),
                },
                "gpt-4": {
                    "calls": int((pov_count * 10 + patch_count * 20) * 0.4),
                    "input_tokens": int(total_tokens * 0.28),
                    "output_tokens": int(total_tokens * 0.12),
                    "cost_usd": round(total_tokens * 0.00001, 4),
                },
            },
            "by_operation": {
                "fuzzing": {
                    "calls": int((pov_count * 10) * 0.8),
                    "tokens": int(total_tokens * 0.4),
                },
                "static_analysis": {
                    "calls": int((pov_count * 10) * 0.2),
                    "tokens": int(total_tokens * 0.2),
                },
                "patch_generation": {
                    "calls": patch_count * 20,
                    "tokens": patch_count * tokens_per_patch,
                },
            },
        }

        llm_usage_path = temp_dir / "llm-usage.json"
        llm_usage_path.write_text(json.dumps(llm_usage, indent=2))

    def _write_crs_log(
        self, temp_dir: Path, events: List[DiscoveryEvent], elapsed_time: float
    ):
        """Write crs-output.log (simulated CRS log showing discoveries)."""
        log_lines = [
            f"[{self._format_timestamp(0)}] INFO: CRS starting up",
            f"[{self._format_timestamp(5)}] INFO: Initializing fuzzing engine",
            f"[{self._format_timestamp(10)}] INFO: Loading target harness",
            f"[{self._format_timestamp(15)}] INFO: Starting fuzzing campaign",
        ]

        # Add log entries for each discovery
        for event in sorted(events, key=lambda e: e.timestamp):
            time_str = self._format_timestamp(event.timestamp)

            if event.event_type == "pov":
                pov_id = event.metadata.get("pov_id", "unknown")
                sanitizer = event.metadata.get("sanitizer", "unknown")
                vuln = event.metadata.get("vuln", "unknown")

                log_lines.extend(
                    [
                        f"{time_str} INFO: Generated 1000 test cases",
                        f"{time_str} INFO: Found crash: {sanitizer} in {vuln}",
                        f"{time_str} INFO: Analyzing crash with LLM",
                        f"{time_str} INFO: Generated POV candidate {pov_id}",
                        f"{time_str} INFO: Validating POV {pov_id}",
                    ]
                )

            elif event.event_type == "patch":
                patch_id = event.metadata.get("patch_id", "unknown")
                vuln = event.metadata.get("vuln", "unknown")

                log_lines.extend(
                    [
                        f"{time_str} INFO: Analyzing vulnerability root cause for {vuln}",
                        f"{time_str} INFO: Generating patch with LLM",
                        f"{time_str} INFO: Created patch {patch_id}",
                        f"{time_str} INFO: Testing patch {patch_id}",
                    ]
                )

        # Add current status
        log_lines.append(
            f"[{self._format_timestamp(elapsed_time)}] INFO: Elapsed time: {elapsed_time:.1f}s"
        )
        log_lines.append(
            f"[{self._format_timestamp(elapsed_time)}] INFO: Continuing operation..."
        )

        log_path = temp_dir / "crs-output.log"
        log_path.write_text("\n".join(log_lines) + "\n")

    def _write_incremental_povs(
        self, temp_dir: Path, events: List[DiscoveryEvent]
    ) -> int:
        """Write incremental POVs (only new ones not in previous snapshots).

        Returns:
            Number of new POVs written
        """
        pov_dir = temp_dir / "povs"
        new_count = 0

        for event in events:
            if event.event_type != "pov":
                continue

            pov_id = event.metadata.get("pov_id", "unknown")
            if pov_id in self.captured_povs:
                continue  # Already captured in previous snapshot

            # Write new POV
            pov_dir.mkdir(exist_ok=True)
            pov_file = pov_dir / pov_id
            pov_file.write_bytes(event.data)
            self.captured_povs.add(pov_id)
            new_count += 1

        return new_count

    def _write_incremental_patches(
        self, temp_dir: Path, events: List[DiscoveryEvent]
    ) -> int:
        """Write incremental patches (only new ones, organized by vuln).

        Returns:
            Number of new patches written
        """
        patches_dir = temp_dir / "patches"
        new_count = 0

        for event in events:
            if event.event_type != "patch":
                continue

            vuln = event.metadata.get("vuln", "unknown")
            patch_id = event.metadata.get("patch_id", "unknown")
            patch_key = f"{vuln}/{patch_id}"

            if patch_key in self.captured_patches:
                continue  # Already captured

            # Create vuln subdirectory
            vuln_patch_dir = patches_dir / vuln
            vuln_patch_dir.mkdir(parents=True, exist_ok=True)

            # Write patch
            patch_file = vuln_patch_dir / f"{patch_id}.diff"
            patch_file.write_text(event.data.decode("utf-8"))
            self.captured_patches.add(patch_key)
            new_count += 1

        return new_count

    def _format_timestamp(self, seconds_offset: float) -> str:
        """Format timestamp for log entries.

        Args:
            seconds_offset: Seconds since trial start

        Returns:
            Formatted timestamp string
        """
        timestamp = self.trial_start_time + seconds_offset
        dt = datetime.fromtimestamp(timestamp)
        return dt.strftime("[%Y-%m-%d %H:%M:%S]")

    def _create_tar_gz(self, source_dir: Path, archive_path: Path):
        """Compress snapshot directory to tar.gz archive.

        Args:
            source_dir: Directory containing snapshot files
            archive_path: Path to output tar.gz file
        """
        with tarfile.open(archive_path, "w:gz") as tar:
            for item in source_dir.rglob("*"):
                if item.is_file():
                    arcname = item.relative_to(source_dir)
                    tar.add(item, arcname=arcname)

        logger.debug(f"Created archive: {archive_path.name}")
