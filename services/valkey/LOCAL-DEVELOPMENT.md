# Local Development Setup - Valkey Host Access

This guide explains how to enable host access to Valkey for local development and testing scenarios where workers or scripts run on the host machine (not inside Docker containers).

## Why Enable Host Access?

By default, Valkey ports are NOT exposed to the host for security. However, for local development you may need host access when:

- Running CRSBench workers directly on your host machine
- Running experiments from your host (not in Docker)
- Using `valkey-cli` from your host system
- Testing or debugging locally

## Quick Enable: Uncomment Ports in docker-compose.yml

### Step 1: Edit docker-compose.yml

Open `services/valkey/docker-compose.yml` and uncomment the ports section:

**Before:**
```yaml
services:
  valkey:
    image: valkey/valkey:8.0-alpine
    container_name: crsbench-valkey
    # NOTE: Ports are NOT exposed to host by default for security
    # Uncomment the line below ONLY for local development if you need direct access
    # ports:
    #   - "127.0.0.1:6379:6379"  # Bind to localhost only
    expose:
      - "6379"  # Expose to other Docker containers only
```

**After:**
```yaml
services:
  valkey:
    image: valkey/valkey:8.0-alpine
    container_name: crsbench-valkey
    # NOTE: Ports are NOT exposed to host by default for security
    # Uncomment the line below ONLY for local development if you need direct access
    ports:
      - "127.0.0.1:6379:6379"  # Bind to localhost only
    expose:
      - "6379"  # Expose to other Docker containers only
```

### Step 2: Restart Valkey

```bash
# Using helper script
python scripts/valkey-helper.py restart

# Or manually
docker-compose -f services/valkey/docker-compose.yml down
docker-compose -f services/valkey/docker-compose.yml up -d
```

### Step 3: Verify Host Access

```bash
# Install valkey-cli on host if needed
# Ubuntu/Debian: sudo apt install redis-tools
# Arch: sudo pacman -S redis (includes redis-cli, compatible with Valkey)
# macOS: brew install redis

# Test connection from host
redis-cli -h localhost -p 6379 ping
# Expected: PONG

# Or using Docker (works without valkey-cli on host)
docker exec crsbench-valkey valkey-cli ping
```

## Using Environment File (.env)

For easier configuration management, you can use a `.env` file:

### Step 1: Copy .env.example

```bash
cd services/valkey
cp .env.example .env
```

### Step 2: Edit .env (Optional)

```bash
# .env
VALKEY_PORT=6379

# Uncomment to enable authentication (recommended for production)
# VALKEY_PASSWORD=your_secure_password
```

### Step 3: Update docker-compose.yml to Use .env

If you want to make port binding configurable via environment:

```yaml
services:
  valkey:
    image: valkey/valkey:8.0-alpine
    container_name: crsbench-valkey
    ports:
      - "127.0.0.1:${VALKEY_PORT:-6379}:6379"
    # ... rest of config
```

## Running Workers on Host

Once you've enabled host access, you can run workers directly on your host:

### Single Worker

```bash
# Set environment variables
export REDIS_HOST=localhost
export EXPERIMENT_NAME=my-experiment

# Start worker
python -m crsbench.distributed.worker
```

### Multiple Workers

```bash
# Terminal 1
export REDIS_HOST=localhost
export EXPERIMENT_NAME=my-experiment
python -m crsbench.distributed.worker

# Terminal 2
export REDIS_HOST=localhost
export EXPERIMENT_NAME=my-experiment
python -m crsbench.distributed.worker

# Or run in background
for i in {1..4}; do
  REDIS_HOST=localhost EXPERIMENT_NAME=my-experiment \
    python -m crsbench.distributed.worker &
done
```

### Run Experiment from Host

```bash
# benchmarks, crses, and other settings are configured in config.yaml
crsbench run \
  --experiment-config config.yaml \
  --experiment-name my-experiment
```

The experiment will automatically connect to Valkey at `localhost:6379`.

## Security Considerations

### ✅ Secure: Bind to localhost (127.0.0.1)

```yaml
ports:
  - "127.0.0.1:6379:6379"  # Only accessible from this machine
```

**Why secure:**
- Only accessible from the local machine
- NOT accessible from network or internet
- Suitable for local development

### ⚠️ Bind to all interfaces (0.0.0.0) — requires password

```yaml
ports:
  - "6379:6379"  # Accessible from network — must use password auth!
```

**Without password:** Insecure — anyone on the network can connect. Never do this.

**With password:** Required for remote workers. Use `--password` flag:
```bash
python scripts/valkey-helper.py --password start
# Auto-generates password, binds 0.0.0.0, saves REDIS_PASSWORD to .env
```

### Adding Authentication

For multi-machine setups or shared environments, use the helper script:

```bash
# Automatically generates password, starts with --requirepass, saves to .env
python scripts/valkey-helper.py --password start

# Copy .env to worker machines
scp .env user@worker:/path/to/CRSBench/.env
```

Or manually:

