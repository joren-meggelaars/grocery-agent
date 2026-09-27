from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from grocery.db.models import Deal, Product, ShoppingListItem
from grocery.products.normalize import product_key
from grocery.shopping import service
from tests.conftest import csrf_of, login
from tests.test_analytics_routes import make_receipt, today_local
from tests.fakes import ean13
from tests.test_cupboard import TODAY, link, obs, product

EAN = ean13("871040001234")
EAN2 = ean13("871040001235")


@pytest.fixture
def env(client, db):
    return client.app.state.settings, db


def deal_for(db, product_id, price_cents=99, retailer="lidl", reason="20% off", **kw):
    row = Deal(
        source="prijsprofeet", external_id=f"d{product_id}-{retailer}", retailer=retailer, name="Deal", price_cents=price_cents,
        matched_product_id=product_id, relevance="mine", reason=reason, size_unverified=False,
        last_seen_at=datetime(TODAY.year, TODAY.month, TODAY.day, 12, tzinfo=timezone.utc), unit_basis=kw.get("unit_basis"),
        # No expiry by default, so the deal is valid whichever "today" the test compares it against
        # (a route test uses the real date; the pure-service tests below pass the fixed TODAY).
        valid_from=kw.get("valid_from"), valid_until=kw.get("valid_until"),
    )
    db.add(row)
    db.commit()
    return row


# --- advice ---------------------------------------------------------------------------------

def test_an_active_deal_wins_over_price_history(env):
    settings, db = env
    p = product(db, "Halfvolle melk 1L", cat="Zuivel & eieren")
    make_receipt(db, "plus", TODAY, [("item", 129, "Zuivel & eieren", "Halfvolle melk 1L")])
    deal_for(db, p.id, price_cents=99, retailer="jumbo")
    advice = service.build_advice(db, [p.id], TODAY)[p.id]
    assert (advice.kind, advice.store, advice.price_cents, advice.source) == ("deal", "Jumbo", 99, "PrijsProfeet")


def test_the_deal_marks_lidl_when_lidl_has_it(env):
    settings, db = env
    p = product(db)
    deal_for(db, p.id, retailer="lidl")
    advice = service.build_advice(db, [p.id], TODAY)[p.id]
    assert advice.lidl_tip is True


def test_an_expired_or_future_deal_is_ignored(env):
    settings, db = env
    p = product(db)
    obs(db, p, "plus", 129, TODAY)
    deal_for(db, p.id, valid_until=TODAY - timedelta(days=1))
    deal_for(db, p.id, valid_from=TODAY + timedelta(days=1), retailer="aldi")
    advice = service.build_advice(db, [p.id], TODAY)[p.id]
    assert advice.kind == "price"


def test_without_a_deal_the_cheapest_alternative_is_used(env):
    settings, db = env
    p = product(db, "Kaas", cat="Zuivel & eieren")
    obs(db, p, "plus", 249, TODAY)
    obs(db, p, "lidl", 199, TODAY - timedelta(days=1), source="shelf")
    advice = service.build_advice(db, [p.id], TODAY)[p.id]
    assert (advice.kind, advice.store, advice.price_cents, advice.lidl_tip) == ("price", "Lidl", 199, True)
    assert "vs Plus" in advice.note


def test_without_a_cheaper_alternative_the_usual_store_is_shown(env):
    settings, db = env
    p = product(db, "Kaas", cat="Zuivel & eieren")
    obs(db, p, "plus", 249, TODAY)
    advice = service.build_advice(db, [p.id], TODAY)[p.id]
    assert (advice.kind, advice.store, advice.price_cents) == ("price", "Plus", 249)
    assert "usual" in advice.note


def test_no_history_and_no_deal_gives_no_advice(env):
    settings, db = env
    p = product(db, "Nooit gekocht")
    assert service.build_advice(db, [p.id], TODAY)[p.id] is None


def test_build_advice_handles_an_empty_list(env):
    settings, db = env
    assert service.build_advice(db, [], TODAY) == {}


# --- adding by name ----------------------------------------------------------------------------

def test_add_by_name_matches_an_existing_product(env):
    settings, db = env
    product(db, "Halfvolle melk 1L", cat="Zuivel & eieren")
    item = service.add_by_name(db, "halfvolle melk 1l", "")
    assert item.product_id is not None and item.raw_name == "halfvolle melk 1l"


def test_add_by_name_falls_back_to_free_text_when_nothing_matches(env):
    settings, db = env
    item = service.add_by_name(db, "Iets heel unieks 12345", "")
    assert item.product_id is None and item.raw_name == "Iets heel unieks 12345"


