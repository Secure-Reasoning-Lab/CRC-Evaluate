from __future__ import annotations

import hashlib
import shutil
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager
from typing import TYPE_CHECKING, Callable

import yaml

from crsbench.evaluation.replay.models import ReplayTask, SessionReplayResult

if TYPE_CHECKING:
    from pathlib import Path


def _base_runner_image_for_project(project_dir: Path) -> str:
    """Resolve the base-runner image tag for one OSS-Fuzz project."""
    tag = "latest"
    project_yaml = project_dir / "project.yaml"
    if project_yaml.is_file():
        data = yaml.safe_load(project_yaml.read_text(encoding="utf-8")) or {}
        base_os_version = data.get("base_os_version")
        if isinstance(base_os_version, str) and base_os_version:
            tag = base_os_version
    return f"gcr.io/oss-fuzz-base/base-runner:{tag}"


class WarmReplaySession(AbstractContextManager["WarmReplaySession"]):
    """Long-lived base-runner container reused for many replay execs."""

    def __init__(
        self,
        *,
        project_name: str,
        project_dir: Path,
        build_output_dir: Path,
        output_dir: Path,
        session_label: str | None = None,
    ) -> None:
        self.project_name = project_name
        self.project_dir = project_dir.resolve()
        self.build_output_dir = build_output_dir.resolve()
        self.output_dir = output_dir.resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        label = session_label or uuid.uuid4().hex[:8]
        self.workspace = self.output_dir / f".session-{label}"
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.testcase_dir = self.workspace / "testcases"
        self.testcase_dir.mkdir(parents=True, exist_ok=True)
        self.runtime_image = _base_runner_image_for_project(self.project_dir)
        self.container_name = (
            f"crsbench-replay-{self.project_name[:20]}-{uuid.uuid4().hex[:10]}"
        )
        self._container_running = False
        self._start_container()

    def _start_container(self) -> None:
        subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--privileged",
                "--shm-size=2g",
                "--name",
                self.container_name,
                "-e",
                "HELPER=True",
                "-e",
                "ARCHITECTURE=x86_64",
                "-v",
                f"{self.build_output_dir}:/out",
                "-v",
                f"{self.workspace}:/workspace",
                self.runtime_image,
                "bash",
                "-lc",
                "mkdir -p /workspace/testcases && tail -f /dev/null",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        self._container_running = True

    def _remove_container(self) -> None:
        if not self._container_running:
            return
        subprocess.run(
            ["docker", "rm", "-f", self.container_name],
            check=False,
            capture_output=True,
            text=True,
        )
        self._container_running = False

    def _run_once(
        self,
        harness_name: str,
        pov_path: Path,
        timeout: int,
    ) -> SessionReplayResult:
        testcase_name = f"{hashlib.sha256(pov_path.read_bytes()).hexdigest()}.bin"
        staged_path = self.testcase_dir / testcase_name
        shutil.copy2(pov_path, staged_path)
        started = time.monotonic()
        result = subprocess.run(
            [
                "docker",
                "exec",
                "-e",
                f"TESTCASE=/workspace/testcases/{testcase_name}",
                self.container_name,
                "reproduce",
                harness_name,
                "-runs=100",
                "-detect_leaks=0",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return SessionReplayResult(
            exit_code=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            duration_seconds=time.monotonic() - started,
            timed_out=False,
            session_restarted=False,
            crashed=None,
        )

    def run(
        self, harness_name: str, pov_path: Path, timeout: int
    ) -> SessionReplayResult:
        started = time.monotonic()
        try:
            result = self._run_once(harness_name, pov_path, timeout)
        except subprocess.TimeoutExpired:
            self._remove_container()
            self._start_container()
            try:
                retried = self._run_once(harness_name, pov_path, timeout)
            except subprocess.TimeoutExpired:
                return SessionReplayResult(
                    exit_code=None,
                    stdout="",
                    stderr="",
                    duration_seconds=time.monotonic() - started,
                    timed_out=True,
                    session_restarted=True,
                    crashed=False,
                )
            except Exception as exc:
                return SessionReplayResult(
                    exit_code=None,
                    stdout="",
                    stderr="",
                    duration_seconds=time.monotonic() - started,
                    timed_out=False,
                    session_restarted=True,
                    crashed=None,
                    error_message=str(exc),
                )

            return SessionReplayResult(
                exit_code=retried.exit_code,
                stdout=retried.stdout,
                stderr=retried.stderr,
                duration_seconds=time.monotonic() - started,
                timed_out=False,
                session_restarted=True,
                crashed=retried.crashed,
                error_message=retried.error_message,
            )
        except Exception as exc:
            return SessionReplayResult(
                exit_code=None,
                stdout="",
                stderr="",
                duration_seconds=time.monotonic() - started,
                timed_out=False,
                session_restarted=False,
                crashed=None,
                error_message=str(exc),
            )

        return result

    def close(self) -> None:
        self._remove_container()
        shutil.rmtree(self.workspace, ignore_errors=True)

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        del exc_type, exc_value, traceback
        self.close()


class WarmReplaySessionPool(AbstractContextManager["WarmReplaySessionPool"]):
    """Shard replay tasks across a fixed set of warm replay sessions."""

    def __init__(self, sessions: list[WarmReplaySession]) -> None:
        if not sessions:
            raise ValueError("WarmReplaySessionPool requires at least one session")
        self.sessions = sessions

    def _session_index_for(self, task: ReplayTask) -> int:
        return int(task.pov_content_hash, 16) % len(self.sessions)

    def run_many(
        self,
        tasks: list[ReplayTask],
        timeout: int,
        on_result: Callable[[ReplayTask, SessionReplayResult], None] | None = None,
    ) -> dict[ReplayTask, SessionReplayResult]:
        if not tasks:
            return {}

        shards: list[list[ReplayTask]] = [[] for _ in self.sessions]
        for task in tasks:
            shards[self._session_index_for(task)].append(task)

        def run_shard(
            session: WarmReplaySession,
            shard: list[ReplayTask],
        ) -> dict[ReplayTask, SessionReplayResult]:
            shard_results: dict[ReplayTask, SessionReplayResult] = {}
            for task in shard:
                result = session.run(
                    task.target_harness, task.pov_path, timeout=timeout
                )
                shard_results[task] = result
                if on_result is not None:
                    on_result(task, result)
            return shard_results

        results: dict[ReplayTask, SessionReplayResult] = {}
        with ThreadPoolExecutor(max_workers=len(self.sessions)) as executor:
            futures = [
                executor.submit(run_shard, session, shard)
                for session, shard in zip(self.sessions, shards, strict=False)
                if shard
            ]
            for future in futures:
                results.update(future.result())
        return results

    def close(self) -> None:
        for session in reversed(self.sessions):
            session.close()

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        del exc_type, exc_value, traceback
        self.close()
