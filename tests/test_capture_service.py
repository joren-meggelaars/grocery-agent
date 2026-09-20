from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select

from grocery.capture.service import (
    CaptureInput,
    confirm_capture,
    create_capture,
    delete_capture,
    normalise_unit_price,
    retry_capture,
    review_count,
)
from grocery.db.base import utcnow
from grocery.db.models import (
    Extraction,
    Job,
    PriceObservation,
    Product,
    ProductEan,
    ShelfCapture,
    Store,
)
from grocery.jobs.handlers import process_one
from grocery.llm.client import ReaderUnavailable
from grocery.products.normalize import product_key
from grocery.receipts.service import ConfirmError
from grocery.uploads.images import UploadError
from grocery.uploads.storage import _resolve
from tests.fakes import (
    FakeShelfReader,
    ean13,
    failed,
    jpeg_bytes,
    off_down,
    off_found,
    off_missing,
    shelf_label,
)

EAN = ean13("871040001234")


@pytest.fixture
def env(client, db):
    return client.app.state.settings, db


def new_capture(env, ean=EAN, store="plus", uuid=None, **kw):
    settings, db = env
    capture, created = create_capture(
        db, settings, client_uuid=uuid or str(uuid4()), ean_text=ean, store_chain=store,
        captured_at=kw.pop("captured_at", utcnow()), photo=kw.pop("photo", jpeg_bytes()), **kw,
    )
    return capture


def work(env, label=None, off=None, reader=None, now=None):
    settings, db = env
    reader = reader or FakeShelfReader(label if label is not None else shelf_label())
    off = off or off_found()
    while process_one(db, settings, lambda s: None, now, shelf_reader_factory=lambda s: reader, off_fetch=off):
        pass  # drain every job that is due
    db.expire_all()
    return reader


def form(**kw) -> CaptureInput:
    base = dict(
        ean_text=EAN, product_name="Halfvolle melk 1L", category_id=None, store_chain="plus", price_cents=129,
        effective_price_cents=None, unit_price_cents=129, unit_basis="l", promo_kind=None, promo_text="",
        requires_card=False, valid_from=None, valid_until=None,
    )
    base.update(kw)
    return CaptureInput(**base)


# --- upload -----------------------------------------------------------------

def test_capture_is_stored_and_queued(env):
    settings, db = env
    c = new_capture(env)
    assert (c.status, c.ean, c.store.chain, c.requires_card) == ("extracting", EAN, "plus", False)
    assert db.scalar(select(Job)).payload == {"capture_id": c.id}
    assert _resolve(settings, c.file.path).exists()
    assert timedelta(days=29) < c.file.delete_after - utcnow() <= timedelta(days=30)


def test_the_same_client_uuid_is_idempotent(env):
    settings, db = env
    uid = str(uuid4())
    first = new_capture(env, uuid=uid)
    second, created = create_capture(db, settings, client_uuid=uid, ean_text=EAN, store_chain="plus",
                                     captured_at=utcnow(), photo=jpeg_bytes("black"))
    assert created is False and second.id == first.id
    assert len(db.scalars(select(ShelfCapture)).all()) == 1 and len(db.scalars(select(Job)).all()) == 1


def test_a_capture_without_a_barcode_is_allowed(env):
    assert new_capture(env, ean="").ean is None
    assert new_capture(env, ean=None).ean is None


@pytest.mark.parametrize(
    "kw,message",
    [
        (dict(ean="4006381333932"), "barcode"),
        (dict(ean="abc"), "barcode"),
        (dict(uuid="not-a-uuid"), "capture id"),
        (dict(store="nope"), "store"),
        (dict(photo=b""), "photo"),
        (dict(photo=b"<html>x</html>"), "JPEG, PNG or WebP"),
        (dict(photo=b"%PDF-1.4 x"), "JPEG, PNG or WebP"),
    ],
)
def test_upload_validation(env, kw, message):
    with pytest.raises(UploadError, match=message):
        new_capture(env, **kw)
    assert env[1].scalar(select(ShelfCapture)) is None


