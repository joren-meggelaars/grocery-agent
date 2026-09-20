from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from grocery.db.base import utcnow
from grocery.db.models import ShelfCapture
from grocery.jobs.handlers import process_one
from grocery.uploads.storage import purge_due_files
from tests.conftest import csrf_of, login
from tests.fakes import FakeShelfReader, ean13, failed, jpeg_bytes, off_found, shelf_label

EAN = ean13("871040001234")


@pytest.fixture
def session(client):
    login(client)
    return client, csrf_of(client)


def upload(client, token, *, uuid=None, ean=EAN, store="plus", photo=None, header=True, when=None):
    photo = jpeg_bytes() if photo is None else photo
    return client.post(
        "/api/captures",
        data={"client_uuid": uuid or str(uuid4()), "ean": ean, "store": store,
              "captured_at": when or utcnow().isoformat()},
        files={"photo": ("label.jpg", photo, "image/jpeg")},
        headers={"X-CSRF-Token": token} if header else {},
    )


def read(client, db, label=None, off=None, reader=None):
    reader = reader or FakeShelfReader(label if label is not None else shelf_label())
    while process_one(db, client.app.state.settings, lambda s: None, None,
                      shelf_reader_factory=lambda s: reader, off_fetch=off or off_found()):
        pass
    db.expire_all()


def form_data(client, **over):
    data = {
        "csrf_token": csrf_of(client), "store": "plus", "ean": EAN, "product": "Halfvolle melk 1L", "category": "",
        "price": "1,29", "unit_price": "1,29", "unit_basis": "l", "promo_kind": "", "promo_text": "",
        "effective_price": "", "valid_from": "", "valid_until": "",
    }
    data.update(over)
    return data


def new_read_capture(client, token, db, store="plus", label=None):
    cid = upload(client, token, store=store).json()["id"]
    read(client, db, label)
    return cid


# --- phone API --------------------------------------------------------------

def test_csrf_endpoint_gives_a_token_to_signed_in_users_only(client):
    assert client.get("/api/csrf", headers={"accept": "application/json"}).status_code == 401
    login(client)
    token = client.get("/api/csrf").json()["token"]
    assert token == csrf_of(client)


def test_upload_creates_a_capture_and_is_idempotent(session):
    client, token = session
    uid = str(uuid4())
    first = upload(client, token, uuid=uid)
    assert first.status_code == 201 and first.json()["status"] == "extracting" and first.json()["created"] is True
    again = upload(client, token, uuid=uid)
    assert again.status_code == 200 and again.json()["id"] == first.json()["id"] and again.json()["created"] is False


def test_upload_needs_the_csrf_header(session):
    client, token = session
    assert upload(client, token, header=False).status_code == 403
    assert upload(client, "wrong-token").status_code == 403


def test_anonymous_upload_is_rejected(client):
    assert upload(client, "x").status_code == 401


@pytest.mark.parametrize(
    "kw,message",
    [(dict(ean="4006381333932"), "barcode"), (dict(store="nope"), "store"), (dict(photo=b"<html>"), "JPEG, PNG or WebP")],
)
def test_upload_errors_are_json_messages(session, kw, message):
    client, token = session
    resp = upload(client, token, **kw)
    assert resp.status_code == 400 and message in resp.json()["error"]


def test_upload_without_a_photo_is_a_400(session):
    client, token = session
    resp = client.post("/api/captures", data={"client_uuid": str(uuid4()), "store": "plus"},
                       headers={"X-CSRF-Token": token})
    assert resp.status_code == 400 and "photo" in resp.json()["error"]


def test_queued_captures_arriving_late_keep_their_original_time(session, db):
    client, token = session
    when = (utcnow() - timedelta(hours=2)).isoformat()
    cid = upload(client, token, when=when).json()["id"]
    db.expire_all()
    assert abs((db.get(ShelfCapture, cid).captured_at - (utcnow() - timedelta(hours=2))).total_seconds()) < 5


# --- the scan page ----------------------------------------------------------

