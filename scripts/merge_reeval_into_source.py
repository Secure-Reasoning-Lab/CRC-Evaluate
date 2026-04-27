#!/usr/bin/env python3
"""Merge a reeval result tree into the original experiment tree.

The reeval tree mirrors the source experiment layout
(`<crs-config>/<cpv>/<harness>/<mode>/<sanitizer>/trial-N/...`) but only
contains files that reeval owns:

- per-trial `pov_verification_results.json`
- per-trial `povs/{cpvs,error,not_vulnerable,unintended,snapshots}/...`
- per-trial `povs/pov_store.json` with reeval-derived verdict fields and
  reeval-time timestamps that overwrite the original CRS run timeline if
  copied blindly

This script overlays the reeval tree onto the source tree without using
`--delete`, then performs a surgical merge of each `pov_store.json` so that:

- POVs present in both source and reeval keep the source's timing fields
  (`first_seen_ts`, `file_mtime`, `crash_signature`) but adopt the reeval
  verdict (`status`, `cpv_matched`, `verification_duration`,
  `crash_log_path`)
- POVs present only in source (skipped by reeval sampling, etc.) are kept
  exactly as they were; the original CRS verdict is trusted rather than
  downgraded
- POVs present only in reeval (the source's pov_store missed them) are
  added with reeval's record verbatim plus a `"source": "reeval_only"`
  marker; their timing fields are not corrected
- top-level `crs_run_start_time` and `cpv_to_first_pov` are preserved from
  the source because they describe original-run timing

Trials whose POV file_mtime distribution is inconsistent with a single CRS
run (typically because the source was retried/resumed and
`crs_run_start_time` was never updated) are merged unchanged but reported
as warnings; an anomaly list is appended to
`<SRC>/.reeval-merge-warnings.log`.

Usage:
    scripts/merge_reeval_into_source.py <SRC_TREE> <REEVAL_TREE> [--apply]

Without `--apply` the script previews changes (rsync `--dry-run` plus a
per-trial pov_store.json diff summary) and exits without mutating anything.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


TIMING_FIELDS = ("first_seen_ts", "file_mtime", "file_size", "crash_signature")
VERDICT_FIELDS = (
    "status",
    "cpv_matched",
    "verification_duration",
    "crash_log_path",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Merge a reeval result tree into the original experiment tree.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("src", type=Path, help="Original experiment tree root")
    parser.add_argument("reeval", type=Path, help="Reeval result tree root")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Execute the merge. Without this flag the script only previews changes.",
    )
    parser.add_argument(
        "--rsync-bin",
        default="rsync",
        help="rsync executable to use (default: rsync)",
    )
    return parser.parse_args()


def resolve_dir(path: Path, label: str) -> Path:
    if not path.exists():
        sys.exit(f"error: {label} does not exist: {path}")
    if not path.is_dir():
        sys.exit(f"error: {label} is not a directory: {path}")
    return path.resolve()


def run_rsync(rsync_bin: str, ree: Path, src: Path, *, dry_run: bool) -> None:
    cmd = [
        rsync_bin,
        "-a",
        "--human-readable",
        "--itemize-changes",
        "--exclude=pov_store.json",
    ]
    if dry_run:
        cmd.append("--dry-run")
    # Trailing slash on source so rsync copies contents, not the dir itself.
    cmd.append(f"{ree}/")
    cmd.append(f"{src}/")
    subprocess.run(cmd, check=True)


def find_pov_store_pairs(
    src_root: Path, ree_root: Path
) -> tuple[list[tuple[Path, Path, Path]], list[Path]]:
    """Return (pairs, missing) where pairs is list of (rel, src_ps, ree_ps)
    and missing lists relative paths that exist in REE but not SRC.
    """
    pairs: list[tuple[Path, Path, Path]] = []
    missing: list[Path] = []
    for ree_ps in sorted(ree_root.rglob("povs/pov_store.json")):
        rel = ree_ps.relative_to(ree_root)
        src_ps = src_root / rel
        if not src_ps.exists():
            missing.append(rel)
            continue
        pairs.append((rel, src_ps, ree_ps))
    return pairs, missing


# Trial timing-anomaly thresholds. A POV file_mtime falling outside
# [crs_run_start_time, crs_run_start_time + TIMING_MAX_HOURS*3600] indicates
# that the original CRS run was retried/resumed (or crs_run_start_time is
# stale). We do not attempt to correct timing; we only emit a warning.
TIMING_MAX_HOURS = 12.0


def merge_pov_store(src_data: dict, ree_data: dict) -> tuple[dict, dict]:
    """Merge SRC and REE pov_store.json.

    Policy:
    - POV in both SRC and REE: REE verdict (status, cpv_matched,
      verification_duration, crash_log_path) + SRC timing
      (first_seen_ts, file_mtime, file_size, crash_signature). For
      crash_signature the SRC value wins when present (REE often null).
    - POV in SRC only: kept verbatim (trust original CRS verdict).
    - POV in REE only: added with REE's record verbatim plus a
      ``"source": "reeval_only"`` marker. Timing fields are taken from REE
      as-is; we do not attempt to recover or recompute them.

    Top-level ``crs_run_start_time`` and ``cpv_to_first_pov`` always come
    from SRC.

    Returns (merged, stats).
    """
    src_povs: dict = src_data.get("povs") or {}
    ree_povs: dict = ree_data.get("povs") or {}

    merged_povs: dict = {}
    stats = {"reverified": 0, "src_only": 0, "ree_only_added": 0}

    for h, sv in src_povs.items():
        rv = ree_povs.get(h)
        if rv is None:
            merged_povs[h] = sv
            stats["src_only"] += 1
            continue

        merged: dict = {}
        merged.update(sv)
        for k in TIMING_FIELDS:
            if k in sv:
                merged[k] = sv[k]
        for k in VERDICT_FIELDS:
            if k in rv:
                merged[k] = rv[k]
        sv_sig = sv.get("crash_signature")
        if sv_sig is not None:
            merged["crash_signature"] = sv_sig
        rv_log = rv.get("crash_log_path")
        merged["crash_log_path"] = (
            rv_log if rv_log is not None else sv.get("crash_log_path")
        )
        merged_povs[h] = merged
        stats["reverified"] += 1

    for h, rv in ree_povs.items():
        if h in src_povs:
            continue
        # REE-only POV: add it verbatim with a provenance marker so
        # downstream consumers can tell these apart from CRS-recorded ones.
        added = dict(rv)
        added["source"] = "reeval_only"
        merged_povs[h] = added
        stats["ree_only_added"] += 1

    out = dict(src_data)
    out["povs"] = merged_povs
    if "crs_run_start_time" in src_data:
        out["crs_run_start_time"] = src_data["crs_run_start_time"]
    if "cpv_to_first_pov" in src_data:
        out["cpv_to_first_pov"] = src_data["cpv_to_first_pov"]
    return out, stats


def detect_timing_anomaly(merged: dict) -> str | None:
    """Return a short human-readable reason if the merged pov_store shows
    timing inconsistent with a single CRS run, otherwise None.

    Heuristics:
    - any POV file_mtime outside [crs_run_start_time,
      crs_run_start_time + TIMING_MAX_HOURS*3600]
    - crs_run_start_time missing while povs is non-empty (cannot evaluate
      relative time)
    """
    crs_start = merged.get("crs_run_start_time")
    povs = merged.get("povs") or {}
    if not povs:
        return None
    if crs_start is None:
        return "crs_run_start_time missing"
    cutoff_hi = crs_start + TIMING_MAX_HOURS * 3600
    n_late = 0
    n_early = 0
    max_h = 0.0
    min_h = 0.0
    for e in povs.values():
        fmt = e.get("file_mtime")
        if not isinstance(fmt, (int, float)):
            continue
        delta_h = (fmt - crs_start) / 3600.0
        if fmt > cutoff_hi:
            n_late += 1
            max_h = max(max_h, delta_h)
        elif fmt < crs_start:
            n_early += 1
            min_h = min(min_h, delta_h)
    if n_late or n_early:
        parts = []
        if n_late:
            parts.append(f"{n_late} POVs >{TIMING_MAX_HOURS:.0f}h after run start (max +{max_h:.1f}h)")
        if n_early:
            parts.append(f"{n_early} POVs before run start (min {min_h:.1f}h)")
        return "; ".join(parts) + " — likely retry/resume of original CRS run"
    return None


def load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json_atomic(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def backup_pov_store(path: Path) -> Path | None:
    """Create one timestamped backup of pov_store.json on first apply.

    If a `pov_store.json.pre-reeval-*` sibling already exists, assume the
    operator already has a pre-merge snapshot and skip making another one.
    """
    siblings = list(path.parent.glob(path.name + ".pre-reeval-*"))
    if siblings:
        return None
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_name(path.name + f".pre-reeval-{ts}")
    shutil.copy2(path, backup)
    return backup


def append_marker(src_root: Path, ree_root: Path) -> Path:
    marker = src_root / ".reeval-merges.log"
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    user = os.environ.get("USER", "unknown")
    try:
        host = socket.gethostname()
    except OSError:
        host = "unknown"
    line = f"{ts}\tfrom={ree_root}\tby={user}\thost={host}\n"
    with marker.open("a", encoding="utf-8") as f:
        f.write(line)
    return marker


def summarize_pairs(
    pairs: list[tuple[Path, Path, Path]], src_root: Path
) -> tuple[dict, list[tuple[Path, str]]]:
    """Preview merge stats and timing anomalies without mutating disk."""
    total = {"reverified": 0, "src_only": 0, "ree_only_added": 0, "trials": 0}
    anomalies: list[tuple[Path, str]] = []
    for _, src_ps, ree_ps in pairs:
        try:
            src_data = load_json(src_ps)
            ree_data = load_json(ree_ps)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"warning: skipping unreadable pov_store pair: {src_ps} ({exc})")
            continue
        merged, stats = merge_pov_store(src_data, ree_data)
        for k, v in stats.items():
            total[k] += v
        total["trials"] += 1
        reason = detect_timing_anomaly(merged)
        if reason:
            anomalies.append((src_ps.relative_to(src_root), reason))
    return total, anomalies


def main() -> int:
    args = parse_args()
    src = resolve_dir(args.src, "SRC_TREE")
    ree = resolve_dir(args.reeval, "REEVAL_TREE")
    if src == ree:
        sys.exit("error: SRC_TREE and REEVAL_TREE resolve to the same path")

    mode = "apply" if args.apply else "dry-run"
    print(f"[{mode}] reeval -> source merge")
    print(f"[{mode}]   source: {src}")
    print(f"[{mode}]   reeval: {ree}")
    print(f"[{mode}] pov_store.json excluded from rsync, no --delete")
    print()

    print(f"[{mode}] === step 1: rsync overlay (excluding pov_store.json) ===")
    # Flush so rsync's child stdout interleaves below our headers, not above.
    sys.stdout.flush()
    run_rsync(args.rsync_bin, ree, src, dry_run=not args.apply)
    print()

    pairs, missing = find_pov_store_pairs(src, ree)
    print(f"[{mode}] === step 2: pov_store.json surgical merge ===")
    print(
        f"[{mode}] {len(pairs)} trials with pov_store.json pairs, "
        f"{len(missing)} missing in source"
    )
    for rel in missing:
        print(f"[{mode}] missing in source, skipping: {rel}")

    if not args.apply:
        totals, anomalies = summarize_pairs(pairs, src)
        print()
        print(f"[dry-run] would merge {totals['trials']} pov_store.json files:")
        print(
            "[dry-run]   reverified entries (REE verdict + SRC timing): "
            f"{totals['reverified']}"
        )
        print(
            "[dry-run]   SRC-only entries kept verbatim:                "
            f"{totals['src_only']}"
        )
        print(
            "[dry-run]   REE-only entries added (verbatim REE record):  "
            f"{totals['ree_only_added']}"
        )
        if anomalies:
            print()
            print(f"[dry-run] ⚠ timing anomalies in {len(anomalies)} trials "
                  "(likely CRS retry/resume; merged but timing fields untouched):")
            for rel, reason in anomalies:
                print(f"[dry-run]   {rel}")
                print(f"[dry-run]     {reason}")
        print()
        print("[dry-run] no files changed. Re-run with --apply to execute.")
        return 0

    applied = 0
    backups = 0
    grand = {"reverified": 0, "src_only": 0, "ree_only_added": 0}
    anomalies: list[tuple[Path, str]] = []
    for _, src_ps, ree_ps in pairs:
        try:
            src_data = load_json(src_ps)
            ree_data = load_json(ree_ps)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"warning: skipping {src_ps}: {exc}")
            continue
        merged, stats = merge_pov_store(src_data, ree_data)
        if backup_pov_store(src_ps) is not None:
            backups += 1
        write_json_atomic(src_ps, merged)
        applied += 1
        for k in grand:
            grand[k] += stats[k]
        reason = detect_timing_anomaly(merged)
        if reason:
            anomalies.append((src_ps.relative_to(src), reason))

    marker = append_marker(src, ree)
    print()
    print(f"[apply] merged pov_store.json files: {applied}")
    print(f"[apply] backups created (.pre-reeval-*): {backups}")
    print(f"[apply]   reverified entries:           {grand['reverified']}")
    print(f"[apply]   SRC-only entries kept:        {grand['src_only']}")
    print(f"[apply]   REE-only entries added:       {grand['ree_only_added']}")
    if anomalies:
        warn_log = src / ".reeval-merge-warnings.log"
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with warn_log.open("a", encoding="utf-8") as f:
            f.write(f"# {ts} from={ree}\n")
            for rel, reason in anomalies:
                f.write(f"{rel}\t{reason}\n")
        print()
        print(f"[apply] ⚠ {len(anomalies)} trials with timing anomalies "
              "(likely CRS retry/resume):")
        for rel, reason in anomalies:
            print(f"[apply]   {rel}")
            print(f"[apply]     {reason}")
        print(f"[apply] anomaly list appended: {warn_log}")
    print(f"[apply] marker appended: {marker}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
