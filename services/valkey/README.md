# Valkey Service for CRSBench

This directory contains Docker Compose configuration for running Valkey, a Redis-compatible data store used by CRSBench's distributed execution system.

## What is Valkey?

Valkey is an open-source, high-performance key/value datastore that is fully compatible with the Redis protocol. It serves as the job queue backend for CRSBench's distributed worker system.

## Quick Start

### Using Helper Script (Recommended)

The easiest way to manage Valkey is using the helper script:

**For Docker network only (secure default):**
```bash
# Start Valkey (no host access)
python scripts/valkey-helper.py start

# Check status
python scripts/valkey-helper.py status
```

**For host access (running workers on same machine):**
```bash
# Start with host binding (localhost:6379)
python scripts/valkey-helper.py --bind-host start

# Now you can run workers on host
export CRSBENCH_REDIS_HOST=localhost
python -m crsbench.distributed.worker
```

**For remote workers (password auth):**
```bash
# Start with password auth (binds 0.0.0.0, auto-generates password, saves to .env)
python scripts/valkey-helper.py --password start

# Copy .env to worker machines
scp .env user@worker:/path/to/CRSBench/.env
```

**Other commands:**
```bash
# Check status (shows port binding and password auth)
python scripts/valkey-helper.py status

# View logs
python scripts/valkey-helper.py logs

# Stop / restart (restart auto-detects password from .env)
python scripts/valkey-helper.py stop
python scripts/valkey-helper.py restart
```

See [scripts/README.md](../../scripts/README.md) for complete helper script documentation.

### Manual Docker Compose

### Start Valkey Service

```bash
# From the services/valkey directory
docker-compose up -d

# Or from the project root
docker-compose -f services/valkey/docker-compose.yml up -d
```

### Verify Valkey is Running

```bash
# Check container status
docker-compose ps

# Test connection
docker exec crsbench-valkey valkey-cli ping
# Expected output: PONG

# Or using valkey-cli directly (if installed locally)
valkey-cli ping
```

### View Logs

```bash
docker-compose logs -f valkey
```

### Stop Valkey Service

```bash
docker-compose down

# Stop and remove volumes (WARNING: deletes all data)
docker-compose down -v
```

## Configuration

### Network Security

**Important**: By default, Valkey ports are **NOT exposed** to the host machine for security reasons. The service is only accessible to other Docker containers via the `expose` directive.

If you need direct host access for development (e.g., using `valkey-cli` from your host), you can uncomment the `ports` section in `docker-compose.yml`:

```yaml
ports:
  - "127.0.0.1:6379:6379"  # Bind to localhost only
```

**Security Note**: Always bind to `127.0.0.1` (localhost) rather than `0.0.0.0` to prevent external access.

### Ports

- **6379**: Valkey server port (exposed to Docker network only by default)
- Can be bound to `127.0.0.1:6379` for local development if needed

### Volumes

- **valkey-data**: Persistent data storage

Data persists across container restarts. To completely remove data, use `docker-compose down -v`.

### Persistence

Valkey is configured with AOF (Append-Only File) persistence enabled via `--appendonly yes`. This ensures data durability across restarts.

## Integration with CRSBench

### Connection Configuration

Set the Redis host in your CRSBench experiment configuration:

```yaml
# experiment-config.yaml
redis_host: localhost  # For local Docker
# or
redis_host: valkey     # For docker-compose network
```

### Environment Variables for Workers

```bash
export CRSBENCH_REDIS_HOST=localhost
export EXPERIMENT_NAME=my-experiment

# Start worker
python -m crsbench.distributed.worker
```

## Management

### Management via Helper Script (Recommended)

```bash
# Clean specific experiment
python scripts/valkey-helper.py clean my-experiment

# List all queues
python scripts/valkey-helper.py list-queues

# Get queue details
python scripts/valkey-helper.py queue-info my-experiment

# View statistics
python scripts/valkey-helper.py stats

# Full cleanup (with confirmation)
python scripts/valkey-helper.py clean-all
```

### Manual Valkey CLI Commands

For advanced users who need direct access:

**Clean Up Experiment Data:**
```bash
# Connect to Valkey
docker exec -it crsbench-valkey valkey-cli

# Delete specific experiment queue
> DEL rq:queue:crsbench_my-experiment
> exit

# Or clean all CRSBench queues
docker exec crsbench-valkey valkey-cli KEYS "rq:*crsbench_*" | xargs docker exec crsbench-valkey valkey-cli DEL
```

**Flush All Data (Use with Caution!):**
```bash
# Flush current database
docker exec crsbench-valkey valkey-cli FLUSHDB

# Flush ALL databases
docker exec crsbench-valkey valkey-cli FLUSHALL
```

**Check Queue Status:**
```bash
# Enter Valkey CLI
docker exec -it crsbench-valkey valkey-cli

# Check queue length
> LLEN rq:queue:crsbench_my-exp

# List all queues
> KEYS rq:queue:*

# Check database size
> DBSIZE

# Exit
> exit
```

## Troubleshooting

### Connection Issues

```bash
# Check if container is running
docker ps | grep valkey

# Check container logs
docker-compose logs valkey

# Test connection
docker exec crsbench-valkey valkey-cli ping

# Check port binding
netstat -an | grep 6379
```

### Reset Everything

```bash
# Stop, remove, and clean up
docker-compose down -v
docker-compose up -d
```

## Advanced Usage

### Custom Configuration

To use a custom Valkey configuration file:

1. Create `valkey.conf` in this directory
2. Update docker-compose.yml:
   ```yaml
   volumes:
     - ./valkey.conf:/etc/valkey/valkey.conf:ro
     - valkey-data:/data
   command: valkey-server /etc/valkey/valkey.conf
   ```

### Resource Limits

Add resource constraints in docker-compose.yml:

```yaml
services:
  valkey:
    # ... other config
    deploy:
      resources:
        limits:
          cpus: '2'
          memory: 4G
        reservations:
          memory: 2G
```

## See Also

- [Experiment Workflow](../../docs/experiment-workflow.md)
- [Distributed Job Queue Design](../../docs/design/distributed/distributed-job-queue.md)
- [Valkey Documentation](https://valkey.io/)
