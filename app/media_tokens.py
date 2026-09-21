"""Single-use, unauthenticated media links used only so Qwen's servers can fetch a
converted video/clip over HTTPS. Tokens are random, job-scoped, short-lived, and are
explicitly released (row + file deleted) by the pipeline as soon as each Qwen call
that needed them finishes — success or failure. A periodic sweep catches anything
left behind by a crash.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from app.config import MEDIA_TOKEN_TTL_SECONDS
from app.db import get_conn, now_iso, tx


def create_token(job_id: str, file_path: Path, content_type: str, ttl_seconds: int = MEDIA_TOKEN_TTL_SECONDS) -> str:
    token = secrets.token_urlsafe(32)
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).isoformat()
    with tx() as conn:
        conn.execute(
            "INSERT INTO media_tokens(token, job_id, file_path, content_type, created_at, expires_at, consumed) "
            "VALUES (?, ?, ?, ?, ?, ?, 0)",
            (token, job_id, str(file_path), content_type, now_iso(), expires_at),
        )
    return token


def resolve_token(token: str) -> Optional[dict]:
    row = get_conn().execute("SELECT * FROM media_tokens WHERE token = ?", (token,)).fetchone()
    if row is None:
        return None
    if datetime.fromisoformat(row["expires_at"]) < datetime.now(timezone.utc):
        release_token(token)
        return None
    return dict(row)


def release_token(token: str, delete_file: bool = True) -> None:
    row = get_conn().execute("SELECT file_path FROM media_tokens WHERE token = ?", (token,)).fetchone()
    with tx() as conn:
        conn.execute("DELETE FROM media_tokens WHERE token = ?", (token,))
    if row and delete_file:
        _safe_delete(Path(row["file_path"]))


def release_all_for_job(job_id: str, delete_file: bool = True) -> None:
    rows = get_conn().execute("SELECT token FROM media_tokens WHERE job_id = ?", (job_id,)).fetchall()
    for r in rows:
        release_token(r["token"], delete_file=delete_file)


def sweep_expired() -> int:
    now = datetime.now(timezone.utc).isoformat()
    rows = get_conn().execute("SELECT token FROM media_tokens WHERE expires_at < ?", (now,)).fetchall()
    for r in rows:
        release_token(r["token"])
    return len(rows)


def _safe_delete(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass
