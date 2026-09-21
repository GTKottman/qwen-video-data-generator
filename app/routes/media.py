"""Public, unauthenticated route — intentionally not behind login (see main.py's
auth_gate). Serves permanently hosted files so an external client (e.g. the Qwen
client app) can fetch a video by its /m/<id> URL at any time.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app import media_store

router = APIRouter()


@router.get("/m/{file_id}")
def fetch_media(file_id: str):
    row = media_store.get(file_id)
    if not row:
        raise HTTPException(404)
    path = Path(row["stored_path"])
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type=row["content_type"])