def test_scan_page_is_identical_for_everyone_so_it_can_be_cached_offline(client, make_client):
    login(client)
    page = client.get("/capture", headers={"accept": "text/html"})
    assert page.status_code == 200 and "Scan barcode" in page.text and "Plus" in page.text
    assert 'name="csrf-token"' not in page.text and "joren" not in page.text
    other = make_client(app=client.app)
    login(other)
    assert other.get("/capture", headers={"accept": "text/html"}).text == page.text


def test_scan_page_says_the_barcode_is_optional_and_puts_the_photo_first(session):
    client, _ = session
    html = client.get("/capture").text
    assert "(optional)" in html and "A photo of the shelf label is all you need" in html
    assert html.index('id="label-photo"') < html.index('id="ean"')


def test_a_photo_only_capture_works_end_to_end(session, db):
    """No barcode at all: the product is recognised from the label text and the confirmed name."""
    client, token = session
    first = upload(client, token, ean="").json()["id"]
    read(client, db, label=shelf_label(product_name="Halfvolle melk 1L"), off=off_found())
    page = client.get(f"/capture/{first}").text
    assert 'value="Halfvolle melk 1L"' in page and 'name="ean" class="big" inputmode="numeric" value=""' in page
    assert client.post(f"/capture/{first}/confirm", data=form_data(client, ean="")).status_code == 303

    second = upload(client, token, ean="", store="lidl").json()["id"]
    read(client, db, label=shelf_label(product_name="halfvolle melk 1l", price_cents=99, unit_price_cents=99))
    assert 'value="Halfvolle melk 1L"' in client.get(f"/capture/{second}").text  # matched by name alone
    client.post(f"/capture/{second}/confirm", data=form_data(client, ean="", store="lidl", price="0,99", unit_price="0,99"))
    assert "Cheaper than usual" in client.get(f"/capture/{second}").text


def test_scan_page_requires_a_session(client):
    resp = client.get("/capture", headers={"accept": "text/html"})
    assert resp.status_code == 303 and resp.headers["location"].startswith("/login")


def test_every_script_and_stylesheet_the_scan_page_needs_is_served(session):
    client, _ = session
    html = client.get("/capture").text
    import re

    assets = re.findall(r'(?:src|href)="(/static/[^"]+)"', html)
    assert any("zxing" in a for a in assets) and any("outbox.js" in a for a in assets)
    for a in assets:
        assert client.get(a).status_code == 200, a


# --- review and feedback ----------------------------------------------------

def test_confirm_screen_is_prefilled_from_the_reader_and_open_food_facts(session, db):
    client, token = session
    cid = new_read_capture(client, token, db, label=shelf_label(promo_text="Bonus", promo_kind="bonus_card"))
    page = client.get(f"/capture/{cid}").text
    assert "Needs review" in page and 'value="1.29"' in page and 'value="Halfvolle melk"' in page
    assert "Halfvolle melk, Campina, 1 L" in page and f'value="{EAN}"' in page
    assert f"/capture/{cid}/image" in page and "Open Food Facts" in page


def test_progress_page_while_the_label_is_being_read(session):
    client, token = session
    cid = upload(client, token).json()["id"]
    page = client.get(f"/capture/{cid}").text
    assert "Reading the label" in page and "data-poll-url" in page
    assert client.get(f"/capture/{cid}/status").json() == {"status": "extracting"}


def test_reader_text_is_escaped(session, db):
    client, token = session
    cid = new_read_capture(client, token, db, label=shelf_label(
        product_name='"><script>alert(1)</script>', promo_text="<img src=x onerror=alert(1)>", promo_kind="other"))
    page = client.get(f"/capture/{cid}").text
    assert "<script>alert" not in page and "<img src=x" not in page and "&lt;script&gt;" in page


def test_confirm_shows_first_price_then_compares_with_history(session, db):
    client, token = session
    first = new_read_capture(client, token, db)
    assert client.post(f"/capture/{first}/confirm", data=form_data(client)).status_code == 303
    page = client.get(f"/capture/{first}").text
    assert "Saved" in page and "EUR 1.29" in page and "First price recorded" in page and "Scan next" in page

    cheaper = new_read_capture(client, token, db, store="lidl", label=shelf_label(price_cents=99, unit_price_cents=99))
    client.post(f"/capture/{cheaper}/confirm",
                data=form_data(client, store="lidl", price="0,99", unit_price="0,99"))
    page = client.get(f"/capture/{cheaper}").text
    assert "Cheaper than usual" in page and "(-23%)" in page and "You usually pay EUR 1.29" in page

    again = new_read_capture(client, token, db)
    client.post(f"/capture/{again}/confirm", data=form_data(client))
    page = client.get(f"/capture/{again}").text
    assert "Cheaper elsewhere" in page and "Lidl: EUR 0.99" in page


