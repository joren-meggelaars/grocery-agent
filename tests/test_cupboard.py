from datetime import date, timedelta

import pytest
from sqlalchemy import select

from grocery.cupboard import service
from grocery.cupboard.service import InvalidBarcode
from grocery.db.base import utcnow
from grocery.db.models import (
    Category,
    CupboardItem,
    CupboardScan,
    Job,
    PriceObservation,
    Product,
    ProductEan,
    Store,
)
from grocery.jobs.handlers import process_one
from grocery.products.normalize import product_key
from grocery.receipts.service import ConfirmError
from tests.conftest import csrf_of, login
from tests.fakes import ean13, off_down, off_found, off_missing
from tests.test_analytics_routes import make_receipt

EAN = ean13("871040001234")
EAN2 = ean13("871040001235")
TODAY = date(2026, 9, 20)


@pytest.fixture
def env(client, db):
    return client.app.state.settings, db


def product(db, name="Halfvolle melk 1L", cat=None):
    cats = {c.name: c.id for c in db.scalars(select(Category))}
    p = Product(name=name, name_key=product_key(name), category_id=cats.get(cat))
    db.add(p)
    db.commit()
    return p


def link(db, p, ean=EAN):
    db.add(ProductEan(ean=ean, product_id=p.id, source="scan"))
    db.commit()


def run_jobs(env, off=None):
    settings, db = env
    off = off or off_found()
    while process_one(db, settings, lambda s: None, None, off_fetch=off):
        pass
    db.expire_all()
    return off


def obs(db, p, chain, price, day=TODAY, source="receipt", unit=None, basis=None):
    store = db.scalar(select(Store).where(Store.chain == chain))
    db.add(PriceObservation(product_id=p.id, store_id=store.id, price_cents=price, unit_price_cents=unit,
                            unit_basis=basis, observed_on=day, source=source))
    db.commit()


# --- scanning ---------------------------------------------------------------

@pytest.mark.parametrize("bad", ["", None, "abc", "4006381333932", "12345"])
def test_invalid_barcodes_are_rejected(env, bad):
    with pytest.raises(InvalidBarcode):
        service.scan(env[1], bad)
    assert env[1].scalar(select(CupboardScan)) is None


def test_a_known_barcode_is_added_at_once_and_a_second_scan_is_a_no_op(env):
    _, db = env
    p = product(db)
    link(db, p)
    first = service.scan(db, EAN)
    assert (first.kind, first.name, first.product_id) == ("added", "Halfvolle melk 1L", p.id)
    item = db.scalar(select(CupboardItem))
    assert item.product_id == p.id and item.heavy_use is False
    again = service.scan(db, EAN, now=utcnow() + timedelta(minutes=5))
    assert again.kind == "already" and len(db.scalars(select(CupboardItem)).all()) == 1
    assert item.last_scanned_at > item.added_at


def test_an_unknown_barcode_waits_for_a_name_and_queues_one_lookup(env):
    _, db = env
    outcome = service.scan(db, EAN)
    assert outcome.kind == "unknown" and outcome.scan_id
    pending = db.scalar(select(CupboardScan))
    assert (pending.ean, pending.status, pending.seen_count) == (EAN, "lookup", 1)
    jobs = db.scalars(select(Job)).all()
    assert [(j.kind, j.payload) for j in jobs] == [("lookup_ean", {"ean": EAN})]

    again = service.scan(db, EAN)
    assert again.kind == "unknown_again"
    assert db.scalar(select(CupboardScan)).seen_count == 2 and len(db.scalars(select(Job)).all()) == 1


def test_barcodes_with_spaces_are_accepted(env):
    assert service.scan(env[1], f" {EAN[:4]} {EAN[4:]} ").kind == "unknown"


# --- worker lookup ----------------------------------------------------------

