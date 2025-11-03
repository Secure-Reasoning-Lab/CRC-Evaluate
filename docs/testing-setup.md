# Testing Environment Setup Guide

This guide helps you set up a local testing environment for CRSBench development and testing.

## Overview

A complete testing environment for CRSBench includes:
- Python environment with dependencies
- Docker for running CRS containers
- Valkey for distributed execution testing
- Test benchmarks

## Prerequisites

### Required Software

- **Python 3.11+**: For running CRSBench
- **uv**: Python package manager
- **Docker**: For CRS execution and Valkey service
- **Git**: For cloning repositories

**Install on Ubuntu/Debian:**
```bash
# Python and pip
sudo apt update
sudo apt install python3.11 python3-pip git

# Docker
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh
sudo usermod -aG docker $USER  # Add user to docker group

# uv (Python package manager)
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**Install on Arch Linux:**
```bash
# Python and Docker
sudo pacman -S python docker git

# uv
curl -LsSf https://astral.sh/uv/install.sh | sh

# Enable Docker service
sudo systemctl enable docker
sudo systemctl start docker
sudo usermod -aG docker $USER
```

**Install on macOS:**
```bash
# Using Homebrew
brew install python@3.11 docker git

# uv
curl -LsSf https://astral.sh/uv/install.sh | sh
```

## Quick Setup

### 1. Clone Repository

```bash
git clone https://github.com/anthropics/CRSBench.git
cd CRSBench
```

### 2. Install CRSBench

```bash
# Create virtual environment and install dependencies
uv venv
source .venv/bin/activate  # On macOS/Linux
# .venv\Scripts\activate   # On Windows

# Install CRSBench in editable mode
uv pip install -e .

# Verify installation
crsbench --help
```

### 3. Start Valkey Service

```bash
# Quick start with helper script
python scripts/valkey-helper.py start

# Verify it's running
python scripts/valkey-helper.py status
```

### 4. Run a Test Experiment

```bash
# Run a simple local test (no distributed execution)
crsbench \
  --experiment-config example-config.yaml \
  --experiment-name test-local \
  --benchmarks test-benchmark \
  --crses example-crs

# Or test distributed execution
python -m crsbench.distributed.worker &  # Start worker in background
crsbench \
  --experiment-config distributed-config.yaml \
  --experiment-name test-distributed \
  --benchmarks test-benchmark \
  --crses example-crs
```

## Detailed Setup

### Python Environment Setup

**1. Create Virtual Environment:**
```bash
cd CRSBench
uv venv
source .venv/bin/activate
```

**2. Install Dependencies:**
```bash
# Install CRSBench with all dependencies
uv pip install -e .

# Install optional dependencies for development
uv pip install pytest pytest-cov ruff mypy

# Install distributed execution dependencies
uv pip install redis rq
```

**3. Verify Installation:**
```bash
# Check CRSBench command
crsbench --help

# Check Python imports
python -c "import crsbench; print(crsbench.__version__)"

# Run tests
uv run pytest tests/ -v
```

### Docker Setup

**1. Install Docker:**

See [Prerequisites](#required-software) for installation instructions.

**2. Verify Docker:**
```bash
# Check Docker is running
docker --version
docker ps

# Pull test images
docker pull python:3.11-slim
```

**3. Configure Docker (Optional):**

For better performance on Linux:
```bash
# Enable BuildKit
echo '{"features": {"buildkit": true}}' | sudo tee /etc/docker/daemon.json
sudo systemctl restart docker
```

### Valkey Setup for Distributed Testing

**Option 1: Using Helper Script (Recommended)**

```bash
# Start Valkey
python scripts/valkey-helper.py start

# Check status
python scripts/valkey-helper.py status

# View logs if needed
python scripts/valkey-helper.py logs
```

**Option 2: Manual Docker Compose**

```bash
# Start Valkey service
docker-compose -f services/valkey/docker-compose.yml up -d

# Verify (via docker exec, ports not exposed by default for security)
docker exec crsbench-valkey valkey-cli ping
```

**Security Note**: By default, Valkey ports are **NOT exposed** to the host. The service is only accessible within the Docker network. This is intentional for security. The helper script uses `docker exec` to interact with Valkey, which works without exposed ports.

**Option 3: System Service (Arch Linux)**

```bash
# Install Valkey
sudo pacman -S valkey

