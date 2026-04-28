from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from crsbench.evaluation.verification.crash_signature import parse_crash_signature


def _entry_identity(entry: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(entry.get("source_id", "")),
        str(entry.get("trial_relative_path", "")),
        str(entry.get("original_pov_relpath", "")),
    )


def _extract_summary_line(log_text: str) -> str:
    for line in log_text.splitlines():
        if line.strip().startswith("SUMMARY:"):
            return line.strip()
    return ""


def _build_signature_metadata(sanitizer_log: str | None) -> dict[str, str]:
    log_text = ""
    if sanitizer_log:
        path = Path(sanitizer_log)
        if path.exists():
            log_text = path.read_text(encoding="utf-8", errors="replace")

    parsed = parse_crash_signature(log_text, top_n=5) if log_text else None
    if parsed is not None:
        return {
            "crash_type": parsed.crash_type,
            "signature_hash": parsed.signature_hash,
            "raw_summary": parsed.raw_summary,
            "signature_source": "parsed",
        }

    fallback_payload = log_text if log_text else (sanitizer_log or "")
    return {
        "crash_type": "unparsed",
        "signature_hash": hashlib.sha256(fallback_payload.encode("utf-8")).hexdigest()[
            :16
        ],
        "raw_summary": _extract_summary_line(log_text),
        "signature_source": "raw_log_hash",
    }


def build_deduplicated_zero_day_entries(
    zero_day_entries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[
        tuple[str, str | None, str | None, str, str],
        dict[str, Any],
    ] = {}

    for raw_entry in sorted(zero_day_entries, key=_entry_identity):
        entry_base = {
            key: value for key, value in raw_entry.items() if key != "replays"
        }
        for replay in raw_entry.get("replays", []):
            signature = _build_signature_metadata(replay.get("sanitizer_log"))
            group_key = (
                str(raw_entry.get("benchmark", "")),
                raw_entry.get("mapped_oss_fuzz_project"),
                replay.get("sanitizer"),
                signature["crash_type"],
                signature["signature_hash"],
            )
            group = grouped.setdefault(
                group_key,
                {
                    "benchmark": raw_entry.get("benchmark"),
                    "mapped_oss_fuzz_project": raw_entry.get("mapped_oss_fuzz_project"),
                    "sanitizer": replay.get("sanitizer"),
                    **signature,
                    "_entries": {},
                },
            )
            source_key = _entry_identity(raw_entry)
            grouped_entry = group["_entries"].setdefault(
                source_key,
                {
                    **entry_base,
                    "replays": [],
                },
            )
            grouped_entry["replays"].append(dict(replay))

    output: list[dict[str, Any]] = []
    for group_key in sorted(grouped):
        group = grouped[group_key]
        entries = [
            group["_entries"][source_key] for source_key in sorted(group["_entries"])
        ]
        for entry in entries:
            entry["replays"] = sorted(
                entry["replays"],
                key=lambda replay: (
                    replay.get("target_harness") or "",
                    replay.get("sanitizer_log") or "",
                ),
            )
        output.append(
            {
                "benchmark": group["benchmark"],
                "mapped_oss_fuzz_project": group["mapped_oss_fuzz_project"],
                "sanitizer": group["sanitizer"],
                "crash_type": group["crash_type"],
                "signature_hash": group["signature_hash"],
                "raw_summary": group["raw_summary"],
                "signature_source": group["signature_source"],
                "source_entry_count": len(entries),
                "replay_count": sum(len(entry["replays"]) for entry in entries),
                "entries": entries,
            }
        )
    return output
