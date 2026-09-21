from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.app_settings import is_setup_complete
from app.auth import current_user
from app.config import APP_SECRET
from app.db import any_user_exists, init_db
from app.routes import auth_routes, dashboard, media, settings_routes, setup

app = FastAPI(title="Qwen Video Host")
app.mount("/static", StaticFiles(directory="app/static"), name="static")

PUBLIC_PREFIXES = ("/static/", "/m/")
PUBLIC_EXACT = {"/health", "/favicon.ico"}


@app.on_event("startup")
def _startup() -> None:
    init_db()


@app.middleware("http")
async def auth_gate(request: Request, call_next):
    path = request.url.path
    if path.startswith(PUBLIC_PREFIXES) or path in PUBLIC_EXACT:
        return await call_next(request)

    if not any_user_exists():
        if not path.startswith("/setup"):
            return RedirectResponse("/setup/account", status_code=303)
        return await call_next(request)

    if path.startswith("/setup") and path != "/setup/account":
        if not current_user(request):
            return RedirectResponse("/login", status_code=303)
        return await call_next(request)
    if path == "/setup/account":
        return RedirectResponse("/login", status_code=303)

    if path == "/login":
        return await call_next(request)

    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    if not is_setup_complete() and not path.startswith("/setup"):
        return RedirectResponse("/setup/check", status_code=303)

    return await call_next(request)


# Registered after auth_gate so it ends up OUTERMOST in the middleware stack (Starlette
# wraps in reverse-registration order) — it must run before auth_gate touches
# request.session, or every request 500s.
app.add_middleware(SessionMiddleware, secret_key=APP_SECRET, same_site="lax", https_only=False)

app.include_router(setup.router)
app.include_router(auth_routes.router)
app.include_router(settings_routes.router)
app.include_router(dashboard.router)
app.include_router(media.router)


@app.get("/health")
def health():
    return {"status": "ok"}
