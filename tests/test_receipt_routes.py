from datetime import timedelta

import pytest
from sqlalchemy import select

from grocery.db.base import utcnow
from grocery.db.models import NameMapping, Receipt
from grocery.jobs.handlers import process_one
from grocery.money import format_cents
from grocery.uploads.storage import purge_due_files
from tests.conftest import csrf_of, login
from tests.fakes import FakeReader, ean13, failed, jpeg_bytes, line, plus_receipt


@pytest.fixture
def session(client):
    login(client)
    return client, csrf_of(client)


def post_upload(client, token, files, csrf=True):
    data = {"csrf_token": token} if csrf else {}
    return client.post("/receipts/new", data=data, files=[("photos", f) for f in files])


def read_receipt(client, db, extraction=None):
    """Upload one photo, run the extraction job with a fake reader, return the receipt id."""
    resp = post_upload(client, csrf_of(client), [("r.jpg", jpeg_bytes(), "image/jpeg")])
    assert resp.status_code == 303, resp.text
    receipt_id = int(resp.headers["location"].rsplit("/", 1)[1])
    process_one(db, client.app.state.settings, lambda s: FakeReader(extraction or plus_receipt()))
    db.expire_all()
    return receipt_id


def form_for(client, receipt, **overrides):
    data = {
        "csrf_token": csrf_of(client), "store": "plus", "purchased_on": "2026-09-19", "total": "8.46",
        "line_count": str(len(receipt.lines)),
    }
    for i, l in enumerate(receipt.lines):
        data.update({
            f"l{i}_orig": str(l.line_no), f"l{i}_kind": l.kind, f"l{i}_raw": l.raw_text,
            f"l{i}_qty": str(l.quantity_milli / 1000), f"l{i}_unit": l.unit,
            f"l{i}_unit_price": format_cents(l.unit_price_cents), f"l{i}_total": format_cents(l.line_total_cents),
            f"l{i}_product": l.suggested_name or "", f"l{i}_category": str(l.category_id or ""),
        })
    data.update(overrides)
    return data


# --- upload -----------------------------------------------------------------

def test_upload_page_and_list_render(session):
    client, _ = session
    assert "Upload and read" in client.get("/receipts/new").text
    assert "No receipts yet" in client.get("/receipts").text


def test_upload_redirects_to_the_receipt_and_shows_progress(session, db):
    client, token = session
    resp = post_upload(client, token, [("r.jpg", jpeg_bytes(), "image/jpeg")])
    assert resp.status_code == 303
    page = client.get(resp.headers["location"])
    assert "Reading your receipt" in page.text and "data-poll-url" in page.text
    assert client.get(resp.headers["location"] + "/status").json() == {"status": "extracting"}


def test_upload_needs_the_csrf_token(session, db):
    client, _ = session
    assert post_upload(client, "", [("r.jpg", jpeg_bytes(), "image/jpeg")], csrf=False).status_code == 403
    assert db.scalar(select(Receipt)) is None


def test_upload_of_a_non_image_is_rejected_with_a_message(session, db):
    client, token = session
    resp = post_upload(client, token, [("evil.jpg", b"<html><script>alert(1)</script></html>", "image/jpeg")])
    assert resp.status_code == 400 and "Unsupported file type" in resp.text
    assert db.scalar(select(Receipt)) is None


def test_upload_with_no_file_selected_is_a_friendly_error(session):
    client, token = session
    resp = client.post("/receipts/new", data={"csrf_token": token})
    assert resp.status_code == 400 and "at least one" in resp.text


def test_anonymous_users_cannot_reach_receipts_or_images(client):
    for path in ("/receipts", "/receipts/new", "/receipts/1", "/receipts/1/image/1", "/receipts/1/status"):
        assert client.get(path, headers={"accept": "application/json"}).status_code == 401, path


# --- review -----------------------------------------------------------------

def test_review_screen_shows_extracted_lines_photo_and_total(session, db):
    client, _ = session
    receipt_id = read_receipt(client, db)
    page = client.get(f"/receipts/{receipt_id}").text
    assert "Needs review" in page and 'id="review-form"' in page
    assert "PLUS HV MELK 1L" in page and "KIPFILET 0,874 KG" in page
    assert 'value="8.46"' in page  # total
    assert f"/receipts/{receipt_id}/image/1" in page
    assert client.get(f"/receipts/{receipt_id}/image/1").headers["content-type"] == "image/jpeg"


