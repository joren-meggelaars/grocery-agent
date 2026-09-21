from datetime import date, timedelta

import pytest
from sqlalchemy import select

from grocery.db.models import PriceObservation, Product, Receipt
from grocery.prices.feedback import price_overview
from grocery.prices.observations import set_pack_content
from grocery.products.content import (
    ContentError,
    format_content,
    is_sold_per_piece,
    parse_content,
    unit_price_from_content,
)
from grocery.products.normalize import product_key
from tests.conftest import csrf_of, login
from tests.fakes import plus_receipt, line, shelf_label
from tests.test_analytics_routes import make_receipt, today_local
from tests.test_capture_routes import form_data, new_read_capture
from tests.test_cupboard import TODAY, obs
from tests.test_receipt_routes import form_for, read_receipt


# --- reading and calculating pack content ----------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("500 g", (500, "kg")), ("500g", (500, "kg")), ("0,5 kg", (500, "kg")), ("1 kg", (1000, "kg")),
    ("1,5 l", (1500, "l")), ("1,5 L", (1500, "l")), ("750 ml", (750, "l")), ("33 cl", (330, "l")),
    ("6 x 33 cl", (1980, "l")), ("6x33cl", (1980, "l")), ("2 x 250 g", (500, "kg")), (" 1 liter ", (1000, "l")),
])
def test_content_is_read_in_grams_or_millilitres(text, expected):
    assert parse_content(text) == expected


@pytest.mark.parametrize("bad", ["", "abc", "500", "g", "5 stuks", "0 g", "-5 g", "500 kg", "1 x", "1e3 g"])
def test_bad_content_is_refused(bad):
    with pytest.raises(ContentError):
        parse_content(bad)


def test_content_is_shown_back_readably():
    assert format_content(500, "kg") == "500 g"
    assert format_content(1500, "kg") == "1,5 kg"
    assert format_content(750, "l") == "750 ml"
    assert format_content(1000, "l") == "1 l"
    assert format_content(None, None) == ""


def test_the_price_per_kg_is_rounded_half_up():
    assert unit_price_from_content(149, 500) == 298
    assert unit_price_from_content(129, 1000) == 129
    assert unit_price_from_content(101, 400) == 253  # 252.5
    assert unit_price_from_content(99, 330) == 300


@pytest.mark.parametrize("name,category,flagged,expected", [
    ("Eieren 10 stuks", None, False, True), ("Komkommer", "Groente & fruit", False, True),
    ("Volkoren brood", None, False, True), ("Iets", "Brood & banket", False, True),
    ("Afwasmiddel", "Huishouden & verzorging", False, True), ("Hagelslag", "Snacks & zoet", True, True),
    ("Halfvolle melk 1L", "Zuivel & eieren", False, False), ("Kipfilet", "Vlees & vis", False, False),
    ("Goudse kaas", None, False, False), ("Spaghetti", "Houdbaar", False, False),
    ("Eierkoeken", None, False, False),  # "ei" only counts as a whole word
])
def test_products_that_are_plainly_sold_per_piece_are_not_asked_about(name, category, flagged, expected):
    assert is_sold_per_piece(name, category, flagged) is expected


# --- shelf labels ------------------------------------------------------------------

def read_label(client, db, **label):
    return new_read_capture(client, csrf_of(client), db, label=shelf_label(unit_price_cents=None, unit_price_per=None, **label))


@pytest.fixture
def session(client):
    login(client)
    return client


def test_the_label_page_asks_for_the_content_when_it_has_no_price_per_unit(session, db):
    cid = read_label(session, db, product_name="Goudse kaas plak")
    page = session.get(f"/capture/{cid}").text
    assert 'name="pack_content"' in page and 'name="pack_shown"' in page and "No price per kg or l on this label" in page


@pytest.mark.parametrize("label", [
    dict(product_name="Halfvolle melk", unit_price_cents=129, unit_price_per="l"),  # the label has it
    dict(product_name="Eieren 10 stuks"), dict(product_name="Volkoren brood"),      # plainly per piece
])
def test_no_question_when_the_unit_price_is_known_or_the_product_is_sold_per_piece(session, db, label):
    cid = new_read_capture(session, csrf_of(session), db, label=shelf_label(**label))
    assert 'name="pack_content"' not in session.get(f"/capture/{cid}").text


