"""Container-internal network blocking for CRS anti-cheating.

This module provides functions to block CRS containers from accessing
vulnerability databases and search engines by injecting iptables rules
INSIDE the container via docker exec.

Architecture:
    [CRS Container with --privileged]
           │
           │ iptables OUTPUT chain (inside container)
           │ DROP packets matching blocked domains
           ▼
    [Internet]

This approach is safer than modifying host iptables because:
- Only affects the specific CRS container
- Rules are automatically cleaned up when container stops
- No impact on other containers or host networking
"""

import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from crsbench.utils.logger import get_logger

logger = get_logger(__name__)

# Path to blocked domains file
BLOCKED_DOMAINS_FILE = Path(__file__).parent / "blocked_domains.txt"


class ContainerBlockingConfig(BaseModel):
    """Configuration for container-internal network blocking.

    This contains information about the blocking rules applied to a container.
    """

    container_id: str = Field(description="Container ID or name")
    rules_count: int = Field(description="Number of blocking rules applied")

    model_config = {"frozen": True}


def _load_blocked_domains() -> list[str]:
    """Load blocked domains from the configuration file.

    Returns:
        List of blocked domain patterns (without leading dots).
    """
    domains = []
    if not BLOCKED_DOMAINS_FILE.exists():
        logger.warning(f"Blocked domains file not found: {BLOCKED_DOMAINS_FILE}")
        return domains

    with BLOCKED_DOMAINS_FILE.open() as f:
        for line in f:
            line = line.strip()
            # Skip comments and empty lines
            if not line or line.startswith("#"):
                continue
            # Remove leading dot (e.g., ".google.com" -> "google.com")
            if line.startswith("."):
                line = line[1:]
            domains.append(line)

    return domains