def test_the_worker_fills_in_the_open_food_facts_name(env):
    _, db = env
    service.scan(db, EAN)
    off = run_jobs(env, off_found(name="Pindakaas", brand="Calvé", quantity="350 g"))
    pending = db.scalar(select(CupboardScan))
    assert (pending.status, pending.off_name) == ("needs_name", "Pindakaas, Calvé, 350 g")
    assert [c[0] for c in off.calls] == [EAN]
    assert db.scalar(select(Job)).status == "done"


@pytest.mark.parametrize("off", [off_missing(), off_down()])
def test_unknown_or_unreachable_open_food_facts_still_lets_you_name_it(env, off):
    _, db = env
    service.scan(db, EAN)
    run_jobs(env, off)
    pending = db.scalar(select(CupboardScan))
    assert pending.status == "needs_name" and pending.off_name is None


def test_a_crashing_lookup_does_not_leave_the_barcode_stuck(env):
    _, db = env
    service.scan(db, EAN)

    def boom(ean, ua):
        raise RuntimeError("boom")

    run_jobs(env, boom)
    assert db.scalar(select(CupboardScan)).status == "needs_name"


def test_a_lookup_for_a_barcode_that_was_already_named_is_harmless(env):
    _, db = env
    outcome = service.scan(db, EAN)
    service.discard_scan(db, outcome.scan_id)
    run_jobs(env)
    assert db.scalar(select(Job)).status == "done"


# --- naming -----------------------------------------------------------------

def test_naming_creates_the_product_the_barcode_link_and_the_list_entry(env):
    _, db = env
    outcome = service.scan(db, EAN)
    cat = db.scalar(select(Category).where(Category.name == "Zuivel & eieren"))
    item = service.name_scan(db, outcome.scan_id, "Halfvolle melk 1L", cat.id, heavy_use=True)
    assert item.heavy_use is True and item.product.name == "Halfvolle melk 1L" and item.product.category_id == cat.id
    assert db.get(ProductEan, EAN).product_id == item.product_id
    assert db.scalar(select(CupboardScan)) is None
    # the next scan of that barcode is recognised without any naming
    assert service.scan(db, EAN).kind == "already"


def test_naming_reuses_an_existing_product_of_the_same_name(env):
    _, db = env
    existing = product(db, "Halfvolle melk 1L")
    outcome = service.scan(db, EAN)
    service.name_scan(db, outcome.scan_id, "halfvolle melk 1l", None, False)
    assert len(db.scalars(select(Product)).all()) == 1
    assert db.get(ProductEan, EAN).product_id == existing.id


def test_two_barcodes_can_share_one_product(env):
    _, db = env
    first, second = service.scan(db, EAN), service.scan(db, EAN2)
    service.name_scan(db, first.scan_id, "Pindakaas", None, False)
    service.name_scan(db, second.scan_id, "Pindakaas", None, True)
    assert len(db.scalars(select(Product)).all()) == 1
    assert len(db.scalars(select(CupboardItem)).all()) == 1 and db.scalar(select(CupboardItem)).heavy_use is True


def test_naming_errors(env):
    _, db = env
    outcome = service.scan(db, EAN)
    with pytest.raises(ConfirmError, match="product name"):
        service.name_scan(db, outcome.scan_id, "   ", None, False)
    with pytest.raises(ConfirmError, match="already handled"):
        service.name_scan(db, 9999, "X", None, False)
    assert db.scalar(select(CupboardScan)) is not None  # nothing was lost


def test_add_without_a_barcode_and_the_heavy_flag_never_gets_lost(env):
    _, db = env
    service.add_product(db, "Appels", None, heavy_use=True)
    again = service.add_product(db, "appels", None, heavy_use=False)  # same product, already marked
    assert again.heavy_use is True and len(db.scalars(select(CupboardItem)).all()) == 1
    with pytest.raises(ConfirmError):
        service.add_product(db, "", None, False)


