"""Import POV variants discovered by a bug-finding CRS into a benchmark.

Bug-finding result layout (input):

    <source_root>/
      <benchmark_name>/
        <harness_name>/
          <build_mode>/                  # commonly "full"
            <sanitizer>/                 # e.g. "address", "undefined"
              trial-*/
                povs/
                  cpvs/
                    <cpv_id>/
                      blobs/<hash>.blob
                      crash_logs/<hash>-...log

Benchmark POV layout (output):

    <benchmark>/.aixcc/<harness>/<cpv_id>/
      blobs/pov_N.blob
      logs/pov_N.log

Discovered blobs are added as new pov_N.blob files (next free index) only when
their content hash does not already match an existing pov_*.blob in the
target CPV. The original pov_0.blob (canonical/intended POV) is never
touched.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

from crsbench.utils.logger import get_logger

if TYPE_CHECKING:
    from pathlib import Path

logger = get_logger(__name__)

_POV_NUM_RE = re.compile(r"^pov_(\d+)$")
_BLOB_GLOB = "*.blob"


@dataclass
class CpvImportResult:
    """Result of importing POVs for one (harness, cpv) pair."""

    harness: str
    cpv: str
    existing_povs: int = 0
    candidates_found: int = 0
    skipped_duplicate: int = 0
    skipped_capacity: int = 0
    imported_povs: list[int] = field(default_factory=list)
    sources: dict[int, str] = field(default_factory=dict)  # pov_num -> source path

    def to_dict(self) -> dict:
        return {
            "harness": self.harness,
            "cpv": self.cpv,
            "existing_povs": self.existing_povs,
            "candidates_found": self.candidates_found,
            "skipped_duplicate": self.skipped_duplicate,
            "skipped_capacity": self.skipped_capacity,
            "imported_povs": self.imported_povs,
            "sources": {str(k): v for k, v in self.sources.items()},
        }


@dataclass
class BenchmarkImportSummary:
    """Aggregate result across all CPVs in one benchmark."""

    benchmark: str
    cpv_results: list[CpvImportResult] = field(default_factory=list)

    @property
    def total_imported(self) -> int:
        return sum(len(r.imported_povs) for r in self.cpv_results)

    @property
    def total_candidates(self) -> int:
        return sum(r.candidates_found for r in self.cpv_results)

    @property
    def total_duplicates(self) -> int:
        return sum(r.skipped_duplicate for r in self.cpv_results)

    @property
    def total_capacity_skipped(self) -> int:
        return sum(r.skipped_capacity for r in self.cpv_results)

    def to_dict(self) -> dict:
        return {
            "benchmark": self.benchmark,
            "total_candidates": self.total_candidates,
            "total_imported": self.total_imported,
            "total_duplicates": self.total_duplicates,
            "total_capacity_skipped": self.total_capacity_skipped,
            "cpv_results": [r.to_dict() for r in self.cpv_results],
        }


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _existing_pov_files(blobs_dir: Path) -> list[tuple[int, Path]]:
    """Return existing (pov_num, path) pairs for pov_N.blob files, sorted."""
    if not blobs_dir.is_dir():
        return []
    out: list[tuple[int, Path]] = []
    for p in blobs_dir.iterdir():
        if not p.is_file() or p.suffix != ".blob":
            continue
        m = _POV_NUM_RE.match(p.stem)
        if not m:
            continue
        out.append((int(m.group(1)), p))
    return sorted(out, key=lambda t: t[0])


def _find_candidate_blobs(source_cpv_root: Path) -> list[Path]:
    """Find candidate POV blobs for a CPV across all trials/sanitizers.

    Walks ``<source_cpv_root>/*/*/trial-*/povs/cpvs/<cpv_id>/blobs/*.blob``.
    The caller passes the *harness* directory; this helper enumerates every
    ``trial-*`` POV under it for the given cpv.
    """
    if not source_cpv_root.is_dir():
        return []
    # Glob is intentionally permissive about the build-mode/sanitizer dirs
    # (e.g. full/address, full/undefined) so future variations still match.
    return sorted(source_cpv_root.glob(_BLOB_GLOB))


def _iter_trial_blobs(harness_root: Path, cpv_id: str) -> list[Path]:
    """Iterate all candidate blobs for a (harness, cpv) under bug-finding root.

    Returns deterministic order: by trial dir name, then blob filename.
    """
    if not harness_root.is_dir():
        return []
    blobs: list[Path] = []
    # build_mode/sanitizer are usually full/<sanitizer> but we glob to be
    # robust to additions like delta/<sanitizer>.
    for blobs_dir in sorted(harness_root.glob(f"*/*/trial-*/povs/cpvs/{cpv_id}/blobs")):
        blobs.extend(sorted(blobs_dir.glob(_BLOB_GLOB)))
    return blobs


