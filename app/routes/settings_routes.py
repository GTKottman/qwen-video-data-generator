from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app import app_settings
from app.auth import current_user, hash_password, verify_password
from app.db import tx

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _ctx(request: Request, **extra):
    base = {
        "user": current_user(request),
        "public_base_url": app_settings.get_public_base_url(),
        "hosting_message": None,
        "hosting_error": None,
        "password_message": None,
        "password_error": None,
    }
    base.update(extra)
    return base


@router.get("/settings")
def settings_page(request: Request):
    return templates.TemplateResponse(request, "settings.html", _ctx(request))


@router.post("/settings/hosting-url")
def update_hosting_url(request: Request, public_base_url: str = Form(...)):
    public_base_url = public_base_url.strip().rstrip("/")
    error = None
    if not (public_base_url.startswith("http://") or public_base_url.startswith("https://")):
        error = "Public base URL must start with http:// or https://."

    if error:
        return templates.TemplateResponse(request, "settings.html", _ctx(request, hosting_error=error), status_code=400)

    app_settings.set_public_base_url(public_base_url)
    return templates.TemplateResponse(request, "settings.html", _ctx(request, hosting_message="Saved."))


@router.post("/settings/password")
def update_password(
    request: Request, current_password: str = Form(...), new_password: str = Form(...), confirm: str = Form(...),
):
    user = current_user(request)
    error = None
    if not user or not verify_password(current_password, user["password_hash"]):
        error = "Current password is incorrect."
    elif len(new_password) < 8:
        error = "New password must be at least 8 characters."
    elif new_password != confirm:
        error = "New passwords do not match."

    if error:
        return templates.TemplateResponse(request, "settings.html", _ctx(request, password_error=error), status_code=400)

    with tx() as conn:
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(new_password), user["id"]))
    return templates.TemplateResponse(request, "settings.html", _ctx(request, password_message="Password updated."))
