import logging

from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from grocery.analytics.charts import eur
from grocery.capture.service import local_date
from grocery.cupboard import service
from grocery.db.base import utcnow
from grocery.db.models import CupboardItem, CupboardScan, Product
from grocery.prices.observations import comparable
from grocery.receipts.service import ConfirmError
from grocery.security.deps import Principal, current_principal, get_db
from grocery.web.templating import templates

log = logging.getLogger(__name__)
pages = APIRouter(prefix="/cupboard")
api = APIRouter(prefix="/api/cupboard")

BASIS_LABEL = {"pack": "per pack", "kg": "per kg", "l": "per l"}
# Messages that may be shown from the query string (anything else is ignored, so the URL cannot carry text).
KNOWN_ERRORS = {"Enter the product name.", "That barcode was already handled."}


def _ctx(principal: Principal, **extra) -> dict:
    return {"user": principal.user, "csrf_token": principal.csrf_token, "eur": eur, **extra}


def _product_names(db: Session) -> list[str]:
    return list(db.scalars(select(Product.name).order_by(Product.name).limit(3000)))


# --- API used by the scan page ------------------------------------------------

@api.post("/scan")
async def scan(request: Request, ean: str = Form(""), db: Session = Depends(get_db)):
    try:
        outcome = await run_in_threadpool(service.scan, db, ean)
    except service.InvalidBarcode as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return {"result": outcome.kind, "name": outcome.name}


# --- pages -------------------------------------------------------------------

@pages.get("", response_class=HTMLResponse)
def cupboard_list(
    request: Request, error: str | None = None,
    principal: Principal = Depends(current_principal), db: Session = Depends(get_db),
):
    today = local_date(utcnow(), request.app.state.settings.timezone)
    return templates.TemplateResponse(
        request, "cupboard/list.html",
        _ctx(principal, rows=service.cupboard_rows(db, today), waiting=len(service.pending_scans(db)),
             categories=service.categories(db), product_names=_product_names(db), basis_label=BASIS_LABEL,
             error=error if error in KNOWN_ERRORS else None),
    )


@pages.post("/add")
def add_by_name(
    product: str = Form(""), category: str = Form(""), heavy: str = Form(""), db: Session = Depends(get_db)
):
    try:
        service.add_product(db, product, int(category) if category.isdigit() else None, bool(heavy))
    except ConfirmError as exc:
        return RedirectResponse(f"/cupboard?error={quote(exc.messages[0])}", status_code=303)
    return RedirectResponse("/cupboard", status_code=303)


@pages.get("/scan", response_class=HTMLResponse)
def scan_page(
    request: Request, principal: Principal = Depends(current_principal), db: Session = Depends(get_db)
):
    return templates.TemplateResponse(
        request, "cupboard/scan.html", _ctx(principal, waiting=len(service.pending_scans(db)))
    )


@pages.get("/name", response_class=HTMLResponse)
def name_page(
    request: Request, error: str | None = None,
    principal: Principal = Depends(current_principal), db: Session = Depends(get_db),
):
    pending = []
    for p in service.pending_scans(db):
        name, category_id = service.suggestion(db, p)
        pending.append({"scan": p, "name": name, "category_id": category_id})
    return templates.TemplateResponse(
        request, "cupboard/name.html",
        _ctx(principal, pending=pending, categories=service.categories(db), product_names=_product_names(db),
             error=error if error in KNOWN_ERRORS else None),
    )


@pages.post("/name/{scan_id}")
def name_one(
    scan_id: int, product: str = Form(""), category: str = Form(""), heavy: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        service.name_scan(db, scan_id, product, int(category) if category.isdigit() else None, bool(heavy))
    except ConfirmError as exc:
        return RedirectResponse(f"/cupboard/name?error={quote(exc.messages[0])}", status_code=303)
    return RedirectResponse("/cupboard/name", status_code=303)


@pages.post("/name/{scan_id}/discard")
def discard(scan_id: int, db: Session = Depends(get_db)):
    service.discard_scan(db, scan_id)
    return RedirectResponse("/cupboard/name", status_code=303)


def _item(db: Session, item_id: int) -> CupboardItem:
    item = db.get(CupboardItem, item_id)
    if item is None:
        raise HTTPException(404, "Not on your list")
    return item


@pages.post("/items/{item_id}/heavy")
def heavy(item_id: int, value: str = Form("1"), db: Session = Depends(get_db)):
    item = _item(db, item_id)
    service.set_heavy(db, item.id, value == "1")
    return RedirectResponse("/cupboard", status_code=303)


@pages.post("/items/{item_id}/remove")
def remove(item_id: int, db: Session = Depends(get_db)):
    service.remove_item(db, _item(db, item_id).id)
    return RedirectResponse("/cupboard", status_code=303)
