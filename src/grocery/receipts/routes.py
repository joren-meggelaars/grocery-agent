import logging
from datetime import date

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from grocery.db.models import Category, Product, Receipt, ReceiptLine, Setting, Store
from grocery.llm.budget import budget_state
from grocery.money import ParseError, format_cents, parse_euro, parse_quantity_milli
from grocery.receipts import forms
from grocery.receipts.service import (
    ConfirmError,
    confirm_receipt,
    create_from_upload,
    delete_receipt,
    quick_add,
    retry_receipt,
)
from grocery.receipts.validation import FLAG_MESSAGES, LineData, validate_receipt
from grocery.refdata import QUICKADD_CATEGORY, TURKISH_PRICE_KEY
from grocery.security.deps import Principal, current_principal, get_db
from grocery.uploads.images import UploadError
from grocery.uploads.storage import read_image
from grocery.web.templating import templates

log = logging.getLogger(__name__)
router = APIRouter(prefix="/receipts")

STATUS_LABEL = {
    "extracting": "Reading...",
    "needs_review": "Needs review",
    "confirmed": "Saved",
    "failed": "Failed",
}


def _get_receipt(db: Session, receipt_id: int) -> Receipt:
    receipt = db.get(Receipt, receipt_id)
    if receipt is None:
        raise HTTPException(404, "Receipt not found")
    return receipt


def _ctx(request: Request, principal: Principal, **extra) -> dict:
    return {"user": principal.user, "csrf_token": principal.csrf_token, **extra}


def _page(request, name, principal, status=200, **extra):
    return templates.TemplateResponse(request, name, _ctx(request, principal, **extra), status_code=status)


# --- list / upload ----------------------------------------------------------

@router.get("", response_class=HTMLResponse)
def receipt_list(
    request: Request, principal: Principal = Depends(current_principal), db: Session = Depends(get_db)
):
    receipts = db.scalars(
        select(Receipt).order_by(Receipt.purchased_on.desc().nulls_last(), Receipt.id.desc()).limit(100)
    ).all()
    return _page(
        request, "receipts/list.html", principal, receipts=receipts, labels=STATUS_LABEL,
        budget=budget_state(db, request.app.state.settings), fmt=format_cents,
    )


@router.get("/new", response_class=HTMLResponse)
def upload_form(request: Request, principal: Principal = Depends(current_principal)):
    return _page(request, "receipts/new.html", principal, error=None)


@router.post("/new", response_class=HTMLResponse)
async def upload(
    request: Request,
    photos: list[UploadFile] = File(default=[]),
    principal: Principal = Depends(current_principal),
    db: Session = Depends(get_db),
):
    settings = request.app.state.settings
    data = [await f.read() for f in photos if f.filename]
    try:
        receipt = await run_in_threadpool(create_from_upload, db, settings, data)
    except UploadError as exc:
        return _page(request, "receipts/new.html", principal, status=400, error=str(exc))
    return RedirectResponse(f"/receipts/{receipt.id}", status_code=303)


# --- manual quick add -------------------------------------------------------

def _quick_defaults(db: Session, chain: str) -> dict:
    category = QUICKADD_CATEGORY.get(chain)
    last = db.scalar(
        select(ReceiptLine)
        .join(Receipt, Receipt.id == ReceiptLine.receipt_id)
        .join(Store, Store.id == Receipt.store_id)
        .where(Store.chain == chain, Receipt.source == "manual", ReceiptLine.unit_price_cents.is_not(None))
        .order_by(Receipt.purchased_on.desc(), Receipt.id.desc())
        .limit(1)
    )
    price = last.unit_price_cents if last else None
    if price is None and chain == "turkish":
        setting = db.get(Setting, TURKISH_PRICE_KEY)
        price = int(setting.value) if setting else None
    return {"category": category, "price_per_kg": format_cents(price)}


@router.get("/quick", response_class=HTMLResponse)
def quick_form(
    request: Request, store: str = "bakery",
    principal: Principal = Depends(current_principal), db: Session = Depends(get_db),
):
    stores = db.scalars(select(Store).order_by(Store.id)).all()
    values = {"store": store, "date": date.today().isoformat(), "description": "", "total": "",
              "weight": "", **_quick_defaults(db, store)}
    return _page(request, "receipts/quick.html", principal, stores=stores, values=values, errors=[])


