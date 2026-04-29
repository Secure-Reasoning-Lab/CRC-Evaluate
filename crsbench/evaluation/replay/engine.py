from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from crsbench.builder.infrastructure import OSSFuzzInfrastructure
from crsbench.evaluation.replay.dedup import build_deduplicated_zero_day_entries
from crsbench.evaluation.replay.mapping import (
    MappingResolution,
    load_benchmark_project_mapping,
    resolve_mapped_project,
)
from crsbench.evaluation.replay.models import (
    ReplayOutcome,
    ReplayResult,
    ReplayTask,
    SessionReplayResult,
    SourcePovRecord,
)
from crsbench.evaluation.replay.projects import ensure_project_link
from crsbench.evaluation.replay.session import WarmReplaySession, WarmReplaySessionPool
from crsbench.utils.logger import get_logger

SessionPoolFactory = Callable[..., WarmReplaySessionPool]
logger = get_logger(__name__)

_GROUP_CHECKPOINT_FORMAT_VERSION = 4
_REPLAY_OUTPUT_FORMAT_VERSION = 3
_ZERO_DAY_REQUIRED_MARKER = "AddressSanitizer"
_ZERO_DAY_JAVA_EXCEPTION_MARKERS = (
    "== Java Exception:",
    'Exception in thread "',
)
_ZERO_DAY_EXCLUDED_SUBSTRINGS = (
    "out of memory",
    "out-of-memory",
    "libfuzzer: timeout",
)


@dataclass
class GroupReplayOutcome:
    entries: list[dict]
    zero_day_entries: list[dict]
    trial_entries: dict[tuple[str, str], list[dict]] = field(default_factory=dict)
    summary_updates: dict[str, int] = field(default_factory=dict)
    naive_replay_tasks: int = 0
    physical_replay_tasks: int = 0
    timing: dict[str, float | int] = field(default_factory=dict)
    phase_intervals: dict[str, dict[str, float]] = field(default_factory=dict)
    checkpoint_reused: bool = False


