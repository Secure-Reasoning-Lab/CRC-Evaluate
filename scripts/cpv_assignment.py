#!/usr/bin/env python3
"""CPV extraction and assignee distribution tool for manual validation."""

import argparse
import sys
from collections import defaultdict
from pathlib import Path

import yaml


def get_source_from_benchmark(benchmark_name: str) -> str:
    """Extract source from benchmark name prefix."""
    if benchmark_name.startswith("afc-"):
        return "afc"
    elif benchmark_name.startswith("atlanta-"):
        return "atlanta"
    elif benchmark_name.startswith("asc-"):
        return "asc"
    elif benchmark_name.startswith("sanity-"):
        return "sanity"
    return "unknown"


def get_project_group(benchmark_name: str) -> str:
    """
    Extract project group from benchmark name by removing trailing number.

    Examples:
        afc-curl-delta-01 -> afc-curl-delta
        afc-curl-delta-02 -> afc-curl-delta
        afc-curl-full-01 -> afc-curl-full
        atlanta-activemq-delta-01 -> atlanta-activemq-delta
    """
    import re

    # Remove trailing -NN pattern (e.g., -01, -02, -10)
    return re.sub(r"-\d+$", "", benchmark_name)


def normalize_language(lang: str) -> str:
    """Normalize language names (jvm -> java, etc.)."""
    lang = lang.lower()
    if lang in ("jvm", "java"):
        return "java"
    if lang in ("c", "c++", "cpp"):
        return "c"
    return lang


def extract_cpvs(
    benchmarks_dir: Path,
    languages: list[str] | None = None,
    sources: list[str] | None = None,
) -> list[dict]:
    """
    Extract all CPVs from benchmarks directory.

    Args:
        benchmarks_dir: Path to benchmarks directory
        languages: Filter by languages (e.g., ['c', 'java'])
        sources: Filter by sources (e.g., ['afc', 'atlanta', 'asc'])

    Returns:
        List of dicts with benchmark_name, harness_name, vuln_id, pov_id
    """
    cpvs = []

    # Normalize filter values
    if languages:
        languages = [normalize_language(l) for l in languages]
    if sources:
        sources = [s.lower() for s in sources]

    for project_yaml in sorted(benchmarks_dir.glob("*/project.yaml")):
        benchmark_dir = project_yaml.parent
        benchmark_name = benchmark_dir.name

        # Skip sanity benchmarks by default
        if benchmark_name.startswith("sanity-"):
            continue

        # Check source filter
        source = get_source_from_benchmark(benchmark_name)
        if sources and source not in sources:
            continue

        # Read project.yaml for language
        with open(project_yaml) as f:
            project_data = yaml.safe_load(f)

        language = normalize_language(project_data.get("language", "unknown"))

        # Check language filter
        if languages and language not in languages:
            continue

        # Read meta.yaml for CPVs
        meta_yaml = benchmark_dir / ".aixcc" / "meta.yaml"
        if not meta_yaml.exists():
            continue

        with open(meta_yaml) as f:
            meta_data = yaml.safe_load(f)

        harness_files = meta_data.get("harness_files", [])
        for harness in harness_files:
            harness_name = harness.get("name", "")
            vulns = harness.get("vulns", [])

            for vuln in vulns:
                vuln_keyword = vuln.get("vuln_keyword", "")
                povs = vuln.get("povs", [])

                for pov in povs:
                    pov_id = pov.get("id", "")
                    cpvs.append(
                        {
                            "benchmark": benchmark_name,
                            "harness": harness_name,
                            "vuln_id": vuln_keyword,
                            "pov_id": pov_id,
                            "language": language,
                            "source": source,
                        }
                    )

    return cpvs


def distribute_assignees(
    cpvs: list[dict],
    assignees: list[str],
) -> list[dict]:
    """
    Distribute CPVs to assignees evenly by project group.

    Projects with same base name (e.g., afc-curl-delta-01, afc-curl-delta-02)
    are assigned to the same person. Overall CPV count per person is balanced.

    Args:
        cpvs: List of CPV dicts
        assignees: List of assignee names

    Returns:
        CPVs with 'assignee' field added
    """
    if not assignees:
        return cpvs

    # Group CPVs by project group (e.g., afc-curl-delta)
    by_group: dict[str, list[dict]] = defaultdict(list)
    for cpv in cpvs:
        group = get_project_group(cpv["benchmark"])
        by_group[group].append(cpv)

    # Sort groups by CPV count (larger first for better distribution)
    sorted_groups = sorted(
        by_group.keys(), key=lambda g: len(by_group[g]), reverse=True
    )

    # Track CPV count per assignee
    assignee_cpv_count = {a: 0 for a in assignees}

    # Assign groups to the person with fewest CPVs
    group_assignments: dict[str, str] = {}
    for group in sorted_groups:
        # Find assignee with minimum CPVs
        min_assignee = min(assignees, key=lambda a: assignee_cpv_count[a])
        group_assignments[group] = min_assignee
        assignee_cpv_count[min_assignee] += len(by_group[group])

    # Apply assignments to CPVs
    result = []
    for cpv in cpvs:
        cpv_with_assignee = cpv.copy()
        group = get_project_group(cpv["benchmark"])
        cpv_with_assignee["assignee"] = group_assignments[group]
        result.append(cpv_with_assignee)

    return result


