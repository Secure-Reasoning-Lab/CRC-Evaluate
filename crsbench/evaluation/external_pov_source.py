"""Load POVs from a prior bug-finding experiment store as bug-fixing inputs.

Given an experiment-data directory produced by a bug-finding run, enumerate
per-CPV POVs discovered across all trials, dedup by crash signature (same
algorithm as ``crsbench benchmark dedup-povs``), and return blob paths ordered
by discovery time so a bug-fixing trial can stage them instead of reading
benchmark ground-truth blobs.

Directory layout expected (produced by ``POVStore``, matches
``pov_store_dir = trial_dir / "povs"`` in
``crsbench/evaluation/verification/pov/manager.py``):

    <experiment_path>/<benchmark>/<harness>/<mode>/<sanitizer>/trial-*/
        povs/
            pov_store.json
            cpvs/<cpv_id>/blobs/<hash>.blob
            cpvs/<cpv_id>/crash_logs/<hash>[-variant].log
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from crsbench.evaluation.verification.crash_signature import parse_crash_signature
from crsbench.evaluation.verification.models import PovVerificationStatus
from crsbench.evaluation.verification.pov.models import POVEntry
from crsbench.utils.logger import get_logger

logger = get_logger(__name__)

DEFAULT_DEDUP_TOP_N = 5


@dataclass(frozen=True)
class POVRecord:
    """One POV discovered by a prior bug-finding trial."""

    blob_path: Path
    content_hash: str
    crash_signature: Optional[str]
    cpv_id: str
    discovery_ts: float
    source_trial_dir: Path


class ExternalPovSource:
    """Read POVs from a single trial of a prior bug-finding experiment.

    Each ``ExternalPovSource`` is bound to exactly one finding ``trial_num``:
    ``list_found_cpvs`` and ``get_pov_blobs`` only see POVs that finding
    ``trial-<trial_num>`` produced. This enforces 1:1 trial pairing so a
    fixing trial-N replays only what finding trial-N discovered, with no
    cross-trial pooling or merging.
    """

    def __init__(
        self,
        experiment_path: Path,
        *,
        trial_num: int,
        dedup_top_n: int = DEFAULT_DEDUP_TOP_N,
    ):
        self.experiment_path = Path(experiment_path)
        self.trial_num = trial_num
        self.dedup_top_n = dedup_top_n

        if not self.experiment_path.is_dir():
            raise ValueError(
                f"inputs.pov.from_experiment path does not exist or is not a "
                f"directory: {self.experiment_path}"
            )
        if trial_num < 1:
            raise ValueError(f"trial_num must be >= 1, got {trial_num}")

    def list_found_cpvs(self, benchmark: str, harness: str, sanitizer: str) -> set[str]:
        """Return CPVs discovered by finding trial-``trial_num`` only."""
        found: set[str] = set()
        for store_dir in self._iter_pov_stores(benchmark, harness, sanitizer):
            data = _read_pov_store_json(store_dir)
            if data is None:
                continue
            found.update(data.get("cpv_to_first_pov", {}).keys())
        return found

    def get_pov_blobs(
        self,
        benchmark: str,
        harness: str,
        sanitizer: str,
        cpv_id: str,
    ) -> list[POVRecord]:
        """Return deduplicated POV records discovered by finding
        trial-``trial_num`` for the given CPV, earliest first.

        Dedup is applied within the single trial only (no cross-trial
        merging):
        - Prefer ``POVEntry.crash_signature`` recorded by bug-finding.
        - Fall back to parsing the POV's crash log with ``parse_crash_signature``.
        - POVs with no parseable signature are conservatively kept.
        - Within the deduplicated set, drop exact content-hash duplicates.
        """
        candidates: list[POVRecord] = []
        for store_dir in self._iter_pov_stores(benchmark, harness, sanitizer):
            data = _read_pov_store_json(store_dir)
            if data is None:
                continue
            for pov_hash, entry_dict in (data.get("povs") or {}).items():
                try:
                    entry = POVEntry(**entry_dict)
                except Exception as exc:
                    logger.warning(f"Skipping malformed POVEntry in {store_dir}: {exc}")
                    continue
                if entry.status != PovVerificationStatus.CPV:
                    continue
                matched = entry.cpv_matched or []
                if cpv_id not in matched:
                    continue

                # POVStore stores blobs under the *first* matched CPV only
                # (see POVStore._get_category_dir). When one POV matches
                # multiple CPVs, blobs for non-primary CPVs live under the
                # primary CPV's directory. Resolve via matched[0].
                primary_cpv = matched[0]
                blob_path = (
                    store_dir / "cpvs" / primary_cpv / "blobs" / f"{pov_hash}.blob"
                )
                if not blob_path.is_file():
                    logger.warning(
                        f"POV blob missing for {cpv_id}/{pov_hash} "
                        f"(primary cpv={primary_cpv}) in {store_dir}"
                    )
                    continue

                signature = entry.crash_signature or self._resolve_crash_signature(
                    store_dir, entry, primary_cpv
                )
                discovery_ts = (
                    entry.file_mtime
                    if entry.file_mtime is not None
                    else entry.first_seen_ts
                )
                candidates.append(
                    POVRecord(
                        blob_path=blob_path,
                        content_hash=pov_hash,
                        crash_signature=signature,
                        cpv_id=cpv_id,
                        discovery_ts=discovery_ts,
                        source_trial_dir=_trial_dir_of(store_dir),
                    )
                )

        return _dedup_records(candidates)

    def _iter_pov_stores(
        self, benchmark: str, harness: str, sanitizer: str
    ) -> list[Path]:
        """Yield only ``trial-<trial_num>/povs`` across all modes.

        A finding run usually has one mode per (benchmark, harness, sanitizer),
        so this yields at most one store. Multi-mode finding runs (rare) get
        each mode's trial-N treated equally — they share the same trial_num
        identity by construction.
        """
        base = self.experiment_path / benchmark / harness
        if not base.is_dir():
            return []
        target = f"trial-{self.trial_num}"
        stores: list[Path] = []
        for mode_dir in sorted(p for p in base.iterdir() if p.is_dir()):
            sanitizer_dir = mode_dir / sanitizer
            if not sanitizer_dir.is_dir():
                continue
            trial_dir = sanitizer_dir / target
            if not trial_dir.is_dir():
                continue
            store_dir = trial_dir / "povs"
            if store_dir.is_dir():
                stores.append(store_dir)
        return stores

    def _resolve_crash_signature(
        self, store_dir: Path, entry: POVEntry, primary_cpv: str
    ) -> Optional[str]:
        """Fall back to parsing the POV's crash log when entry has no signature.

        ``primary_cpv`` is ``entry.cpv_matched[0]`` — POVStore stores crash
        logs alongside blobs under the first matched CPV only.
        """
        log_path: Optional[Path] = None
        if entry.crash_log_path:
            candidate = store_dir / entry.crash_log_path
            if candidate.is_file():
                log_path = candidate
        if log_path is None:
            crash_logs_dir = store_dir / "cpvs" / primary_cpv / "crash_logs"
            if crash_logs_dir.is_dir():
                matches = sorted(crash_logs_dir.glob(f"{entry.hash}*.log"))
                if matches:
                    log_path = matches[0]
        if log_path is None:
            return None
        try:
            text = log_path.read_text(errors="replace")
        except OSError as exc:
            logger.warning(f"Could not read crash log {log_path}: {exc}")
            return None
        sig = parse_crash_signature(text, top_n=self.dedup_top_n)
        return sig.signature_hash if sig else None


def _read_pov_store_json(store_dir: Path) -> Optional[dict]:
    path = store_dir / "pov_store.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(f"Could not read {path}: {exc}")
        return None


def _trial_dir_of(store_dir: Path) -> Path:
    """Given ``.../trial-N/povs``, return the ``trial-N`` directory."""
    return store_dir.parent


def _dedup_records(records: list[POVRecord]) -> list[POVRecord]:
    """Dedup by crash signature (earliest wins), then by content hash."""
    records_sorted = sorted(records, key=lambda r: (r.discovery_ts, r.content_hash))

    kept: list[POVRecord] = []
    seen_signatures: set[str] = set()
    for rec in records_sorted:
        if rec.crash_signature is None:
            kept.append(rec)
            continue
        if rec.crash_signature in seen_signatures:
            continue
        seen_signatures.add(rec.crash_signature)
        kept.append(rec)

    # Drop any remaining exact content-hash duplicates, preserving order.
    seen_hashes: set[str] = set()
    deduped: list[POVRecord] = []
    for rec in kept:
        if rec.content_hash in seen_hashes:
            continue
        seen_hashes.add(rec.content_hash)
        deduped.append(rec)
    return deduped
