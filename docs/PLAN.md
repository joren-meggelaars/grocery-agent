# Grocery Agent: plan

Status: approved 2026-09-20 with the decisions below. Phases 0 to 4 built, restyled 2026-09-21.

## Goals

Track grocery spend from receipts, show what is bought regularly, give a monthly overview against the
EUR 400 reference, capture shelf prices in the shop, and warn about large savings from weekly folders.
Single user, iPhone, English UI. Everything stays on the own server except calls to the Claude API.

## Decisions (from the user)

| Topic | Decision |
|---|---|
| Shops | Plus and Jumbo are the regular shops. **Lidl is the savings target.** Bread comes from the bakery (spend is tracked, not price-compared). Meat, mostly chicken, comes from a Turkish supermarket that is much cheaper than the big chains: it is the **price baseline for meat**, so meat is never suggested back to Plus/Jumbo unless they beat that price per kg. |
| Deals | **Scrape publicly available weekly folders / flyers** for offers and warn about large savings. |
| Phone | iPhone (Safari, home-screen PWA). |
| Models and cost | Sonnet 5 for receipts and labels, per-task model setting, monthly API cap EUR 5 with alerts at 80% and 100%. |
| Tailscale | Already on SRV-DOC-01. Serve on its own HTTPS port. Identity mode `require`. |
| Images | Re-encoded, EXIF-stripped JPEG counts as the original. **Deleted 7 days after the item is ingested and confirmed**; unconfirmed items after 30 days. Raw model JSON and all extracted text stay in the database. |
| Backups | Database only (photos are transient). Nightly dump, 14 kept, optional age encryption, copy off the VM now and then. |
| UI | English. Product names stay as printed (Dutch). |
| Open Food Facts | Lookups of scanned barcodes are fine, cached. |
| Digital receipts | The shops do not offer them: receipts are photos. |
| Phase 5 scope | All tiers acceptable (own data, community snapshots, direct adapters), with a per-source terms review first. |
| Repo | `Grocery-agent` |

## Architecture

```
iPhone / PC on tailnet --HTTPS--> Tailscale Serve (host) --> 127.0.0.1:8090
   -> [app: FastAPI + Jinja2]  -> [db: Postgres 18, internal network only]
   -> [worker: Claude API calls, jobs, image cleanup; later price sources]
egress only: api.anthropic.com, world.openfoodfacts.org, folder/price sources
```

- Sync SQLAlchemy 2.0 + psycopg3, Alembic, Pydantic v2, uv, pytest. Postgres job table (`SKIP LOCKED`), no Redis.
- Frontend: server-rendered Jinja2 with small hand-written scripts (no HTMX, no build step): photo shrinking, review grid,
  status polling; later scanner and outbox. Vendored libraries only.
  Strict CSP, no npm supply chain. Camera and service worker need HTTPS, which Serve provides.
- iPhone specifics: BarcodeDetector is not available in iOS Safari, so scanning uses a vendored ZXing (wasm) fallback
  (verify in phase 2). Background Sync does not exist on iOS: the offline outbox flushes on app open,
  `online` and `visibilitychange`. IndexedDB can be evicted for non-installed sites: the PWA must be added to the
  home screen and request `navigator.storage.persist()`.
- Offline: capture works offline; confirming a label needs Claude, so it happens later from a "to review" list.

## Access model and threat model

Reachable only via Tailscale Serve (ACL-limited). The container publishes `127.0.0.1:8090` only; the database has no
published port; the app runs non-root, read-only, all capabilities dropped.

| Risk | Mitigation |
|---|---|
| Other tailnet user / stolen tailnet device | Own login with lockout, identity mode `require`, ACLs, session list and revoke |
| Forged Tailscale headers | Trusted only from the Docker gateway (`172.30.90.1`), never sufficient alone in `require` mode |
| Password brute force | argon2id, per-user exponential backoff (1 to 15 min), no permanent lockout, unknown users throttled identically |
| CSRF | SameSite=Strict, session-bound token, Origin + Sec-Fetch-Site checks, Host allowlist |
| Malicious upload (phase 1) | Magic-byte sniffing, size and pixel caps, always re-encode, PDFs rasterised in a limited subprocess, random filenames outside web root |
| Prompt injection via receipt/label text | Model has no tools; output is untrusted, schema-validated, never reaches SQL/shell/HTML unescaped |
| Stored XSS from OFF / scraped text | Autoescape, strict CSP, no inline scripts |
| SSRF | Fixed outbound hosts, validated inputs, response size caps |
| API cost runaway | Upload rate limit, dedupe by hash, monthly budget with hard stop |
| Secrets | `.env` chmod 600, not in git, no secrets or receipt content in logs |
| Supply chain | `uv.lock`, vendored + hashed JS, pin image digests after first build |
| Co-tenancy (TrendWatcher, Clothing Advisor on the same VM) | Own compose project, networks, volumes, Postgres |