```bash
# Set password in .env
echo "REDIS_PASSWORD=your_secure_password" >> .env

# Connect with authentication
redis-cli -h localhost -p 6379 -a your_secure_password ping

# Workers read REDIS_PASSWORD from .env automatically
python -m crsbench.distributed.worker
```

## Checking Port Binding

Verify that Valkey is correctly bound to localhost only:

```bash
# Check what's listening on port 6379
netstat -an | grep 6379
# Should show: 127.0.0.1:6379 (localhost only)

# Or using ss
ss -tlnp | grep 6379

# Or using lsof
lsof -i :6379
```

**Expected output:**
```
127.0.0.1:6379  # ✅ Correct - localhost only
0.0.0.0:6379    # ❌ Wrong - all interfaces (insecure)
```

## Disabling Host Access (Back to Secure Default)

When you're done with local development and want to return to the secure default:

### Step 1: Comment out ports

Edit `services/valkey/docker-compose.yml`:

```yaml
services:
  valkey:
    # ports:
    #   - "127.0.0.1:6379:6379"
    expose:
      - "6379"
```

### Step 2: Restart Valkey in Secure Mode

```bash
python scripts/valkey-helper.py restart
```

### Step 3: Verify port is not exposed

```bash
netstat -an | grep 6379
# Should show NO output (or only Docker internal)
```

## Alternative: Run Workers in Docker Network

Instead of enabling host access, you can run workers in the Docker network:

### Option 1: Docker Compose for Workers

Create `docker-compose.worker.yml`:

```yaml
version: "3.8"

services:
  worker:
    build:
      context: ../..
      dockerfile: Dockerfile  # Your CRSBench Dockerfile
    depends_on:
      - valkey
    environment:
      - REDIS_HOST=crsbench-valkey
      - EXPERIMENT_NAME=${EXPERIMENT_NAME:-default}
    command: python -m crsbench.distributed.worker
    deploy:
      replicas: 2

networks:
  default:
    external:
      name: valkey_default  # Connect to Valkey's network
```

Start workers:

```bash
EXPERIMENT_NAME=my-exp docker-compose -f docker-compose.worker.yml up
```

### Option 2: Docker Run with Network

```bash
# Run worker container on same network
docker run --rm \
  --network valkey_default \
  -e REDIS_HOST=crsbench-valkey \
  -e EXPERIMENT_NAME=my-exp \
  crsbench:latest \
  python -m crsbench.distributed.worker
```

This way, workers access Valkey via Docker network without exposing ports to host.

## Troubleshooting

### Problem: Connection refused from host

**Cause**: Ports not exposed or bound to wrong interface

**Solution:**
1. Check docker-compose.yml has uncommented ports
2. Verify binding to `127.0.0.1:6379:6379`
3. Restart Valkey: `python scripts/valkey-helper.py restart`
4. Check port binding: `netstat -an | grep 6379`

### Problem: Connection works from Docker but not host

**Cause**: Ports exposed to Docker network but not host

**Solution:** Uncomment the `ports:` section in docker-compose.yml

### Problem: Workers can't connect (ECONNREFUSED)

**Solution:**
```bash
# Check REDIS_HOST environment variable
echo $REDIS_HOST  # Should be 'localhost' for host workers

# Check Valkey is running
python scripts/valkey-helper.py status

# Test connection
redis-cli -h localhost -p 6379 ping
```

### Problem: "Address already in use" error

**Cause**: Another service (Redis, Valkey) is using port 6379

**Solution:**
```bash
# Find what's using the port
lsof -i :6379

# Stop the conflicting service
sudo systemctl stop redis
# or
sudo systemctl stop valkey

# Then restart CRSBench Valkey
python scripts/valkey-helper.py restart
```

## Best Practices

1. **Development**: Use `--bind-host` (localhost binding) for single-machine dev
2. **Testing**: Use localhost binding for integration tests
3. **Multi-machine**: Use `--password` (binds 0.0.0.0 with auth) for remote workers
4. **Production**: Use Docker network with authentication
5. **Never**: Bind to `0.0.0.0` without password authentication
6. **Cleanup**: Disable port exposure when not actively developing

## Summary

**For local development with host-based workers:**

1. Uncomment `ports: - "127.0.0.1:6379:6379"` in `services/valkey/docker-compose.yml`
2. Restart Valkey: `python scripts/valkey-helper.py restart`
3. Set `REDIS_HOST=localhost` when running workers
4. Run your experiments and workers from host

**Security checklist:**
- ✅ Use `--bind-host` for localhost-only access
- ✅ Use `--password` when binding to `0.0.0.0` (remote workers)
- ✅ Never bind to `0.0.0.0` without password auth
- ✅ Disable port exposure when not needed
- ✅ Use firewall rules if exposing beyond localhost

See also:
- [Valkey Service README](README.md) - Main documentation
- [Experiment Workflow](../../docs/experiment-workflow.md) - Full guide
- [Testing Setup Guide](../../docs/testing-setup.md) - Development environment
