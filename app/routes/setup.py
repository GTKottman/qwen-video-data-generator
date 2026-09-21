from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app import app_settings, system_check
from app.auth import create_user, get_user_by_username
from app.config import QWEN_BASE_URLS
from app.db import any_user_exists
from app.qwen_client import QwenAPIError, build_client, test_connection

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/setup/account")
def setup_account_form(request: Request):
    if any_user_exists():
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(request, "setup_account.html", {"error": None})


@router.post("/setup/account")
def setup_account_submit(
    request: Request, username: str = Form(...), password: str = Form(...), confirm: str = Form(...)
):
    if any_user_exists():
        return RedirectResponse("/login", status_code=303)
    username = username.strip()
    error = None
    if len(username) < 3:
        error = "Username must be at least 3 characters."
    elif len(password) < 8:
        error = "Password must be at least 8 characters."
    elif password != confirm:
        error = "Passwords do not match."
    elif get_user_by_username(username):
        error = "That username is taken."
    if error:
        return templates.TemplateResponse(request, "setup_account.html", {"error": error}, status_code=400)

    create_user(username, password)
    user = get_user_by_username(username)
    request.session["user_id"] = user["id"]
    return RedirectResponse("/setup/check", status_code=303)


@router.get("/setup/check")
def setup_check(request: Request):
    results = system_check.run_all()
    all_ok = all(r.ok for r in results if r.name != "disk space")  # disk space is advisory only
    return templates.TemplateResponse(request, "setup_check.html", {"results": results, "all_ok": all_ok})


@router.post("/setup/check/continue")
def setup_check_continue():
    return RedirectResponse("/setup/apikey", status_code=303)


@router.get("/setup/apikey")
def setup_apikey_form(request: Request):
    return templates.TemplateResponse(request, "setup_apikey.html", {
        "error": None, "regions": QWEN_BASE_URLS,
        "public_base_url": app_settings.get_public_base_url(),
    })


@router.post("/setup/apikey")
def setup_apikey_submit(
    request: Request, api_key: str = Form(...), region: str = Form(...), public_base_url: str = Form(...),
):
    api_key = api_key.strip()
    public_base_url = public_base_url.strip().rstrip("/")
    error = None
    if region not in QWEN_BASE_URLS:
        error = "Unknown region."
    elif not api_key:
        error = "API key is required."
    elif not public_base_url.startswith("https://") and not public_base_url.startswith("http://"):
        error = "Public base URL must start with http:// or https://."

    if not error:
        try:
            client = build_client(api_key, QWEN_BASE_URLS[region])
            test_connection(client)
        except QwenAPIError as exc:
            error = f"Could not verify this API key: {exc}"

    if error:
        return templates.TemplateResponse(request, "setup_apikey.html", {
            "error": error, "regions": QWEN_BASE_URLS, "public_base_url": public_base_url,
        }, status_code=400)

    app_settings.set_qwen_api_key(api_key)
    app_settings.set_qwen_region(region)
    app_settings.set_public_base_url(public_base_url)
    app_settings.mark_setup_complete()
    return RedirectResponse("/", status_code=303)
