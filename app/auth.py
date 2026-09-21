import bcrypt
from fastapi import Request
from starlette.responses import RedirectResponse

from app.db import get_conn, now_iso, tx


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except ValueError:
        return False


def create_user(username: str, password: str) -> None:
    with tx() as conn:
        conn.execute(
            "INSERT INTO users(username, password_hash, created_at) VALUES (?, ?, ?)",
            (username, hash_password(password), now_iso()),
        )


def get_user_by_username(username: str):
    return get_conn().execute(
        "SELECT * FROM users WHERE username = ?", (username,)
    ).fetchone()


def current_user(request: Request):
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    return get_conn().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def login_required(request: Request):
    """Returns a RedirectResponse if not authenticated, else None."""
    if not current_user(request):
        return RedirectResponse(url="/login", status_code=303)
    return None
