#!/usr/bin/env bash
# =============================================================================
# Remote Worker Setup Script
# =============================================================================
# Sets up a remote machine to run as a CRSBench distributed worker.
#
# Usage:
#   # On the remote machine (cerebros or ramjet):
#   bash setup-remote-worker.sh <machine-name>
#
#   # Examples:
#   bash setup-remote-worker.sh cerebros
#   bash setup-remote-worker.sh ramjet
#
# Or from cyclonus via SSH:
#   ssh cerebros.gtisc.gatech.edu 'bash -s cerebros' < scripts/setup-remote-worker.sh
# =============================================================================

set -euo pipefail

MACHINE_NAME="${1:?Usage: $0 <machine-name> (cerebros|ramjet)}"
REPO_URL="git@github.com:sslab-gatech/CRSBench.git"
BRANCH="feat/distributed"
INSTALL_DIR="$HOME/CRSBench"
REDIS_HOST="cyclonus.gtisc.gatech.edu"

echo "============================================="
echo " CRSBench Remote Worker Setup: $MACHINE_NAME"
echo "============================================="

# ----- Step 1: Check prerequisites -----
echo ""
echo "[1/9] Checking prerequisites..."

if ! command -v git &>/dev/null; then
    echo "ERROR: git not found. Install git first."
    exit 1
fi

if ! command -v docker &>/dev/null; then
    echo "ERROR: docker not found. Install Docker first."
    exit 1
fi

if ! docker info &>/dev/null; then
    echo "ERROR: Docker daemon not running or current user lacks permission."
    echo "  Try: sudo usermod -aG docker $USER && newgrp docker"
    exit 1
fi

echo "  git:    $(git --version)"
echo "  docker: $(docker --version)"

# ----- Step 2: Clone or update repo -----
echo ""
echo "[2/9] Setting up CRSBench repo..."

if [ -d "$INSTALL_DIR/.git" ]; then
    echo "  Repo exists at $INSTALL_DIR, updating..."
    cd "$INSTALL_DIR"
    git fetch origin
    git checkout "$BRANCH"
    git pull origin "$BRANCH"
else
    echo "  Cloning to $INSTALL_DIR..."
    git clone "$REPO_URL" "$INSTALL_DIR"
    cd "$INSTALL_DIR"
    git checkout "$BRANCH"
fi

# ----- Step 3: Configure git for SSH-based submodule access -----
echo ""
echo "[3/9] Configuring git SSH access for submodules..."

# Submodules are configured with HTTPS URLs but the repos may be private.
# Rewrite HTTPS GitHub URLs to SSH so we can use SSH keys instead of PAT.
if ! git config --global --get url."git@github.com:".insteadOf &>/dev/null; then
    git config --global url."git@github.com:".insteadOf "https://github.com/"
    echo "  Configured HTTPS→SSH URL rewrite for github.com"
else
    echo "  HTTPS→SSH URL rewrite already configured"
fi

# Verify SSH access to GitHub
if ssh -T git@github.com 2>&1 | grep -q "successfully authenticated"; then
    echo "  GitHub SSH access: OK"
else
    echo "WARNING: GitHub SSH access may not be configured."
    echo "  Submodule init may fail. To fix:"
    echo "    1. Generate key: ssh-keygen -t ed25519 -C \"$MACHINE_NAME@gatech\""
    echo "    2. Add to GitHub: https://github.com/settings/keys"
    echo "    3. Test: ssh -T git@github.com"
fi

# ----- Step 4: Init submodules -----
echo ""
echo "[4/9] Initializing submodules..."
git submodule update --init --recursive
echo "  Submodules initialized"

# ----- Step 5: Install uv and Python dependencies -----
echo ""
echo "[5/9] Installing Python environment..."

if ! command -v uv &>/dev/null; then
    echo "  Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi

echo "  uv: $(uv --version)"
echo "  Installing CRSBench dependencies..."
uv sync --all-extras
echo "  Installing CRSBench in editable mode..."
uv pip install -e .

# ----- Step 6: Bundle benchmarks -----
echo ""
echo "[6/9] Bundling benchmarks..."
uv run crsbench benchmark bundle-all --force --workers 20 benchmarks/
echo "  Benchmarks bundled"

# ----- Step 7: Prepare CRS Docker images -----
echo ""
echo "[7/9] Preparing CRS (atlantis-multilang-given_fuzzer)..."
uv run oss-bugfind-crs prepare atlantis-multilang-given_fuzzer
echo "  CRS prepared"

# ----- Step 8: Verify config file exists -----
echo ""
echo "[8/9] Checking experiment config..."

CONFIG_FILE="$INSTALL_DIR/experiment-configs/experiment-config-afc-${MACHINE_NAME}.yaml"
if [ -f "$CONFIG_FILE" ]; then
    echo "  Config found: $CONFIG_FILE"
else
    echo "ERROR: Config file not found: $CONFIG_FILE"
    echo "  Expected experiment-config-afc-${MACHINE_NAME}.yaml in experiment-configs/"
    exit 1
fi

# ----- Step 9: Test Redis connectivity -----
echo ""
echo "[9/9] Testing Redis connectivity..."

if uv run python -c "
import redis, os
r = redis.Redis(host='$REDIS_HOST', password=os.environ.get('REDIS_PASSWORD'), socket_connect_timeout=5)
r.ping()
print('  Redis connection OK: $REDIS_HOST')
" 2>/dev/null; then
    :
else
    echo "WARNING: Cannot reach Redis at $REDIS_HOST"
    echo "  Make sure Redis is running on cyclonus and is network-accessible."
    echo "  Check: redis-cli -h $REDIS_HOST ping"
fi

# ----- Done -----
echo ""
echo "============================================="
echo " Setup complete for $MACHINE_NAME!"
echo "============================================="
echo ""
echo "To start the worker:"
echo "  cd $INSTALL_DIR"
echo "  uv run crsbench worker \\"
echo "    --experiment-config $CONFIG_FILE"
echo ""
echo "To run in background with tmux:"
echo "  tmux new-session -d -s crsbench-worker \\"
echo "    'cd $INSTALL_DIR && uv run crsbench worker --experiment-config $CONFIG_FILE'"
echo ""
echo "To monitor:"
echo "  tmux attach -t crsbench-worker"
