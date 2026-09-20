from datetime import date, timedelta

import pytest
from sqlalchemy import select

from grocery.db.models import PriceObservation, Product, Receipt, Store
from grocery.jobs.handlers import process_one
from grocery.prices.feedback import feedback_for
from grocery.prices.observations import comparable
from grocery.products.normalize import product_key
from grocery.receipts.service import (
    ConfirmInput,
    LineInput,
    confirm_receipt,
    create_from_upload,
    delete_receipt,
    quick_add,
)
from tests.fakes import FakeReader, jpeg_bytes, line, plus_receipt

TODAY = date(2026, 9, 20)


def store(db, chain):
    return db.scalar(select(Store).where(Store.chain == chain))


def product(db, name="Halfvolle melk 1L"):
    p = Product(name=name, name_key=product_key(name))
    db.add(p)
    db.flush()
    return p


def obs(db, p, chain, price, day=TODAY, source="receipt", unit=None, basis=None, promo=False, valid_until=None, ref=None):
    o = PriceObservation(
        product_id=p.id, store_id=store(db, chain).id, price_cents=price, unit_price_cents=unit, unit_basis=basis,
        is_promo=promo, observed_on=day, valid_until=valid_until, source=source, source_ref_id=ref,
    )
    db.add(o)
    db.commit()
    return o


# --- comparable -------------------------------------------------------------

def test_comparable_prefers_the_unit_price_when_there_is_one():
    assert comparable(742, 849, "kg") == ("kg", 849)
    assert comparable(129, None, None) == ("pack", 129)
    assert comparable(129, 129, None) == ("pack", 129)  # a unit price without a basis is ignored


# --- feedback ---------------------------------------------------------------

def test_first_price_for_a_product(client, db):
    p = product(db)
    fb = feedback_for(db, p.id, store(db, "plus").id, "pack", 129, TODAY)
    assert fb.verdict == "first" and fb.usual is None and fb.cheaper_elsewhere == []


@pytest.mark.parametrize("price,verdict", [(100, "cheaper"), (113, "cheaper"), (116, "usual"), (120, "usual"), (124, "usual"), (127, "pricier"), (140, "pricier")])
def test_verdict_bands_around_the_usual_price(client, db, price, verdict):
    p = product(db)
    for i in range(3):
        obs(db, p, "plus", 120, TODAY - timedelta(days=10 * (i + 1)))
    fb = feedback_for(db, p.id, store(db, "plus").id, "pack", price, TODAY)
    assert fb.usual == 120 and fb.verdict == verdict
    assert fb.pct_vs_usual == pytest.approx((price - 120) / 120)


def test_usual_is_the_median_of_the_last_five_paid_prices(client, db):
    p = product(db)
    for i, price in enumerate([500, 110, 120, 130, 140, 150]):  # 500 is the oldest and drops out
        obs(db, p, "plus", price, TODAY - timedelta(days=60 - i * 10))
    fb = feedback_for(db, p.id, store(db, "plus").id, "pack", 125, TODAY)
    assert fb.usual == 130 and fb.usual_count == 5


def test_usual_prefers_receipts_over_shelf_sightings(client, db):
    p = product(db)
    obs(db, p, "plus", 200, TODAY - timedelta(days=20), source="receipt")
    obs(db, p, "lidl", 90, TODAY - timedelta(days=2), source="shelf")
    fb = feedback_for(db, p.id, store(db, "plus").id, "pack", 190, TODAY)
    assert fb.usual == 200  # what I actually paid, not the shelf price seen elsewhere


def test_shelf_only_history_still_gives_a_reference_but_skips_promotions(client, db):
    p = product(db)
    obs(db, p, "jumbo", 150, TODAY - timedelta(days=5), source="shelf")
    obs(db, p, "jumbo", 99, TODAY - timedelta(days=2), source="shelf", promo=True)
    fb = feedback_for(db, p.id, store(db, "plus").id, "pack", 150, TODAY)
    assert fb.usual == 150 and fb.verdict == "usual"


def test_cheaper_elsewhere_lists_the_best_recent_price_per_other_store(client, db):
    p = product(db)
    obs(db, p, "plus", 129, TODAY - timedelta(days=15))
    obs(db, p, "lidl", 99, TODAY - timedelta(days=3), source="shelf")
    obs(db, p, "lidl", 109, TODAY - timedelta(days=1), source="shelf")  # same store: best one wins
    obs(db, p, "jumbo", 119, TODAY - timedelta(days=9), source="shelf")
    obs(db, p, "aldi", 129, TODAY - timedelta(days=2), source="shelf")  # not cheaper
    obs(db, p, "ah", 80, TODAY - timedelta(days=90), source="shelf")  # too old
    fb = feedback_for(db, p.id, store(db, "plus").id, "pack", 129, TODAY)
    assert [(s.store, s.value) for s in fb.cheaper_elsewhere] == [("Lidl", 99), ("Jumbo", 119)]


