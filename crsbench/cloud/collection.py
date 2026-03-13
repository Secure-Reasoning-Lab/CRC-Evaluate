"""Rsync-based artifact collector for GCE workers.

Implements a stage-then-publish pattern:
1. rsync trial artifacts from worker into a staging directory
2. verify the staged tree contains valid trials (metadata.json sentinel)
3. publish by merging staging into the final experiment_filestore path
4. clean up staging

Covers ARTF-01 through ARTF-04.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import tenacity

from crsbench.utils.logger import get_logger

if TYPE_CHECKING:
    from crsbench.cloud.gce.models import GceWorkerRecord
    from crsbench.validation.schemas import GceWorkerFleetConfig

logger = get_logger(__name__)


class ArtifactCollectionError(Exception):
    """Raised when artifact collection fails verification or publication."""


class ArtifactCollector:
    """Collect trial artifacts from a GCE worker via rsync.

    The collector is stateless; all parameters are passed directly to methods.
    """

    def collect(
        self,
        worker: GceWorkerRecord,
        fleet: GceWorkerFleetConfig,
        experiment_name: str,
        experiment_filestore: Path,
        remote_experiment_dir: str,
    ) -> Path:
        """Rsync trial artifacts from *worker* and publish them to *experiment_filestore*.

        Args:
            worker: GCE worker instance record (provides name, zone, IP).
            fleet: Fleet configuration (provides project, ssh_via_iap).
            experiment_name: Name of the experiment (used to build the final path).
            experiment_filestore: Orchestrator-local directory where experiments live.
            remote_experiment_dir: Absolute path on the worker containing the experiment tree.

        Returns:
            Path to the final experiment directory (``experiment_filestore / experiment_name``).

        Raises:
            ArtifactCollectionError: If the staged tree fails verification or rsync fails.
        """
        staging_dir = (
            experiment_filestore / ".collect-staging" / worker.name / experiment_name
        )

        # Clear any stale staging from a prior interrupted run
        if staging_dir.exists():
            logger.debug("Removing stale staging dir: %s", staging_dir)
            shutil.rmtree(staging_dir)
        staging_dir.mkdir(parents=True, exist_ok=True)

        cmd = self._build_rsync_cmd(
            worker=worker,
            fleet=fleet,
            remote_experiment_dir=remote_experiment_dir,
            staging_dir=staging_dir,
        )

        self._run_rsync_with_retry(cmd)

        # Verify before publishing
        self._verify_staging(staging_dir)

        final_dir = experiment_filestore / experiment_name
        self._publish(staging_dir, final_dir)

        # Clean up the per-worker staging parent
        worker_staging = experiment_filestore / ".collect-staging" / worker.name
        if worker_staging.exists():
            shutil.rmtree(worker_staging, ignore_errors=True)

        logger.info(
            "Artifact collection complete: worker=%s experiment=%s final_dir=%s",
            worker.name,
            experiment_name,
            final_dir,
        )
        return final_dir

    def _run_rsync_with_retry(self, cmd: list[str]) -> None:
        """Run the rsync command with exponential-backoff retry."""

        @tenacity.retry(
            retry=tenacity.retry_if_exception_type(subprocess.CalledProcessError),
            wait=tenacity.wait_exponential(multiplier=1, min=2, max=30),
            stop=tenacity.stop_after_attempt(3),
            reraise=True,
        )
        def _run() -> None:
            subprocess.run(cmd, check=True)

        _run()

    def _build_rsync_cmd(
        self,
        worker: GceWorkerRecord,
        fleet: GceWorkerFleetConfig,
        remote_experiment_dir: str,
        staging_dir: Path,
    ) -> list[str]:
        """Return the full rsync command as a list of strings.

        Flags used:
        - ``-a``: archive mode (preserves mtimes, permissions, symlinks — ARTF-02)
        - ``--mkpath``: create destination dirs as needed (rsync 3.2+)
        - ``--partial-dir=.rsync-partial``: job-local partial dir (no cross-job collisions)
        - ``--delay-updates``: stage all files before renaming into place
        - ``--delete-delay``: remove remote-deleted files after transfer completes
        """
        ssh_cmd = self._build_ssh_command(worker, fleet)

        if fleet.ssh_via_iap:
            remote_host = worker.name
        else:
            remote_host = worker.internal_ip or worker.name

        source = f"{remote_host}:{remote_experiment_dir}/"
        dest = str(staging_dir) + "/"

        return [
            "rsync",
            "-a",
            "--mkpath",
            "--partial-dir=.rsync-partial",
            "--delay-updates",
            "--delete-delay",
            "-e",
            ssh_cmd,
            source,
            dest,
        ]

    def _build_ssh_command(
        self,
        worker: GceWorkerRecord,
        fleet: GceWorkerFleetConfig,
    ) -> str:
        """Return the ``-e`` argument string for rsync SSH transport.

        For IAP:  ``gcloud compute ssh INSTANCE --project=P --zone=Z --tunnel-through-iap -- -W %h:%p``
        For direct-IP: ``ssh -o BatchMode=yes -o StrictHostKeyChecking=yes``
        """
        if fleet.ssh_via_iap:
            zone = worker.zone or fleet.zone or ""
            return (
                f"gcloud compute ssh {worker.name}"
                f" --project={fleet.project}"
                f" --zone={zone}"
                f" --tunnel-through-iap"
                f" -- -W %h:%p"
            )
        return "ssh -o BatchMode=yes -o StrictHostKeyChecking=yes"

    def _verify_staging(self, staging_dir: Path) -> None:
        """Verify the staged tree contains at least one valid trial.

        Uses ``discover_trials`` as the sentinel check: a valid trial must have
        ``metadata.json`` present (status == "valid"). Raises
        ``ArtifactCollectionError`` if no valid trials are found.
        """
        from crsbench.reporting.snapshot_loader import discover_trials

        all_trials = discover_trials(staging_dir)
        valid_trials = [t for t in all_trials if t.status == "valid"]
        if not valid_trials:
            raise ArtifactCollectionError(
                f"No valid trials in staged tree: {staging_dir} "
                f"(found {len(all_trials)} trial dirs, none with valid metadata.json). "
                "Collection aborted — final path not written."
            )
        logger.info(
            "Staged tree verified: %d valid trial(s) found in %s",
            len(valid_trials),
            staging_dir,
        )

    def _publish(self, staging_dir: Path, final_dir: Path) -> None:
        """Merge *staging_dir* contents into *final_dir* and remove staging.

        Uses ``shutil.copytree`` with ``dirs_exist_ok=True`` so that incremental
        collections (multiple workers, multiple runs) merge correctly into the
        same final experiment directory.
        """
        final_dir.mkdir(parents=True, exist_ok=True)
        shutil.copytree(staging_dir, final_dir, dirs_exist_ok=True)
        shutil.rmtree(staging_dir)
        logger.debug("Published staging %s -> %s", staging_dir, final_dir)
