"""Deals radar: ask PrijsProfeet about what you buy, decide which offers are worth a look, alert through Home Assistant.

Two kinds of relevant offers:
- "mine": a product you buy (bought on at least two receipts in the last year, or on your products-at-home list)
  that is now clearly cheaper than you usually pay. Without a price history, a big folder discount is enough.
- "notable": an offer in a category you watch (household, drugstore by default) with a discount big enough to
  consider, for products you buy so rarely that there is no price history to compare with.
"""

import http.client
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from statistics import median
from urllib.parse import urlsplit

from rapidfuzz import fuzz
from sqlalchemy import distinct, func, select
from sqlalchemy.orm import Session

from grocery.config import Settings
from grocery.db.base import utcnow
from grocery.db.models import CupboardItem, Deal, DealRule, DealRun, Job, PriceObservation, Product, Receipt, ReceiptLine, Store
from grocery.deals.client import PrijsProfeet, SourceError
from grocery.jobs import queue
from grocery.prices.observations import comparable
from grocery.products.content import ContentError, parse_content, unit_price_from_content
from grocery.products.normalize import normalize_raw
from grocery.settings_store import Effective

log = logging.getLogger(__name__)

SOURCE = "prijsprofeet"
JOB_KIND = "deals_refresh"
CHUNK = 10  # questions per job: 10 x 3.5 s, so receipt reading in the same worker is never held up for long
MAX_PRODUCT_QUERIES = 120
MATCH_MIN = 82  # fuzzy name score (0-100) to call an offer "this product"
MIN_VISITS = 2  # receipts a product needs to count as one you buy
MAX_PLAUSIBLE_PCT = 75  # "90% off" is nearly always a pack-size or data quirk
REFRESH_EVERY = timedelta(hours=20)
RETRY_AFTER_FAILURE = timedelta(hours=6)
PURGE_AFTER = timedelta(days=14)
MAX_ALERT_LINES = 8
SOURCE_NAME = "PrijsProfeet (prijsprofeet.nl)"

_UNITS = "g|gr|kg|ml|cl|l|ltr|st|stuks|x"
_SIZE_TOKEN = re.compile(rf"^(\d+([.,]\d+)?({_UNITS})?|{_UNITS})$")  # 500g, 6, 33, cl, x

# (url, token, payload) -> (http status, body); replaced in tests
HaPost = Callable[[str, str, dict], tuple[int, bytes]]


# --- what to ask the source ------------------------------------------------------

@dataclass
class Tracked:
    product: Product
    visits: int
    on_cupboard: bool


def tracked_products(db: Session, today: date) -> list[Tracked]:
    """Products you buy or have at home: heavy-use ones first, then by how often you bought them."""
    since = today - timedelta(days=365)
    visits = dict(
        db.execute(
            select(ReceiptLine.product_id, func.count(distinct(ReceiptLine.receipt_id)))
            .join(Receipt, Receipt.id == ReceiptLine.receipt_id)
            .where(
                Receipt.status == "confirmed", Receipt.purchased_on >= since, ReceiptLine.kind == "item",
                ReceiptLine.product_id.is_not(None),
            )
            .group_by(ReceiptLine.product_id)
        ).all()
    )
    heavy = {i.product_id: i.heavy_use for i in db.scalars(select(CupboardItem))}
    ids = set(visits) | set(heavy)
    if not ids:
        return []
    products = {p.id: p for p in db.scalars(select(Product).where(Product.id.in_(ids)))}
    rows = [Tracked(products[i], visits.get(i, 0), i in heavy) for i in ids if i in products]
    rows.sort(key=lambda t: (not heavy.get(t.product.id, False), -t.visits, t.product.name.casefold()))
    return rows


def query_text(name: str) -> str:
    """The words of a product name worth searching for: sizes like '1L' or '500g' are left out."""
    words = [w for w in normalize_raw(name).split() if not _SIZE_TOKEN.match(w)]
    return " ".join(words[:5])


