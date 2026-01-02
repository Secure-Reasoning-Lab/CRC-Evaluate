# Securing Valkey/Redis for Public Access

This guide covers how to securely expose Valkey/Redis to the internet with password authentication and TLS encryption using Let's Encrypt.

## Table of Contents
- [Security Requirements](#security-requirements)
- [Basic Password Authentication](#basic-password-authentication)
- [TLS with Let's Encrypt](#tls-with-lets-encrypt)
- [Auto-Renewal Strategies](#auto-renewal-strategies)
- [Client Connection](#client-connection)
- [Troubleshooting](#troubleshooting)

---

## Security Requirements

### Binding Rules
| Scenario | Password | TLS | Behavior |
|----------|----------|-----|----------|
| localhost (127.0.0.1) | Optional | No | ✅ OK for testing |
| Public IP (0.0.0.0) | **Required** | Recommended | ⚠️ Password minimum |
| WAN/Internet | **Required** | **Required** | ✅ Production ready |

### CRSBench Validation
CRSBench enforces this in code:
```python
def validate_redis_config(redis_host: str, redis_password: Optional[str] = None):
    """Validate Redis connection security."""
    is_public = redis_host not in ("localhost", "127.0.0.1") and not redis_host.startswith("127.")

    if is_public and not redis_password:
        raise ValueError(
            "Redis password required for public binding. "
            "Set REDIS_PASSWORD environment variable."
        )
```

---

## Basic Password Authentication

### Minimal Secure Setup

**docker-compose.yml**:
```yaml
version: "3.8"

services:
  valkey:
    image: valkey/valkey:8.0-alpine
    container_name: crsbench-valkey
    ports:
      - "0.0.0.0:6379:6379"  # Public binding
    environment:
      - VALKEY_PASSWORD=${VALKEY_PASSWORD}
    healthcheck:
      test: ["CMD", "valkey-cli", "-a", "${VALKEY_PASSWORD}", "ping"]
      interval: 5s
      timeout: 3s
      retries: 5
    volumes:
      - valkey-data:/data
    restart: unless-stopped
    command: >
      valkey-server
      --appendonly yes
      --requirepass ${VALKEY_PASSWORD}
      --maxclients 1000

volumes:
  valkey-data:
    driver: local
```

**Generate strong password**:
```bash
openssl rand -base64 32
```

**Create `.env` file** (DO NOT commit to git):
```env
VALKEY_PASSWORD=your_very_strong_random_secret_here
```

**Add to `.gitignore`**:
```
services/valkey/.env
```

---

## TLS with Let's Encrypt

Let's Encrypt now supports certificates for **public IP addresses**.

### Prerequisites
- Public IPv4 or IPv6 address
- Port 80 (HTTP-01) or 443 (TLS-ALPN-01) accessible for validation
- OR DNS control (for DNS-01 challenge)

### Option A: Host-Based Certbot (Recommended)

This approach runs certbot on the host, avoiding port conflicts.

#### 1. Install Certbot
```bash
sudo apt update
sudo apt install certbot
```

#### 2. Obtain Initial Certificate

**For Public IP**:
```bash
sudo certbot certonly --standalone -d YOUR_PUBLIC_IP
```

**For Domain Name** (easier renewal):
```bash
sudo certbot certonly --standalone -d valkey.yourdomain.com
```

Certificates saved to:
- `/etc/letsencrypt/live/YOUR_IP/fullchain.pem`
- `/etc/letsencrypt/live/YOUR_IP/privkey.pem`

#### 3. Configure Valkey with TLS

**docker-compose.yml**:
```yaml
version: "3.8"

services:
  valkey:
    image: valkey/valkey:8.0-alpine
    container_name: crsbench-valkey
    ports:
      - "0.0.0.0:6379:6379"
    environment:
      - VALKEY_PASSWORD=${VALKEY_PASSWORD}
    healthcheck:
      test: ["CMD", "valkey-cli", "-a", "${VALKEY_PASSWORD}", "--tls", "--insecure", "ping"]
      interval: 5s
      timeout: 3s
      retries: 5
    volumes:
      - valkey-data:/data
      - /etc/letsencrypt/live/YOUR_IP:/certs:ro
      - /etc/letsencrypt/archive/YOUR_IP:/certs-archive:ro
    restart: unless-stopped
    command: >
      valkey-server
      --appendonly yes
      --requirepass ${VALKEY_PASSWORD}
      --tls-port 6379
      --port 0
      --tls-cert-file /certs/fullchain.pem
      --tls-key-file /certs/privkey.pem
      --tls-auth-clients no
      --maxclients 1000

volumes:
  valkey-data:
    driver: local
```

**Note**: `--port 0` disables non-TLS port. Only TLS connections allowed.

#### 4. Setup Auto-Renewal

Let's Encrypt certs expire in **90 days**. Setup automatic renewal:

```bash
# Test renewal (dry run)
sudo certbot renew --dry-run

# Add cron job for auto-renewal
sudo crontab -e
```

Add this line (runs twice daily):
```cron
0 0,12 * * * certbot renew --quiet --deploy-hook "docker restart crsbench-valkey"
```

The `--deploy-hook` restarts Valkey to reload the new certificates.

---

### Option B: Certbot Container (Alternative)

Use if you prefer Docker-only setup. **Warning**: Requires port 80 for renewals.

**docker-compose.yml**:
```yaml
version: "3.8"

services:
  certbot:
    image: certbot/certbot
    container_name: certbot
    volumes:
      - ./certs:/etc/letsencrypt
      - ./certbot-var:/var/lib/letsencrypt
    entrypoint: "/bin/sh -c 'trap exit TERM; while :; do certbot renew --standalone --preferred-challenges http; sleep 12h & wait $${!}; done;'"
    ports:
      - "80:80"  # Required for HTTP-01 challenge
    restart: unless-stopped

  valkey:
    image: valkey/valkey:8.0-alpine
    container_name: crsbench-valkey
    depends_on:
      - certbot
    ports:
      - "0.0.0.0:6379:6379"
    environment:
      - VALKEY_PASSWORD=${VALKEY_PASSWORD}
    healthcheck:
      test: ["CMD", "valkey-cli", "-a", "${VALKEY_PASSWORD}", "--tls", "--insecure", "ping"]
      interval: 5s
      timeout: 3s
      retries: 5
    volumes:
      - valkey-data:/data
      - ./certs/live/YOUR_IP:/certs:ro
    restart: unless-stopped
    command: >
      valkey-server
      --appendonly yes
      --requirepass ${VALKEY_PASSWORD}
      --tls-port 6379
      --port 0
      --tls-cert-file /certs/fullchain.pem
      --tls-key-file /certs/privkey.pem
      --tls-auth-clients no
      --maxclients 1000

volumes:
  valkey-data:
    driver: local
```

**Initial certificate**:
```bash
docker-compose run --rm certbot certonly --standalone -d YOUR_PUBLIC_IP --email your@email.com --agree-tos
```

---

### Option C: DNS Challenge (Best for Multiple Servers)

DNS-01 challenge doesn't require port 80/443. Works with any DNS provider.

**Example with Cloudflare**:

```bash
# Install DNS plugin
pip install certbot-dns-cloudflare

# Create credentials file
sudo mkdir -p /etc/letsencrypt
sudo cat > /etc/letsencrypt/cloudflare.ini <<EOF
dns_cloudflare_api_token = YOUR_CLOUDFLARE_API_TOKEN
EOF
sudo chmod 600 /etc/letsencrypt/cloudflare.ini

# Obtain certificate
sudo certbot certonly \
  --dns-cloudflare \
  --dns-cloudflare-credentials /etc/letsencrypt/cloudflare.ini \
  -d valkey.yourdomain.com \
  --email your@email.com \
  --agree-tos

# Auto-renewal cron
0 0 * * * certbot renew --quiet --post-hook "docker restart crsbench-valkey"
```

Plugins available for: Cloudflare, AWS Route53, Google Cloud DNS, DigitalOcean, etc.

---

## Auto-Renewal Strategies

### Strategy Comparison

| Method | Pros | Cons |
|--------|------|------|
| **Host cron + certbot** | Simple, no port conflicts | Requires host access |
| **Certbot container** | Docker-only | Needs port 80 |
| **DNS challenge** | No port requirements | Requires DNS API access |

### Monitoring Renewal

Check certificate expiration:
```bash
# Host-based
sudo certbot certificates

# Container-based
docker exec certbot certbot certificates
```

Check Valkey is using TLS:
```bash
# Should show TLS connection info
openssl s_client -connect YOUR_IP:6379 -servername YOUR_IP
```

---

## Client Connection

### Python (redis-py)

**Without TLS** (localhost only):
```python
import redis

redis_conn = redis.Redis(
    host='localhost',
    port=6379,
    password='your_password'
)
```

**With TLS** (production):
```python
import redis

redis_conn = redis.Redis(
    host='your-public-ip',
    port=6379,
    password='your_password',
    ssl=True,
    ssl_cert_reqs='required',
    ssl_ca_certs='/etc/ssl/certs/ca-certificates.crt'  # System CA bundle
)
```

**With TLS (skip verification - not recommended)**:
```python
import redis

redis_conn = redis.Redis(
    host='your-public-ip',
    port=6379,
    password='your_password',
    ssl=True,
    ssl_cert_reqs=None
)
```

### RQ (Redis Queue)

**Without TLS**:
```python
import redis
import rq

redis_conn = redis.Redis(host='localhost', port=6379, password='password')
queue = rq.Queue('default', connection=redis_conn)
```

**With TLS**:
```python
import redis
import rq

redis_conn = redis.Redis(
    host='your-public-ip',
    port=6379,
    password='password',
    ssl=True,
    ssl_cert_reqs='required'
)
queue = rq.Queue('default', connection=redis_conn)
```

### redis-cli

**Without TLS**:
```bash
redis-cli -h your-ip -a your_password
```

**With TLS**:
```bash
redis-cli -h your-ip -a your_password --tls --insecure
```

Or with cert validation:
```bash
redis-cli -h your-ip -a your_password \
  --tls \
  --cacert /etc/ssl/certs/ca-certificates.crt
```

---

## Troubleshooting

### Connection Refused

**Check Valkey is listening**:
```bash
docker logs crsbench-valkey
```

Look for:
```
Ready to accept connections tcp
```

**Check firewall**:
```bash
# Allow port 6379
sudo ufw allow 6379/tcp
```

### TLS Handshake Failed

**Verify certificate**:
```bash
openssl s_client -connect YOUR_IP:6379 -servername YOUR_IP
```

Should show certificate details, not errors.

**Check certificate files mounted**:
```bash
docker exec crsbench-valkey ls -la /certs/
```

Should show:
```
fullchain.pem
privkey.pem
```

### Authentication Failed

**Verify password**:
```bash
docker exec crsbench-valkey valkey-cli -a your_password ping
```

Should return `PONG`.

**Check environment variable**:
```bash
docker exec crsbench-valkey env | grep VALKEY_PASSWORD
```

### Certificate Renewal Failed

**Check certbot logs**:
```bash
# Host-based
sudo tail -f /var/log/letsencrypt/letsencrypt.log

# Container-based
docker logs certbot
```

**Common issues**:
- Port 80/443 blocked by firewall
- DNS not pointing to correct IP
- Rate limiting (5 failures per hour)

**Test renewal manually**:
```bash
sudo certbot renew --dry-run
```

---

## Additional Security Measures

### Rate Limiting with iptables

Protect against connection floods:

```bash
# Limit to 10 new connections per minute per IP
sudo iptables -A INPUT -p tcp --dport 6379 -m state --state NEW \
  -m recent --set --name REDIS

sudo iptables -A INPUT -p tcp --dport 6379 -m state --state NEW \
  -m recent --update --seconds 60 --hitcount 10 --name REDIS -j DROP

# Save rules
sudo netfilter-persistent save
```

### Monitoring

**Check failed authentication attempts**:
```bash
docker logs crsbench-valkey 2>&1 | grep "AUTH failed"
```

**Monitor connections**:
```bash
docker exec crsbench-valkey valkey-cli -a your_password CLIENT LIST
```

---

## Queue Clearing (After Disabling FLUSHALL)

**CRSBench `clear_queue()` already works** - it uses `DEL` per job, not `FLUSHALL`:

```python
from crsbench.distributed.queue import clear_queue

cleared = clear_queue(queue)
print(f"Cleared {cleared} jobs")
```

RQ built-in methods also safe:
```python
queue.empty()                        # Clear queued jobs
queue.failed_job_registry.cleanup()  # Clear failed jobs
```

**All commands remain available** - password protection is sufficient. No need to rename/disable commands.

---

## Free Alternatives to Public IP Certs

### Use a Free Domain Name

Services offering free domains/subdomains:
- **DuckDNS.org** - Free subdomain (yourname.duckdns.org)
- **FreeDNS** - Free subdomains
- **No-IP.com** - Free hostname

Benefits:
- Easier cert management
- Wildcard certs possible
- Better client compatibility
- No IP-based limitations

**Setup with DuckDNS**:
```bash
# Update your IP at DuckDNS
curl "https://www.duckdns.org/update?domains=yourname&token=YOUR_TOKEN"

# Get cert
sudo certbot certonly --standalone -d yourname.duckdns.org

# Auto-update IP (cron)
*/5 * * * * curl -s "https://www.duckdns.org/update?domains=yourname&token=YOUR_TOKEN" > /dev/null
```

---

## References

- [Valkey TLS Documentation](https://valkey.io/topics/encryption/)
- [Let's Encrypt for IP Addresses](https://letsencrypt.org/docs/certificates-for-ip-addresses/)
- [Certbot Documentation](https://certbot.eff.org/docs/)
- [redis-py SSL/TLS](https://redis-py.readthedocs.io/en/stable/examples/ssl_connection_examples.html)