def test_a_capture_time_in_the_future_is_clamped(env):
    now = utcnow()
    c = new_capture(env, captured_at=now + timedelta(days=30), now=now)
    assert c.captured_at == now


def test_the_capture_time_from_the_phone_is_kept(env):
    when = utcnow() - timedelta(hours=3)
    assert new_capture(env, captured_at=when).captured_at == when


def test_label_photos_are_downscaled(env):
    settings, db = env
    settings.label_max_edge = 200
    c = new_capture(env, photo=jpeg_bytes(size=(1600, 1200)))
    assert max(c.file.width, c.file.height) == 200


# --- worker step ------------------------------------------------------------

def test_label_is_read_and_prefilled(env):
    settings, db = env
    c = new_capture(env)
    reader = work(env, shelf_label(promo_kind="x_for_y", promo_text="2 voor 2.00", effective_price_cents=100,
                                   valid_until="2026-09-27", requires_card=True))
    assert reader.calls == [(1, None)]
    db.refresh(c)
    assert c.status == "needs_review"
    assert (c.price_cents, c.effective_price_cents) == (129, 100)
    assert (c.unit_price_cents, c.unit_basis) == (129, "l")
    assert (c.promo_kind, c.promo_text, c.requires_card) == ("x_for_y", "2 voor 2.00", True)
    assert c.valid_until == date(2026, 9, 27)
    assert c.product_name == "Halfvolle melk" and c.off_name == "Halfvolle melk, Campina, 1 L"
    ex = db.get(Extraction, c.extraction_id)
    assert ex.kind == "shelf" and ex.parsed_ok and ex.raw_json["price_cents"] == 129 and ex.cost_est_eur > 0


def test_open_food_facts_is_asked_once_and_the_answer_is_cached(env):
    settings, db = env
    off = off_found()
    new_capture(env)
    new_capture(env)
    work(env, off=off)
    work(env, off=off)
    assert len(off.calls) == 1


def test_a_failing_open_food_facts_does_not_stop_the_label_being_read(env):
    _, db = env
    c = new_capture(env)
    work(env, off=off_down())
    db.refresh(c)
    assert c.status == "needs_review" and c.off_name is None and c.product_name == "Halfvolle melk"


def test_known_barcode_links_straight_to_its_product(env):
    settings, db = env
    p = Product(name="Halfvolle melk 1L", name_key=product_key("Halfvolle melk 1L"))
    db.add(p)
    db.flush()
    db.add(ProductEan(ean=EAN, product_id=p.id, source="scan"))
    db.commit()
    c = new_capture(env)
    work(env, shelf_label(product_name="COMPLETELY DIFFERENT TEXT"))
    db.refresh(c)
    assert c.product_id == p.id and c.product_name == "Halfvolle melk 1L"


def test_barcode_printed_on_the_label_is_used_when_the_scan_failed(env):
    _, db = env
    c = new_capture(env, ean=None)
    off = off_found(name="Pindakaas")
    work(env, shelf_label(ean_on_label=" " + EAN + " "), off=off)
    db.refresh(c)
    assert c.ean == EAN and c.off_name.startswith("Pindakaas") and [call[0] for call in off.calls] == [EAN]


def test_a_wrong_barcode_on_the_label_is_ignored(env):
    _, db = env
    c = new_capture(env, ean=None)
    work(env, shelf_label(ean_on_label="4006381333932"))
    db.refresh(c)
    assert c.ean is None


def test_similar_existing_product_is_suggested_by_name(env):
    settings, db = env
    db.add(Product(name="Halfvolle melk", name_key=product_key("Halfvolle melk")))
    db.commit()
    c = new_capture(env)
    work(env, shelf_label(product_name="halfvolle  MELK"))
    db.refresh(c)
    assert c.product_name == "Halfvolle melk"


@pytest.mark.parametrize(
    "cents,per,expected",
    [
        (129, "kg", (129, "kg")), (129, "l", (129, "l")), (89, "100g", (890, "kg")),
        (45, "100ml", (450, "l")), (129, "pcs", (None, None)), (None, "kg", (None, None)), (129, None, (None, None)),
    ],
)
def test_unit_prices_are_normalised_to_kg_or_l(cents, per, expected):
    assert normalise_unit_price(cents, per) == expected


