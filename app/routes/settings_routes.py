from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app import app_settings
from app.auth import current_user, hash_password, verify_password
from app.config import QWEN_BASE_URLS
from app.db import tx
from app.qwen_client import QwenAPIError, build_client, test_connection

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _ctx(request: Request, **extra):
    base = {
        "user": current_user(request),
        "region": app_settings.get_qwen_region(),
        "regions": QWEN_BASE_URLS,
        "public_base_url": app_settings.get_public_base_url(),
        "has_api_key": bool(app_settings.get_qwen_api_key()),
        "api_message": None,
        "api_error": None,
        "password_message": None,
        "password_error": None,
    }
    base.update(extra)
    return base


@router.get("/settings")
def settings_page(request: Request):
    return templates.TemplateResponse(request, "settings.html", _ctx(request))


@router.post("/settings/api-key")
def update_api_key(
    request: Request, api_key: str = Form(...), region: str = Form(...), public_base_url: str = Form(...),
):
    api_key = api_key.strip()
    public_base_url = public_base_url.strip().rstrip("/")
    error = None
    if region not in QWEN_BASE_URLS:
        error = "Unknown region."
    elif not api_key:
        error = "API key is required."
    elif not (public_base_url.startswith("http://") or public_base_url.startswith("https://")):
        error = "Public base URL must start with http:// or https://."

    if not error:
        try:
            client = build_client(api_key, QWEN_BASE_URLS[region])
            test_connection(client)
        except QwenAPIError as exc:
            error = f"Could not verify this API key: {exc}"

    if error:
        return templates.TemplateResponse(request, "settings.html", _ctx(request, api_error=error), status_code=400)

    app_settings.set_qwen_api_key(api_key)
    app_settings.set_qwen_region(region)
    app_settings.set_public_base_url(public_base_url)
    return templates.TemplateResponse(request, "settings.html", _ctx(request, api_message="Saved and verified."))


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