# Start service
sudo systemctl start valkey
sudo systemctl enable valkey

# Test connection
valkey-cli ping
```

See [Distributed Execution Guide](distributed-execution.md) for complete Valkey documentation.

## Testing Workflows

### Workflow 1: Quick Local Test

Test without distributed execution:

```bash
# No Valkey needed for local mode
crsbench \
  --experiment-config config.yaml \
  --experiment-name quick-test \
  --benchmarks test-benchmark \
  --crses test-crs
```

### Workflow 2: Distributed Execution Test

Test with workers and queue:

```bash
# 1. Start Valkey with host access
python scripts/valkey-helper.py start --bind-host

# 2. Start workers on host
export REDIS_HOST=localhost
export EXPERIMENT_NAME=dist-test

python -m crsbench.distributed.worker &
python -m crsbench.distributed.worker &

# 3. Run experiment
crsbench \
  --experiment-config distributed-config.yaml \
  --experiment-name dist-test \
  --benchmarks bench1,bench2 \
  --crses crs1,crs2

# 4. Clean up
python scripts/valkey-helper.py clean dist-test
```

**Note**: The `--bind-host` flag binds Valkey to `localhost:6379` (127.0.0.1 only), which is secure for local development.

### Workflow 3: Development Testing Loop

Rapid iteration during development:

```bash
# 1. Start Valkey once (with host access if testing distributed features)
python scripts/valkey-helper.py start --bind-host

# 2. Run tests repeatedly
uv run pytest tests/test_myfeature.py -v

# Make code changes...

# 3. Re-run tests
uv run pytest tests/test_myfeature.py -v

# 4. Test with real experiment
export REDIS_HOST=localhost
crsbench --experiment-name dev-test ...

# 5. Clean up between runs
python scripts/valkey-helper.py clean dev-test

# Or full reset
python scripts/valkey-helper.py clean-all
```

### Workflow 4: Integration Testing

Full end-to-end testing:

```bash
# 1. Clean environment
python scripts/valkey-helper.py clean-all

# 2. Start fresh
python scripts/valkey-helper.py restart

# 3. Run integration tests
uv run pytest tests/test_integration.py -v

# 4. Run real experiment
crsbench --experiment-config full-config.yaml ...

# 5. Verify results
python scripts/valkey-helper.py stats
python scripts/valkey-helper.py list-queues
```

## Common Testing Scenarios

### Testing a New CRS

```bash
# 1. Add CRS configuration
mkdir -p example_configs/my-new-crs
# ... create crs.yaml

# 2. Test build
oss-crs build example_configs/my-new-crs test-benchmark

# 3. Test run
oss-crs run example_configs/my-new-crs test-benchmark test-harness

# 4. Test with CRSBench
crsbench \
  --experiment-name test-new-crs \
  --crses my-new-crs \
  --benchmarks test-benchmark
```

### Testing Benchmark Changes

```bash
# 1. Make changes to benchmark
# Edit benchmarks/my-benchmark/...

# 2. Validate benchmark metadata
python -m crsbench.validation.validate_benchmark benchmarks/my-benchmark

# 3. Test with CRS
crsbench \
  --experiment-name test-benchmark \
  --benchmarks my-benchmark \
  --crses test-crs
```

### Testing Distributed Execution Changes

```bash
# 1. Start Valkey
python scripts/valkey-helper.py start

# 2. Start worker with debugging
REDIS_HOST=localhost EXPERIMENT_NAME=debug-test \
  python -m crsbench.distributed.worker

# 3. In another terminal, run experiment
crsbench \
  --experiment-config config.yaml \
  --experiment-name debug-test \
  --benchmarks test-benchmark

# 4. Monitor queue
python scripts/valkey-helper.py queue-info debug-test
python scripts/valkey-helper.py logs
```

## Troubleshooting

### Python Environment Issues

**Problem**: `crsbench` command not found

```bash
# Ensure virtual environment is activated
source .venv/bin/activate

# Reinstall in editable mode
uv pip install -e .
```

**Problem**: Import errors or missing dependencies

```bash
# Reinstall all dependencies
uv pip install -e .

# Or force reinstall
uv pip install --force-reinstall -e .
```

### Docker Issues

**Problem**: Permission denied errors

```bash
# Add user to docker group
sudo usermod -aG docker $USER