class ReplayEngine:
    """Replay discovered POVs against latest OSS-Fuzz targets."""

    def __init__(
        self,
        *,
        oss_fuzz_path: Path,
        projects_root: Path,
        output_dir: Path,
        jobs: int,
        group_jobs: int = 1,
        per_pov_timeout: int,
        resume: bool = False,
        infra: OSSFuzzInfrastructure | None = None,
        mapping: dict[str, str | None] | None = None,
        session_pool_factory: SessionPoolFactory | None = None,
    ) -> None:
        self.oss_fuzz_path = Path(oss_fuzz_path).resolve()
        self.projects_root = Path(projects_root).resolve()
        self.output_dir = Path(output_dir).resolve()
        self.jobs = jobs
        self.group_jobs = group_jobs
        self.per_pov_timeout = per_pov_timeout
        self.resume = resume
        self.infra = (
            infra if infra is not None else OSSFuzzInfrastructure(self.oss_fuzz_path)
        )
        self.mapping = (
            mapping if mapping is not None else load_benchmark_project_mapping()
        )
        self.session_pool_factory = (
            session_pool_factory
            if session_pool_factory is not None
            else self._make_session_pool
        )
        self._project_locks: dict[str, threading.Lock] = {}
        self._project_locks_lock = threading.Lock()
        self._zero_day_log_lock = threading.Lock()
        self._logged_zero_day_keys: set[tuple[str, ...]] = set()

    def _make_session_pool(
        self,
        *,
        project_name: str,
        sanitizer: str,
        session_count: int,
    ) -> WarmReplaySessionPool:
        sessions = [
            WarmReplaySession(
                project_name=project_name,
                project_dir=self.projects_root / project_name,
                build_output_dir=self.oss_fuzz_path / "build" / "out" / project_name,
                output_dir=self.output_dir / ".sessions" / project_name / sanitizer,
                session_label=f"worker-{index}",
            )
            for index in range(session_count)
        ]
        return WarmReplaySessionPool(sessions)

    def _monotonic(self) -> float:
        return time.monotonic()

    @staticmethod
    def _empty_group_timing() -> dict[str, float | int]:
        return {
            "group_wall_seconds": 0.0,
            "lock_wait_wall_seconds": 0.0,
            "build_and_prepare_wall_seconds": 0.0,
            "session_pool_setup_wall_seconds": 0.0,
            "replay_wall_seconds": 0.0,
            "result_aggregation_wall_seconds": 0.0,
            "task_duration_seconds_sum": 0.0,
            "task_duration_seconds_max": 0.0,
            "task_duration_seconds_avg": 0.0,
            "session_count": 0,
        }

    @classmethod
    def _group_timing_payload(
        cls,
        timing: dict[str, float | int] | None = None,
    ) -> dict[str, float | int]:
        payload = cls._empty_group_timing()
        if isinstance(timing, dict):
            payload.update(timing)
        return payload

    @staticmethod
    def _phase_interval(
        started_at: float,
        ended_at: float,
        *,
        run_started_at: float,
    ) -> dict[str, float]:
        wall_seconds = max(0.0, ended_at - started_at)
        started_offset = max(0.0, started_at - run_started_at)
        ended_offset = max(started_offset, ended_at - run_started_at)
        return {
            "started_offset_seconds": round(started_offset, 6),
            "ended_offset_seconds": round(ended_offset, 6),
            "wall_seconds": round(wall_seconds, 6),
        }

    @staticmethod
    def _merged_interval_duration(
        intervals: list[dict[str, float] | None],
    ) -> float:
        spans = sorted(
            (
                interval["started_offset_seconds"],
                interval["ended_offset_seconds"],
            )
            for interval in intervals
            if interval is not None
        )
        if not spans:
            return 0.0

        merged_start, merged_end = spans[0]
        total = 0.0
        for start, end in spans[1:]:
            if start <= merged_end:
                merged_end = max(merged_end, end)
                continue
            total += merged_end - merged_start
            merged_start, merged_end = start, end
        total += merged_end - merged_start
        return round(total, 6)

    @staticmethod
    def _update_completed_replay_metrics(
        replay_results_by_task: dict[ReplayTask, ReplayResult],
        *,
        summary_updates: dict[str, int],
        timing: dict[str, float | int],
    ) -> tuple[int, int]:
        naive_replay_tasks = sum(
            len(task.source_records) for task in replay_results_by_task
        )
        physical_replay_tasks = len(replay_results_by_task)
        for replay_result in replay_results_by_task.values():
            summary_updates[replay_result.outcome + "_count"] += 1
        summary_updates["unique_replay_tasks_executed"] += physical_replay_tasks
        if physical_replay_tasks > 0:
            task_duration_sum = sum(
                replay_result.duration_seconds
                for replay_result in replay_results_by_task.values()
            )
            timing["task_duration_seconds_sum"] = round(task_duration_sum, 6)
            timing["task_duration_seconds_max"] = round(
                max(
                    replay_result.duration_seconds
                    for replay_result in replay_results_by_task.values()
                ),
                6,
            )
            timing["task_duration_seconds_avg"] = round(
                task_duration_sum / physical_replay_tasks,
                6,
            )
        return naive_replay_tasks, physical_replay_tasks

    def _artifact_dir(self, task: ReplayTask) -> Path:
        return (
            self.output_dir
            / "artifacts"
            / task.mapped_project
            / task.sanitizer
            / task.target_harness
            / task.pov_content_hash
        )

    @staticmethod
    def _record_sort_key(record: SourcePovRecord) -> tuple[str, str, str]:
        return (
            record.source_id,
            record.trial_relative_path,
            record.original_pov_relpath,
        )

    def _resolve_record_project(self, benchmark: str) -> MappingResolution:
        resolution = resolve_mapped_project(benchmark, self.mapping)
        if (
            resolution.reason == "missing_mapping"
            and (self.projects_root / benchmark).is_dir()
        ):
            return MappingResolution(
                benchmark=benchmark,
                mapped_project=benchmark,
                reason="mapped",
            )
        return resolution

    def _project_lock(self, project_name: str) -> threading.Lock:
        with self._project_locks_lock:
            lock = self._project_locks.get(project_name)
            if lock is None:
                lock = threading.Lock()
                self._project_locks[project_name] = lock
            return lock

    @staticmethod
    def _group_input_signature(records: list[SourcePovRecord]) -> str:
        payload = json.dumps(
            sorted(
                [
                    (
                        record.source_id,
                        record.trial_relative_path,
                        record.original_pov_relpath,
                        record.pov_content_hash,
                        record.source_sanitizer or "address",
                    )
                    for record in records
                ]
            ),
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _group_checkpoint_path(self, mapped_project: str, sanitizer: str) -> Path:
        return (
            self.output_dir
            / ".state"
            / "groups"
            / mapped_project
            / sanitizer
            / "group-result.json"
        )

    @staticmethod
    def _serialize_group_outcome(outcome: GroupReplayOutcome) -> dict:
        return {
            "entries": outcome.entries,
            "zero_day_entries": outcome.zero_day_entries,
            "trial_entries": [
                {
                    "source_id": source_id,
                    "trial_relative_path": trial_relative_path,
                    "entries": entries,
                }
                for (source_id, trial_relative_path), entries in sorted(
                    outcome.trial_entries.items()
                )
            ],
            "summary_updates": outcome.summary_updates,
            "naive_replay_tasks": outcome.naive_replay_tasks,
            "physical_replay_tasks": outcome.physical_replay_tasks,
            "timing": outcome.timing,
        }

    @staticmethod
    def _deserialize_group_outcome(data: dict) -> GroupReplayOutcome:
        trial_entries = {
            (item["source_id"], item["trial_relative_path"]): item["entries"]
            for item in data.get("trial_entries", [])
        }
        return GroupReplayOutcome(
            entries=data.get("entries", []),
            zero_day_entries=data.get("zero_day_entries", []),
            trial_entries=trial_entries,
            summary_updates=data.get("summary_updates", {}),
            naive_replay_tasks=data.get("naive_replay_tasks", 0),
            physical_replay_tasks=data.get("physical_replay_tasks", 0),
            timing=ReplayEngine._group_timing_payload(data.get("timing")),
        )

    @staticmethod
    def _serialize_group_summary(
        mapped_project: str,
        sanitizer: str,
        records: list[SourcePovRecord],
        outcome: GroupReplayOutcome,
    ) -> dict:
        return {
            "mapped_project": mapped_project,
            "sanitizer": sanitizer,
            "source_pov_instances": len(records),
            "checkpoint_reused": outcome.checkpoint_reused,
            "naive_replay_tasks": outcome.naive_replay_tasks,
            "physical_replay_tasks": outcome.physical_replay_tasks,
            "summary_updates": outcome.summary_updates,
            "timing": outcome.timing,
            "phase_intervals": outcome.phase_intervals,
        }

    def _load_group_checkpoint(
        self,
        mapped_project: str,
        sanitizer: str,
        *,
        input_signature: str,
    ) -> GroupReplayOutcome | None:
        checkpoint_path = self._group_checkpoint_path(mapped_project, sanitizer)
        if not checkpoint_path.exists():
            return None

        try:
            data = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        except Exception:
            return None

        if data.get("format_version") != _GROUP_CHECKPOINT_FORMAT_VERSION:
            return None
        if data.get("input_signature") != input_signature:
            return None

        outcome = data.get("outcome")
        if not isinstance(outcome, dict):
            return None
        return self._deserialize_group_outcome(outcome)

    def _write_group_checkpoint(
        self,
        mapped_project: str,
        sanitizer: str,
        *,
        records: list[SourcePovRecord],
        outcome: GroupReplayOutcome,
    ) -> None:
        checkpoint_path = self._group_checkpoint_path(mapped_project, sanitizer)
        checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = checkpoint_path.with_suffix(".tmp")
        payload = {
            "format_version": _GROUP_CHECKPOINT_FORMAT_VERSION,
            "input_signature": self._group_input_signature(records),
            "outcome": self._serialize_group_outcome(outcome),
        }
        temp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temp_path.replace(checkpoint_path)

    def _zero_day_log_path(self) -> Path:
        return self.output_dir / "0day.log"

    def _load_zero_day_log_state(self) -> None:
        self._logged_zero_day_keys = set()
        if not self.resume:
            return

        zero_day_log = self._zero_day_log_path()
        if not zero_day_log.exists():
            return

        for line in zero_day_log.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
            except Exception:
                continue
            for replay in entry.get("replays", []):
                self._logged_zero_day_keys.add(
                    (
                        entry.get("source_id", ""),
                        entry.get("trial_relative_path", ""),
                        entry.get("original_pov_relpath", ""),
                        entry.get("mapped_oss_fuzz_project", ""),
                        replay.get("sanitizer", ""),
                        replay.get("target_harness", ""),
                        entry.get("pov_content_hash", ""),
                    )
                )

    @staticmethod
    def _is_standard_zero_day_output(
        *,
        outcome: str,
        stdout: str,
        stderr: str,
    ) -> bool:
        if outcome != "crash":
            return False

        combined = f"{stdout}\n{stderr}"
        combined_lower = combined.casefold()
        if any(token in combined_lower for token in _ZERO_DAY_EXCLUDED_SUBSTRINGS):
            return False

        if _ZERO_DAY_REQUIRED_MARKER in combined:
            return True

        return any(marker in combined for marker in _ZERO_DAY_JAVA_EXCEPTION_MARKERS)

    def _append_zero_day_log(
        self,
        record: SourcePovRecord,
        replay_result: ReplayResult,
    ) -> None:
        if not replay_result.standard_zero_day:
            return

        key = (
            record.source_id,
            record.trial_relative_path,
            record.original_pov_relpath,
            replay_result.mapped_project,
            replay_result.sanitizer,
            replay_result.target_harness,
            record.pov_content_hash,
        )
        entry = self._base_entry(
            record,
            mapped_project=replay_result.mapped_project,
            status="replayed",
            replays=[self._serialize_0day_replay_result(replay_result)],
        )
        zero_day_log = self._zero_day_log_path()
        zero_day_log.parent.mkdir(parents=True, exist_ok=True)
        with self._zero_day_log_lock:
            if key in self._logged_zero_day_keys:
                return
            with zero_day_log.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            self._logged_zero_day_keys.add(key)

    def _build_replay_tasks(
        self,
        mapped_project: str,
        sanitizer: str,
        harnesses: list[str],
        records: list[SourcePovRecord],
    ) -> list[ReplayTask]:
        grouped: dict[tuple[str, str, str, str], list[SourcePovRecord]] = defaultdict(
            list
        )
        for record in sorted(records, key=self._record_sort_key):
            for harness in sorted(harnesses):
                key = (mapped_project, sanitizer, harness, record.pov_content_hash)
                grouped[key].append(record)

        tasks: list[ReplayTask] = []
        for (
            project_name,
            task_sanitizer,
            harness,
            pov_hash,
        ), grouped_records in sorted(grouped.items()):
            tasks.append(
                ReplayTask(
                    mapped_project=project_name,
                    sanitizer=task_sanitizer,
                    target_harness=harness,
                    pov_content_hash=pov_hash,
                    pov_path=grouped_records[0].original_pov_path,
                    source_records=tuple(grouped_records),
                )
            )
        return tasks

    def _write_artifact(
        self,
        task: ReplayTask,
        session_result: SessionReplayResult,
        outcome: ReplayOutcome,
        *,
        standard_zero_day: bool,
    ) -> ReplayResult:
        artifact_dir = self._artifact_dir(task)
        artifact_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = artifact_dir / "stdout.txt"
        stderr_path = artifact_dir / "stderr.txt"
        sanitizer_log_path = artifact_dir / "sanitizer.log"
        metadata_path = artifact_dir / "metadata.json"

        stdout_path.write_text(session_result.stdout, encoding="utf-8")
        stderr_path.write_text(session_result.stderr, encoding="utf-8")
        sanitizer_log_path.write_text(
            f"{session_result.stdout}\n===== STDERR =====\n{session_result.stderr}",
            encoding="utf-8",
        )
        metadata_path.write_text(
            json.dumps(
                {
                    "mapped_project": task.mapped_project,
                    "sanitizer": task.sanitizer,
                    "target_harness": task.target_harness,
                    "pov_content_hash": task.pov_content_hash,
                    "outcome": outcome,
                    "exit_code": session_result.exit_code,
                    "duration_seconds": session_result.duration_seconds,
                    "session_restarted": session_result.session_restarted,
                    "error_message": session_result.error_message,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        return ReplayResult(
            mapped_project=task.mapped_project,
            sanitizer=task.sanitizer,
            target_harness=task.target_harness,
            pov_content_hash=task.pov_content_hash,
            outcome=outcome,
            exit_code=session_result.exit_code,
            duration_seconds=session_result.duration_seconds,
            artifact_dir=artifact_dir,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            sanitizer_log_path=sanitizer_log_path,
            session_restarted=session_result.session_restarted,
            standard_zero_day=standard_zero_day,
            error_message=session_result.error_message,
        )

    @staticmethod
    def _base_entry(
        record: SourcePovRecord,
        *,
        mapped_project: str | None,
        status: str,
        replays: list[dict],
        error_message: str | None = None,
    ) -> dict:
        return {
            "source_id": record.source_id,
            "source_dir": str(record.source_dir),
            "experiment_name": record.experiment_name,
            "trial_relative_path": record.trial_relative_path,
            "benchmark": record.benchmark,
            "source_harness": record.source_harness,
            "source_sanitizer": record.source_sanitizer,
            "original_pov_path": str(record.original_pov_path),
            "original_pov_relpath": record.original_pov_relpath,
            "pov_filename": record.pov_filename,
            "pov_content_hash": record.pov_content_hash,
            "mapped_oss_fuzz_project": mapped_project,
            "status": status,
            "replays": replays,
            "error_message": error_message,
        }

    @staticmethod
    def _serialize_replay_result(
        result: ReplayResult, *, include_stdio: bool = True
    ) -> dict:
        payload = {
            "target_harness": result.target_harness,
            "sanitizer": result.sanitizer,
            "outcome": result.outcome,
            "exit_code": result.exit_code,
            "duration_seconds": result.duration_seconds,
            "artifact_dir": str(result.artifact_dir) if result.artifact_dir else None,
            "sanitizer_log": (
                str(result.sanitizer_log_path) if result.sanitizer_log_path else None
            ),
            "session_restarted": result.session_restarted,
            "error_message": result.error_message,
        }
        if include_stdio:
            payload["stdout"] = str(result.stdout_path) if result.stdout_path else None
            payload["stderr"] = str(result.stderr_path) if result.stderr_path else None
        return payload

    @staticmethod
    def _serialize_0day_replay_result(result: ReplayResult) -> dict:
        return ReplayEngine._serialize_replay_result(result, include_stdio=False)

    @staticmethod
    def _empty_session_result(error_message: str) -> SessionReplayResult:
        return SessionReplayResult(
            exit_code=None,
            stdout="",
            stderr="",
            duration_seconds=0.0,
            timed_out=False,
            session_restarted=False,
            crashed=None,
            error_message=error_message,
        )

    def _classify_session_outcome(
        self,
        session_result: SessionReplayResult,
    ) -> ReplayOutcome:
        if session_result.error_message:
            return "error"
        if session_result.timed_out:
            return "timeout"

        classification = self.infra.classify_reproduce_result(
            exit_code=session_result.exit_code or 0,
            stdout=session_result.stdout,
            stderr=session_result.stderr,
        )
        if classification.exit_code == 124:
            return "timeout"
        return "crash" if classification.crashed else "no_crash"

    def _group_status_outcome(
        self,
        records: list[SourcePovRecord],
        *,
        mapped_project: str,
        status: str,
        summary_counter: str,
        summary_updates: dict[str, int] | None = None,
        error_message: str | None = None,
        naive_replay_tasks: int = 0,
        physical_replay_tasks: int = 0,
        timing: dict[str, float | int] | None = None,
        phase_intervals: dict[str, dict[str, float]] | None = None,
    ) -> GroupReplayOutcome:
        entries: list[dict] = []
        trial_entries: dict[tuple[str, str], list[dict]] = defaultdict(list)
        for record in records:
            entry = self._base_entry(
                record,
                mapped_project=mapped_project,
                status=status,
                replays=[],
                error_message=error_message,
            )
            entries.append(entry)
            trial_entries[(record.source_id, record.trial_relative_path)].append(entry)

        updates = dict(summary_updates or {})
        updates[summary_counter] = updates.get(summary_counter, 0) + len(records)
        return GroupReplayOutcome(
            entries=entries,
            zero_day_entries=[],
            trial_entries=dict(trial_entries),
            summary_updates=updates,
            naive_replay_tasks=naive_replay_tasks,
            physical_replay_tasks=physical_replay_tasks,
            timing=self._group_timing_payload(timing),
            phase_intervals=dict(phase_intervals or {}),
        )

    def _timed_group_status_outcome(
        self,
        records: list[SourcePovRecord],
        *,
        mapped_project: str,
        status: str,
        summary_counter: str,
        group_started: float,
        run_started_at: float,
        summary_updates: dict[str, int] | None = None,
        error_message: str | None = None,
        naive_replay_tasks: int = 0,
        physical_replay_tasks: int = 0,
        timing: dict[str, float | int] | None = None,
        phase_intervals: dict[str, dict[str, float]] | None = None,
    ) -> GroupReplayOutcome:
        timing_payload = self._group_timing_payload(timing)
        phase_payload = dict(phase_intervals or {})
        result_aggregation_started = self._monotonic()
        outcome = self._group_status_outcome(
            records,
            mapped_project=mapped_project,
            status=status,
            summary_counter=summary_counter,
            summary_updates=summary_updates,
            error_message=error_message,
            naive_replay_tasks=naive_replay_tasks,
            physical_replay_tasks=physical_replay_tasks,
            timing=timing_payload,
            phase_intervals=phase_payload,
        )
        result_aggregation_ended = self._monotonic()
        timing_payload["result_aggregation_wall_seconds"] = round(
            result_aggregation_ended - result_aggregation_started,
            6,
        )
        timing_payload["group_wall_seconds"] = round(
            result_aggregation_ended - group_started,
            6,
        )
        phase_payload["result_aggregation"] = self._phase_interval(
            result_aggregation_started,
            result_aggregation_ended,
            run_started_at=run_started_at,
        )
        outcome.timing = timing_payload
        outcome.phase_intervals = phase_payload
        return outcome

    def _run_group(
        self,
        mapped_project: str,
        sanitizer: str,
        records: list[SourcePovRecord],
        *,
        run_started_at: float,
    ) -> GroupReplayOutcome:
        project_dir = self.projects_root / mapped_project
        if not project_dir.exists():
            return self._group_status_outcome(
                records,
                mapped_project=mapped_project,
                status="target_project_missing",
                summary_counter="target_project_missing_count",
            )

        summary_updates: dict[str, int] = defaultdict(int)
        naive_replay_tasks = 0
        physical_replay_tasks = 0
        timing = self._group_timing_payload()
        phase_intervals: dict[str, dict[str, float]] = {}

        group_started = self._monotonic()
        lock_wait_started = self._monotonic()
        with self._project_lock(mapped_project):
            lock_wait_ended = self._monotonic()
            timing["lock_wait_wall_seconds"] = round(
                lock_wait_ended - lock_wait_started,
                6,
            )
            phase_intervals["project_lock_wait"] = self._phase_interval(
                lock_wait_started,
                lock_wait_ended,
                run_started_at=run_started_at,
            )

            build_prepare_started = self._monotonic()
            try:
                ensure_project_link(
                    self.oss_fuzz_path, self.projects_root, mapped_project
                )
                build_result = self.infra.build_project_fuzzers(
                    mapped_project,
                    sanitizer=sanitizer,
                )
            except Exception as exc:
                build_prepare_ended = self._monotonic()
                timing["build_and_prepare_wall_seconds"] = round(
                    build_prepare_ended - build_prepare_started,
                    6,
                )
                timing["group_wall_seconds"] = round(
                    build_prepare_ended - group_started,
                    6,
                )
                phase_intervals["build_and_prepare"] = self._phase_interval(
                    build_prepare_started,
                    build_prepare_ended,
                    run_started_at=run_started_at,
                )
                return self._group_status_outcome(
                    records,
                    mapped_project=mapped_project,
                    status="error",
                    summary_counter="error_count",
                    error_message=str(exc),
                    timing=timing,
                    phase_intervals=phase_intervals,
                )
            build_prepare_ended = self._monotonic()
            timing["build_and_prepare_wall_seconds"] = round(
                build_prepare_ended - build_prepare_started,
                6,
            )
            phase_intervals["build_and_prepare"] = self._phase_interval(
                build_prepare_started,
                build_prepare_ended,
                run_started_at=run_started_at,
            )

            if not build_result.success:
                timing["group_wall_seconds"] = round(
                    build_prepare_ended - group_started,
                    6,
                )
                return self._group_status_outcome(
                    records,
                    mapped_project=mapped_project,
                    status="build_error",
                    summary_counter="build_error_count",
                    timing=timing,
                    phase_intervals=phase_intervals,
                )

            summary_updates["projects_built"] += 1
            harnesses = self.infra.list_fuzz_targets(mapped_project)
            naive_replay_tasks = len(records) * len(harnesses)
            tasks = self._build_replay_tasks(
                mapped_project, sanitizer, harnesses, records
            )
            summary_updates["unique_replay_tasks_executed"] += 0
            timing["session_count"] = max(1, min(self.jobs, len(tasks))) if tasks else 0

            if not tasks:
                result_aggregation_started = self._monotonic()
                entries: list[dict] = []
                trial_entries: dict[tuple[str, str], list[dict]] = defaultdict(list)
                for record in records:
                    entry = self._base_entry(
                        record,
                        mapped_project=mapped_project,
                        status="replayed",
                        replays=[],
                    )
                    entries.append(entry)
                    trial_entries[
                        (record.source_id, record.trial_relative_path)
                    ].append(entry)
                result_aggregation_ended = self._monotonic()
                timing["result_aggregation_wall_seconds"] = round(
                    result_aggregation_ended - result_aggregation_started,
                    6,
                )
                phase_intervals["result_aggregation"] = self._phase_interval(
                    result_aggregation_started,
                    result_aggregation_ended,
                    run_started_at=run_started_at,
                )
                outcome = GroupReplayOutcome(
                    entries=entries,
                    zero_day_entries=[],
                    trial_entries=dict(trial_entries),
                    summary_updates=dict(summary_updates),
                    naive_replay_tasks=naive_replay_tasks,
                    physical_replay_tasks=0,
                    timing=timing,
                    phase_intervals=phase_intervals,
                )
                outcome.timing["group_wall_seconds"] = round(
                    result_aggregation_ended - group_started,
                    6,
                )
                self._write_group_checkpoint(
                    mapped_project,
                    sanitizer,
                    records=records,
                    outcome=outcome,
                )
                return outcome

            session_setup_started = self._monotonic()
            try:
                pool = self.session_pool_factory(
                    project_name=mapped_project,
                    sanitizer=sanitizer,
                    session_count=max(1, min(self.jobs, len(tasks))),
                )
            except Exception as exc:
                session_setup_ended = self._monotonic()
                timing["session_pool_setup_wall_seconds"] = round(
                    session_setup_ended - session_setup_started,
                    6,
                )
                timing["group_wall_seconds"] = round(
                    session_setup_ended - group_started,
                    6,
                )
                phase_intervals["session_pool_setup"] = self._phase_interval(
                    session_setup_started,
                    session_setup_ended,
                    run_started_at=run_started_at,
                )
                return self._group_status_outcome(
                    records,
                    mapped_project=mapped_project,
                    status="error",
                    summary_counter="error_count",
                    summary_updates=dict(summary_updates),
                    error_message=str(exc),
                    naive_replay_tasks=naive_replay_tasks,
                    timing=timing,
                    phase_intervals=phase_intervals,
                )
            session_setup_ended = self._monotonic()
            timing["session_pool_setup_wall_seconds"] = round(
                session_setup_ended - session_setup_started,
                6,
            )
            phase_intervals["session_pool_setup"] = self._phase_interval(
                session_setup_started,
                session_setup_ended,
                run_started_at=run_started_at,
            )

            replay_results_by_record: dict[SourcePovRecord, list[ReplayResult]] = (
                defaultdict(list)
            )
            replay_results_by_task: dict[ReplayTask, ReplayResult] = {}
            results_lock = threading.Lock()

            def record_task_result(
                task: ReplayTask,
                session_result: SessionReplayResult,
            ) -> None:
                outcome = self._classify_session_outcome(session_result)
                standard_zero_day = self._is_standard_zero_day_output(
                    outcome=outcome,
                    stdout=session_result.stdout,
                    stderr=session_result.stderr,
                )
                replay_result = self._write_artifact(
                    task,
                    session_result,
                    outcome,
                    standard_zero_day=standard_zero_day,
                )
                crash_records: list[SourcePovRecord] = []
                with results_lock:
                    if task in replay_results_by_task:
                        return
                    replay_results_by_task[task] = replay_result
                    for record in task.source_records:
                        replay_results_by_record[record].append(replay_result)
                        if replay_result.standard_zero_day:
                            crash_records.append(record)

                for record in crash_records:
                    self._append_zero_day_log(record, replay_result)

            replay_started = self._monotonic()
            replay_error: str | None = None
            try:
                session_results = pool.run_many(
                    tasks,
                    timeout=self.per_pov_timeout,
                    on_result=record_task_result,
                )
            except Exception as exc:
                replay_error = str(exc)
                session_results = {}
            finally:
                pool.close()
            replay_ended = self._monotonic()
            timing["replay_wall_seconds"] = round(
                replay_ended - replay_started,
                6,
            )
            phase_intervals["replay"] = self._phase_interval(
                replay_started,
                replay_ended,
                run_started_at=run_started_at,
            )
            if replay_error is not None:
                (
                    naive_replay_tasks,
                    physical_replay_tasks,
                ) = self._update_completed_replay_metrics(
                    replay_results_by_task,
                    summary_updates=summary_updates,
                    timing=timing,
                )
                return self._timed_group_status_outcome(
                    records,
                    mapped_project=mapped_project,
                    status="error",
                    summary_counter="error_count",
                    group_started=group_started,
                    run_started_at=run_started_at,
                    summary_updates=dict(summary_updates),
                    error_message=replay_error,
                    naive_replay_tasks=naive_replay_tasks,
                    physical_replay_tasks=physical_replay_tasks,
                    timing=timing,
                    phase_intervals=phase_intervals,
                )

            for task in tasks:
                if task not in replay_results_by_task:
                    session_result = session_results.get(
                        task,
                        self._empty_session_result(
                            "Session pool did not return a result"
                        ),
                    )
                    record_task_result(task, session_result)

            (
                naive_replay_tasks,
                physical_replay_tasks,
            ) = self._update_completed_replay_metrics(
                replay_results_by_task,
                summary_updates=summary_updates,
                timing=timing,
            )

            result_aggregation_started = self._monotonic()
            entries: list[dict] = []
            zero_day_entries: list[dict] = []
            trial_entries: dict[tuple[str, str], list[dict]] = defaultdict(list)
            for record in records:
                replays = sorted(
                    replay_results_by_record[record],
                    key=lambda item: (item.target_harness, item.pov_content_hash),
                )
                zero_day_replays = [item for item in replays if item.standard_zero_day]
                entry = self._base_entry(
                    record,
                    mapped_project=mapped_project,
                    status="replayed",
                    replays=[self._serialize_replay_result(item) for item in replays],
                )
                entries.append(entry)
                trial_entries[(record.source_id, record.trial_relative_path)].append(
                    entry
                )
                if zero_day_replays:
                    zero_day_entries.append(
                        self._base_entry(
                            record,
                            mapped_project=mapped_project,
                            status="replayed",
                            replays=[
                                self._serialize_0day_replay_result(item)
                                for item in zero_day_replays
                            ],
                        )
                    )
            result_aggregation_ended = self._monotonic()
            timing["result_aggregation_wall_seconds"] = round(
                result_aggregation_ended - result_aggregation_started,
                6,
            )
            timing["group_wall_seconds"] = round(
                result_aggregation_ended - group_started,
                6,
            )
            phase_intervals["result_aggregation"] = self._phase_interval(
                result_aggregation_started,
                result_aggregation_ended,
                run_started_at=run_started_at,
            )

            outcome = GroupReplayOutcome(
                entries=entries,
                zero_day_entries=zero_day_entries,
                trial_entries=dict(trial_entries),
                summary_updates=dict(summary_updates),
                naive_replay_tasks=naive_replay_tasks,
                physical_replay_tasks=physical_replay_tasks,
                timing=timing,
                phase_intervals=phase_intervals,
            )
            self._write_group_checkpoint(
                mapped_project,
                sanitizer,
                records=records,
                outcome=outcome,
            )
            return outcome

    def run(
        self,
        source_records: list[SourcePovRecord],
        *,
        discovery_stats: dict[str, int] | None = None,
        source_dirs: list[Path] | None = None,
    ) -> None:
        started_at = self._monotonic()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        discovery_stats = discovery_stats or {}
        input_source_dirs = (
            [Path(item).resolve() for item in source_dirs]
            if source_dirs is not None
            else sorted({record.source_dir.resolve() for record in source_records})
        )
        manifest = {
            "format_version": _REPLAY_OUTPUT_FORMAT_VERSION,
            "source_dirs": [str(item) for item in input_source_dirs],
            "source_ids": sorted({record.source_id for record in source_records}),
            "oss_fuzz_path": str(self.oss_fuzz_path),
            "projects_root": str(self.projects_root),
            "output_dir": str(self.output_dir),
            "jobs": self.jobs,
            "group_jobs": self.group_jobs,
            "resume": self.resume,
            "per_pov_timeout": self.per_pov_timeout,
        }
        (self.output_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2),
            encoding="utf-8",
        )
        if self.resume:
            self._load_zero_day_log_state()
        else:
            self._logged_zero_day_keys = set()
            zero_day_log = self._zero_day_log_path()
            if zero_day_log.exists():
                zero_day_log.unlink()
        naive_replay_tasks = 0
        physical_replay_tasks = 0
        summary = {
            "source_roots_processed": discovery_stats.get(
                "source_roots_processed",
                len(input_source_dirs),
            ),
            "trials_processed": discovery_stats.get("trials_processed", 0),
            "trials_skipped": discovery_stats.get("trials_skipped", 0),
            "mappings_resolved": 0,
            "projects_built": 0,
            "unique_replay_tasks_executed": 0,
            "original_pov_instances_mapped": len(source_records),
            "crash_count": 0,
            "no_crash_count": 0,
            "timeout_count": 0,
            "error_count": 0,
            "missing_mapping_count": 0,
            "unsupported_mapping_count": 0,
            "target_project_missing_count": 0,
            "build_error_count": 0,
            "0day_count": 0,
            "crashing_replay_count": 0,
        }
        global_entries: list[dict] = []
        zero_day_entries: list[dict] = []
        trial_entries: dict[tuple[str, str], list[dict]] = defaultdict(list)
        grouped_records: dict[tuple[str, str], list[SourcePovRecord]] = defaultdict(
            list
        )

        for record in sorted(source_records, key=self._record_sort_key):
            resolution = self._resolve_record_project(record.benchmark)
            if resolution.reason != "mapped" or resolution.mapped_project is None:
                summary[f"{resolution.reason}_count"] += 1
                entry = self._base_entry(
                    record,
                    mapped_project=None,
                    status=resolution.reason,
                    replays=[],
                )
                global_entries.append(entry)
                trial_entries[(record.source_id, record.trial_relative_path)].append(
                    entry
                )
                continue

            summary["mappings_resolved"] += 1
            sanitizer = record.source_sanitizer or "address"
            grouped_records[(resolution.mapped_project, sanitizer)].append(record)

        group_items = sorted(grouped_records.items())
        group_outcomes: dict[tuple[str, str], GroupReplayOutcome] = {}
        pending_group_items: list[tuple[tuple[str, str], list[SourcePovRecord]]] = []
        for key, records in group_items:
            if self.resume:
                checkpoint = self._load_group_checkpoint(
                    key[0],
                    key[1],
                    input_signature=self._group_input_signature(records),
                )
                if checkpoint is not None:
                    logger.info(
                        "Reusing checkpointed replay group "
                        f"project={key[0]} sanitizer={key[1]}"
                    )
                    checkpoint.checkpoint_reused = True
                    group_outcomes[key] = checkpoint
                    continue
            pending_group_items.append((key, records))

        planning_ended = self._monotonic()
        if self.group_jobs == 1:
            for (mapped_project, sanitizer), records in pending_group_items:
                group_outcomes[(mapped_project, sanitizer)] = self._run_group(
                    mapped_project,
                    sanitizer,
                    records,
                    run_started_at=started_at,
                )
        elif pending_group_items:
            with ThreadPoolExecutor(max_workers=self.group_jobs) as executor:
                future_to_key = {
                    executor.submit(
                        self._run_group,
                        mapped_project,
                        sanitizer,
                        records,
                        run_started_at=started_at,
                    ): (
                        mapped_project,
                        sanitizer,
                    )
                    for (mapped_project, sanitizer), records in pending_group_items
                }
                for future in as_completed(future_to_key):
                    key = future_to_key[future]
                    try:
                        group_outcomes[key] = future.result()
                    except Exception as exc:
                        group_outcomes[key] = self._group_status_outcome(
                            grouped_records[key],
                            mapped_project=key[0],
                            status="error",
                            summary_counter="error_count",
                            error_message=str(exc),
                        )
        group_execution_ended = self._monotonic()

        for key, _records in group_items:
            outcome = group_outcomes[key]
            naive_replay_tasks += outcome.naive_replay_tasks
            physical_replay_tasks += outcome.physical_replay_tasks
            global_entries.extend(outcome.entries)
            zero_day_entries.extend(outcome.zero_day_entries)
            for trial_key, entries in outcome.trial_entries.items():
                trial_entries[trial_key].extend(entries)
            for summary_key, value in outcome.summary_updates.items():
                summary[summary_key] += value

        finalization_started = group_execution_ended
        deduplicated_replay_tasks_saved = naive_replay_tasks - physical_replay_tasks
        dedup_multiplier = (
            naive_replay_tasks / physical_replay_tasks
            if physical_replay_tasks > 0
            else 0.0
        )
        deduplicated_zero_day_entries = build_deduplicated_zero_day_entries(
            zero_day_entries
        )
        executed_group_outcomes = [
            outcome
            for outcome in group_outcomes.values()
            if not outcome.checkpoint_reused
        ]
        current_run_group_count_executed = len(executed_group_outcomes)
        current_run_group_count_reused = (
            len(group_items) - current_run_group_count_executed
        )
        current_run_physical_replay_tasks_executed = sum(
            outcome.physical_replay_tasks for outcome in executed_group_outcomes
        )
        current_run_physical_replay_tasks_reused = (
            physical_replay_tasks - current_run_physical_replay_tasks_executed
        )

        def active_wall_seconds(phase_name: str) -> float:
            return self._merged_interval_duration(
                [
                    outcome.phase_intervals.get(phase_name)
                    for outcome in executed_group_outcomes
                ]
            )

        finalization_ended = self._monotonic()
        elapsed_seconds = round(finalization_ended - started_at, 6)
        physical_replay_tasks_per_second = (
            physical_replay_tasks / elapsed_seconds if elapsed_seconds > 0 else 0.0
        )
        original_pov_instances_per_second = (
            len(source_records) / elapsed_seconds if elapsed_seconds > 0 else 0.0
        )
        current_run = {
            "group_count_total": len(group_items),
            "group_count_executed": current_run_group_count_executed,
            "group_count_reused": current_run_group_count_reused,
            "physical_replay_tasks_executed": (
                current_run_physical_replay_tasks_executed
            ),
            "physical_replay_tasks_reused": current_run_physical_replay_tasks_reused,
            "timing": {
                "planning_wall_seconds": round(planning_ended - started_at, 6),
                "group_execution_wall_seconds": round(
                    group_execution_ended - planning_ended,
                    6,
                ),
                "finalization_wall_seconds": round(
                    finalization_ended - finalization_started,
                    6,
                ),
                "lock_wait_active_wall_seconds": active_wall_seconds(
                    "project_lock_wait"
                ),
                "build_and_prepare_active_wall_seconds": active_wall_seconds(
                    "build_and_prepare"
                ),
                "session_pool_setup_active_wall_seconds": active_wall_seconds(
                    "session_pool_setup"
                ),
                "replay_active_wall_seconds": active_wall_seconds("replay"),
                "result_aggregation_active_wall_seconds": active_wall_seconds(
                    "result_aggregation"
                ),
                "lock_wait_wall_seconds_sum": round(
                    sum(
                        float(outcome.timing["lock_wait_wall_seconds"])
                        for outcome in executed_group_outcomes
                    ),
                    6,
                ),
                "build_and_prepare_wall_seconds_sum": round(
                    sum(
                        float(outcome.timing["build_and_prepare_wall_seconds"])
                        for outcome in executed_group_outcomes
                    ),
                    6,
                ),
                "session_pool_setup_wall_seconds_sum": round(
                    sum(
                        float(outcome.timing["session_pool_setup_wall_seconds"])
                        for outcome in executed_group_outcomes
                    ),
                    6,
                ),
                "replay_wall_seconds_sum": round(
                    sum(
                        float(outcome.timing["replay_wall_seconds"])
                        for outcome in executed_group_outcomes
                    ),
                    6,
                ),
                "result_aggregation_wall_seconds_sum": round(
                    sum(
                        float(outcome.timing["result_aggregation_wall_seconds"])
                        for outcome in executed_group_outcomes
                    ),
                    6,
                ),
                "task_duration_seconds_sum": round(
                    sum(
                        float(outcome.timing["task_duration_seconds_sum"])
                        for outcome in executed_group_outcomes
                    ),
                    6,
                ),
                "task_duration_seconds_max": round(
                    max(
                        (
                            float(outcome.timing["task_duration_seconds_max"])
                            for outcome in executed_group_outcomes
                        ),
                        default=0.0,
                    ),
                    6,
                ),
            },
        }
        group_summary = [
            self._serialize_group_summary(
                key[0],
                key[1],
                records,
                group_outcomes[key],
            )
            for key, records in group_items
        ]
        # These counters reflect the emitted crash-only 0day view, not
        # deduplicated physical replay tasks.
        summary["0day_count"] = len(zero_day_entries)
        summary["0day_raw_count"] = len(zero_day_entries)
        summary["0day_dedup_count"] = len(deduplicated_zero_day_entries)
        summary["crashing_replay_count"] = sum(
            len(entry["replays"]) for entry in zero_day_entries
        )
        summary.update(
            {
                "elapsed_seconds": elapsed_seconds,
                "original_pov_instances_total": len(source_records),
                "naive_replay_tasks": naive_replay_tasks,
                "physical_replay_tasks": physical_replay_tasks,
                "deduplicated_replay_tasks_saved": deduplicated_replay_tasks_saved,
                "physical_replay_tasks_per_second": round(
                    physical_replay_tasks_per_second,
                    6,
                ),
                "original_pov_instances_per_second": round(
                    original_pov_instances_per_second,
                    6,
                ),
                "dedup_multiplier": round(dedup_multiplier, 6),
                "current_run": current_run,
            }
        )

        (self.output_dir / "summary.json").write_text(
            json.dumps(summary, indent=2),
            encoding="utf-8",
        )
        (self.output_dir / "group-summary.json").write_text(
            json.dumps(group_summary, indent=2),
            encoding="utf-8",
        )
        (self.output_dir / "pov-to-crash-map.json").write_text(
            json.dumps(global_entries, indent=2),
            encoding="utf-8",
        )
        (self.output_dir / "0day.json").write_text(
            json.dumps(zero_day_entries, indent=2),
            encoding="utf-8",
        )
        (self.output_dir / "0day-dedup.json").write_text(
            json.dumps(deduplicated_zero_day_entries, indent=2),
            encoding="utf-8",
        )
        for (source_id, trial_relative_path), entries in sorted(trial_entries.items()):
            trial_index_path = (
                self.output_dir
                / "trials"
                / source_id
                / trial_relative_path
                / "pov-index.json"
            )
            trial_index_path.parent.mkdir(parents=True, exist_ok=True)
            trial_index_path.write_text(
                json.dumps(entries, indent=2),
                encoding="utf-8",
            )

        logger.info(
            "Replay throughput: "
            f"naive_tasks={naive_replay_tasks} "
            f"physical_tasks={physical_replay_tasks} "
            f"saved_tasks={deduplicated_replay_tasks_saved} "
            f"original_pov_instances={len(source_records)} "
            f"elapsed={elapsed_seconds:.3f}s "
            f"physical_tasks_per_second={summary['physical_replay_tasks_per_second']:.3f} "
            f"original_pov_instances_per_second={summary['original_pov_instances_per_second']:.3f} "
            f"dedup_multiplier={summary['dedup_multiplier']:.3f}"
        )