@router.post("/quick", response_class=HTMLResponse)
async def quick_submit(
    request: Request, principal: Principal = Depends(current_principal), db: Session = Depends(get_db)
):
    form = await request.form()
    get = lambda k: (form.get(k) or "").strip()  # noqa: E731
    errors = []
    purchased_on = None
    try:
        purchased_on = date.fromisoformat(get("purchased_on")) if get("purchased_on") else None
    except ValueError:
        errors.append("The date is not valid.")
    try:
        total = parse_euro(get("total")) if get("total") else None
        weight = parse_quantity_milli(get("weight")) if get("weight") else None
        price_per_kg = parse_euro(get("price_per_kg")) if get("price_per_kg") else None
    except ParseError:
        total = weight = price_per_kg = None
        errors.append("Check the amount, weight and price per kg.")

    receipt = None
    if not errors:
        try:
            receipt = await run_in_threadpool(
                quick_add, db, store_chain=get("store"), purchased_on=purchased_on,
                description=get("description"), total_cents=total, weight_milli=weight,
                price_per_kg_cents=price_per_kg,
            )
        except ConfirmError as exc:
            errors.extend(exc.messages)
    if receipt is not None:
        return RedirectResponse(f"/receipts/{receipt.id}", status_code=303)

    stores = db.scalars(select(Store).order_by(Store.id)).all()
    values = {"store": get("store"), "date": get("purchased_on"), "description": get("description"),
              "total": get("total"), "weight": get("weight"), "price_per_kg": get("price_per_kg")}
    return _page(request, "receipts/quick.html", principal, status=400, stores=stores, values=values, errors=errors)


# --- one receipt ------------------------------------------------------------

def _review_context(db: Session, receipt: Receipt, rows: list[dict], validation, errors: list[str]) -> dict:
    return dict(
        receipt=receipt, rows=rows, errors=errors,
        stores=db.scalars(select(Store).order_by(Store.id)).all(),
        categories=db.scalars(select(Category).order_by(Category.sort_order)).all(),
        product_names=db.scalars(select(Product.name).order_by(Product.name).limit(3000)).all(),
        flags=[FLAG_MESSAGES.get(f, f) for f in (validation.flags if validation else receipt.flags or [])],
        notes=(receipt.extraction.raw_json or {}).get("legibility_notes") if receipt.extraction else None,
        blank=forms.blank_row("__i__"), fmt=format_cents, page_count=len(receipt.files),
        selected_store=receipt.store.chain if receipt.store else "other",
        today=date.today().isoformat(),
    )


def _detail(request, principal, db, receipt, rows=None, errors=None, status=200, form_values=None, editing=False):
    validation = validate_receipt(
        [LineData(l.line_no, l.kind, l.quantity_milli, l.unit_price_cents, l.line_total_cents) for l in receipt.lines],
        receipt.total_cents, receipt.purchased_on,
    )
    ctx = _review_context(db, receipt, rows if rows is not None else forms.rows_from_receipt(receipt, validation),
                          validation, errors or [])
    ctx["form_values"] = form_values
    ctx["labels"] = STATUS_LABEL
    ctx["editing"] = editing or bool(errors)
    return _page(request, "receipts/detail.html", principal, status=status, **ctx)


@router.get("/{receipt_id}", response_class=HTMLResponse)
def receipt_detail(
    receipt_id: int, request: Request, edit: bool = False,
    principal: Principal = Depends(current_principal), db: Session = Depends(get_db),
):
    return _detail(request, principal, db, _get_receipt(db, receipt_id), editing=edit)


@router.get("/{receipt_id}/status")
def receipt_status(receipt_id: int, db: Session = Depends(get_db)):
    return JSONResponse({"status": _get_receipt(db, receipt_id).status})


@router.get("/{receipt_id}/image/{page}")
def receipt_image(receipt_id: int, page: int, request: Request, db: Session = Depends(get_db)):
    receipt = _get_receipt(db, receipt_id)
    match = next((rf for rf in receipt.files if rf.page_no == page), None)
    data = read_image(request.app.state.settings, match.file) if match else None
    if data is None:
        raise HTTPException(404, "Image not available (photos are removed a week after saving)")
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "private, no-store"})


@router.post("/{receipt_id}/confirm", response_class=HTMLResponse)
async def confirm(
    receipt_id: int, request: Request,
    principal: Principal = Depends(current_principal), db: Session = Depends(get_db),
):
    receipt = _get_receipt(db, receipt_id)
    if receipt.status not in ("needs_review", "confirmed"):
        raise HTTPException(409, "This receipt is not ready for review")
    form = await request.form()
    data, rows, errors = forms.parse_confirm_form(form)
    if not errors:
        try:
            await run_in_threadpool(confirm_receipt, db, request.app.state.settings, receipt, data)
        except ConfirmError as exc:
            errors = exc.messages
    if errors:
        return _detail(request, principal, db, receipt, rows=rows, errors=errors, status=422,
                       form_values={"store": data.store_chain, "purchased_on": form.get("purchased_on", ""),
                                    "total": form.get("total", ""), "accept": data.accept_mismatch})
    return RedirectResponse(f"/receipts/{receipt.id}", status_code=303)


@router.post("/{receipt_id}/retry")
def retry(receipt_id: int, db: Session = Depends(get_db)):
    receipt = _get_receipt(db, receipt_id)
    retry_receipt(db, receipt)
    return RedirectResponse(f"/receipts/{receipt.id}", status_code=303)


@router.post("/{receipt_id}/delete")
def delete(receipt_id: int, request: Request, db: Session = Depends(get_db)):
    delete_receipt(db, request.app.state.settings, _get_receipt(db, receipt_id))
    return RedirectResponse("/receipts", status_code=303)