def test_unit_price_per_100g_is_stored_per_kg(env):
    _, db = env
    c = new_capture(env)
    work(env, shelf_label(unit_price_cents=89, unit_price_per="100g"))
    db.refresh(c)
    assert (c.unit_price_cents, c.unit_basis) == (890, "kg")


def test_bad_dates_from_the_model_are_dropped(env):
    _, db = env
    c = new_capture(env)
    work(env, shelf_label(valid_until="volgende week", valid_from="2026-02-30"))
    db.refresh(c)
    assert c.valid_until is None and c.valid_from is None and c.status == "needs_review"


def test_failure_modes(env):
    settings, db = env
    c = new_capture(env)
    work(env, reader=FakeShelfReader(failed("The model's answer was cut off (image too long).")))
    db.refresh(c)
    assert c.status == "failed" and "cut off" in c.error
    retry_capture(db, c)
    assert c.status == "extracting"
    work(env)
    db.refresh(c)
    assert c.status == "needs_review"


def test_transient_errors_retry_then_fail_cleanly(env):
    from grocery.jobs import queue

    _, db = env
    c = new_capture(env)
    reader = FakeShelfReader(failed("overloaded", retryable=True))
    now = utcnow()
    for attempt in range(queue.MAX_ATTEMPTS):
        work(env, reader=reader, now=now + timedelta(hours=attempt))
    db.refresh(c)
    assert c.status == "failed" and "several attempts" in c.error


def test_missing_key_and_budget_are_explained(env):
    settings, db = env
    c = new_capture(env)

    def no_key(_):
        raise ReaderUnavailable("ANTHROPIC_API_KEY is not set")

    process_one(db, settings, lambda s: None, shelf_reader_factory=no_key)
    db.refresh(c)
    assert c.status == "failed" and "ANTHROPIC_API_KEY" in c.error

    settings.llm_monthly_budget_eur = 0.5
    db.add(Extraction(kind="shelf", model="m", prompt_version="x", cost_est_eur=1.0))
    db.commit()
    c2 = new_capture(env)
    reader = work(env)
    db.refresh(c2)
    assert c2.status == "failed" and "budget" in c2.error.lower() and reader.calls == []


# --- confirm ----------------------------------------------------------------

def test_confirm_creates_product_barcode_link_and_price_observation(env):
    settings, db = env
    c = new_capture(env)
    work(env)
    now = utcnow()
    confirm_capture(db, settings, c, form(), now)

    assert c.status == "confirmed" and c.confirmed_at == now
    p = db.scalar(select(Product))
    assert p.name == "Halfvolle melk 1L"
    assert db.get(ProductEan, EAN).product_id == p.id
    o = db.scalar(select(PriceObservation))
    assert (o.source, o.source_ref_id, o.product_id, o.price_cents, o.unit_basis) == ("shelf", c.id, p.id, 129, "l")
    assert o.store.chain == "plus" and o.ean == EAN
    assert c.file.delete_after == now + timedelta(days=7)
    assert review_count(db) == 0


def test_the_observation_date_is_the_local_date_of_the_capture(env):
    settings, db = env
    c = new_capture(env, captured_at=datetime(2026, 9, 20, 22, 30, tzinfo=timezone.utc))  # 00:30 next day in NL
    confirm_capture(db, settings, c, form())
    assert db.scalar(select(PriceObservation)).observed_on == date(2026, 9, 21)


def test_promotion_price_is_what_gets_recorded(env):
    settings, db = env
    c = new_capture(env)
    confirm_capture(db, settings, c, form(price_cents=129, effective_price_cents=100, promo_kind="x_for_y",
                                          promo_text="2 voor 2.00", requires_card=True))
    o = db.scalar(select(PriceObservation))
    assert (o.price_cents, o.is_promo, o.requires_card, o.promo_text) == (100, True, True, "2 voor 2.00")
    assert o.unit_price_cents is None  # the printed unit price belongs to the normal price


def test_a_second_scan_of_the_same_barcode_reuses_the_product(env):
    settings, db = env
    confirm_capture(db, settings, new_capture(env), form())
    second = new_capture(env)
    work(env)
    assert second.product_name == "Halfvolle melk 1L"  # suggested from the link, no typing needed
    confirm_capture(db, settings, second, form(price_cents=119, unit_price_cents=None))
    assert len(db.scalars(select(Product)).all()) == 1 and len(db.scalars(select(PriceObservation)).all()) == 2


def test_relinking_a_barcode_replaces_the_old_answer(env):
    settings, db = env
    confirm_capture(db, settings, new_capture(env), form())
    confirm_capture(db, settings, new_capture(env), form(product_name="Melk halfvol"))
    assert db.get(ProductEan, EAN).product.name == "Melk halfvol"


def test_confirm_without_a_barcode_still_records_the_price(env):
    settings, db = env
    c = new_capture(env, ean=None)
    confirm_capture(db, settings, c, form(ean_text=""))
    assert db.scalar(select(ProductEan)) is None and db.scalar(select(PriceObservation)) is not None


def test_editing_a_confirmed_capture_replaces_its_observation(env):
    settings, db = env
    c = new_capture(env)
    confirm_capture(db, settings, c, form())
    confirm_capture(db, settings, c, form(price_cents=139, unit_price_cents=139))
    rows = db.scalars(select(PriceObservation)).all()
    assert len(rows) == 1 and rows[0].price_cents == 139


@pytest.mark.parametrize(
    "kw,message",
    [
        (dict(price_cents=None), "shelf price"),
        (dict(price_cents=0), "shelf price"),
        (dict(product_name="  "), "product name"),
        (dict(ean_text="4006381333932"), "barcode"),
        (dict(store_chain="nope"), "store"),
        (dict(effective_price_cents=0), "promotion price"),
        (dict(unit_basis="pcs"), "kg or l"),
        (dict(promo_kind="bogus"), "promotion type"),
    ],
)
def test_confirm_validation(env, kw, message):
    settings, db = env
    c = new_capture(env)
    with pytest.raises(ConfirmError, match=message):
        confirm_capture(db, settings, c, form(**kw))
    assert c.status == "extracting" and db.scalar(select(Product)) is None


def test_delete_removes_photo_and_observation(env):
    settings, db = env
    c = new_capture(env)
    confirm_capture(db, settings, c, form())
    path = _resolve(settings, c.file.path)
    delete_capture(db, settings, c)
    assert not path.exists()
    assert db.scalar(select(ShelfCapture)) is None and db.scalar(select(PriceObservation)) is None
    assert db.scalar(select(Product)) is not None  # the product and its barcode link stay


# --- which name is suggested ------------------------------------------------

def test_the_shelf_label_name_leads_over_the_open_food_facts_name(env):
    _, db = env
    c = new_capture(env)
    work(env, shelf_label(product_name="Volle melk"), off=off_found(name="Campina Whole Milk Long Name"))
    db.refresh(c)
    assert c.product_name == "Volle melk" and "Campina" in c.off_name


def test_open_food_facts_name_is_used_when_the_label_has_none(env):
    _, db = env
    c = new_capture(env)
    work(env, shelf_label(product_name=None), off=off_found(name="Pindakaas"))
    db.refresh(c)
    assert c.product_name == "Pindakaas"


def test_an_existing_product_is_recognised_through_the_open_food_facts_name(env):
    settings, db = env
    db.add(Product(name="Pindakaas", name_key=product_key("Pindakaas")))
    db.commit()
    c = new_capture(env)
    work(env, shelf_label(product_name="PK 350G"), off=off_found(name="Pindakaas"))
    db.refresh(c)
    assert c.product_name == "Pindakaas"  # the known product, not the cryptic label text


def test_no_name_anywhere_leaves_the_field_empty(env):
    _, db = env
    c = new_capture(env)
    work(env, shelf_label(product_name=None), off=off_missing())
    db.refresh(c)
    assert c.product_name is None and c.status == "needs_review"