def test_the_same_store_and_ended_promotions_are_never_suggested(client, db):
    p = product(db)
    obs(db, p, "plus", 129, TODAY - timedelta(days=15))
    obs(db, p, "plus", 89, TODAY - timedelta(days=1), source="shelf")  # same store as this capture
    obs(db, p, "lidl", 79, TODAY - timedelta(days=10), source="shelf", promo=True, valid_until=TODAY - timedelta(days=3))
    fb = feedback_for(db, p.id, store(db, "plus").id, "pack", 129, TODAY)
    assert fb.cheaper_elsewhere == []


def test_only_the_same_basis_is_compared(client, db):
    p = product(db, "Kipfilet")
    obs(db, p, "turkish", 742, TODAY - timedelta(days=5), unit=849, basis="kg")
    obs(db, p, "plus", 450, TODAY - timedelta(days=5))  # per pack: not comparable with per kg
    fb = feedback_for(db, p.id, store(db, "plus").id, "kg", 1199, TODAY)
    assert fb.usual == 849 and fb.verdict == "pricier"
    assert fb.cheaper_elsewhere[0].store == "Turkish supermarket"


def test_a_capture_is_not_compared_with_itself(client, db):
    p = product(db)
    obs(db, p, "plus", 129, TODAY - timedelta(days=10))
    me = obs(db, p, "plus", 60, TODAY, source="shelf", ref=42)
    fb = feedback_for(db, p.id, store(db, "plus").id, "pack", 60, TODAY, exclude=("shelf", 42))
    assert fb.usual == 129 and me.id  # the just-saved 0.60 is not part of "usual"


def test_old_observations_are_ignored(client, db):
    p = product(db)
    obs(db, p, "plus", 50, TODAY - timedelta(days=400))
    assert feedback_for(db, p.id, store(db, "plus").id, "pack", 129, TODAY).verdict == "first"


# --- observations from receipts --------------------------------------------

def _confirmed_receipt(client, db):
    settings = client.app.state.settings
    receipt = create_from_upload(db, settings, [jpeg_bytes()])
    process_one(db, settings, lambda s: FakeReader(plus_receipt()))
    db.refresh(receipt)
    data = ConfirmInput(
        store_chain="plus", purchased_on=receipt.purchased_on, total_cents=receipt.total_cents,
        lines=[LineInput(l.line_no, l.kind, l.raw_text, l.quantity_milli, l.unit, l.unit_price_cents,
                         l.line_total_cents, l.suggested_name or "", l.category_id) for l in receipt.lines],
    )
    confirm_receipt(db, settings, receipt, data)
    return receipt


def test_confirming_a_receipt_records_one_observation_per_product_line(client, db):
    receipt = _confirmed_receipt(client, db)
    rows = db.scalars(select(PriceObservation).where(PriceObservation.source_ref_id == receipt.id)).all()
    assert len(rows) == 2  # the discount and the deposit are not products
    melk = next(r for r in rows if r.unit_basis is None)
    kip = next(r for r in rows if r.unit_basis == "kg")
    assert (melk.price_cents, melk.store.chain, melk.observed_on) == (129, "plus", date(2026, 9, 19))
    assert (kip.price_cents, kip.unit_price_cents) == (742, 849)


def test_reconfirming_replaces_instead_of_duplicating(client, db):
    receipt = _confirmed_receipt(client, db)
    settings = client.app.state.settings
    data = ConfirmInput(
        store_chain="plus", purchased_on=receipt.purchased_on, total_cents=receipt.total_cents,
        lines=[LineInput(l.line_no, l.kind, l.raw_text, l.quantity_milli, l.unit, l.unit_price_cents,
                         l.line_total_cents, l.product.name if l.product else "", l.category_id) for l in receipt.lines],
    )
    confirm_receipt(db, settings, receipt, data)
    assert db.scalars(select(PriceObservation).where(PriceObservation.source_ref_id == receipt.id)).all().__len__() == 2


def test_deleting_a_receipt_removes_its_observations(client, db):
    receipt = _confirmed_receipt(client, db)
    delete_receipt(db, client.app.state.settings, receipt)
    assert db.scalar(select(PriceObservation)) is None


def test_quick_add_feeds_the_price_history_with_the_per_kg_price(client, db):
    quick_add(db, store_chain="turkish", purchased_on=TODAY, description="", total_cents=None,
              weight_milli=874, price_per_kg_cents=849)
    row = db.scalar(select(PriceObservation))
    assert (row.store.chain, row.unit_basis, row.unit_price_cents, row.price_cents) == ("turkish", "kg", 849, 742)


def test_a_receipt_without_a_date_or_store_records_nothing(client, db):
    settings = client.app.state.settings
    receipt = create_from_upload(db, settings, [jpeg_bytes()])
    process_one(db, settings, lambda s: FakeReader(plus_receipt(date="unreadable", chain="unknown")))
    db.refresh(receipt)
    from grocery.prices.observations import record_receipt

    assert record_receipt(db, receipt) == 0
