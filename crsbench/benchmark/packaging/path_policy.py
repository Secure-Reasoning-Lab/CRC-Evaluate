"""Path policy checks for benchmark scripts.

This module enforces benchmark script portability across CRSBench direct runs
and oss-crs sidecar runs by banning legacy hard-coded source roots.
"""

from dataclasses import dataclass
from pathlib import Path

# Legacy/custom helper roots that are not guaranteed by official OSS-Fuzz flows.
FORBIDDEN_PATH_TOKENS: tuple[str, ...] = ("/built-src", "/test-src")


@dataclass(frozen=True)
class PathPolicyViolation:
    """Single forbidden path occurrence in a benchmark file."""

    file: Path
    line_number: int
    token: str
    line: str


def _iter_candidate_files(benchmark_path: Path) -> list[Path]:
    candidates: list[Path] = []
    for pattern in ("**/*.sh", "Dockerfile", "Dockerfile.*"):
        candidates.extend(sorted(benchmark_path.glob(pattern)))
    # De-duplicate while preserving deterministic order.
    unique: list[Path] = []
    seen: set[Path] = set()
    for path in candidates:
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        unique.append(path)
    return unique


def _strip_inline_comment(line: str) -> str:
    """Strip shell/Docker-style inline comments while respecting quotes."""
    in_single = False
    in_double = False
    escaped = False

    for i, ch in enumerate(line):
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
            continue
        # In shell-style syntax, "#" starts a comment only at start-of-line
        # or when preceded by whitespace.
        if (
            ch == "#"
            and not in_single
            and not in_double
            and (i == 0 or line[i - 1].isspace())
        ):
            return line[:i]
    return line


def find_path_policy_violations(benchmark_path: Path) -> list[PathPolicyViolation]:
    """Find forbidden legacy source path tokens in benchmark files."""
    violations: list[PathPolicyViolation] = []
    for file_path in _iter_candidate_files(benchmark_path):
        try:
            lines = file_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for line_number, line in enumerate(lines, start=1):
            stripped = line.lstrip()
            # Ignore comment-only lines to avoid false positives in migration notes.
            if stripped.startswith("#"):
                continue
            candidate = _strip_inline_comment(line)
            for token in FORBIDDEN_PATH_TOKENS:
                if token in candidate:
                    violations.append(
                        PathPolicyViolation(
                            file=file_path,
                            line_number=line_number,
                            token=token,
                            line=line.strip(),
                        )
                    )
    return violations


def format_path_policy_error(
    benchmark_path: Path, violations: list[PathPolicyViolation]
) -> str:
    """Render a concise, deterministic validation error message."""
    rel_root = benchmark_path.resolve()
    formatted: list[str] = []
    for v in violations:
        try:
            rel = v.file.resolve().relative_to(rel_root)
            location = str(rel)
        except Exception:
            location = str(v.file)
        formatted.append(f"{location}:{v.line_number} contains '{v.token}'")
    return f"[{benchmark_path.name}] Forbidden legacy source paths: " + "; ".join(
        formatted
    )
