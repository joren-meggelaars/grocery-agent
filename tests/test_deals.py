import json
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from grocery.db.models import CupboardItem, Deal, DealRun, Job, Setting
from grocery.deals import service
from grocery.deals.client import MIN_INTERVAL, PrijsProfeet, SourceError, parse_offer
from grocery.jobs.handlers import process_one
from grocery.settings_store import effective
from tests.conftest import csrf_of, login
from tests.test_analytics_routes import make_receipt
from tests.test_cupboard import obs, product

TODAY = date(2026, 9, 24)
NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)


def raw(**kw) -> dict:
    base = dict(
        product_id="jumbo_1", name="Robijn Wasmiddel Color", brand="Robijn", ean=None, price=5.25, original_price=10.49,
        quantity=None, unit=None, unit_price=None, product_url="https://www.jumbo.com/p/1", retailer="jumbo",
        unified_category="huishouden", is_promotional=True, promotional_keywords=["1+1 gratis"],
        promotion_type="one_plus_one", savings_percentage=50.0, savings_amount=5.24, multi_buy_quantity=2,
        multi_buy_price=10.49, valid_from="2026-09-23", valid_until="2026-10-06", loyalty_price=None,
        loyalty_program=None, in_store_only=None,
    )
    base.update(kw)
    return base


class FakeSource:
    """Answers search questions from a dict: product query text -> results, or (category, retailer) -> results."""

    def __init__(self, answers=None, status=200):
        self.answers, self.status, self.calls = answers or {}, status, []

    def __call__(self, path, params, ua, key):
        q = dict(params)
        self.calls.append((path, params, ua, key))
        if self.status != 200:
            return self.status, b"{}"
        which = q["q"] if q["q"] != "*" else (q["category"], q["retailer"])
        return 200, json.dumps({"results": self.answers.get(which, [])}).encode()


@pytest.fixture
def env(client, db):
    return client.app.state.settings, db


def eff_of(env):
    settings, db = env
    return effective(db, settings)


def set_setting(db, key, value):
    db.add(Setting(key=f"app.{key}", value=value))
    db.commit()


def bought(db, name, prices, chain="plus", cat="Zuivel & eieren", times=None):
    """A product you buy: `times` receipts (default one per price) with a receipt price each."""
    p = product(db, name, cat=cat)
    for i, cents in enumerate(prices):
        day = TODAY - timedelta(days=10 * (i + 1))
        make_receipt(db, chain, day, [("item", cents, cat, name)])
        obs(db, p, chain, cents, day)
    return p


def refresh(env, source, now=NOW):
    """A whole refresh in one go, the way the worker does it in chunks."""
    settings, db = env
    eff = eff_of(env)
    run = service.start_run(db, eff, TODAY, now)
    ps = PrijsProfeet("ua", fetch=source, sleep=lambda s: None)
    while not service.run_chunk(db, run, ps, eff, now):
        pass
    service.finish_run(db, run, settings, eff, TODAY, now, lambda *a: (200, b"{}"))
    db.expire_all()
    return run


# --- reading the source ------------------------------------------------------------

def test_an_offer_is_read_with_its_promotion_and_validity():
    o = parse_offer(raw())
    assert (o.retailer, o.price_cents, o.original_price_cents, o.savings_pct) == ("jumbo", 525, 1049, 50.0)
    assert (o.buy_quantity, o.promo_text, o.valid_until) == (2, "1+1 gratis", date(2026, 10, 6))


def test_aldi_publishes_the_bundle_total_as_the_price_so_it_is_divided_by_the_bundle_size():
    o = parse_offer(raw(retailer="aldi", price=0.79, multi_buy_quantity=2, original_price=0.89, promotional_keywords=[]))
    assert o.price_cents == 40 and o.buy_quantity == 2 and o.promo_text == "2 for EUR 10.49"


@pytest.mark.parametrize("bad", [
    raw(retailer="albert_heijn"), raw(price=0), raw(price=None), raw(name=""), raw(product_id=""),
    raw(is_promotional=False), "not a dict", None,
])
def test_unusable_results_are_dropped(bad):
    assert parse_offer(bad) is None