def print_cpv_table(cpvs: list[dict], show_header: bool = True) -> None:
    """Print CPVs in CSV format: benchmark, vuln_id, assignee."""
    if show_header:
        print("benchmark,vuln_id,assignee")

    for cpv in sorted(cpvs, key=lambda x: (x["benchmark"], x["vuln_id"])):
        assignee = cpv.get("assignee", "")
        print(f"{cpv['benchmark']},{cpv['vuln_id']},{assignee}")


def print_stats(cpvs: list[dict]) -> None:
    """Print statistics about CPV distribution."""
    print("\n=== Statistics ===", file=sys.stderr)
    print(f"Total CPVs: {len(cpvs)}", file=sys.stderr)

    # By language
    by_lang: dict[str, int] = defaultdict(int)
    for cpv in cpvs:
        by_lang[cpv["language"]] += 1
    print("\nBy language:", file=sys.stderr)
    for lang, count in sorted(by_lang.items()):
        print(f"  {lang}: {count}", file=sys.stderr)

    # By source
    by_source: dict[str, int] = defaultdict(int)
    for cpv in cpvs:
        by_source[cpv["source"]] += 1
    print("\nBy source:", file=sys.stderr)
    for source, count in sorted(by_source.items()):
        print(f"  {source}: {count}", file=sys.stderr)


def print_summary(cpvs: list[dict]) -> None:
    """Print summary of assignee assignments."""
    if not cpvs or "assignee" not in cpvs[0] or not cpvs[0]["assignee"]:
        return

    print("\n=== Assignment Summary ===", file=sys.stderr)

    # Collect data by assignee
    by_assignee: dict[str, dict] = defaultdict(
        lambda: {"cpv_count": 0, "benchmarks": set(), "groups": set()}
    )

    for cpv in cpvs:
        assignee = cpv["assignee"]
        by_assignee[assignee]["cpv_count"] += 1
        by_assignee[assignee]["benchmarks"].add(cpv["benchmark"])
        by_assignee[assignee]["groups"].add(get_project_group(cpv["benchmark"]))

    # Print summary for each assignee
    for assignee in sorted(by_assignee.keys()):
        data = by_assignee[assignee]
        print(f"\n[{assignee}]", file=sys.stderr)
        print(f"  Total CPVs: {data['cpv_count']}", file=sys.stderr)
        print(
            f"  Projects: {len(data['benchmarks'])} "
            f"({len(data['groups'])} groups)",
            file=sys.stderr,
        )
        print("  Project groups:", file=sys.stderr)
        for group in sorted(data["groups"]):
            # Count benchmarks in this group
            benchmarks_in_group = [
                b for b in data["benchmarks"] if get_project_group(b) == group
            ]
            print(f"    - {group} ({len(benchmarks_in_group)} benchmarks)", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract CPVs and distribute to assignees for manual validation"
    )
    parser.add_argument(
        "--benchmarks-dir",
        type=Path,
        default=Path(__file__).parent.parent / "benchmarks",
        help="Path to benchmarks directory",
    )
    parser.add_argument(
        "--language",
        "-l",
        action="append",
        dest="languages",
        help="Filter by language (c, java). Can be specified multiple times.",
    )
    parser.add_argument(
        "--source",
        "-s",
        action="append",
        dest="sources",
        help="Filter by source (afc, atlanta, asc). Can be specified multiple times.",
    )
    parser.add_argument(
        "--assignees",
        "-a",
        type=str,
        help="Comma-separated list of assignee names",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="Show statistics",
    )
    parser.add_argument(
        "--no-header",
        action="store_true",
        help="Don't print table header",
    )

    args = parser.parse_args()

    # Extract CPVs
    cpvs = extract_cpvs(
        args.benchmarks_dir,
        languages=args.languages,
        sources=args.sources,
    )

    if not cpvs:
        print("No CPVs found matching the filters.", file=sys.stderr)
        sys.exit(1)

    # Distribute to assignees if provided
    if args.assignees:
        assignees = [a.strip() for a in args.assignees.split(",")]
        cpvs = distribute_assignees(cpvs, assignees)

    # Print results
    print_cpv_table(cpvs, show_header=not args.no_header)

    if args.stats:
        print_stats(cpvs)

    # Print summary if assignees were provided
    if args.assignees:
        print_summary(cpvs)


if __name__ == "__main__":
    main()
