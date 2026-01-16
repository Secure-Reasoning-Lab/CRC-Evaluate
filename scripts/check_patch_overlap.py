#!/usr/bin/env python3
"""
Check for overlapping patch hunks across CPVs within the same project.

This script analyzes ground-truth patches in the benchmarks directory and
identifies when different CPVs (Challenge Problem Vulnerabilities) within
the same project have overlapping patch hunks.
"""

import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PatchHunk:
    """Represents a single hunk from a unified diff."""

    file_path: str
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    content: str

    def overlaps_with(self, other: "PatchHunk") -> bool:
        """Check if this hunk overlaps with another hunk in the same file."""
        if self.file_path != other.file_path:
            return False

        # Check line range overlap for old lines
        self_old_end = self.old_start + self.old_count
        other_old_end = other.old_start + other.old_count

        return not (self_old_end <= other.old_start or other_old_end <= self.old_start)


@dataclass
class PatchInfo:
    """Information about a patch file."""

    benchmark: str
    harness: str
    cpv: str
    patch_file: str
    hunks: list[PatchHunk]


def parse_unified_diff(diff_content: str) -> list[PatchHunk]:
    """Parse a unified diff and extract all hunks."""
    hunks = []
    current_file = None

    # Pattern for file header (--- a/path or +++ b/path)
    file_pattern = re.compile(r"^(?:---|\+\+\+) [ab]/(.+)$")
    # Pattern for hunk header (@@ -old_start,old_count +new_start,new_count @@)
    hunk_pattern = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

    lines = diff_content.split("\n")
    i = 0

    while i < len(lines):
        line = lines[i]

        # Check for file header
        file_match = file_pattern.match(line)
        if file_match and line.startswith("---"):
            current_file = file_match.group(1)
            i += 1
            continue

        # Check for hunk header
        hunk_match = hunk_pattern.match(line)
        if hunk_match and current_file:
            old_start = int(hunk_match.group(1))
            old_count = int(hunk_match.group(2)) if hunk_match.group(2) else 1
            new_start = int(hunk_match.group(3))
            new_count = int(hunk_match.group(4)) if hunk_match.group(4) else 1

            # Collect hunk content
            hunk_lines = [line]
            i += 1
            while i < len(lines):
                next_line = lines[i]
                if (
                    next_line.startswith("diff ")
                    or next_line.startswith("---")
                    or hunk_pattern.match(next_line)
                ):
                    break
                hunk_lines.append(next_line)
                i += 1

            hunk = PatchHunk(
                file_path=current_file,
                old_start=old_start,
                old_count=old_count,
                new_start=new_start,
                new_count=new_count,
                content="\n".join(hunk_lines),
            )
            hunks.append(hunk)
            continue

        i += 1

    return hunks


def extract_project_name(benchmark_name: str) -> str:
    """Extract the project name from a benchmark directory name.

    The benchmark name itself is the project (including the number suffix).

    Examples:
        afc-curl-delta-01 -> afc-curl-delta-01
        atlanta-apache-commons-compress-delta-01 -> atlanta-apache-commons-compress-delta-01
    """
    return benchmark_name


def collect_patches(benchmarks_dir: Path) -> list[PatchInfo]:
    """Collect all patch files from the benchmarks directory."""
    patches = []

    for benchmark_dir in sorted(benchmarks_dir.iterdir()):
        if not benchmark_dir.is_dir():
            continue

        aixcc_dir = benchmark_dir / ".aixcc"
        if not aixcc_dir.exists():
            continue

        for harness_dir in aixcc_dir.iterdir():
            if not harness_dir.is_dir() or harness_dir.name == "meta.yaml":
                continue

            for cpv_dir in harness_dir.iterdir():
                if not cpv_dir.is_dir() or not cpv_dir.name.startswith("cpv_"):
                    continue

                patches_dir = cpv_dir / "patches"
                if not patches_dir.exists():
                    continue

                for patch_file in patches_dir.glob("*.diff"):
                    try:
                        content = patch_file.read_text()
                        hunks = parse_unified_diff(content)

                        patches.append(
                            PatchInfo(
                                benchmark=benchmark_dir.name,
                                harness=harness_dir.name,
                                cpv=cpv_dir.name,
                                patch_file=str(patch_file),
                                hunks=hunks,
                            )
                        )
                    except Exception as e:
                        print(f"Warning: Failed to parse {patch_file}: {e}")

    return patches


@dataclass
class OverlapResult:
    """Result of comparing two patches."""

    patch1: PatchInfo
    patch2: PatchInfo
    is_identical: bool  # True if all hunks are identical
    overlapping_hunks: list[tuple[PatchHunk, PatchHunk]]