## Data model (target)

Money is integer cents; timestamps UTC.

```
users, sessions, auth_events, settings                        -- phase 0 (built)
files(sha256, path, kind, delete_after)                       -- transient images
stores(chain text, name, role[regular|target|baseline|other]) -- plus, jumbo, lidl, aldi, bakery, turkish ...
categories(name, parent_id, counts_as_food)
products, product_eans, off_cache, name_mappings(chain, raw_norm -> product, confirmed_count)
extractions(kind, model, prompt_version, raw_json, tokens, cost_est_eur, latency_ms, request_id)
receipts(store, purchased_on, total_cents, status, sum_delta_cents, flags, source[photo|pdf|manual])
receipt_lines(kind[item|discount|deposit|bag|rounding], raw_text, qty, unit_price, line_total, product_id, match_*)
shelf_captures(client_uuid UNIQUE, ean, store, price, unit_price, promo_*, requires_card, valid_*, status)
catalog_items, price_observations(source[receipt|shelf|folder|plugin:*], observed_at, valid_until), price_sources(health)
flyer_offers(chain, week, raw_text, price, promo_kind, valid_from/until, source_url, product_id?, confidence)
product_matches(kind[identical|substitute], method[ean|fuzzy|manual], confidence, status)
inventory_items, jobs
```

Receipt validation: line math (qty x unit = total, +-1 cent), sum of items + discounts + deposit + rounding equals the
receipt total exactly, flags for date and duplicates; delta shown live in the review screen.
Normalisation: exact `name_mappings` hit auto-applies, a fuzzy match (rapidfuzz, in Python so it also runs on SQLite in
tests) is a suggestion to check, otherwise a new product is proposed from the same extraction call. Every correction
raises `confirmed_count`.

## Phases

| Phase | Result | Effort (days) |
|---|---|---|
| **0 Foundations** (built) | Compose stack, login, sessions, CSRF, headers, deny-by-default, migrations, backup script, README | 1.5-2 |
| **1 Receipts** (built, first real receipts pending) | Upload (multi-photo), extraction job, review/edit, validation, learned names, manual quick-add (bakery, Turkish supermarket per-kg), token/cost log, image retention job | 4-5 |
| **2 In-store** (built, not yet tried in a shop) | Barcode scan (ZXing), shelf photo, confirm, OFF cache, "usual vs now" feedback, large-button UI, offline outbox, service worker | 4-5 |
| **3 Analysis** (built) | Top products, per category and store, monthly view vs EUR 400, trend, charts | 2 |
| **4 Cupboard** (built, scope narrowed) | Scan to add stock, linked to last price and store | 1-1.5 |
| **5 Deals radar and comparison** | Weekly folders of Lidl, Plus, Jumbo (Aldi optional) into `flyer_offers`, matched to what you buy, alerts for large savings; then per-product/basket comparison with identical vs substitute savings | 5-7 |

### Phase 5 notes

- Order: deals radar first (most value), comparison second.
- "Large saving" = configurable threshold on percentage and euros versus your usual paid price, or versus the cheapest
  known equivalent (Lidl mostly sells own brands, so many matches are "comparable substitute", not "identical").
- Prefer folder sources that publish HTML/JSON offers (free to read). Folder images/PDFs need vision and cost tokens
  (rough estimate: several EUR per month for three chains weekly): that is **asked before it is built**.
- Per-source terms/robots review before any adapter is written; polite fetching (identity in User-Agent, low rate,
  cache), staleness tracking per source, circuit breaker so one failing source cannot break the app.
- Notification channel still open (iOS Web Push for the home-screen PWA, e-mail via Graph, or Home Assistant push).
- Preliminary landscape (quick search, not yet verified in depth): no official price APIs for the chains were found;
  community projects exist (`checkjebon`, `SupermarktConnector`, `open-supermarket-api`) but checkjebon's issue #24
  (2026-09-15) reports stale data for AH, Plus, Aldi, so the aggregate is weakest for the chains that matter here.

## Cost (estimate, prices cached 2026-06-24)

