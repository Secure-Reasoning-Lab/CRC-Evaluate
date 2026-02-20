"""Crash signature parsing and hashing for sanitizer stack traces.

This module parses ASAN/sanitizer crash logs into deterministic crash
signatures for deduplication. Supported formats:
- ASAN/AddressSanitizer: heap-buffer-overflow, use-after-free, etc.
- libFuzzer: timeout, out-of-memory
- Java/Jazzer: Java exceptions with stack traces

Each crash signature contains:
- crash_type: The type of crash (e.g., "heap-buffer-overflow")
- frames: Parsed stack frames (top N)
- signature_hash: Deterministic SHA256 hash for grouping
- raw_summary: SUMMARY line for display
"""

import hashlib
import re
from dataclasses import dataclass, field
from typing import Optional

from crsbench.utils.logger import get_logger

logger = get_logger(__name__)

# Regex patterns for stack frame parsing
# ASAN C/C++ with source: #N 0xaddr in func_name file.c:line[:col]
# Format: path/file.c:LINE:COL or path/file.c:LINE
# File path is non-greedy (.+?) to stop at the first :LINE pattern.
_ASAN_FRAME_WITH_SOURCE = re.compile(
    r"^\s*#(\d+)\s+0x[0-9a-fA-F]+\s+in\s+(\S+)\s+(.+?):(\d+)(?::\d+)?"
)
# ASAN C/C++ without source: #N 0xaddr in func_name (module+0xoffset)
_ASAN_FRAME_NO_SOURCE = re.compile(
    r"^\s*#(\d+)\s+0x[0-9a-fA-F]+\s+in\s+(\S+)\s+\(([^)]+)\+0x[0-9a-fA-F]+\)"
)
# ASAN frame with just address (no function name): #N 0xaddr (module+0xoffset)
_ASAN_FRAME_ADDR_ONLY = re.compile(
    r"^\s*#(\d+)\s+0x[0-9a-fA-F]+\s+\(([^)]+)\+0x[0-9a-fA-F]+\)"
)

# Java stack trace: at pkg.Class.method(File.java:line)
_JAVA_FRAME = re.compile(r"^\s*at\s+(\S+)\(([^:]+):(\d+)\)")

# Internal sanitizer frames to filter out
_SANITIZER_INTERNAL_PREFIXES = (
    "__sanitizer",
    "__interceptor",
    "__asan",
    "__msan",
    "__tsan",
    "__ubsan",
    "__lsan",
)

# ASAN ERROR line: ==PID==ERROR: AddressSanitizer: CRASH_TYPE on/at ...
_ASAN_ERROR = re.compile(r"==\d+==ERROR:\s+(\w+Sanitizer):\s+(\S+)")

# Java exception: == Java Exception: class_name
_JAVA_EXCEPTION = re.compile(r"==\s*Java Exception:\s*(\S+)")

# libFuzzer SUMMARY: SUMMARY: libFuzzer: type
_LIBFUZZER_SUMMARY = re.compile(r"SUMMARY:\s+libFuzzer:\s+(\S+)")


@dataclass
class StackFrame:
    """A single parsed stack frame."""

    frame_num: int
    function: str
    file: str
    line: int


@dataclass
class CrashSignature:
    """Deterministic crash signature from sanitizer output.

    Attributes:
        frames: Parsed stack frames (top N, filtered)
        crash_type: e.g., "heap-buffer-overflow", "timeout"
        signature_hash: SHA256 hex hash (first 16 chars)
        raw_summary: SUMMARY line from sanitizer output
    """

    frames: list[StackFrame] = field(default_factory=list)
    crash_type: str = ""
    signature_hash: str = ""
    raw_summary: str = ""


def compute_signature_hash(crash_type: str, frames: list[StackFrame]) -> str:
    """Compute deterministic hash from crash type and stack frames.

    Args:
        crash_type: The crash type string
        frames: List of stack frames

    Returns:
        First 16 hex chars of SHA256 hash
    """
    parts = [crash_type]
    for f in frames:
        parts.append(f"{f.function}:{f.file}:{f.line}")
    payload = "|".join(parts)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _is_sanitizer_internal(function_name: str) -> bool:
    """Check if a function is an internal sanitizer frame."""
    return any(
        function_name.startswith(prefix) for prefix in _SANITIZER_INTERNAL_PREFIXES
    )


