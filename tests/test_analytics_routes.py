from datetime import date, timedelta

import pytest
from sqlalchemy import select

from grocery.analytics.aggregate import add_months, first_of_month
from grocery.analytics.queries import load_categories, load_receipts
from grocery.capture.service import local_date
from grocery.db.base import utcnow
from grocery.db.models import Category, Receipt, ReceiptLine, Store
from grocery.receipts.service import get_or_create_product
from tests.conftest import login


def today_local(client) -> date:
    return local_date(utcnow(), client.app.state.settings.timezone)


def make_receipt(db, chain, on, lines, total=None, status="confirmed"):
    """lines: (kind, cents, category name, product name)"""
    store = db.scalar(select(Store).where(Store.chain == chain))
    cats = {c.name: c.id for c in db.scalars(select(Category))}
    receipt = Receipt(
        store_id=store.id, purchased_on=on, status=status, source="photo", flags=[],
        total_cents=total if total is not None else sum(l[1] for l in lines),
    )
    for number, (kind, cents, cat, name) in enumerate(lines, start=1):
        product = get_or_create_product(db, name, cats.get(cat)) if name else None
        receipt.lines.append(
            ReceiptLine(
                line_no=number, kind=kind, raw_text=name or kind, quantity_milli=1000, unit="pcs",
                line_total_cents=cents, product_id=product.id if product else None,
                category_id=cats.get(cat), match_source="manual",
            )
        )
    db.add(receipt)
    db.commit()
    return receipt


@pytest.fixture
def session(client):
    login(client)
    return client


@pytest.fixture
def month_start(client):
    return first_of_month(today_local(client))


# --- loading ----------------------------------------------------------------

def test_only_confirmed_dated_receipts_are_loaded(client, db, month_start):
    make_receipt(db, "plus", month_start, [("item", 500, "Zuivel & eieren", "Melk")])
    make_receipt(db, "plus", month_start, [("item", 700, "Zuivel & eieren", "Kaas")], status="needs_review")
    make_receipt(db, "plus", month_start, [("item", 900, "Zuivel & eieren", "Boter")], status="failed")
    undated = make_receipt(db, "plus", month_start, [("item", 100, "Zuivel & eieren", "Ei")])
    undated.purchased_on = None
    db.commit()
    facts = load_receipts(db)
    assert [f.total_cents for f in facts] == [500]
    assert facts[0].store_name == "Plus" and facts[0].lines[0].product_name == "Melk"


def test_the_date_window_is_since_inclusive_until_exclusive(client, db):
    for day in (date(2026, 8, 31), date(2026, 9, 1), date(2026, 9, 30), date(2026, 10, 1)):
        make_receipt(db, "plus", day, [("item", 100, None, None)])
    got = load_receipts(db, since=date(2026, 9, 1), until=date(2026, 10, 1))
    assert [f.on for f in got] == [date(2026, 9, 1), date(2026, 9, 30)]


def test_categories_carry_the_food_flag(client, db):
    cats = {c.name: c for c in load_categories(db).values()}
    assert cats["Huishouden & verzorging"].counts_as_food is False and cats["Vlees & vis"].counts_as_food is True


# --- overview ---------------------------------------------------------------

def test_overview_needs_a_session(client):
    for path in ("/overview", "/regulars"):
        assert client.get(path, headers={"accept": "application/json"}).status_code == 401


def test_an_empty_month_is_friendly(session):
    page = session.get("/overview").text
    assert "No saved receipts" in page and "€0.00" in page and "Previous month" in page


def test_overview_shows_hero_charts_and_tables(session, db, month_start):
    make_receipt(db, "plus", month_start, [
        ("item", 30000, "Vlees & vis", "Kipfilet"), ("discount", -5000, None, None), ("item", 4000, "Huishouden & verzorging", "Afwasmiddel"),
    ])
    make_receipt(db, "jumbo", month_start, [("item", 15000, "Zuivel & eieren", "Melk")])
    page = session.get("/overview").text
    assert "€400.00 reference" in page
    assert '<p class="figure">€400.00</p>' in page  # 25000 + 15000 food; the 40.00 of household is not food
    assert "€440.00" in page  # all purchases
    assert page.count('role="img"') >= 4  # meter and three charts
    assert "Food spend per month" in page and "Where it went" in page and "Per store" in page
    assert "Vlees &amp; vis" in page and "€250.00" in page  # 300.00 - 50.00 discount
    assert "(not food)" in page and "Plus" in page and "Jumbo" in page


def test_the_reference_comes_from_the_setting(session, db, month_start):
    session.app.state.settings.monthly_reference_eur = 250
    assert "€250.00 reference" in session.get("/overview").text


@pytest.mark.parametrize("spent,state", [(10000, "state-ok"), (34000, "state-warn"), (45000, "state-over")])
def test_meter_state_follows_the_share_of_the_reference(session, db, month_start, spent, state):
    make_receipt(db, "plus", month_start, [("item", spent, "Zuivel & eieren", "Melk")])
    page = session.get("/overview").text
    assert f'class="viz-fill {state}"' in page and f'class="state {state}"' in page