def test_only_http_links_barcodes_and_kg_l_units_are_kept():
    o = parse_offer(raw(product_url="javascript:alert(1)", ean="87104000", unit="stuk", unit_price=1.0))
    assert o.product_url is None and o.ean == "87104000" and o.unit_basis is None
    assert parse_offer(raw(ean="12ab")).ean is None
    ok = parse_offer(raw(unit="kg", unit_price=4.98, quantity="500 g"))
    assert (ok.unit_basis, ok.unit_price_cents, ok.quantity_text) == ("kg", 498, "500 g")


def test_a_price_that_is_not_below_the_was_price_has_no_was_price():
    assert parse_offer(raw(original_price=5.25)).original_price_cents is None


# --- asking politely ----------------------------------------------------------------

def test_calls_are_spaced_by_the_minimum_interval():
    now, slept = [0.0], []

    def sleep(s):
        slept.append(s)
        now[0] += s

    ps = PrijsProfeet("ua", fetch=FakeSource({"x": []}), sleep=sleep, clock=lambda: now[0])
    for _ in range(3):
        ps.search([("q", "x")])
    assert len(slept) == 2 and all(s == pytest.approx(MIN_INTERVAL) for s in slept) and ps.calls == 3


def test_the_user_agent_and_the_optional_key_are_sent():
    fetch = FakeSource({"x": []})
    PrijsProfeet("MyApp/1.0 (self-hosted)", api_key="k123", fetch=fetch, sleep=lambda s: None).search([("q", "x")])
    assert fetch.calls[0][2:] == ("MyApp/1.0 (self-hosted)", "k123")


@pytest.mark.parametrize("status,stop", [(403, True), (429, True), (500, True), (503, True), (422, False), (404, False)])
def test_source_errors_say_whether_to_stop_asking(status, stop):
    with pytest.raises(SourceError) as exc:
        PrijsProfeet("ua", fetch=FakeSource(status=status), sleep=lambda s: None).search([("q", "x")])
    assert exc.value.stop is stop


@pytest.mark.parametrize("body", [b"not json", b'{"nope": 1}', b'{"results": "x"}'])
def test_an_unexpected_answer_stops_the_run(body):
    with pytest.raises(SourceError) as exc:
        PrijsProfeet("ua", fetch=lambda *a: (200, body), sleep=lambda s: None).search([("q", "x")])
    assert exc.value.stop


def test_a_network_error_stops_the_run():
    def down(*a):
        raise OSError("unreachable")

    with pytest.raises(SourceError) as exc:
        PrijsProfeet("ua", fetch=down, sleep=lambda s: None).search([("q", "x")])
    assert exc.value.stop


# --- what to ask -----------------------------------------------------------------------

def test_query_text_drops_sizes():
    assert service.query_text("Halfvolle melk 1L") == "halfvolle melk"
    assert service.query_text("Spaghetti 500g") == "spaghetti"
    assert service.query_text("Cola 6 x 33 cl") == "cola"


def test_the_plan_asks_about_your_products_and_the_watched_categories(env):
    settings, db = env
    bought(db, "Halfvolle melk 1L", [129, 125])
    bought(db, "Pindakaas", [299])
    heavy = product(db, "Kaas plakken", cat="Zuivel & eieren")
    db.add(CupboardItem(product_id=heavy.id, heavy_use=True))
    db.commit()
    plan = service.build_plan(db, eff_of(env), TODAY)
    products = [p["q"] for p in plan if p["kind"] == "product"]
    assert products == ["kaas plakken", "halfvolle melk", "pindakaas"]  # heavy use first, then most receipts
    categories = {(p["category"], p["retailer"]) for p in plan if p["kind"] == "category"}
    assert categories == {(c, r) for c in ("huishouden", "drogisterij") for r in ("jumbo", "plus", "aldi", "lidl")}


def test_without_watched_categories_only_products_are_asked(env):
    settings, db = env
    set_setting(db, "deals_categories", "")
    bought(db, "Pindakaas", [299])
    assert {p["kind"] for p in service.build_plan(db, eff_of(env), TODAY)} == {"product"}