# Log out and log back in, or
newgrp docker
```

**Problem**: Docker containers not starting

```bash
# Check Docker service
sudo systemctl status docker

# Restart Docker
sudo systemctl restart docker

# Check Docker logs
docker logs <container-name>
```

### Valkey Issues

**Problem**: Valkey won't start

```bash
# Check if port is already in use
netstat -an | grep 6379
lsof -i :6379

# Stop any existing instance
python scripts/valkey-helper.py stop

# Remove old container if needed
docker stop crsbench-valkey
docker rm crsbench-valkey

# Start fresh
python scripts/valkey-helper.py start
```

**Problem**: Workers can't connect to Valkey

```bash
# Check Valkey is running
python scripts/valkey-helper.py status

# Test connection
docker exec crsbench-valkey valkey-cli ping

# Check REDIS_HOST environment variable
echo $REDIS_HOST  # Should be 'localhost' or Valkey hostname
```

**Problem**: Queue issues or stale jobs

```bash
# Check queue status
python scripts/valkey-helper.py list-queues
python scripts/valkey-helper.py queue-info my-experiment

# Clean specific experiment
python scripts/valkey-helper.py clean my-experiment

# Full reset
python scripts/valkey-helper.py clean-all
```

### Test Failures

**Problem**: Tests fail with "Valkey not available"

```bash
# Start Valkey for tests
python scripts/valkey-helper.py start

# Or skip distributed tests
uv run pytest tests/ -v -m "not distributed"
```

**Problem**: Docker tests timeout

```bash
# Increase timeout in test configuration
# Or run with more verbose output
uv run pytest tests/ -v -s
```

## Environment Variables

Useful environment variables for testing:

```bash
# Distributed execution
export REDIS_HOST=localhost        # Valkey hostname
export EXPERIMENT_NAME=test-exp    # Experiment name for workers

# CRSBench paths
export OSS_FUZZ_HOME=/path/to/oss-fuzz
export CRSBENCH_DATA=/tmp/crsbench-data

# Docker
export DOCKER_BUILDKIT=1           # Enable BuildKit

# Logging
export LOG_LEVEL=DEBUG             # More verbose logs
```

## Testing Checklist

Before submitting changes, verify:

- [ ] Virtual environment activated
- [ ] All dependencies installed (`uv pip install -e .`)
- [ ] Unit tests pass (`uv run pytest tests/ -v`)
- [ ] Valkey tests pass (start Valkey first)
- [ ] Integration tests pass
- [ ] Manual experiment runs successfully
- [ ] No stale queues (`python scripts/valkey-helper.py list-queues`)
- [ ] Docker containers cleaned up (`docker ps`)

## Performance Tips

### For Faster Testing

1. **Use local mode when possible**: Skip Valkey for single-job tests
2. **Limit trials**: Use `trials: 1` in test configs
3. **Use small benchmarks**: Test with minimal benchmarks first
4. **Parallel tests**: Run pytest with `-n auto` (requires pytest-xdist)

```bash
# Install pytest-xdist
uv pip install pytest-xdist

# Run tests in parallel
uv run pytest tests/ -v -n auto
```

### For Distributed Testing

1. **Adjust worker count**: Match to CPU cores (1 worker per 2-4 cores)
2. **Monitor resources**: Use `htop` or `docker stats`
3. **Clean between runs**: Use helper script to avoid stale data

```bash
# Monitor Docker resource usage
docker stats

# Monitor Valkey usage
python scripts/valkey-helper.py stats
```

## See Also

- [Distributed Execution Guide](distributed-execution.md) - Full distributed setup
- [Scripts README](../scripts/README.md) - Helper script documentation
- [Valkey Service README](../services/valkey/README.md) - Valkey configuration
- [Contributing Guide](../CONTRIBUTING.md) - Development workflow

## Quick Reference

**Essential Commands:**
```bash
# Environment
source .venv/bin/activate
uv pip install -e .

# Valkey
python scripts/valkey-helper.py start
python scripts/valkey-helper.py status
python scripts/valkey-helper.py clean <experiment>

# Testing
uv run pytest tests/ -v
crsbench --experiment-name test ...

# Cleanup
python scripts/valkey-helper.py clean-all
docker system prune -f
```
