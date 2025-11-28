"""Discovery timeline models for realistic POV/patch timing simulation."""

import random
from dataclasses import dataclass, field
from typing import List, Dict, Any, Tuple

from crsbench.bench_snapgen.generator import BenchmarkData
from crsbench.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class DiscoveryEvent:
    """Represents a POV or patch discovery event in the timeline.

    Attributes:
        timestamp: Time in seconds since trial start
        event_type: Type of event ('pov' or 'patch')
        data: Binary blob for POV or text content for patch
        metadata: Additional information (harness, vuln, pov_id, etc.)
        is_valid: True if valid, False if injected fault
    """

    timestamp: float
    event_type: str  # 'pov' or 'patch'
    data: bytes
    metadata: Dict[str, Any] = field(default_factory=dict)
    is_valid: bool = True


class DiscoveryTimeline:
    """Timeline of POV and patch discovery events.

    Manages chronological ordering of discoveries and provides
    filtering by time for incremental snapshot generation.

    Attributes:
        events: List of discovery events, sorted by timestamp
    """

    def __init__(self):
        """Initialize empty timeline."""
        self.events: List[DiscoveryEvent] = []

    def add_pov(
        self,
        time: float,
        pov_blob: bytes,
        harness: str,
        vuln: str,
        pov_id: str,
        sanitizer: str = "",
        error_token: str = "",
        is_valid: bool = True,
    ):
        """Add POV discovery event to timeline.

        Args:
            time: Discovery time in seconds since trial start
            pov_blob: Binary POV data
            harness: Harness name
            vuln: Vulnerability keyword
            pov_id: POV identifier
            sanitizer: Sanitizer type (optional)
            error_token: Error pattern (optional)
            is_valid: Whether this is a valid POV (False for fault injection)
        """
        event = DiscoveryEvent(
            timestamp=time,
            event_type="pov",
            data=pov_blob,
            metadata={
                "harness": harness,
                "vuln": vuln,
                "pov_id": pov_id,
                "sanitizer": sanitizer,
                "error_token": error_token,
            },
            is_valid=is_valid,
        )
        self.events.append(event)
        self._sort_events()

    def add_patch(
        self,
        time: float,
        patch_diff: str,
        harness: str,
        vuln: str,
        patch_id: str,
        is_valid: bool = True,
    ):
        """Add patch generation event to timeline.

        Args:
            time: Generation time in seconds since trial start
            patch_diff: Patch content (unified diff format)
            harness: Harness name
            vuln: Vulnerability keyword
            patch_id: Patch identifier
            is_valid: Whether this is a valid patch (False for fault injection)
        """
        # Convert patch string to bytes for uniform storage
        patch_bytes = patch_diff.encode("utf-8")

        event = DiscoveryEvent(
            timestamp=time,
            event_type="patch",
            data=patch_bytes,
            metadata={
                "harness": harness,
                "vuln": vuln,
                "patch_id": patch_id,
            },
            is_valid=is_valid,
        )
        self.events.append(event)
        self._sort_events()

    def get_events_before(self, time: float) -> List[DiscoveryEvent]:
        """Get all events that occurred before or at the given time.

        Args:
            time: Time threshold in seconds

        Returns:
            List of events with timestamp <= time, sorted by timestamp
        """
        return [event for event in self.events if event.timestamp <= time]

    def _sort_events(self):
        """Sort events by timestamp."""
        self.events.sort(key=lambda e: e.timestamp)


class POVDiscoveryModel:
    """Model realistic POV discovery timing based on difficulty.

    Discovery times are based on percentage of trial duration, with
    harder bugs discovered later. Multiple POVs for the same root cause
    are clustered together with small jitter.
    """

    # Difficulty levels map to discovery time ranges (as % of trial duration)
    DIFFICULTY_TIMING = {
        1: (0.10, 0.30),  # Easy: 10-30% of trial time
        2: (0.25, 0.50),  # Medium-Low: 25-50%
        3: (0.30, 0.60),  # Medium: 30-60%
        4: (0.50, 0.75),  # Hard: 50-75%
        5: (0.50, 0.90),  # Very Hard: 50-90%
    }

    def get_discovery_times(
        self, difficulty: int, pov_count: int, max_time: float
    ) -> List[float]:
        """Generate realistic discovery times for POVs.

        Multiple POVs for the same vulnerability are clustered with jitter,
        simulating a CRS finding variations after the initial discovery.

        Args:
            difficulty: Difficulty level 1-5
            pov_count: Number of POVs to generate times for
            max_time: Maximum trial time in seconds

        Returns:
            List of discovery times in seconds, sorted chronologically

        Raises:
            ValueError: If difficulty not in 1-5 range
        """
        if difficulty not in self.DIFFICULTY_TIMING:
            raise ValueError(f"Invalid difficulty: {difficulty}. Must be 1-5")

        if pov_count == 0:
            return []

        min_pct, max_pct = self.DIFFICULTY_TIMING[difficulty]

        # Base discovery time (when first POV is found)
        base_time = max_time * random.uniform(min_pct, max_pct)

        # Generate times for all POVs, clustering around base time
        times = []
        for i in range(pov_count):
            if i == 0:
                # First POV at base time
                times.append(base_time)
            else:
                # Subsequent POVs clustered with ±5-10% jitter
                jitter_pct = random.uniform(-0.05, 0.10)
                jitter = max_time * jitter_pct
                clustered_time = base_time + jitter

                # Clamp to valid range [0, max_time]
                clustered_time = max(0.0, min(clustered_time, max_time))
                times.append(clustered_time)

        return sorted(times)


