"""Shared manifest utilities for incremental dataset upload/download."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from pathlib import Path

MANIFEST_FILE = ".crsbench-manifest.json"
REMOTE_INDEX_PATH = "index/benchmarks.jsonl"
REMOTE_INDEX_ALIAS_PATH = "benchmarks-metadata.jsonl"
GROUND_TRUTH_DIR = ".aixcc"


class _Digest(Protocol):
    def update(self, data: bytes, /) -> object: ...


@dataclass
class BenchmarkManifestEntry:
    """Fingerprint and metadata for one benchmark bundle."""

    benchmark: str
    benchmark_source_sha256: str
    ground_truth_source_sha256: str
    benchmark_archive_sha256: str = ""
    ground_truth_archive_sha256: str = ""
    benchmark_archive_size: int = 0
    ground_truth_archive_size: int = 0
    source_commit: str = ""
    updated_at: str = ""
    has_ground_truth: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "BenchmarkManifestEntry":
        """Build entry from dict, tolerating missing optional fields."""
        return cls(
            benchmark=str(data.get("benchmark", "")),
            benchmark_source_sha256=str(data.get("benchmark_source_sha256", "")),
            ground_truth_source_sha256=str(data.get("ground_truth_source_sha256", "")),
            benchmark_archive_sha256=str(data.get("benchmark_archive_sha256", "")),
            ground_truth_archive_sha256=str(data.get("ground_truth_archive_sha256", "")),
            benchmark_archive_size=int(data.get("benchmark_archive_size", 0)),
            ground_truth_archive_size=int(data.get("ground_truth_archive_size", 0)),
            source_commit=str(data.get("source_commit", "")),
            updated_at=str(data.get("updated_at", "")),
            has_ground_truth=bool(data.get("has_ground_truth", False)),
        )

    def to_dict(self) -> dict[str, object]:
        """Serialize entry to a JSON-compatible dict."""
        return asdict(self)


def compute_source_fingerprints(benchmark_dir: Path) -> tuple[str, str]:
    """Compute deterministic source hashes for benchmark and ground truth trees."""
    benchmark_hash = hashlib.sha256()
    ground_truth_hash = hashlib.sha256()

    for path in _iter_files(benchmark_dir):
        rel = path.relative_to(benchmark_dir)
        if rel.parts and rel.parts[0] == GROUND_TRUTH_DIR:
            _update_hash(ground_truth_hash, rel, path)
        else:
            _update_hash(benchmark_hash, rel, path)

    return benchmark_hash.hexdigest(), ground_truth_hash.hexdigest()


def compute_file_sha256(path: Path) -> str:
    """Compute SHA256 of a file."""
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_remote_index(path: Path) -> dict[str, BenchmarkManifestEntry]:
    """Load remote JSONL index into a benchmark->entry mapping."""
    if not path.is_file():
        return {}
    entries: dict[str, BenchmarkManifestEntry] = {}
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            data = json.loads(line)
            entry = BenchmarkManifestEntry.from_dict(data)
            if entry.benchmark:
                entries[entry.benchmark] = entry
    return entries


def write_remote_index(path: Path, entries: dict[str, BenchmarkManifestEntry]) -> None:
    """Write remote JSONL index from benchmark->entry mapping."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for name in sorted(entries):
            f.write(json.dumps(entries[name].to_dict(), sort_keys=True))
            f.write("\n")


def load_local_manifest(output_dir: Path) -> dict[str, BenchmarkManifestEntry]:
    """Load local installed-manifest (`.crsbench-manifest.json`)."""
    manifest_path = output_dir / MANIFEST_FILE
    if not manifest_path.is_file():
        return {}
    with manifest_path.open() as f:
        data = json.load(f)
    entries: dict[str, BenchmarkManifestEntry] = {}
    for name, raw in data.items():
        if isinstance(raw, dict):
            entries[name] = BenchmarkManifestEntry.from_dict(raw)
    return entries


def write_local_manifest(
    output_dir: Path,
    entries: dict[str, BenchmarkManifestEntry],
) -> None:
    """Write local installed-manifest (`.crsbench-manifest.json`)."""
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / MANIFEST_FILE
    data = {name: entry.to_dict() for name, entry in sorted(entries.items())}
    with manifest_path.open("w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")


def _iter_files(root: Path) -> list[Path]:
    """Return sorted file list under root."""
    return sorted(p for p in root.rglob("*") if p.is_file())


def _update_hash(digest: _Digest, rel: Path, path: Path) -> None:
    """Update digest with path identity and file bytes."""
    digest.update(str(rel).encode("utf-8"))
    digest.update(b"\0")
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
