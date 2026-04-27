from __future__ import annotations

import json
import time
from collections import defaultdict
from pathlib import Path
from typing import Callable

from crsbench.builder.infrastructure import OSSFuzzInfrastructure
from crsbench.evaluation.replay.mapping import (
    MappingResolution,
    load_benchmark_project_mapping,
    resolve_mapped_project,
)
from crsbench.evaluation.replay.models import (
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


class ReplayEngine:
    """Replay discovered POVs against latest OSS-Fuzz targets."""

    def __init__(
        self,
        *,
        oss_fuzz_path: Path,
        projects_root: Path,
        output_dir: Path,
        jobs: int,
        per_pov_timeout: int,
        infra: OSSFuzzInfrastructure | None = None,
        mapping: dict[str, str | None] | None = None,
        session_pool_factory: SessionPoolFactory | None = None,
    ) -> None:
        self.oss_fuzz_path = Path(oss_fuzz_path).resolve()
        self.projects_root = Path(projects_root).resolve()
        self.output_dir = Path(output_dir).resolve()
        self.jobs = jobs
        self.per_pov_timeout = per_pov_timeout
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
        outcome: str,
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

    def run(
        self,
        source_records: list[SourcePovRecord],
        *,
        discovery_stats: dict[str, int] | None = None,
        source_dirs: list[Path] | None = None,
    ) -> None:
        started_at = time.monotonic()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        discovery_stats = discovery_stats or {}
        input_source_dirs = (
            [Path(item).resolve() for item in source_dirs]
            if source_dirs is not None
            else sorted({record.source_dir.resolve() for record in source_records})
        )
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

        for (mapped_project, sanitizer), records in sorted(grouped_records.items()):
            project_dir = self.projects_root / mapped_project
            if not project_dir.exists():
                summary["target_project_missing_count"] += len(records)
                for record in records:
                    entry = self._base_entry(
                        record,
                        mapped_project=mapped_project,
                        status="target_project_missing",
                        replays=[],
                    )
                    global_entries.append(entry)
                    trial_entries[
                        (record.source_id, record.trial_relative_path)
                    ].append(entry)
                continue

            try:
                ensure_project_link(
                    self.oss_fuzz_path, self.projects_root, mapped_project
                )
                build_result = self.infra.build_project_fuzzers(
                    mapped_project,
                    sanitizer=sanitizer,
                )
            except Exception as exc:
                summary["error_count"] += len(records)
                for record in records:
                    entry = self._base_entry(
                        record,
                        mapped_project=mapped_project,
                        status="error",
                        replays=[],
                        error_message=str(exc),
                    )
                    global_entries.append(entry)
                    trial_entries[
                        (record.source_id, record.trial_relative_path)
                    ].append(entry)
                continue

            if not build_result.success:
                summary["build_error_count"] += len(records)
                for record in records:
                    entry = self._base_entry(
                        record,
                        mapped_project=mapped_project,
                        status="build_error",
                        replays=[],
                    )
                    global_entries.append(entry)
                    trial_entries[
                        (record.source_id, record.trial_relative_path)
                    ].append(entry)
                continue

            summary["projects_built"] += 1
            harnesses = self.infra.list_fuzz_targets(mapped_project)
            naive_replay_tasks += len(records) * len(harnesses)
            tasks = self._build_replay_tasks(
                mapped_project, sanitizer, harnesses, records
            )
            summary["unique_replay_tasks_executed"] += len(tasks)

            if not tasks:
                for record in records:
                    entry = self._base_entry(
                        record,
                        mapped_project=mapped_project,
                        status="replayed",
                        replays=[],
                    )
                    global_entries.append(entry)
                    trial_entries[
                        (record.source_id, record.trial_relative_path)
                    ].append(entry)
                continue

            try:
                pool = self.session_pool_factory(
                    project_name=mapped_project,
                    sanitizer=sanitizer,
                    session_count=max(1, min(self.jobs, len(tasks))),
                )
            except Exception as exc:
                summary["error_count"] += len(records)
                for record in records:
                    entry = self._base_entry(
                        record,
                        mapped_project=mapped_project,
                        status="error",
                        replays=[],
                        error_message=str(exc),
                    )
                    global_entries.append(entry)
                    trial_entries[
                        (record.source_id, record.trial_relative_path)
                    ].append(entry)
                continue

            try:
                session_results = pool.run_many(tasks, timeout=self.per_pov_timeout)
            except Exception as exc:
                summary["error_count"] += len(records)
                for record in records:
                    entry = self._base_entry(
                        record,
                        mapped_project=mapped_project,
                        status="error",
                        replays=[],
                        error_message=str(exc),
                    )
                    global_entries.append(entry)
                    trial_entries[
                        (record.source_id, record.trial_relative_path)
                    ].append(entry)
                continue
            finally:
                pool.close()

            replay_results_by_record: dict[SourcePovRecord, list[ReplayResult]] = (
                defaultdict(list)
            )
            for task in tasks:
                session_result = session_results.get(
                    task,
                    self._empty_session_result("Session pool did not return a result"),
                )
                physical_replay_tasks += 1
                if session_result.error_message:
                    outcome = "error"
                elif session_result.timed_out:
                    outcome = "timeout"
                else:
                    classification = self.infra.classify_reproduce_result(
                        exit_code=session_result.exit_code or 0,
                        stdout=session_result.stdout,
                        stderr=session_result.stderr,
                    )
                    if classification.exit_code == 124:
                        outcome = "timeout"
                    else:
                        outcome = "crash" if classification.crashed else "no_crash"

                replay_result = self._write_artifact(task, session_result, outcome)
                summary[f"{outcome}_count"] += 1
                for record in task.source_records:
                    replay_results_by_record[record].append(replay_result)

            for record in records:
                replays = sorted(
                    replay_results_by_record[record],
                    key=lambda item: (item.target_harness, item.pov_content_hash),
                )
                crashing_replays = [item for item in replays if item.outcome == "crash"]
                entry = self._base_entry(
                    record,
                    mapped_project=mapped_project,
                    status="replayed",
                    replays=[self._serialize_replay_result(item) for item in replays],
                )
                global_entries.append(entry)
                trial_entries[(record.source_id, record.trial_relative_path)].append(
                    entry
                )
                if crashing_replays:
                    zero_day_entries.append(
                        self._base_entry(
                            record,
                            mapped_project=mapped_project,
                            status="replayed",
                            replays=[
                                self._serialize_0day_replay_result(item)
                                for item in crashing_replays
                            ],
                        )
                    )

        elapsed_seconds = round(time.monotonic() - started_at, 6)
        physical_replay_tasks_per_second = (
            physical_replay_tasks / elapsed_seconds if elapsed_seconds > 0 else 0.0
        )
        original_pov_instances_per_second = (
            len(source_records) / elapsed_seconds if elapsed_seconds > 0 else 0.0
        )
        deduplicated_replay_tasks_saved = naive_replay_tasks - physical_replay_tasks
        dedup_multiplier = (
            naive_replay_tasks / physical_replay_tasks
            if physical_replay_tasks > 0
            else 0.0
        )
        # These counters reflect the emitted crash-only 0day view, not
        # deduplicated physical replay tasks.
        summary["0day_count"] = len(zero_day_entries)
        summary["crashing_replay_count"] = sum(
            len(entry["replays"]) for entry in zero_day_entries
        )
        summary.update(
            {
                "elapsed_seconds": elapsed_seconds,
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
            }
        )

        manifest = {
            "source_dirs": [str(item) for item in input_source_dirs],
            "source_ids": sorted({record.source_id for record in source_records}),
            "oss_fuzz_path": str(self.oss_fuzz_path),
            "projects_root": str(self.projects_root),
            "output_dir": str(self.output_dir),
            "jobs": self.jobs,
            "per_pov_timeout": self.per_pov_timeout,
        }
        (self.output_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2),
            encoding="utf-8",
        )
        (self.output_dir / "summary.json").write_text(
            json.dumps(summary, indent=2),
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