def test_a_refresh_is_worked_off_in_chunks_and_stores_only_matching_offers(env):
    settings, db = env
    bought(db, "Halfvolle melk", [129, 125])
    source = FakeSource({
        "halfvolle melk": [
            raw(product_id="j1", name="Campina Halfvolle melk", brand="Campina", price=0.99, original_price=1.29,
                unified_category="zuivel-eieren"),
            raw(product_id="j2", name="Hokkaido pompoen", brand=None, unified_category="groente-fruit"),
        ],
    })
    run = refresh(env, source)
    assert run.status == "ok" and run.calls == len(run.plan)
    names = set(db.scalars(select(Deal.name)))
    assert "Campina Halfvolle melk" in names and "Hokkaido pompoen" not in names
    assert db.scalar(select(Deal).where(Deal.external_id == "j1")).matched_product_id is not None


def test_offers_of_a_category_question_are_all_kept(env):
    settings, db = env
    source = FakeSource({("huishouden", "lidl"): [raw(product_id="l1", retailer="lidl", name="Ariel capsules")]})
    refresh(env, source)
    assert db.scalar(select(Deal).where(Deal.external_id == "l1")).matched_product_id is None


def test_a_blocked_source_marks_the_run_failed_and_keeps_what_was_known(env):
    settings, db = env
    bought(db, "Pindakaas", [299])
    run = refresh(env, FakeSource(status=403))
    assert run.status == "failed" and "403" in run.error and run.calls == 1


def test_a_question_the_source_rejects_is_skipped_and_the_run_goes_on(env):
    settings, db = env
    bought(db, "Pindakaas", [299])
    bought(db, "Melk", [129])
    calls = []

    def fetch(path, params, ua, key):
        calls.append(params)
        return (422, b"{}") if dict(params)["q"] == "pindakaas" else (200, b'{"results": []}')

    run = refresh(env, fetch)
    assert run.status == "ok" and run.stats["skipped"] == 1 and len(calls) == len(run.plan)


# --- what is worth a look ---------------------------------------------------------------

def rate(env, offers, **setup):
    settings, db = env
    for o in offers:
        service.upsert(db, parse_offer(o), NOW)
    db.commit()
    return db


def test_a_product_you_buy_that_is_clearly_cheaper_than_usual_is_for_you(env):
    settings, db = env
    p = bought(db, "Halfvolle melk", [129, 129])
    d = service.upsert(db, parse_offer(raw(product_id="m1", name="Halfvolle melk", brand=None, price=0.99, original_price=1.29,
                                           unified_category="zuivel-eieren", multi_buy_quantity=None, promotional_keywords=[])), NOW)
    d.matched_product_id = p.id
    db.commit()
    service.evaluate(db, eff_of(env), TODAY)
    db.refresh(d)
    assert d.relevance == "mine" and d.usual_price_cents == 129 and d.vs_usual_pct == pytest.approx(-0.2326, abs=0.001)
    assert "23% below what you usually pay" in d.reason and d.size_unverified is True  # pack against pack, sizes unknown


def test_a_small_difference_is_not_worth_an_alert(env):
    settings, db = env
    p = bought(db, "Halfvolle melk", [129, 129])
    d = service.upsert(db, parse_offer(raw(product_id="m1", name="Halfvolle melk", price=1.20, unified_category="zuivel-eieren")), NOW)
    d.matched_product_id = p.id
    db.commit()
    service.evaluate(db, eff_of(env), TODAY)
    assert db.get(Deal, d.id).relevance is None


def test_a_product_bought_once_and_not_at_home_does_not_count_as_one_you_buy(env):
    settings, db = env
    p = bought(db, "Halfvolle melk", [129])
    d = service.upsert(db, parse_offer(raw(product_id="m1", name="Halfvolle melk", price=0.50, unified_category="zuivel-eieren")), NOW)
    d.matched_product_id = p.id
    db.commit()
    service.evaluate(db, eff_of(env), TODAY)
    assert db.get(Deal, d.id).relevance is None
    db.add(CupboardItem(product_id=p.id))
    db.commit()
    service.evaluate(db, eff_of(env), TODAY)
    assert db.get(Deal, d.id).relevance == "mine"


