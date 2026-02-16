#!/usr/bin/env python3
"""
Valkey Helper - Service management for CRSBench distributed execution

This utility script simplifies common Valkey service management tasks and queue
operations for testing and development workflows.

Usage:
    # Service management
    python scripts/valkey-helper.py start                # localhost:6379
    python scripts/valkey-helper.py --password start     # 0.0.0.0:6379 with auth (remote workers)
    python scripts/valkey-helper.py stop
    python scripts/valkey-helper.py restart
    python scripts/valkey-helper.py status
    python scripts/valkey-helper.py logs

    # Queue management
    python scripts/valkey-helper.py clean my-experiment
    python scripts/valkey-helper.py clean-all
    python scripts/valkey-helper.py list-queues
    python scripts/valkey-helper.py queue-info my-experiment

    # Monitoring
    python scripts/valkey-helper.py stats

Examples:
    # Quick start (binds to localhost:6379)
    $ python scripts/valkey-helper.py start
    $ python scripts/valkey-helper.py status

    # Start with password auth (for remote workers)
    $ python scripts/valkey-helper.py --password start
    $ scp .env user@worker-machine:/path/to/CRSBench/.env

    # Clean up between experiments
    $ python scripts/valkey-helper.py clean test-exp-1
    $ python scripts/valkey-helper.py list-queues

    # Full reset for clean slate
    $ python scripts/valkey-helper.py clean-all

    # Debug queue issues
    $ python scripts/valkey-helper.py queue-info my-experiment
    $ python scripts/valkey-helper.py stats
"""

import argparse
import secrets
import subprocess
import sys
import os
from pathlib import Path


# ANSI color codes for terminal output
class Colors:
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    BLUE = '\033[94m'
    RESET = '\033[0m'
    BOLD = '\033[1m'


def print_success(msg: str) -> None:
    """Print success message in green."""
    print(f"{Colors.GREEN}✓{Colors.RESET} {msg}")


def print_error(msg: str) -> None:
    """Print error message in red."""
    print(f"{Colors.RED}✗{Colors.RESET} {msg}", file=sys.stderr)


def print_info(msg: str) -> None:
    """Print info message in blue."""
    print(f"{Colors.BLUE}ℹ{Colors.RESET} {msg}")


def print_warning(msg: str) -> None:
    """Print warning message in yellow."""
    print(f"{Colors.YELLOW}⚠{Colors.RESET} {msg}")


def run_command(cmd: list[str], capture_output: bool = False, check: bool = True) -> subprocess.CompletedProcess:
    """
    Run a shell command with error handling.

    Args:
        cmd: Command as list of strings
        capture_output: If True, capture stdout/stderr
        check: If True, raise exception on non-zero exit

    Returns:
        CompletedProcess object
    """
    try:
        result = subprocess.run(
            cmd,
            capture_output=capture_output,
            text=True,
            check=check
        )
        return result
    except subprocess.CalledProcessError as e:
        print_error(f"Command failed: {' '.join(cmd)}")
        if e.stderr:
            print_error(f"Error: {e.stderr.strip()}")
        sys.exit(1)
    except FileNotFoundError:
        print_error(f"Command not found: {cmd[0]}")
        print_info("Make sure Docker is installed")
        sys.exit(1)


def get_compose_file_path() -> Path:
    """Get path to Valkey docker-compose file."""
    repo_root = Path(__file__).parent.parent
    compose_file = repo_root / "services" / "valkey" / "docker-compose.yml"

    if not compose_file.exists():
        print_error(f"Docker compose file not found: {compose_file}")
        print_info("Make sure you're running this script from the CRSBench repository")
        sys.exit(1)

    return compose_file


def docker_compose_cmd(compose_file: Path, *args: str) -> list[str]:
    """Build docker compose (v2 plugin) command with file path."""
    return ["docker", "compose", "-f", str(compose_file), *args]


def _get_env_file_path() -> Path:
    """Get path to the repository .env file."""
    return Path(__file__).parent.parent / ".env"


