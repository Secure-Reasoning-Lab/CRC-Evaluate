"""Follow CRSBench systemd journals for one or more live cloud instances."""

from __future__ import annotations

import heapq
import json
import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, TextIO

from crsbench.cloud.cli._config_reconnect import (
    resolve_cloud_context,
    resolve_effective_experiment_name,
)
from crsbench.cloud.cli._instance_inventory import (
    CloudInstanceInventoryRow,
    list_cloud_instances,
    list_inventory_selector_matches,
    resolve_inventory_selector,
)
from crsbench.cloud.cli._remote_access import (
    build_ssh_command,
    print_selection_rows,
    select_target,
)
from crsbench.cloud.providers import provisioner_for_context
from crsbench.utils.logger import get_logger

if TYPE_CHECKING:
    import argparse

logger = get_logger(__name__)

_LOG_MERGE_BY_ARRIVAL = "arrival"
_LOG_MERGE_BY_TIMESTAMP = "timestamp"
_FAN_IN_RETRY_DELAY_SEC = 1.0
_FAN_IN_MAX_RETRIES = 2
_TIMESTAMP_REORDER_WINDOW_SEC = 1.0


@dataclass(frozen=True)
class _LogStreamEvent:
    """One event emitted by a live target stream."""

    kind: str
    target: CloudInstanceInventoryRow
    message: str = ""
    timestamp: float | None = None
    state: str | None = None


@dataclass(order=True)
class _BufferedJournalRecord:
    """Timestamp-ordered journal record buffered for best-effort merge."""

    timestamp: float
    sequence: int
    rendered_lines: tuple[str, ...] = field(compare=False)


@dataclass
class _TimestampMergeState:
    """Observed timestamp frontier for bounded best-effort merge ordering."""

    max_seen_timestamp: float | None = None
    latest_buffer_arrival_monotonic: float | None = None


def run_log(args: argparse.Namespace) -> int:
    """Follow role-appropriate CRSBench user journals on live cloud VMs."""
    experiment_name = resolve_effective_experiment_name(args.config, None)
    context = resolve_cloud_context(args.config, experiment_name)
    provisioner = provisioner_for_context(context)
    rows = list_cloud_instances(context, experiment_name, provisioner)
    if not rows:
        return 1

    targets = _resolve_log_targets(rows, args)
    if not targets:
        return 1

    if not _use_fan_in_log_mode(args):
        return _run_single_target_log(targets[0])
    return _run_multi_target_log_session(targets, merge_by=args.merge_by)


def _use_fan_in_log_mode(args: argparse.Namespace) -> bool:
    """Return whether cloud log should use the multi-stream fan-in path."""
    return bool(
        getattr(args, "all_instances", False)
        or getattr(args, "role", None)
        or getattr(args, "instances", None)
        or getattr(args, "merge_by", _LOG_MERGE_BY_ARRIVAL) != _LOG_MERGE_BY_ARRIVAL
    )


def _run_single_target_log(target: CloudInstanceInventoryRow) -> int:
    """Preserve the legacy single-target passthrough path."""
    cmd = build_ssh_command(
        target,
        remote_command=_build_log_remote_command(
            _service_name_for_role(target.role),
            structured=False,
        ),
    )
    try:
        return subprocess.run(cmd, check=False).returncode
    except KeyboardInterrupt:
        return 130


def _resolve_log_targets(
    rows: list[CloudInstanceInventoryRow],
    args: argparse.Namespace,
) -> list[CloudInstanceInventoryRow] | None:
    """Resolve one or more live log targets from CLI args."""
    explicit_multi_target = bool(
        getattr(args, "all_instances", False)
        or getattr(args, "role", None)
        or getattr(args, "instances", None)
    )
    if not explicit_multi_target:
        selected = select_target(rows, getattr(args, "instance", None))
        if selected is None:
            return None
        return [selected]

    resolved: list[CloudInstanceInventoryRow] = []
    seen: set[tuple[str, str, str, str]] = set()

    def add_row(row: CloudInstanceInventoryRow) -> None:
        row_key = _target_key(row)
        if row_key in seen:
            return
        seen.add(row_key)
        resolved.append(row)

    if getattr(args, "all_instances", False):
        for row in rows:
            add_row(row)

    role_selector = getattr(args, "role", None)
    if role_selector:
        resolved_role = _normalize_role_selector(role_selector)
        if resolved_role is None:
            logger.error(
                "Unsupported cloud log role selector {}. Use orch, work, or eval.",
                role_selector,
            )
            return None
        role_matches = [row for row in rows if row.role == resolved_role]
        if not role_matches:
            logger.error("No live cloud instances matched role {}", role_selector)
            print_selection_rows(rows)
            return None
        for row in role_matches:
            add_row(row)

    selectors: list[str] = []
    positional_selector = getattr(args, "instance", None)
    if positional_selector:
        selectors.append(positional_selector)
    selectors.extend(getattr(args, "instances", []) or [])

    for selector in selectors:
        matches = list_inventory_selector_matches(rows, selector)
        if not matches:
            logger.error("No live cloud instance matched selector {}", selector)
            print_selection_rows(rows)
            return None
        selected = resolve_inventory_selector(rows, selector)
        if selected is None:
            print_selection_rows(matches)
            logger.error(
                "Selector {} matched multiple live cloud instances in cloud log "
                "multi-target mode; choose a more specific selector or use --role/--all",
                selector,
            )
            return None
        add_row(selected)

    if not resolved:
        logger.error(
            "cloud log multi-target mode requires --all, --role, or at least one "
            "explicit --instance selector"
        )
        return None
    return resolved


