"""Permanent hosted video files, served publicly and unauthenticated at /m/<id> so an
external client (e.g. the Qwen client app) can fetch them by URL. Unlike the old
media_tokens (single-use, short-lived, created/destroyed per Qwen call), these live
until the user deletes them from the dashboard.
"""
from __future__ import annotations

import secrets
from pathlib import Path
from typing import Optional

from app.db import get_conn, now_iso, tx


def create(
    original_filename: str,
    stored_path: Path,
    content_type: str,
    size_bytes: int,
    duration_ms: Optional[int],
    width: Optional[int],
    height: Optional[int],
    thumbnail_path: Optional[Path] = None,
) -> str:
    file_id = secrets.token_urlsafe(16)
    with tx() as conn:
        conn.execute(
            "INSERT INTO hosted_files("
            "id, original_filename, stored_path, content_type, size_bytes, "
            "duration_ms, width, height, thumbnail_path, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                file_id, original_filename, str(stored_path), content_type, size_bytes,
                duration_ms, width, height, str(thumbnail_path) if thumbnail_path else None,
                now_iso(),
            ),
        )
    return file_id


def get(file_id: str) -> Optional[dict]:
    row = get_conn().execute("SELECT * FROM hosted_files WHERE id = ?", (file_id,)).fetchone()
    return dict(row) if row else None


def list_all() -> list[dict]:
    rows = get_conn().execute("SELECT * FROM hosted_files ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]


def delete(file_id: str) -> None:
    row = get_conn().execute("SELECT * FROM hosted_files WHERE id = ?", (file_id,)).fetchone()
    if row is None:
        return
    with tx() as conn:
        conn.execute("DELETE FROM hosted_files WHERE id = ?", (file_id,))
    _safe_delete(Path(row["stored_path"]))
    if row["thumbnail_path"]:
        _safe_delete(Path(row["thumbnail_path"]))


def _safe_delete(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass
