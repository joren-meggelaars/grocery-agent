from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from grocery.db.base import utcnow
from grocery.db.models import (
    Category,
    Extraction,
    Job,
    NameMapping,
    Product,
    ProductEan,
    Receipt,
    ReceiptFile,
    Store,
)
from grocery.jobs import queue
from grocery.jobs.handlers import process_one
from grocery.llm.client import ReaderUnavailable
from grocery.receipts.service import (
    ConfirmError,
    ConfirmInput,
    LineInput,
    confirm_receipt,
    create_from_upload,
    delete_receipt,
    quick_add,
    retry_receipt,
)
from grocery.uploads.images import UploadError
from grocery.uploads.storage import _resolve
from tests.fakes import FakeReader, ean13, failed, jpeg_bytes, line, off_found, off_missing, plus_receipt


@pytest.fixture
def env(client, db):
    return client.app.state.settings, db


def upload(env, n=1):
    settings, db = env
    return create_from_upload(db, settings, [jpeg_bytes(color=(i * 40, 0, 0)) for i in range(n)])


def run_job(env, reader, now=None):
    settings, db = env
    return process_one(db, settings, lambda s: reader, now)


def confirm_input(receipt, **overrides):
    """What the review form posts back for the receipt as extracted."""
    lines = [
        LineInput(l.line_no, l.kind, l.raw_text, l.quantity_milli, l.unit, l.unit_price_cents,
                  l.line_total_cents, (l.product.name if l.product else l.suggested_name) or "", l.category_id)
        for l in receipt.lines
    ]
    base = dict(store_chain="plus", purchased_on=receipt.purchased_on, total_cents=receipt.total_cents, lines=lines)
    base.update(overrides)
    return ConfirmInput(**base)


# --- upload -----------------------------------------------------------------

def test_upload_creates_receipt_files_and_a_job(env):
    settings, db = env
    receipt = upload(env, n=2)
    assert receipt.status == "extracting" and receipt.source == "photo"
    assert [rf.page_no for rf in receipt.files] == [1, 2]
    job = db.scalar(select(Job))
    assert (job.kind, job.payload, job.status) == ("extract_receipt", {"receipt_id": receipt.id}, "queued")
    for rf in receipt.files:
        assert _resolve(settings, rf.file.path).exists()
        remaining = rf.file.delete_after - utcnow()
        assert timedelta(days=29) < remaining <= timedelta(days=30)


@pytest.mark.parametrize(
    "files,message",
    [
        ([], "at least one"),
        ([b""], "empty"),
        ([b"<html>hi</html>"], "Unsupported"),
        ([jpeg_bytes()] * 7, "at most"),
        ([b"%PDF-1.4 x", jpeg_bytes()], "not a mix"),
    ],
)
def test_upload_rejects_bad_input(env, files, message):
    settings, db = env
    with pytest.raises(UploadError, match=message):
        create_from_upload(db, settings, files)
    assert db.scalar(select(Receipt)) is None and db.scalar(select(Job)) is None


def test_upload_rejects_oversized_files(env):
    settings, db = env
    settings.max_upload_bytes = 100
    with pytest.raises(UploadError, match="larger than"):
        create_from_upload(db, settings, [jpeg_bytes()])


def test_the_same_photo_twice_is_flagged(env):
    first = upload(env)
    second = upload(env)
    assert first.flags == [] and second.flags == ["duplicate_photo"]


# --- extraction -------------------------------------------------------------

def test_extraction_fills_the_receipt_and_logs_usage(env):
    settings, db = env
    receipt = upload(env)
    reader = FakeReader(plus_receipt())
    assert run_job(env, reader) is True

    db.refresh(receipt)
    assert receipt.status == "needs_review"
    assert receipt.store.chain == "plus" and receipt.purchased_on == date(2026, 9, 19)
    assert receipt.total_cents == 846 and receipt.sum_delta_cents == 0 and receipt.flags == []
    assert [l.kind for l in receipt.lines] == ["item", "item", "discount", "deposit"]
    assert receipt.lines[1].quantity_milli == 874 and receipt.lines[1].unit == "kg"
    assert receipt.lines[0].suggested_name == "Halfvolle melk 1L"
    zuivel = db.scalar(select(Category).where(Category.name == "Zuivel & eieren"))
    assert receipt.lines[0].category_id == zuivel.id
    assert reader.calls == [(1, None)]

    ex = db.get(Extraction, receipt.extraction_id)
    assert ex.parsed_ok and ex.input_tokens == 3000 and ex.output_tokens == 900
    assert ex.cost_est_eur == pytest.approx((3000 * 2 + 900 * 10) / 1e6 * settings.usd_to_eur, rel=1e-6)
    assert ex.raw_json["total_cents"] == 846  # the raw model output is kept verbatim
    assert db.scalar(select(Job)).status == "done"


def test_a_receipt_that_does_not_add_up_is_flagged(env):
    _, db = env
    receipt = upload(env)
    run_job(env, FakeReader(plus_receipt(total=900)))
    db.refresh(receipt)
    assert receipt.status == "needs_review"
    assert receipt.sum_delta_cents == 54 and "sum_mismatch" in receipt.flags


def test_unreadable_date_becomes_a_flag_not_a_crash(env):
    _, db = env
    receipt = upload(env)
    run_job(env, FakeReader(plus_receipt(date="19-09-2026")))
    db.refresh(receipt)
    assert receipt.purchased_on is None and "no_date" in receipt.flags


def test_unknown_chain_leaves_the_store_empty(env):
    _, db = env
    receipt = upload(env)
    run_job(env, FakeReader(plus_receipt(chain="unknown")))
    db.refresh(receipt)
    assert receipt.store_id is None


def test_pdf_with_text_layer_sends_text_instead_of_images(env):
    settings, db = env
    receipt = upload(env)
    receipt.source_text = "PLUS ... TOTAAL 8,46"
    db.commit()
    reader = FakeReader(plus_receipt())
    run_job(env, reader)
    assert reader.calls == [(0, "PLUS ... TOTAAL 8,46")]


def test_permanent_model_failure_fails_the_receipt_with_a_clear_message(env):
    _, db = env
    receipt = upload(env)
    run_job(env, FakeReader(failed("The model's answer was cut off (receipt too long).")))
    db.refresh(receipt)
    assert receipt.status == "failed" and "cut off" in receipt.error
    ex = db.scalar(select(Extraction))
    assert ex.parsed_ok is False and "cut off" in ex.error
    assert db.scalar(select(Job)).status == "done"  # handled: the failure is on the receipt


def test_transient_failure_is_retried_with_backoff_then_succeeds(env):
    _, db = env
    receipt = upload(env)
    now = utcnow()
    reader = FakeReader([failed("APIConnectionError", retryable=True), plus_receipt()])

    run_job(env, reader, now)
    db.refresh(receipt)
    job = db.scalar(select(Job))
    assert receipt.status == "extracting" and job.status == "queued" and job.attempts == 1
    assert job.run_after > now  # backoff

    assert run_job(env, reader, now) is False  # not due yet
    run_job(env, reader, now + timedelta(minutes=5))
    db.refresh(receipt)
    assert receipt.status == "needs_review"


def test_retries_are_bounded_and_then_the_receipt_fails(env):
    _, db = env
    receipt = upload(env)
    reader = FakeReader(failed("overloaded", retryable=True))
    now = utcnow()
    for attempt in range(queue.MAX_ATTEMPTS):
        assert run_job(env, reader, now + timedelta(hours=attempt)) is True
    db.refresh(receipt)
    assert receipt.status == "failed" and "several attempts" in receipt.error
    assert db.scalar(select(Job)).status == "failed"


def test_missing_api_key_gives_an_actionable_error(env):
    settings, db = env
    receipt = upload(env)

    def no_key(_settings):
        raise ReaderUnavailable("ANTHROPIC_API_KEY is not set")

    process_one(db, settings, no_key)
    db.refresh(receipt)
    assert receipt.status == "failed" and "ANTHROPIC_API_KEY" in receipt.error


def test_budget_exhausted_blocks_new_extractions(env):
    settings, db = env
    settings.llm_monthly_budget_eur = 1.0
    db.add(Extraction(kind="receipt", model="claude-sonnet-5", prompt_version="x", cost_est_eur=1.2))
    receipt = upload(env)
    reader = FakeReader(plus_receipt())
    run_job(env, reader)
    db.refresh(receipt)
    assert receipt.status == "failed" and "budget" in receipt.error.lower()
    assert reader.calls == []  # no API call was made


def test_budget_counts_only_the_current_month(env):
    settings, db = env
    settings.llm_monthly_budget_eur = 1.0
    last_month = utcnow().replace(day=1) - timedelta(days=2)
    db.add(Extraction(kind="receipt", model="m", prompt_version="x", cost_est_eur=5.0, created_at=last_month))
    receipt = upload(env)
    run_job(env, FakeReader(plus_receipt()))
    db.refresh(receipt)
    assert receipt.status == "needs_review"


def test_failed_receipt_can_be_retried(env):
    _, db = env
    receipt = upload(env)
    run_job(env, FakeReader(failed("cut off")))
    db.refresh(receipt)
    retry_receipt(db, receipt)
    assert receipt.status == "extracting"
    run_job(env, FakeReader(plus_receipt()))
    db.refresh(receipt)
    assert receipt.status == "needs_review"


def test_a_crashing_extraction_does_not_kill_the_worker(env):
    settings, db = env
    receipt = upload(env)

    class Boom(FakeReader):
        def read(self, images, text):
            raise RuntimeError("boom")

    run_job(env, Boom())
    db.refresh(receipt)
    assert receipt.status == "failed" and db.scalar(select(Job)).status == "failed"


# --- confirm + learning -----------------------------------------------------

def extracted(env, **kw):
    _, db = env
    receipt = upload(env)
    run_job(env, FakeReader(plus_receipt(**kw)))
    db.refresh(receipt)
    return receipt


def test_confirm_stores_products_mappings_and_schedules_photo_deletion(env):
    settings, db = env
    receipt = extracted(env)
    now = utcnow()
    result = confirm_receipt(db, settings, receipt, confirm_input(receipt), now)

    assert result.delta_cents == 0
    assert receipt.status == "confirmed" and receipt.confirmed_at == now
    assert {p.name for p in db.scalars(select(Product))} == {"Halfvolle melk 1L", "Kipfilet"}
    m = db.scalar(select(NameMapping).where(NameMapping.raw_norm == "plus hv melk 1l"))
    assert m.chain == "plus" and m.confirmed_count == 1
    assert all(rf.file.delete_after == now + timedelta(days=7) for rf in receipt.files)
    assert [l.match_source for l in receipt.lines if l.kind == "item"] == ["manual", "manual"]


def test_corrections_are_learned_and_reused_on_the_next_receipt(env):
    settings, db = env
    first = extracted(env)
    data = confirm_input(first)
    data.lines[0].product_name = "Melk halfvol 1L"  # user renames the product
    cat = db.scalar(select(Category).where(Category.name == "Dranken"))
    data.lines[0].category_id = cat.id
    confirm_receipt(db, settings, first, data)

    second = extracted(env, date="2026-09-26")
    melk = second.lines[0]
    assert melk.match_source == "mapping" and melk.match_confidence == 1.0
    assert melk.product.name == "Melk halfvol 1L"
    assert melk.category_id == cat.id  # the learned product's category, not the model's guess

    confirm_receipt(db, settings, second, confirm_input(second))
    m = db.scalar(select(NameMapping).where(NameMapping.raw_norm == "plus hv melk 1l"))
    assert m.confirmed_count == 2
    assert [l.match_source for l in second.lines if l.kind == "item"][0] == "mapping"  # kept, not "manual"


def test_repointing_a_mapping_replaces_the_old_answer(env):
    settings, db = env
    first = extracted(env)
    confirm_receipt(db, settings, first, confirm_input(first))
    second = extracted(env, date="2026-09-26")
    data = confirm_input(second)
    data.lines[0].product_name = "Andere melk"
    confirm_receipt(db, settings, second, data)
    m = db.scalar(select(NameMapping).where(NameMapping.raw_norm == "plus hv melk 1l"))
    assert db.get(Product, m.product_id).name == "Andere melk" and m.confirmed_count == 1