def _find_crash_log(blob_path: Path, cpv_id: str) -> Optional[Path]:
    """Locate the CPV-attributed crash log for a candidate blob.

    Bug-finding CRS runs each POV against several build variants and emits
    one log per variant in ``../crash_logs/``:

    - ``*-cpvN.log`` — **drop-one build**: only the patch for cpv_N is
      removed, every other cpv is patched. If this log shows a crash, the
      crash is unambiguously caused by cpv_N (no other cpv can contribute).
      For a POV stored under ``cpvs/cpv_N/``, this is the canonical crash
      evidence.
    - ``*-deltaref.log`` / ``*-fullbase.log`` — fully-unpatched vulnerable
      build. Any crash here is correct but, in multi-cpv benchmarks,
      whichever bug fires first determines the stack, so the log may
      describe a different cpv than the one we labeled this POV with.
    - ``*-allpatched.log`` — every patch applied; typically no crash.

    Selection order for a POV under ``cpvs/cpv_N/``:
      1. ``*-cpvN.log`` (cpv-attributed crash; preferred)
      2. ``*-deltaref.log`` / ``*-fullbase.log`` (fallback)
      3. otherwise ``None`` and the caller skips log copying.

    ``cpv_id`` uses underscore form (e.g., ``cpv_0``) while filenames use
    ``cpv0`` (no underscore), so we normalize when matching.
    """
    crash_logs_dir = blob_path.parent.parent / "crash_logs"
    if not crash_logs_dir.is_dir():
        return None
    stem = blob_path.stem

    # 1. Drop-one log for this cpv (preferred)
    cpv_tag = cpv_id.replace("_", "")  # "cpv_0" -> "cpv0"
    matches = sorted(crash_logs_dir.glob(f"{stem}-*-{cpv_tag}.log"))
    if matches:
        return matches[0]

    # 2. Fully-unpatched build (fallback)
    for suffix in ("deltaref", "fullbase"):
        matches = sorted(crash_logs_dir.glob(f"{stem}-*-{suffix}.log"))
        if matches:
            return matches[0]
    return None


def import_cpv_povs(
    benchmark_path: Path,
    source_root: Path,
    harness: str,
    cpv_id: str,
    *,
    max_new_povs: Optional[int] = None,
    copy_logs: bool = True,
    dry_run: bool = False,
) -> CpvImportResult:
    """Import new POV variants for one (harness, cpv) into the benchmark.

    Args:
        benchmark_path: Path to ``benchmarks/<benchmark>``.
        source_root: Path to ``<bug_finding_root>/<benchmark>``.
        harness: Harness name (e.g. ``curl_fuzzer_http``).
        cpv_id: CPV identifier (e.g. ``cpv_0``).
        max_new_povs: Maximum number of NEW POVs to add. ``None`` means
            unlimited.
        copy_logs: Whether to copy a matching crash log alongside as
            ``logs/pov_N.log``.
        dry_run: If True, do not write files; only report.

    Returns:
        CpvImportResult describing what was (or would be) imported.
    """
    target_blobs = benchmark_path / ".aixcc" / harness / cpv_id / "blobs"
    target_logs = benchmark_path / ".aixcc" / harness / cpv_id / "logs"

    result = CpvImportResult(harness=harness, cpv=cpv_id)

    existing = _existing_pov_files(target_blobs)
    result.existing_povs = len(existing)
    existing_hashes = {_sha256_file(p) for _, p in existing}
    next_pov_num = max((n for n, _ in existing), default=-1) + 1

    harness_root = source_root / harness
    candidates = _iter_trial_blobs(harness_root, cpv_id)
    result.candidates_found = len(candidates)

    if not candidates:
        return result

    seen_in_run: set[str] = set()

    for blob in candidates:
        if max_new_povs is not None and len(result.imported_povs) >= max_new_povs:
            result.skipped_capacity += 1
            continue

        try:
            content_hash = _sha256_file(blob)
        except OSError as exc:
            logger.warning(f"  skip unreadable blob {blob}: {exc}")
            continue

        if content_hash in existing_hashes or content_hash in seen_in_run:
            result.skipped_duplicate += 1
            continue
        seen_in_run.add(content_hash)

        pov_num = next_pov_num
        next_pov_num += 1

        result.imported_povs.append(pov_num)
        result.sources[pov_num] = str(blob)

        if dry_run:
            continue

        target_blobs.mkdir(parents=True, exist_ok=True)
        target_blob = target_blobs / f"pov_{pov_num}.blob"
        shutil.copy2(blob, target_blob)

        if copy_logs:
            log = _find_crash_log(blob, cpv_id)
            if log is not None:
                target_logs.mkdir(parents=True, exist_ok=True)
                shutil.copy2(log, target_logs / f"pov_{pov_num}.log")

    return result


