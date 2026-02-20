"""POV deduplication for benchmark management.

Scans .aixcc/{harness}/{cpv}/blobs/ and logs/ directories, parses crash
signatures from log files, and removes duplicate POV variants that share
the same crash site.

pov_0 is always treated as ground truth and never removed.
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from crsbench.evaluation.verification.crash_signature import parse_crash_signature
from crsbench.utils.logger import get_logger

logger = get_logger(__name__)

# Match pov_N in filename
_POV_NUM_RE = re.compile(r"^pov_(\d+)$")


@dataclass
class PovEntry:
    """A POV blob/log pair in a CPV directory."""

    pov_num: int
    blob_path: Path
    log_path: Optional[Path]
    signature_hash: Optional[str] = None


@dataclass
class DedupResult:
    """Result of deduplicating a single CPV directory."""

    harness: str
    cpv: str
    total_povs: int = 0
    kept: list[int] = field(default_factory=list)
    removed: list[int] = field(default_factory=list)
    unparseable: list[int] = field(default_factory=list)


@dataclass
class DedupSummary:
    """Aggregated result across all CPVs in a benchmark."""

    benchmark: str
    cpv_results: list[DedupResult] = field(default_factory=list)

    @property
    def total_povs(self) -> int:
        return sum(r.total_povs for r in self.cpv_results)

    @property
    def total_kept(self) -> int:
        return sum(len(r.kept) for r in self.cpv_results)

    @property
    def total_removed(self) -> int:
        return sum(len(r.removed) for r in self.cpv_results)

    def to_dict(self) -> dict:
        return {
            "benchmark": self.benchmark,
            "total_povs": self.total_povs,
            "total_kept": self.total_kept,
            "total_removed": self.total_removed,
            "cpv_results": [
                {
                    "harness": r.harness,
                    "cpv": r.cpv,
                    "total_povs": r.total_povs,
                    "kept": r.kept,
                    "removed": r.removed,
                    "unparseable": r.unparseable,
                }
                for r in self.cpv_results
            ],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)


def scan_povs(cpv_dir: Path) -> list[PovEntry]:
    """Scan a CPV directory for pov_N.blob files and match with logs.

    Args:
        cpv_dir: Path to .aixcc/{harness}/{cpv}/ directory

    Returns:
        List of PovEntry sorted by pov_num
    """
    blobs_dir = cpv_dir / "blobs"
    logs_dir = cpv_dir / "logs"

    if not blobs_dir.is_dir():
        return []

    entries: list[PovEntry] = []

    for blob_path in sorted(blobs_dir.iterdir()):
        if not blob_path.is_file() or blob_path.suffix != ".blob":
            continue

        match = _POV_NUM_RE.match(blob_path.stem)
        if not match:
            continue

        pov_num = int(match.group(1))
        log_path = logs_dir / f"pov_{pov_num}.log"
        if not log_path.is_file():
            log_path = None

        entries.append(
            PovEntry(
                pov_num=pov_num,
                blob_path=blob_path,
                log_path=log_path,
            )
        )

    return sorted(entries, key=lambda e: e.pov_num)


def dedup_cpv_povs(
    cpv_dir: Path,
    harness: str,
    cpv: str,
    *,
    top_n: int = 5,
    dry_run: bool = True,
) -> DedupResult:
    """Deduplicate POVs within a single CPV directory.

    pov_0 is always kept as ground truth. Its signature seeds the
    seen_hashes set. Subsequent POVs with the same hash are removed.
    POVs with unparseable logs are conservatively kept.

    Args:
        cpv_dir: Path to .aixcc/{harness}/{cpv}/ directory
        harness: Harness name (for reporting)
        cpv: CPV identifier (for reporting)
        top_n: Stack frame depth for signature parsing
        dry_run: If True, report only; if False, delete duplicate files

    Returns:
        DedupResult with kept/removed/unparseable POV numbers
    """
    entries = scan_povs(cpv_dir)
    result = DedupResult(
        harness=harness,
        cpv=cpv,
        total_povs=len(entries),
    )

    if not entries:
        return result

    seen_hashes: set[str] = set()

    for entry in entries:
        # Parse signature from log
        if entry.log_path:
            log_text = entry.log_path.read_text(errors="replace")
            sig = parse_crash_signature(log_text, top_n=top_n)
            if sig:
                entry.signature_hash = sig.signature_hash
            else:
                entry.signature_hash = None
        else:
            entry.signature_hash = None

        # pov_0 is always kept as ground truth
        if entry.pov_num == 0:
            result.kept.append(entry.pov_num)
            if entry.signature_hash:
                seen_hashes.add(entry.signature_hash)
            continue

        # No parseable signature → conservatively keep
        if not entry.signature_hash:
            result.kept.append(entry.pov_num)
            result.unparseable.append(entry.pov_num)
            continue

        # Check for duplicate
        if entry.signature_hash in seen_hashes:
            result.removed.append(entry.pov_num)
            if not dry_run:
                entry.blob_path.unlink(missing_ok=True)
                if entry.log_path:
                    entry.log_path.unlink(missing_ok=True)
        else:
            seen_hashes.add(entry.signature_hash)
            result.kept.append(entry.pov_num)

    return result


def dedup_benchmark_povs(
    benchmark_path: Path,
    *,
    harness_filter: Optional[str] = None,
    cpv_filter: Optional[str] = None,
    top_n: int = 5,
    dry_run: bool = True,
) -> DedupSummary:
    """Deduplicate POVs across all CPVs in a benchmark.

    Scans .aixcc/{harness}/{cpv}/ directories for POV blob/log pairs,
    groups by crash signature, and removes duplicates.

    Args:
        benchmark_path: Path to benchmark root directory
        harness_filter: If set, only process this harness
        cpv_filter: If set, only process this CPV (e.g., "cpv_0")
        top_n: Stack frame depth for signature parsing
        dry_run: If True, report only; if False, delete duplicate files

    Returns:
        DedupSummary with per-CPV results
    """
    aixcc_dir = benchmark_path / ".aixcc"
    if not aixcc_dir.is_dir():
        raise ValueError(f"No .aixcc directory found at {benchmark_path}")

    summary = DedupSummary(benchmark=benchmark_path.name)

    for harness_dir in sorted(aixcc_dir.iterdir()):
        if not harness_dir.is_dir():
            continue
        # Skip non-harness entries (meta.yaml, ref.diff, etc.)
        if harness_dir.name.startswith("."):
            continue

        harness_name = harness_dir.name

        if harness_filter and harness_name != harness_filter:
            continue

        for cpv_dir in sorted(harness_dir.iterdir()):
            if not cpv_dir.is_dir():
                continue
            if not cpv_dir.name.startswith("cpv_"):
                continue

            cpv_name = cpv_dir.name

            if cpv_filter and cpv_name != cpv_filter:
                continue

            # Check if this CPV has blobs
            blobs_dir = cpv_dir / "blobs"
            if not blobs_dir.is_dir():
                continue

            result = dedup_cpv_povs(
                cpv_dir,
                harness_name,
                cpv_name,
                top_n=top_n,
                dry_run=dry_run,
            )
            summary.cpv_results.append(result)

            action = "would remove" if dry_run else "removed"
            logger.info(
                f"{harness_name}/{cpv_name}: "
                f"{result.total_povs} POVs, "
                f"{len(result.kept)} kept, "
                f"{len(result.removed)} {action}"
            )

    return summary