def build_plan(db: Session, eff: Effective, today: date) -> list[dict]:
    plan: list[dict] = []
    for t in tracked_products(db, today)[:MAX_PRODUCT_QUERIES]:
        q = query_text(t.product.name)
        if q:
            plan.append({"kind": "product", "product_id": t.product.id, "q": q})
    for slug in [c for c in eff.deals_categories.split(",") if c]:
        for retailer in ("jumbo", "plus", "aldi", "lidl"):
            plan.append({"kind": "category", "category": slug, "retailer": retailer})
    return plan


def _params(item: dict, eff: Effective) -> list[tuple[str, str]]:
    base = [("promotion_status", "active"), ("page_size", "20")]
    if item["kind"] == "product":
        return [("q", item["q"]), *base]
    return [
        ("q", "*"), ("category", item["category"]), ("retailer", item["retailer"]),
        ("min_savings", str(eff.deals_notable_min_pct)), ("sort_by", "savings_percentage:desc"), *base,
    ]


# --- refresh ------------------------------------------------------------------------

def start_run(db: Session, eff: Effective, today: date, now: datetime) -> DealRun:
    run = DealRun(started_at=now, plan=build_plan(db, eff, today), cursor=0, calls=0, status="running")
    db.add(run)
    db.flush()
    return run


def ensure_refresh_queued(db: Session, eff: Effective, now: datetime, force: bool = False) -> bool:
    """Queue a refresh when one is due (nightly), unless one is queued, running, or failed recently."""
    if eff.deals_enabled != "on":
        return False
    if db.scalar(select(Job.id).where(Job.kind == JOB_KIND, Job.status.in_(("queued", "running"))).limit(1)):
        return False
    last = db.scalar(select(DealRun).order_by(DealRun.id.desc()).limit(1))
    if last is not None and not force:
        if last.status == "running" and now - last.started_at < timedelta(hours=2):
            return False
        if last.finished_at is not None:
            gap = REFRESH_EVERY if last.status == "ok" else RETRY_AFTER_FAILURE
            if now - last.finished_at < gap:
                return False
    queue.enqueue(db, JOB_KIND, {"run_id": None}, now)
    db.commit()
    return True


def upsert(db: Session, offer, now: datetime) -> Deal:
    deal = db.scalar(select(Deal).where(Deal.source == SOURCE, Deal.external_id == offer.external_id))
    if deal is None:
        deal = Deal(source=SOURCE, external_id=offer.external_id, size_unverified=False)
        db.add(deal)
    deal.retailer, deal.name, deal.brand, deal.ean, deal.category = offer.retailer, offer.name, offer.brand, offer.ean, offer.category
    deal.private_label = offer.private_label
    deal.price_cents, deal.original_price_cents = offer.price_cents, offer.original_price_cents
    deal.savings_pct, deal.savings_cents = offer.savings_pct, offer.savings_cents
    deal.promo_type, deal.promo_text = offer.promo_type, offer.promo_text
    deal.buy_quantity, deal.bundle_price_cents = offer.buy_quantity, offer.bundle_price_cents
    deal.unit_price_cents, deal.unit_basis, deal.quantity_text = offer.unit_price_cents, offer.unit_basis, offer.quantity_text
    deal.loyalty_price_cents, deal.loyalty_program = offer.loyalty_price_cents, offer.loyalty_program
    deal.in_store_only, deal.product_url = offer.in_store_only, offer.product_url
    deal.valid_from, deal.valid_until, deal.last_seen_at = offer.valid_from, offer.valid_until, now
    return deal


def match_score(product: Product, offer) -> float:
    return fuzz.token_set_ratio(normalize_raw(product.name), normalize_raw(f"{offer.brand or ''} {offer.name}"))