def _normalize_role_selector(selector: str) -> str | None:
    """Normalize a role selector to the canonical inventory role name."""
    normalized = selector.strip().casefold()
    if normalized in {"orch", "orchestrator"}:
        return "orchestrator"
    if normalized in {"work", "worker"}:
        return "worker"
    if normalized in {"eval", "evaluator"}:
        return "evaluator"
    return None


def _run_multi_target_log_session(
    targets: list[CloudInstanceInventoryRow],
    *,
    merge_by: str,
) -> int:
    """Fan in live journal streams from multiple targets into one local session."""
    event_queue: queue.Queue[_LogStreamEvent] = queue.Queue()
    stop_event = threading.Event()
    active_processes: dict[tuple[str, str, str, str], subprocess.Popen[str]] = {}
    active_processes_lock = threading.Lock()
    worker_threads = [
        threading.Thread(
            target=_stream_target_logs,
            args=(
                target,
                event_queue,
                stop_event,
                active_processes,
                active_processes_lock,
            ),
            daemon=True,
        )
        for target in targets
    ]
    for worker_thread in worker_threads:
        worker_thread.start()

    any_stream_started = False
    completed_targets = 0
    final_states: dict[tuple[str, str, str, str], str] = {}
    buffered_records: list[_BufferedJournalRecord] = []
    merge_state = _TimestampMergeState()
    next_sequence = 0

    try:
        while completed_targets < len(targets):
            try:
                event = event_queue.get(timeout=0.1)
            except queue.Empty:
                if merge_by == _LOG_MERGE_BY_TIMESTAMP:
                    _flush_ready_buffered_records(
                        buffered_records,
                        merge_state,
                        time.monotonic(),
                    )
                continue

            if event.kind == "stream_started":
                any_stream_started = True
                continue

            if event.kind == "journal_record":
                rendered_lines = _render_prefixed_lines(
                    event.target,
                    "journal",
                    event.message,
                )
                if merge_by == _LOG_MERGE_BY_TIMESTAMP:
                    arrival_monotonic = time.monotonic()
                    journal_timestamp = (
                        event.timestamp if event.timestamp is not None else time.time()
                    )
                    heapq.heappush(
                        buffered_records,
                        _BufferedJournalRecord(
                            timestamp=journal_timestamp,
                            sequence=next_sequence,
                            rendered_lines=rendered_lines,
                        ),
                    )
                    _note_buffered_timestamp_record(
                        merge_state,
                        journal_timestamp,
                        arrival_monotonic,
                    )
                    next_sequence += 1
                    _flush_ready_buffered_records(
                        buffered_records,
                        merge_state,
                        arrival_monotonic,
                    )
                else:
                    _emit_rendered_lines(rendered_lines)
                continue

            if merge_by == _LOG_MERGE_BY_TIMESTAMP:
                _flush_ready_buffered_records(
                    buffered_records,
                    merge_state,
                    time.monotonic(),
                )

            if event.kind == "transport_record":
                _emit_rendered_lines(
                    _render_prefixed_lines(event.target, "transport", event.message)
                )
                continue

            if event.kind == "control":
                _emit_rendered_lines(
                    _render_prefixed_lines(event.target, "control", event.message)
                )
                continue

            if event.kind == "target_done":
                final_states[_target_key(event.target)] = event.state or "detached"
                completed_targets += 1

        if merge_by == _LOG_MERGE_BY_TIMESTAMP:
            _flush_all_buffered_records(buffered_records)
    except KeyboardInterrupt:
        stop_event.set()
        _terminate_stream_processes(active_processes, active_processes_lock)
        return 130
    finally:
        stop_event.set()
        _terminate_stream_processes(active_processes, active_processes_lock)
        for worker_thread in worker_threads:
            worker_thread.join(timeout=1.0)

    if not any_stream_started:
        return 1
    if final_states and all(state == "failed" for state in final_states.values()):
        return 1
    return 0