def test_without_price_history_a_big_folder_discount_is_enough(env):
    settings, db = env
    p = product(db, "Pindakaas", cat="Houdbaar")
    db.add(CupboardItem(product_id=p.id))
    db.commit()
    d = service.upsert(db, parse_offer(raw(product_id="p1", name="Pindakaas", price=1.50, original_price=3.00,
                                           savings_percentage=50.0, unified_category="ontbijt")), NOW)
    d.matched_product_id = p.id
    db.commit()
    service.evaluate(db, eff_of(env), TODAY)
    d = db.get(Deal, d.id)
    assert d.relevance == "mine" and "no price history" in d.reason


def test_prices_per_kg_are_compared_per_kg_and_the_pack_size_is_then_verified(env):
    settings, db = env
    p = product(db, "Goudse kaas", cat="Zuivel & eieren")
    make_receipt(db, "plus", TODAY - timedelta(days=5), [("item", 499, "Zuivel & eieren", "Goudse kaas")])
    make_receipt(db, "plus", TODAY - timedelta(days=9), [("item", 499, "Zuivel & eieren", "Goudse kaas")])
    obs(db, p, "plus", 249, TODAY - timedelta(days=5), unit=998, basis="kg")
    d = service.upsert(db, parse_offer(raw(product_id="k1", name="Goudse kaas", price=1.99, original_price=2.49, unit="kg",
                                           unit_price=7.96, savings_percentage=20.0, unified_category="kaas")), NOW)
    d.matched_product_id = p.id
    db.commit()
    service.evaluate(db, eff_of(env), TODAY)
    d = db.get(Deal, d.id)
    assert d.relevance == "mine" and d.size_unverified is False and d.usual_price_cents == 998


def test_a_different_pack_size_is_compared_per_kg_using_the_content_you_saved(env):
    from grocery.prices.observations import set_pack_content

    settings, db = env
    p = bought(db, "Goudse kaas", [249, 249])
    set_pack_content(db, p, 500, "kg")  # 500 g at EUR 2.49 = 4.98 per kg
    db.commit()
    bigger = service.upsert(db, parse_offer(raw(product_id="k1", name="Goudse kaas", price=4.00, quantity="1 kg", multi_buy_quantity=None,
                                                promotional_keywords=[], unified_category="kaas")), NOW)  # 4.00/kg: cheaper
    bigger.matched_product_id = p.id
    db.commit()
    service.evaluate(db, eff_of(env), TODAY)
    bigger = db.get(Deal, bigger.id)
    assert bigger.relevance == "mine" and bigger.size_unverified is False and bigger.usual_price_cents == 498


def test_a_big_discount_in_a_watched_category_is_worth_a_look_even_for_a_product_you_never_bought(env):
    settings, db = env
    d = service.upsert(db, parse_offer(raw(product_id="w1", name="Ariel capsules", price=6.00, original_price=12.00,
                                           savings_percentage=50.0, unified_category="huishouden")), NOW)
    db.commit()
    counts = service.evaluate(db, eff_of(env), TODAY)
    d = db.get(Deal, d.id)
    assert d.relevance == "notable" and counts == {"mine": 0, "notable": 1} and "50% off, saves EUR 6.00" in d.reason


@pytest.mark.parametrize("override,why", [
    (dict(unified_category="snoep-koek-chips"), "not a watched category"),
    (dict(price=8.50, original_price=10.00, savings_percentage=15.0), "discount too small"),
    (dict(price=0.50, original_price=1.20, savings_percentage=58.0), "saves less than EUR 2"),
    (dict(price=1.00, original_price=50.00, savings_percentage=98.0), "implausibly big: a data quirk"),
    (dict(valid_until="2026-09-20"), "expired"),
])
def test_offers_that_are_not_notable(env, override, why):
    settings, db = env
    fields = dict(product_id="w1", name="Ariel capsules", price=6.00, original_price=12.00, savings_percentage=50.0,
                  unified_category="huishouden")
    d = service.upsert(db, parse_offer(raw(**{**fields, **override})), NOW)
    db.commit()
    service.evaluate(db, eff_of(env), TODAY)
    assert db.get(Deal, d.id).relevance is None, why


def test_the_thresholds_come_from_the_settings(env):
    settings, db = env
    set_setting(db, "deals_notable_min_pct", "60")
    d = service.upsert(db, parse_offer(raw(product_id="w1", name="Ariel capsules", price=6.00, original_price=12.00,
                                           savings_percentage=50.0, unified_category="huishouden")), NOW)
    db.commit()
    service.evaluate(db, eff_of(env), TODAY)
    assert db.get(Deal, d.id).relevance is None


