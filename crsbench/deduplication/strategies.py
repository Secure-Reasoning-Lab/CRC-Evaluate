"""Deduplication strategies for POV analysis."""

from abc import ABC, abstractmethod
from typing import List, Set, Dict, Tuple
from dataclasses import dataclass
from .analyzer import RootCause


@dataclass
class DeduplicationGroup:
    """A group of POVs that share the same root cause."""
    root_cause: RootCause
    pov_names: List[str]
    representative_pov: str  # The POV chosen to represent this group


class DeduplicationStrategy(ABC):
    """Abstract base class for POV deduplication strategies."""

    @abstractmethod
    def deduplicate(self, root_causes: Dict[str, RootCause]) -> List[DeduplicationGroup]:
        """Deduplicate POVs based on root cause analysis.

        Args:
            root_causes: Mapping of POV name to its root cause

        Returns:
            List of deduplication groups
        """
        pass


class ExactMatchStrategy(DeduplicationStrategy):
    """Strategy that groups POVs with exactly matching root causes."""

    def deduplicate(self, root_causes: Dict[str, RootCause]) -> List[DeduplicationGroup]:
        """Group POVs with identical root causes."""
        groups = {}

        for pov_name, root_cause in root_causes.items():
            # Use root cause hash as grouping key
            key = hash(root_cause)

            if key not in groups:
                groups[key] = {
                    'root_cause': root_cause,
                    'povs': []
                }

            groups[key]['povs'].append(pov_name)

        # Convert to DeduplicationGroup objects
        result = []
        for group_data in groups.values():
            povs = group_data['povs']
            result.append(DeduplicationGroup(
                root_cause=group_data['root_cause'],
                pov_names=povs,
                representative_pov=self._select_representative(povs)
            ))

        return result

    def _select_representative(self, pov_names: List[str]) -> str:
        """Select representative POV from a group."""
        # Simple heuristic: choose the first alphabetically
        return sorted(pov_names)[0]


class LocationBasedStrategy(DeduplicationStrategy):
    """Strategy that groups POVs based on source location similarity."""

    def __init__(self, location_tolerance: int = 5):
        """Initialize with location tolerance in lines."""
        self.location_tolerance = location_tolerance

    def deduplicate(self, root_causes: Dict[str, RootCause]) -> List[DeduplicationGroup]:
        """Group POVs based on source location proximity."""
        # Group by vulnerability type and file first
        type_file_groups = {}

        for pov_name, root_cause in root_causes.items():
            file_part = root_cause.source_location.split(':')[0]
            key = (root_cause.vulnerability_type, file_part)

            if key not in type_file_groups:
                type_file_groups[key] = []

            type_file_groups[key].append((pov_name, root_cause))

        # Within each type/file group, cluster by line proximity
        result = []
        for (vuln_type, file), povs in type_file_groups.items():
            location_groups = self._cluster_by_location(povs)
            result.extend(location_groups)

        return result

    def _cluster_by_location(self, povs: List[Tuple[str, RootCause]]) -> List[DeduplicationGroup]:
        """Cluster POVs by source location proximity."""
        if not povs:
            return []

        # Sort by line number
        sorted_povs = sorted(povs, key=lambda x: self._extract_line(x[1].source_location))
        clusters = []
        current_cluster = [sorted_povs[0]]

        for pov_name, root_cause in sorted_povs[1:]:
            current_line = self._extract_line(root_cause.source_location)
            cluster_line = self._extract_line(current_cluster[0][1].source_location)

            if abs(current_line - cluster_line) <= self.location_tolerance:
                current_cluster.append((pov_name, root_cause))
            else:
                # Finalize current cluster and start new one
                clusters.append(self._create_group_from_cluster(current_cluster))
                current_cluster = [(pov_name, root_cause)]

        # Add final cluster
        if current_cluster:
            clusters.append(self._create_group_from_cluster(current_cluster))

        return clusters

    def _extract_line(self, source_location: str) -> int:
        """Extract line number from source location."""
        try:
            return int(source_location.split(':')[1])
        except (IndexError, ValueError):
            return 0

    def _create_group_from_cluster(self, cluster: List[Tuple[str, RootCause]]) -> DeduplicationGroup:
        """Create deduplication group from cluster."""
        pov_names = [pov_name for pov_name, _ in cluster]
        representative_root_cause = cluster[0][1]  # Use first as representative

        return DeduplicationGroup(
            root_cause=representative_root_cause,
            pov_names=pov_names,
            representative_pov=pov_names[0]
        )