def test_model_text_is_escaped_in_the_review_page(session, db):
    client, _ = session
    evil = plus_receipt(
        lines=[line(raw='<script>alert("x")</script>', total=100, name='"><img src=x onerror=alert(1)>')],
        total=100,
    )
    receipt_id = read_receipt(client, db, evil)
    page = client.get(f"/receipts/{receipt_id}").text
    assert "<script>alert" not in page and "<img src=x" not in page
    assert "&lt;script&gt;" in page


def test_confirm_saves_and_the_receipt_becomes_read_only(session, db):
    client, _ = session
    receipt_id = read_receipt(client, db)
    receipt = db.get(Receipt, receipt_id)
    resp = client.post(f"/receipts/{receipt_id}/confirm", data=form_for(client, receipt))
    assert resp.status_code == 303
    db.expire_all()
    assert db.get(Receipt, receipt_id).status == "confirmed"
    assert db.scalar(select(NameMapping).where(NameMapping.raw_norm == "plus hv melk 1l")) is not None
    page = client.get(f"/receipts/{receipt_id}").text
    assert "Saved" in page and "Halfvolle melk 1L" in page and 'id="review-form"' not in page


def test_confirm_needs_the_csrf_token(session, db):
    client, _ = session
    receipt_id = read_receipt(client, db)
    data = form_for(client, db.get(Receipt, receipt_id))
    del data["csrf_token"]
    assert client.post(f"/receipts/{receipt_id}/confirm", data=data).status_code == 403


def test_mismatch_rerenders_with_the_typed_values_and_a_clear_message(session, db):
    client, _ = session
    receipt_id = read_receipt(client, db)
    receipt = db.get(Receipt, receipt_id)
    data = form_for(client, receipt, total="9.00", l0_product="Mijn melk")
    resp = client.post(f"/receipts/{receipt_id}/confirm", data=data)
    assert resp.status_code == 422
    assert "below the total" in resp.text and 'value="9.00"' in resp.text and "Mijn melk" in resp.text
    db.expire_all()
    assert db.get(Receipt, receipt_id).status == "needs_review"  # nothing was saved

    data["accept_mismatch"] = "on"
    assert client.post(f"/receipts/{receipt_id}/confirm", data=data).status_code == 303


@pytest.mark.parametrize(
    "override,message",
    [
        ({"l0_total": "abc"}, "Line 1"),
        ({"total": "x"}, "total is not a valid amount"),
        ({"purchased_on": "2026-13-45"}, "date is not valid"),
        ({"l0_raw": ""}, "text is empty"),
    ],
)
def test_invalid_form_input_gives_specific_errors(session, db, override, message):
    client, _ = session
    receipt_id = read_receipt(client, db)
    data = form_for(client, db.get(Receipt, receipt_id), **override)
    resp = client.post(f"/receipts/{receipt_id}/confirm", data=data)
    assert resp.status_code == 422 and message in resp.text


def test_removed_and_empty_rows_are_dropped_and_new_rows_are_added(session, db):
    client, _ = session
    receipt_id = read_receipt(client, db)
    receipt = db.get(Receipt, receipt_id)
    data = form_for(client, receipt, total="8.96", line_count="6")
    data["l2_delete"] = "on"  # remove the discount (-0.50) ...
    data["l3_delete"] = "on"  # ... and the deposit (+0.25): 1.29 + 7.42 = 8.71 remain
    data.update(  # a new row, comma decimal accepted: 8.71 + 0.25 = 8.96
        l4_raw="BROODJE", l4_total="0,25", l4_kind="item", l4_qty="1", l4_unit="pcs", l4_product="Broodje"
    )
    assert client.post(f"/receipts/{receipt_id}/confirm", data=data).status_code == 303
    db.expire_all()
    assert [l.raw_text for l in db.get(Receipt, receipt_id).lines] == [
        "PLUS HV MELK 1L", "KIPFILET 0,874 KG", "BROODJE",
    ]


def test_failed_receipt_shows_the_reason_and_can_be_retried(session, db):
    client, token = session
    resp = post_upload(client, token, [("r.jpg", jpeg_bytes(), "image/jpeg")])
    receipt_id = int(resp.headers["location"].rsplit("/", 1)[1])
    process_one(
        db, client.app.state.settings,
        lambda s: FakeReader(failed("The model's answer was cut off (receipt too long).")),
    )
    page = client.get(f"/receipts/{receipt_id}").text
    assert "cut off" in page and "Try again" in page
    assert client.post(f"/receipts/{receipt_id}/retry", data={"csrf_token": csrf_of(client)}).status_code == 303
    db.expire_all()
    assert db.get(Receipt, receipt_id).status == "extracting"