def patches_are_identical(patch1: PatchInfo, patch2: PatchInfo) -> bool:
    """Check if two patches have identical hunks (same file paths and content)."""
    if len(patch1.hunks) != len(patch2.hunks):
        return False

    # Sort hunks by file path and start line for consistent comparison
    sorted_hunks1 = sorted(patch1.hunks, key=lambda h: (h.file_path, h.old_start))
    sorted_hunks2 = sorted(patch2.hunks, key=lambda h: (h.file_path, h.old_start))

    for hunk1, hunk2 in zip(sorted_hunks1, sorted_hunks2):
        if hunk1.file_path != hunk2.file_path:
            return False
        if hunk1.content != hunk2.content:
            return False

    return True


def find_overlaps(
    patches: list[PatchInfo],
) -> dict[str, list[OverlapResult]]:
    """Find overlapping hunks between different CPVs within the same project."""
    # Group patches by project
    project_patches: dict[str, list[PatchInfo]] = defaultdict(list)
    for patch in patches:
        project = extract_project_name(patch.benchmark)
        project_patches[project].append(patch)

    overlaps: dict[str, list[OverlapResult]] = {}

    for project, project_patch_list in project_patches.items():
        project_overlaps = []

        # Compare all pairs of patches
        for i, patch1 in enumerate(project_patch_list):
            for patch2 in project_patch_list[i + 1 :]:
                # Skip if same benchmark and CPV
                if patch1.benchmark == patch2.benchmark and patch1.cpv == patch2.cpv:
                    continue

                # Check if patches are identical
                is_identical = patches_are_identical(patch1, patch2)

                if is_identical:
                    # For identical patches, pair up all hunks
                    sorted_hunks1 = sorted(
                        patch1.hunks, key=lambda h: (h.file_path, h.old_start)
                    )
                    sorted_hunks2 = sorted(
                        patch2.hunks, key=lambda h: (h.file_path, h.old_start)
                    )
                    overlapping_hunks = list(zip(sorted_hunks1, sorted_hunks2))
                    project_overlaps.append(
                        OverlapResult(
                            patch1=patch1,
                            patch2=patch2,
                            is_identical=True,
                            overlapping_hunks=overlapping_hunks,
                        )
                    )
                else:
                    # Find overlapping hunks for non-identical patches
                    overlapping_hunks = []
                    for hunk1 in patch1.hunks:
                        for hunk2 in patch2.hunks:
                            if hunk1.overlaps_with(hunk2):
                                overlapping_hunks.append((hunk1, hunk2))

                    if overlapping_hunks:
                        project_overlaps.append(
                            OverlapResult(
                                patch1=patch1,
                                patch2=patch2,
                                is_identical=False,
                                overlapping_hunks=overlapping_hunks,
                            )
                        )

        if project_overlaps:
            overlaps[project] = project_overlaps

    return overlaps


def get_cpv_id(patch: PatchInfo) -> str:
    """Get a unique identifier for a CPV."""
    return f"{patch.benchmark}/{patch.harness}/{patch.cpv}"


def group_identical_patches(
    results: list[OverlapResult],
) -> list[set[str]]:
    """Group identical patches using union-find."""
    # Build adjacency for identical patches
    identical_pairs = [
        (get_cpv_id(r.patch1), get_cpv_id(r.patch2))
        for r in results
        if r.is_identical
    ]

    if not identical_pairs:
        return []

    # Union-find to group identical patches
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        if x not in parent:
            parent[x] = x
        if parent[x] != x:
            parent[x] = find(parent[x])
        return parent[x]

    def union(x: str, y: str) -> None:
        px, py = find(x), find(y)
        if px != py:
            parent[px] = py

    for cpv1, cpv2 in identical_pairs:
        union(cpv1, cpv2)

    # Group by root
    groups: dict[str, set[str]] = defaultdict(set)
    for cpv in parent:
        groups[find(cpv)].add(cpv)

    return list(groups.values())