Sonnet 5 USD 2 / 10 per million tokens in/out. About USD 0.05 per receipt (1-3 images), about USD 0.005 per shelf
label: roughly USD 1-3 per month. Levers: client resize, text-layer PDFs sent as text, dedupe by hash, no LLM call
where SQL can decide, per-task model setting. To be measured from the logged token usage.

## Not yet verified

- `docker compose` stack and Dockerfile were written on a machine without Docker: first build happens on the VM.
- `tailscale serve` flag syntax for the installed version (docs page was unreachable when planning).
- Whether Serve preserves the original `Host` header (if not, `ALLOWED_HOSTS` needs the rewritten value; the log says
  what arrived). The Origin check uses the browser-supplied Origin either way.
- iOS Safari BarcodeDetector availability and ZXing performance (phase 2).
- Anthropic API data-retention terms for receipt images (check before the first real upload).

## Phase 1 as built (deviations from the plan)

- Fuzzy matching uses rapidfuzz in Python instead of `pg_trgm`; quantities are stored as integer thousandths
  (`quantity_milli`) to avoid decimals; `receipt_lines.suggested_name` keeps the model's proposed product name.
- The model is called with `messages.create` and a JSON schema (`transform_schema`), and validated with Pydantic in our
  code, so the raw output is kept even when it does not validate. Prompt: `src/grocery/llm/prompts/receipt_v1.md`.
- Digital PDF receipts with a text layer are sent as text (no images); scans and photos as images.
- Only the worker container talks to the Claude API; the app container never does.
- Not yet exercised against the real API: the prompt and the request shape are covered by tests with a stub client only.
  Expect one round of prompt tuning after the first real receipts (the raw output is in `extractions.raw_json`).

## Phase 2 as built (deviations and findings)

- Barcode scanning uses the browser's BarcodeDetector when present and the vendored `@zxing/library` 0.21.3 otherwise
  (the pure-JavaScript build: no WebAssembly, so no CSP exception is needed on Safari). The file was checked byte for
  byte against the npm tarball; `tests/test_pwa.py` fails if it changes. Typing the number always works.
- Every capture goes through an IndexedDB outbox, also when online, so a flaky connection never loses one. Uploads are
  idempotent on a client-generated id. The scan page carries no personal data or CSRF token (the outbox fetches a fresh
  token from `/api/csrf` per upload), which lets the service worker keep it for offline use.
- `/sw.js` and `/manifest.webmanifest` are the only extra public routes (deny-by-default test updated). The service
  worker only handles the scan page and `/static/`, never API calls.
- Open Food Facts answers an unknown barcode with HTTP 200 and `status: 0` (not 404); checked against the real API.
  Malformed answers are never cached as "not found". Lookups run in the worker only.
- Receipts and shelf labels share one `price_observations` table; "usual price" is the median of the last five
  receipt prices, shelf sightings only stand in when there is no receipt yet. Comparisons use per kg/l when both sides
  have it, otherwise per pack.
- The shelf label name leads over the Open Food Facts name (short, what you see in the shop); the latter is a fallback
  and a second chance to recognise a product you already have.
- Not verified: the camera and scanner on a real iPhone (no device here), the real Claude call for shelf labels
  (prompt `shelf_v1`, stub-tested only), and the offline flow end to end on a phone. Expect one round of tuning after the
  first shop visit.

## Phase 3 as built

- Pure aggregation functions (`analytics/aggregate.py`) over plain facts, loaded by `analytics/queries.py`; the pages only
  format. Month total = sum of receipt totals; categories come from the lines; a discount is booked to the item above it;
  the remainder of an accepted receipt difference is shown as "Not itemised" (counted as food). Food spend =
  categories flagged `counts_as_food` plus that remainder, compared with `MONTHLY_REFERENCE_EUR`.
- Charts are server-rendered inline SVG (no library, nothing for the CSP to allow), one series each: the current month is
  the accent and earlier months grey, bars at most 24px with a 4px rounded data end, hairline axis, a table next to every
  chart, tooltips via a small script (tap or hover). Checked visually in a headless browser at phone width; colour contrast
  of bars and status colours was computed against the card backgrounds in light and dark mode.
- "Most bought" counts receipts, not lines. Shelf captures are prices, not purchases, and are not part of spend.
- Not done on purpose: category budgets, per-product price trend charts (Phase 5 territory), export.

## Phase 4 as built (scope decided with the user)

- The user does not want stock keeping: no quantities, no expiry dates. It is a **one-off inventory** of products at home,
  to pick the ones worth comparing at cheaper shops. So: continuous barcode scanning at home, unknown barcodes named
  afterwards in one sitting, a "used a lot" star, and per product the last paid price and a cheaper sighting.
