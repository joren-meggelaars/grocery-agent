from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from grocery.capture.service import local_date
from grocery.db.base import utcnow
from grocery.db.models import Product
from grocery.receipts.service import ConfirmError
from grocery.security.deps import Principal, current_principal, get_db
from grocery.settings_store import effective
from grocery.shopping import service
from grocery.web.templating import templates

pages = APIRouter(prefix="/shopping-list")
api = APIRouter(prefix="/api/shopping-list")

BASIS_LABEL = {"pack": "per pack", "kg": "per kg", "l": "per l"}
# Messages that may be shown from the query string (anything else is ignored, so the URL cannot carry text).
KNOWN_ERRORS = {"Enter a product name.", "Unknown product."}


def _safe_next(value: str | None) -> str:
    if value and value.startswith("/") and not value.startswith("//") and "\\" not in value:
        return value
    return "/shopping-list"


def _product_names(db: Session) -> list[str]:
    return list(db.scalars(select(Product.name).order_by(Product.name).limit(3000)))


@pages.get("", response_class=HTMLResponse)
def list_page(
    request: Request, error: str | None = None,
    principal: Principal = Depends(current_principal), db: Session = Depends(get_db),
):
    today = local_date(utcnow(), effective(db, request.app.state.settings).timezone)
    return templates.TemplateResponse(
        request, "shopping.html",
        {
            "user": principal.user, "csrf_token": principal.csrf_token, "rows": service.rows(db, today),
            "basis_label": BASIS_LABEL, "product_names": _product_names(db),
            "error": error if error in KNOWN_ERRORS else None,
        },
    )


@pages.post("/add")
def add(product: str = Form(""), note: str = Form(""), db: Session = Depends(get_db)):
    try:
        service.add_by_name(db, product, note)
    except ConfirmError as exc:
        return RedirectResponse(f"/shopping-list?error={quote(exc.messages[0])}", status_code=303)
    service.queue_ha_sync(db)
    return RedirectResponse("/shopping-list", status_code=303)


@pages.post("/add-product")
def add_product(product_id: int = Form(...), next: str = Form("/shopping-list"), db: Session = Depends(get_db)):
    try:
        service.add_by_product(db, product_id)
    except ConfirmError as exc:
        return RedirectResponse(f"{_safe_next(next)}?error={quote(exc.messages[0])}", status_code=303)
    service.queue_ha_sync(db)
    return RedirectResponse(_safe_next(next), status_code=303)


@pages.post("/items/{item_id}/bought")
def mark_bought(item_id: int, db: Session = Depends(get_db)):
    service.toggle_bought(db, item_id)
    service.queue_ha_sync(db)
    return RedirectResponse("/shopping-list", status_code=303)


@pages.post("/items/{item_id}/remove")
def remove_item(item_id: int, db: Session = Depends(get_db)):
    service.remove(db, item_id)
    service.queue_ha_sync(db)
    return RedirectResponse("/shopping-list", status_code=303)


@pages.post("/clear-bought")
def clear_bought(db: Session = Depends(get_db)):
    service.clear_bought(db)
    service.queue_ha_sync(db)
    return RedirectResponse("/shopping-list", status_code=303)


@api.post("/scan")
async def scan(ean: str = Form(""), db: Session = Depends(get_db)):
    try:
        item = service.add_by_ean(db, ean)
    except service.InvalidBarcode as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    service.queue_ha_sync(db)
    return {"name": item.raw_name}
