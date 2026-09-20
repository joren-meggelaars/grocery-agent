# Grocery Agent

Self-hosted grocery spending tracker: receipts to structured data, shelf-price capture in the shop,
monthly overview, and deal alerts from weekly folders. Runs on SRV-DOC-01 next to TrendWatcher and
Clothing Advisor. **Not exposed publicly**: the only way in is Tailscale Serve.

Status: **Phase 0** (foundations: login, sessions, CSRF, security headers, deny-by-default routing, Postgres,
migrations, backups), **Phase 1** (receipts: upload, reading with Claude, review and correct, validation,
learned product names, quick add for the bakery and the Turkish supermarket) and **Phase 2** (in the shop: scan a
barcode, photograph the shelf label, instant comparison with what you usually pay, offline queue) **Phase 3**
(monthly overview against your reference, trend, categories and stores, and what you buy most) and **Phase 4**
(a one-off inventory of the products you have at home, as the list to compare at cheaper shops). The full plan is in
[docs/PLAN.md](docs/PLAN.md).

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
| `ANTHROPIC_API_KEY` | Key for reading receipts. Create a dedicated one in the Anthropic Console with a monthly limit of about EUR 5. Without it receipts fail with a message saying so. |
| `LLM_MODEL_SHELF` / `LLM_EFFORT_SHELF` | Model and effort for shelf labels (default Sonnet 5, effort low). Labels are simple, so Haiku 4.5 may be enough later: compare readings in the `extractions` table first. |
| `OFF_USER_AGENT` | Sent to Open Food Facts, which asks for an identifier with a contact, for example `GroceryAgent/0.1 (you@example.com)`. |
| `MONTHLY_REFERENCE_EUR` | The monthly food budget the overview compares against (default 400). |
| `LLM_MONTHLY_BUDGET_EUR` | New receipts are not read once the estimated spend this month reaches this (banner at 80%). Default 5. |

```bash
# 3. Start (builds the image, runs migrations, serves on 127.0.0.1:8090 only)
docker compose up -d --build       # app + worker + db
docker compose ps
docker compose logs -f app worker    # Ctrl+C to leave

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

## Using it (Phase 1)

- **Add receipt:** upload one or more photos (top to bottom for a long receipt) or one PDF. Photos are shrunk in the
  browser first. The worker reads the receipt (10 to 30 seconds), then the page shows the review screen.
- **Review:** check store, date, total and every line. The bar at the bottom shows lines against total live; a
  difference means a line was misread or missed. Type or pick the product name per line: what you confirm is
  remembered per store, so the same receipt text is recognised next time. Save.
- **Bakery / Turkish supermarket:** the quick-add buttons. For weighed goods enter weight and price per kg (the price is
  prefilled from your last entry, first time EUR 8.49/kg).
- **Photos are deleted 7 days after you save a receipt** (30 days if it is never saved). Extracted text, your
  corrections and the raw model output stay in the database.
- **Cost:** every reading logs its tokens and an estimated cost (`extractions` table); the home page shows this
  month's total.

```bash
# what did the last readings cost?
docker compose exec db psql -U grocery -d grocery   -c "select id, model, input_tokens, output_tokens, round(cost_est_eur::numeric, 4) as eur, parsed_ok, error from extractions order by id desc limit 10;"
```

## In the shop (Phase 2, iPhone)

**One-time setup** (do this on wifi, before you go):
1. Open `https://<vm-name>.<tailnet>.ts.net:8443/capture` in **Safari** and sign in.
2. Share button, then **Add to Home Screen**. Always open the app from that icon: iOS may delete the offline queue of
   a plain Safari tab after a week without use, but not of an installed app.
3. Open the app once while online, and allow the camera when asked. This also saves the scan page for offline use.

**Per product:**
1. Pick the store (it remembers the last one).
2. **Scan barcode** (or type the number; a wrong check digit is flagged). No barcode, for example on fresh produce, is fine.
3. **Take photo of the price label** and press **Save**.
4. With a connection you land on the confirm screen a few seconds later: name, price, price per kg or l, promotion,
   validity, all prefilled from the label (and Open Food Facts for the product name). Correct what is wrong, **Save**.
   You then see what you usually pay for this product and where it was cheaper.
5. **Scan next.** Untick "Review right after saving" to scan a whole aisle first and review from *To review* later.

**Without signal:** the capture is stored on the phone and the counter at the top shows how many wait. They upload by
themselves when you open the app again with a connection (iOS cannot upload in the background). The label is read
after uploading, so review offline captures later from *To review*. If the counter says *Sign in to upload*, your session
expired: sign in and open the scan page again.