def run_chunk(db: Session, run: DealRun, source: PrijsProfeet, eff: Effective, now: datetime) -> bool:
    """Work off the next questions of the plan. True when the run is finished (done or given up)."""
    end = min(len(run.plan), run.cursor + CHUNK)
    skipped = 0
    for item in run.plan[run.cursor:end]:
        try:
            offers = source.search(_params(item, eff))
        except SourceError as exc:
            if exc.stop:
                run.calls += source.calls
                run.status, run.error, run.finished_at = "failed", str(exc)[:1000], now
                db.commit()
                return True
            skipped += 1
            log.warning("deals: question skipped: %s", exc)
            offers = []
        product = db.get(Product, item["product_id"]) if item["kind"] == "product" else None
        for offer in offers:
            if product is None:
                upsert(db, offer, now)  # a category question: everything it returns is of interest
                continue
            score = match_score(product, offer)
            if score < MATCH_MIN:
                continue  # a search hit for another product: not stored
            deal = upsert(db, offer, now)
            if deal.matched_product_id is None or score > (deal.match_score or 0):
                deal.matched_product_id, deal.match_score = product.id, float(score)
    run.calls += source.calls
    run.cursor = end
    run.stats = {**(run.stats or {}), "skipped": (run.stats or {}).get("skipped", 0) + skipped}
    db.commit()
    return run.cursor >= len(run.plan)


# --- what is worth a look ---------------------------------------------------------------

def usual_price(db: Session, product_id: int, today: date) -> tuple[str, int] | None:
    """(basis, cents): the median of your last receipt prices, per kg/l when those exist, else per pack."""
    rows = db.scalars(
        select(PriceObservation)
        .where(
            PriceObservation.product_id == product_id, PriceObservation.source == "receipt",
            PriceObservation.observed_on >= today - timedelta(days=365),
        )
        .order_by(PriceObservation.observed_on.desc(), PriceObservation.id.desc())
        .limit(10)
    ).all()
    values = [comparable(o.price_cents, o.unit_price_cents, o.unit_basis) for o in rows]
    if not values:
        return None
    basis = values[0][0]
    same = [v for b, v in values if b == basis][:5]
    return basis, round(median(same))


def _offer_amount(deal: Deal) -> tuple[int, str] | None:
    if not deal.quantity_text:
        return None
    try:
        return parse_content(deal.quantity_text)
    except ContentError:
        return None


def compare(deal: Deal, product: Product, usual: tuple[str, int]) -> tuple[int, float, bool] | None:
    """(usual price in the terms compared, offer against usual as a fraction, pack size unverified)."""
    basis, cents = usual
    amount = _offer_amount(deal)
    if basis in ("kg", "l"):
        if deal.unit_basis == basis and deal.unit_price_cents:
            offer = deal.unit_price_cents
        elif amount and amount[1] == basis:
            offer = unit_price_from_content(deal.price_cents, amount[0])
        else:
            return None
        return cents, (offer - cents) / cents, False
    if product.pack_content and product.pack_basis and amount and amount[1] == product.pack_basis:
        if 0.95 <= amount[0] / product.pack_content <= 1.05:
            return cents, (deal.price_cents - cents) / cents, False
        usual_unit = unit_price_from_content(cents, product.pack_content)
        offer_unit = unit_price_from_content(deal.price_cents, amount[0])
        return usual_unit, (offer_unit - usual_unit) / usual_unit, False
    return cents, (deal.price_cents - cents) / cents, True  # pack against pack, sizes not known to be equal


# --- what you told the radar ------------------------------------------------------------

REASONS = {
    "brand": "Not my brand",
    "category": "No interest in this category",
    "a_brand": "I do not want A-brands in this category",
    "product": "Not this product",
    "other": "Something else (only this offer)",
}
_KIND = {"brand": "brand", "category": "category", "a_brand": "a_brand", "product": "product", "other": "deal"}


def available_reasons(deal: Deal) -> list[str]:
    """The reasons that make sense for this offer: a brand rule needs a brand, an A-brand rule an A-brand."""
    out = []
    if deal.brand:
        out.append("brand")
    if deal.category:
        out.append("category")
        if deal.private_label is False:
            out.append("a_brand")
    return out + ["product", "other"]


