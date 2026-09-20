import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from grocery.capture import forms
from grocery.capture.service import (
    categories,
    confirm_capture,
    create_capture,
    delete_capture,
    local_date,
    retry_capture,
)
from grocery.db.base import utcnow
from grocery.db.models import Product, ShelfCapture, Store
from grocery.money import format_cents
from grocery.prices.feedback import feedback_for
from grocery.prices.observations import shelf_comparable
from grocery.receipts.service import ConfirmError
from grocery.security.deps import Principal, current_principal, get_db
from grocery.uploads.images import UploadError
from grocery.uploads.storage import read_image
from grocery.web.templating import templates

log = logging.getLogger(__name__)
pages = APIRouter(prefix="/capture")
api = APIRouter(prefix="/api")

STATUS_LABEL = {"extracting": "Reading...", "needs_review": "Needs review", "confirmed": "Saved", "failed": "Failed"}
BASIS_LABEL = {"pack": "per pack", "kg": "per kg", "l": "per l"}


def _get(db: Session, capture_id: int) -> ShelfCapture:
    capture = db.get(ShelfCapture, capture_id)
    if capture is None:
        raise HTTPException(404, "Capture not found")
    return capture


# --- API used by the phone (also from the offline outbox) --------------------

@api.get("/csrf")
def csrf(principal: Principal = Depends(current_principal)):
    """The outbox fetches a fresh token right before it uploads, so a queued capture never carries a stale one."""
    return {"token": principal.csrf_token}


def _parse_when(value: str | None) -> datetime | None:
    try:
        moment = datetime.fromisoformat((value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


@api.post("/captures")
async def upload_capture(
    request: Request,
    client_uuid: str = Form(""),
    ean: str = Form(""),
    store: str = Form(""),
    captured_at: str = Form(""),
    photo: UploadFile | None = File(default=None),
    db: Session = Depends(get_db),
):
    settings = request.app.state.settings
    data = await photo.read() if photo is not None else b""
    try:
        capture, created = await run_in_threadpool(
            create_capture, db, settings, client_uuid=client_uuid, ean_text=ean, store_chain=store,
            captured_at=_parse_when(captured_at), photo=data,
        )
    except UploadError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"id": capture.id, "created": created, "status": capture.status},
                        status_code=201 if created else 200)


# --- pages -------------------------------------------------------------------

@pages.get("", response_class=HTMLResponse)
def scan_page(request: Request, db: Session = Depends(get_db)):
    """Identical for everyone (no user data, no CSRF token) so the service worker can cache it for offline use."""
    stores = db.scalars(select(Store).order_by(Store.id)).all()
    return templates.TemplateResponse(request, "capture/scan.html", {"stores": stores})


@pages.get("/queue", response_class=HTMLResponse)
def queue_page(
    request: Request, principal: Principal = Depends(current_principal), db: Session = Depends(get_db)
):
    captures = db.scalars(select(ShelfCapture).order_by(ShelfCapture.id.desc()).limit(60)).all()
    return templates.TemplateResponse(
        request, "capture/queue.html",
        {"user": principal.user, "csrf_token": principal.csrf_token, "captures": captures,
         "labels": STATUS_LABEL, "fmt": format_cents},
    )


def _feedback(db: Session, request: Request, capture: ShelfCapture):
    comp = shelf_comparable(capture)
    if capture.status != "confirmed" or comp is None or capture.product_id is None or capture.store_id is None:
        return None
    settings = request.app.state.settings
    basis, value = comp
    return feedback_for(
        db, capture.product_id, capture.store_id, basis, value,
        local_date(utcnow(), settings.timezone), exclude=("shelf", capture.id),
    )


def _detail(request, principal, db, capture, values=None, errors=None, status=200, manual=False):
    settings = request.app.state.settings
    default_store = capture.store.chain if capture.store else "other"
    return templates.TemplateResponse(
        request, "capture/detail.html",
        {
            "user": principal.user, "csrf_token": principal.csrf_token, "capture": capture,
            "labels": STATUS_LABEL, "fmt": format_cents, "errors": errors or [], "manual": manual,
            "values": values or forms.values_from_capture(capture, default_store),
            "stores": db.scalars(select(Store).order_by(Store.id)).all(),
            "categories": categories(db),
            "product_names": db.scalars(select(Product.name).order_by(Product.name).limit(3000)).all(),
            "feedback": _feedback(db, request, capture), "basis_label": BASIS_LABEL,
            "notes": (capture.extraction.raw_json or {}).get("legibility_notes") if capture.extraction else None,
            "has_photo": bool(capture.file and capture.file.path),
            "settings": settings,
        },
        status_code=status,
    )


@pages.get("/{capture_id}", response_class=HTMLResponse)
def capture_detail(
    capture_id: int, request: Request, manual: bool = False,
    principal: Principal = Depends(current_principal), db: Session = Depends(get_db),
):
    return _detail(request, principal, db, _get(db, capture_id), manual=manual)


@pages.get("/{capture_id}/status")
def capture_status(capture_id: int, db: Session = Depends(get_db)):
    return JSONResponse({"status": _get(db, capture_id).status})


@pages.get("/{capture_id}/image")
def capture_image(capture_id: int, request: Request, db: Session = Depends(get_db)):
    capture = _get(db, capture_id)
    data = read_image(request.app.state.settings, capture.file) if capture.file else None
    if data is None:
        raise HTTPException(404, "Photo not available (photos are removed a week after saving)")
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "private, no-store"})


@pages.post("/{capture_id}/confirm", response_class=HTMLResponse)
async def confirm(
    capture_id: int, request: Request,
    principal: Principal = Depends(current_principal), db: Session = Depends(get_db),
):
    capture = _get(db, capture_id)
    if capture.status not in ("needs_review", "failed", "confirmed"):
        raise HTTPException(409, "This capture is still being read")
    form = await request.form()
    data, values, errors = forms.parse_capture_form(form)
    if not errors:
        try:
            await run_in_threadpool(confirm_capture, db, request.app.state.settings, capture, data)
        except ConfirmError as exc:
            errors = exc.messages
    if errors:
        return _detail(request, principal, db, capture, values=values, errors=errors, status=422, manual=True)
    return RedirectResponse(f"/capture/{capture.id}", status_code=303)


@pages.post("/{capture_id}/retry")
def retry(capture_id: int, db: Session = Depends(get_db)):
    capture = _get(db, capture_id)
    retry_capture(db, capture)
    return RedirectResponse(f"/capture/{capture.id}", status_code=303)


@pages.post("/{capture_id}/delete")
def delete(capture_id: int, request: Request, db: Session = Depends(get_db)):
    delete_capture(db, request.app.state.settings, _get(db, capture_id))
    return RedirectResponse("/capture/queue", status_code=303)