def test_add_by_name_stores_a_trimmed_note(env):
    settings, db = env
    item = service.add_by_name(db, "Wasmiddel", "  2 pakken  ")
    assert item.note == "2 pakken"


def test_add_by_name_rejects_an_empty_name(env):
    from grocery.receipts.service import ConfirmError

    settings, db = env
    with pytest.raises(ConfirmError, match="Enter a product name"):
        service.add_by_name(db, "   ", "")


# --- adding by product / barcode ------------------------------------------------------------------

def test_add_by_product_creates_an_item(env):
    settings, db = env
    p = product(db)
    item = service.add_by_product(db, p.id)
    assert item.product_id == p.id and item.raw_name == p.name


def test_add_by_product_does_not_duplicate_an_unbought_item(env):
    settings, db = env
    p = product(db)
    first = service.add_by_product(db, p.id)
    second = service.add_by_product(db, p.id)
    assert first.id == second.id
    assert db.scalar(select(ShoppingListItem.id).where(ShoppingListItem.product_id == p.id).limit(2).offset(1)) is None


def test_add_by_product_allows_a_new_row_once_the_old_one_is_bought(env):
    settings, db = env
    p = product(db)
    first = service.add_by_product(db, p.id)
    service.toggle_bought(db, first.id)
    second = service.add_by_product(db, p.id)
    assert second.id != first.id


def test_add_by_product_unknown_id(env):
    from grocery.receipts.service import ConfirmError

    settings, db = env
    with pytest.raises(ConfirmError, match="Unknown product"):
        service.add_by_product(db, 999999)


def test_add_by_ean_uses_the_linked_product(env):
    settings, db = env
    p = product(db)
    link(db, p, EAN)
    item = service.add_by_ean(db, EAN)
    assert item.product_id == p.id


def test_add_by_ean_rejects_bad_or_unknown_barcodes(env):
    settings, db = env
    with pytest.raises(service.InvalidBarcode, match="not valid"):
        service.add_by_ean(db, "123")
    with pytest.raises(service.InvalidBarcode, match="Unknown barcode"):
        service.add_by_ean(db, EAN)


# --- changing --------------------------------------------------------------------------------

def test_toggle_bought_marks_and_unmarks(env):
    settings, db = env
    item = service.add_by_name(db, "Brood", "")
    assert item.bought_at is None
    service.toggle_bought(db, item.id)
    db.refresh(item)
    assert item.bought_at is not None
    service.toggle_bought(db, item.id)
    db.refresh(item)
    assert item.bought_at is None


def test_toggle_bought_on_a_missing_item_does_nothing(env):
    settings, db = env
    service.toggle_bought(db, 999999)  # must not raise


def test_remove_deletes_the_item(env):
    settings, db = env
    item = service.add_by_name(db, "Brood", "")
    service.remove(db, item.id)
    assert db.get(ShoppingListItem, item.id) is None


def test_clear_bought_removes_only_bought_items(env):
    settings, db = env
    a = service.add_by_name(db, "Brood", "")
    b = service.add_by_name(db, "Melk", "")
    service.toggle_bought(db, a.id)
    assert service.clear_bought(db) == 1
    assert db.get(ShoppingListItem, a.id) is None and db.get(ShoppingListItem, b.id) is not None


def test_pending_count_excludes_bought_items(env):
    settings, db = env
    a = service.add_by_name(db, "Brood", "")
    service.add_by_name(db, "Melk", "")
    assert service.pending_count(db) == 2
    service.toggle_bought(db, a.id)
    assert service.pending_count(db) == 1


def test_rows_lists_unbought_first_then_bought(env):
    settings, db = env
    a = service.add_by_name(db, "Brood", "")
    b = service.add_by_name(db, "Melk", "")
    service.toggle_bought(db, a.id)
    ordered = [r.item.id for r in service.rows(db, TODAY)]
    assert ordered == [b.id, a.id]


# --- pages -------------------------------------------------------------------------------------

@pytest.fixture
def session(client):
    login(client)
    return client, csrf_of(client)


def test_the_page_requires_a_session(client):
    assert client.get("/shopping-list", headers={"accept": "text/html"}).status_code == 303


def test_adding_by_name_and_showing_advice(session, db):
    client, token = session
    p = product(db, "Kaas", cat="Zuivel & eieren")
    obs(db, p, "plus", 249, TODAY)
    obs(db, p, "lidl", 199, TODAY - timedelta(days=1), source="shelf")
    resp = client.post("/shopping-list/add", data={"csrf_token": token, "product": "Kaas"})
    assert resp.status_code == 303
    page = client.get("/shopping-list").text
    assert "Kaas" in page and "Lidl" in page and "1.99" in page and "savings tip" in page