def _rule_value(reason: str, deal: Deal) -> str:
    if reason == "brand":
        return (deal.brand or "").casefold()
    if reason in ("category", "a_brand"):
        return deal.category or ""
    if reason == "product":
        return normalize_raw(deal.name)
    return deal.external_id


def dismiss(db: Session, deal: Deal, reason: str, note: str, now: datetime) -> DealRule:
    """Say an offer is not interesting; the reason decides how far that reaches. Raises ValueError for a reason
    that does not fit this offer."""
    if reason not in available_reasons(deal):
        raise ValueError("That reason does not fit this offer.")
    kind, value = _KIND[reason], _rule_value(reason, deal)
    note = " ".join(note.split())[:300] or None
    rule = db.scalar(select(DealRule).where(DealRule.kind == kind, DealRule.value == value))
    if rule is None:
        rule = DealRule(kind=kind, value=value, label=deal.name[:300], created_at=now)
        db.add(rule)
    if note:
        rule.note = note
    db.commit()
    return rule


def load_rules(db: Session) -> dict[str, set[str]]:
    rules: dict[str, set[str]] = {k: set() for k in ("brand", "category", "a_brand", "product", "deal")}
    for rule in db.scalars(select(DealRule)):
        rules.setdefault(rule.kind, set()).add(rule.value)
    return rules


def blocked(rules: dict[str, set[str]], deal: Deal) -> bool:
    return bool(
        (deal.brand and deal.brand.casefold() in rules["brand"])
        or (deal.category and deal.category in rules["category"])
        or (deal.category in rules["a_brand"] and deal.private_label is False)
        or normalize_raw(deal.name) in rules["product"]
        or deal.external_id in rules["deal"]
    )


def evaluate(db: Session, eff: Effective, today: date) -> dict[str, int]:
    watched = {c for c in eff.deals_categories.split(",") if c}
    rules = load_rules(db)
    tracked = {t.product.id: t for t in tracked_products(db, today)}
    counts = {"mine": 0, "notable": 0}
    for deal in db.scalars(select(Deal).where((Deal.valid_until.is_(None)) | (Deal.valid_until >= today))):
        deal.relevance = deal.reason = deal.usual_price_cents = deal.vs_usual_pct = None
        deal.size_unverified = False
        t = tracked.get(deal.matched_product_id) if deal.matched_product_id else None
        if t is not None and (t.visits >= MIN_VISITS or t.on_cupboard):
            usual = usual_price(db, t.product.id, today)
            result = compare(deal, t.product, usual) if usual else None
            if result is not None:
                deal.usual_price_cents, deal.vs_usual_pct, deal.size_unverified = result[0], result[1], result[2]
                if result[1] <= -eff.deals_mine_min_pct / 100:
                    deal.relevance = "mine"
                    deal.reason = f"{round(-result[1] * 100)}% below what you usually pay"
            elif usual is None and deal.savings_pct and eff.deals_notable_min_pct <= deal.savings_pct <= MAX_PLAUSIBLE_PCT:
                deal.relevance, deal.reason = "mine", f"{round(deal.savings_pct)}% off, no price history for this product yet"
        if (
            deal.relevance is None and deal.category in watched and deal.savings_pct
            and eff.deals_notable_min_pct <= deal.savings_pct <= MAX_PLAUSIBLE_PCT
            and (deal.savings_cents or 0) >= round(eff.deals_notable_min_eur * 100)
        ):
            deal.relevance, deal.reason = "notable", f"{round(deal.savings_pct)}% off, saves EUR {(deal.savings_cents or 0) / 100:.2f}"
        if deal.relevance and blocked(rules, deal):
            deal.relevance = deal.reason = None
        if deal.relevance:
            counts[deal.relevance] += 1
    db.commit()
    return counts


def purge_old(db: Session, today: date) -> int:
    old = db.scalars(select(Deal).where(Deal.valid_until < today - PURGE_AFTER)).all()
    for deal in old:
        db.delete(deal)
    db.commit()
    return len(old)


# --- alerts through Home Assistant -------------------------------------------------------