def test_toggle_remove_and_discard(env):
    _, db = env
    item = service.add_product(db, "Pasta", None, False)
    service.set_heavy(db, item.id, True)
    assert db.get(CupboardItem, item.id).heavy_use is True
    service.set_heavy(db, item.id, False)
    assert db.get(CupboardItem, item.id).heavy_use is False
    service.remove_item(db, item.id)
    assert db.scalar(select(CupboardItem)) is None and db.scalar(select(Product)) is not None  # product stays
    service.remove_item(db, 9999)
    service.set_heavy(db, 9999, True)  # unknown ids are ignored
    service.discard_scan(db, 9999)


def test_the_name_suggestion_uses_the_product_part_or_an_existing_product(env):
    _, db = env
    p = CupboardScan(ean=EAN, status="needs_name", off_name="Halfvolle melk, Campina, 1 L")
    assert service.suggestion(db, p) == ("Halfvolle melk", None)
    existing = product(db, "Halfvolle melk", cat="Zuivel & eieren")
    name, category = service.suggestion(db, p)
    assert name == "Halfvolle melk" and category == existing.category_id
    assert service.suggestion(db, CupboardScan(ean=EAN2, status="needs_name", off_name=None)) == ("", None)


# --- the list ---------------------------------------------------------------

def test_an_empty_cupboard_has_no_rows(env):
    assert service.cupboard_rows(env[1], TODAY) == []


def test_rows_are_sorted_heavy_first_then_by_how_often_bought(env):
    _, db = env
    melk, pasta, rijst = product(db, "Melk"), product(db, "Pasta"), product(db, "Rijst")
    for p, heavy in ((melk, False), (pasta, False), (rijst, True)):
        db.add(CupboardItem(product_id=p.id, heavy_use=heavy))
    db.commit()
    make_receipt(db, "plus", TODAY - timedelta(days=3), [("item", 129, None, "Melk")])
    make_receipt(db, "plus", TODAY - timedelta(days=10), [("item", 129, None, "Melk"), ("item", 129, None, "Melk")])
    make_receipt(db, "plus", TODAY - timedelta(days=5), [("item", 99, None, "Pasta")])
    make_receipt(db, "plus", TODAY - timedelta(days=200), [("item", 99, None, "Rijst")])  # too old to count
    rows = service.cupboard_rows(db, TODAY)
    assert [(r.name, r.times_bought) for r in rows] == [("Rijst", 0), ("Melk", 2), ("Pasta", 1)]


def test_each_row_shows_the_last_price_and_a_cheaper_sighting(env):
    _, db = env
    melk = product(db, "Halfvolle melk 1L", cat="Zuivel & eieren")
    db.add(CupboardItem(product_id=melk.id))
    db.commit()
    obs(db, melk, "plus", 129, TODAY - timedelta(days=8))
    obs(db, melk, "plus", 135, TODAY - timedelta(days=2))  # the latest one counts
    obs(db, melk, "lidl", 99, TODAY - timedelta(days=1), source="shelf")
    row = service.cupboard_rows(db, TODAY)[0]
    assert (row.last.store, row.last.value, row.basis, row.paid) == ("Plus", 135, "pack", True)  # paid, not the shelf price
    assert (row.cheaper.store, row.cheaper.value, row.category) == ("Lidl", 99, "Zuivel & eieren")


def test_without_a_receipt_a_shelf_sighting_stands_in_and_is_not_called_paid(env):
    _, db = env
    p = product(db, "Pasta")
    db.add(CupboardItem(product_id=p.id))
    db.commit()
    obs(db, p, "lidl", 89, TODAY - timedelta(days=1), source="shelf")
    row = service.cupboard_rows(db, TODAY)[0]
    assert (row.last.store, row.paid) == ("Lidl", False)


def test_a_product_never_seen_at_a_price_has_no_price_or_suggestion(env):
    _, db = env
    p = product(db, "Onbekend")
    db.add(CupboardItem(product_id=p.id))
    db.commit()
    row = service.cupboard_rows(db, TODAY)[0]
    assert row.last is None and row.cheaper is None and row.basis is None