def test_promotion_price_and_card_are_shown(session, db):
    client, token = session
    cid = new_read_capture(client, token, db)
    client.post(f"/capture/{cid}/confirm", data=form_data(
        client, promo_kind="x_for_y", promo_text="2 voor 2.00", effective_price="1,00", requires_card="on",
        valid_until="2026-09-27"))
    page = client.get(f"/capture/{cid}").text
    assert "EUR 1.00" in page and "2 voor 2.00" in page and "card or app needed" in page and "until 2026-09-27" in page


def test_confirm_errors_rerender_with_typed_values(session, db):
    client, token = session
    cid = new_read_capture(client, token, db)
    resp = client.post(f"/capture/{cid}/confirm", data=form_data(client, price="cheap", product="Mijn melk"))
    assert resp.status_code == 422 and "shelf price is not a valid amount" in resp.text
    assert 'value="Mijn melk"' in resp.text and 'value="cheap"' in resp.text
    resp = client.post(f"/capture/{cid}/confirm", data=form_data(client, ean="4006381333932"))
    assert resp.status_code == 422 and "barcode number is not valid" in resp.text
    db.expire_all()
    assert db.get(ShelfCapture, cid).status == "needs_review"


def test_confirm_needs_the_csrf_token(session, db):
    client, token = session
    cid = new_read_capture(client, token, db)
    data = form_data(client)
    del data["csrf_token"]
    assert client.post(f"/capture/{cid}/confirm", data=data).status_code == 403


def test_failed_capture_offers_retry_and_manual_entry(session, db):
    client, token = session
    cid = upload(client, token).json()["id"]
    read(client, db, reader=FakeShelfReader(failed("The model's answer did not match the expected format.")))
    page = client.get(f"/capture/{cid}").text
    assert "did not match" in page and "Try again" in page and "Enter it by hand" in page

    manual = client.get(f"/capture/{cid}?manual=1").text
    assert 'name="price"' in manual
    resp = client.post(f"/capture/{cid}/confirm", data=form_data(client))
    assert resp.status_code == 303
    assert "Saved" in client.get(f"/capture/{cid}").text


def test_retry_and_delete(session, db):
    client, token = session
    cid = upload(client, token).json()["id"]
    read(client, db, reader=FakeShelfReader(failed("boom")))
    assert client.post(f"/capture/{cid}/retry", data={"csrf_token": csrf_of(client)}).status_code == 303
    db.expire_all()
    assert db.get(ShelfCapture, cid).status == "extracting"
    assert client.post(f"/capture/{cid}/delete", data={"csrf_token": csrf_of(client)}).status_code == 303
    assert client.get(f"/capture/{cid}", headers={"accept": "application/json"}).status_code == 404


def test_photo_is_served_until_retention_ends(session, db):
    client, token = session
    cid = new_read_capture(client, token, db)
    client.post(f"/capture/{cid}/confirm", data=form_data(client))
    assert client.get(f"/capture/{cid}/image").headers["content-type"] == "image/jpeg"
    settings = client.app.state.settings
    assert purge_due_files(db, settings, utcnow() + timedelta(days=8)) >= 1
    assert client.get(f"/capture/{cid}/image").status_code == 404


def test_queue_page_and_home_badge(session, db):
    client, token = session
    cid = new_read_capture(client, token, db)
    assert "Halfvolle melk" in client.get("/capture/queue").text
    home = client.get("/").text
    assert "1 capture to review" in home and "Scan in store" in home
    client.post(f"/capture/{cid}/confirm", data=form_data(client))
    assert "to review" not in client.get("/").text


def test_anonymous_users_cannot_reach_capture_pages(client):
    for path in ("/capture/queue", "/capture/1", "/capture/1/status", "/capture/1/image"):
        assert client.get(path, headers={"accept": "application/json"}).status_code == 401, path