def test_delete_removes_the_receipt(session, db):
    client, _ = session
    receipt_id = read_receipt(client, db)
    assert client.post(f"/receipts/{receipt_id}/delete", data={"csrf_token": csrf_of(client)}).status_code == 303
    assert client.get(f"/receipts/{receipt_id}", headers={"accept": "application/json"}).status_code == 404


def test_image_is_gone_after_the_retention_period(session, db):
    client, _ = session
    receipt_id = read_receipt(client, db)
    receipt = db.get(Receipt, receipt_id)
    client.post(f"/receipts/{receipt_id}/confirm", data=form_for(client, receipt))
    settings = client.app.state.settings
    db.expire_all()
    assert purge_due_files(db, settings, utcnow() + timedelta(days=6)) == 0
    assert client.get(f"/receipts/{receipt_id}/image/1").status_code == 200
    assert purge_due_files(db, settings, utcnow() + timedelta(days=8)) == 1
    assert client.get(f"/receipts/{receipt_id}/image/1").status_code == 404


# --- quick add --------------------------------------------------------------

def test_quick_form_prefills_the_turkish_price_per_kg(session):
    client, _ = session
    page = client.get("/receipts/quick?store=turkish").text
    assert 'value="8.49"' in page and 'placeholder="Kip"' in page


def test_quick_add_weighed_purchase_and_it_remembers_the_price(session):
    client, token = session
    resp = client.post("/receipts/quick", data={
        "csrf_token": token, "store": "turkish", "purchased_on": "2026-09-20", "description": "",
        "total": "", "weight": "0,874", "price_per_kg": "8,49",
    })
    assert resp.status_code == 303
    page = client.get(resp.headers["location"]).text
    assert "EUR 7.42" in page and "added by hand" in page
    client.post("/receipts/quick", data={
        "csrf_token": token, "store": "turkish", "purchased_on": "2026-09-21", "total": "", "weight": "1",
        "price_per_kg": "8,99",
    })
    assert 'value="8.99"' in client.get("/receipts/quick?store=turkish").text


def test_quick_form_has_an_optional_barcode_field(session):
    client, _ = session
    page = client.get("/receipts/quick?store=plus").text
    assert 'id="ean"' in page and 'name="ean"' in page


def test_quick_add_with_a_barcode_links_it_and_a_second_purchase_reuses_the_name(session, db):
    client, token = session
    ean = ean13("871040001234")
    resp = client.post("/receipts/quick", data={
        "csrf_token": token, "store": "plus", "purchased_on": "2026-09-20", "description": "Pastinaak",
        "total": "0,99", "ean": ean,
    })
    assert resp.status_code == 303
    assert "Pastinaak" in client.get(resp.headers["location"]).text

    resp2 = client.post("/receipts/quick", data={
        "csrf_token": token, "store": "plus", "purchased_on": "2026-09-21", "description": "",
        "total": "1,19", "ean": ean,
    })
    assert resp2.status_code == 303
    assert "Pastinaak" in client.get(resp2.headers["location"]).text


def test_quick_add_with_an_invalid_barcode_is_rejected(session):
    client, token = session
    resp = client.post("/receipts/quick", data={
        "csrf_token": token, "store": "plus", "purchased_on": "2026-09-20", "total": "1,00", "ean": "123",
    })
    assert resp.status_code == 400 and "barcode" in resp.text


def test_quick_add_bakery_and_errors(session):
    client, token = session
    base = {"csrf_token": token, "store": "bakery", "purchased_on": "2026-09-20"}
    assert client.post("/receipts/quick", data={**base, "total": "3,95"}).status_code == 303
    bad = client.post("/receipts/quick", data={**base, "total": "lots"})
    assert bad.status_code == 400 and "Check the amount" in bad.text
    empty = client.post("/receipts/quick", data=base)
    assert empty.status_code == 400 and "Enter the amount" in empty.text


def test_list_and_home_show_receipts_and_budget(session, db):
    client, _ = session
    read_receipt(client, db)
    assert "Needs review" in client.get("/receipts").text
    assert "API spend this month" in client.get("/").text
