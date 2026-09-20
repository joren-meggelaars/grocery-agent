# Grocery Agent

Self-hosted grocery spending tracker: receipts to structured data, shelf-price capture in the shop,
monthly overview, and deal alerts from weekly folders. Runs on SRV-DOC-01 next to TrendWatcher and
Clothing Advisor. **Not exposed publicly**: the only way in is Tailscale Serve.

Status: **Phase 0 (foundations)**: login, sessions, CSRF, security headers, deny-by-default routing, Postgres,
migrations, backups. Receipts arrive in Phase 1. The full plan is in [docs/PLAN.md](docs/PLAN.md).

## Deploy on SRV-DOC-01

Run these on the VM. Docker Compose and Tailscale are already installed there.

```bash
# 1. Get the code (use your deploy key / HTTPS remote, as for the other apps)
git clone <your-repo-url> ~/grocery-agent
cd ~/grocery-agent

# 2. Configure (secrets stay in .env, which is git-ignored)
cp .env.example .env
chmod 600 .env
sed -i "s/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=$(openssl rand -hex 24)/" .env

# Find the VM's Tailscale name and your Tailscale login, then edit ALLOWED_HOSTS and TS_ALLOWED_LOGINS:
tailscale status --json | jq -r '.Self.DNSName'                         # trailing dot: drop it
tailscale status --json | jq -r '.User[(.Self.UserID|tostring)].LoginName'  # or read it in the admin console
nano .env
```

`.env` values that matter:

| Variable | Meaning |
|---|---|
| `ALLOWED_HOSTS` | The `*.ts.net` name (no scheme, no port). Requests for any other Host are rejected. |
| `TS_IDENTITY_MODE` | `require` (default): password **and** a matching Tailscale login. `sso`: Tailscale login alone. `off`. |
| `TS_ALLOWED_LOGINS` | Tailscale login(s) allowed in. |
| `TS_TRUSTED_PROXY_IPS` | Only this peer may supply the identity header. Keep `172.30.90.1/32` (the `web` network gateway in `docker-compose.yml`). |

```bash
# 3. Start (builds the image, runs migrations, serves on 127.0.0.1:8090 only)
docker compose up -d --build
docker compose ps
docker compose logs -f app          # Ctrl+C to leave

# 4. Create your login (asks for the password twice; minimum 12 characters)
docker compose run --rm app python -m grocery.cli create-user joren --tailscale-login you@example.com

# 5. Local check on the VM
curl -s http://127.0.0.1:8090/healthz          # {"status":"ok"}
```

### Tailscale Serve

The app publishes `127.0.0.1:8090:8000` (loopback only). Serve is the only way in: no port forward,
no Nginx Proxy Manager route, no Funnel.

```bash
tailscale serve status                                        # see what already exists first; do not overwrite it
sudo tailscale serve --bg --https=8443 http://127.0.0.1:8090  # its own HTTPS port, next to anything on 443
tailscale serve status
```

Then open `https://<vm-name>.<tailnet>.ts.net:8443/` on your iPhone (Safari) and sign in.
Prerequisites: MagicDNS and HTTPS certificates enabled in the Tailscale admin console (the certificate
puts the machine name in public Certificate Transparency logs).

> The `tailscale serve` flag syntax has changed between versions. If the command errors, check
> `tailscale serve --help` on the VM and adjust; the intent is "HTTPS on a free port, proxying to
> `http://127.0.0.1:8090`". To remove the mapping: `sudo tailscale serve --https=8443 off`.

### Verify it works and is not exposed

```bash
# The identity check: the source address the app sees must be the trusted proxy.
docker compose exec db psql -U grocery -d grocery \
  -c "select ts, username, ip, kind from auth_events order by id desc limit 3;"
#   ip must be 172.30.90.1. If not, put that address in TS_TRUSTED_PROXY_IPS and `docker compose up -d`.

# Nothing else listens: 8090 must be bound to 127.0.0.1 only ...
ss -ltnp | grep 8090
# ... and from another machine on the LAN this must time out / be refused:
curl -m 3 http://10.0.100.8:8090/healthz
```

## Operations

```bash
git pull && docker compose up -d --build          # update (runs new migrations on start)
docker compose logs --tail 100 app                # logs

# accounts
docker compose run --rm app python -m grocery.cli set-password joren      # also signs out all devices
docker compose run --rm app python -m grocery.cli unlock joren            # clear a login lockout
docker compose run --rm app python -m grocery.cli revoke-sessions joren
```

### Backups

Only the database is backed up (receipts, lines, your learned product names, price history, settings).
Photos are not: they are deleted a week after ingestion. Expect a few MB per year.

```bash
chmod +x scripts/backup.sh
./scripts/backup.sh                               # ./backups/grocery-YYYY-MM-DD-HHMM.sql.gz, keeps the newest 14
# nightly at 03:15:
( crontab -l 2>/dev/null; echo '15 3 * * * cd ~/grocery-agent && ./scripts/backup.sh >> backups/backup.log 2>&1' ) | crontab -

# restore into an empty database:
gunzip -c backups/<file>.sql.gz | docker compose exec -T db psql -U grocery -d grocery
```

Set `AGE_RECIPIENT=age1...` when running the script to encrypt the dump with [age](https://age-encryption.org).
Copy dumps off the VM now and then; a backup that lives only on the same disk is not a backup.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `Invalid host header` | The name in the URL is not in `ALLOWED_HOSTS`. The log line `rejected Host header` shows what arrived. |
| Everything is `403 Forbidden` | Identity gate. Look for `tailscale identity rejected` / `ignoring Tailscale-User-Login header from untrusted peer X` in the logs: fix `TS_TRUSTED_PROXY_IPS` or `TS_ALLOWED_LOGINS`. Temporary escape hatch: `TS_IDENTITY_MODE=off`. |
| `Too many attempts` | Login lockout (5 failures, then 1 to 15 minutes). Wait, or run the `unlock` command above. |
| Compose says a `$` variable is unset | A `$` in a `.env` value. Keep secrets alphanumeric. |

## Development

```bash
uv sync
uv run pytest                                                   # 73 tests, SQLite, no Docker needed
DATABASE_URL=sqlite:///./dev.db COOKIE_SECURE=false uv run alembic upgrade head
DATABASE_URL=sqlite:///./dev.db COOKIE_SECURE=false uv run uvicorn --factory grocery.main:create_app --port 8000
```

## Security model in one paragraph

Tailscale Serve is the only entry. On top of that the app has its own login (argon2id, server-side
sessions stored as hashes, `__Host-` cookie that is HttpOnly/Secure/SameSite=Strict), a CSRF token plus
Origin/Fetch-Metadata checks on every unsafe request, per-user login backoff, a Host allowlist, a strict CSP
and other security headers, and deny-by-default routing: a route with no explicit `@public` marker requires a
session, enforced by a global dependency and covered by a test that enumerates every route. The Tailscale
identity header is only trusted from the Docker gateway address, and in the default mode it never replaces the
password. Details and the threat model are in [docs/PLAN.md](docs/PLAN.md).