def test_the_over_state_says_by_how_much_with_words(session, db, month_start):
    make_receipt(db, "plus", month_start, [("item", 45000, "Zuivel & eieren", "Melk")])
    assert "Over the reference by €50.00" in session.get("/overview").text


def test_comparison_with_the_previous_month(session, db, month_start):
    last = add_months(month_start, -1)
    make_receipt(db, "plus", last, [("item", 30000, "Zuivel & eieren", "Melk")])
    make_receipt(db, "plus", month_start, [("item", 33000, "Zuivel & eieren", "Melk")])
    page = session.get("/overview").text
    assert "€30.00 (10%)" in page and "more than last month (€300.00)" in page and "trend-bad" in page
    make_receipt(db, "plus", month_start, [("item", -6000, "Zuivel & eieren", None)], total=-6000)
    assert "less than last month" in session.get("/overview").text and "trend-good" in session.get("/overview").text


def test_navigation_between_months(session, db, month_start):
    page = session.get("/overview").text
    assert 'aria-label="Next month"' not in page  # nothing after the current month
    assert f"month={add_months(month_start, -1):%Y-%m}" in page
    previous = session.get(f"/overview?month={add_months(month_start, -1):%Y-%m}").text
    assert 'aria-label="Next month"' in previous and f"{add_months(month_start, -1):%B %Y}" in previous


@pytest.mark.parametrize("value", ["abc", "2026-13", "2026-00", "99999-01", "1999-05", "", "2026-1"])
def test_invalid_months_fall_back_to_the_current_month(session, month_start, value):
    assert f"{month_start:%B %Y}" in session.get("/overview", params={"month": value}).text


def test_future_months_are_clamped_to_the_current_month(session, month_start):
    future = add_months(month_start, 3)
    page = session.get(f"/overview?month={future:%Y-%m}").text
    assert f"{month_start:%B %Y}" in page


def test_a_past_month_shows_its_own_receipts(session, db, month_start):
    last = add_months(month_start, -1)
    make_receipt(db, "lidl", last + timedelta(days=4), [("item", 12345, "Groente & fruit", "Appels")])
    page = session.get(f"/overview?month={last:%Y-%m}").text
    assert "€123.45" in page and "Lidl" in page and "No saved receipts" not in page
    current = session.get("/overview").text
    assert '<p class="figure">€0.00</p>' in current  # this month has nothing yet ...
    assert "less than last month (€123.45)" in current  # ... and is compared with the past month


def test_names_from_the_database_are_escaped_in_the_charts(session, db, month_start):
    store = db.scalar(select(Store).where(Store.chain == "other"))
    store.name = '<script>alert("s")</script>'
    db.commit()
    make_receipt(db, "other", month_start, [("item", 1000, "Overig", "Ding")])
    page = session.get("/overview").text
    assert "<script>alert" not in page and "&lt;script&gt;" in page


def test_chart_tooltips_and_tables_carry_the_values(session, db, month_start):
    make_receipt(db, "plus", month_start, [("item", 12345, "Zuivel & eieren", "Melk")])
    page = session.get("/overview").text
    assert f'data-tip="{month_start:%B %Y}: €123.45 food"' in page
    assert "<summary>Table</summary>" in page and page.count("<table") == 3
    assert "/static/js/charts.js" in page


# --- regulars ---------------------------------------------------------------

def test_regulars_empty_state_and_tabs(session):
    page = session.get("/regulars").text
    assert "No products yet" in page and 'aria-current="page"' in page
    for label in ("This month", "Last 3 months", "Last 12 months", "All time"):
        assert label in page


def test_regulars_ranks_products_by_visits(session, db, month_start):
    for offset in range(3):
        make_receipt(db, "plus", month_start + timedelta(days=offset), [("item", 129, "Zuivel & eieren", "Halfvolle melk 1L")])
    make_receipt(db, "jumbo", month_start, [("item", 899, "Vlees & vis", "Kipfilet"), ("item", 50, None, None)])
    page = session.get("/regulars?period=all").text
    assert page.index("Halfvolle melk 1L") < page.index("Kipfilet")
    assert "€1.29" in page and "1 product line without a product name is not counted" in page


def test_regulars_period_filter_and_invalid_period(session, db, month_start):
    make_receipt(db, "plus", month_start - timedelta(days=200), [("item", 300, "Groente & fruit", "Oude appels")])
    make_receipt(db, "plus", month_start, [("item", 129, "Zuivel & eieren", "Melk")])
    assert "Oude appels" not in session.get("/regulars?period=3m").text
    assert "Oude appels" in session.get("/regulars?period=all").text
    assert "Oude appels" not in session.get("/regulars?period=bogus").text  # falls back to 3 months


def test_regulars_escapes_product_names(session, db, month_start):
    make_receipt(db, "plus", month_start, [("item", 129, "Overig", "<img src=x onerror=alert(1)>")])
    page = session.get("/regulars").text
    assert "<img src=x" not in page and "&lt;img src=x" in page


def test_home_links_to_the_analytics_pages(session):
    home = session.get("/").text
    assert 'href="/overview"' in home and 'href="/regulars"' in home
