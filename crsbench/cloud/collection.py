"""Rsync-based artifact collector for GCE workers.

Implements a stage-then-publish pattern:
1. rsync trial artifacts from worker into a staging directory
2. verify the staged tree contains valid trials (metadata.json sentinel)
3. publish by merging staging into the final experiment_filestore path
4. clean up staging

Covers ARTF-01 through ARTF-04.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
from pathlib import Path  # noqa: TC003
from typing import TYPE_CHECKING, Protocol

import tenacity

from crsbench.cloud.launch_state import cloud_state_dir, remote_logs_dir
from crsbench.utils.logger import get_logger

if TYPE_CHECKING:
    from crsbench.cloud.gce.models import GceWorkerRecord


class SshTransportConfig(Protocol):
    """Minimal transport settings needed for rsync/SSH collection."""

    project: str
    zone: str | None
    ssh_via_iap: bool


logger = get_logger(__name__)


class ArtifactCollectionError(Exception):
    """Raised when artifact collection fails verification or publication."""


class ArtifactCollector:
    """Collect trial artifacts from a GCE worker via rsync.

    The collector is stateless; all parameters are passed directly to methods.
    """

    def __init__(
        self,
        *,
        base_path: Path | str | None = None,
        journal_lines: int = 2000,
    ) -> None:
        self._base_path = Path(base_path) if base_path is not None else None
        self._journal_lines = journal_lines
        self._ssh_users_by_project: dict[str, str] = {}

    def collect(
        self,
        worker: GceWorkerRecord,
        fleet: SshTransportConfig,
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
        known_hosts_path = self._prepare_ssh_access(
            worker=worker,
            fleet=fleet,
            experiment_filestore=experiment_filestore,
        )
        ssh_user = self._direct_ssh_user(fleet) if not fleet.ssh_via_iap else None
        staging_dir = (
            experiment_filestore / ".collect-staging" / worker.name / experiment_name
        )

        # Clear any stale staging from a prior interrupted run
        if staging_dir.exists():
            logger.debug("Removing stale staging dir: {}", staging_dir)
            shutil.rmtree(staging_dir)
        staging_dir.mkdir(parents=True, exist_ok=True)

        cmd = self._build_rsync_cmd(
            worker=worker,
            fleet=fleet,
            remote_experiment_dir=remote_experiment_dir,
            staging_dir=staging_dir,
            known_hosts_path=known_hosts_path,
            ssh_user=ssh_user,
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
            "Artifact collection complete: worker={} experiment={} final_dir={}",
            worker.name,
            experiment_name,
            final_dir,
        )
        return final_dir

    def collect_logs(
        self,
        worker: GceWorkerRecord,
        fleet: SshTransportConfig,
        experiment_name: str,
        experiment_filestore: Path,
        remote_experiment_dir: str,
    ) -> Path:
        """Collect remote service journals and in-progress trial logs for one VM."""
        logs_root = self._remote_logs_dir(experiment_filestore, experiment_name)
        instance_logs_dir = logs_root / worker.name
        instance_logs_dir.mkdir(parents=True, exist_ok=True)

        known_hosts_path = self._prepare_ssh_access(
            worker=worker,
            fleet=fleet,
            experiment_filestore=experiment_filestore,
        )
        ssh_user = self._direct_ssh_user(fleet) if not fleet.ssh_via_iap else None

        for destination, command in self._log_commands(worker).items():
            destination = logs_root / destination
            destination.parent.mkdir(parents=True, exist_ok=True)
            output = self._run_remote_command(
                worker=worker,
                fleet=fleet,
                known_hosts_path=known_hosts_path,
                ssh_user=ssh_user,
                command=command,
            )
            combined = output.stdout
            if output.stderr:
                combined = f"{combined}\n{output.stderr}".strip() + "\n"
            destination.write_text(combined, encoding="utf-8")

        if self._remote_path_exists(
            worker=worker,
            fleet=fleet,
            known_hosts_path=known_hosts_path,
            ssh_user=ssh_user,
            remote_path=remote_experiment_dir,
        ):
            cmd = self._build_log_rsync_cmd(
                worker=worker,
                fleet=fleet,
                remote_experiment_dir=remote_experiment_dir,
                staging_dir=instance_logs_dir / "trial-artifacts",
                known_hosts_path=known_hosts_path,
                ssh_user=ssh_user,
            )
            self._run_rsync_with_retry(cmd)

        logger.info(
            "Remote log collection complete: worker={} experiment={} logs_dir={}",
            worker.name,
            experiment_name,
            instance_logs_dir,
        )
        return logs_root

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
        fleet: SshTransportConfig,
        remote_experiment_dir: str,
        staging_dir: Path,
        known_hosts_path: Path | None = None,
        ssh_user: str | None = None,
    ) -> list[str]:
        """Return the full rsync command as a list of strings.

        Flags used:
        - ``-a``: archive mode (preserves mtimes, permissions, symlinks — ARTF-02)
        - ``--mkpath``: create destination dirs as needed (rsync 3.2+)
        - ``--partial-dir=.rsync-partial``: job-local partial dir (no cross-job collisions)
        - ``--delay-updates``: stage all files before renaming into place
        - ``--delete-delay``: remove remote-deleted files after transfer completes
        """
        if (
            known_hosts_path is None
            and not fleet.ssh_via_iap
            and self._base_path is not None
        ):
            known_hosts_path = cloud_state_dir(self._base_path) / "known_hosts"
        ssh_cmd = self._build_ssh_command(worker, fleet, known_hosts_path)

        remote_host = self._remote_host(worker, fleet)
        if ssh_user is not None and not fleet.ssh_via_iap:
            remote_host = f"{ssh_user}@{remote_host}"

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

    def _build_log_rsync_cmd(
        self,
        worker: GceWorkerRecord,
        fleet: SshTransportConfig,
        remote_experiment_dir: str,
        staging_dir: Path,
        known_hosts_path: Path | None = None,
        ssh_user: str | None = None,
    ) -> list[str]:
        """Return an rsync command that only copies lightweight trial-observability files."""
        if (
            known_hosts_path is None
            and not fleet.ssh_via_iap
            and self._base_path is not None
        ):
            known_hosts_path = cloud_state_dir(self._base_path) / "known_hosts"
        ssh_cmd = self._build_ssh_command(worker, fleet, known_hosts_path)
        remote_host = self._remote_host(worker, fleet)
        if ssh_user is not None and not fleet.ssh_via_iap:
            remote_host = f"{ssh_user}@{remote_host}"

        source = f"{remote_host}:{remote_experiment_dir}/"
        dest = str(staging_dir) + "/"

        return [
            "rsync",
            "-a",
            "--mkpath",
            "--prune-empty-dirs",
            "--include=*/",
            "--include=trial_matrix.json",
            "--include=metadata.json",
            "--include=worker.log",
            "--include=.success",
            "--include=.failure",
            "--exclude=*",
            "-e",
            ssh_cmd,
            source,
            dest,
        ]

    def _build_ssh_command(
        self,
        worker: GceWorkerRecord,
        fleet: SshTransportConfig,
        known_hosts_path: Path | None = None,
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
        parts = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "ConnectTimeout=10",
        ]
        identity_file = Path.home() / ".ssh" / "google_compute_engine"
        if identity_file.is_file():
            parts.extend(
                [
                    "-i",
                    shlex.quote(str(identity_file)),
                    "-o",
                    "IdentitiesOnly=yes",
                ]
            )
        if known_hosts_path is not None:
            parts.extend(
                [
                    "-o",
                    f"UserKnownHostsFile={shlex.quote(str(known_hosts_path))}",
                ]
            )
        return " ".join(parts)

    def _state_dir(self, experiment_filestore: Path) -> Path:
        """Return the local state directory used for SSH trust and log capture."""
        if self._base_path is not None:
            return cloud_state_dir(self._base_path)
        return experiment_filestore / ".crsbench-cloud"

    def _remote_logs_dir(
        self, experiment_filestore: Path, experiment_name: str
    ) -> Path:
        """Return the experiment-specific local remote-log directory."""
        if self._base_path is not None:
            return remote_logs_dir(self._base_path, experiment_name)
        return self._state_dir(experiment_filestore) / "remote-logs" / experiment_name

    def _known_hosts_path(self, experiment_filestore: Path) -> Path:
        """Return the local known_hosts file used for direct-IP collection."""
        return self._state_dir(experiment_filestore) / "known_hosts"

    def _remote_host(
        self,
        worker: GceWorkerRecord,
        fleet: SshTransportConfig,
    ) -> str:
        """Return the SSH/rsync host token for one worker."""
        if fleet.ssh_via_iap:
            return worker.name
        return worker.external_ip or worker.internal_ip or worker.name

    def _prepare_ssh_access(
        self,
        *,
        worker: GceWorkerRecord,
        fleet: SshTransportConfig,
        experiment_filestore: Path,
    ) -> Path | None:
        """Seed host trust for direct-IP SSH transport and return the known_hosts path."""
        if fleet.ssh_via_iap or self._base_path is None:
            return None

        known_hosts_path = self._known_hosts_path(experiment_filestore)
        known_hosts_path.parent.mkdir(parents=True, exist_ok=True)
        known_hosts_path.touch(exist_ok=True)
        known_hosts_path.chmod(0o600)

        remote_host = self._remote_host(worker, fleet)
        subprocess.run(
            [
                "ssh-keygen",
                "-R",
                remote_host,
                "-f",
                str(known_hosts_path),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        keyscan = subprocess.run(
            [
                "ssh-keyscan",
                "-T",
                "5",
                "-t",
                "ed25519",
                remote_host,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        if not keyscan.stdout.strip():
            raise ArtifactCollectionError(
                f"ssh-keyscan returned no host key for {remote_host}"
            )
        with known_hosts_path.open("a", encoding="utf-8") as handle:
            handle.write(keyscan.stdout)
        return known_hosts_path

    def _run_remote_command(
        self,
        *,
        worker: GceWorkerRecord,
        fleet: SshTransportConfig,
        known_hosts_path: Path | None,
        ssh_user: str | None,
        command: str,
    ) -> subprocess.CompletedProcess[str]:
        """Run one shell command on a remote VM and capture stdout/stderr."""
        if fleet.ssh_via_iap:
            zone = worker.zone or fleet.zone or ""
            cmd = [
                "gcloud",
                "compute",
                "ssh",
                worker.name,
                f"--project={fleet.project}",
                f"--zone={zone}",
                "--tunnel-through-iap",
                f"--command={command}",
            ]
        else:
            remote_host = self._remote_host(worker, fleet)
            ssh_target = (
                f"{ssh_user}@{remote_host}" if ssh_user is not None else remote_host
            )
            cmd = [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                "ConnectTimeout=10",
            ]
            identity_file = Path.home() / ".ssh" / "google_compute_engine"
            if identity_file.is_file():
                cmd.extend(["-i", str(identity_file), "-o", "IdentitiesOnly=yes"])
            if known_hosts_path is not None:
                cmd.extend(["-o", f"UserKnownHostsFile={str(known_hosts_path)}"])
            cmd.extend([ssh_target, command])

        return subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
        )

    def _remote_path_exists(
        self,
        *,
        worker: GceWorkerRecord,
        fleet: SshTransportConfig,
        known_hosts_path: Path | None,
        ssh_user: str | None,
        remote_path: str,
    ) -> bool:
        """Return whether the remote experiment directory exists."""
        result = self._run_remote_command(
            worker=worker,
            fleet=fleet,
            known_hosts_path=known_hosts_path,
            ssh_user=ssh_user,
            command=f"test -d {shlex.quote(remote_path)}",
        )
        return result.returncode == 0

    def _direct_ssh_user(self, fleet: SshTransportConfig) -> str | None:
        """Return the local OS Login username for direct GCE SSH, if configured."""
        if self._base_path is None:
            return None

        project = fleet.project
        cached = self._ssh_users_by_project.get(project)
        if cached is not None:
            return cached

        result = subprocess.run(
            [
                "gcloud",
                "compute",
                "os-login",
                "describe-profile",
                f"--project={project}",
                "--format=value(posixAccounts[0].username)",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        username = result.stdout.strip()
        if not username:
            raise ArtifactCollectionError(
                f"Unable to resolve OS Login username for project {project}"
            )
        self._ssh_users_by_project[project] = username
        return username

    def _service_name(self, worker: GceWorkerRecord) -> str:
        """Return the CRSBench user service name expected on the target VM."""
        if worker.labels.get("crsbench-role") == "orchestrator":
            return "crsbench-orchestrator.service"
        return "crsbench-worker.service"

    def _log_commands(self, worker: GceWorkerRecord) -> dict[Path, str]:
        """Return remote commands for service journals and runtime summaries."""
        service_name = self._service_name(worker)
        service_log_name = service_name.removesuffix(".service")
        instance_dir = Path(worker.name)
        uid_expr = 'uid="$(id -u crsbench 2>/dev/null || echo 1001)"'

        commands = {
            instance_dir / "runtime-summary.txt": (
                "set -e; "
                f"{uid_expr}; "
                'echo "hostname=$(hostname)"; '
                'echo "instance_role='
                + (
                    "orchestrator"
                    if service_name.startswith("crsbench-orchestrator")
                    else "worker"
                )
                + '"; '
                "id crsbench || true; "
                "timedatectl show --property=Timezone --value || true; "
                "docker info --format '{{.CgroupDriver}}' || true; "
                'systemctl is-active "user@${uid}.service" || true; '
                'test -S "/run/user/${uid}/bus" && echo USER_BUS=present || echo USER_BUS=missing; '
                "test -f /var/lib/systemd/linger/crsbench && echo LINGER=present || echo LINGER=missing; "
                "ss -ltnp | grep 6379 || true"
            ),
            instance_dir / "google-startup-scripts.journal.log": (
                f"sudo journalctl -u google-startup-scripts.service -b -n {self._journal_lines} --no-pager || true"
            ),
            instance_dir / "google-guest-agent.journal.log": (
                f"sudo journalctl -u google-guest-agent.service -b -n {self._journal_lines} --no-pager || true"
            ),
            instance_dir / f"{service_log_name}.journal.log": (
                f"{uid_expr}; "
                "sudo -u crsbench env "
                'XDG_RUNTIME_DIR="/run/user/${uid}" '
                'DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/${uid}/bus" '
                f"journalctl --user -u {service_name} -b -n {self._journal_lines} --no-pager || true"
            ),
        }
        if service_name == "crsbench-orchestrator.service":
            commands[instance_dir / "orchestrator.log"] = (
                "sudo cat /var/lib/crsbench/orchestrator.log || true"
            )
        return commands

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
            "Staged tree verified: {} valid trial(s) found in {}",
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
        subprocess.run(
            [
                "rsync",
                "-a",
                f"{staging_dir}/",
                f"{final_dir}/",
            ],
            check=True,
        )
        shutil.rmtree(staging_dir)
        logger.debug("Published staging {} -> {}", staging_dir, final_dir)