def _load_env_password() -> str | None:
    """Parse .env file for REDIS_PASSWORD value."""
    env_file = _get_env_file_path()
    if not env_file.exists():
        return None
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() == "REDIS_PASSWORD" and value.strip():
            return value.strip()
    return None


def _save_env_password(password: str) -> None:
    """Update or append REDIS_PASSWORD in .env, chmod 600."""
    env_file = _get_env_file_path()
    lines: list[str] = []
    found = False

    if env_file.exists():
        for line in env_file.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("REDIS_PASSWORD=") or (
                not stripped.startswith("#") and "REDIS_PASSWORD=" in stripped
            ):
                lines.append(f"REDIS_PASSWORD={password}")
                found = True
            else:
                lines.append(line)

    if not found:
        lines.append(f"REDIS_PASSWORD={password}")

    env_file.write_text("\n".join(lines) + "\n")
    env_file.chmod(0o600)


def _server_requires_password() -> bool:
    """Check if the running Valkey container was started with --requirepass."""
    try:
        result = subprocess.run(
            ["docker", "inspect", "crsbench-valkey", "--format", "{{join .Args \" \"}}"],
            capture_output=True, text=True, check=True,
        )
        return "--requirepass" in result.stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def _get_active_password() -> str | None:
    """Return password only if the running server actually requires it."""
    if not _server_requires_password():
        return None
    env_val = os.environ.get("REDIS_PASSWORD")
    if env_val:
        return env_val
    return _load_env_password()


def valkey_cli_cmd(container: str, *args: str, password: str | None = None) -> list[str]:
    """
    Build valkey-cli command via docker exec.

    Note: We use docker exec to access Valkey since ports are not exposed
    to the host by default for security reasons.
    """
    cmd = ["docker", "exec", container, "valkey-cli"]
    if password:
        cmd.extend(["-a", password])
    cmd.extend(args)
    return cmd


