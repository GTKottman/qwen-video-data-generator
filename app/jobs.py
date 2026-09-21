from __future__ import annotations

from typing import Any, Optional

from app.db import get_conn, now_iso, tx

VALID_COLUMNS = {
    "status", "stage_detail", "updated_at", "source_duration_ms", "clipped",
    "output_duration_ms", "output_size_bytes", "output_resolution", "thumbnail_path",
    "error_message", "chapters_count", "events_count", "safety_rating", "dataset_dir",
}


def create_job(job_id: str, original_filename: str) -> None:
    ts = now_iso()
    with tx() as conn:
        conn.execute(
            "INSERT INTO jobs(id, original_filename, status, created_at, updated_at) "
            "VALUES (?, ?, 'queued', ?, ?)",
            (job_id, original_filename, ts, ts),
        )


def update_job(job_id: str, **fields: Any) -> None:
    fields["updated_at"] = now_iso()
    bad = set(fields) - VALID_COLUMNS
    if bad:
        raise ValueError(f"unknown job columns: {bad}")
    cols = ", ".join(f"{k} = ?" for k in fields)
    with tx() as conn:
        conn.execute(f"UPDATE jobs SET {cols} WHERE id = ?", (*fields.values(), job_id))


def set_status(job_id: str, status: str, stage_detail: str = "") -> None:
    update_job(job_id, status=status, stage_detail=stage_detail)


def add_log(job_id: str, level: str, message: str) -> None:
    with tx() as conn:
        conn.execute(
            "INSERT INTO job_logs(job_id, ts, level, message) VALUES (?, ?, ?, ?)",
            (job_id, now_iso(), level, message),
        )


def get_job(job_id: str) -> Optional[dict]:
    row = get_conn().execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return dict(row) if row else None


def list_jobs(limit: int = 200) -> list[dict]:
    rows = get_conn().execute(
        "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_logs(job_id: str, limit: int = 2000) -> list[dict]:
    rows = get_conn().execute(
        "SELECT * FROM job_logs WHERE job_id = ? ORDER BY id ASC LIMIT ?", (job_id, limit)
    ).fetchall()
    return [dict(r) for r in rows]