- Tables `cupboard_items` (one row per product) and `cupboard_scans` (barcodes waiting for a name). The worker job
  `lookup_ean` fetches the Open Food Facts name (cached); unknown or unreachable never blocks naming.
- "Last paid" comes from receipts only; a shelf sighting stands in as "Last seen" when there is no receipt yet.
- The shop scan tells you when the product is already on the list.
- Feeds Phase 5: the list (plus heavy-use flag and purchase frequency) is the watch list for the deals radar.

## Look and feel (restyle, 2026-09-21)

- Same structure as the Trendwatch admin (design tokens in CSS variables, dark sidebar with icon navigation, soft bordered
  cards, pill badges and tabs, a gradient hero card), in black and blue: black sidebar and app bar, blue primary and
  accents, a black-to-blue hero on Home and the overview, true black in dark mode. Light and dark follow the system.
- Phone: black app bar with a menu button, plus a fixed bottom tab bar (Home, Receipts, a raised Scan button, Overview,
  More) so the main things are one thumb away. Desktop: the sidebar.
- The scan page stays identical for every user (so the service worker can cache it): the sidebar there has an Account link
  instead of a Sign-out form, which needs a CSRF token.
- Contrast was computed for 38 text/background pairs in both modes (all pass); the pages were checked in a headless browser
  at phone and desktop width, light and dark. A real iPhone has not seen it yet.

## Phase 5 as built: deals radar (source: PrijsProfeet, alerts: Home Assistant)

- **Source**: the PrijsProfeet public API, without a key (optional free key in `.env`). Terms v1.6 read on 2026-09-23: free use,
  also for a personal tool; name PrijsProfeet wherever the data is shown (Deals page and notification carry it); prices shown as
  current at most 24 h old (nightly refresh, expired offers hidden); do not walk the catalogue to copy it (we only ask about
  products you buy plus a few categories); 30 requests/min per endpoint per IP (calls are spaced 3.5 s); user agents containing
  "bot"/"crawler"/"spider" get a 403 (ours does not). Only the four chains you shop at are kept: Jumbo, Plus, Aldi, Lidl.
  Aldi publishes the bundle total as its price: divided by the bundle size. Response shape checked against the live OpenAPI spec.
- **What is asked** (`deals/service.py:build_plan`): one search per product you buy (receipts in the last year, or on the
  products-at-home list; heavy-use first; at most 120) and, for the watched categories (default `huishouden`, `drogisterij`),
  one query per category and chain with a minimum discount. Search hits that do not fuzzy-match the product (rapidfuzz >= 82)
  are not stored.
- **Two kinds of alert**: *mine* = a product you bought on 2+ receipts (or have at home) that is at least N% below your usual
  paid price, compared per kg/l when the pack content is known, else per pack (flagged "check the pack size"); without a price
  history a folder discount of at least the notable threshold counts. *Notable* = a watched category, discount >= N% and a saving
  >= EUR M per item, for products with no history. Discounts above 75% are ignored (nearly always a data quirk).
  All thresholds and the categories live on the Settings page.
- **Running**: the worker's hourly housekeeping queues a refresh when the last one is 20 h old (6 h after a failure). A refresh is
  a `deals_refresh` job that works off the plan in chunks of 10 questions and queues its own continuation, so receipt reading in
  the same worker is never blocked for long. A 403/429/5xx/unreachable answer stops the run (circuit breaker). "Refresh now" on the
  Deals page has a 30 minute cooldown.
- **Alerts**: one Home Assistant notification per refresh for everything new (`notify.<service>` through the REST API); a deal is
  alerted once (`alerted_at`); a failed send is retried on the next refresh. `.env`: `HA_URL`, `HA_TOKEN`, `HA_NOTIFY_SERVICE`.
- **Not done**: product images (privacy/CSP), vision on folder PDFs (only if this source turns out too thin), cross-retailer EAN
  matching (a paid PrijsProfeet tier), iOS web push (Home Assistant was chosen instead).
- **"Not interesting" (added later)**: each offer has a form with reasons that fit it: not my brand, no interest in the category,
  no A-brands in the category (only offered for A-brands; house-brand and unknown offers are kept), not this product (matched on
  the normalised name, so any shop and spelling), or something else (this offer only, with your own words). Every reason can carry a
  note. They are stored as `deal_rules` and applied in every evaluation, so they survive the nightly refresh and never alert;
  the page lists them with a Remove button. Needs `deals.private_label` (migration 0008).