def is_valkey_running() -> bool:
    """Check if Valkey container is running."""
    try:
        result = subprocess.run(
            ["docker", "ps", "--filter", "name=crsbench-valkey", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            check=True
        )
        return "crsbench-valkey" in result.stdout
    except subprocess.CalledProcessError:
        return False


def cmd_start(args: argparse.Namespace) -> None:
    """Start Valkey service."""
    compose_file = get_compose_file_path()

    if is_valkey_running():
        print_warning("Valkey is already running")
        return

    password = None

    if args.password:
        # --password mode: bind 0.0.0.0, require auth
        password = _load_env_password() or secrets.token_urlsafe(24)
        bind_addr = "0.0.0.0:6379:6379"

        print_info("Starting Valkey service with password auth (0.0.0.0:6379)...")
        print_warning("Port 6379 will be accessible from ALL interfaces")

        # Stop any existing container first
        run_command(["docker", "stop", "crsbench-valkey"], check=False, capture_output=True)
        run_command(["docker", "rm", "crsbench-valkey"], check=False, capture_output=True)

        run_command([
            "docker", "run", "-d",
            "--name", "crsbench-valkey",
            "-p", bind_addr,
            "-v", "valkey_valkey-data:/data",
            "--restart", "unless-stopped",
            "valkey/valkey:8.0-alpine",
            "valkey-server", "--appendonly", "yes", "--requirepass", password,
        ])

        _save_env_password(password)

    else:
        # Default: bind to localhost (127.0.0.1:6379)
        print_info("Starting Valkey service (localhost:6379)...")

        # Create external volume if it doesn't exist (idempotent)
        run_command(
            ["docker", "volume", "create", "valkey_valkey-data"],
            check=False,
            capture_output=True,
        )

        # Clean up any stopped containers to avoid name conflicts
        run_command(["docker", "stop", "crsbench-valkey"], check=False, capture_output=True)
        run_command(["docker", "rm", "crsbench-valkey"], check=False, capture_output=True)

        run_command(docker_compose_cmd(compose_file, "up", "-d"))

    # Wait a moment and check status
    import time
    time.sleep(2)

    if is_valkey_running():
        print_success("Valkey service started")

        if args.password:
            print_success("Password auth: ENABLED")
            print_success("Host access enabled at 0.0.0.0:6379")
            print_info(f"Password saved to {_get_env_file_path()}")
            print_info("Copy .env to worker machines: scp .env user@worker:/path/to/CRSBench/.env")
        else:
            print_success("Host access enabled at localhost:6379")

        # Test connection
        try:
            result = run_command(
                valkey_cli_cmd("crsbench-valkey", "ping", password=password),
                capture_output=True,
            )
            if "PONG" in result.stdout:
                print_success("Connection test successful (PONG)")
        except Exception:
            print_warning("Service started but connection test failed")
    else:
        print_error("Failed to start Valkey service")
        sys.exit(1)


def cmd_stop(args: argparse.Namespace) -> None:
    """Stop Valkey service."""
    if not is_valkey_running():
        print_warning("Valkey is not running")
        return

    print_info("Stopping Valkey service...")

    # Try docker-compose first, fall back to docker stop
    compose_file = get_compose_file_path()
    try:
        subprocess.run(
            docker_compose_cmd(compose_file, "down"),
            capture_output=True, text=True, check=False,
        )
    except FileNotFoundError:
        pass  # docker-compose not installed, fall through to docker stop

    # If that didn't work, try stopping container directly
    if is_valkey_running():
        run_command(["docker", "stop", "crsbench-valkey"], check=False, capture_output=True)
        run_command(["docker", "rm", "crsbench-valkey"], check=False, capture_output=True)

    print_success("Valkey service stopped")


def cmd_restart(args: argparse.Namespace) -> None:
    """Restart Valkey service."""
    # If not explicitly requesting password mode, detect from .env
    if not args.password and _load_env_password():
        print_info("Detected existing password in .env, re-enabling password auth")
        args.password = True
    cmd_stop(args)
    cmd_start(args)


def cmd_status(args: argparse.Namespace) -> None:
    """Check Valkey service status."""
    if not is_valkey_running():
        print_error("Valkey is not running")
        print_info("Start it with: python scripts/valkey-helper.py start")
        sys.exit(1)

    password = _get_active_password()

    print_success("Valkey container is running")

    # Check port binding
    try:
        result = run_command(
            ["docker", "port", "crsbench-valkey"],
            capture_output=True,
            check=False,
        )
        if result.stdout.strip():
            print_success("Host access: ENABLED")
            print_info(f"Port binding: {result.stdout.strip()}")
        else:
            print_info("Host access: DISABLED (Docker network only)")
    except Exception:
        pass

    # Password auth status
    if password:
        print_success("Password auth: ENABLED")
    else:
        print_info("Password auth: DISABLED")

    # Test connection
    try:
        result = run_command(
            valkey_cli_cmd("crsbench-valkey", "ping", password=password),
            capture_output=True,
        )
        if "PONG" in result.stdout:
            print_success("Connection test: PONG")
        else:
            print_warning(f"Connection test: {result.stdout.strip()}")
    except Exception:
        print_error("Connection test failed")
        sys.exit(1)

    # Get info
    try:
        result = run_command(
            valkey_cli_cmd("crsbench-valkey", "info", "server", password=password),
            capture_output=True,
        )
        for line in result.stdout.split('\n'):
            if line.startswith('redis_version:') or line.startswith('valkey_version:'):
                version = line.split(':')[1].strip()
                print_info(f"Version: {version}")
                break
    except Exception:
        pass


def cmd_logs(args: argparse.Namespace) -> None:
    """View Valkey logs."""
    compose_file = get_compose_file_path()

    if not is_valkey_running():
        print_error("Valkey is not running")
        sys.exit(1)

    print_info("Streaming Valkey logs (Ctrl+C to exit)...")
    try:
        run_command(docker_compose_cmd(compose_file, "logs", "-f", "valkey"), check=False)
    except KeyboardInterrupt:
        print("\n")
        print_info("Stopped streaming logs")


def cmd_clean(args: argparse.Namespace) -> None:
    """Clean specific experiment queue."""
    if not is_valkey_running():
        print_error("Valkey is not running")
        sys.exit(1)

    password = _get_active_password()
    experiment = args.experiment
    queue_name = f"rq:queue:crsbench_{experiment}"

    print_info(f"Cleaning experiment queue: {experiment}")

    # Check if queue exists
    result = run_command(
        valkey_cli_cmd("crsbench-valkey", "EXISTS", queue_name, password=password),
        capture_output=True,
    )

    if result.stdout.strip() == "0":
        print_warning(f"Queue does not exist: {queue_name}")
        return

    # Delete all keys related to this experiment
    print_info(f"Deleting keys matching: rq:*crsbench_{experiment}*")

    # Get all matching keys
    result = run_command(
        valkey_cli_cmd("crsbench-valkey", "KEYS", f"rq:*crsbench_{experiment}*", password=password),
        capture_output=True,
    )

    keys = [k for k in result.stdout.strip().split('\n') if k]

    if not keys:
        print_warning("No keys found to delete")
        return

    print_info(f"Found {len(keys)} keys to delete")

    # Delete each key
    for key in keys:
        run_command(
            valkey_cli_cmd("crsbench-valkey", "DEL", key, password=password),
            capture_output=True,
        )

    print_success(f"Cleaned experiment queue: {experiment}")


def cmd_clean_all(args: argparse.Namespace) -> None:
    """Flush entire Valkey database (with confirmation)."""
    if not is_valkey_running():
        print_error("Valkey is not running")
        sys.exit(1)

    password = _get_active_password()

    print_warning("WARNING: This will delete ALL data in Valkey!")
    print_warning("This includes all CRSBench queues and any other data.")

    if not args.force:
        response = input(f"{Colors.YELLOW}Are you sure? Type 'yes' to confirm:{Colors.RESET} ")
        if response.lower() != "yes":
            print_info("Cancelled")
            return

    print_info("Flushing entire database...")
    run_command(
        valkey_cli_cmd("crsbench-valkey", "FLUSHDB", password=password),
        capture_output=True,
    )
    print_success("Database flushed")


def cmd_list_queues(args: argparse.Namespace) -> None:
    """List all CRSBench queues."""
    if not is_valkey_running():
        print_error("Valkey is not running")
        sys.exit(1)

    password = _get_active_password()

    print_info("CRSBench queues:")

    # Get all queue keys
    result = run_command(
        valkey_cli_cmd("crsbench-valkey", "KEYS", "rq:queue:crsbench_*", password=password),
        capture_output=True,
    )

    queues = [q for q in result.stdout.strip().split('\n') if q]

    if not queues:
        print_info("No queues found")
        return

    for queue in queues:
        # Get queue length
        result = run_command(
            valkey_cli_cmd("crsbench-valkey", "LLEN", queue, password=password),
            capture_output=True,
        )
        length = result.stdout.strip()

        # Extract experiment name
        exp_name = queue.replace("rq:queue:crsbench_", "")

        print(f"  • {exp_name}: {length} jobs")


def cmd_queue_info(args: argparse.Namespace) -> None:
    """Show detailed info about specific experiment queue."""
    if not is_valkey_running():
        print_error("Valkey is not running")
        sys.exit(1)

    password = _get_active_password()
    experiment = args.experiment
    queue_name = f"rq:queue:crsbench_{experiment}"

    print_info(f"Queue info for: {experiment}")
    print(f"Queue name: {queue_name}")

    # Check if exists
    result = run_command(
        valkey_cli_cmd("crsbench-valkey", "EXISTS", queue_name, password=password),
        capture_output=True,
    )

    if result.stdout.strip() == "0":
        print_warning("Queue does not exist")
        return

    # Get queue length
    result = run_command(
        valkey_cli_cmd("crsbench-valkey", "LLEN", queue_name, password=password),
        capture_output=True,
    )
    length = int(result.stdout.strip())
    print(f"Queued jobs: {length}")

    # Get all related keys
    result = run_command(
        valkey_cli_cmd("crsbench-valkey", "KEYS", f"rq:*crsbench_{experiment}*", password=password),
        capture_output=True,
    )

    keys = [k for k in result.stdout.strip().split('\n') if k]
    print(f"Total keys: {len(keys)}")

    # Categorize keys
    for key in keys:
        if "finished" in key:
            result = run_command(
                valkey_cli_cmd("crsbench-valkey", "SCARD", key, password=password),
                capture_output=True,
            )
            print(f"Finished jobs: {result.stdout.strip()}")
        elif "failed" in key:
            result = run_command(
                valkey_cli_cmd("crsbench-valkey", "SCARD", key, password=password),
                capture_output=True,
            )
            print(f"Failed jobs: {result.stdout.strip()}")


def cmd_stats(args: argparse.Namespace) -> None:
    """Show Valkey database statistics."""
    if not is_valkey_running():
        print_error("Valkey is not running")
        sys.exit(1)

    password = _get_active_password()

    print_info("Valkey database statistics:")

    # Database size
    result = run_command(
        valkey_cli_cmd("crsbench-valkey", "DBSIZE", password=password),
        capture_output=True,
    )
    dbsize = result.stdout.strip()
    print(f"Total keys: {dbsize}")

    # Memory usage
    result = run_command(
        valkey_cli_cmd("crsbench-valkey", "info", "memory", password=password),
        capture_output=True,
    )

    for line in result.stdout.split('\n'):
        if line.startswith('used_memory_human:'):
            memory = line.split(':')[1].strip()
            print(f"Memory usage: {memory}")
            break

    # Count CRSBench queues
    result = run_command(
        valkey_cli_cmd("crsbench-valkey", "KEYS", "rq:queue:crsbench_*", password=password),
        capture_output=True,
    )
    queues = [q for q in result.stdout.strip().split('\n') if q]
    print(f"CRSBench queues: {len(queues)}")


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Valkey service management helper for CRSBench",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s start              Start Valkey (localhost:6379)
  %(prog)s --password start   Start with password auth (0.0.0.0:6379)
  %(prog)s status             Check if running
  %(prog)s clean my-exp       Clean specific experiment
  %(prog)s clean-all          Flush entire database
  %(prog)s list-queues        Show all queues
        """
    )

    # Add global flags
    parser.add_argument('--password', action='store_true',
                        help='Enable password auth (auto-generates, binds 0.0.0.0, saves to .env)')

    subparsers = parser.add_subparsers(dest='command', required=True, help='Command to run')

    # Service management commands
    subparsers.add_parser('start', help='Start Valkey service')
    subparsers.add_parser('stop', help='Stop Valkey service')
    subparsers.add_parser('restart', help='Restart Valkey service')

    subparsers.add_parser('status', help='Check service status')
    subparsers.add_parser('logs', help='View service logs')

    # Queue management commands
    clean_parser = subparsers.add_parser('clean', help='Clean specific experiment queue')
    clean_parser.add_argument('experiment', help='Experiment name')

    clean_all_parser = subparsers.add_parser('clean-all', help='Flush entire database (WARNING: destructive)')
    clean_all_parser.add_argument('--force', action='store_true', help='Skip confirmation prompt')

    subparsers.add_parser('list-queues', help='List all CRSBench queues')

    queue_info_parser = subparsers.add_parser('queue-info', help='Show detailed queue info')
    queue_info_parser.add_argument('experiment', help='Experiment name')

    # Monitoring commands
    subparsers.add_parser('stats', help='Show database statistics')

    args = parser.parse_args()

    # Route to command handlers
    commands = {
        'start': cmd_start,
        'stop': cmd_stop,
        'restart': cmd_restart,
        'status': cmd_status,
        'logs': cmd_logs,
        'clean': cmd_clean,
        'clean-all': cmd_clean_all,
        'list-queues': cmd_list_queues,
        'queue-info': cmd_queue_info,
        'stats': cmd_stats,
    }

    handler = commands.get(args.command)
    if handler:
        handler(args)
    else:
        print_error(f"Unknown command: {args.command}")
        sys.exit(1)


if __name__ == "__main__":
    main()
