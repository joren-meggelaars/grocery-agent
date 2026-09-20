from datetime import timedelta

import pytest

from grocery.db.base import utcnow
from grocery.db.models import OffCache
from grocery.products.ean import check_digit_ok, clean_ean, normalize_ean
from grocery.products.off import OffProduct, http_fetch, lookup
from tests.fakes import ean13, off_down, off_found, off_missing


# --- EAN --------------------------------------------------------------------

@pytest.mark.parametrize("code", ["4006381333931", "73513537", "036000291452"])
def test_known_valid_codes(code):
    assert clean_ean(code) == code


def test_generated_codes_are_valid():
    for body in ("871040001234", "871981800001", "540000000012"):
        assert clean_ean(ean13(body)) == ean13(body)


def test_wrong_check_digit_is_rejected():
    assert clean_ean("4006381333932") is None
    assert clean_ean("73513538") is None


@pytest.mark.parametrize("bad", [None, "", "abc", "12345", "40063813339311", "4006 3813 33931x", "٤٠٠٦٣٨١٣٣٣٩٣١"])
def test_non_barcodes_are_rejected(bad):
    assert clean_ean(bad) is None


def test_spaces_and_hyphens_are_tolerated():
    assert clean_ean("4006 3813-33931") == "4006381333931"
    assert normalize_ean("4006381333932") == "4006381333932"  # shape ok, checksum is checked separately
    assert not check_digit_ok("4006381333932")


# --- Open Food Facts --------------------------------------------------------

def test_found_product_is_returned_and_cached(client, db):
    settings = client.app.state.settings
    fetch = off_found()
    product = lookup(db, settings, "4006381333931", fetch)
    assert product == OffProduct("Halfvolle melk", "Campina", "1 L")
    assert product.display == "Halfvolle melk, Campina, 1 L"
    assert fetch.calls == [("4006381333931", settings.off_user_agent)]

    again = lookup(db, settings, "4006381333931", fetch)  # from the cache: no second request
    assert again == product and len(fetch.calls) == 1


def test_unknown_product_is_cached_too_but_for_a_shorter_time(client, db):
    settings = client.app.state.settings
    fetch = off_missing()
    now = utcnow()
    assert lookup(db, settings, "4006381333931", fetch, now) is None
    assert lookup(db, settings, "4006381333931", fetch, now + timedelta(days=3)) is None
    assert len(fetch.calls) == 1  # still cached
    lookup(db, settings, "4006381333931", fetch, now + timedelta(days=settings.off_missing_ttl_days + 1))
    assert len(fetch.calls) == 2  # expired: asked again


def test_found_entries_live_longer(client, db):
    settings = client.app.state.settings
    fetch = off_found()
    now = utcnow()
    lookup(db, settings, "4006381333931", fetch, now)
    lookup(db, settings, "4006381333931", fetch, now + timedelta(days=settings.off_missing_ttl_days + 1))
    assert len(fetch.calls) == 1
    lookup(db, settings, "4006381333931", fetch, now + timedelta(days=settings.off_found_ttl_days + 1))
    assert len(fetch.calls) == 2


def test_unreachable_service_returns_none_and_is_not_cached(client, db):
    settings = client.app.state.settings
    assert lookup(db, settings, "4006381333931", off_down()) is None
    assert db.get(OffCache, "4006381333931") is None


@pytest.mark.parametrize("status,body", [(429, b"{}"), (500, b"oops"), (200, b"not json"), (200, b"[1, 2]")])
def test_bad_answers_are_not_cached(client, db, status, body):
    settings = client.app.state.settings
    assert lookup(db, settings, "4006381333931", lambda e, u: (status, body)) is None
    assert db.get(OffCache, "4006381333931") is None


def test_invalid_barcode_never_reaches_the_network(client, db):
    settings = client.app.state.settings

    def boom(ean, ua):
        raise AssertionError("must not be called")

    assert lookup(db, settings, "not-a-barcode", boom) is None
    assert lookup(db, settings, "../../etc/passwd", boom) is None


def test_product_without_a_name_or_with_hostile_text_is_handled(client, db):
    settings = client.app.state.settings
    body = b'{"status": 1, "product": {"product_name": "  <b>Melk</b>\\n\\n  1L ", "brands": "A,B", "quantity": null}}'
    product = lookup(db, settings, "4006381333931", lambda e, u: (200, body))
    assert product.name == "<b>Melk</b> 1L" and product.brand == "A"  # stored as data; templates escape it


def test_display_skips_duplicates():
    assert OffProduct("Campina Melk", "campina", None).display == "Campina Melk, campina"
    assert OffProduct("Melk", "Melk", "1 L").display == "Melk, 1 L"
    assert OffProduct(None, None, None).display is None


def test_http_fetch_refuses_non_digits():
    with pytest.raises(ValueError):
        http_fetch("12/../3", "ua")