def test_a_product_that_is_already_the_cheapest_shows_no_suggestion(env):
    _, db = env
    p = product(db, "Kip")
    db.add(CupboardItem(product_id=p.id))
    db.commit()
    obs(db, p, "turkish", 742, TODAY - timedelta(days=1), unit=849, basis="kg")
    obs(db, p, "plus", 1199, TODAY - timedelta(days=3), unit=1199, basis="kg")
    row = service.cupboard_rows(db, TODAY)[0]
    assert row.last.store == "Turkish supermarket" and row.cheaper is None


def test_on_cupboard_helper(env):
    _, db = env
    p = product(db)
    assert service.on_cupboard(db, p.id) is False and service.on_cupboard(db, None) is False
    db.add(CupboardItem(product_id=p.id))
    db.commit()
    assert service.on_cupboard(db, p.id) is True


# --- API and pages ----------------------------------------------------------

@pytest.fixture
def session(client):
    login(client)
    return client, csrf_of(client)


def api_scan(client, token, ean, header=True):
    return client.post("/api/cupboard/scan", data={"ean": ean}, headers={"X-CSRF-Token": token} if header else {})


def test_scan_api_needs_a_session_and_the_csrf_header(client, session):
    anon = client.__class__(client.app, base_url="https://testserver", client=("172.30.90.1", 1),
                            headers={"Origin": "https://testserver"})
    assert api_scan(anon, "x", EAN).status_code == 401
    c, token = session
    assert api_scan(c, token, EAN, header=False).status_code == 403


def test_scan_api_results(session, db):
    client, token = session
    p = product(db)
    link(db, p)
    assert api_scan(client, token, EAN).json() == {"result": "added", "name": "Halfvolle melk 1L"}
    assert api_scan(client, token, EAN).json()["result"] == "already"
    unknown = api_scan(client, token, EAN2).json()
    assert unknown["result"] == "unknown" and unknown["name"] is None
    bad = api_scan(client, token, "4006381333932")
    assert bad.status_code == 400 and "not valid" in bad.json()["error"]


def test_the_cupboard_page_shows_prices_stars_and_a_cheaper_hint(session, db):
    client, _ = session
    p = product(db, "Halfvolle melk 1L", cat="Zuivel & eieren")
    db.add(CupboardItem(product_id=p.id, heavy_use=True))
    db.commit()
    today = date.today()
    obs(db, p, "plus", 129, today - timedelta(days=4))
    obs(db, p, "lidl", 99, today - timedelta(days=1), source="shelf")
    page = client.get("/cupboard").text
    assert "Halfvolle melk 1L" in page and "used a lot" in page and 'aria-pressed="true"' in page
    assert "Last paid" in page and "€1.29" in page and "at Plus" in page and "Seen cheaper:" in page and "Lidl" in page and "€0.99" in page


def test_empty_cupboard_explains_what_to_do(session):
    client, _ = session
    page = client.get("/cupboard").text
    assert "Nothing here yet" in page and "Scan products at home" in page
    assert "scanned product" not in page  # no "Name N scanned products" button when nothing is waiting


def test_toggle_remove_and_add_from_the_page(session, db):
    client, token = session
    item = service.add_product(db, "Pasta", None, False)
    post = lambda path, **data: client.post(path, data={"csrf_token": token, **data})  # noqa: E731
    assert post(f"/cupboard/items/{item.id}/heavy", value="1").status_code == 303
    db.expire_all()
    assert db.get(CupboardItem, item.id).heavy_use is True
    assert post(f"/cupboard/items/{item.id}/heavy", value="0").status_code == 303
    db.expire_all()
    assert db.get(CupboardItem, item.id).heavy_use is False
    assert post("/cupboard/add", product="Rijst", heavy="on").status_code == 303
    assert "Rijst" in client.get("/cupboard").text
    assert post(f"/cupboard/items/{item.id}/remove").status_code == 303
    assert post("/cupboard/items/9999/remove").status_code == 404
    assert client.post(f"/cupboard/items/{item.id}/heavy", data={"value": "1"}).status_code == 403  # no token