def test_a_deal_shows_the_source_name(session, db):
    client, token = session
    p = product(db, "Wasmiddel", cat="Huishouden & verzorging")
    deal_for(db, p.id, price_cents=399, retailer="aldi", reason="33% off")
    client.post("/shopping-list/add", data={"csrf_token": token, "product": "Wasmiddel"})
    page = client.get("/shopping-list").text
    assert "Aldi" in page and "3.99" in page and "Source: PrijsProfeet" in page


def test_an_empty_name_shows_the_error_and_does_not_add_a_row(session):
    client, token = session
    resp = client.post("/shopping-list/add", data={"csrf_token": token, "product": ""})
    assert resp.status_code == 303
    page = client.get(resp.headers["location"]).text
    assert "Enter a product name" in page


def test_add_needs_the_csrf_token(client):
    login(client)
    assert client.post("/shopping-list/add", data={"product": "Brood"}).status_code == 403


def test_add_product_button_from_another_page_returns_there(session, db):
    client, token = session
    p = product(db)
    resp = client.post("/shopping-list/add-product", data={
        "csrf_token": token, "product_id": p.id, "next": "/cupboard",
    })
    assert resp.status_code == 303 and resp.headers["location"] == "/cupboard"
    assert p.name in client.get("/shopping-list").text


def test_add_product_button_rejects_an_open_redirect(session, db):
    client, token = session
    p = product(db)
    resp = client.post("/shopping-list/add-product", data={
        "csrf_token": token, "product_id": p.id, "next": "https://evil.example",
    })
    assert resp.headers["location"] == "/shopping-list"


def test_regulars_and_cupboard_pages_have_an_add_to_shopping_list_button(session, db):
    client, token = session
    p = product(db, "Kaas", cat="Zuivel & eieren")
    make_receipt(db, "plus", today_local(client), [("item", 249, "Zuivel & eieren", "Kaas")])
    from grocery.db.models import CupboardItem

    db.add(CupboardItem(product_id=p.id))
    db.commit()
    assert 'action="/shopping-list/add-product"' in client.get("/regulars?period=all").text
    assert 'action="/shopping-list/add-product"' in client.get("/cupboard").text


def test_mark_bought_and_undo(session, db):
    client, token = session
    item = service.add_by_name(db, "Brood", "")
    resp = client.post(f"/shopping-list/items/{item.id}/bought", data={"csrf_token": token})
    assert resp.status_code == 303
    page = client.get("/shopping-list").text
    assert 'class="shopping-item bought"' in page or "shopping-item  bought" in page or " bought" in page
    client.post(f"/shopping-list/items/{item.id}/bought", data={"csrf_token": token})
    db.refresh(item)
    assert item.bought_at is None


def test_remove_item_via_the_page(session, db):
    client, token = session
    item = service.add_by_name(db, "Brood", "")
    resp = client.post(f"/shopping-list/items/{item.id}/remove", data={"csrf_token": token})
    assert resp.status_code == 303
    assert "Brood" not in client.get("/shopping-list").text


def test_clear_bought_via_the_page(session, db):
    client, token = session
    a = service.add_by_name(db, "Brood", "")
    service.add_by_name(db, "Melk", "")
    service.toggle_bought(db, a.id)
    resp = client.post("/shopping-list/clear-bought", data={"csrf_token": token})
    assert resp.status_code == 303
    page = client.get("/shopping-list").text
    assert "Brood" not in page and "Melk" in page


def test_mutations_need_the_csrf_token(session, db):
    client, token = session
    item = service.add_by_name(db, "Brood", "")
    assert client.post(f"/shopping-list/items/{item.id}/bought").status_code == 403
    assert client.post(f"/shopping-list/items/{item.id}/remove").status_code == 403
    assert client.post("/shopping-list/clear-bought").status_code == 403


# --- scan API ------------------------------------------------------------------------------------

def test_scan_api_adds_a_known_barcode(session, db):
    client, token = session
    p = product(db)
    link(db, p, EAN)
    resp = client.post("/api/shopping-list/scan", data={"ean": EAN}, headers={"X-CSRF-Token": token})
    assert resp.status_code == 200 and resp.json() == {"name": p.name}
    assert p.name in client.get("/shopping-list").text


def test_scan_api_rejects_an_unknown_barcode(session):
    client, token = session
    resp = client.post("/api/shopping-list/scan", data={"ean": EAN}, headers={"X-CSRF-Token": token})
    assert resp.status_code == 400 and "Unknown barcode" in resp.json()["error"]


