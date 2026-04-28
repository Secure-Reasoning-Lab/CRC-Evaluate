"""Build a minimal file manifest for the ``inputs.pov.from_experiment`` bundle.

When running a chained bug-finding -> bug-fixing experiment on cloud VMs, we
need to ship the finding-phase POV corpus to every VM that will read it. The
reader is :class:`crsbench.evaluation.external_pov_source.ExternalPovSource`,
which only reads a narrow subset of files under the experiment tree:

    <root>/<benchmark>/<harness>/<mode>/<sanitizer>/trial-*/povs/
        pov_store.json
        cpvs/<primary_cpv>/blobs/<pov_hash>.blob
        cpvs/<primary_cpv>/crash_logs/<pov_hash>*.log

Rather than rsync the entire experiment tree to each VM, we walk the local
tree, parse each ``pov_store.json``, and emit only the files that will
actually be consumed. The returned list is suitable for ``rsync --files-from``
and preserves relative layout on the remote side.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from crsbench.utils.logger import get_logger

if TYPE_CHECKING:
    from collections.abc import Iterable

logger = get_logger(__name__)

_POV_STATUS_CPV = "cpv"


def build_from_experiment_manifest(
    local_root: Path,
    *,
    allowed_benchmarks: Iterable[str] | None = None,
) -> list[str]:
    """Return POSIX relative paths of files ExternalPovSource would read.

    Walks the ``trial-*/povs`` subtree under ``local_root`` and, for each
    ``pov_store.json``, collects the blob for every ``status=cpv`` entry plus
    the matching crash log when ``crash_signature`` is missing (the reader
    falls back to parsing the log in that case).

    When ``allowed_benchmarks`` is provided, the walk is restricted to the
    listed top-level benchmark directories under ``local_root``. Phase-1
    sources may contain CPVs for benchmarks that are not in the current
    run's suite; filtering avoids stat-walking and shipping POVs and crash
    logs that ``ExternalPovSource`` would never consume. Benchmarks listed
    but absent on disk are skipped silently — phase-1 simply produced no
    findings for them.

    Parsing errors (missing or malformed ``pov_store.json``) are logged and
    the offending subtree is skipped; we never abort on partial corruption.
    """
    root = Path(local_root)
    if not root.is_dir():
        raise ValueError(f"from_experiment local path is not a directory: {root}")

    manifest: list[str] = []
    for store_path in _iter_pov_stores(root, allowed_benchmarks):
        if not store_path.is_file():
            continue

        rel_store = store_path.relative_to(root).as_posix()
        data = _load_pov_store(store_path)
        if data is None:
            continue

        manifest.append(rel_store)

        store_dir = store_path.parent
        for pov_hash, entry in (data.get("povs") or {}).items():
            if not isinstance(entry, dict):
                continue
            if _entry_status(entry) != _POV_STATUS_CPV:
                continue

            matched = entry.get("cpv_matched") or []
            if not matched:
                continue
            primary_cpv = matched[0]

            blob_path = store_dir / "cpvs" / primary_cpv / "blobs" / f"{pov_hash}.blob"
            if blob_path.is_file():
                manifest.append(blob_path.relative_to(root).as_posix())
            else:
                logger.warning(
                    "from_experiment: blob missing for {}/{} under {}",
                    primary_cpv,
                    pov_hash,
                    store_dir,
                )

            if entry.get("crash_signature"):
                continue

            for log_rel in _iter_crash_log_relpaths(
                store_dir, entry, primary_cpv, pov_hash, root
            ):
                manifest.append(log_rel)

    # Preserve deterministic order and drop any duplicates introduced by the
    # crash-log fallback glob.
    seen: set[str] = set()
    deduped: list[str] = []
    for rel in manifest:
        if rel in seen:
            continue
        seen.add(rel)
        deduped.append(rel)
    return deduped


def _iter_pov_stores(
    root: Path,
    allowed_benchmarks: Iterable[str] | None,
) -> list[Path]:
    """Yield ``trial-*/povs/pov_store.json`` paths, optionally suite-filtered.

    Without ``allowed_benchmarks``, walks the whole tree. With one, only the
    listed top-level benchmark subdirectories are walked — every other
    sibling under ``root`` is skipped entirely (no stat, no rglob).
    """
    if allowed_benchmarks is None:
        return sorted(root.rglob("trial-*/povs/pov_store.json"))

    benchmark_names = sorted({name for name in allowed_benchmarks if name})
    if not benchmark_names:
        return []

    stores: list[Path] = []
    for name in benchmark_names:
        bench_dir = root / name
        if not bench_dir.is_dir():
            continue
        stores.extend(sorted(bench_dir.rglob("trial-*/povs/pov_store.json")))
    return stores


def _load_pov_store(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("from_experiment: could not read {}: {}", path, exc)
        return None


def _entry_status(entry: dict) -> str | None:
    """Extract ``status`` as a lowercase string, tolerant of enum variants."""
    status = entry.get("status")
    if status is None:
        return None
    if isinstance(status, str):
        return status.lower()
    value = getattr(status, "value", None)
    if isinstance(value, str):
        return value.lower()
    return None


def _iter_crash_log_relpaths(
    store_dir: Path,
    entry: dict,
    primary_cpv: str,
    pov_hash: str,
    root: Path,
) -> list[str]:
    """Resolve crash-log paths for an entry missing ``crash_signature``.

    Mirrors :meth:`ExternalPovSource._resolve_crash_signature`: prefer the
    explicit ``crash_log_path`` on the entry; fall back to globbing
    ``cpvs/<primary>/crash_logs/<pov_hash>*.log``.
    """
    results: list[str] = []
    explicit = entry.get("crash_log_path")
    if explicit:
        candidate = store_dir / explicit
        if candidate.is_file():
            results.append(candidate.relative_to(root).as_posix())
            return results

    crash_logs_dir = store_dir / "cpvs" / primary_cpv / "crash_logs"
    if crash_logs_dir.is_dir():
        for log_path in sorted(crash_logs_dir.glob(f"{pov_hash}*.log")):
            if log_path.is_file():
                results.append(log_path.relative_to(root).as_posix())
    return results