def ha_configured(settings: Settings) -> bool:
    return bool(settings.ha_url and settings.ha_token and re.match(r"^notify\.[a-z0-9_]+$", settings.ha_notify_service))


def ha_post(url: str, token: str, payload: dict) -> tuple[int, bytes]:
    import json

    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("HA_URL must be an http(s) address")
    cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
    conn = cls(parts.hostname, parts.port, timeout=10)
    try:
        conn.request(
            "POST", parts.path or "/", body=json.dumps(payload),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        response = conn.getresponse()
        return response.status, response.read(100_000)
    finally:
        conn.close()


def _line(deal: Deal, stores: dict[str, str]) -> str:
    price = f"EUR {deal.price_cents / 100:.2f}"
    extra = f" (buy {deal.buy_quantity})" if deal.buy_quantity else ""
    return f"{stores.get(deal.retailer, deal.retailer)}: {deal.name}, {price}{extra}. {deal.reason}."


def build_message(deals: list[Deal], stores: dict[str, str]) -> tuple[str, str]:
    mine = [d for d in deals if d.relevance == "mine"]
    notable = [d for d in deals if d.relevance == "notable"]
    parts = []
    if mine:
        parts.append(f"{len(mine)} for you")
    if notable:
        parts.append(f"{len(notable)} worth a look")
    title = "Deals: " + ", ".join(parts)
    ordered = mine + notable
    lines = [_line(d, stores) for d in ordered[:MAX_ALERT_LINES]]
    if len(ordered) > MAX_ALERT_LINES:
        lines.append(f"+{len(ordered) - MAX_ALERT_LINES} more on the Deals page.")
    lines.append(f"Source: {SOURCE_NAME}")
    return title, "\n".join(lines)


def pending_alerts(db: Session, today: date) -> list[Deal]:
    return list(
        db.scalars(
            select(Deal)
            .where(
                Deal.relevance.is_not(None), Deal.alerted_at.is_(None),
                (Deal.valid_until.is_(None)) | (Deal.valid_until >= today),
                (Deal.valid_from.is_(None)) | (Deal.valid_from <= today),
            )
            .order_by(Deal.relevance, Deal.vs_usual_pct, Deal.id)  # "mine" sorts before "notable"
        )
    )


def send_alerts(
    db: Session, settings: Settings, eff: Effective, today: date, now: datetime, post: HaPost = ha_post
) -> int:
    """One notification for everything new. Returns how many deals it covered (0: nothing new or not sent)."""
    if eff.deals_alerts != "on" or not ha_configured(settings):
        return 0
    deals = pending_alerts(db, today)
    if not deals:
        return 0
    stores = {s.chain: s.name for s in db.scalars(select(Store))}
    title, message = build_message(deals, stores)
    url = settings.ha_url.rstrip("/") + "/api/services/notify/" + settings.ha_notify_service.split(".", 1)[1]
    try:
        status, _ = post(url, settings.ha_token.get_secret_value(), {"title": title, "message": message})
    except (OSError, http.client.HTTPException, ValueError) as exc:
        log.warning("deals: notification failed (%s); will retry on the next refresh", type(exc).__name__)
        return 0
    if status != 200:
        log.warning("deals: Home Assistant answered %s; will retry on the next refresh", status)
        return 0
    for deal in deals:
        deal.alerted_at = now
    db.commit()
    return len(deals)


def finish_run(
    db: Session, run: DealRun, settings: Settings, eff: Effective, today: date, now: datetime, post: HaPost = ha_post
) -> None:
    counts = evaluate(db, eff, today)
    purged = purge_old(db, today)
    sent = send_alerts(db, settings, eff, today, now, post) if run.status != "failed" else 0
    if run.status != "failed":
        run.status = "ok"
    run.finished_at = now
    run.stats = {**(run.stats or {}), **counts, "alerted": sent, "purged": purged}
    db.commit()
    log.info("deals: run %s %s (%s calls) %s", run.id, run.status, run.calls, run.stats)
