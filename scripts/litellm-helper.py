#!/usr/bin/env python3
"""LiteLLM Helper - Manage LiteLLM instances for CRSBench.

This script provides convenient commands to manage LiteLLM proxy instances,
similar to the valkey-helper.py script.

Usage:
    python scripts/litellm-helper.py start [--port PORT] [--config CONFIG]
    python scripts/litellm-helper.py stop
    python scripts/litellm-helper.py status
    python scripts/litellm-helper.py logs [-f]
    python scripts/litellm-helper.py health
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

# Paths
PROJECT_ROOT = Path(__file__).parent.parent
SERVICES_DIR = PROJECT_ROOT / "services" / "litellm"
DEFAULT_CONFIG = SERVICES_DIR / "default-models.yaml"
CONTAINER_NAME = "litellm"


def run_command(cmd, capture=True, check=True):
    """Run a shell command."""
    try:
        if capture:
            result = subprocess.run(
                cmd, capture_output=True, text=True, check=check
            )
            return result.stdout.strip()
        else:
            subprocess.run(cmd, check=check)
            return None
    except subprocess.CalledProcessError as e:
        print(f"Error: {e.stderr if capture else e}", file=sys.stderr)
        return None


def start(args):
    """Start LiteLLM instance."""
    port = args.port
    config = Path(args.config)

    if not config.exists():
        print(f"Error: Config file not found: {config}")
        return 1

    # Check if already running
    if is_running():
        print(f"LiteLLM is already running (container: {CONTAINER_NAME})")
        return 1

    print(f"Starting LiteLLM on port {port}...")
    print(f"Config: {config}")

    # Create logs directory
    logs_dir = PROJECT_ROOT / "logs" / "litellm"
    logs_dir.mkdir(parents=True, exist_ok=True)

    # Run LiteLLM container
    cmd = [
        "docker", "run", "-d",
        "--name", CONTAINER_NAME,
        "-p", f"{port}:4000",
        "-v", f"{config}:/app/config.yaml:ro",
        "-v", f"{logs_dir}:/logs",
        "--env-file", str(PROJECT_ROOT / ".env"),
        "-e", "JSON_LOGS=true",
        "ghcr.io/berriai/litellm:main-stable",
        "--config", "/app/config.yaml",
        "--detailed_debug"
    ]

    result = run_command(cmd)
    if result:
        print(f"✓ LiteLLM started successfully")
        print(f"  Container: {CONTAINER_NAME}")
        print(f"  Port: {port}")
        print(f"  Logs: {logs_dir}")
        print(f"\nCheck health: python scripts/litellm-helper.py health")
        return 0
    else:
        print("✗ Failed to start LiteLLM")
        return 1


def stop(args):
    """Stop LiteLLM instance."""
    if not is_running():
        print(f"LiteLLM is not running")
        return 1

    print(f"Stopping LiteLLM...")
    run_command(["docker", "stop", CONTAINER_NAME], capture=False)
    run_command(["docker", "rm", CONTAINER_NAME], capture=False)
    print(f"✓ LiteLLM stopped")
    return 0


def status(args):
    """Show LiteLLM status."""
    if is_running():
        # Get container info
        inspect = run_command([
            "docker", "inspect", CONTAINER_NAME,
            "--format", "{{json .}}"]
        )

        if inspect:
            info = json.loads(inspect)
            state = info["State"]
            config = info["Config"]
            network = info["NetworkSettings"]

            print("LiteLLM Status: RUNNING")
            print(f"  Container: {CONTAINER_NAME}")
            print(f"  Image: {config['Image']}")
            print(f"  Started: {state['StartedAt']}")
            print(f"  Status: {state['Status']}")

            # Port mappings
            ports = network.get("Ports", {})
            for container_port, bindings in ports.items():
                if bindings:
                    for binding in bindings:
                        host_port = binding['HostPort']
                        print(f"  Port: {host_port} → {container_port}")

            # Health status
            health = state.get("Health", {})
            if health:
                print(f"  Health: {health.get('Status', 'unknown')}")

            return 0
    else:
        print("LiteLLM Status: NOT RUNNING")
        return 1


def logs(args):
    """Show LiteLLM logs."""
    if not is_running():
        print(f"LiteLLM is not running")
        return 1

    cmd = ["docker", "logs", CONTAINER_NAME]
    if args.follow:
        cmd.append("-f")

    subprocess.run(cmd)
    return 0


def health(args):
    """Check LiteLLM health."""
    if not is_running():
        print("✗ LiteLLM is not running")
        return 1

    # Get port
    inspect = run_command([
        "docker", "inspect", CONTAINER_NAME,
        "--format", "{{json .NetworkSettings.Ports}}"]
    )

    if not inspect:
        print("✗ Failed to get container info")
        return 1

    ports = json.loads(inspect)
    port = None
    for bindings in ports.values():
        if bindings:
            port = bindings[0]["HostPort"]
            break

    if not port:
        print("✗ No port mapping found")
        return 1

    # Check health endpoint
    try:
        import requests
        response = requests.get(f"http://localhost:{port}/health", timeout=5)
        if response.status_code == 200:
            print(f"✓ LiteLLM is healthy (port {port})")
            print(f"  Response: {response.json()}")
            return 0
        else:
            print(f"✗ Health check failed: {response.status_code}")
            return 1
    except ImportError:
        # Fallback to curl
        result = run_command([
            "curl", "-sf", f"http://localhost:{port}/health"
        ])
        if result:
            print(f"✓ LiteLLM is healthy (port {port})")
            return 0
        else:
            print(f"✗ Health check failed")
            return 1
    except Exception as e:
        print(f"✗ Health check failed: {e}")
        return 1


def is_running():
    """Check if LiteLLM container is running."""
    result = run_command([
        "docker", "ps", "-q", "-f", f"name={CONTAINER_NAME}"
    ])
    return bool(result)


def main():
    parser = argparse.ArgumentParser(
        description="LiteLLM Helper - Manage LiteLLM instances"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # start command
    start_parser = subparsers.add_parser("start", help="Start LiteLLM instance")
    start_parser.add_argument(
        "--port", type=int, default=4000,
        help="Host port to expose (default: 4000)"
    )
    start_parser.add_argument(
        "--config", default=str(DEFAULT_CONFIG),
        help=f"Path to config file (default: {DEFAULT_CONFIG})"
    )

    # stop command
    subparsers.add_parser("stop", help="Stop LiteLLM instance")

    # status command
    subparsers.add_parser("status", help="Show LiteLLM status")

    # logs command
    logs_parser = subparsers.add_parser("logs", help="View LiteLLM logs")
    logs_parser.add_argument(
        "-f", "--follow", action="store_true",
        help="Follow log output"
    )

    # health command
    subparsers.add_parser("health", help="Check LiteLLM health")

    args = parser.parse_args()

    # Map commands to functions
    commands = {
        "start": start,
        "stop": stop,
        "status": status,
        "logs": logs,
        "health": health,
    }

    return commands[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