class StackTraceStrategy(DeduplicationStrategy):
    """Strategy that groups POVs based on stack trace similarity."""

    def __init__(self, stack_depth: int = 3, similarity_threshold: float = 0.7):
        """Initialize with stack trace comparison parameters.

        Args:
            stack_depth: Number of top stack frames to compare
            similarity_threshold: Minimum similarity score (0.0-1.0)
        """
        self.stack_depth = stack_depth
        self.similarity_threshold = similarity_threshold

    def deduplicate(self, root_causes: Dict[str, RootCause]) -> List[DeduplicationGroup]:
        """Group POVs based on stack trace similarity."""
        pov_list = list(root_causes.items())
        groups = []
        processed = set()

        for i, (pov_name, root_cause) in enumerate(pov_list):
            if pov_name in processed:
                continue

            # Find similar POVs
            similar_povs = [(pov_name, root_cause)]
            processed.add(pov_name)

            for j, (other_pov, other_cause) in enumerate(pov_list[i+1:], i+1):
                if other_pov in processed:
                    continue

                if self._are_stack_traces_similar(root_cause, other_cause):
                    similar_povs.append((other_pov, other_cause))
                    processed.add(other_pov)

            # Create group if we found similar POVs
            if similar_povs:
                groups.append(self._create_group_from_similar(similar_povs))

        return groups

    def _are_stack_traces_similar(self, cause1: RootCause, cause2: RootCause) -> bool:
        """Check if two root causes have similar stack traces."""
        # Must be same vulnerability type
        if cause1.vulnerability_type != cause2.vulnerability_type:
            return False

        # Compare top N frames
        frames1 = cause1.stack_trace[:self.stack_depth]
        frames2 = cause2.stack_trace[:self.stack_depth]

        if not frames1 or not frames2:
            return False

        # Calculate similarity score
        matches = 0
        total = max(len(frames1), len(frames2))

        for f1, f2 in zip(frames1, frames2):
            if f1.function == f2.function and f1.file == f2.file:
                matches += 1

        similarity = matches / total if total > 0 else 0.0
        return similarity >= self.similarity_threshold

    def _create_group_from_similar(self, similar_povs: List[Tuple[str, RootCause]]) -> DeduplicationGroup:
        """Create deduplication group from similar POVs."""
        pov_names = [pov_name for pov_name, _ in similar_povs]

        # Choose representative with highest confidence
        best_pov = max(similar_povs, key=lambda x: x[1].confidence)

        return DeduplicationGroup(
            root_cause=best_pov[1],
            pov_names=pov_names,
            representative_pov=best_pov[0]
        )


class HybridStrategy(DeduplicationStrategy):
    """Hybrid strategy that combines multiple deduplication approaches."""

    def __init__(self, strategies: List[DeduplicationStrategy] = None):
        """Initialize with list of strategies to combine."""
        if strategies is None:
            strategies = [
                ExactMatchStrategy(),
                LocationBasedStrategy(location_tolerance=3),
                StackTraceStrategy(stack_depth=3, similarity_threshold=0.8)
            ]
        self.strategies = strategies

    def deduplicate(self, root_causes: Dict[str, RootCause]) -> List[DeduplicationGroup]:
        """Apply multiple strategies and merge results."""
        all_groups = []

        # Apply each strategy
        for strategy in self.strategies:
            groups = strategy.deduplicate(root_causes)
            all_groups.extend(groups)

        # Merge overlapping groups
        return self._merge_overlapping_groups(all_groups)

    def _merge_overlapping_groups(self, groups: List[DeduplicationGroup]) -> List[DeduplicationGroup]:
        """Merge groups that have overlapping POVs."""
        if not groups:
            return []

        # Convert to sets for easier manipulation
        group_sets = [(set(g.pov_names), g) for g in groups]
        merged = []

        while group_sets:
            current_set, current_group = group_sets.pop(0)
            merged_with = []

            # Find overlapping groups
            remaining = []
            for pov_set, group in group_sets:
                if current_set & pov_set:  # Intersection exists
                    current_set |= pov_set  # Union
                    merged_with.append(group)
                else:
                    remaining.append((pov_set, group))

            group_sets = remaining

            # Create merged group
            if merged_with:
                # Combine all POVs
                all_povs = list(current_set)

                # Choose best representative (highest confidence)
                all_causes = [current_group] + merged_with
                best_group = max(all_causes, key=lambda g: g.root_cause.confidence)

                merged_group = DeduplicationGroup(
                    root_cause=best_group.root_cause,
                    pov_names=all_povs,
                    representative_pov=best_group.representative_pov
                )
            else:
                merged_group = current_group

            merged.append(merged_group)

        return merged