def test_confirm_marks_edited_lines(env):
    settings, db = env
    receipt = extracted(env)
    data = confirm_input(receipt)
    data.lines[0].line_total_cents = 139
    data.lines[0].unit_price_cents = 139
    data.total_cents = 856
    confirm_receipt(db, settings, receipt, data)
    assert [l.edited for l in receipt.lines] == [True, False, False, False]


def test_added_line_and_deleted_line(env):
    settings, db = env
    receipt = extracted(env)
    data = confirm_input(receipt)
    data.lines = data.lines[:2] + [LineInput(None, "item", "BROODJE", 1000, "pcs", None, 54, "Broodje", None)]
    data.total_cents = 129 + 742 + 54
    confirm_receipt(db, settings, receipt, data)
    assert [l.raw_text for l in receipt.lines] == ["PLUS HV MELK 1L", "KIPFILET 0,874 KG", "BROODJE"]
    assert receipt.lines[2].edited and receipt.lines[2].match_source == "manual"


def test_mismatch_needs_an_explicit_override(env):
    settings, db = env
    receipt = extracted(env)
    data = confirm_input(receipt, total_cents=900)
    with pytest.raises(ConfirmError, match="below the total"):
        confirm_receipt(db, settings, receipt, data)
    assert receipt.status == "needs_review"

    data.accept_mismatch = True
    confirm_receipt(db, settings, receipt, data)
    assert receipt.status == "confirmed" and receipt.sum_delta_cents == 54 and "sum_mismatch" in receipt.flags


@pytest.mark.parametrize("field,value,message", [
    ("purchased_on", None, "date"), ("total_cents", None, "total"), ("lines", [], "at least one line"),
])
def test_confirm_requires_date_total_and_lines(env, field, value, message):
    settings, db = env
    receipt = extracted(env)
    with pytest.raises(ConfirmError, match=message):
        confirm_receipt(db, settings, receipt, confirm_input(receipt, **{field: value}))


def test_possible_duplicate_is_flagged_on_the_second_receipt(env):
    settings, db = env
    first = extracted(env)
    confirm_receipt(db, settings, first, confirm_input(first))
    second = extracted(env)  # same store, date and total
    assert "possible_duplicate" in second.flags


def test_delete_removes_files_from_disk(env):
    settings, db = env
    receipt = upload(env)
    paths = [_resolve(settings, rf.file.path) for rf in receipt.files]
    assert all(p.exists() for p in paths)
    delete_receipt(db, settings, receipt)
    assert not any(p.exists() for p in paths)
    assert db.scalar(select(Receipt)) is None and db.scalar(select(ReceiptFile)) is None


# --- quick add --------------------------------------------------------------

def test_quick_add_weighed_chicken_at_the_turkish_supermarket(env):
    _, db = env
    r = quick_add(db, store_chain="turkish", purchased_on=date(2026, 9, 20), description="",
                  total_cents=None, weight_milli=874, price_per_kg_cents=849)
    assert (r.status, r.source, r.total_cents) == ("confirmed", "manual", 742)
    l = r.lines[0]
    assert (l.raw_text, l.unit, l.quantity_milli, l.unit_price_cents) == ("Kip", "kg", 874, 849)
    assert l.product.category.name == "Vlees & vis"


def test_quick_add_bakery_flat_amount(env):
    _, db = env
    r = quick_add(db, store_chain="bakery", purchased_on=date(2026, 9, 20), description="Volkoren brood",
                  total_cents=395)
    assert r.total_cents == 395 and r.lines[0].product.category.name == "Brood & banket"
    assert r.lines[0].unit == "pcs" and r.sum_delta_cents == 0


@pytest.mark.parametrize("kw,message", [
    (dict(store_chain="nope", total_cents=100), "store"),
    (dict(store_chain="bakery", total_cents=None), "amount"),
    (dict(store_chain="bakery", total_cents=0), "amount"),
    (dict(store_chain="bakery", total_cents=100, purchased_on=None), "date"),
])
def test_quick_add_validation(env, kw, message):
    _, db = env
    args = dict(purchased_on=date(2026, 9, 20), description="x")
    args.update(kw)
    with pytest.raises(ConfirmError, match=message):
        quick_add(db, **args)


