from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from grocery.db.models import AuthSession
from grocery.web.templating import localtime
from tests.conftest import csrf_of, login
from tests.test_capture_routes import upload

STATIC = Path(__file__).resolve().parent.parent / "src/grocery/web/static"


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


# --- times are shown in the user's timezone ---------------------------------

def test_summer_time_is_two_hours_ahead_and_crosses_midnight():
    assert localtime(utc(2026, 9, 20, 22, 30)) == "21 Sep 00:30"


def test_winter_time_is_one_hour_ahead():
    assert localtime(utc(2026, 1, 15, 12, 0)) == "15 Jan 13:00"


def test_the_switch_to_summer_time_is_followed():
    assert localtime(utc(2026, 3, 28, 12, 0), "%H:%M") == "13:00"  # still winter time
    assert localtime(utc(2026, 3, 29, 12, 0), "%H:%M") == "14:00"  # CEST started that night


def test_naive_datetimes_count_as_utc_and_none_is_empty():
    assert localtime(datetime(2026, 9, 20, 22, 30)) == "21 Sep 00:30"
    assert localtime(None) == ""


def test_custom_format():
    assert localtime(utc(2026, 9, 20, 10, 5), "%Y-%m-%d %H:%M") == "2026-09-20 12:05"


def test_the_capture_queue_shows_local_time_without_the_utc_label(client):
    login(client)
    token = csrf_of(client)
    upload(client, token, when="2026-09-20T22:30:00+00:00")
    page = client.get("/capture/queue").text
    assert "21 Sep 00:30" in page and "UTC" not in page


def test_the_account_page_shows_session_times_in_local_time(client, db):
    login(client)
    session = db.scalar(select(AuthSession))
    page = client.get("/account").text
    assert "UTC" not in page
    assert f"Signed in {localtime(session.created_at)}" in page


def test_the_timezone_comes_from_the_setting(make_client):
    client = make_client(timezone="UTC")
    login(client)
    upload(client, csrf_of(client), when="2026-09-20T22:30:00+00:00")
    assert "20 Sep 22:30" in client.get("/capture/queue").text


# --- quick add: amount from weight x price per kg ---------------------------

def test_the_quick_form_loads_the_calculation_script_and_keeps_its_field_ids(client):
    login(client)
    page = client.get("/receipts/quick?store=turkish").text
    assert "/static/js/quick.js" in page
    for field in ('id="weight"', 'id="price_per_kg"', 'id="total"', 'id="total-note"'):
        assert field in page
    assert page.index('id="weight"') < page.index('id="price_per_kg"') < page.index('id="total"')  # weigh first, amount after


def test_the_script_rounds_half_up_like_the_server():
    js = (STATIC / "js/quick.js").read_text(encoding="utf-8")
    assert "Math.floor((w * p + 500) / 1000)" in js  # same as receipts.validation.expected_line_total
    from grocery.receipts.validation import expected_line_total

    assert expected_line_total(874, 849) == 742 and expected_line_total(500, 101) == 51


def test_a_total_typed_next_to_a_weight_wins_over_the_calculation(client):
    login(client)
    token = csrf_of(client)
    client.post("/receipts/quick", data={
        "csrf_token": token, "store": "turkish", "purchased_on": "2026-09-20", "weight": "0,874",
        "price_per_kg": "8,49", "total": "7,50",
    })
    page = client.get("/receipts").text
    assert "EUR 7.50" in page and "EUR 7.42" not in page


@pytest.mark.parametrize("weight,price,expected", [("0,874", "8,49", "7.42"), ("1", "8,49", "8.49"), ("2,5", "8,49", "21.23")])
def test_the_server_calculates_the_same_amount_when_the_field_is_left_empty(client, weight, price, expected):
    login(client)
    token = csrf_of(client)
    resp = client.post("/receipts/quick", data={
        "csrf_token": token, "store": "turkish", "purchased_on": "2026-09-20", "weight": weight, "price_per_kg": price,
    })
    assert resp.status_code == 303
    assert f"EUR {expected}" in client.get(resp.headers["location"]).text