def is_already_blocked(container_id: str) -> bool:
    """Check if container already has blocking rules.

    Args:
        container_id: Docker container ID or name.

    Returns:
        True if blocking rules are already present, False otherwise.
    """
    result = subprocess.run(
        ["docker", "exec", container_id, "iptables", "-L", "OUTPUT", "-n"],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        return False

    # Check if DROP rules with string matching exist
    return "DROP" in result.stdout and "STRING" in result.stdout


def inject_blocking_rules(container_id: str) -> ContainerBlockingConfig:
    """Inject iptables blocking rules into a running container.

    This function uses docker exec to add iptables OUTPUT rules that block
    traffic to blocked domains. The container must be running with
    --privileged or --cap-add=NET_ADMIN.

    Args:
        container_id: Docker container ID or name.

    Returns:
        ContainerBlockingConfig with details about applied rules.

    Raises:
        RuntimeError: If rule injection fails.
    """
    # Check if already blocked (prevents duplicate rules in parallel execution)
    if is_already_blocked(container_id):
        logger.info(f"Container {container_id} already has blocking rules, skipping")
        return ContainerBlockingConfig(container_id=container_id, rules_count=0)

    domains = _load_blocked_domains()
    if not domains:
        logger.warning("No blocked domains configured")
        return ContainerBlockingConfig(container_id=container_id, rules_count=0)

    logger.info(f"Injecting blocking rules into container {container_id}")
    logger.info(
        f"Blocking {len(domains)} domains (HTTP + HTTPS = {len(domains) * 2} rules)"
    )

    rules_applied = 0

    for domain in domains:
        # Block HTTP (port 80) traffic to this domain
        http_cmd = [
            "docker",
            "exec",
            container_id,
            "iptables",
            "-A",
            "OUTPUT",
            "-p",
            "tcp",
            "--dport",
            "80",
            "-m",
            "string",
            "--string",
            domain,
            "--algo",
            "bm",
            "-j",
            "DROP",
        ]

        result = subprocess.run(http_cmd, capture_output=True, text=True)
        if result.returncode != 0:
            logger.warning(
                f"Failed to add HTTP block rule for {domain}: {result.stderr}"
            )
        else:
            rules_applied += 1

        # Block HTTPS (port 443) traffic to this domain
        https_cmd = [
            "docker",
            "exec",
            container_id,
            "iptables",
            "-A",
            "OUTPUT",
            "-p",
            "tcp",
            "--dport",
            "443",
            "-m",
            "string",
            "--string",
            domain,
            "--algo",
            "bm",
            "-j",
            "DROP",
        ]

        result = subprocess.run(https_cmd, capture_output=True, text=True)
        if result.returncode != 0:
            logger.warning(
                f"Failed to add HTTPS block rule for {domain}: {result.stderr}"
            )
        else:
            rules_applied += 1

    logger.info(
        f"Applied {rules_applied}/{len(domains) * 2} blocking rules to container {container_id}"
    )

    return ContainerBlockingConfig(
        container_id=container_id,
        rules_count=rules_applied,
    )


def verify_blocking_rules(container_id: str) -> bool:
    """Verify that blocking rules are active in a container.

    Args:
        container_id: Docker container ID or name.

    Returns:
        True if blocking rules are present, False otherwise.
    """
    result = subprocess.run(
        ["docker", "exec", container_id, "iptables", "-L", "OUTPUT", "-n"],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        logger.warning(f"Failed to verify rules in container {container_id}")
        return False

    # Check if any DROP rules with string matching exist
    return "DROP" in result.stdout and "string" in result.stdout.lower()


def clear_blocking_rules(container_id: str) -> None:
    """Clear all blocking rules from a container.

    This flushes the OUTPUT chain, removing all blocking rules.
    Note: This is usually not necessary as rules are automatically
    cleaned up when the container stops.

    Args:
        container_id: Docker container ID or name.
    """
    logger.info(f"Clearing blocking rules from container {container_id}")

    result = subprocess.run(
        ["docker", "exec", container_id, "iptables", "-F", "OUTPUT"],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        logger.warning(f"Failed to clear rules: {result.stderr}")


# =============================================================================
# Container Watcher for automatic blocking rule injection
# =============================================================================

# Default polling interval in seconds
DEFAULT_POLL_INTERVAL = 5.0


def find_containers_by_image(image_name: str) -> list[str]:
    """Find running container IDs by image name.

    Args:
        image_name: Docker image name (e.g., "atlantis-c:latest").

    Returns:
        List of container IDs running the specified image.
    """
    result = subprocess.run(
        [
            "docker",
            "ps",
            "--filter",
            f"ancestor={image_name}",
            "--format",
            "{{.ID}}",
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        logger.warning(f"Failed to find containers for image {image_name}")
        return []

    return [cid.strip() for cid in result.stdout.strip().split("\n") if cid.strip()]


class ContainerBlockingWatcher:
    """Background watcher that injects blocking rules into new CRS containers.

    This class monitors for new Docker containers running a specific image
    and automatically injects iptables blocking rules when they appear.

    Usage:
        watcher = ContainerBlockingWatcher("crs-image:latest")
        watcher.start()
        try:
            # Run CRS command here (blocking)
            subprocess.run(["oss-crs", "run", ...])
        finally:
            watcher.stop()

    Or as a context manager:
        with ContainerBlockingWatcher("crs-image:latest"):
            subprocess.run(["oss-crs", "run", ...])
    """

    def __init__(
        self,
        image_name: str,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
    ):
        """Initialize the container blocking watcher.

        Args:
            image_name: Docker image name to watch for.
            poll_interval: Seconds between polling for new containers.
        """
        self.image_name = image_name
        self.poll_interval = poll_interval
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._seen_containers: set[str] = set()
        self._lock = threading.Lock()

    def start(self) -> None:
        """Start the background watcher thread."""
        if self._running:
            logger.warning("ContainerBlockingWatcher is already running")
            return

        self._running = True
        self._seen_containers = set()
        self._thread = threading.Thread(target=self._watch_loop, daemon=True)
        self._thread.start()
        logger.info(
            f"Started ContainerBlockingWatcher for image {self.image_name} "
            f"(poll interval: {self.poll_interval}s)"
        )

    def stop(self) -> None:
        """Stop the background watcher thread."""
        if not self._running:
            return

        self._running = False
        if self._thread:
            self._thread.join(timeout=self.poll_interval + 1)
            self._thread = None

        with self._lock:
            blocked_count = len(self._seen_containers)

        logger.info(
            f"Stopped ContainerBlockingWatcher (blocked {blocked_count} container(s))"
        )

    def _watch_loop(self) -> None:
        """Main loop that polls for new containers and injects rules."""
        while self._running:
            try:
                self._check_and_block_containers()
            except Exception as e:
                logger.error(f"Error in container watcher: {e}")

            time.sleep(self.poll_interval)

    def _check_and_block_containers(self) -> None:
        """Check for new containers and inject blocking rules."""
        container_ids = find_containers_by_image(self.image_name)

        for container_id in container_ids:
            with self._lock:
                if container_id in self._seen_containers:
                    continue
                self._seen_containers.add(container_id)

            logger.info(f"New container detected: {container_id}")
            try:
                inject_blocking_rules(container_id)
            except Exception as e:
                logger.error(
                    f"Failed to inject blocking rules into {container_id}: {e}"
                )

    def __enter__(self) -> "ContainerBlockingWatcher":
        """Context manager entry - start the watcher."""
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Context manager exit - stop the watcher."""
        self.stop()

    @property
    def blocked_containers(self) -> set[str]:
        """Return the set of container IDs that have been blocked."""
        with self._lock:
            return self._seen_containers.copy()