def test_quick_add_with_a_barcode_and_a_typed_name_links_the_two(env):
    settings, db = env
    ean = ean13("871040001234")
    r = quick_add(db, store_chain="plus", purchased_on=date(2026, 9, 20), description="Pastinaak",
                  total_cents=99, ean_text=ean)
    link = db.get(ProductEan, ean)
    assert link is not None and link.product_id == r.lines[0].product_id and link.source == "scan"


def test_quick_add_with_only_a_barcode_reuses_the_product_it_is_already_linked_to(env):
    settings, db = env
    ean = ean13("871040001234")
    first = quick_add(db, store_chain="plus", purchased_on=date(2026, 9, 20), description="Pastinaak", total_cents=99, ean_text=ean)
    second = quick_add(db, store_chain="plus", purchased_on=date(2026, 9, 21), description="", total_cents=119, ean_text=ean)
    assert second.lines[0].product_id == first.lines[0].product_id
    assert second.lines[0].raw_text == "Pastinaak"


def test_an_unknown_barcode_without_a_name_is_looked_up_at_open_food_facts(env):
    settings, db = env
    ean = ean13("871040001234")
    r = quick_add(db, store_chain="plus", purchased_on=date(2026, 9, 20), description="", total_cents=249,
                  ean_text=ean, settings=settings, off_fetch=off_found(name="Goudse kaas"))
    assert r.lines[0].raw_text == "Goudse kaas"
    assert db.get(ProductEan, ean).product_id == r.lines[0].product_id


def test_an_unknown_barcode_with_nothing_found_falls_back_to_the_store_default(env):
    settings, db = env
    ean = ean13("871040001234")
    r = quick_add(db, store_chain="bakery", purchased_on=date(2026, 9, 20), description="", total_cents=395,
                  ean_text=ean, settings=settings, off_fetch=off_missing())
    assert r.lines[0].raw_text == "Brood"
    assert db.get(ProductEan, ean).product_id == r.lines[0].product_id


def test_a_bad_barcode_is_refused(env):
    _, db = env
    with pytest.raises(ConfirmError, match="barcode"):
        quick_add(db, store_chain="bakery", purchased_on=date(2026, 9, 20), description="", total_cents=395, ean_text="123")


def test_typing_a_different_name_repoints_an_existing_barcode(env):
    settings, db = env
    ean = ean13("871040001234")
    quick_add(db, store_chain="plus", purchased_on=date(2026, 9, 20), description="Pastinaak", total_cents=99, ean_text=ean)
    second = quick_add(db, store_chain="plus", purchased_on=date(2026, 9, 21), description="Winterpeen", total_cents=89, ean_text=ean)
    assert db.get(ProductEan, ean).product_id == second.lines[0].product_id


# --- job queue --------------------------------------------------------------

def test_queue_claims_in_order_and_only_when_due(env):
    _, db = env
    now = utcnow()
    later = queue.enqueue(db, "x", {}, run_after=now + timedelta(hours=1))
    first = queue.enqueue(db, "x", {}, run_after=now - timedelta(minutes=2))
    second = queue.enqueue(db, "x", {}, run_after=now - timedelta(minutes=1))
    db.commit()
    assert queue.claim_next(db, now).id == first.id
    assert queue.claim_next(db, now).id == second.id
    assert queue.claim_next(db, now) is None
    assert later.status == "queued"


def test_stuck_running_jobs_are_recovered(env):
    _, db = env
    now = utcnow()
    queue.enqueue(db, "x", {}, run_after=now - timedelta(hours=1))
    db.commit()
    job = queue.claim_next(db, now - timedelta(hours=1))
    assert job.status == "running"
    assert queue.recover_stuck(db, now) == 1
    assert job.status == "queued"


def test_unknown_job_kind_is_marked_failed_not_retried(env):
    settings, db = env
    queue.enqueue(db, "mystery", {})
    db.commit()
    process_one(db, settings, lambda s: None)
    assert db.scalar(select(Job)).status == "failed"
