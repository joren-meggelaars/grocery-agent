from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from grocery.security.deps import Principal, current_principal, get_db
from grocery.settings_store import FIELDS, SettingsError, effective, parse_and_save
from grocery.web.templating import templates

router = APIRouter()


def _values(eff) -> dict[str, str]:
    # %g for floats: "450" instead of "450.0", still "450.5" where that matters.
    return {f.key: (f"{getattr(eff, f.key):g}" if f.kind == "float" else str(getattr(eff, f.key))) for f in FIELDS}


@router.get("/settings", response_class=HTMLResponse)
def settings_page(
    request: Request, saved: bool = False,
    principal: Principal = Depends(current_principal), db: Session = Depends(get_db),
):
    eff = effective(db, request.app.state.settings)
    return templates.TemplateResponse(
        request, "settings.html",
        {"user": principal.user, "csrf_token": principal.csrf_token, "fields": FIELDS,
         "values": _values(eff), "errors": [], "saved": saved},
    )


@router.post("/settings", response_class=HTMLResponse)
async def settings_save(
    request: Request, principal: Principal = Depends(current_principal), db: Session = Depends(get_db)
):
    settings = request.app.state.settings
    form = await request.form()
    errors: list[str] = []
    values = {f.key: (form.get(f.key) or "").strip() for f in FIELDS}
    try:
        parse_and_save(db, settings, form)
    except SettingsError as exc:
        errors = exc.messages
    if errors:
        return templates.TemplateResponse(
            request, "settings.html",
            {"user": principal.user, "csrf_token": principal.csrf_token, "fields": FIELDS,
             "values": values, "errors": errors, "saved": False},
            status_code=422,
        )
    return RedirectResponse("/settings?saved=1", status_code=303)
