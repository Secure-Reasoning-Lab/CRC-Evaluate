#!/usr/bin/env python3
"""Audit a CRS experiment tree for trial completeness and LLM-usage presence.

Usage:
    scripts/check_experiment_completeness.py <CRS_DIR> [--trials N] [--quiet]
        [--trial-keys-output PATH]

Where ``CRS_DIR`` is the directory immediately above the per-benchmark trees.
For example::

    /data/crsbench/ccs-final/fixing_all_all-agents/crs-claude-code/

Layouts handled (auto-detected by locating ``trial-*`` directories):

- bug-finding:  ``<bench>/<harness>/<mode>/<sanitizer>/trial-N``
- bug-fixing:   ``<bench>/<harness>/<cpv_X>/<mode>/<sanitizer>/trial-N``
- any other depth where the trial parent is a (harness/sanitizer/...) leaf

For each leaf (the parent directory of the ``trial-*`` dirs) the script checks:

- exactly N trials exist (default 3)
- every trial directory contains a ``.success`` marker
- every trial directory contains ``llm-usage.json`` with ``total_cost_usd > 0``

Prints one line per problem leaf/trial and exits non-zero if any issue is found.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

from crsbench.distributed.queue import build_trial_key


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "crs_dir", type=Path, help="Directory immediately above per-benchmark trees"
    )
    p.add_argument(
        "--trials", type=int, default=3, help="Expected trials per leaf (default: 3)"
    )
    p.add_argument(
        "--quiet", action="store_true", help="Only print summary, not per-leaf issues"
    )
    p.add_argument(
        "--trial-keys-output",
        type=Path,
        help=(
            "Write newline-delimited canonical logical trial keys for bad/missing "
            "trials to PATH"
        ),
    )
    return p.parse_args(argv)


def llm_cost(trial_dir: Path) -> tuple[float | None, str]:
    """Return (cost_usd_or_None, reason_if_problem)."""
    path = trial_dir / "llm-usage.json"
    if not path.exists():
        return None, "llm-usage.json missing"
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"llm-usage.json unreadable ({exc})"
    cost = data.get("total_cost_usd")
    if not isinstance(cost, (int, float)):
        return None, "total_cost_usd missing or non-numeric"
    if cost <= 0:
        return float(cost), "total_cost_usd <= 0"
    return float(cost), ""


def _resolve_self_named_wrapped_root(root: Path) -> Path:
    """Follow repeated wrapper directories named the same as the current root."""
    resolved = root
    while True:
        try:
            visible_entries = [
                entry for entry in resolved.iterdir() if not entry.name.startswith(".")
            ]
        except OSError:
            return resolved
        if len(visible_entries) != 1:
            return resolved
        nested = visible_entries[0]
        if not nested.is_dir() or nested.name != resolved.name:
            return resolved
        resolved = nested


def _parse_trial_dir_components(
    trial_dir: Path, collected_root: Path
) -> tuple[str, str, str, str, str, int, str | None] | None:
    """Parse canonical trial-key components from a collected trial directory."""
    try:
        relative = trial_dir.relative_to(collected_root)
    except ValueError:
        return None

    parts = relative.parts
    if len(parts) == 6:
        crs, benchmark, harness, mode, sanitizer, trial_part = parts
        target_cpv_id: str | None = None
    elif len(parts) == 7:
        crs, benchmark, harness, target_cpv_id, mode, sanitizer, trial_part = parts
    else:
        return None

    if not trial_part.startswith("trial-"):
        return None

    try:
        trial_num = int(trial_part.removeprefix("trial-"))
    except ValueError:
        return None

    return crs, benchmark, harness, mode, sanitizer, trial_num, target_cpv_id


def _trial_key_from_trial_dir(trial_dir: Path, collected_root: Path) -> str | None:
    parsed = _parse_trial_dir_components(trial_dir, collected_root)
    if parsed is None:
        return None
    crs, benchmark, harness, mode, sanitizer, trial_num, target_cpv_id = parsed
    return build_trial_key(
        crs=crs,
        benchmark=benchmark,
        harness=harness,
        mode=mode,
        sanitizer=sanitizer,
        trial_num=trial_num,
        target_cpv_id=target_cpv_id,
    )


def _trial_key_from_reference_trial(
    trial_dir: Path, collected_root: Path, *, trial_num: int
) -> str | None:
    parsed = _parse_trial_dir_components(trial_dir, collected_root)
    if parsed is None:
        return None
    crs, benchmark, harness, mode, sanitizer, _, target_cpv_id = parsed
    return build_trial_key(
        crs=crs,
        benchmark=benchmark,
        harness=harness,
        mode=mode,
        sanitizer=sanitizer,
        trial_num=trial_num,
        target_cpv_id=target_cpv_id,
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    crs_dir: Path = args.crs_dir.resolve()
    if not crs_dir.is_dir():
        sys.exit(f"error: not a directory: {crs_dir}")

    expected = args.trials
    trial_keys_root = (
        _resolve_self_named_wrapped_root(crs_dir)
        if args.trial_keys_output is not None
        else None
    )
    problem_trial_keys: list[str] = []
    seen_problem_trial_keys: set[str] = set()

    def remember_problem_key(key: str | None) -> None:
        if key is None or key in seen_problem_trial_keys:
            return
        seen_problem_trial_keys.add(key)
        problem_trial_keys.append(key)

    # Group trial-* directories by their parent (= leaf).
    leaves: dict[Path, list[Path]] = defaultdict(list)
    for trial in crs_dir.rglob("trial-*"):
        if trial.is_dir() and trial.parent != crs_dir:
            leaves[trial.parent].append(trial)

    if not leaves:
        sys.exit(f"error: no trial-* directories found under {crs_dir}")

    benches: set[str] = set()
    n_trials = 0
    bad_trial_count = 0
    bad_success = 0
    bad_llm = 0
    issues: list[str] = []

    for leaf in sorted(leaves):
        rel = leaf.relative_to(crs_dir)
        benches.add(rel.parts[0])
        trials = sorted(leaves[leaf])
        present_trial_nums: set[int] = set()
        for t in trials:
            try:
                present_trial_nums.add(int(t.name.removeprefix("trial-")))
            except ValueError:
                continue
        if len(trials) != expected:
            bad_trial_count += 1
            issues.append(f"[trials={len(trials)} expected={expected}] {rel}")
            if trial_keys_root is not None and trials:
                reference_trial = trials[0]
                for missing_trial_num in range(1, expected + 1):
                    if missing_trial_num not in present_trial_nums:
                        remember_problem_key(
                            _trial_key_from_reference_trial(
                                reference_trial,
                                trial_keys_root,
                                trial_num=missing_trial_num,
                            )
                        )
        for t in trials:
            n_trials += 1
            problems: list[str] = []
            if not (t / ".success").exists():
                bad_success += 1
                problems.append(".success missing")
            cost, reason = llm_cost(t)
            if reason:
                bad_llm += 1
                problems.append(reason if cost is None else f"{reason} (cost={cost})")
            if problems:
                issues.append(f"[{'; '.join(problems)}] {rel}/{t.name}")
                if trial_keys_root is not None:
                    remember_problem_key(_trial_key_from_trial_dir(t, trial_keys_root))

    if not args.quiet:
        for line in issues:
            print(line)
        if issues:
            print()

    print(f"crs_dir:        {crs_dir}")
    print(f"benchmarks:     {len(benches)}")
    print(f"leaves:         {len(leaves)}  (parents of trial-*)")
    print(f"trials seen:    {n_trials}  (expected per leaf: {expected})")
    print(f"leaves with trial count != {expected}: {bad_trial_count}")
    print(f"trials missing .success:               {bad_success}")
    print(f"trials missing/zero llm-usage:         {bad_llm}")

    if args.trial_keys_output is not None:
        output_text = "\n".join(sorted(problem_trial_keys))
        if output_text:
            output_text += "\n"
        try:
            args.trial_keys_output.parent.mkdir(parents=True, exist_ok=True)
            args.trial_keys_output.write_text(output_text, encoding="utf-8")
        except OSError as exc:
            print(
                f"error: failed to write trial keys to {args.trial_keys_output}: {exc}",
                file=sys.stderr,
            )
            return 2

    return 0 if not (bad_trial_count or bad_success or bad_llm) else 1


if __name__ == "__main__":
    sys.exit(main())