class PatchGenerationModel:
    """Model patch generation timing after POV discovery.

    Patches are generated after analyzing POVs, with delay based on
    the difficulty of understanding and fixing the vulnerability.
    """

    # Difficulty levels map to patch generation delays (seconds after first POV)
    DIFFICULTY_DELAYS = {
        1: (300, 900),  # Easy: 5-15 minutes after POV
        2: (600, 1200),  # Medium-Low: 10-20 minutes
        3: (900, 1800),  # Medium: 15-30 minutes
        4: (1200, 2400),  # Hard: 20-40 minutes
        5: (1800, 3600),  # Very Hard: 30-60 minutes
    }

    def get_patch_time(
        self, first_pov_time: float, difficulty: int, max_time: float
    ) -> float:
        """Generate patch generation time after POV discovery.

        Args:
            first_pov_time: Time when first POV was discovered
            difficulty: Difficulty level 1-5
            max_time: Maximum trial time in seconds

        Returns:
            Patch generation time in seconds

        Raises:
            ValueError: If difficulty not in 1-5 range
        """
        if difficulty not in self.DIFFICULTY_DELAYS:
            raise ValueError(f"Invalid difficulty: {difficulty}. Must be 1-5")

        min_delay, max_delay = self.DIFFICULTY_DELAYS[difficulty]
        delay = random.uniform(min_delay, max_delay)

        patch_time = first_pov_time + delay

        # Don't exceed trial duration
        return min(patch_time, max_time)


def create_discovery_timeline(
    benchmark_data: BenchmarkData,
    difficulty_level: int,
    max_time: float,
    mode: str = "bug-finding",
) -> DiscoveryTimeline:
    """Create timeline of POV/patch discoveries from benchmark ground truth.

    Args:
        benchmark_data: Loaded benchmark ground truth
        difficulty_level: Difficulty level 1-5 (controls timing)
        max_time: Maximum trial time in seconds
        mode: 'bug-finding' (POVs only) or 'patch-generation' (POVs + patches)

    Returns:
        DiscoveryTimeline with events ordered chronologically

    Raises:
        ValueError: If mode invalid or difficulty not in 1-5
    """
    if mode not in ("bug-finding", "patch-generation"):
        raise ValueError(f"Invalid mode: {mode}")
    if not 1 <= difficulty_level <= 5:
        raise ValueError(f"difficulty_level must be 1-5, got {difficulty_level}")

    timeline = DiscoveryTimeline()
    pov_model = POVDiscoveryModel()
    patch_model = PatchGenerationModel()

    logger.info(
        f"Creating discovery timeline: mode={mode}, difficulty={difficulty_level}, "
        f"max_time={max_time}s"
    )

    # Track first POV time per vulnerability for patch timing
    vuln_first_pov_times: Dict[Tuple[str, str], float] = {}

    # Generate POV discoveries for each vulnerability
    for harness in benchmark_data.meta.harness_files:
        for vuln in harness.vulns or []:
            # Get POVs for this vulnerability from ground truth
            # key is (harness_name, vuln_keyword, pov_id)
            vuln_povs = [
                (key[2], key)  # (pov_id, full_key)
                for key, pov_data in benchmark_data.povs.items()
                if key[0] == harness.name and key[1] == vuln.vuln_keyword
            ]

            if not vuln_povs:
                logger.warning(
                    f"No POVs found for {harness.name}/{vuln.vuln_keyword}"
                )
                continue

            # Generate discovery times for this vulnerability's POVs
            pov_count = len(vuln_povs)
            discovery_times = pov_model.get_discovery_times(
                difficulty=difficulty_level, pov_count=pov_count, max_time=max_time
            )

            # Add POV discoveries to timeline
            for (pov_id, pov_key), time in zip(vuln_povs, discovery_times):
                pov_data = benchmark_data.povs[pov_key]

                timeline.add_pov(
                    time=time,
                    pov_blob=pov_data.blob,
                    harness=harness.name,
                    vuln=vuln.vuln_keyword,
                    pov_id=pov_id,
                    sanitizer=pov_data.sanitizer,
                    error_token=pov_data.error_token or "",
                    is_valid=True,
                )

            # Track first POV time for patch generation
            if discovery_times:
                vuln_key = (harness.name, vuln.vuln_keyword)
                vuln_first_pov_times[vuln_key] = min(discovery_times)

    # Generate patch discoveries (for patch-generation mode)
    if mode == "patch-generation":
        for harness in benchmark_data.meta.harness_files:
            for vuln in harness.vulns or []:
                vuln_key = (harness.name, vuln.vuln_keyword)

                # Only generate patch if we have POVs for this vuln
                if vuln_key not in vuln_first_pov_times:
                    continue

                first_pov_time = vuln_first_pov_times[vuln_key]

                # Calculate patch generation time
                patch_time = patch_model.get_patch_time(
                    first_pov_time, difficulty_level, max_time
                )

                # Get patch from ground truth (usually patch_0 fixes all POVs)
                patch_key = (harness.name, vuln.vuln_keyword, "patch_0")
                if patch_key in benchmark_data.patches:
                    patch_content = benchmark_data.patches[patch_key]

                    timeline.add_patch(
                        time=patch_time,
                        patch_diff=patch_content,
                        harness=harness.name,
                        vuln=vuln.vuln_keyword,
                        patch_id="patch_0",
                        is_valid=True,
                    )
                else:
                    logger.warning(
                        f"No patch found for {harness.name}/{vuln.vuln_keyword}"
                    )

    logger.info(
        f"Created timeline with {len(timeline.events)} events "
        f"({len([e for e in timeline.events if e.event_type == 'pov'])} POVs, "
        f"{len([e for e in timeline.events if e.event_type == 'patch'])} patches)"
    )

    return timeline