def test_old_offers_are_purged_after_two_weeks(env):
    settings, db = env
    old = service.upsert(db, parse_offer(raw(product_id="o1", valid_until="2026-09-01")), NOW)
    fresh = service.upsert(db, parse_offer(raw(product_id="o2", valid_until="2026-09-20")), NOW)
    db.commit()
    assert service.purge_old(db, TODAY) == 1
    assert db.get(Deal, fresh.id) is not None and db.scalar(select(Deal).where(Deal.external_id == "o1")) is None


# --- alerts through Home Assistant --------------------------------------------------------

@pytest.fixture
def ha_env(make_client, make_settings):
    from pydantic import SecretStr

    client = make_client(ha_url="http://ha.local:8123", ha_token=SecretStr("tok"), ha_notify_service="notify.mobile_app_phone")
    with client.app.state.session_factory() as db:
        yield client, db


def two_deals(db):
    p = bought(db, "Halfvolle melk", [129, 129])
    a = service.upsert(db, parse_offer(raw(product_id="m1", name="Halfvolle melk", price=0.99, unified_category="zuivel-eieren",
                                           multi_buy_quantity=None, promotional_keywords=[])), NOW)
    a.matched_product_id = p.id
    service.upsert(db, parse_offer(raw(product_id="w1", name="Ariel capsules", price=6.00, original_price=12.00,
                                       savings_percentage=50.0, unified_category="huishouden", retailer="lidl")), NOW)
    db.commit()


def test_one_notification_covers_everything_new_and_names_the_source(ha_env):
    client, db = ha_env
    settings = client.app.state.settings
    two_deals(db)
    eff = effective(db, settings)
    service.evaluate(db, eff, TODAY)
    sent = []
    n = service.send_alerts(db, settings, eff, TODAY, NOW, lambda url, token, payload: sent.append((url, token, payload)) or (200, b"[]"))
    assert n == 2 and len(sent) == 1
    url, token, payload = sent[0]
    assert url == "http://ha.local:8123/api/services/notify/mobile_app_phone" and token == "tok"
    assert payload["title"] == "Deals: 1 for you, 1 worth a look"
    lines = payload["message"].splitlines()
    assert "Halfvolle melk" in lines[0] and "Ariel capsules" in lines[1]  # yours first
    assert lines[-1] == "Source: PrijsProfeet (prijsprofeet.nl)"


def test_a_deal_is_alerted_only_once(ha_env):
    client, db = ha_env
    settings = client.app.state.settings
    two_deals(db)
    eff = effective(db, settings)
    service.evaluate(db, eff, TODAY)
    post = lambda *a: (200, b"[]")  # noqa: E731
    assert service.send_alerts(db, settings, eff, TODAY, NOW, post) == 2
    assert service.send_alerts(db, settings, eff, TODAY, NOW, post) == 0


def test_a_failed_notification_is_retried_on_the_next_refresh(ha_env):
    client, db = ha_env
    settings = client.app.state.settings
    two_deals(db)
    eff = effective(db, settings)
    service.evaluate(db, eff, TODAY)
    assert service.send_alerts(db, settings, eff, TODAY, NOW, lambda *a: (401, b"unauthorized")) == 0

    def down(*a):
        raise OSError("unreachable")

    assert service.send_alerts(db, settings, eff, TODAY, NOW, down) == 0
    assert db.scalar(select(Deal).where(Deal.alerted_at.is_not(None))) is None
    assert service.send_alerts(db, settings, eff, TODAY, NOW, lambda *a: (200, b"[]")) == 2


def test_no_alert_without_home_assistant(env):
    settings, db = env
    two_deals(db)
    eff = eff_of(env)
    service.evaluate(db, eff, TODAY)
    assert not service.ha_configured(settings)
    assert service.send_alerts(db, settings, eff, TODAY, NOW, lambda *a: (200, b"")) == 0