def _discover_benchmark_cpvs(benchmark_path: Path) -> list[tuple[str, str]]:
    """List (harness, cpv_id) pairs that exist in a benchmark's .aixcc tree."""
    aixcc = benchmark_path / ".aixcc"
    if not aixcc.is_dir():
        return []
    out: list[tuple[str, str]] = []
    for harness_dir in sorted(aixcc.iterdir()):
        if not harness_dir.is_dir() or harness_dir.name.startswith("."):
            continue
        for cpv_dir in sorted(harness_dir.iterdir()):
            if not cpv_dir.is_dir() or not cpv_dir.name.startswith("cpv_"):
                continue
            out.append((harness_dir.name, cpv_dir.name))
    return out


def import_benchmark_povs(
    benchmark_path: Path,
    source_root: Path,
    *,
    max_new_povs_per_cpv: Optional[int] = None,
    harness_filter: Optional[str] = None,
    cpv_filter: Optional[str] = None,
    copy_logs: bool = True,
    dry_run: bool = False,
) -> BenchmarkImportSummary:
    """Import POVs for every (harness, cpv) in a benchmark.

    Args:
        benchmark_path: Path to ``benchmarks/<benchmark>``.
        source_root: Path to ``<bug_finding_root>/<benchmark>`` (the per-
            benchmark sub-directory; not the multi-benchmark root).
        max_new_povs_per_cpv: Cap on number of NEW POVs to add per CPV.
        harness_filter: If set, only this harness is processed.
        cpv_filter: If set, only this CPV is processed.
        copy_logs: Whether to copy matching crash logs as pov_N.log.
        dry_run: If True, only report; nothing is written.

    Returns:
        BenchmarkImportSummary aggregating per-CPV results.
    """
    summary = BenchmarkImportSummary(benchmark=benchmark_path.name)

    if not (benchmark_path / ".aixcc").is_dir():
        raise ValueError(f"Not a benchmark (missing .aixcc): {benchmark_path}")

    if not source_root.is_dir():
        logger.warning(
            f"[{benchmark_path.name}] source not found: {source_root} — skipping"
        )
        return summary

    pairs = _discover_benchmark_cpvs(benchmark_path)
    for harness, cpv_id in pairs:
        if harness_filter and harness != harness_filter:
            continue
        if cpv_filter and cpv_id != cpv_filter:
            continue
        result = import_cpv_povs(
            benchmark_path,
            source_root,
            harness,
            cpv_id,
            max_new_povs=max_new_povs_per_cpv,
            copy_logs=copy_logs,
            dry_run=dry_run,
        )
        summary.cpv_results.append(result)
        verb = "would add" if dry_run else "added"
        logger.info(
            f"[{benchmark_path.name}] {harness}/{cpv_id}: "
            f"existing={result.existing_povs} "
            f"candidates={result.candidates_found} "
            f"{verb}={len(result.imported_povs)} "
            f"dup={result.skipped_duplicate} "
            f"cap-skipped={result.skipped_capacity}"
        )
    return summary


def import_many(
    benchmark_paths: list[Path],
    source_root: Path,
    *,
    max_new_povs_per_cpv: Optional[int] = None,
    harness_filter: Optional[str] = None,
    cpv_filter: Optional[str] = None,
    copy_logs: bool = True,
    dry_run: bool = False,
) -> list[BenchmarkImportSummary]:
    """Run ``import_benchmark_povs`` for each benchmark.

    ``source_root`` is the multi-benchmark root (e.g.
    ``/data/.../atlantis-multilang-given_fuzzer``); each benchmark is
    expected at ``<source_root>/<benchmark_name>``.
    """
    summaries: list[BenchmarkImportSummary] = []
    for bp in benchmark_paths:
        per_bench_source = source_root / bp.name
        try:
            summaries.append(
                import_benchmark_povs(
                    bp,
                    per_bench_source,
                    max_new_povs_per_cpv=max_new_povs_per_cpv,
                    harness_filter=harness_filter,
                    cpv_filter=cpv_filter,
                    copy_logs=copy_logs,
                    dry_run=dry_run,
                )
            )
        except ValueError as exc:
            logger.error(f"[{bp.name}] {exc}")
    return summaries


def summaries_to_json(summaries: list[BenchmarkImportSummary]) -> str:
    return json.dumps([s.to_dict() for s in summaries], indent=2)