def test_saving_with_the_content_calculates_the_price_per_kg_and_remembers_the_content(session, db):
    cid = read_label(session, db, product_name="Goudse kaas plak", price_cents=249)
    resp = session.post(f"/capture/{cid}/confirm", data=form_data(
        session, product="Goudse kaas plak", price="2,49", unit_price="", pack_shown="1", pack_content="500 g"))
    assert resp.status_code == 303
    db.expire_all()
    product = db.scalar(select(Product).where(Product.name_key == product_key("Goudse kaas plak")))
    assert (product.pack_content, product.pack_basis) == (500, "kg")
    observation = db.scalar(select(PriceObservation).where(PriceObservation.source == "shelf"))
    assert (observation.unit_price_cents, observation.unit_basis) == (498, "kg")
    assert "EUR 4.98 per kg" in session.get(f"/capture/{cid}").text


def test_a_promotion_price_also_gets_a_price_per_kg_from_the_content(session, db):
    cid = read_label(session, db, product_name="Goudse kaas plak", price_cents=249)
    session.post(f"/capture/{cid}/confirm", data=form_data(
        session, product="Goudse kaas plak", price="2,49", unit_price="", effective_price="1,99", promo_kind="fixed_price",
        promo_text="Aanbieding", pack_shown="1", pack_content="500 g"))
    db.expire_all()
    observation = db.scalar(select(PriceObservation).where(PriceObservation.source == "shelf"))
    assert (observation.price_cents, observation.unit_price_cents, observation.unit_basis) == (199, 398, "kg")


def test_known_content_is_prefilled_and_used_without_asking_again(session, db):
    first = read_label(session, db, product_name="Goudse kaas plak", price_cents=249)
    session.post(f"/capture/{first}/confirm", data=form_data(
        session, product="Goudse kaas plak", price="2,49", unit_price="", pack_shown="1", pack_content="500 g"))
    second = read_label(session, db, product_name="Goudse kaas plak", price_cents=199)
    page = session.get(f"/capture/{second}").text
    assert "Pack content known from earlier" in page and 'value="500 g"' in page
    session.post(f"/capture/{second}/confirm", data=form_data(
        session, product="Goudse kaas plak", price="1,99", unit_price="", pack_shown="1", pack_content="500 g"))
    db.expire_all()
    latest = db.scalars(select(PriceObservation).order_by(PriceObservation.id.desc())).first()
    assert (latest.unit_price_cents, latest.unit_basis) == (398, "kg")


def test_the_checkbox_stops_the_question_for_that_product(session, db):
    first = read_label(session, db, product_name="Bloemkoolroosjes", price_cents=199)
    session.post(f"/capture/{first}/confirm", data=form_data(
        session, product="Bloemkoolroosjes", price="1,99", unit_price="", pack_shown="1", sold_per_piece="on"))
    second = read_label(session, db, product_name="Bloemkoolroosjes", price_cents=199)
    assert 'name="pack_content"' not in session.get(f"/capture/{second}").text


def test_leaving_the_question_empty_is_allowed(session, db):
    cid = read_label(session, db, product_name="Goudse kaas plak")
    resp = session.post(f"/capture/{cid}/confirm", data=form_data(
        session, product="Goudse kaas plak", unit_price="", pack_shown="1", pack_content=""))
    assert resp.status_code == 303
    db.expire_all()
    observation = db.scalar(select(PriceObservation))
    assert observation.unit_price_cents is None


def test_a_wrong_content_gives_a_message_and_keeps_what_was_typed(session, db):
    cid = read_label(session, db, product_name="Goudse kaas plak")
    resp = session.post(f"/capture/{cid}/confirm", data=form_data(
        session, product="Goudse kaas plak", unit_price="", pack_shown="1", pack_content="veel"))
    assert resp.status_code == 422
    assert "Pack content: Enter the content like 500 g" in resp.text and 'value="veel"' in resp.text


def test_saving_content_gives_earlier_pack_prices_a_price_per_kg(session, db):
    make_receipt(db, "plus", TODAY, [("item", 249, "Zuivel & eieren", "Goudse kaas plak")])
    product = db.scalar(select(Product).where(Product.name_key == product_key("Goudse kaas plak")))
    obs(db, product, "plus", 249)
    set_pack_content(db, product, 500, "kg")
    db.commit()
    assert [(o.unit_price_cents, o.unit_basis) for o in db.scalars(select(PriceObservation))] == [(498, "kg")]