def test_no_alert_when_alerts_are_switched_off(ha_env):
    client, db = ha_env
    set_setting(db, "deals_alerts", "off")
    two_deals(db)
    eff = effective(db, client.app.state.settings)
    service.evaluate(db, eff, TODAY)
    assert service.ha_configured(client.app.state.settings)
    assert service.send_alerts(db, client.app.state.settings, eff, TODAY, NOW, lambda *a: (200, b"")) == 0


@pytest.mark.parametrize("bad", ["notify.phone; drop", "mobile_app_phone", "light.kitchen", "notify.", ""])
def test_the_notify_service_name_is_checked(make_settings, bad):
    from pydantic import SecretStr

    s = make_settings(ha_url="http://ha.local", ha_token=SecretStr("t"), ha_notify_service=bad)
    assert not service.ha_configured(s)


def test_a_long_list_is_cut_off_with_a_pointer_to_the_deals_page(env):
    settings, db = env
    for i in range(11):
        service.upsert(db, parse_offer(raw(product_id=f"w{i}", name=f"Waspoeder {i}", price=6.00, original_price=12.00,
                                           savings_percentage=50.0, unified_category="huishouden")), NOW)
    db.commit()
    service.evaluate(db, eff_of(env), TODAY)
    deals = service.pending_alerts(db, TODAY)
    title, message = service.build_message(deals, {"jumbo": "Jumbo"})
    assert title == "Deals: 11 worth a look" and "+3 more on the Deals page." in message and message.count("\n") == 9


# --- when a refresh runs ---------------------------------------------------------------------

def test_a_refresh_is_queued_once_and_then_only_when_due(env):
    settings, db = env
    eff = eff_of(env)
    assert service.ensure_refresh_queued(db, eff, NOW) is True
    assert service.ensure_refresh_queued(db, eff, NOW) is False  # already queued
    db.execute(Job.__table__.delete())
    db.add(DealRun(started_at=NOW - timedelta(hours=3), finished_at=NOW - timedelta(hours=3), status="ok", plan=[], cursor=0, calls=0))
    db.commit()
    assert service.ensure_refresh_queued(db, eff, NOW) is False  # done 3 hours ago
    assert service.ensure_refresh_queued(db, eff, NOW + timedelta(hours=18)) is True  # a night later
    assert service.ensure_refresh_queued(db, eff, NOW, force=True) is False  # still one queued


def test_a_failed_refresh_is_retried_after_six_hours_not_every_hour(env):
    settings, db = env
    eff = eff_of(env)
    db.add(DealRun(started_at=NOW, finished_at=NOW, status="failed", plan=[], cursor=0, calls=1, error="403"))
    db.commit()
    assert service.ensure_refresh_queued(db, eff, NOW + timedelta(hours=2)) is False
    assert service.ensure_refresh_queued(db, eff, NOW + timedelta(hours=7)) is True


def test_nothing_is_queued_when_the_radar_is_off(env):
    settings, db = env
    set_setting(db, "deals_enabled", "off")
    assert service.ensure_refresh_queued(db, eff_of(env), NOW, force=True) is False


def run_jobs(env, source, sent=None):
    settings, db = env
    while process_one(db, settings, lambda s: None, NOW, deals_fetch=source, sleep=lambda s: None,
                      ha_post=lambda url, token, payload: (sent.append(payload) if sent is not None else None) or (200, b"[]")):
        pass
    db.expire_all()