def _parse_asan_frames(
    lines: list[str], start_idx: int, *, top_n: int
) -> list[StackFrame]:
    """Parse ASAN stack frames from log lines.

    Args:
        lines: All log lines
        start_idx: Index to start scanning from
        top_n: Maximum number of frames to return

    Returns:
        List of parsed StackFrame objects (up to top_n, filtered)
    """
    frames: list[StackFrame] = []

    for i in range(start_idx, len(lines)):
        line = lines[i]

        # Stop if we hit a blank line or non-frame line after finding frames
        if frames and not line.strip().startswith("#"):
            break

        # Try source-info frame first
        match = _ASAN_FRAME_WITH_SOURCE.match(line)
        if match:
            func = match.group(2)
            if _is_sanitizer_internal(func):
                continue
            frames.append(
                StackFrame(
                    frame_num=int(match.group(1)),
                    function=func,
                    file=match.group(3),
                    line=int(match.group(4)),
                )
            )
            if len(frames) >= top_n:
                break
            continue

        # Try no-source frame
        match = _ASAN_FRAME_NO_SOURCE.match(line)
        if match:
            func = match.group(2)
            if _is_sanitizer_internal(func):
                continue
            frames.append(
                StackFrame(
                    frame_num=int(match.group(1)),
                    function=func,
                    file=match.group(3),
                    line=0,
                )
            )
            if len(frames) >= top_n:
                break
            continue

        # Try address-only frame
        match = _ASAN_FRAME_ADDR_ONLY.match(line)
        if match:
            frames.append(
                StackFrame(
                    frame_num=int(match.group(1)),
                    function="<unknown>",
                    file=match.group(2),
                    line=0,
                )
            )
            if len(frames) >= top_n:
                break

    return frames


def _parse_java_frames(
    lines: list[str], start_idx: int, *, top_n: int
) -> list[StackFrame]:
    """Parse Java stack trace frames.

    Args:
        lines: All log lines
        start_idx: Index to start scanning from
        top_n: Maximum number of frames to return

    Returns:
        List of parsed StackFrame objects (up to top_n)
    """
    frames: list[StackFrame] = []

    for i in range(start_idx, len(lines)):
        match = _JAVA_FRAME.match(lines[i])
        if match:
            frames.append(
                StackFrame(
                    frame_num=len(frames),
                    function=match.group(1),
                    file=match.group(2),
                    line=int(match.group(3)),
                )
            )
            if len(frames) >= top_n:
                break

    return frames


def _extract_summary_line(lines: list[str]) -> str:
    """Extract the SUMMARY line from log output."""
    for line in lines:
        if line.strip().startswith("SUMMARY:"):
            return line.strip()
    return ""


def parse_crash_signature(
    crash_log: str, *, top_n: int = 5
) -> Optional[CrashSignature]:
    """Parse sanitizer crash log into a deterministic crash signature.

    Supports:
    - ASAN/AddressSanitizer stack traces (C/C++)
    - Java/Jazzer exception traces
    - libFuzzer timeout and out-of-memory

    Args:
        crash_log: Full crash log text from sanitizer output
        top_n: Maximum number of stack frames to include in signature

    Returns:
        CrashSignature if parseable, None if no recognizable crash pattern found
    """
    if not crash_log:
        return None

    lines = crash_log.splitlines()
    raw_summary = _extract_summary_line(lines)

    # Try ASAN/sanitizer ERROR pattern
    for i, line in enumerate(lines):
        match = _ASAN_ERROR.search(line)
        if match:
            crash_type = match.group(2)
            # Parse frames starting after the ERROR line
            frames = _parse_asan_frames(lines, i + 1, top_n=top_n)
            if not frames:
                # No frames parsed — can't distinguish crash sites.
                # Return None to avoid false-positive dedup.
                return None
            sig_hash = compute_signature_hash(crash_type, frames)
            return CrashSignature(
                frames=frames,
                crash_type=crash_type,
                signature_hash=sig_hash,
                raw_summary=raw_summary,
            )

    # Try Java exception pattern
    for i, line in enumerate(lines):
        match = _JAVA_EXCEPTION.search(line)
        if match:
            crash_type = match.group(1)
            frames = _parse_java_frames(lines, i + 1, top_n=top_n)
            if not frames:
                return None
            sig_hash = compute_signature_hash(crash_type, frames)
            return CrashSignature(
                frames=frames,
                crash_type=crash_type,
                signature_hash=sig_hash,
                raw_summary=raw_summary,
            )

    # Try standard Java exception (Exception in thread ...)
    for i, line in enumerate(lines):
        if line.strip().startswith("Exception in thread "):
            # Extract exception class from the line: Exception in thread "main" pkg.ClassName: msg
            parts = line.strip().split('"', 2)
            if len(parts) >= 3:
                exception_part = parts[2].strip().split(":", 1)[0].strip()
                crash_type = exception_part if exception_part else "UnknownException"
            else:
                crash_type = "UnknownException"
            frames = _parse_java_frames(lines, i + 1, top_n=top_n)
            if not frames:
                return None
            sig_hash = compute_signature_hash(crash_type, frames)
            return CrashSignature(
                frames=frames,
                crash_type=crash_type,
                signature_hash=sig_hash,
                raw_summary=raw_summary,
            )

    # Try libFuzzer SUMMARY (timeout, out-of-memory)
    for line in lines:
        match = _LIBFUZZER_SUMMARY.search(line)
        if match:
            crash_type = match.group(1)
            sig_hash = compute_signature_hash(crash_type, [])
            return CrashSignature(
                frames=[],
                crash_type=crash_type,
                signature_hash=sig_hash,
                raw_summary=line.strip(),
            )

    # No recognizable pattern
    return None