**What gets compared:** the same product, identified by its barcode or by the name you gave it (the name you type is
matched against your receipt products, so shelf prices and receipts share one history). Products from different
brands, such as Lidl's own brand versus a brand name, are not "the same product"; that comparison arrives with the deals
radar in Phase 5.

Open Food Facts data is used under the ODbL licence (attribution is shown on the confirm screen). Every barcode you
scan is sent to Open Food Facts once and cached afterwards, and only from the worker container.

## Monthly overview (Phase 3)

**Monthly overview** shows one calendar month (arrows for other months):
- **Food spend** against your reference (default EUR 400, set `MONTHLY_REFERENCE_EUR`), with a meter that turns amber from
  80% and red above 100%, the change against last month, the average of the previous months, and (from day 7) where the month
  ends at the current pace. Big shops make that estimate rough.
- **All purchases** is what actually left your wallet (the sum of the receipt totals). **Not food** is the household
  category (`Huishouden & verzorging`); everything else counts as food.
- **Trend:** six months as columns, this month highlighted, the reference as a line.
- **Where it went:** per category. A discount is booked to the product line above it, so a discounted chicken reduces
  *Vlees & vis*. Deposit, bags and rounding go to *Overig*.
- **Not itemised** appears only when a receipt was saved with "lines do not add up": that difference is kept so the
  numbers still add up to the receipt totals.
- **Per store:** total spent per store, the bakery and the Turkish supermarket included.
- Every chart has a **Table** underneath with all the numbers; tap a bar to see its value.

**What I buy most** ranks products by the number of receipts they were on (a product twice on one receipt counts once),
then by spend, with the usual price (per pack, or per kg for weighed goods) and the last purchase. Choose the period at
the top. Only receipt lines that have a product name count, so name the lines when you review a receipt.

Only **saved** receipts with a date count; receipts still under review, failed ones and shelf-label captures do not.

## Products at home (Phase 4)

A one-off inventory, not stock keeping: no quantities and no expiry dates. The goal is a list of the products you have at
home and use, so you know what to compare at cheaper shops.

1. **Products at home, Scan products at home, Start camera.** Hold one barcode at a time in front of the camera; the
   camera stays on and each barcode is reported once. A barcode the app already knows (from a shelf scan or an earlier
   round) goes on the list at once; an unknown one is queued and the worker looks its name up at Open Food Facts.
2. **Name scanned products:** one screen with every unknown barcode, the Open Food Facts name prefilled (or the existing
   product when the name matches). Use the same short names you use on receipts; typing shows your known products. Tick
   *I use this a lot* where it applies.
3. **The list:** heavy-use products first, then by how often you bought them in the last 90 days. Per product: what you
   last paid and where (from a receipt), and **Seen cheaper** when a shelf label or receipt at another shop was cheaper
   in the last two months. Products without a barcode (fresh produce, bread) can be added by name.
4. In the shop, when you scan a label of a product that is on the list, the confirm screen says *You have this at home*.

The list is the starting point for Phase 5: the products you use most are the ones worth comparing at Lidl.

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
| `Cross-origin request rejected` | The Origin check refused a POST. The log line `rejected cross-origin` shows the Origin and Sec-Fetch-Site that arrived; the Origin host must be in `ALLOWED_HOSTS`. |
| Receipt says `ANTHROPIC_API_KEY is not set` | Add the key to `.env`, run `docker compose up -d`, open the receipt and press *Try again*. |
| Camera does not start | Safari needs HTTPS (Tailscale Serve gives that) and camera permission: iPhone Settings, Safari (or the installed app), Camera. Typing the number always works. |
| Scan page blank offline | Open `/capture` once while online from the home-screen icon so the page is saved. |
| Receipt stays on *Reading...* | The worker is not running or cannot reach the API: `docker compose logs --tail 50 worker`. |
| `Monthly API budget reached` | Raise `LLM_MONTHLY_BUDGET_EUR` in `.env`, `docker compose up -d`, then *Try again*. |
| `Too many attempts` | Login lockout (5 failures, then 1 to 15 minutes). Wait, or run the `unlock` command above. |
| Compose says a `$` variable is unset | A `$` in a `.env` value. Keep secrets alphanumeric. |

## Development

```bash
uv sync
uv run pytest                                                   # 350 tests, SQLite, no Docker or API key needed
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