def test_the_worker_runs_a_long_refresh_in_several_jobs(env):
    settings, db = env
    for i in range(23):
        bought(db, f"Product{chr(97 + i)}", [100, 100])
    source = FakeSource()
    service.ensure_refresh_queued(db, eff_of(env), NOW)
    run_jobs(env, source)
    run = db.scalar(select(DealRun))
    plan = len(run.plan)
    assert plan > service.CHUNK * 2 and run.status == "ok" and run.calls == plan == len(source.calls)
    assert db.scalar(select(Job).where(Job.status != "done")) is None
    assert len(db.scalars(select(Job).where(Job.kind == service.JOB_KIND)).all()) == -(-plan // service.CHUNK)


def test_the_worker_end_to_end_alerts_once(ha_env):
    client, db = ha_env
    settings = client.app.state.settings
    p = bought(db, "Halfvolle melk", [129, 129])
    source = FakeSource({"halfvolle melk": [raw(product_id="m1", name="Halfvolle melk", brand=None, price=0.99, original_price=1.29,
                                                 unified_category="zuivel-eieren", multi_buy_quantity=None, promotional_keywords=[])]})
    sent = []
    service.ensure_refresh_queued(db, effective(db, settings), NOW)
    run_jobs((settings, db), source, sent)
    assert len(sent) == 1 and "Halfvolle melk" in sent[0]["message"]
    service.ensure_refresh_queued(db, effective(db, settings), NOW + timedelta(hours=25))
    run_jobs((settings, db), source, sent)
    assert len(sent) == 1  # the same deal is not announced again


def test_a_disabled_radar_completes_the_job_without_asking_the_source(env):
    settings, db = env
    service.ensure_refresh_queued(db, eff_of(env), NOW)
    set_setting(db, "deals_enabled", "off")
    source = FakeSource()
    run_jobs(env, source)
    assert source.calls == [] and db.scalar(select(DealRun)) is None


# --- the Deals page ----------------------------------------------------------------------

def page_setup(client, db):
    login(client)
    p = bought(db, "Halfvolle melk", [129, 129])
    d = service.upsert(db, parse_offer(raw(product_id="m1", name="Halfvolle melk", price=0.99, original_price=1.29,
                                           unified_category="zuivel-eieren", multi_buy_quantity=None, promotional_keywords=[])), NOW)
    d.matched_product_id = p.id
    service.upsert(db, parse_offer(raw(product_id="w1", name='<img src=x onerror=alert(1)> Ariel', price=6.00, original_price=12.00,
                                       savings_percentage=50.0, unified_category="huishouden", retailer="lidl")), NOW)
    db.commit()
    service.evaluate(db, effective(db, client.app.state.settings), date.today())


def test_the_deals_page_shows_both_kinds_the_reason_and_the_source(client, db):
    login(client)
    db.add(Setting(key="app.cycle_start_day", value="1"))
    db.commit()
    page = client.get("/deals").text
    assert "Nothing this week is clearly cheaper" in page and "Not refreshed yet" in page
    assert "prijsprofeet.nl" in page and "PrijsProfeet" in page


def test_deals_are_listed_with_prices_escaped_and_safe_links(client, db):
    page_setup(client, db)
    # the fixtures are valid until 2026-10-06; only check when that is still in the future, else use the untouched page
    from grocery.db.models import Deal as D

    for d in db.scalars(select(D)):
        d.valid_until = date.today() + timedelta(days=3)
        d.valid_from = date.today() - timedelta(days=1)
    db.commit()
    service.evaluate(db, effective(db, client.app.state.settings), date.today())
    page = client.get("/deals").text
    assert "For you" in page and "Halfvolle melk" in page and "23% below what you usually pay" in page
    assert "Worth a look" in page and "<img src=x" not in page and "&lt;img src=x" in page
    assert 'href="https://www.jumbo.com/p/1"' in page and "rel=\"noopener noreferrer\"" in page
    assert "Check that the pack size" in page


def test_deals_need_a_session(client):
    assert client.get("/deals", headers={"accept": "text/html"}).status_code == 303
    assert client.post("/deals/refresh").status_code == 401


def test_refresh_now_queues_a_job_once_and_respects_the_cooldown(client, db):
    login(client)
    token = csrf_of(client)
    resp = client.post("/deals/refresh", data={"csrf_token": token})
    assert resp.status_code == 303 and resp.headers["location"] == "/deals?refresh=queued"
    assert client.post("/deals/refresh", data={"csrf_token": token}).headers["location"] == "/deals?refresh=busy"
    assert "Refresh started" in client.get("/deals?refresh=queued").text
    db.execute(Job.__table__.delete())
    db.add(DealRun(started_at=datetime.now(timezone.utc), finished_at=datetime.now(timezone.utc), status="ok", plan=[], cursor=0, calls=0))
    db.commit()
    assert client.post("/deals/refresh", data={"csrf_token": token}).headers["location"] == "/deals?refresh=recent"


def test_refresh_now_needs_the_csrf_token(client):
    login(client)
    assert client.post("/deals/refresh").status_code == 403


def test_the_menu_and_home_page_link_to_the_deals(client):
    login(client)
    assert 'href="/deals"' in client.get("/").text