# --- receipts -----------------------------------------------------------------------

def test_the_receipt_review_asks_for_piece_priced_lines_only(session, db):
    receipt_id = read_receipt(session, db)  # milk per piece, chicken by weight, discount, deposit
    page = session.get(f"/receipts/{receipt_id}").text
    assert page.count('name="l0_content"') == 1  # milk
    assert 'name="l1_content"' not in page       # chicken is weighed already
    assert 'name="l2_content"' not in page and 'name="l3_content"' not in page


def confirm_with(session, db, receipt_id, **extra):
    receipt = db.get(Receipt, receipt_id)
    return session.post(f"/receipts/{receipt_id}/confirm", data=form_for(session, receipt, **extra))


def test_content_typed_on_a_receipt_line_gives_that_purchase_a_price_per_l(session, db):
    receipt_id = read_receipt(session, db)
    assert confirm_with(session, db, receipt_id, l0_packask="1", l0_content="1 l").status_code == 303
    db.expire_all()
    milk = db.scalar(select(Product).where(Product.name_key == product_key("Halfvolle melk 1L")))
    assert (milk.pack_content, milk.pack_basis) == (1000, "l")
    observation = db.scalar(select(PriceObservation).where(PriceObservation.product_id == milk.id))
    assert (observation.price_cents, observation.unit_price_cents, observation.unit_basis) == (129, 129, "l")


def test_a_second_receipt_uses_the_known_content_without_a_question_to_answer(session, db):
    first = read_receipt(session, db)
    confirm_with(session, db, first, l0_packask="1", l0_content="1 l")
    second = read_receipt(session, db)
    page = session.get(f"/receipts/{second}").text
    assert "Pack content known from earlier" in page and 'value="1 l"' in page
    confirm_with(session, db, second, l0_packask="1", l0_content="1 l", purchased_on="2026-09-21")
    db.expire_all()
    assert {o.unit_basis for o in db.scalars(select(PriceObservation).where(PriceObservation.unit_price_cents == 129))} == {"l"}


def test_a_receipt_line_can_be_marked_as_sold_per_piece(session, db):
    receipt_id = read_receipt(session, db, plus_receipt(
        total=349, lines=[line(raw="BOLLEN", total=349, unit_price=349, name="Bloemkoolroosjes", cat="Groente & fruit")]))
    confirm_with(session, db, receipt_id, total="3.49", l0_packask="1", l0_perpiece="on")
    db.expire_all()
    product = db.scalar(select(Product).where(Product.name_key == product_key("Bloemkoolroosjes")))
    assert product.sold_per_piece is True
    again = read_receipt(session, db, plus_receipt(
        total=349, lines=[line(raw="BOLLEN", total=349, unit_price=349, name="Bloemkoolroosjes", cat="Groente & fruit")]))
    assert 'name="l0_content"' not in session.get(f"/receipts/{again}").text


def test_a_wrong_content_on_a_receipt_line_is_reported_and_keeps_the_typed_value(session, db):
    receipt_id = read_receipt(session, db)
    resp = confirm_with(session, db, receipt_id, l0_packask="1", l0_content="veel")
    assert resp.status_code == 422 and "Line 1: pack content." in resp.text and 'value="veel"' in resp.text
    assert db.get(Receipt, receipt_id).status == "needs_review"


def test_a_receipt_without_the_question_answered_saves_as_before(session, db):
    receipt_id = read_receipt(session, db)
    assert confirm_with(session, db, receipt_id, l0_packask="1", l0_content="").status_code == 303
    db.expire_all()
    milk = db.scalar(select(Product).where(Product.name_key == product_key("Halfvolle melk 1L")))
    assert milk.pack_content is None and milk.sold_per_piece is False


# --- the cheapest alternative --------------------------------------------------------

def product_of(db, name):
    return db.scalar(select(Product).where(Product.name_key == product_key(name)))


@pytest.fixture
def kaas(db):
    make_receipt(db, "plus", TODAY, [("item", 249, "Zuivel & eieren", "Kaas")])
    return product_of(db, "Kaas")


def overview(db, product, today=TODAY):
    return price_overview(db, [product.id], today)[product.id]