def test_error_messages_come_only_from_a_known_list(session):
    client, token = session
    resp = client.post("/cupboard/add", data={"csrf_token": token, "product": "  "})
    assert resp.status_code == 303
    assert "Enter the product name." in client.get(resp.headers["location"]).text
    assert "phishing" not in client.get("/cupboard?error=phishing+text").text


def test_naming_flow_on_the_page(session, db):
    client, token = session
    api_scan(client, token, EAN)
    while process_one(db, client.app.state.settings, lambda s: None, None, off_fetch=off_found(name="Pindakaas", brand="Calvé", quantity="350 g")):
        pass
    db.expire_all()
    page = client.get("/cupboard/name").text
    assert f"Barcode {EAN}" in page and "Open Food Facts: Pindakaas, Calvé, 350 g" in page and 'value="Pindakaas"' in page
    scan_id = db.scalar(select(CupboardScan)).id
    resp = client.post(f"/cupboard/name/{scan_id}", data={"csrf_token": token, "product": "Pindakaas", "category": "", "heavy": "on"})
    assert resp.status_code == 303
    assert "Nothing waiting for a name" in client.get("/cupboard/name").text
    assert "Pindakaas" in client.get("/cupboard").text and "Name 1" not in client.get("/cupboard").text


def test_naming_with_an_empty_name_shows_an_error_and_keeps_the_barcode(session, db):
    client, token = session
    api_scan(client, token, EAN)
    scan_id = db.scalar(select(CupboardScan)).id
    resp = client.post(f"/cupboard/name/{scan_id}", data={"csrf_token": token, "product": ""})
    assert "Enter the product name." in client.get(resp.headers["location"]).text
    assert db.scalar(select(CupboardScan)) is not None


def test_discarding_a_barcode(session, db):
    client, token = session
    api_scan(client, token, EAN)
    scan_id = db.scalar(select(CupboardScan)).id
    assert client.post(f"/cupboard/name/{scan_id}/discard", data={"csrf_token": token}).status_code == 303
    db.expire_all()
    assert db.scalar(select(CupboardScan)) is None


def test_the_list_links_to_naming_when_barcodes_are_waiting(session, db):
    client, token = session
    api_scan(client, token, EAN)
    api_scan(client, token, EAN2)
    assert "Name 2 scanned products" in client.get("/cupboard").text


def test_names_from_the_database_and_open_food_facts_are_escaped(session, db):
    client, token = session
    service.add_product(db, "<script>alert(1)</script>", None, False)
    api_scan(client, token, EAN)
    while process_one(db, client.app.state.settings, lambda s: None, None, off_fetch=off_found(name="<img src=x onerror=alert(1)>", brand="B", quantity="1")):
        pass
    for path in ("/cupboard", "/cupboard/name"):
        page = client.get(path).text
        assert "<script>alert" not in page and "<img src=x" not in page and "&lt;" in page


def test_the_scan_page_and_home_link(session):
    client, _ = session
    page = client.get("/cupboard/scan").text
    assert "Start camera" in page and "cupboard_scan.js" in page and "zxing" in page
    assert 'href="/cupboard"' in client.get("/").text


def test_the_shop_scan_says_when_you_already_have_the_product(session, db):
    from tests.test_capture_routes import form_data, new_read_capture

    client, token = session
    cid = new_read_capture(client, token, db)
    client.post(f"/capture/{cid}/confirm", data=form_data(client))
    assert "You have this at home" not in client.get(f"/capture/{cid}").text
    p = db.scalar(select(Product))
    db.add(CupboardItem(product_id=p.id))
    db.commit()
    assert "You have this at home" in client.get(f"/capture/{cid}").text


def test_the_scanner_supports_continuous_mode_without_repeating_a_held_barcode():
    from pathlib import Path

    js = (Path(__file__).resolve().parent.parent / "src/grocery/web/static/js/scanner.js").read_text(encoding="utf-8")
    assert "options.continuous" in js and "lastAt = now" in js  # seeing a code again refreshes its timer