def test_scan_api_needs_a_session_and_the_csrf_header(client, session):
    anon = client.__class__(client.app, base_url="https://testserver", client=("172.30.90.1", 1),
                            headers={"Origin": "https://testserver"})
    assert anon.post("/api/shopping-list/scan", data={"ean": EAN}).status_code == 401
    c, token = session
    assert c.post("/api/shopping-list/scan", data={"ean": EAN}).status_code == 403


# --- navigation and home tile --------------------------------------------------------------------

def test_the_menu_and_home_tile_link_to_the_shopping_list(session, db):
    client, token = session
    assert 'href="/shopping-list"' in client.get("/").text
    service.add_by_name(db, "Brood", "")
    assert "1 to buy" in client.get("/").text


# --- Home Assistant: keep a sensor in sync so a zone-enter automation can remind you -----------

def test_pending_names_excludes_bought_items(env):
    settings, db = env
    a = service.add_by_name(db, "Brood", "")
    service.add_by_name(db, "Melk", "")
    service.toggle_bought(db, a.id)
    assert service.pending_names(db) == ["Melk"]


def test_push_state_sends_the_pending_items(env):
    settings, db = env
    service.add_by_name(db, "Brood", "")
    service.add_by_name(db, "Melk", "")
    calls = []

    def post(url, token, payload):
        calls.append((url, token, payload))
        return 200, b"{}"

    from pydantic import SecretStr

    settings.ha_url, settings.ha_token = "http://ha.local:8123", SecretStr("tok")
    assert service.push_state(db, settings, post) is True
    url, token, payload = calls[0]
    assert url == "http://ha.local:8123/api/states/sensor.grocery_shopping_list" and token == "tok"
    assert payload["state"] == "2" and payload["attributes"]["items"] == ["Brood", "Melk"]
    assert payload["attributes"]["text"] == "Brood, Melk"


def test_push_state_with_an_empty_list_still_pushes_a_zero(env):
    settings, db = env
    from pydantic import SecretStr

    settings.ha_url, settings.ha_token = "http://ha.local:8123", SecretStr("tok")
    sent = {}

    def post(url, token, payload):
        sent.update(payload)
        return 200, b"{}"

    assert service.push_state(db, settings, post) is True
    assert sent["state"] == "0" and sent["attributes"]["items"] == []


def test_push_state_without_ha_configured_does_nothing(env):
    settings, db = env
    calls = []
    assert service.push_state(db, settings, lambda *a: calls.append(a) or (200, b"{}")) is False
    assert calls == []


def test_push_state_handles_a_failed_or_unreachable_ha(env):
    settings, db = env
    from pydantic import SecretStr

    settings.ha_url, settings.ha_token = "http://ha.local:8123", SecretStr("tok")
    assert service.push_state(db, settings, lambda *a: (500, b"nope")) is False

    def down(*a):
        raise OSError("unreachable")

    assert service.push_state(db, settings, down) is False


def test_queue_ha_sync_is_queued_once(env):
    from grocery.db.models import Job

    settings, db = env
    assert service.queue_ha_sync(db) is True
    assert service.queue_ha_sync(db) is False
    assert db.scalar(select(Job.id).where(Job.kind == service.HA_SYNC_JOB)) is not None


def test_the_worker_pushes_state_when_the_sync_job_runs(env):
    from grocery.db.models import Job
    from grocery.jobs.handlers import process_one
    from pydantic import SecretStr

    settings, db = env
    settings.ha_url, settings.ha_token = "http://ha.local:8123", SecretStr("tok")
    service.add_by_name(db, "Brood", "")
    service.queue_ha_sync(db)
    sent = []
    process_one(db, settings, lambda s: None, ha_post=lambda *a: sent.append(a) or (200, b"{}"))
    assert len(sent) == 1 and sent[0][2]["attributes"]["items"] == ["Brood"]
    assert db.scalar(select(func.count()).select_from(Job).where(Job.status != "done")) == 0


def test_a_mutation_via_the_page_queues_a_sync_job(session, db):
    from grocery.db.models import Job

    client, token = session
    client.post("/shopping-list/add", data={"csrf_token": token, "product": "Brood"})
    assert db.scalar(select(Job.id).where(Job.kind == service.HA_SYNC_JOB)) is not None


def test_the_product_and_note_fields_switch_off_ios_suggestions(session):
    client, token = session
    page = client.get("/shopping-list").text
    for name in ("product", "note"):
        field = page.split(f'name="{name}"', 1)[1].split(">", 1)[0]
        for attr in ('autocomplete="off"', 'autocorrect="off"', 'spellcheck="false"', "data-1p-ignore", 'data-lpignore="true"'):
            assert attr in field, (name, attr)