def print_overlaps(
    overlaps: dict[str, list[OverlapResult]],
    *,
    verbose: bool = False,
) -> None:
    """Print the found overlaps in a readable format."""
    if not overlaps:
        print("No overlapping patch hunks found across different CPVs.")
        return

    print("=" * 80)
    print("PATCH OVERLAP REPORT")
    print("=" * 80)

    total_identical_patches = 0
    total_overlapping_patches = 0

    for project, project_overlaps in sorted(overlaps.items()):
        print(f"\n## Project: {project}")
        print("-" * 60)

        # Group identical patches
        identical_groups = group_identical_patches(project_overlaps)

        # Collect all CPVs involved in identical groups
        identical_cpvs: set[str] = set()
        for group in identical_groups:
            identical_cpvs.update(group)

        # Collect overlapping (non-identical) CPVs
        overlapping_cpvs: set[str] = set()
        for r in project_overlaps:
            if not r.is_identical:
                overlapping_cpvs.add(get_cpv_id(r.patch1))
                overlapping_cpvs.add(get_cpv_id(r.patch2))

        # Print identical groups
        if identical_groups:
            group_patch_count = sum(len(g) for g in identical_groups)
            print(f"\n  IDENTICAL PATCHES ({group_patch_count} patches in {len(identical_groups)} groups):")
            for i, group in enumerate(identical_groups, 1):
                sorted_cpvs = sorted(group)
                print(f"    Group {i} ({len(group)} patches):")
                for cpv in sorted_cpvs:
                    print(f"      - {cpv}")
            total_identical_patches += group_patch_count

        # Print overlapping CPVs
        if overlapping_cpvs:
            print(f"\n  OVERLAPPING PATCHES ({len(overlapping_cpvs)} patches):")
            if verbose:
                # Show detailed overlap info
                seen_pairs: set[tuple[str, str]] = set()
                for r in project_overlaps:
                    if not r.is_identical:
                        cpv1 = get_cpv_id(r.patch1)
                        cpv2 = get_cpv_id(r.patch2)
                        pair_key = (min(cpv1, cpv2), max(cpv1, cpv2))
                        if pair_key not in seen_pairs:
                            seen_pairs.add(pair_key)
                            files = sorted(set(h1.file_path for h1, _ in r.overlapping_hunks))
                            print(f"    {cpv1} <-> {cpv2}")
                            print(f"      files: {', '.join(files)}")
            else:
                for cpv in sorted(overlapping_cpvs):
                    print(f"    - {cpv}")

            total_overlapping_patches += len(overlapping_cpvs)

    # Print summary
    print("\n" + "=" * 80)
    print("SUMMARY")
    print("=" * 80)
    print(f"Projects with overlaps: {len(overlaps)}")
    print(f"Identical patches: {total_identical_patches}")
    print(f"Overlapping patches: {total_overlapping_patches}")
    print("=" * 80)


def export_csv(
    overlaps: dict[str, list[OverlapResult]],
    output_path: Path,
) -> None:
    """Export overlaps to CSV format."""
    import csv

    with output_path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "project",
            "benchmark1",
            "harness1",
            "cpv1",
            "benchmark2",
            "harness2",
            "cpv2",
            "identical_patch",
            "overlapping_files",
        ])

        for project, project_overlaps in sorted(overlaps.items()):
            for result in project_overlaps:
                # Get unique files with overlaps
                overlapping_files = sorted(
                    set(hunk1.file_path for hunk1, _ in result.overlapping_hunks)
                )
                writer.writerow([
                    project,
                    result.patch1.benchmark,
                    result.patch1.harness,
                    result.patch1.cpv,
                    result.patch2.benchmark,
                    result.patch2.harness,
                    result.patch2.cpv,
                    result.is_identical,
                    ";".join(overlapping_files),
                ])

    print(f"CSV exported to: {output_path}")


def main() -> None:
    """Main entry point."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Check for overlapping patch hunks across CPVs within the same project."
    )
    parser.add_argument(
        "--benchmarks-dir",
        type=Path,
        help="Path to benchmarks directory (default: ../benchmarks relative to script)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Show detailed output for each overlap",
    )
    parser.add_argument(
        "--csv",
        type=Path,
        metavar="FILE",
        help="Export results to CSV file",
    )
    parser.add_argument(
        "--project",
        type=str,
        help="Filter results to a specific project",
    )
    args = parser.parse_args()

    # Determine benchmarks directory
    if args.benchmarks_dir:
        benchmarks_dir = args.benchmarks_dir
    else:
        script_dir = Path(__file__).parent
        benchmarks_dir = script_dir.parent / "benchmarks"

    if not benchmarks_dir.exists():
        print(f"Error: Benchmarks directory not found: {benchmarks_dir}")
        return

    print(f"Scanning benchmarks directory: {benchmarks_dir}")
    patches = collect_patches(benchmarks_dir)
    print(f"Found {len(patches)} patch files")

    overlaps = find_overlaps(patches)

    # Filter by project if specified
    if args.project:
        overlaps = {k: v for k, v in overlaps.items() if k == args.project}
        if not overlaps:
            print(f"No overlaps found for project: {args.project}")
            return

    print_overlaps(overlaps, verbose=args.verbose)

    if args.csv:
        export_csv(overlaps, args.csv)


if __name__ == "__main__":
    main()
