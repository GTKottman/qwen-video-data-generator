from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app import app_settings, system_check
from app.auth import create_user, get_user_by_username
from app.db import any_user_exists

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
    return RedirectResponse("/setup/hosting", status_code=303)


@router.get("/setup/hosting")
def setup_hosting_form(request: Request):
    return templates.TemplateResponse(request, "setup_hosting.html", {
        "error": None, "public_base_url": app_settings.get_public_base_url(),
    })


@router.post("/setup/hosting")
def setup_hosting_submit(request: Request, public_base_url: str = Form(...)):
    public_base_url = public_base_url.strip().rstrip("/")
    error = None
    if not public_base_url.startswith("https://") and not public_base_url.startswith("http://"):
        error = "Public base URL must start with http:// or https://."

    if error:
        return templates.TemplateResponse(request, "setup_hosting.html", {
            "error": error, "public_base_url": public_base_url,
        }, status_code=400)

    app_settings.set_public_base_url(public_base_url)
    app_settings.mark_setup_complete()
    return RedirectResponse("/", status_code=303)
