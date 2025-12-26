"""Fault injection for generating invalid POVs and patches.

Used for testing evaluation pipeline robustness by injecting
realistic-looking but invalid data.
"""

import random

from crsbench.bench_snapgen.timeline import DiscoveryTimeline
from crsbench.utils.logger import get_logger

logger = get_logger(__name__)


class FaultInjector:
    """Generate invalid POVs and patches for testing.

    Injects various types of faults to test evaluation robustness:
    - Invalid POVs: Random data that won't trigger crashes
    - Invalid patches: Various malformed or incorrect patch types

    Attributes:
        fault_rate: Probability of injecting faults (0.0-1.0)
    """

    def __init__(self, fault_rate: float = 0.1):
        """Initialize fault injector.

        Args:
            fault_rate: Probability of injection (default: 0.1 = 10%)

        Raises:
            ValueError: If fault_rate not in [0.0, 1.0]
        """
        if not 0.0 <= fault_rate <= 1.0:
            raise ValueError(f"fault_rate must be in [0.0, 1.0], got {fault_rate}")

        self.fault_rate = fault_rate
        self.invalid_pov_counter = 0
        self.invalid_patch_counter = 0

    def should_inject(self) -> bool:
        """Probabilistically decide if fault should be injected.

        Returns:
            True if fault should be injected based on fault_rate
        """
        return random.random() < self.fault_rate

    def create_invalid_pov(self) -> tuple[bytes, dict]:
        """Create invalid POV that won't trigger crash.

        Returns:
            Tuple of (pov_blob, metadata) for invalid POV
        """
        self.invalid_pov_counter += 1

        # Generate random data that won't trigger vulnerability
        blob_size = random.randint(64, 512)
        blob = random.randbytes(blob_size)

        metadata = {
            "pov_id": f"invalid_pov_{self.invalid_pov_counter}",
            "harness": "unknown",
            "vuln": "unknown",
            "sanitizer": "none",
            "error_token": "",
            "fault_type": "invalid_pov",
        }

        return blob, metadata

    def create_invalid_patch(
        self, fault_type: str = "syntax_error"
    ) -> tuple[str, dict]:
        """Create invalid patch with specified fault type.

        Args:
            fault_type: Type of fault to inject:
                - 'syntax_error': Malformed diff format
                - 'wrong_file': Patches non-existent file
                - 'incomplete': Partial/incomplete fix
                - 'breaks_build': Introduces syntax errors

        Returns:
            Tuple of (patch_content, metadata) for invalid patch

        Raises:
            ValueError: If fault_type invalid
        """
        valid_types = ("syntax_error", "wrong_file", "incomplete", "breaks_build")
        if fault_type not in valid_types:
            raise ValueError(
                f"Invalid fault_type: {fault_type}. Must be one of {valid_types}"
            )

        self.invalid_patch_counter += 1

        if fault_type == "syntax_error":
            # Malformed diff (invalid unified diff format)
            patch_content = """--- a/src/main.c
+++ b/src/main.c
@ -10,5 +10,5  # Invalid hunk header (missing second @)
-    old line
+    new line
"""

        elif fault_type == "wrong_file":
            # Patches a file that doesn't exist or unrelated to vulnerability
            patch_content = """--- a/nonexistent_file_that_does_not_exist.c
+++ b/nonexistent_file_that_does_not_exist.c
@@ -1,3 +1,3 @@
-old code
+new code
"""

        elif fault_type == "incomplete":
            # Patches only some vulnerable code, doesn't fix all POVs
            patch_content = """--- a/src/parser.c
+++ b/src/parser.c
@@ -45,7 +45,7 @@
 void parse_input(char *input, size_t len) {
-    // Missing bound check fix here
+    if (len > MAX_SIZE) return;  // Only partial fix, missing other checks
 }
"""

        elif fault_type == "breaks_build":
            # Introduces syntax error in code
            patch_content = """--- a/src/parser.c
+++ b/src/parser.c
@@ -45,7 +45,7 @@
 void parse_input(char *input, size_t len) {
-    char buffer[256];
+    char buffer[512]  // Missing semicolon - breaks build
 }
"""

        metadata = {
            "patch_id": f"invalid_patch_{self.invalid_patch_counter}",
            "vuln": "unknown",
            "harness": "unknown",
            "fault_type": fault_type,
        }

        return patch_content, metadata


def inject_faults_into_timeline(
    timeline: DiscoveryTimeline, fault_injector: FaultInjector, max_time: float
):
    """Inject invalid POVs and patches into timeline.

    Modifies timeline in-place by adding invalid events scattered
    throughout the trial duration.

    Args:
        timeline: Discovery timeline to inject faults into
        fault_injector: Fault injector instance
        max_time: Maximum trial time in seconds
    """
    if fault_injector.fault_rate == 0.0:
        return  # No injection

    # Count valid events
    valid_povs = [e for e in timeline.events if e.event_type == "pov" and e.is_valid]
    valid_patches = [
        e for e in timeline.events if e.event_type == "patch" and e.is_valid
    ]

    # Calculate injection counts
    invalid_pov_count = int(len(valid_povs) * fault_injector.fault_rate)
    invalid_patch_count = int(len(valid_patches) * fault_injector.fault_rate)

    logger.info(
        f"Injecting {invalid_pov_count} invalid POVs and "
        f"{invalid_patch_count} invalid patches (rate={fault_injector.fault_rate})"
    )

    # Inject invalid POVs (randomly scattered)
    for _ in range(invalid_pov_count):
        pov_blob, metadata = fault_injector.create_invalid_pov()
        time = random.uniform(0, max_time)

        timeline.add_pov(
            time=time,
            pov_blob=pov_blob,
            harness=metadata["harness"],
            vuln=metadata["vuln"],
            pov_id=metadata["pov_id"],
            sanitizer=metadata["sanitizer"],
            error_token=metadata["error_token"],
            is_valid=False,  # Mark as invalid for tracking
        )

    # Inject invalid patches (randomly scattered)
    fault_types = ["syntax_error", "wrong_file", "incomplete", "breaks_build"]
    for _ in range(invalid_patch_count):
        fault_type = random.choice(fault_types)
        patch_content, metadata = fault_injector.create_invalid_patch(fault_type)
        time = random.uniform(0, max_time)

        timeline.add_patch(
            time=time,
            patch_diff=patch_content,
            harness=metadata["harness"],
            vuln=metadata["vuln"],
            patch_id=metadata["patch_id"],
            is_valid=False,  # Mark as invalid for tracking
        )

    logger.info(
        f"Injected {invalid_pov_count} invalid POVs and "
        f"{invalid_patch_count} invalid patches into timeline"
    )