def _stream_target_logs(
    target: CloudInstanceInventoryRow,
    event_queue: queue.Queue[_LogStreamEvent],
    stop_event: threading.Event,
    active_processes: dict[tuple[str, str, str, str], subprocess.Popen[str]],
    active_processes_lock: threading.Lock,
) -> None:
    """Stream one target's journal with bounded reconnect retries."""
    service_name = _service_name_for_role(target.role)
    attempts = 0

    while not stop_event.is_set():
        try:
            cmd = build_ssh_command(
                target,
                remote_command=_build_log_remote_command(
                    service_name,
                    structured=True,
                ),
            )
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except Exception as exc:
            attempts += 1
            if attempts <= _FAN_IN_MAX_RETRIES and not stop_event.is_set():
                event_queue.put(
                    _LogStreamEvent(
                        kind="control",
                        target=target,
                        message=(
                            f"failed to start log stream: {exc}; retrying in "
                            f"{_FAN_IN_RETRY_DELAY_SEC:.1f}s "
                            f"({attempts}/{_FAN_IN_MAX_RETRIES})"
                        ),
                    )
                )
                if stop_event.wait(_FAN_IN_RETRY_DELAY_SEC):
                    return
                continue

            event_queue.put(
                _LogStreamEvent(
                    kind="control",
                    target=target,
                    message=f"failed to start log stream: {exc}",
                )
            )
            event_queue.put(
                _LogStreamEvent(kind="target_done", target=target, state="failed")
            )
            return

        with active_processes_lock:
            active_processes[_target_key(target)] = process

        stream_started = threading.Event()
        stdout_thread = threading.Thread(
            target=_read_target_stdout,
            args=(target, process.stdout, event_queue, stream_started, stop_event),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=_read_target_stderr,
            args=(target, process.stderr, event_queue, stop_event),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()
        returncode = process.wait()
        stdout_thread.join(timeout=1.0)
        stderr_thread.join(timeout=1.0)

        with active_processes_lock:
            active_processes.pop(_target_key(target), None)

        if stop_event.is_set():
            return

        if returncode == 0:
            event_queue.put(
                _LogStreamEvent(kind="target_done", target=target, state="detached")
            )
            return

        attempts += 1
        if attempts <= _FAN_IN_MAX_RETRIES:
            event_queue.put(
                _LogStreamEvent(
                    kind="control",
                    target=target,
                    message=(
                        f"log stream exited with code {returncode}; retrying in "
                        f"{_FAN_IN_RETRY_DELAY_SEC:.1f}s "
                        f"({attempts}/{_FAN_IN_MAX_RETRIES})"
                    ),
                )
            )
            if stop_event.wait(_FAN_IN_RETRY_DELAY_SEC):
                return
            continue

        event_queue.put(
            _LogStreamEvent(
                kind="control",
                target=target,
                message=f"log stream exited with code {returncode}",
            )
        )
        event_queue.put(
            _LogStreamEvent(kind="target_done", target=target, state="failed")
        )
        return


def _read_target_stdout(
    target: CloudInstanceInventoryRow,
    stream: TextIO | None,
    event_queue: queue.Queue[_LogStreamEvent],
    stream_started: threading.Event,
    stop_event: threading.Event,
) -> None:
    """Read structured journal output for one target."""
    if stream is None:
        return
    try:
        for raw_line in stream:
            if stop_event.is_set():
                return
            line = raw_line.rstrip("\n")
            if not stream_started.is_set():
                stream_started.set()
                event_queue.put(_LogStreamEvent(kind="stream_started", target=target))
            timestamp, message = _parse_journal_line(line)
            event_queue.put(
                _LogStreamEvent(
                    kind="journal_record",
                    target=target,
                    message=message,
                    timestamp=timestamp,
                )
            )
    finally:
        stream.close()


def _read_target_stderr(
    target: CloudInstanceInventoryRow,
    stream: TextIO | None,
    event_queue: queue.Queue[_LogStreamEvent],
    stop_event: threading.Event,
) -> None:
    """Read transport stderr for one target without blocking other streams."""
    if stream is None:
        return
    try:
        for raw_line in stream:
            if stop_event.is_set():
                return
            line = raw_line.rstrip("\n")
            if not line:
                continue
            event_queue.put(
                _LogStreamEvent(
                    kind="transport_record",
                    target=target,
                    message=line,
                )
            )
    finally:
        stream.close()


def _build_log_remote_command(
    service_name: str,
    *,
    structured: bool,
) -> list[str]:
    """Build the remote journalctl invocation for cloud log."""
    uid_expr = 'uid="$(id -u crsbench 2>/dev/null || echo 1001)"'
    journal_cmd = f"journalctl --user -u {service_name} -b -f --no-pager"
    if structured:
        journal_cmd += " -o json"
    return [
        "bash",
        "-lc",
        (
            f"{uid_expr}; "
            "sudo -u crsbench env "
            'XDG_RUNTIME_DIR="/run/user/${uid}" '
            'DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/${uid}/bus" '
            f"{journal_cmd}"
        ),
    ]


def _parse_journal_line(raw_line: str) -> tuple[float, str]:
    """Parse one journalctl output line into a timestamp and rendered message."""
    fallback_timestamp = time.time()
    try:
        payload = json.loads(raw_line)
    except json.JSONDecodeError:
        return fallback_timestamp, raw_line

    message = _coerce_journal_value(payload.get("MESSAGE"))
    if not message:
        message = raw_line
    timestamp = (
        _parse_journal_timestamp(payload.get("__REALTIME_TIMESTAMP"))
        or _parse_journal_timestamp(payload.get("_SOURCE_REALTIME_TIMESTAMP"))
        or fallback_timestamp
    )
    return timestamp, message


def _coerce_journal_value(value: object) -> str:
    """Coerce one journal field value into readable text."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        parts = [_coerce_journal_value(item) for item in value]
        return " ".join(part for part in parts if part)
    return json.dumps(value, sort_keys=True)


def _parse_journal_timestamp(value: object) -> float | None:
    """Parse a journal microsecond timestamp into Unix seconds."""
    text = _coerce_journal_value(value).strip()
    if not text:
        return None
    try:
        return int(text) / 1_000_000
    except ValueError:
        return None


def _render_prefixed_lines(
    target: CloudInstanceInventoryRow,
    source_kind: str,
    message: str,
) -> tuple[str, ...]:
    """Render one logical event as prefixed output lines."""
    prefix = f"{target.alias} | {target.role} | {source_kind}"
    lines = message.splitlines() or [""]
    return tuple(f"{prefix} | {line}" if line else f"{prefix} |" for line in lines)


def _emit_rendered_lines(lines: tuple[str, ...]) -> None:
    """Write rendered lines to stdout without ANSI decoration."""
    for line in lines:
        sys.stdout.write(f"{line}\n")
    sys.stdout.flush()


def _note_buffered_timestamp_record(
    merge_state: _TimestampMergeState,
    record_timestamp: float,
    arrival_monotonic: float,
) -> None:
    """Advance the observed source timestamp frontier for timestamp merge."""
    if (
        merge_state.max_seen_timestamp is None
        or record_timestamp > merge_state.max_seen_timestamp
    ):
        merge_state.max_seen_timestamp = record_timestamp
    merge_state.latest_buffer_arrival_monotonic = arrival_monotonic


def _flush_ready_buffered_records(
    buffered_records: list[_BufferedJournalRecord],
    merge_state: _TimestampMergeState,
    now_monotonic: float,
) -> None:
    """Flush buffered timestamp-ordered records that are ready to render."""
    if merge_state.max_seen_timestamp is not None:
        remote_watermark = (
            merge_state.max_seen_timestamp - _TIMESTAMP_REORDER_WINDOW_SEC
        )
        while buffered_records and buffered_records[0].timestamp <= remote_watermark:
            record = heapq.heappop(buffered_records)
            _emit_rendered_lines(record.rendered_lines)

    if (
        buffered_records
        and merge_state.latest_buffer_arrival_monotonic is not None
        and now_monotonic - merge_state.latest_buffer_arrival_monotonic
        >= _TIMESTAMP_REORDER_WINDOW_SEC
    ):
        _flush_all_buffered_records(buffered_records)

    if not buffered_records:
        merge_state.latest_buffer_arrival_monotonic = None


def _flush_all_buffered_records(
    buffered_records: list[_BufferedJournalRecord],
) -> None:
    """Flush every remaining buffered timestamp-ordered record."""
    while buffered_records:
        record = heapq.heappop(buffered_records)
        _emit_rendered_lines(record.rendered_lines)


def _terminate_stream_processes(
    active_processes: dict[tuple[str, str, str, str], subprocess.Popen[str]],
    active_processes_lock: threading.Lock,
) -> None:
    """Terminate active transport subprocesses after operator interrupt/shutdown."""
    with active_processes_lock:
        processes = list(active_processes.values())
        active_processes.clear()

    for process in processes:
        try:
            if process.poll() is None:
                process.terminate()
        except Exception:
            continue

    for process in processes:
        try:
            if process.poll() is None:
                process.kill()
        except Exception:
            continue


def _target_key(target: CloudInstanceInventoryRow) -> tuple[str, str, str, str]:
    """Return the stable identity key for one live cloud instance row."""
    return (target.provider, target.project, target.zone, target.name)


def _service_name_for_role(role: str) -> str:
    if role == "orchestrator":
        return "crsbench-orchestrator.service"
    if role == "evaluator":
        return "crsbench-evaluator.service"
    return "crsbench-worker.service"
