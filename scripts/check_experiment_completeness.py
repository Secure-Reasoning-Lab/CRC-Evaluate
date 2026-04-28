#!/usr/bin/env python3
"""Audit a CRS experiment tree for trial completeness and LLM-usage presence.

Usage:
    scripts/check_experiment_completeness.py <CRS_DIR> [--trials N] [--quiet]

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


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("crs_dir", type=Path, help="Directory immediately above per-benchmark trees")
    p.add_argument("--trials", type=int, default=3, help="Expected trials per leaf (default: 3)")
    p.add_argument("--quiet", action="store_true", help="Only print summary, not per-leaf issues")
    return p.parse_args()


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


def main() -> int:
    args = parse_args()
    crs_dir: Path = args.crs_dir.resolve()
    if not crs_dir.is_dir():
        sys.exit(f"error: not a directory: {crs_dir}")

    expected = args.trials

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
        if len(trials) != expected:
            bad_trial_count += 1
            issues.append(f"[trials={len(trials)} expected={expected}] {rel}")
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

    return 0 if not (bad_trial_count or bad_success or bad_llm) else 1


if __name__ == "__main__":
    sys.exit(main())