def test_the_cheapest_alternative_is_the_lowest_price_at_another_store(db, kaas):
    obs(db, kaas, "plus", 249, TODAY)
    obs(db, kaas, "lidl", 199, TODAY - timedelta(days=3), source="shelf")
    obs(db, kaas, "jumbo", 219, TODAY - timedelta(days=2), source="shelf")
    obs(db, kaas, "aldi", 299, TODAY - timedelta(days=2), source="shelf")
    view = overview(db, kaas)
    assert (view.last.store, view.last.value, view.paid) == ("Plus", 249, True)
    assert (view.alternative.store, view.alternative.value) == ("Lidl", 199)
    assert view.pct == pytest.approx(-0.2008, abs=0.001)


def test_the_alternative_is_shown_even_when_it_is_more_expensive(db, kaas):
    obs(db, kaas, "plus", 249, TODAY)
    obs(db, kaas, "lidl", 299, TODAY - timedelta(days=3), source="shelf")
    view = overview(db, kaas)
    assert view.alternative.store == "Lidl" and view.pct > 0


def test_the_same_store_is_never_its_own_alternative(db, kaas):
    obs(db, kaas, "plus", 249, TODAY)
    obs(db, kaas, "plus", 199, TODAY - timedelta(days=3), source="shelf")
    assert overview(db, kaas).alternative is None


def test_old_and_expired_prices_are_not_alternatives(db, kaas):
    obs(db, kaas, "plus", 249, TODAY)
    obs(db, kaas, "lidl", 199, TODAY - timedelta(days=61), source="shelf")
    assert overview(db, kaas).alternative is None
    db.add(PriceObservation(
        product_id=kaas.id, store_id=db.scalar(select(PriceObservation.store_id).limit(1)), price_cents=150,
        observed_on=TODAY - timedelta(days=5), source="shelf", is_promo=True, valid_until=TODAY - timedelta(days=1)))
    db.commit()
    assert overview(db, kaas).alternative is None


def test_prices_per_another_unit_are_reported_as_not_comparable(db, kaas):
    obs(db, kaas, "plus", 249, TODAY)
    obs(db, kaas, "lidl", 199, TODAY - timedelta(days=2), source="shelf", unit=398, basis="kg")
    view = overview(db, kaas)
    assert view.alternative is None and view.incomparable is True


def test_a_product_without_prices_has_an_empty_overview(db, kaas):
    db.execute(PriceObservation.__table__.delete())
    db.commit()
    view = overview(db, kaas)
    assert view.last is None and view.alternative is None and view.incomparable is False


def test_what_i_buy_most_shows_the_cheapest_alternative_next_to_the_last_purchase(client, db):
    login(client)
    today = today_local(client)
    make_receipt(db, "plus", today, [("item", 249, "Zuivel & eieren", "Kaas")])
    kaas = product_of(db, "Kaas")
    obs(db, kaas, "plus", 249, today)
    obs(db, kaas, "lidl", 199, today - timedelta(days=1), source="shelf")
    page = client.get("/regulars?period=all").text
    assert "Last: " in page and ", Plus" in page and "Cheapest alternative:" in page
    assert "Lidl" in page and "€1.99" in page and "-20%" in page


def test_what_i_buy_most_says_so_when_there_is_no_alternative(client, db):
    login(client)
    make_receipt(db, "plus", today_local(client), [("item", 249, "Zuivel & eieren", "Kaas")])
    assert "none seen at another store" in client.get("/regulars?period=all").text


def test_the_scan_result_names_the_cheapest_alternative_even_when_nothing_is_cheaper(client, db):
    login(client)
    lidl = new_read_capture(client, csrf_of(client), db, store="lidl", label=shelf_label(price_cents=99, unit_price_cents=99))
    client.post(f"/capture/{lidl}/confirm", data=form_data(client, store="lidl", price="0,99", unit_price="0,99"))
    plus = new_read_capture(client, csrf_of(client), db, store="plus", label=shelf_label(price_cents=129, unit_price_cents=129))
    client.post(f"/capture/{plus}/confirm", data=form_data(client, store="plus", price="1,29", unit_price="1,29"))
    page = client.get(f"/capture/{plus}").text
    assert "Cheaper elsewhere" in page and "Lidl" in page
    lidl_page = client.get(f"/capture/{lidl}").text
    assert "Cheapest alternative:" in lidl_page and "Plus" in lidl_page and "Cheaper elsewhere" not in lidl_page
